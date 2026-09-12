/* 确认弹窗 —— 从 common.js 的 eval 兼容层拆出为独立模块 */
/* 注意: 与 js/confirm.js(App.Confirmation 工具确认卡片) 是两个不同模块 */
(function () {
  window.App = window.App || {};

  App.confirm = function confirmDialog(message, opts = {}) {
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
  };
})();
