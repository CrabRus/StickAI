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
};
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
      this.write({ type: 'hello', token: info.token, protocol: 1,
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

function severityName(s) {
  return ['error', 'warning', 'info', 'hint'][s] || 'error';
}

// ------------------------------------------------------------------ activate
function activate(context) {
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
  <svg width="38" height="46" viewBox="0 0 38 46" aria-hidden="true">
    <g stroke="var(--accent)" stroke-width="3.2" stroke-linecap="round" fill="none">
      <circle cx="14" cy="8" r="6" fill="var(--accent)" stroke="none"/>
      <path d="M14 14 L14 30 M14 30 L8 44 M14 30 L19 44 M14 18 L22 24 M14 19 L20 27"/>
    </g>
    <rect x="18" y="14" width="18" height="12" rx="1.5" transform="rotate(-8 27 20)" fill="#9aa0a6"/>
    <rect class="screen" x="20" y="16" width="14" height="8" rx="1" transform="rotate(-8 27 20)" fill="#cfd3d7"/>
    <rect x="15" y="26" width="20" height="2.6" rx="1" fill="#9aa0a6"/>
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
    const label = { explain: L.explain_of, comment: L.comment_of, diagnose: L.error_of, ask: '' }[kind];
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

  async function ask(question) {
    question = String(question || '').trim();
    if (!question) return;
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
    if (msg.type === 'run') {
      const run = { explain, comment, diagnose }[msg.action];
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
    vscode.commands.registerCommand('stickai.diagnose', (uri, range) => diagnose('', uri, range)),
    vscode.commands.registerCommand('stickai.ask', () => { ensurePanel(true); post({ cmd: 'focusAsk' });
      if (panel) panel.reveal(undefined, false); }),
    vscode.commands.registerCommand('stickai.showPanel', () => ensurePanel(true)),
    vscode.commands.registerCommand('stickai.reconnect', () => link.reconnect()),
    vscode.window.onDidChangeWindowState(contextSoon),
    vscode.window.onDidChangeActiveTextEditor(contextSoon),
    vscode.window.onDidChangeTextEditorSelection(contextSoon),
  );
  showState();
  link.connect();
}

function deactivate() {}

module.exports = { activate, deactivate };
