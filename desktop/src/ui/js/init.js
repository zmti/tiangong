/* 应用初始化:命名空间 + 调试开关 + 模块加载 */
(function () {
  // 调试开关:设置为 true 启用详细日志
  window.App = {
    DEBUG: false,
    backendUrl: '',
    backendApiKey: '',
    els: {},
    streaming: false,
    abortController: null,
    selectedImageFile: null,
  };

  // 初始化状态管理
  if (App.State) {
    App.State.migrate();
    App.State.load();
    // 同步到全局状态
    App.sessionId = App.State.get('session_id');
    App.theme = App.State.get('theme');
  }

  // 设置主题
  if (App.theme) {
    document.body.dataset.theme = App.theme;
  }

  console.log('[App] 初始化完成, sessionId:', App.sessionId);
})();
