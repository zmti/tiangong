/* 主模型选择器(App.ModelPicker):按钮 + 浮层就地选模型
 *
 * 设计要点(与 App.Mode 同一套模式):
 * - provider/model 选择只存在 App.ModelPicker._state 一份,localStorage 仅作持久化副本;
 * - 按钮徽标显示当前模型,点击展开浮层:搜索 + 按提供商分组的模型清单,
 *   点击提供商标题行 = 用该提供商默认模型;未配置的提供商灰显不可选;
 * - 支持多种存储键名的自动迁移:
 *     App.Storage (新): model_provider / model_name
 *     App.Storage._custom: model_state
 *     localStorage 旧键: agent_model_state / agent_provider / agent_model_<pid>
 */
App.ModelPicker = (() => {
  const CUSTOM_KEY = 'model_state';

  let _state = { provider: '', model: '' };
  let _btn = null;

  /* ---------- 状态 ---------- */
  function load() {
    try {
      // 1. 优先从 App.Storage._custom (model_state 整体对象)
      if (App.Storage) {
        const saved = App.Storage.getCustom(CUSTOM_KEY);
        if (saved && typeof saved === 'object') {
          _state.provider = typeof saved.provider === 'string' ? saved.provider : '';
          _state.model = typeof saved.model === 'string' ? saved.model : '';
          return;
        }
        // 2. 从统一存储直接键加载
        const prov = App.Storage.get('model_provider') || '';
        const model = App.Storage.get('model_name') || '';
        if (prov || model) {
          _state = { provider: prov, model: model };
          return;
        }
        // 3. 迁移旧键: agent_provider + agent_model_<pid>
        const provOld = App.Storage.get('provider');
        if (provOld) {
          const modelOld = App.Storage.getCustom(`model_${provOld}`) || '';
          _state = { provider: provOld, model: modelOld };
          // 写入新键
          App.Storage.set('model_provider', provOld);
          App.Storage.set('model_name', modelOld);
          return;
        }
        // 3b. 从新统一存储直接键（已由 storage.js 迁移填入）
        const provNew = App.Storage.get('model_provider') || '';
        const modelNew = App.Storage.get('model_name') || '';
        if (provNew || modelNew) {
          _state = { provider: provNew, model: modelNew };
          return;
        }
      }
      // 4. 降级到 localStorage
      const raw = localStorage.getItem('agent_model_state');
      if (raw) {
        const saved = JSON.parse(raw);
        if (saved && typeof saved === 'object') {
          _state.provider = typeof saved.provider === 'string' ? saved.provider : '';
          _state.model = typeof saved.model === 'string' ? saved.model : '';
          return;
        }
      }
      // 5. 迁移旧 localStorage 键: agent_provider + agent_model_<providerId>
      const prov = localStorage.getItem('agent_provider') || '';
      const model = prov ? (localStorage.getItem(`agent_model_${prov}`) || '') : '';
      if (prov || model) {
        _state = { provider: prov, model: model };
      }
    } catch (_) { /* 忽略 */ }
    save();
  }

  function save() {
    try {
      if (App.Storage) {
        App.Storage.setCustom(CUSTOM_KEY, _state);
        App.Storage.set('model_provider', _state.provider);
        App.Storage.set('model_name', _state.model);
        if (_state.provider) {
          App.Storage.set('provider', _state.provider);
          App.Storage.setCustom(`model_${_state.provider}`, _state.model);
        }
      } else {
        localStorage.setItem('agent_model_state', JSON.stringify(_state));
        localStorage.setItem('agent_provider', _state.provider);
        if (_state.provider) localStorage.setItem(`agent_model_${_state.provider}`, _state.model);
      }
    } catch (_) { /* 忽略 */ }
  }

  function get() { return { provider: _state.provider, model: _state.model }; }

  function set(provider, model) {
    _state = { provider: provider || '', model: model || '' };
    save();
    refreshBadge();
    notify();
  }

  function clear() { set('', ''); }

  function providerName(pid) {
    const p = (App.Settings._providers || []).find((x) => x.id === pid);
    return p ? p.name : (pid || '');
  }

  function isConfigured(pid) {
    const p = (App.Settings._providers || []).find((x) => x.id === pid);
    return !p || !!p.configured;
  }

  function summary() {
    if (!_state.provider) {
      // 未选择模型时,尝试显示后端当前配置的默认模型
      const active = App.Settings._activeProvider || '';
      const activeModel = App.Settings._activeModel || '';
      if (active) {
        return activeModel
          ? `${providerName(active)} · ${activeModel}`
          : `${providerName(active)} · 默认`;
      }
      return '默认模型';
    }
    return _state.model
      ? `${providerName(_state.provider)} · ${_state.model}`
      : `${_state.provider} · 默认`;
  }

  const _listeners = [];
  function onChange(cb) { _listeners.push(cb); }
  function notify() { _listeners.forEach((cb) => { try { cb(get()); } catch (_) {} }); }

  /* ---------- 徽标 ---------- */
  function refreshBadge() {
    if (!_btn) _btn = App.els.modelPickerBtn || null;
    if (!_btn) return;
    _btn.textContent = `🤖 ${summary()}`;
    _btn.title = _state.provider
      ? `当前模型: ${_state.provider}${_state.model ? '/' + _state.model : ''} (点击更换)`
      : '跟随后端默认模型 (点击选择)';
  }

  /* ---------- 浮层 ---------- */
  let popEl = null;
  function ensurePop() {
    if (!popEl) popEl = App.els.modelPopover || document.getElementById('modelPopover');
    return popEl;
  }
  function isOpen() { return !!popEl && !popEl.hidden; }
  function closePop() { if (popEl) popEl.hidden = true; }

  function positionPop(pop) {
    if (!_btn) return;
    const r = _btn.getBoundingClientRect();
    const left = Math.min(Math.max(8, r.left), window.innerWidth - pop.offsetWidth - 8);
    pop.style.left = left + 'px';
    pop.style.bottom = (window.innerHeight - r.top + 8) + 'px';
  }

  async function openPop() {
    const pop = ensurePop();
    if (!pop) return;
    pop.hidden = false;
    positionPop(pop);
    const s = pop.querySelector('.mpl-search');
    if (s) s.focus();
    renderPopList(pop);
  }

  function renderPopList(pop) {
    const list = pop.querySelector('.mpl-list');
    if (!list) return;
    const query = (pop.querySelector('.mpl-search') || {}).value || '';
    const providers = App.Settings._providers || [];
    const filtered = query
      ? providers.filter((p) => p.name.toLowerCase().includes(query.toLowerCase()) || p.id.toLowerCase().includes(query.toLowerCase()))
      : providers;
    if (!filtered.length) {
      list.innerHTML = '<div class="mpl-empty">未找到匹配的提供商</div>';
      return;
    }
    list.innerHTML = filtered.map((p) => {
      const configured = isConfigured(p.id);
      const selected = _state.provider === p.id;
      // 兼容后端返回的 default_model 或 models[0]
      const defaultModel = p.default_model || (p.models && p.models[0]) || '';
      const label = configured
        ? `${p.name}${defaultModel ? ' · ' + defaultModel : ''}`
        : `${p.name} (未配置)`;
      const cls = configured ? 'mpl-provider' : 'mpl-provider disabled';
      return `<div class="${cls}" data-prov="${p.id}">${label}</div>`;
    }).join('');
    list.querySelectorAll('.mpl-provider').forEach((el) => {
      el.addEventListener('click', () => {
        const pid = el.dataset.prov;
        if (!isConfigured(pid)) return;
        const p = providers.find((x) => x.id === pid);
        // 兼容后端返回的 default_model 或 models[0]
        const model = p.default_model || (p.models && p.models[0]) || '';
        set(pid, model || '');
        closePop();
      });
    });
  }

  /* ---------- 初始化 ---------- */
  function init() {
    load();
    refreshBadge();
    if (App.els.modelPickerBtn) {
      App.els.modelPickerBtn.addEventListener('click', () => {
        if (isOpen()) closePop(); else openPop();
      });
    }
    document.addEventListener('click', (e) => {
      if (!popEl || popEl.hidden || popEl.contains(e.target)) return;
      if (!App.els.modelPickerBtn || !App.els.modelPickerBtn.contains(e.target)) closePop();
    });
  }

  return { load, save, get, set, clear, init, isOpen, closePop, refreshBadge };
})();

/* 初始化 */
document.addEventListener('DOMContentLoaded', () => {
  App.ModelPicker.init();
});
