/* 协作模式中心(App.Mode):单一状态源 + 就地配置 popover + 模式徽标
 *
 * 设计要点:
 * - mode / critic / ensemble / 编排参数只存在 App.Mode._state 一份;
 *   UI 控件与请求参数(chat.js modeParams)都读写它,localStorage 仅作持久化副本。
 * - 模式分两组:基础(single/auto/review/ensemble)与多智能体团队(sequential/hierarchical/consensual)。
 * - 选中需要配置的模式时自动展开 popover,就地给出该模式的最小配置项;
 *   团队模式以"预设模板"呈现,底层候选执行体清单折叠进"高级自定义"。
 * - 防呆:评审模型与主模型相同、择优清单不足 2 个时给出提示。
 */
App.Mode = (() => {
  const STORE_KEY = 'agent_mode_state';
  const ENSEMBLE_MAX = 3;

  // 模式元数据:label 显示名 / desc 一句话说明 / cost 相对成本提示
  const MODES = {
    single:       { label: '快速回答', desc: '单模型直接作答,不做评审,最快最省', cost: '~0.8×', group: 'basic', needsConfig: false },
    auto:         { label: '自动',     desc: '跟随后端全局设置,默认推荐',          cost: '~1×',   group: 'basic', needsConfig: false },
    review:       { label: '质量把关', desc: '一个模型执行,另一个模型独立评审防幻觉', cost: '~1.5×', group: 'basic', needsConfig: true },
    ensemble:     { label: '多模型择优', desc: '多个模型并行作答,评审模型选出最佳',  cost: '~N×',   group: 'basic', needsConfig: true },
    sequential:   { label: '流水线团队', desc: '多个执行体按阶段顺序接力完成',       cost: '~2×',   group: 'team', needsConfig: true },
    hierarchical: { label: '分工协作', desc: '先拆解子任务,再分配给合适的执行体',    cost: '~2×+',  group: 'team', needsConfig: true },
    consensual:   { label: '擂台赛',   desc: '多执行体并行作答,投票选出最优',       cost: '~3×',   group: 'team', needsConfig: true },
  };

  // 团队模式预设模板:把"候选执行体清单"等底层参数打包成语义化模板
  const TEMPLATES = {
    pipeline: { mode: 'sequential',   label: '快审流水线', desc: '自动探索 → 独立评审,两阶段顺序完善', candidates: ['auto', 'review'] },
    split:    { mode: 'hierarchical', label: '分工协作',   desc: '拆解子任务,分配给合适执行体并汇总', candidates: [] },
    arena:    { mode: 'consensual',   label: '擂台赛',     desc: '单模型 / 自动 / 评审 三方并行投票',  candidates: ['single', 'auto', 'review'] },
  };
  const CANDIDATE_LABELS = { single: '快速回答', auto: '自动', review: '质量把关', ensemble: '多模型择优' };

  let _state = null;

  /* ---------- 状态 ---------- */
  function defaultState() {
    return {
      mode: 'auto',
      critic: '',                       // "provider:model" 或 ""
      ensemble: [],                     // ["provider:model", ...]
      orch: { template: '', subtasks: 3, candidates: [] },
    };
  }

  function load() {
    _state = defaultState();
    try {
      const raw = localStorage.getItem(STORE_KEY);
      if (raw) {
        const saved = JSON.parse(raw);
        if (saved && MODES[saved.mode]) _state.mode = saved.mode;
        if (typeof saved.critic === 'string') _state.critic = saved.critic;
        if (Array.isArray(saved.ensemble)) _state.ensemble = saved.ensemble.filter(Boolean).slice(0, ENSEMBLE_MAX);
        if (saved.orch && typeof saved.orch === 'object') {
          if (typeof saved.orch.template === 'string') _state.orch.template = saved.orch.template;
          const n = parseInt(saved.orch.subtasks, 10);
          if (Number.isFinite(n)) _state.orch.subtasks = Math.min(8, Math.max(1, n));
          if (Array.isArray(saved.orch.candidates)) {
            _state.orch.candidates = saved.orch.candidates.filter((c) => CANDIDATE_LABELS[c]);
          }
        }
        return;
      }
      // 迁移旧版分散的 localStorage 键(agent_mode / agent_critic / agent_ensemble)
      const legacyMode = localStorage.getItem('agent_mode');
      if (legacyMode && MODES[legacyMode]) _state.mode = legacyMode;
      _state.critic = localStorage.getItem('agent_critic') || '';
      _state.ensemble = (localStorage.getItem('agent_ensemble') || '')
        .split(',').map((s) => s.trim()).filter(Boolean).slice(0, ENSEMBLE_MAX);
    } catch (_) { /* 损坏的持久化数据按默认处理 */ }
    normalize();
    save();
  }

  function save() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(_state)); } catch (_) { /* 忽略 */ }
  }

  // 各编排流程缺省对应的规范模板
  const CANONICAL = { sequential: 'pipeline', hierarchical: 'split', consensual: 'arena' };

  function isOrch(mode) {
    return mode === 'sequential' || mode === 'hierarchical' || mode === 'consensual';
  }

  // 状态规范化:校验 mode/template 一致性,补齐规范默认模板。
  // 模板优先级:已匹配的显式模板 > 按候选清单反推的预设 > 有自定义候选则为 custom > 流程规范模板。
  function normalize() {
    if (!MODES[_state.mode]) _state.mode = 'auto';
    if (_state.orch.template !== 'custom' && !TEMPLATES[_state.orch.template]) _state.orch.template = '';
    if (!isOrch(_state.mode)) { _state.orch.template = ''; return; }
    const t = TEMPLATES[_state.orch.template];
    if (t && t.mode !== _state.mode) _state.orch.template = '';
    if (!_state.orch.template) {
      const match = Object.entries(TEMPLATES).find(([, tpl]) =>
        tpl.mode === _state.mode && tpl.candidates.join(',') === _state.orch.candidates.join(','));
      if (match) _state.orch.template = match[0];
      else if (_state.orch.candidates.length) _state.orch.template = 'custom';
      else _state.orch.template = CANONICAL[_state.mode];
    }
  }

  function effectiveCandidates() {
    const t = TEMPLATES[_state.orch.template];
    return t ? t.candidates.slice() : _state.orch.candidates.filter((c) => CANDIDATE_LABELS[c]);
  }

  function shortName(spec) {
    const i = spec.indexOf(':');
    return i >= 0 ? spec.slice(i + 1) : spec;
  }

  // "provider:model" 或 "provider/model" → { provider, model }(无分隔符 = 仅 provider)
  function parseSpec(spec) {
    if (typeof App.parseModelRef === 'function') return App.parseModelRef(spec);
    const i = spec.indexOf(':');
    return i >= 0
      ? { provider: spec.slice(0, i), model: spec.slice(i + 1) }
      : { provider: spec, model: '' };
  }

  // "provider:model" → "提供商名 · 模型名"(查不到缓存时回退原始 id)
  function prettySpec(spec) {
    const { provider, model } = parseSpec(spec);
    const p = (App.Settings._providers || []).find((x) => x.id === provider);
    const m = ((App._modelsCache || {})[provider] || []).find((x) => x.id === model);
    return `${p ? p.name : provider} · ${m ? m.name : (model || '默认')}`;
  }

  /* ---------- 对外 API ---------- */
  function params() {
    const p = { mode: _state.mode };
    if (_state.critic) {
      const i = _state.critic.indexOf(':');
      p.critic_provider = i >= 0 ? _state.critic.slice(0, i) : _state.critic;
      p.critic_model = i >= 0 ? _state.critic.slice(i + 1) : '';
    }
    if (_state.ensemble.length) p.models = _state.ensemble.slice();
    if (isOrch(_state.mode)) {
      if (_state.mode === 'hierarchical' && _state.orch.subtasks > 0) p.orch_subtasks = _state.orch.subtasks;
      const cands = effectiveCandidates();
      if (cands.length) p.orch_candidates = cands;
    }
    return p;
  }

  function get() { return JSON.parse(JSON.stringify(_state)); }
  function set(patch) {
    if (!patch) return;
    if (patch.mode && MODES[patch.mode]) _state.mode = patch.mode;
    if (typeof patch.critic === 'string') _state.critic = patch.critic;
    if (Array.isArray(patch.ensemble)) _state.ensemble = patch.ensemble.filter(Boolean).slice(0, ENSEMBLE_MAX);
    if (patch.orch && typeof patch.orch === 'object') {
      if (typeof patch.orch.template === 'string') _state.orch.template = patch.orch.template;
      if (patch.orch.subtasks !== undefined) {
        const n = parseInt(patch.orch.subtasks, 10);
        if (Number.isFinite(n)) _state.orch.subtasks = Math.min(8, Math.max(1, n));
      }
      if (Array.isArray(patch.orch.candidates)) {
        _state.orch.candidates = patch.orch.candidates.filter((c) => CANDIDATE_LABELS[c]);
      }
    }
    normalize();
    save();
    refreshBadge();
    notify();
  }

  const _listeners = [];
  function onChange(cb) { _listeners.push(cb); }
  function notify() { _listeners.forEach((cb) => { try { cb(get()); } catch (_) {} }); }

  /* ---------- 徽标 ---------- */
  function refreshBadge() {
    const badge = App.els.modeBadge;
    if (!badge) return;
    const meta = MODES[_state.mode];
    if (!meta || !meta.needsConfig) { badge.hidden = true; return; }
    badge.hidden = false;
    badge.classList.remove('warn');
    let text = '';
    switch (_state.mode) {
      case 'review':
        if (_state.critic) text = `评审 · ${shortName(_state.critic)}`;
        else { text = '评审 · 未指定'; badge.classList.add('warn'); }
        break;
      case 'ensemble': {
        const n = _state.ensemble.length;
        text = n > 0 ? `择优 ×${n}` : '择优 · 未配置';
        if (n < 2) badge.classList.add('warn');
        break;
      }
      case 'sequential': {
        const c = effectiveCandidates();
        text = `流水线 · ${c.length ? c.map((m) => (MODES[m] || {}).label || m).join('→') : '默认'}`;
        break;
      }
      case 'hierarchical':
        text = `分工 · ${_state.orch.subtasks} 任务`;
        break;
      case 'consensual': {
        const c = effectiveCandidates();
        text = `擂台 ×${c.length || 3}`;
        break;
      }
    }
    badge.textContent = text;
    badge.title = `${meta.label}:${meta.desc}${meta.cost ? ` · 成本${meta.cost}` : ''}`;
  }

  /* ---------- Popover ---------- */
  let popEl = null;

  function ensurePopover() {
    if (popEl) return popEl;
    popEl = App.els.modePopover || document.getElementById('modePopover');
    return popEl;
  }

  function isOpen() { return !!popEl && !popEl.hidden; }

  function closePop() {
    if (popEl) popEl.hidden = true;
  }

  function openPop() {
    const pop = ensurePopover();
    if (!pop) return;
    renderPop(pop);
    pop.hidden = false;
    positionPop(pop);
  }

  function togglePop() { isOpen() ? closePop() : openPop(); }

  function positionPop(pop) {
    const anchor = App.els.modeBadge;
    const ref = (anchor && !anchor.hidden ? anchor : App.els.modeSelect);
    if (!ref) return;
    const r = ref.getBoundingClientRect();
    const left = Math.min(Math.max(8, r.left), window.innerWidth - pop.offsetWidth - 8);
    pop.style.left = left + 'px';
    pop.style.bottom = (window.innerHeight - r.top + 8) + 'px';
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function sectionTitle(box, text) { box.appendChild(el('div', 'mp-title', text)); }
  function sectionNote(box, text) { box.appendChild(el('div', 'mp-note', text)); }

  function warnBox(box) {
    const w = el('div', 'mp-warn');
    w.style.display = 'none';
    box.appendChild(w);
    return w;
  }

  /* --- review:评审模型(与主选择器同一浮层组件) --- */
  function buildCriticSection(pop) {
    sectionTitle(pop, '评审模型');
    sectionNote(pop, '独立于执行模型,负责核查答案质量;留空则使用后端全局 AGENT_CRITIC_PROVIDER。');
    const row = el('div', 'mp-row');
    const btn = el('button', 'mini-btn mp-pick-btn');
    btn.type = 'button';
    const refreshBtn = () => {
      btn.textContent = _state.critic ? prettySpec(_state.critic) : '跟随全局配置';
      btn.title = _state.critic ? `${_state.critic}(点击更换)` : '点击指定评审模型';
    };
    btn.addEventListener('click', async () => {
      if (!App.ModelPicker || !App.ModelPicker.choose) return;
      const picked = await App.ModelPicker.choose({
        anchor: btn,
        current: parseSpec(_state.critic),
        allowEmpty: true,
        emptyLabel: '跟随全局配置',
        emptyDesc: '使用后端 AGENT_CRITIC_PROVIDER',
      });
      if (!picked) return; // 关闭浮层 = 取消
      _state.critic = picked.provider
        ? (picked.model ? `${picked.provider}:${picked.model}` : picked.provider)
        : '';
      save(); refreshBadge(); refreshWarn(); notify();
      refreshBtn();
    });
    row.appendChild(btn);
    pop.appendChild(row);
    const warn = warnBox(pop);

    // 与主模型相同时提示自评偏袒(critic 仅 provider 时按"主模型同提供商"也算)
    const refreshWarn = () => {
      const main = App.Settings.getSelectedModel();
      const same = !!_state.critic && !!main
        && (_state.critic === main
          || (!_state.critic.includes(':') && main.startsWith(`${_state.critic}:`)));
      warn.style.display = same ? 'flex' : 'none';
      if (same) warn.textContent = '⚠ 评审模型与主模型相同,可能自评偏袒,建议换一个模型。';
    };

    refreshBtn();
    refreshWarn();
  }

  /* --- ensemble:参与模型清单(添加走同一浮层组件) --- */
  function buildEnsembleSection(pop) {
    sectionTitle(pop, `参与模型(最多 ${ENSEMBLE_MAX} 个)`);
    sectionNote(pop, '多个模型并行作答同一任务,再由评审模型打分选优。');
    const list = el('div', 'mp-ens-list');
    pop.appendChild(list);
    const addBtn = el('button', 'mini-btn mp-add-btn', '＋ 添加模型');
    addBtn.type = 'button';
    pop.appendChild(addBtn);
    const warn = warnBox(pop);

    addBtn.addEventListener('click', async () => {
      if (_state.ensemble.length >= ENSEMBLE_MAX) return;
      if (!App.ModelPicker || !App.ModelPicker.choose) return;
      const picked = await App.ModelPicker.choose({
        anchor: addBtn,
        current: { provider: '', model: '' },
      });
      if (!picked || !picked.provider) return;
      // 只点提供商标题行 = 用其默认模型(spec 不带 :model,后端兼容)
      const spec = picked.model ? `${picked.provider}:${picked.model}` : picked.provider;
      if (!_state.ensemble.includes(spec)) {
        _state.ensemble.push(spec);
        save(); renderRows(); refreshBadge(); notify();
      }
    });

    function removeSpec(spec) {
      _state.ensemble = _state.ensemble.filter((s) => s !== spec);
      save(); renderRows(); refreshBadge(); notify();
    }

    function renderRows() {
      list.innerHTML = '';
      _state.ensemble.forEach((spec, idx) => {
        const row = el('div', 'ensemble-item');
        row.appendChild(el('span', 'ens-count', `#${idx + 1}`));
        row.appendChild(el('span', 'mp-ens-name', prettySpec(spec)));
        row.title = spec;
        const rm = el('button', 'ens-remove', '×');
        rm.type = 'button';
        rm.title = '移除此模型';
        rm.addEventListener('click', (ev) => {
          // renderRows 会立即把本按钮移出 DOM,阻断冒泡防止外层"点外部"误判
          ev.stopPropagation();
          removeSpec(spec);
        });
        row.appendChild(rm);
        list.appendChild(row);
      });
      const full = _state.ensemble.length >= ENSEMBLE_MAX;
      addBtn.textContent = full ? `已达上限(${ENSEMBLE_MAX})` : '＋ 添加模型';
      addBtn.disabled = full;
      warn.style.display = _state.ensemble.length < 2 ? 'flex' : 'none';
      warn.textContent = _state.ensemble.length === 0
        ? '⚠ 尚未配置参与模型,发送时将使用后端全局 AGENT_ENSEMBLE_MODELS。'
        : '⚠ 仅 1 个参与模型,效果等同单模型,建议至少添加 2 个。';
    }
    renderRows();
  }

  /* --- 团队模式:模板 + 高级自定义 --- */
  function buildTeamSection(pop) {
    sectionTitle(pop, '团队模板');
    sectionNote(pop, '模板决定由哪些执行体、按什么流程协作。');
    const grid = el('div', 'mp-tpl-grid');
    for (const [key, tpl] of Object.entries(TEMPLATES)) {
      const card = el('button', 'mp-tpl' + (_state.orch.template === key ? ' active' : ''));
      card.type = 'button';
      card.appendChild(el('div', 'mp-tpl-name', tpl.label));
      card.appendChild(el('div', 'mp-tpl-desc', tpl.desc));
      card.addEventListener('click', (ev) => {
        ev.stopPropagation(); // 点击后 renderPop 整体重渲染,本卡片会被移出 DOM
        _state.mode = tpl.mode;
        _state.orch.template = key;
        _state.orch.candidates = tpl.candidates.slice();
        save();
        if (App.els.modeSelect) App.els.modeSelect.value = tpl.mode;
        renderPop(pop);
        refreshBadge(); notify();
      });
      grid.appendChild(card);
    }
    pop.appendChild(grid);

    if (_state.mode === 'hierarchical') {
      const field = el('label', 'mp-field', '子任务数量 ');
      const num = document.createElement('input');
      num.type = 'number';
      num.min = '1'; num.max = '8'; num.step = '1';
      num.value = String(_state.orch.subtasks);
      num.className = 'mp-num';
      num.title = '层级编排生效:建议拆成几个子任务';
      num.addEventListener('change', () => {
        const n = parseInt(num.value, 10);
        if (Number.isFinite(n)) {
          _state.orch.subtasks = Math.min(8, Math.max(1, n));
          num.value = String(_state.orch.subtasks);
          save(); refreshBadge(); notify();
        }
      });
      field.appendChild(num);
      pop.appendChild(field);
    }

    const adv = el('details', 'mp-adv');
    adv.appendChild(el('summary', '', '高级:自定义执行体'));
    const chips = el('div', 'mp-chips');
    Object.entries(CANDIDATE_LABELS).forEach(([val, label]) => {
      const lab = el('label', 'mp-chip');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.value = val;
      cb.checked = effectiveCandidates().includes(val);
      cb.addEventListener('change', () => {
        const cur = new Set(effectiveCandidates());
        if (cb.checked) cur.add(val); else cur.delete(val);
        _state.orch.template = 'custom';
        _state.orch.candidates = Array.from(cur);
        save();
        grid.querySelectorAll('.mp-tpl').forEach((c) => c.classList.remove('active'));
        refreshBadge(); notify();
      });
      lab.appendChild(cb);
      lab.appendChild(document.createTextNode(' ' + label));
      chips.appendChild(lab);
    });
    adv.appendChild(chips);
    adv.appendChild(el('div', 'mp-note', '留空则由后端在全部执行模式中自动分配。'));
    pop.appendChild(adv);
  }

  function renderPop(pop) {
    pop.innerHTML = '';
    const meta = MODES[_state.mode];
    if (!meta) { closePop(); return; }
    const head = el('div', 'mp-head');
    head.appendChild(el('span', 'mp-name', meta.label));
    if (meta.cost) head.appendChild(el('span', 'mp-cost', meta.cost));
    pop.appendChild(head);
    pop.appendChild(el('div', 'mp-desc', meta.desc));

    if (_state.mode === 'review') buildCriticSection(pop);
    else if (_state.mode === 'ensemble') buildEnsembleSection(pop);
    else buildTeamSection(pop);
  }

  /* ---------- 初始化 ---------- */
  function init() {
    load();
    const sel = App.els.modeSelect;
    if (sel) {
      sel.value = _state.mode;
      sel.addEventListener('change', () => {
        set({ mode: sel.value });
        if (MODES[_state.mode].needsConfig) openPop(); else closePop();
      });
    }
    const badge = App.els.modeBadge;
    if (badge) {
      badge.addEventListener('click', (e) => { e.stopPropagation(); togglePop(); });
    }
    document.addEventListener('click', (e) => {
      const pop = ensurePopover();
      if (!isOpen()) return;
      const t = e.target;
      // 处理器在冒泡途中可能已把被点元素移出 DOM(删除成员 ×、切模板重渲染),
      // 此时 contains(t) 恒为 false,会被误判成"点外部"而整面板关闭
      if (!t || (typeof t.isConnected === 'boolean' && !t.isConnected)) return;
      if (pop.contains(t) || t === badge) return;
      // 模型选择浮层(.mp-chooser)由 App.ModelPicker 挂载在 body 上,
      // 不在本面板 DOM 内——其内部的点击不视为"点外部",否则选模型时整个面板会被误关
      if (typeof t.closest === 'function' && t.closest('.mp-chooser')) return;
      closePop();
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePop(); });
    window.addEventListener('resize', () => { if (isOpen()) positionPop(popEl); });
    refreshBadge();
  }

  return {
    MODES,
    TEMPLATES,
    init,
    get,
    set,
    params,
    onChange,
    isOrch,
    refreshBadge,
    openPopover: openPop,
    closePopover: closePop,
  };
})();
