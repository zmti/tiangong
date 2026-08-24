/* 兼容性层:从旧 common.js 迁移，保持向后兼容 */
(function () {
  // 执行轨迹面板（保留在原位置，移动到最后）
  const execTraceModule = `
  /* ---------- 执行轨迹面板 ---------- */
  App.ExecTrace = (function () {
    let steps = [];
    let startTime = 0;
    let panelOpen = false;

    const TOOL_ICONS = {
      read: '📖', write: '✏️', exec: '▶️', search: '🔍', browser: '🌐',
      edit: '🔧', system: '⚙️', tool: '🔨', file: '📄', code: '💻',
      generate_image: '🖼️', python: '🐍', shell: '⚡', plan: '📋',
    };
    function getIcon(name) {
      const n = (name || '').toLowerCase();
      for (const [k, v] of Object.entries(TOOL_ICONS)) {
        if (n.includes(k)) return v;
      }
      return '🔨';
    }

    function getElapsed(ms) {
      if (!ms || ms < 1000) return '<1s';
      if (ms < 60000) return (ms / 1000).toFixed(1) + 's';
      return (ms / 60000).toFixed(1) + 'm';
    }

    function ensurePanel() {
      if (!panelOpen) {
        document.body.classList.add('exec-panel-open');
        panelOpen = true;
        const traceTab = document.querySelector('.exec-tab[data-tab="trace"]');
        if (traceTab) traceTab.click();
      }
    }

    function renderList() {
      const listEl = document.getElementById('execTraceList');
      if (!listEl) return;
      if (steps.length === 0) {
        listEl.innerHTML = '<div class="exec-empty">暂无执行记录</div>';
        return;
      }
      const running = steps.filter(s => s.status === 'running').length;
      const done = steps.filter(s => s.status === 'done').length;
      const failed = steps.filter(s => s.status === 'fail').length;
      let html = '<div class="exec-trace-header" style="padding:8px 12px;font-size:12px;color:var(--muted);border-bottom:1px solid var(--border);">'
        + '<span>' + done + '✓</span><span style="margin:0 6px;">·</span>'
        + '<span style="color:var(--err)">' + failed + '✗</span><span style="margin:0 6px;">·</span>'
        + '<span style="color:var(--primary)">' + running + '⏳</span><span style="margin:0 6px;">·</span>'
        + '<span>' + getElapsed(Date.now() - startTime) + '</span></div>';
      html += '<div class="exec-trace-list">';
      for (const s of steps) {
        const cls = s.status === 'fail' ? 'et-fail' : s.status === 'running' ? 'et-running' : 'et-done';
        html += '<div class="exec-trace-item ' + cls + '" title="' + App.escapeHtml(s.args || '') + '">'
          + '<span class="exec-trace-icon">' + s.icon + '</span>'
          + '<span class="exec-trace-name">' + App.escapeHtml(s.name) + '</span>'
          + '<span class="exec-trace-status">' + (s.status === 'running' ? '⏳' : s.status === 'fail' ? '✗' : '✓') + '</span>'
          + '<span class="exec-trace-time">' + s.time + '</span></div>';
      }
      html += '</div>';
      listEl.innerHTML = html;
    }

    function start() {
      startTime = Date.now();
      steps = [];
      ensurePanel();
      renderList();
    }

    function addStep(name, args, status) {
      const icon = getIcon(name);
      const elapsed = Date.now() - startTime;
      steps.push({ name, icon, status: status || 'running', args, time: new Date().toLocaleTimeString(), elapsed });
      ensurePanel();
      renderList();
    }

    function updateStep(name, status, result) {
      const step = steps.find(s => s.name === name && s.status === 'running');
      if (!step) return;
      step.status = status;
      step.result = result;
      step.elapsed = Date.now() - startTime;
      renderList();
    }

    function finish() {
      steps.forEach(s => { if (s.status === 'running') s.status = 'done'; });
      renderList();
      const total = steps.length;
      const done = steps.filter(s => s.status === 'done').length;
      const failed = steps.filter(s => s.status === 'fail').length;
      const elapsed = Date.now() - startTime;
      steps = [];
      startTime = 0;
      return { total, done, failed, elapsed };
    }

    function clear() {
      steps = [];
      startTime = 0;
      const listEl = document.getElementById('execTraceList');
      if (listEl) listEl.innerHTML = '<div class="exec-empty">暂无执行记录</div>';
    }

    function toggle() {
      panelOpen = !panelOpen;
      document.body.classList.toggle('exec-panel-open', panelOpen);
    }

    return { start, addStep, updateStep, finish, clear, toggle };
  })();
  `;

  // 灯箱功能
  const lightboxModule = `
  /* ---------- 灯箱 ---------- */
  function openLb(src, name) {
    App.els.lbImg.src = src;
    App.els.lbImg.alt = name || '图片';
    App.els.lightbox.classList.add('open');
  }
  function closeLb() {
    App.els.lightbox.classList.remove('open');
    App.els.lbImg.src = '';
  }
  function downloadLb() {
    const src = App.els.lbImg.src;
    if (!src) return;
    const a = document.createElement('a');
    a.href = src;
    a.download = 'image.png';
    document.body.appendChild(a);
    a.click();
    a.remove();
  }
  async function copyImage(src) {
    try {
      if (src.startsWith('data:')) {
        const resp = await fetch(src);
        const blob = await resp.blob();
        await navigator.clipboard.write([new ClipboardItem({ [blob.type || 'image/png']: blob })]);
      } else {
        await navigator.clipboard.writeText(src);
      }
      toast('📋 已复制图片', 'ok');
    } catch (e) {
      toast('复制失败: ' + e.message, 'err');
    }
  }
  function copyLb() { return copyImage(App.els.lbImg.src); }
  `;

  // 语音朗读
  const ttsModule = `
  /* ---------- 语音朗读 ---------- */
  let _audioEl = null;
  function stopSpeak() {
    if (_audioEl) { try { _audioEl.pause(); } catch (_) {} _audioEl = null; }
  }
  async function speak(text) {
    stopSpeak();
    const clean = String(text).replace(/[^a-zA-Z0-9\u4e00-\u9fa5\s]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 2000);
    if (!clean) return;
    let lastErr = '';
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const res = await App.api('/v1/tts', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: clean }),
        });
        if (!res.ok) {
          try { const d = await res.json(); lastErr = d.detail || 'HTTP ' + res.status; } catch (_) { lastErr = 'HTTP ' + res.status; }
          await new Promise(r => setTimeout(r, 500));
          continue;
        }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        _audioEl = new Audio(url);
        _audioEl.play().catch(() => {});
        _audioEl.onended = () => { URL.revokeObjectURL(url); _audioEl = null; };
        _audioEl.onerror = () => { URL.revokeObjectURL(url); _audioEl = null; };
        // 页面卸载时释放 URL
        const cleanup = () => { if (_audioEl) { URL.revokeObjectURL(url); _audioEl = null; } };
        window.addEventListener('beforeunload', cleanup, { once: true });
        return;
      } catch (e) {
        lastErr = e.message;
        await new Promise(r => setTimeout(r, 500));
      }
    }
    App.appendMessage('朗读失败: ' + lastErr, 'error');
  }
  `;

  // Toast 通知
  const toastModule = `
  /* ---------- Toast 通知 ---------- */
  let _toastWrap = null;
  function toast(msg, type = 'info') {
    if (!_toastWrap) {
      _toastWrap = document.createElement('div');
      _toastWrap.className = 'toast-wrap';
      document.body.appendChild(_toastWrap);
    }
    const el = document.createElement('div');
    el.className = 'toast ' + type;
    el.textContent = msg;
    _toastWrap.appendChild(el);
    requestAnimationFrame(() => el.classList.add('show'));
    setTimeout(() => {
      el.classList.remove('show');
      setTimeout(() => el.remove(), 350);
    }, 3200);
  }
  `;

  // 确认弹窗
  const confirmModule = `
  /* ---------- 确认弹窗 ---------- */
  function confirmDialog(message, opts = {}) {
    return new Promise((resolve) => {
      let settled = false;
      const mask = document.createElement('div');
      mask.className = 'modal-mask';
      const card = document.createElement('div');
      card.className = 'confirm-dialog';
      const body = document.createElement('div');
      body.className = 'cd-body';
      body.textContent = message;
      const actions = document.createElement('div');
      actions.className = 'cd-actions';
      const cancel = document.createElement('button');
      cancel.className = 'lb-btn cd-cancel';
      cancel.textContent = '取消';
      const ok = document.createElement('button');
      ok.className = 'lb-btn cd-ok' + (opts.danger ? ' danger' : '');
      ok.textContent = opts.okText || '确定';
      actions.appendChild(cancel);
      actions.appendChild(ok);
      card.appendChild(body);
      card.appendChild(actions);
      mask.appendChild(card);
      document.body.appendChild(mask);

      function done(val) {
        if (settled) return;
        settled = true;
        document.removeEventListener('keydown', onKey);
        mask.remove();
        resolve(val);
      }
      function onKey(e) {
        if (e.key === 'Escape') { e.preventDefault(); done(false); }
      }
      cancel.addEventListener('click', () => done(false));
      ok.addEventListener('click', () => done(true));
      mask.addEventListener('click', (e) => { if (e.target === mask) done(false); });
      document.addEventListener('keydown', onKey);
      ok.focus();
    });
  }
  `;

  // 合并所有模块
  const combined = execTraceModule + lightboxModule + ttsModule + toastModule + confirmModule;
  
  // 执行合并的代码
  try {
    eval(combined);
  } catch (e) {
    console.error('[common.js] 兼容层初始化失败:', e);
  }

  // 导出到 App
  window.App = window.App || {};
  App.openLb = openLb;
  App.closeLb = closeLb;
  App.downloadLb = downloadLb;
  App.copyImage = copyImage;
  App.copyLb = copyLb;
  App.speak = speak;
  App.stopSpeak = stopSpeak;
  App.toast = toast;
  App.confirm = confirmDialog;
})();
