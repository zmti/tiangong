/* 统一状态管理:替代散落的 localStorage 操作 */
(function () {
  const STORE_PREFIX = 'agent_';
  
  // 状态定义与默认值
  const STATE_SCHEMA = {
    session_id: 'desktop-' + Date.now().toString(36),
    theme: 'light',
    mode: 'auto',
    model: '',
    provider: '',
    pinned_sessions: [],
    last_session: '',
    // ... 可扩展
  };

  let _state = {};
  let _listeners = [];

  function load() {
    try {
      const raw = localStorage.getItem(STORE_PREFIX + 'state_v2');
      if (raw) {
        _state = { ...STATE_SCHEMA, ...JSON.parse(raw) };
        return;
      }
    } catch (e) { /* 损坏数据按默认处理 */ }
    _state = { ...STATE_SCHEMA };
    save();
  }

  function save() {
    try {
      localStorage.setItem(STORE_PREFIX + 'state_v2', JSON.stringify(_state));
      _listeners.forEach(fn => fn(_state));
    } catch (_) {}
  }

  function get(key) {
    return _state[key] !== undefined ? _state[key] : STATE_SCHEMA[key];
  }

  function set(key, value) {
    _state[key] = value;
    save();
  }

  function subscribe(fn) {
    _listeners.push(fn);
    return () => { _listeners = _listeners.filter(f => f !== fn); };
  }

  // 兼容旧版键迁移
  function migrate() {
    const keys = ['session_id', 'theme', 'mode', 'model', 'provider', 'pinned_sessions', 'last_session'];
    keys.forEach(key => {
      const oldKey = STORE_PREFIX + key;
      if (!localStorage.getItem(STORE_PREFIX + 'state_v2') && localStorage.getItem(oldKey)) {
        set(key, localStorage.getItem(oldKey));
        localStorage.removeItem(oldKey);
      }
    });
  }

  window.App = window.App || {};
  App.State = { load, save, get, set, subscribe, migrate };
})();
