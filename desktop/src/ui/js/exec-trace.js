/* 执行轨迹面板 —— 从 common.js 的 eval 兼容层拆出为独立模块 */
(function () {
  window.App = window.App || {};

  const TOOL_ICONS = {
    read: '📖', write: '✏️', edit: '🔧', exec: '▶️', search: '🔍', browser: '🌐',
    edit: '🔧', system: '⚙️', tool: '🔨', file: '📄', code: '💻',
    generate_image: '🖼️', python: '🐍', shell: '⚡', plan: '📋',
    read_file: '📖', write_file: '✏️', edit_file: '🔧',
    run_terminal: '⚡', run_code: '💻', git: '📦',
    code_search: '🔎', file_search: '🔍',
    fetch_content: '🌐', delegate_task: '🔀', save_memory: '🧠', search_memory: '🔎',
  };

  function getIcon(name) {
    const n = (name || '').toLowerCase();
    for (const [k, v] of Object.entries(TOOL_ICONS)) {
      if (n.includes(k)) return v;
    }
    return '🔨';
  }

  // 从工具参数中提取关键路径/命令
  function extractDetail(name, args) {
    try {
      const obj = typeof args === 'string' ? JSON.parse(args) : args;
      if (!obj || typeof obj !== 'object') return null;
      if (name === 'read_file' || name === 'read') return '📖 ' + (obj.path || obj.file || obj.filePath || '');
      if (name === 'write_file' || name === 'write') return '✏️ ' + (obj.path || obj.file || obj.filePath || '');
      if (name === 'edit_file' || name === 'edit') return '🔧 ' + (obj.path || obj.file || obj.filePath || '');
      if (name === 'run_terminal' || name === 'exec') return '⚡ ' + (obj.command || obj.cmd || '');
      if (name === 'run_code' || name === 'python') return '💻 执行代码';
      if (name.startsWith('git_')) return '📦 Git ' + name.replace('git_', '');
      if (name === 'code_search' || name === 'search') return '🔍 ' + (obj.query || obj.q || '');
      if (name === 'file_search') return '🔍 文件搜索';
      return null;
    } catch (_) { return null; }
  }

  function getElapsed(ms) {
    if (!ms || ms < 1000) return '<1s';
    if (ms < 60000) return (ms / 1000).toFixed(1) + 's';
    return (ms / 60000).toFixed(1) + 'm';
  }

  App.ExecTrace = (function () {
    let steps = [];
    let startTime = 0;
    let panelOpen = false;

    function ensurePanel() {
      if (!panelOpen) {
        const fd = document.getElementById('featureDialog');
        if (fd && !fd.hidden) return;
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
      let html = '<div class="exec-trace-header">'
        + '<span style="color:var(--ok)">' + done + '✓</span>'
        + '<span style="margin:0 4px;color:var(--muted)">·</span>'
        + '<span style="color:var(--err)">' + failed + '✗</span>'
        + '<span style="margin:0 4px;color:var(--muted)">·</span>'
        + '<span style="color:var(--primary)">' + running + '⏳</span>'
        + '<span style="margin:0 4px;color:var(--muted)">·</span>'
        + '<span>' + getElapsed(Date.now() - startTime) + '</span>'
        + '</div>';
      html += '<div class="exec-trace-list">';
      for (const s of steps) {
        const cls = s.status === 'fail' ? 'et-fail' : s.status === 'running' ? 'et-running' : 'et-done';
        const detail = s.detail || '';
        html += '<div class="exec-trace-item ' + cls + '" title="' + App.escapeHtml(s.args || '') + '">'
          + '<span class="exec-trace-icon">' + s.icon + '</span>'
          + '<div class="exec-trace-item-body">'
          + '<span class="exec-trace-name">' + App.escapeHtml(s.name) + '</span>'
          + (detail ? '<span class="exec-trace-detail">' + App.escapeHtml(detail) + '</span>' : '')
          + '</div>'
          + '<span class="exec-trace-status">' + (s.status === 'running' ? '⏳' : s.status === 'fail' ? '✗' : '✓') + '</span>'
          + '<span class="exec-trace-time">' + s.elapsed_str + '</span></div>';
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
      const detail = extractDetail(name, args);
      steps.push({
        name, icon, status: status || 'running', args,
        time: new Date().toLocaleTimeString(),
        elapsed, elapsed_str: getElapsed(elapsed), detail
      });
      ensurePanel();
      renderList();
    }

    function updateStep(name, status, result) {
      const step = steps.find(s => s.name === name && s.status === 'running');
      if (!step) return;
      step.status = status;
      step.result = result;
      step.elapsed = Date.now() - startTime;
      step.elapsed_str = getElapsed(step.elapsed);
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
})();
