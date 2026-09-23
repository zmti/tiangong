/* 应用初始化:命名空间 + 调试开关 + 模块加载 */
(function () {
  // 调试开关:默认关闭(流式渲染时日志会刷屏,故不再常开)。
  // 排查时在 DevTools 执行 App.setDebug(true) 即时生效;
  // 或 localStorage.setItem('tg.debug','1') 后 Ctrl+R(持久化,刷新后仍开启)。
  let _dbg = false;
  try { _dbg = localStorage.getItem('tg.debug') === '1'; } catch (_) { /* 存储不可用时忽略 */ }
  window.App = {
    DEBUG: _dbg,
    backendUrl: '',
    backendApiKey: '',
    els: {},
    streaming: false,
    abortController: null,
    selectedImageFile: null,
    _groupId: 0,
  };

  // 运行时切换调试日志(持久化到 localStorage,刷新后保持)
  App.setDebug = function (on) {
    App.DEBUG = !!on;
    try {
      if (App.DEBUG) localStorage.setItem('tg.debug', '1');
      else localStorage.removeItem('tg.debug');
    } catch (_) { /* 忽略 */ }
    console.log('[App.DEBUG]', App.DEBUG ? '已开启详细日志(SSE/renderMarkdown/recover 等)' : '已关闭详细日志');
    return App.DEBUG;
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

  // 同步兜底:Electron loadFile 时把后端地址放在 ?backend= 上,这里直接读取。
  // 否则 IPC(get-backend-url)异步返回前渲染的首屏历史消息里,
  // App.backendUrl 仍是空字符串,相对路径 /images/xx.png 在 file:// 页面下无法加载。
  try {
    const q = new URLSearchParams(location.search).get('backend');
    if (q) App.backendUrl = q;
  } catch (_) { /* 非浏览器环境或 URL 不可用则忽略 */ }

  // 从 Electron 主进程获取后端地址(异步,覆盖上面的同步兜底值)
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
