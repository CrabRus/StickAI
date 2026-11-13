// StickAI for VS Code: a link to the StickAI desktop app (127.0.0.1 only).
// Explain code (Ctrl+Alt+E), comment it (Ctrl+Alt+C, shown as a diff to accept), ask questions
// in the panel, "What's this error?" from the light bulb. Code leaves the editor only on such a
// request (or when you ask in the StickAI chat while VS Code is in use).
'use strict';

const vscode = require('vscode');
const net = require('net');
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const cp = require('child_process');

const RETRY_MS = 4000;
const MAX_LINES = 800;
const MAX_CHARS = 60000;
const CONTEXT_LINES = 40;
const ERROR_NEAR_LINES = 3;
const MAX_HISTORY = 6;
const SCHEME = 'stickai-proposal';
const SYMBOL_KINDS = new Set([
  vscode.SymbolKind.Function, vscode.SymbolKind.Method, vscode.SymbolKind.Constructor,
  vscode.SymbolKind.Class, vscode.SymbolKind.Interface, vscode.SymbolKind.Struct, vscode.SymbolKind.Enum,
]);
const RU = (vscode.env.language || '').startsWith('ru');
const T = {
  notRunning: RU ? 'StickAI не запущен: запусти его и попробуй ещё раз.' : 'StickAI is not running: start it, then try again.',
  retry: RU ? 'Повторить' : 'Retry',
  openFile: RU ? 'StickAI: открой файл и выдели код.' : 'StickAI: open a file and select some code first.',
  whatError: RU ? 'StickAI: что за ошибка?' : "StickAI: What's this error?",
  disconnected: RU ? 'StickAI не запущен (нажми, чтобы переподключиться)' : 'StickAI is not running (click to retry)',
  reviewing: RU ? 'Стикмены смотрят код…' : 'The stickmen are reviewing…',
  noRun: RU ? 'StickAI: в терминале VS Code пока ничего не падало, и ошибок в файле нет.' : "StickAI: nothing has failed in VS Code's terminal yet, and the file has no errors.",
  cantRun: RU ? 'StickAI умеет запускать .c, .cpp, .py и .js.' : 'StickAI can run .c, .cpp, .py and .js files.',
  whyFailed: RU ? 'Почему упала команда' : 'Why did this fail:',
};
const HELP_RU = [
  '## Что я умею',
  '',
  '**Команды**: пиши в поле внизу или жми клавиши. Код — выделенный, или функция под курсором, или весь файл.',
  '',
  '- `/explain` — объяснить код · **Ctrl+Alt+E**',
  '- `/comment` — прокомментировать: покажу изменения, примешь кнопкой · **Ctrl+Alt+C**',
  '- `/hint` — подсказка, а не ответ: 1/3 → 2/3 → 3/3 при повторе · **Ctrl+Alt+H**',
  '- `/quiz` — 3 вопроса, понимаешь ли ты свой код (ответь здесь же)',
  '- `/review` — ревью от четверых: Синий, Красный, Зелёный, Жёлтый · **Ctrl+Alt+R**',
  '- `/run` — собрать и запустить файл (C, C++, Python, JS), ввод с клавиатуры работает · **Ctrl+Alt+Enter** или ▶',
  '- `/terminal` — что не так в терминале (последняя упавшая команда) · **Ctrl+Alt+T**',
  '- `/error` — что за ошибка у курсора · или лампочка 💡',
  '- `/help` — этот список',
  '',
  '**Просто вопрос** — пиши как обычно: «а почему тут цикл?», «как сделать быстрее?». Я отвечаю про выделенный код и помню прошлые ответы.',
  '',
  '**Сам, без просьбы:**',
  '- что-то упало при запуске или сборке — подсказка в пузыре над стикменом, клик открывает строку;',
  '- одно и то же упало 3 раза — предложу разобрать подробно;',
  '- после сохранения гляну изменённые строки и скажу, только если есть реальная проблема;',
  '- тесты прошли — все радуются, упали — я хватаюсь за лоб.',
  '',
  '**Из чата StickAI** (пока VS Code открыт): «объясни этот код», «прокомментируй функцию», «что за ошибка?», «что в терминале?».',
].join('\n');
const HELP_EN = [
  '## What I can do',
  '',
  '**Commands**: type them in the box below or press the keys. The code is the selection, or the function under the cursor, or the whole file.',
  '',
  '- `/explain` — explain the code · **Ctrl+Alt+E**',
  '- `/comment` — add comments: shown as a diff, you accept it · **Ctrl+Alt+C**',
  '- `/hint` — a hint, not the answer: 1/3 → 2/3 → 3/3 when asked again · **Ctrl+Alt+H**',
  '- `/quiz` — 3 questions to check you understand your code (answer here)',
  '- `/review` — review by the four: Blue, Red, Green, Yellow · **Ctrl+Alt+R**',
  '- `/run` — build and run the file (C, C++, Python, JS), typing input works · **Ctrl+Alt+Enter** or ▶',
  '- `/terminal` — what went wrong in the terminal (the last failed command) · **Ctrl+Alt+T**',
  '- `/error` — what is the error at the cursor · or the light bulb 💡',
  '- `/help` — this list',
  '',
  '**Just ask** as usual: "why the loop here?", "how to make it faster?". I answer about the selected code and remember the last answers.',
  '',
  '**On my own:**',
  '- something fails when you run or build: a hint in a bubble over the stickman, a click opens the line;',
  '- the same thing fails 3 times: I offer a proper look;',
  '- after you save I glance at the changed lines and speak only if something is really wrong;',
  '- tests pass: everyone cheers; they fail: I facepalm.',
  '',
  '**From the StickAI chat** (while VS Code is open): "explain this code", "comment this function", "what is this error?", "what is in the terminal?".',
].join('\n');

const DEFAULT_LABELS = {
  title: '{name}: code', thinking: '{name} is reading the code…', explain_of: 'Explaining', comment_of: 'Comments for',
  error_of: 'Problem in', question: 'You', whole_file: 'whole file', lines: 'lines', stop: 'Stop',
  waiting: 'Select code and press Ctrl+Alt+E to explain it, Ctrl+Alt+C to comment it, or ask a question below.',
  empty: 'Nothing to work on: the selection is empty.', ask_send: 'Ask', writing_comments: '{name} is writing comments…', review: 'Review the changes, then:',
  accept: 'Accept', reject: 'Reject', applied: 'Applied. Ctrl+Z in the editor undoes it.',
  rejected: 'Rejected, nothing changed.', unsafe: 'Careful: this changes more than comments.',
  stale: 'The code was edited in the meantime, so the changes were not applied.',
  no_errors: 'No errors or warnings near the cursor.', copy: 'Copy', copied: 'Copied',
  no_changes: 'Nothing to add: the code is already well commented.',
};

let EXT_VERSION = '';  // this extension's version (StickAI asks for a reload when a newer one is installed)

function infoPath() {
  const base = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local');
  return path.join(base, 'StickAI', 'vscode.json');
}
const norm = (s) => s.replace(/\r\n/g, '\n');

// ------------------------------------------------------------------ the link
class Link {
  constructor(onMessage, onState) {
    this.onMessage = onMessage;
    this.onState = onState;
    this.sock = null;
    this.ready = false;
    this.buf = '';
    this.timer = null;
    this.welcome = null;
    this.disposed = false;
  }

  connect() {
    if (this.sock || this.disposed) return;
    let info;
    try {
      info = JSON.parse(fs.readFileSync(infoPath(), 'utf8'));
    } catch (e) {
      this.retry();
      return;
    }
    const sock = net.createConnection({ host: '127.0.0.1', port: info.port });
    this.sock = sock;
    sock.setEncoding('utf8');
    sock.setNoDelay(true);
    sock.on('connect', () => {
      this.write({ type: 'hello', token: info.token, protocol: 1, version: EXT_VERSION,
        workspace: vscode.workspace.name || '', focused: vscode.window.state.focused });
    });
    sock.on('data', (d) => {
      this.buf += d;
      let i;
      while ((i = this.buf.indexOf('\n')) >= 0) {
        const line = this.buf.slice(0, i);
        this.buf = this.buf.slice(i + 1);
        if (!line.trim()) continue;
        let msg;
        try { msg = JSON.parse(line); } catch (e) { continue; }
        if (msg.type === 'welcome') {
          this.welcome = msg;
          if (!this.ready) { this.ready = true; this.onState(true); }
        }
        this.onMessage(msg);
      }
    });
    sock.on('error', () => { /* 'close' follows */ });
    sock.on('close', () => {
      const was = this.ready;
      this.sock = null;
      this.ready = false;
      this.buf = '';
      if (was) this.onState(false);
      this.retry();
    });
  }

  retry() {
    if (this.timer || this.disposed) return;
    this.timer = setTimeout(() => { this.timer = null; this.connect(); }, RETRY_MS);
  }

  reconnect() {
    if (this.sock) this.sock.destroy();
    else this.connect();
  }

  write(msg) {
    if (!this.sock) return false;
    this.sock.write(JSON.stringify(msg) + '\n');
    return true;
  }

  send(msg) { return this.ready && this.write(msg); }

  dispose() {
    this.disposed = true;
    clearTimeout(this.timer);
    if (this.sock) this.sock.destroy();
  }
}

// ------------------------------------------------------------------ proposals (virtual docs for the diff)
class Proposals {
  constructor() {
    this.docs = new Map();   // uri string -> text
    this.emitter = new vscode.EventEmitter();
    this.onDidChange = this.emitter.event;
  }
  provideTextDocumentContent(uri) { return this.docs.get(uri.toString()) || ''; }
  put(id, side, name, text) {
    const uri = vscode.Uri.from({ scheme: SCHEME, path: `/${id}/${side}/${name}` });
    this.docs.set(uri.toString(), text);
    this.emitter.fire(uri);
    return uri;
  }
  drop(id) {
    for (const k of [...this.docs.keys()]) if (k.includes(`/${id}/`)) this.docs.delete(k);
  }
}

// ------------------------------------------------------------------ picking the code
async function enclosingSymbol(doc, pos) {
  let symbols;
  try {
    symbols = await vscode.commands.executeCommand('vscode.executeDocumentSymbolProvider', doc.uri);
  } catch (e) {
    return null;
  }
  let best = null;
  const walk = (list) => {
    for (const s of list || []) {
      const r = s.range || (s.location && s.location.range);
      if (!r || !r.contains(pos)) continue;
      if (SYMBOL_KINDS.has(s.kind)) best = { name: s.name, range: r };   // innermost wins
      walk(s.children);
    }
  };
  walk(symbols);
  return best;
}

// Lines [startLine, endLine] (0-based, inclusive) of doc as a request.
function slice(doc, startLine, endLine, extra) {
  endLine = Math.min(endLine, doc.lineCount - 1);
  let truncated = false;
  if (endLine - startLine + 1 > MAX_LINES) { endLine = startLine + MAX_LINES - 1; truncated = true; }
  let code = norm(doc.getText(new vscode.Range(startLine, 0, endLine, doc.lineAt(endLine).text.length)));
  if (code.length > MAX_CHARS) { code = code.slice(0, MAX_CHARS); truncated = true; }
  const whole = startLine === 0 && endLine >= doc.lineCount - 1;
  const text = (a, b) => (a > b ? '' : norm(doc.getText(new vscode.Range(a, 0, b, doc.lineAt(b).text.length))));
  return Object.assign({
    code, whole, truncated, symbolName: '',
    before: whole ? '' : text(Math.max(0, startLine - CONTEXT_LINES), startLine - 1),
    after: whole ? '' : text(endLine + 1, Math.min(doc.lineCount - 1, endLine + CONTEXT_LINES)),
    start_line: startLine + 1, end_line: endLine + 1,
    file: vscode.workspace.asRelativePath(doc.uri, false), language: doc.languageId,
  }, extra || {});
}

// The selection; with nothing selected, the function / class under the cursor, else the whole file.
async function pickCode(editor, wholeIfNone = true) {
  const doc = editor.document;
  const sel = editor.selection;
  if (!sel.isEmpty) {
    const end = sel.end.character === 0 && sel.end.line > sel.start.line ? sel.end.line - 1 : sel.end.line;
    return slice(doc, sel.start.line, end);
  }
  const sym = await enclosingSymbol(doc, sel.active);
  if (sym && sym.range.end.line - sym.range.start.line < MAX_LINES) {
    return slice(doc, sym.range.start.line, sym.range.end.line, { symbolName: sym.name });
  }
  return wholeIfNone ? slice(doc, 0, doc.lineCount - 1) : null;
}

// ------------------------------------------------------------------ terminal runs
// Shell integration (VS Code 1.93+) reports each command with its exit code and output.
const ANSI = /\x1b\[[0-9;?]*[ -\/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]/g;
const MAX_RUN_OUTPUT = 60000;

// Where an error points: the last frame in YOUR files (not the standard library / site-packages).
function errorLocation(out, cwd) {
  const found = [];
  const add = (file, line) => {
    if (!file || !line) return;
    let p = file.trim();
    if (!path.isAbsolute(p) && cwd) p = path.join(cwd, p);
    if (/[\/](site-packages|dist-packages|node_modules|lib[\/]python\d|\.venv)[\/]/i.test(p)) return;
    if (!fs.existsSync(p)) return;
    found.push({ file: p, line: +line });
  };
  let m;
  const py = /File "([^"]+)", line (\d+)/g;                                   // Python traceback
  while ((m = py.exec(out))) add(m[1], m[2]);
  const cc = /^((?:[A-Za-z]:)?[^\s:"'][^:\n"']*\.(?:c|h|cc|cpp|cxx|hpp|rs|go|java|cs|ts|tsx|js|mjs|py)):(\d+)(?::\d+)?:?\s*(?:fatal )?error/gim;
  while ((m = cc.exec(out))) add(m[1], m[2]);                               // gcc / clang / tsc / rustc-ish
  const msvc = /^((?:[A-Za-z]:)?[^\s(:][^(\n:]*\.(?:c|h|cpp|hpp|cs))\((\d+)\)\s*:\s*(?:fatal )?error/gim;
  while ((m = msvc.exec(out))) add(m[1], m[2]);                             // MSVC
  const js = /\(?((?:[A-Za-z]:)?[^\s():]+\.(?:js|mjs|cjs|ts)):(\d+):\d+\)?/g;
  while ((m = js.exec(out))) add(m[1], m[2]);                               // node stack
  return found.length ? found[found.length - 1] : null;
}

async function codeAround(file, line) {
  try {
    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(file));
    const a = Math.max(0, line - 1 - 12);
    const b = Math.min(doc.lineCount - 1, line - 1 + 8);
    return { start: a + 1, code: norm(doc.getText(new vscode.Range(a, 0, b, doc.lineAt(b).text.length))) };
  } catch (e) {
    return null;
  }
}

function severityName(s) {
  return ['error', 'warning', 'info', 'hint'][s] || 'error';
}

// ------------------------------------------------------------------ activate
function activate(context) {
  EXT_VERSION = (context.extension && context.extension.packageJSON && context.extension.packageJSON.version) || '';
  let nextId = 1;
  const jobs = new Map();     // id -> { kind, uri, pick, answer, question, docVersion }
  let thread = [];            // [{ q, a }] questions and answers about the current topic
  let topic = null;           // { uri, pick } the code the current thread is about
  const proposals = new Proposals();

  // ---------------- panel
  let panel = null;
  let ready = false;
  let queue = [];
  const labels = () => {
    const w = link.welcome || {};
    const l = Object.assign({}, DEFAULT_LABELS, w.labels || {});
    const name = w.name || 'StickAI';
    for (const k of Object.keys(l)) l[k] = String(l[k]).split('{name}').join(name);
    return { labels: l, name, color: w.color || '#FEC503' };
  };
  const post = (m) => {
    if (!panel) return;
    if (ready) panel.webview.postMessage(m);
    else queue.push(m);
  };
  const ensurePanel = (reveal) => {
    if (panel) {
      if (reveal && !panel.visible) panel.reveal(undefined, true);
      return;
    }
    const media = vscode.Uri.joinPath(context.extensionUri, 'media');
    panel = vscode.window.createWebviewPanel('stickai.panel', labels().labels.title,
      { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true },
      { enableScripts: true, retainContextWhenHidden: true, localResourceRoots: [media] });
    panel.iconPath = vscode.Uri.joinPath(media, 'icon.svg');  // the tab: a stickman with a laptop
    ready = false;
    queue = [];
    const w = panel.webview;
    const nonce = crypto.randomBytes(16).toString('base64');
    w.html = `<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${w.cspSource}; script-src 'nonce-${nonce}';">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="${w.asWebviewUri(vscode.Uri.joinPath(media, 'panel.css'))}">
</head><body>
<header>
  <svg width="40" height="40" viewBox="0 0 48 48" aria-hidden="true">
    <g fill="none" stroke="var(--accent)" stroke-width="3.6" stroke-linecap="round" stroke-linejoin="round">
      <path d="M17 17 Q17.5 24 16 30"/><path d="M16 30 Q13 36 10.5 44"/><path d="M16 30 Q19 37 22.5 43.5"/>
      <path d="M17 20 Q21 25 26 26"/><path d="M17 21 Q22 23 28 21.5"/>
    </g>
    <circle cx="17" cy="10" r="6.6" fill="var(--accent)"/>
    <path d="M22 27 L40.5 27" stroke="#8E949C" stroke-width="2.6" stroke-linecap="round"/>
    <path d="M40.5 27 L44.5 12.5" stroke="#B9BEC5" stroke-width="3.4" stroke-linecap="round"/>
    <path class="screen" d="M39 25.2 L42.6 13.6" stroke="#7FD0FF" stroke-width="1.4" stroke-linecap="round"/>
  </svg>
  <div class="grow"><div class="who" id="who">StickAI</div></div>
  <button id="stop" hidden>Stop</button>
</header>
<div id="thread"><p class="hint" id="hint"></p></div>
<footer><textarea id="ask" rows="1"></textarea><button id="send" class="primary">Ask</button></footer>
<script nonce="${nonce}" src="${w.asWebviewUri(vscode.Uri.joinPath(media, 'panel.js'))}"></script>
</body></html>`;
    panel.onDidDispose(() => {
      for (const [id, j] of jobs) if (j.running) link.send({ type: 'cancel', id });
      panel = null;
    });
    w.onDidReceiveMessage(fromView);
  };
  const refreshLabels = () => {
    if (!panel) return;
    const l = labels();
    panel.title = l.labels.title;
    post({ cmd: 'init', ...l });
  };

  function fromView(m) {
    if (m.cmd === 'ready') {
      ready = true;
      panel.webview.postMessage({ cmd: 'init', ...labels() });
      for (const q of queue.splice(0)) panel.webview.postMessage(q);
    } else if (m.cmd === 'stop') {
      for (const [id, j] of jobs) if (j.running) link.send({ type: 'cancel', id });
    } else if (m.cmd === 'goto') {
      const j = jobs.get(m.id);
      const uri = (j && j.uri) || (topic && topic.uri);
      if (uri) goto(uri, m.line);
    } else if (m.cmd === 'ask') {
      ask(m.text);
    } else if (m.cmd === 'accept') {
      accept(m.id);
    } else if (m.cmd === 'reject') {
      reject(m.id);
    } else if (m.cmd === 'copy') {
      vscode.env.clipboard.writeText(String(m.text || ''));
    }
  }

  function goto(uri, line1) {
    const line = Math.max(0, (line1 | 0) - 1);
    vscode.window.showTextDocument(uri, { viewColumn: vscode.ViewColumn.One, preserveFocus: false }).then((ed) => {
      const r = new vscode.Range(line, 0, line, 0);
      ed.selection = new vscode.Selection(r.start, r.start);
      ed.revealRange(r, vscode.TextEditorRevealType.InCenterIfOutsideViewport);
    }, () => {});
  }

  // ---------------- starting jobs
  const where = (pick) => {
    const L = labels().labels;
    if (pick.whole) return `${pick.file} · ${L.whole_file}`;
    return `${pick.file} · ${pick.symbolName ? pick.symbolName + ' · ' : ''}${L.lines} ${pick.start_line}–${pick.end_line}`;
  };
  const editorNow = () => vscode.window.activeTextEditor
    || vscode.window.visibleTextEditors.find((e) => e.document.uri.scheme === 'file');
  const notRunning = () => {
    vscode.window.showWarningMessage(T.notRunning, T.retry).then((c) => { if (c === T.retry) link.reconnect(); });
  };
  const failed = (speaker, reason, text) => {
    if (speaker) link.send({ type: 'run_failed', speaker, reason });
    else vscode.window.showInformationMessage(text);
  };

  function startJob(kind, editor, pick, speaker, extra) {
    const id = nextId++;
    const L = labels().labels;
    const fresh = kind !== 'ask';
    if (fresh) { thread = []; topic = { uri: editor.document.uri, pick }; }
    jobs.set(id, { kind, uri: editor ? editor.document.uri : (topic && topic.uri), pick, answer: '', running: true,
      question: (extra && extra.question) || '', docVersion: editor ? editor.document.version : 0 });
    ensurePanel(true);
    const label = { explain: L.explain_of, comment: L.comment_of, diagnose: L.error_of, ask: '', quiz: L.quiz_of,
      hint: String(L.hint_of || 'Hint {level}/3 for').split('{level}').join(String((extra && extra.level) || 1)) }[kind];
    post({ cmd: 'begin', id, kind, fresh, question: (extra && extra.question) || '',
      meta: pick && label ? `${label}: ${where(pick)}` : (pick && kind === 'ask' ? where(pick) : '') });
    link.send(Object.assign({ type: kind, id, speaker: speaker || '' }, pick || {}, extra || {}));
    return id;
  }

  async function explain(speaker) {
    const editor = editorNow();
    if (!editor) return failed(speaker, 'no_editor', T.openFile);
    if (!link.ready) return notRunning();
    const pick = await pickCode(editor);
    if (!pick.code.trim()) return failed(speaker, 'no_editor', labels().labels.empty);
    startJob('explain', editor, pick, speaker);
  }

  // study mode: a hint that gets more concrete each time you ask again about the same code
  let lastHint = { key: '', level: 0 };
  async function hint() {
    const editor = editorNow();
    if (!editor) return failed('', 'no_editor', T.openFile);
    if (!link.ready) return notRunning();
    const pick = await pickCode(editor);
    if (!pick.code.trim()) return failed('', 'no_editor', labels().labels.empty);
    const key = editor.document.uri.toString() + ':' + pick.start_line + '-' + pick.end_line;
    const level = key === lastHint.key ? Math.min(3, lastHint.level + 1) : 1;
    lastHint = { key, level };
    startJob('hint', editor, pick, '', { level });
  }

  async function quiz() {
    const editor = editorNow();
    if (!editor) return failed('', 'no_editor', T.openFile);
    if (!link.ready) return notRunning();
    const pick = await pickCode(editor);
    if (!pick.code.trim()) return failed('', 'no_editor', labels().labels.empty);
    startJob('quiz', editor, pick, '');
  }

  // four stickmen, one line each, in bubbles on the desktop (click one: jump to its line)
  async function review() {
    const editor = editorNow();
    if (!editor) return failed('', 'no_editor', T.openFile);
    if (!link.ready) return notRunning();
    const pick = await pickCode(editor);
    if (!pick.code.trim()) return failed('', 'no_editor', labels().labels.empty);
    link.send(Object.assign({ type: 'review', id: nextId++, path: editor.document.uri.fsPath }, pick));
    vscode.window.setStatusBarMessage(`$(comment-discussion) ${T.reviewing}`, 4000);
  }

  async function comment(speaker) {
    const editor = editorNow();
    if (!editor) return failed(speaker, 'no_editor', T.openFile);
    if (!link.ready) return notRunning();
    const pick = await pickCode(editor);
    if (!pick.code.trim()) return failed(speaker, 'no_editor', labels().labels.empty);
    startJob('comment', editor, pick, speaker);
  }

  async function diagnose(speaker, uriArg, rangeArg) {
    let editor = editorNow();
    if (uriArg && (!editor || editor.document.uri.toString() !== uriArg.toString())) {
      editor = await vscode.window.showTextDocument(uriArg, { preserveFocus: true });
    }
    if (!editor) return failed(speaker, 'no_editor', T.openFile);
    if (!link.ready) return notRunning();
    const doc = editor.document;
    const at = rangeArg || editor.selection;
    const all = vscode.languages.getDiagnostics(doc.uri)
      .filter((d) => d.severity <= vscode.DiagnosticSeverity.Warning);
    const near = all.filter((d) => d.range.end.line >= at.start.line - ERROR_NEAR_LINES
      && d.range.start.line <= at.end.line + ERROR_NEAR_LINES)
      .sort((a, b) => a.severity - b.severity || Math.abs(a.range.start.line - at.start.line)
        - Math.abs(b.range.start.line - at.start.line));
    if (!near.length) return failed(speaker, 'no_errors', labels().labels.no_errors);
    const main = near[0];
    const sym = await enclosingSymbol(doc, main.range.start);
    const pick = sym && sym.range.end.line - sym.range.start.line < 200
      ? slice(doc, sym.range.start.line, sym.range.end.line, { symbolName: sym.name })
      : slice(doc, Math.max(0, main.range.start.line - 20), main.range.end.line + 20);
    const diagnostics = near.slice(0, 5).map((d) => ({
      line: d.range.start.line + 1, severity: severityName(d.severity), message: d.message,
      source: d.source || '', code: typeof d.code === 'object' && d.code ? String(d.code.value) : String(d.code || ''),
    }));
    startJob('diagnose', editor, pick, speaker, { diagnostics });
  }

  // "/help" (or "what can you do") and slash commands typed in the panel's box: answered here, no AI
  const SLASH = {
    '/explain': () => explain(''), '/comment': () => comment(''), '/hint': () => hint(), '/quiz': () => quiz(),
    '/review': () => review(), '/run': () => runFile(), '/terminal': () => explainRun(''),
    '/error': () => diagnose(''), '/help': () => showHelp(),
  };
  const HELP_WORDS = /^(help|помощь|команды|справка|commands)\??$|спис\S*(?: \S+){0,3} команд|все (?:твои |свои )?команды|какие (?:у тебя )?команды|что ты умеешь|what can you do|your commands|list (?:of )?(?:all )?commands/i;
  function showHelp() {
    const id = nextId++;
    ensurePanel(true);
    post({ cmd: 'begin', id, kind: 'help', fresh: false, meta: '' });
    post({ cmd: 'chunk', id, text: RU ? HELP_RU : HELP_EN });
    post({ cmd: 'done', id });
  }

  async function ask(question) {
    question = String(question || '').trim();
    if (!question) return;
    const word = question.split(/\s+/)[0].toLowerCase();
    if (SLASH[word]) return SLASH[word]();
    if (word.startsWith('/')) return showHelp();  // an unknown /command: show what there is
    if (HELP_WORDS.test(question)) return showHelp();
    if (!link.ready) return notRunning();
    const editor = editorNow();
    let pick = null;
    if (editor && !editor.selection.isEmpty) pick = await pickCode(editor);   // a new selection wins
    else if (topic) pick = topic.pick;
    else if (editor) pick = await pickCode(editor, false);
    if (pick && editor && !topic) topic = { uri: editor.document.uri, pick };
    startJob('ask', editor, pick, '', { question, history: thread.slice(-MAX_HISTORY) });
  }

  // ---------------- comment proposals
  async function showProposal(id, code, safe) {
    const j = jobs.get(id);
    if (!j || !j.pick) return;
    j.proposal = code;
    const name = path.basename(j.uri.fsPath || j.uri.path);
    const left = proposals.put(id, 'before', name, j.pick.code);
    const right = proposals.put(id, 'after', name, code);
    j.diff = right;
    post({ cmd: 'proposal', id, safe });
    await vscode.commands.executeCommand('vscode.diff', left, right,
      `${labels().name}: ${name} (L${j.pick.start_line}–${j.pick.end_line})`,
      { preview: true, viewColumn: vscode.ViewColumn.One });
    const L = labels().labels;
    vscode.window.showInformationMessage(`${labels().name}: ${L.review}`, L.accept, L.reject).then((c) => {
      if (c === L.accept) accept(id);
      else if (c === L.reject) reject(id);
    });
  }

  async function closeDiff(j) {
    if (!j.diff || !vscode.window.tabGroups) return;
    const tabs = [];
    for (const g of vscode.window.tabGroups.all) {
      for (const t of g.tabs) {
        if (t.input && t.input.modified && t.input.modified.toString() === j.diff.toString()) tabs.push(t);
      }
    }
    if (tabs.length) await vscode.window.tabGroups.close(tabs);
  }

  async function accept(id) {
    const j = jobs.get(id);
    if (!j || !j.proposal || j.resolved) return;
    j.resolved = true;
    const doc = await vscode.workspace.openTextDocument(j.uri);
    const s = j.pick.start_line - 1;
    const e = Math.min(j.pick.end_line - 1, doc.lineCount - 1);
    const range = new vscode.Range(s, 0, e, doc.lineAt(e).text.length);
    let state = 'stale';
    if (norm(doc.getText(range)) === j.pick.code) {
      const edit = new vscode.WorkspaceEdit();
      edit.replace(j.uri, range, j.proposal);
      if (await vscode.workspace.applyEdit(edit)) {
        state = 'applied';
        link.send({ type: 'applied', id, speaker: j.speaker || '' });
      }
    }
    await closeDiff(j);
    if (state === 'applied') await vscode.window.showTextDocument(j.uri, { viewColumn: vscode.ViewColumn.One });
    proposals.drop(id);
    post({ cmd: 'resolved', id, state });
  }

  async function reject(id) {
    const j = jobs.get(id);
    if (!j || j.resolved) return;
    j.resolved = true;
    await closeDiff(j);
    proposals.drop(id);
    post({ cmd: 'resolved', id, state: 'rejected' });
  }

  // ---------------- messages from StickAI
  function fromApp(msg) {
    if (msg.type === 'welcome') { refreshLabels(); showState(); sendContext(); return; }
    if (msg.type === 'ask_about') {  // "you seem stuck": a proper look in the panel
      ensurePanel(true);
      ask(String(msg.text || ''));
      return;
    }
    if (msg.type === 'goto') {  // a click on a stickman's hint: show me that line
      if (msg.file) goto(vscode.Uri.file(msg.file), msg.line || 1);
      return;
    }
    if (msg.type === 'run') {
      const run = { explain, comment, diagnose, terminal: explainRun }[msg.action];
      if (run) run(msg.speaker || '');
      else if (msg.action === 'ask') { ensurePanel(true); post({ cmd: 'focusAsk' }); }
      return;
    }
    const j = jobs.get(msg.id);
    if (!j) return;
    if (msg.type === 'start') return;
    if (msg.type === 'chunk') {
      j.answer += msg.text;
      post({ cmd: 'chunk', id: msg.id, text: msg.text });
    } else if (msg.type === 'done') {
      j.running = false;
      if (j.kind !== 'comment') thread.push({ q: j.question || `(${j.kind})`, a: j.answer.slice(0, 6000) });
      post({ cmd: 'done', id: msg.id });
    } else if (msg.type === 'error') {
      j.running = false;
      post({ cmd: 'error', id: msg.id, message: msg.message });
    } else if (msg.type === 'proposal') {
      j.running = false;
      j.speaker = msg.speaker || '';
      showProposal(msg.id, msg.code, msg.safe);
    }
  }

  // ---------------- status bar + context
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  function showState() {
    if (link.ready) {
      const name = labels().name;
      status.text = `$(person) ${name}`;
      status.tooltip = `StickAI: ${name} — Ctrl+Alt+E / Ctrl+Alt+C / Ctrl+Alt+Q`;
      status.command = 'stickai.showPanel';
    } else {
      status.text = '$(debug-disconnect) StickAI';
      status.tooltip = T.disconnected;
      status.command = 'stickai.reconnect';
    }
    status.show();
  }
  function sendContext() {
    const ed = vscode.window.activeTextEditor;
    link.send({
      type: 'context', focused: vscode.window.state.focused,
      file: ed ? vscode.workspace.asRelativePath(ed.document.uri, false) : '',
      language: ed ? ed.document.languageId : '',
      has_selection: !!(ed && !ed.selection.isEmpty),
      selection: ed ? [ed.selection.start.line + 1, ed.selection.end.line + 1] : [],
    });
  }
  let ctxTimer = null;
  const contextSoon = () => { clearTimeout(ctxTimer); ctxTimer = setTimeout(sendContext, 400); };

  const link = new Link(fromApp, () => { showState(); if (link.ready) sendContext(); });

  // ---------------- light bulb: "What's this error?"
  const actions = {
    provideCodeActions(doc, range, ctx) {
      const diags = (ctx.diagnostics || []).filter((d) => d.severity <= vscode.DiagnosticSeverity.Warning);
      if (!diags.length) return [];
      const a = new vscode.CodeAction(T.whatError, vscode.CodeActionKind.QuickFix);
      a.command = { command: 'stickai.diagnose', title: T.whatError, arguments: [doc.uri, diags[0].range] };
      a.diagnostics = diags;
      return [a];
    },
  };

  context.subscriptions.push(
    status,
    { dispose: () => link.dispose() },
    vscode.workspace.registerTextDocumentContentProvider(SCHEME, proposals),
    vscode.languages.registerCodeActionsProvider({ scheme: 'file' }, actions,
      { providedCodeActionKinds: [vscode.CodeActionKind.QuickFix] }),
    vscode.languages.registerCodeActionsProvider({ scheme: 'untitled' }, actions,
      { providedCodeActionKinds: [vscode.CodeActionKind.QuickFix] }),
    vscode.commands.registerCommand('stickai.explain', () => explain('')),
    vscode.commands.registerCommand('stickai.comment', () => comment('')),
    vscode.commands.registerCommand('stickai.hint', () => hint()),
    vscode.commands.registerCommand('stickai.explainRun', () => explainRun('')),
    vscode.commands.registerCommand('stickai.quiz', () => quiz()),
    vscode.commands.registerCommand('stickai.review', () => review()),
    vscode.commands.registerCommand('stickai.diagnose', (uri, range) => diagnose('', uri, range)),
    vscode.commands.registerCommand('stickai.ask', () => { ensurePanel(true); post({ cmd: 'focusAsk' });
      if (panel) panel.reveal(undefined, false); }),
    vscode.commands.registerCommand('stickai.showPanel', () => ensurePanel(true)),
    vscode.commands.registerCommand('stickai.reconnect', () => link.reconnect()),
    vscode.window.onDidChangeWindowState(contextSoon),
    vscode.window.onDidChangeActiveTextEditor(contextSoon),
    vscode.window.onDidChangeTextEditorSelection(contextSoon),
    ...watchTerminal(),
    ...watchSaves(),
    ...watchTasks(),
    vscode.commands.registerCommand('stickai.run', () => runFile()),
  );
  showState();
  link.connect();

  // after a save: the changed lines (+ a few around them) for a quick look; StickAI decides if it says anything
  function watchSaves() {
    const SKIP = new Set(['plaintext', 'markdown', 'json', 'jsonc', 'log', 'ignore', 'git-commit', 'git-rebase',
      'properties', 'ini', 'csv', 'xml', 'yaml', 'toml']);
    const snaps = new Map();
    const remember = (doc) => {
      if (doc.uri.scheme === 'file' && doc.getText().length < 400000) snaps.set(doc.uri.toString(), norm(doc.getText()));
    };
    vscode.workspace.textDocuments.forEach(remember);
    const opened = vscode.workspace.onDidOpenTextDocument(remember);
    const closed = vscode.workspace.onDidCloseTextDocument((d) => snaps.delete(d.uri.toString()));
    const saved = vscode.workspace.onDidSaveTextDocument((doc) => {
      const old = snaps.get(doc.uri.toString());
      remember(doc);
      if (old === undefined || !link.ready || doc.uri.scheme !== 'file' || SKIP.has(doc.languageId)) return;
      const a = old.split('\n');
      const b = norm(doc.getText()).split('\n');
      let top = 0;
      while (top < a.length && top < b.length && a[top] === b[top]) top++;
      if (top === a.length && top === b.length) return;  // saved without changes
      let ea = a.length - 1, eb = b.length - 1;
      while (ea >= top && eb >= top && a[ea] === b[eb]) { ea--; eb--; }
      if (eb < top) return;  // only deletions: nothing new to look at
      if (eb - top > 120) return;  // a big rewrite / paste: not a "glance"
      const from = Math.max(0, top - 6), to = Math.min(b.length - 1, eb + 6);
      const diagnostics = vscode.languages.getDiagnostics(doc.uri)
        .filter((d) => d.severity <= vscode.DiagnosticSeverity.Warning && d.range.start.line >= from && d.range.start.line <= to)
        .slice(0, 6).map((d) => ({ line: d.range.start.line + 1, message: d.message }));
      link.send({ type: 'saved', file: vscode.workspace.asRelativePath(doc.uri, false), path: doc.uri.fsPath,
        language: doc.languageId, start_line: from + 1, changed_from: top + 1, changed_to: eb + 1,
        code: b.slice(from, to + 1).join('\n'), diagnostics });
    });
    return [opened, closed, saved];
  }

  // the last command that failed in VS Code's terminal (kept for "explain the terminal error")
  let lastFail = null;
  async function explainRun(speaker) {
    if (!link.ready) return notRunning();
    if (!lastFail) {  // nothing failed yet: the errors VS Code underlines in this file, if any
      const ed = editorNow();
      const errs = ed ? vscode.languages.getDiagnostics(ed.document.uri)
        .filter((d) => d.severity === vscode.DiagnosticSeverity.Error) : [];
      if (errs.length) return diagnose(speaker, ed.document.uri, errs[0].range);
      if (speaker) link.send({ type: 'run_failed', speaker, reason: 'no_run' });
      else vscode.window.showInformationMessage(T.noRun);
      return;
    }
    const f = lastFail;
    const loc = f.location || {};
    const pick = loc.code ? { code: loc.code, start_line: loc.start || 1, end_line: (loc.start || 1) + loc.code.split('\n').length - 1,
      file: loc.name || path.basename(loc.file || ''), language: path.extname(loc.file || '').slice(1), whole: false,
      before: '', after: '', truncated: false } : null;
    const tail = String(f.output || '').trim().split('\n').slice(-30).join('\n');
    const question = `${T.whyFailed} \`${f.command}\`\n\n\`\`\`\n${tail}\n\`\`\``;
    thread = [];
    topic = loc.file ? { uri: vscode.Uri.file(loc.file), pick } : topic;
    startJob('ask', loc.file ? { document: { uri: vscode.Uri.file(loc.file), version: 0 } } : null, pick, speaker || '',
      { question, history: [] });
  }

  // a failed task (e.g. "C/C++: gcc.exe build active file"): its errors come from the Problems list
  function watchTasks() {
    return [vscode.tasks.onDidEndTaskProcess(async (e) => {
      if (!link.ready || !e.exitCode) return;
      await new Promise((r) => setTimeout(r, 800));  // let the problem matcher fill the Problems list
      const t = e.execution.task;
      const ex = t.execution || {};
      const line = ex.commandLine || [ex.command && (ex.command.value || ex.command), ...(ex.args || []).map((a) => a.value || a)]
        .filter(Boolean).join(' ');
      const errs = [];
      for (const [uri, ds] of vscode.languages.getDiagnostics()) {
        for (const d of ds) if (d.severity === vscode.DiagnosticSeverity.Error) errs.push({ uri, d });
      }
      const out = errs.slice(0, 8).map(({ uri, d }) =>
        `${vscode.workspace.asRelativePath(uri, false)}:${d.range.start.line + 1}: error: ${d.message}`).join('\n')
        || `the task "${t.name}" failed with exit code ${e.exitCode}`;
      const msg = { type: 'run_end', command: `${t.name}${line ? ': ' + line : ''}`, exit_code: e.exitCode, output: out };
      if (errs.length) {
        const first = errs[0];
        const around = await codeAround(first.uri.fsPath, first.d.range.start.line + 1);
        msg.location = Object.assign({ file: first.uri.fsPath, line: first.d.range.start.line + 1,
          name: vscode.workspace.asRelativePath(first.uri, false) }, around || {});
      }
      lastFail = msg;
      link.send(msg);
    })];
  }

  // "Run with StickAI": compile / run the current file in our own terminal. Works without the shell's
  // integration (blocked when PowerShell may not run scripts), so the output is always seen; input works too.
  async function runFile() {
    const ed = editorNow();
    if (!ed) return vscode.window.showInformationMessage(T.openFile);
    const doc = ed.document;
    if (doc.isDirty) await doc.save();
    const file = doc.uri.fsPath, dir = path.dirname(file), ext = path.extname(file).toLowerCase();
    const base = path.join(dir, path.basename(file, ext));
    const exe = base + (process.platform === 'win32' ? '.exe' : '');
    const plans = {
      '.py': [['python', [file]]],
      '.c': [['gcc', ['-g', '-Wall', file, '-o', exe]], [exe, []]],
      '.cpp': [['g++', ['-g', '-Wall', file, '-o', exe]], [exe, []]],
      '.cc': [['g++', ['-g', '-Wall', file, '-o', exe]], [exe, []]],
      '.js': [['node', [file]]],
    };
    const steps = plans[ext];
    if (!steps) return vscode.window.showInformationMessage(T.cantRun);
    const write = new vscode.EventEmitter();
    let child = null, output = '', line = '', done = false;
    const show = (t) => write.fire(t.replace(/\r?\n/g, '\r\n'));
    const pty = {
      onDidWrite: write.event,
      open: () => go(0),
      close: () => { if (child) child.kill(); },
      handleInput: (data) => {  // your typing (or a pasted / sent line) goes to the program's input
        if (done || !child) return;
        for (const ch of data) {
          if (ch === '\r' || ch === '\n') { show('\n'); child.stdin.write(line + '\n'); output += line + '\n'; line = ''; }
          else if (ch === '\x7f' || ch === '\b') { if (line) { line = line.slice(0, -1); write.fire('\b \b'); } }
          else if (ch >= ' ') { line += ch; write.fire(ch); }
        }
      },
    };
    const finish = async (code, shown) => {
      done = true;
      show(`\n\x1b[90m— ${shown} → exit code ${code} —\x1b[0m\n`);
      if (!link.ready) return;
      const msg = { type: 'run_end', command: shown, exit_code: code, cwd: dir, output: output.slice(-4000) };
      if (code) {
        const loc = errorLocation(output, dir);
        if (loc) {
          const around = await codeAround(loc.file, loc.line);
          msg.location = Object.assign({ file: loc.file, line: loc.line,
            name: vscode.workspace.asRelativePath(loc.file, false) }, around || {});
        }
        lastFail = msg;
      }
      link.send(msg);
    };
    const go = (i) => {
      const [cmd, args] = steps[i];
      const shown = [path.basename(cmd), ...args.map((a) => (a.includes(dir) ? path.basename(a) : a))].join(' ');
      show(`\x1b[33m> ${shown}\x1b[0m\n`);
      try {
        child = cp.spawn(cmd, args, { cwd: dir, windowsHide: true });
      } catch (err) {
        return finish(127, shown);
      }
      child.stdout.on('data', (d) => { const t = d.toString(); output += t; show(t); });
      child.stderr.on('data', (d) => { const t = d.toString(); output += t; show(`\x1b[31m${t}\x1b[0m`); });
      child.on('error', (err) => { output += String(err.message) + '\n'; show(`\x1b[31m${err.message}\x1b[0m\n`); });
      child.on('close', (code) => {
        child = null;
        if (code === 0 && i + 1 < steps.length) return go(i + 1);  // built: now run it
        finish(code === null ? 1 : code, shown);
      });
    };
    const term = vscode.window.createTerminal({ name: `StickAI ▶ ${path.basename(file)}`, pty });
    term.show();
  }

  function watchTerminal() {
    if (!vscode.window.onDidStartTerminalShellExecution || !vscode.window.onDidEndTerminalShellExecution) return [];
    const runs = new Map();
    const start = vscode.window.onDidStartTerminalShellExecution(async (e) => {
      const rec = { out: '' };
      runs.set(e.execution, rec);
      try {
        for await (const d of e.execution.read()) {
          rec.out += d;
          if (rec.out.length > MAX_RUN_OUTPUT) rec.out = rec.out.slice(-MAX_RUN_OUTPUT / 2);
        }
      } catch (err) { /* the terminal went away */ }
    });
    const end = vscode.window.onDidEndTerminalShellExecution(async (e) => {
      if (!link.ready) { runs.delete(e.execution); return; }
      await new Promise((r) => setTimeout(r, 200));  // let the reader catch the last output
      const rec = runs.get(e.execution);
      runs.delete(e.execution);
      const out = (rec ? rec.out : '').replace(ANSI, '').replace(/\r/g, '');
      const command = (e.execution.commandLine && e.execution.commandLine.value) || '';
      const cwdUri = e.execution.cwd || (e.shellIntegration && e.shellIntegration.cwd);
      const cwd = cwdUri ? cwdUri.fsPath
        : (vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0].uri.fsPath) || '';
      const msg = { type: 'run_end', command, exit_code: e.exitCode === undefined ? null : e.exitCode,
        cwd, output: out.slice(-4000) };
      if (e.exitCode) lastFail = msg;  // for "what's wrong in the terminal?"
      if (e.exitCode) {
        const loc = errorLocation(out, cwd);
        if (loc) {
          const around = await codeAround(loc.file, loc.line);
          msg.location = Object.assign({ file: loc.file, line: loc.line,
            name: vscode.workspace.asRelativePath(loc.file, false) }, around || {});
        }
      }
      link.send(msg);
    });
    return [start, end];
  }
}

function deactivate() {}

module.exports = { activate, deactivate };
