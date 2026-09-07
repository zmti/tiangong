/* 统一存储管理:替代散落的 localStorage 操作 */
(function () {
  const STORE_KEY = 'agent_state_v2';

  // 状态模式定义（类型 + 默认值）
  const SCHEMA = {
    // 会话相关
    session_id: { type: 'string', default: '' },
    last_session: { type: 'string', default: '' },
    pinned_sessions: { type: 'array', default: [] },

    // 主题与界面
    theme: { type: 'string', default: 'light' },
    mode: { type: 'string', default: 'auto' },

    // 模型选择
    model_provider: { type: 'string', default: '' },
    model_name: { type: 'string', default: '' },

    // 自定义扩展
    _custom: { type: 'object', default: {} },
  };

  let _data = {};
  const _listeners = {};

  // ========== 内部工具 ==========
  function getDefault(key) {
    const def = SCHEMA[key]?.default;
    return typeof def === 'object' ? JSON.parse(JSON.stringify(def)) : def;
  }

  function validate(key, value) {
    const expected = SCHEMA[key]?.type;
    if (!expected) return true; // 未知 key 不做校验

    if (expected === 'string') return typeof value === 'string';
    if (expected === 'number') return typeof value === 'number';
    if (expected === 'boolean') return typeof value === 'boolean';
    if (expected === 'array') return Array.isArray(value);
    if (expected === 'object') return value !== null && typeof value === 'object';
    return true;
  }

  function migrate() {
    // 迁移旧版分散的 localStorage 键
    const migrations = [
      { from: 'agent_session', to: 'last_session', transform: v => v },
      { from: 'agent_pinned_sessions', to: 'pinned_sessions', transform: v => { try { return JSON.parse(v) || []; } catch { return []; } } },
      { from: 'agent_theme', to: 'theme', transform: v => v },
      { from: 'agent_mode', to: 'mode', transform: v => v },
      { from: 'agent_provider', to: 'model_provider', transform: v => v },
      { from: 'agent_session_collapsed', to: 'sidebar_collapsed', transform: v => v === '1' },
    ];

    const raw = localStorage.getItem(STORE_KEY);
    if (raw) return; // 已有新格式，不迁移

    migrations.forEach(({ from, to, transform }) => {
      const val = localStorage.getItem(from);
      if (val !== null) {
        _data[to] = transform(val);
        localStorage.removeItem(from);
      }
    });

    // 迁移模型状态：旧格式 agent_model_<providerId> → model_name
    const prov = localStorage.getItem('agent_provider');
    if (prov) {
      const modelKey = `agent_model_${prov}`;
      const modelVal = localStorage.getItem(modelKey);
      if (modelVal !== null) {
        _data.model_name = modelVal;
        localStorage.removeItem(modelKey);
      }
    }

    // 迁移 agent_model_state（旧整体格式）
    const rawModelState = localStorage.getItem('agent_model_state');
    if (rawModelState) {
      try {
        const saved = JSON.parse(rawModelState);
        if (saved && typeof saved === 'object') {
          _data.model_provider = typeof saved.provider === 'string' ? saved.provider : _data.model_provider;
          _data.model_name = typeof saved.model === 'string' ? saved.model : _data.model_name;
        }
      } catch (_) {}
      localStorage.removeItem('agent_model_state');
    }

    save();
  }

  function load() {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object') {
          _data = {};
          Object.keys(SCHEMA).forEach(key => {
            const def = getDefault(key);
            if (typeof def === 'object' && !Array.isArray(def)) {
              _data[key] = { ...def, ...(parsed[key] || {}) };
            } else {
              _data[key] = parsed[key] !== undefined ? parsed[key] : def;
            }
          });
          return;
        }
      }
    } catch (e) { /* 损坏数据按默认处理 */ }

    // 使用默认值
    Object.keys(SCHEMA).forEach(key => {
      _data[key] = getDefault(key);
    });
    save();
  }

  function save() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(_data));
    } catch (_) { /* 忽略存储错误 */ }
  }

  // ========== 公开 API ==========
  function get(key, fallback) {
    const val = _data[key];
    if (val !== undefined && validate(key, val)) return val;
    return fallback !== undefined ? fallback : getDefault(key);
  }

  function set(key, value) {
    if (!validate(key, value)) {
      console.warn(`[App.Storage] Invalid type for ${key}: expected ${SCHEMA[key]?.type}`);
      return;
    }
    _data[key] = value;
    save();
    // 触发监听器
    const listeners = _listeners[key];
    if (listeners && listeners.length) {
      listeners.forEach(cb => {
        try { cb(value); } catch (_) { /* 忽略监听器错误 */ }
      });
    }
  }

  function remove(key) {
    delete _data[key];
    save();
  }

  function subscribe(key, callback) {
    const listeners = _listeners[key] || (_listeners[key] = []);
    listeners.push(callback);
    // 返回取消订阅函数
    return () => {
      const idx = listeners.indexOf(callback);
      if (idx !== -1) listeners.splice(idx, 1);
    };
  }

  function getCustom(key) {
    return _data._custom[key];
  }

  function setCustom(key, value) {
    _data._custom[key] = value;
    save();
  }

  // 初始化
  migrate();
  load();

  window.App = window.App || {};
  App.Storage = { get, set, remove, subscribe, getCustom, setCustom, load, save };

  // 同步到 App 命名空间（常用键）
  App.theme = App.Storage.get('theme');
  App.sessionId = App.Storage.get('last_session');
})();
