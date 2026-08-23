/* 主模型选择器(App.ModelPicker):按钮 + 浮层就地选模型
 *
 * 设计要点(与 App.Mode 同一套模式):
 * - provider/model 选择只存在 App.ModelPicker._state 一份,localStorage 仅作持久化副本;
 * - 按钮徽标显示当前模型,点击展开浮层:搜索 + 按提供商分组的模型清单,
 *   点击提供商标题行 = 用该提供商默认模型;未配置的提供商灰显不可选;
 * - 旧版分散键 agent_provider / agent_model_<pid> 首次启动自动迁移。
 */
App.ModelPicker = (() => {
  const STORE_KEY = 'agent_model_state';

  let _state = { provider: '', model: '' };
  let _btn = null;

  /* ---------- 状态 ---------- */
  function load() {
    try {
      const raw = localStorage.getItem(STORE_KEY);
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
    try { localStorage.setItem(STORE_KEY, JSON.stringify(_state)); } catch (_) { /* 忽略 */ }
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
    pop.dataset.kw = '';
    renderLoading(pop);
    pop.hidden = false;
    positionPop(pop);
    // 补拉缺失的模型列表(已缓存的直接用),再渲染
    await prefetchModels();
    if (!isOpen()) return; // 用户已关闭
    renderPop(pop);
    positionPop(pop);
  }

  function togglePop() { isOpen() ? closePop() : openPop(); }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function renderLoading(pop) {
    pop.innerHTML = '';
    pop.appendChild(el('div', 'mpl-loading', '加载模型列表…'));
  }

  /* 共享渲染:搜索框 + 可选"留空"行 + 按提供商分组的模型清单。
   * 主选择器与 choose()(评审模型/择优成员等场景)共用。 */
  function renderModelList(pop, cfg) {
    pop.innerHTML = '';

    const search = document.createElement('input');
    search.type = 'text';
    search.className = 'mpl-search';
    search.placeholder = '搜索提供商 / 模型…';
    search.value = pop.dataset.kw || '';
    search.addEventListener('input', () => {
      pop.dataset.kw = search.value;
      renderModelList(pop, cfg);
      const s = pop.querySelector('.mpl-search');
      if (s) { s.focus(); s.setSelectionRange(s.value.length, s.value.length); }
    });
    pop.appendChild(search);

    const pick = cfg.onPick;
    const cache = App._modelsCache || {};

    if (cfg.allowEmpty) {
      const isEmpty = !cfg.current.provider;
      const defRow = el('div', 'mpl-row' + (isEmpty ? ' active' : ''));
      defRow.appendChild(el('span', 'mpl-name', cfg.emptyLabel || '默认模型'));
      defRow.appendChild(el('span', 'mpl-desc', cfg.emptyDesc || '跟随后端 AGENT_LLM_PROVIDER 配置'));
      defRow.addEventListener('click', () => pick('', ''));
      pop.appendChild(defRow);
    }

    const kw = (pop.dataset.kw || '').trim().toLowerCase();
    const providers = (App.Settings._providers || []).filter((p) => {
      if (!kw) return true;
      if ((p.name || '').toLowerCase().includes(kw) || (p.id || '').toLowerCase().includes(kw)) return true;
      return (cache[p.id] || []).some((m) =>
        (m.name || '').toLowerCase().includes(kw) || (m.id || '').toLowerCase().includes(kw));
    });

    providers.forEach((p) => {
      const models = cache[p.id] || [];
      const filtered = models.filter((m) => !kw
        || (m.name || '').toLowerCase().includes(kw)
        || (m.id || '').toLowerCase().includes(kw));
      if (kw && !filtered.length) return;

      const group = el('div', 'mpl-group');
      const head = el('div', 'mpl-group-head' + (p.configured ? '' : ' disabled'));
      head.appendChild(el('span', 'mpl-gname', p.name + (p.configured ? '' : '(未配置)')));
      if (p.configured) {
        head.title = `使用 ${p.name} 的默认模型`;
        head.addEventListener('click', () => pick(p.id, ''));
      }
      group.appendChild(head);

      if (!p.configured) {
        group.appendChild(el('div', 'mpl-empty', '缺少 API Key,暂不可用'));
      } else if (!filtered.length) {
        group.appendChild(el('div', 'mpl-empty', '该提供商无模型列表,点击标题用其默认模型'));
      }
      filtered.forEach((m) => {
        const isActive = cfg.current.provider === p.id && cfg.current.model === m.id;
        const row = el('div', 'mpl-row' + (isActive ? ' active' : ''));
        const nameSpan = el('span', 'mpl-name', m.name);
        if (m.speed) nameSpan.appendChild(el('span', 'mpl-tag', m.speed));
        if (m.quality) nameSpan.title = m.quality;
        row.appendChild(nameSpan);
        if (m.speed) row.appendChild(el('span', 'mpl-tag', m.speed));
        if (m.quality) row.title = m.quality;
        row.addEventListener('click', () => pick(p.id, m.id));
        group.appendChild(row);
      });
      pop.appendChild(group);
    });

    if (!providers.length) pop.appendChild(el('div', 'mpl-empty', '后端未返回可用提供商'));
  }

  function prefetchModels() {
    const providers = App.Settings._providers || [];
    const missing = providers.filter((p) => p.configured
      && !(App._modelsCache && App._modelsCache[p.id]));
    return Promise.all(missing.map(async (p) => {
      try { await App.Settings.loadModels(p.id, document.createElement('select')); } catch (_) { /* 忽略 */ }
    }));
  }

function renderPop(pop) {
    renderModelList(pop, {
      current: get(),
      allowEmpty: true,
      emptyLabel: '默认模型',
      onPick: (p, m) => { set(p, m); closePop(); },
    });
  }

  /* ---------- 通用选择器(评审模型/择优成员等场景复用) ---------- */
  let _chooserEl = null;
  let _chooserClose = null;

  function positionAtAnchor(pop, anchor) {
    if (anchor && anchor.getBoundingClientRect) {
      const r = anchor.getBoundingClientRect();
      pop.style.left = Math.min(Math.max(8, r.left), Math.max(8, window.innerWidth - pop.offsetWidth - 8)) + 'px';
      pop.style.bottom = (window.innerHeight - r.top + 8) + 'px';
      return;
    }
    // 无锚点:屏幕下方居中
    pop.style.left = Math.max(8, Math.round((window.innerWidth - pop.offsetWidth) / 2)) + 'px';
    pop.style.bottom = '80px';
  }

  /* 打开一个临时模型选择浮层。返回 Promise:
   * 选中 → { provider, model }(model 为空串 = 该提供商默认);关闭(Esc / 点外部 / 被新浮层顶替)→ null */
  function choose(opts = {}) {
    const {
      anchor = null,
      current = { provider: '', model: '' },
      allowEmpty = false,
      emptyLabel = '跟随全局配置',
      emptyDesc = '',
    } = opts;

    return new Promise((resolve) => {
      if (_chooserClose) _chooserClose(null); // 同时只允许一个选择器
      const pop = el('div', 'mode-popover mpl-pop mp-chooser');

      let done = false;
      const finish = (val) => {
        if (done) return;
        done = true;
        document.removeEventListener('mousedown', outside, true);
        document.removeEventListener('keydown', onKey, true);
        pop.remove();
        if (_chooserEl === pop) _chooserEl = null;
        if (_chooserClose === finish) _chooserClose = null;
        resolve(val);
      };

      const outside = (e) => { if (!pop.contains(e.target)) finish(null); };
      const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); finish(null); } };
      // 等触发它打开的那次点击冒泡结束再监听,避免"一打开就被关掉"
      setTimeout(() => {
        document.addEventListener('mousedown', outside, true);
        document.addEventListener('keydown', onKey, true);
      }, 0);

      _chooserEl = pop;
      _chooserClose = finish;
      document.body.appendChild(pop);
      renderLoading(pop);
      positionAtAnchor(pop, anchor);

      prefetchModels().then(() => {
        if (_chooserEl !== pop) return;
        renderModelList(pop, {
          current,
          allowEmpty,
          emptyLabel,
          emptyDesc,
          onPick: (p, m) => finish({ provider: p, model: m }),
        });
        positionAtAnchor(pop, anchor);
      });
    });
  }

  /* ---------- 初始化 ---------- */
  function init() {
    load();
    _btn = App.els.modelPickerBtn || null;
    if (_btn) _btn.addEventListener('click', (e) => { e.stopPropagation(); togglePop(); });
    const pop = ensurePop();
    if (pop) pop.addEventListener('click', (e) => e.stopPropagation());
    document.addEventListener('click', (e) => {
      if (!isOpen()) return;
      if ((pop && pop.contains(e.target)) || e.target === _btn) return;
      // choose() 的临时浮层挂在 body 上,其内点击不算"点外部"
      const t = e.target;
      if (t && typeof t.closest === 'function' && t.closest('.mp-chooser')) return;
      closePop();
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePop(); });
    window.addEventListener('resize', () => { if (isOpen()) positionPop(pop); });
    refreshBadge();
  }

  function isChoosing() { return !!_chooserEl; }

  return { init, get, set, clear, onChange, refreshBadge, choose, isChoosing };
})();
