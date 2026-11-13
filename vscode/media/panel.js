// The StickAI panel: a thread of explanations, answers and comment proposals, plus a question box.
'use strict';
(function () {
  const vscode = acquireVsCodeApi();
  const $ = (id) => document.getElementById(id);
  let L = {};
  let name = 'StickAI';
  const secs = new Map();   // id -> { el, kind, text, busy }
  const MAX_SECS = 20;

  // ---------------------------------------------------------------- markdown (small, safe subset)
  function esc(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  function lineRefs(s) {
    return s.replace(/\bL(\d{1,5})(?:\s*[-–]\s*L?(\d{1,5}))?\b/g,
      (m, a) => '<span class="line" data-line="' + a + '">' + m + '</span>');
  }
  function inline(s) {
    return s.split(/(`[^`]+`)/g).map((p) => {
      if (p.length > 1 && p[0] === '`' && p[p.length - 1] === '`') {
        const inner = p.slice(1, -1);
        const m = /^L(\d{1,5})$/.exec(inner);
        return m ? '<span class="line" data-line="' + m[1] + '"><code>' + esc(inner) + '</code></span>'
                 : '<code>' + esc(inner) + '</code>';
      }
      let h = esc(p);
      h = h.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
           .replace(/(^|[^*\w])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>')
           .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1');
      return lineRefs(h);
    }).join('');
  }
  function markdown(src) {
    const lines = src.replace(/\r/g, '').split('\n');
    let out = '';
    let i = 0;
    let list = null;
    let para = [];
    const flushPara = () => { if (para.length) { out += '<p>' + inline(para.join(' ')) + '</p>'; para = []; } };
    const closeList = () => { if (list) { out += '</' + list + '>'; list = null; } };
    while (i < lines.length) {
      const ln = lines[i];
      let m;
      if (/^\s*```/.test(ln)) {
        flushPara(); closeList();
        const code = [];
        i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
        i++;
        out += '<div class="pre"><button class="copy">' + esc(L.copy || 'Copy') + '</button><pre><code>'
             + esc(code.join('\n')) + '</code></pre></div>';
        continue;
      }
      if ((m = /^(#{1,6})\s+(.*)$/.exec(ln))) {
        flushPara(); closeList();
        const n = Math.min(4, m[1].length + 1);
        out += '<h' + n + '>' + inline(m[2]) + '</h' + n + '>';
      } else if ((m = /^\s*[-*+]\s+(.*)$/.exec(ln)) || (m = /^\s*\d+[.)]\s+(.*)$/.exec(ln))) {
        flushPara();
        const kind = /^\s*\d/.test(ln) ? 'ol' : 'ul';
        if (list !== kind) { closeList(); out += '<' + kind + '>'; list = kind; }
        out += '<li>' + inline(m[1]) + '</li>';
      } else if (!ln.trim()) {
        flushPara(); closeList();
      } else if (list && /^\s{2,}\S/.test(ln)) {
        out = out.replace(/<\/li>$/, ' ' + inline(ln.trim()) + '</li>');
      } else {
        closeList();
        para.push(ln.trim());
      }
      i++;
    }
    flushPara(); closeList();
    return out;
  }

  // ---------------------------------------------------------------- sections
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function render(s) {
    const body = s.el.querySelector('.body');
    if (!s.text) return;
    body.innerHTML = markdown(s.text) + (s.busy ? '<span class="caret"></span>' : '');
  }
  function updateBusy() {
    const any = [...secs.values()].some((s) => s.busy);
    document.body.classList.toggle('busy', any);
    $('stop').hidden = !any;
  }
  function scrollDown(force) {
    const t = $('thread');
    if (force || t.scrollHeight - t.scrollTop - t.clientHeight < 80) t.scrollTop = t.scrollHeight;
  }
  function begin(m) {
    if (m.fresh) {  // a new topic: drop the old answers (but keep the hint line element)
      for (const s of secs.values()) s.el.remove();
      secs.clear();
    }
    const hint = $('hint');
    if (hint) hint.hidden = true;
    const box = el('div', 'sec');
    box.dataset.id = m.id;
    if (m.kind === 'ask') {
      const q = el('div', 'q');
      q.appendChild(el('b', '', L.question || 'You'));
      q.appendChild(document.createTextNode(m.question || ''));
      box.appendChild(q);
    }
    if (m.meta) box.appendChild(el('div', 'meta', m.meta));
    const body = el('div', 'body');
    body.appendChild(el('p', 'thinking', m.kind === 'comment' ? L.writing_comments : L.thinking));
    box.appendChild(body);
    box.appendChild(el('div', 'foot'));
    $('thread').appendChild(box);
    secs.set(m.id, { el: box, kind: m.kind, text: '', busy: true });
    while (secs.size > MAX_SECS) {
      const [first] = secs.keys();
      secs.get(first).el.remove();
      secs.delete(first);
    }
    updateBusy();
    scrollDown(true);
  }
  function finish(s) { s.busy = false; render(s); updateBusy(); }

  // ---------------------------------------------------------------- messages from the extension
  window.addEventListener('message', (ev) => {
    const m = ev.data;
    if (m.cmd === 'init') {
      L = m.labels; name = m.name;
      document.documentElement.style.setProperty('--accent', m.color);
      $('who').textContent = name;
      $('stop').textContent = L.stop;
      $('hint').textContent = L.waiting;
      $('send').textContent = L.ask_send;
      return;
    }
    if (m.cmd === 'focusAsk') { $('ask').focus(); return; }
    if (m.cmd === 'begin') { begin(m); return; }
    const s = secs.get(m.id);
    if (!s) return;
    const foot = s.el.querySelector('.foot');
    if (m.cmd === 'chunk') {
      s.text += m.text; render(s); scrollDown(false);
    } else if (m.cmd === 'done') {
      finish(s);
    } else if (m.cmd === 'error') {
      finish(s);
      if (!s.text) s.el.querySelector('.body').innerHTML = '';
      s.el.querySelector('.body').appendChild(el('div', 'err', m.message));
    } else if (m.cmd === 'proposal') {
      finish(s);
      foot.innerHTML = '';
      if (!m.safe) foot.appendChild(el('div', 'warn', L.unsafe));
      foot.appendChild(el('span', 'note', L.review));
      const ok = el('button', 'primary', L.accept);
      const no = el('button', '', L.reject);
      ok.onclick = () => vscode.postMessage({ cmd: 'accept', id: m.id });
      no.onclick = () => vscode.postMessage({ cmd: 'reject', id: m.id });
      foot.appendChild(ok);
      foot.appendChild(no);
      scrollDown(true);
    } else if (m.cmd === 'resolved') {
      foot.innerHTML = '';
      const cls = m.state === 'stale' ? 'warn' : 'note';
      foot.appendChild(el('span', cls, L[m.state] || m.state));
    }
  });

  // ---------------------------------------------------------------- user input
  $('stop').addEventListener('click', () => vscode.postMessage({ cmd: 'stop' }));
  $('thread').addEventListener('click', (e) => {
    const line = e.target.closest('.line');
    const sec = e.target.closest('.sec');
    if (line && sec) vscode.postMessage({ cmd: 'goto', id: +sec.dataset.id, line: +line.dataset.line });
    const copy = e.target.closest('.copy');
    if (copy) {
      vscode.postMessage({ cmd: 'copy', text: copy.parentElement.querySelector('code').textContent });
      copy.textContent = L.copied || 'Copied';
      setTimeout(() => { copy.textContent = L.copy || 'Copy'; }, 1500);
    }
  });
  function sendAsk() {
    const t = $('ask').value.trim();
    if (!t) return;
    vscode.postMessage({ cmd: 'ask', text: t });
    $('ask').value = '';
    grow();
  }
  function grow() {
    const a = $('ask');
    a.style.height = '32px';  // one line, the same height as the button; grows with the text
    a.style.height = Math.min(Math.max(a.scrollHeight + 2, 32), 160) + 'px';
  }
  $('send').addEventListener('click', sendAsk);
  $('ask').addEventListener('input', grow);
  $('ask').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendAsk(); }
  });
  vscode.postMessage({ cmd: 'ready' });
})();
