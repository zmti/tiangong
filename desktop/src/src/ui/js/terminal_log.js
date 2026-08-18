/* 终端日志面板:显示最近执行的终端命令输出,方便查看长任务结果 */
App.TerminalLog = {};
App.TerminalLog._entries = [];
App.TerminalLog._maxEntries = 200;

App.TerminalLog.capture = function(name, args, result, success) {
  const entry = {
    id: Date.now(),
    name,
    args: typeof args === 'string' ? args.slice(0, 120) : JSON.stringify(args || {}).slice(0, 120),
    result: typeof result === 'string' ? result.slice(0, 800) : (result ? JSON.stringify(result).slice(0, 800) : ''),
    success: success !== false,
    time: new Date().toLocaleTimeString(),
  };
  App.TerminalLog._entries.push(entry);
  if (App.TerminalLog._entries.length > App.TerminalLog._maxEntries) {
    App.TerminalLog._entries.shift();
  }
  if (document.body.classList.contains('side-open')) {
    const panel = document.getElementById('terminalPanel');
    if (panel && panel.classList.contains('active')) {
      App.TerminalLog.render();
    }
  }
};

App.TerminalLog.render = function() {
  const listEl = document.getElementById('terminalLogList');
  if (!listEl) return;
  listEl.innerHTML = '';
  const entries = App.TerminalLog._entries;
  if (!entries.length) {
    listEl.innerHTML = '<div class="session-empty">暂无终端执行记录</div>';
    return;
  }
  for (const e of entries) {
    const item = document.createElement('div');
    item.className = 'term-log-item' + (e.success ? '' : ' term-log-err');
    const head = document.createElement('div');
    head.className = 'term-log-head';
    const icon = e.success ? '✓' : '✗';
    head.innerHTML = `<span class="term-log-icon">${icon}</span><span class="term-log-name">${App.escapeHtml(e.name)}</span><span class="term-log-time">${e.time}</span>`;
    item.appendChild(head);
    if (e.args && e.args !== '{}') {
      const argsEl = document.createElement('div');
      argsEl.className = 'term-log-args';
      argsEl.textContent = e.args;
      item.appendChild(argsEl);
    }
    if (e.result) {
      const resEl = document.createElement('div');
      resEl.className = 'term-log-result';
      resEl.textContent = e.result.slice(0, 500);
      if (e.result.length > 500) {
        resEl.title = e.result;
      }
      item.appendChild(resEl);
    }
    listEl.appendChild(item);
  }
};

App.TerminalLog.clear = function() {
  App.TerminalLog._entries = [];
  App.TerminalLog.render();
};
