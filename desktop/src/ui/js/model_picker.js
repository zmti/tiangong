/* 主模型选择器(App.ModelPicker):按钮 + 浮层就地选模型
 *
 * 设计要点(与 App.Mode 同一套模式):
 * - provider/model 选择只存在 App.ModelPicker._state 一份,localStorage 仅作持久化副本;
 * - 按钮徽标显示当前模型,点击展开浮层:搜索 + 按提供商分组的模型清单,
 *   点击提供商标题行 = 用该提供商默认模型;未配置的提供商灰显不可选;
 * - 旧版分散键 agent_provider / agent_model_<pid> 首次启动自动迁移。
 */
App.ModelPicker = (() => {
  const STORE_KEY = 'model_state';

  let _state = { provider: '', model: '' };
  let _btn = null;

  /* ---------- 状态 ---------- */
  function load() {
    try {
      // 优先从统一存储加载
      if (App.Storage) {
        const saved = App.Storage.getCustom(STORE_KEY);
        if (saved && typeof saved === 'object') {
          _state.provider = typeof saved.provider === 'string' ? saved.provider : '';
          _state.model = typeof saved.model === 'string' ? saved.model : '';
          return;
        }
        // 迁移旧键
        const prov = App.Storage.get('provider');
        if (prov) {
          const model = App.Storage.getCustom(`model_${prov}`) || '';
          _state = { provider: prov, model };
          return;
        }
      }
      // 降级到 localStorage
      const raw = localStorage.getItem('agent_model_state');
      if (raw) {
        const saved = JSON.parse(raw);
        if (saved && typeof saved === 'object') {
          _state.provider = typeof saved.provider === 'string' ? saved.provider : '';
          _state.model = typeof saved.model === 'string' ? saved.model : '';
          return;
        }
      }
      // 迁移旧键:agent_provider + agent_model_<providerId>
      const prov = localStorage.getItem('agent_provider') || '';
      const model = prov ? (localStorage.getItem(`agent_model_${prov}`) || '') : '';
      if (prov) _state = { provider: prov, model };
    } catch (_) { /* 忽略 */ }
    save();
  }

  function save() {
    try {
      if (App.Storage) {
        App.Storage.setCustom(STORE_KEY, _state);
        if (_state.provider) {
          App.Storage.set('provider', _state.provider);
          App.Storage.setCustom(`model_${_state.provider}`, _state.model);
        }
      } else {
        localStorage.setItem('agent_model_state', JSON.stringify(_state));
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
    if (!_state.provider) return '默认模型';
    return _state.model
      ? `${providerName(_state.provider)} · ${_state.model}`
      : `${providerName(_state.provider)} · 默认`;
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
      const defaultModel = p.models && p.models[0];
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
        const model = p && p.models && p.models[0];
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

  return { load, save, get, set, clear, init, isOpen, closePop };
})();

/* 初始化 */
document.addEventListener('DOMContentLoaded', () => {
  App.ModelPicker.init();
});
