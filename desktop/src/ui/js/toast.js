/* Toast 通知 —— 从 common.js 的 eval 兼容层拆出为独立模块 */
(function () {
  window.App = window.App || {};

  let _toastWrap = null;

  App.toast = function toast(msg, type = 'info') {
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
  };
})();
