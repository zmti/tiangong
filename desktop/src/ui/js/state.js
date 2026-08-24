/* 统一状态管理:基于 App.Storage 替代散落的 localStorage 操作 */
(function () {
  // 状态定义
  const STATE_SCHEMA = {
    session_id: 'desktop-' + Date.now().toString(36),
    theme: 'light',
    mode: 'auto',
    model: '',
    provider: '',
    pinned_sessions: [],
    last_session: '',
  };

  // 初始化时从统一存储加载
  function load() {
    if (!App.Storage) return;
    
    Object.keys(STATE_SCHEMA).forEach(key => {
      const val = App.Storage.get(key);
      if (val !== undefined) {
        STATE_SCHEMA[key] = val;
      }
    });
  }

  function save() {
    if (!App.Storage) return;
    Object.keys(STATE_SCHEMA).forEach(key => {
      App.Storage.set(key, STATE_SCHEMA[key]);
    });
  }

  function get(key) {
    if (!App.Storage) return STATE_SCHEMA[key];
    return App.Storage.get(key);
  }

  function set(key, value) {
    STATE_SCHEMA[key] = value;
    save();
  }

  // 兼容旧代码的直接 localStorage 访问
  function getLegacy(key) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : null;
    } catch (_) {
      return null;
    }
  }

  function setLegacy(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch (_) {}
  }

  window.App = window.App || {};
  App.State = { load, save, get, set, getLegacy, setLegacy };
})();
