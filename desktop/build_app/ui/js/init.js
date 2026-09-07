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

  // 初始化 DOM 元素缓存
  if (App.Elements) {
    App.els = App.Elements.init();
  }

  // 初始化存储管理
  if (App.Storage) {
    App.Storage.migrate();
    App.Storage.load();
    // 同步到全局状态
    App.sessionId = App.Storage.get('last_session');
    App.theme = App.Storage.get('theme');
  }

  // 设置主题
  if (App.theme) {
    document.body.dataset.theme = App.theme;
  }

  // 从 Electron 主进程获取后端地址
  if (typeof window.agentApp !== 'undefined') {
    window.agentApp.getBackendUrl().then(url => {
      App.backendUrl = url || '';
      console.log('[App] 后端地址已设置:', App.backendUrl);
    }).catch(err => {
      console.error('[App] 获取后端地址失败:', err);
    });
    window.agentApp.getBackendApiKey().then(key => {
      App.backendApiKey = key || '';
    }).catch(() => {});
  }

  console.log('[App] 初始化完成, sessionId:', App.sessionId, 'backendUrl:', App.backendUrl);
})();
