/* 协作模式中心 - 使用统一存储 */
App.Mode = (() => {
  const ENSEMBLE_MAX = 3;

  // 模式元数据
  const MODES = {
    single:       { label: '快速回答', desc: '单模型直接作答,不做评审,最快最省', cost: '~0.8×', group: 'basic', needsConfig: false },
    auto:         { label: '自动',     desc: '跟随后端全局设置,默认推荐',          cost: '~1×',   group: 'basic', needsConfig: false },
    review:       { label: '质量把关', desc: '一个模型执行,另一个模型独立评审防幻觉', cost: '~1.5×', group: 'basic', needsConfig: true },
    ensemble:     { label: '多模型择优', desc: '多个模型并行作答,评审模型选出最佳',  cost: '~N×',   group: 'basic', needsConfig: true },
    sequential:   { label: '流水线团队', desc: '多个执行体按阶段顺序接力完成',       cost: '~2×',   group: 'team', needsConfig: true },
    hierarchical: { label: '分工协作', desc: '先拆解子任务,再分配给合适的执行体',    cost: '~2×+',  group: 'team', needsConfig: true },
    consensual:   { label: '擂台赛',   desc: '多执行体并行作答,投票选出最优',       cost: '~3×',   group: 'team', needsConfig: true },
  };

  // 团队模式预设模板
  const TEMPLATES = {
    pipeline: { mode: 'sequential',   label: '快审流水线', desc: '自动探索 → 独立评审,两阶段顺序完善', candidates: ['auto', 'review'] },
    split:    { mode: 'hierarchical', label: '分工协作',   desc: '拆解子任务,分配给合适执行体并汇总', candidates: [] },
    arena:    { mode: 'consensual',   label: '擂台赛',     desc: '单模型 / 自动 / 评审 三方并行投票',  candidates: ['single', 'auto', 'review'] },
  };
  const CANDIDATE_LABELS = { single: '快速回答', auto: '自动', review: '质量把关', ensemble: '多模型择优' };

  let _state = null;

  function defaultState() {
    return {
      mode: 'auto',
      critic: '',
      ensemble: [],
      orch: { template: '', subtasks: 3, candidates: [] },
    };
  }

  function load() {
    _state = defaultState();
    try {
      // 优先从统一存储加载
      if (App.Storage) {
        const mode = App.Storage.get('mode');
        if (mode && MODES[mode]) _state.mode = mode;
        // 加载其他模式相关状态
        const critic = App.Storage.getCustom('mode_critic');
        if (typeof critic === 'string') _state.critic = critic;
        const ensemble = App.Storage.getCustom('mode_ensemble');
        if (Array.isArray(ensemble)) _state.ensemble = ensemble.filter(Boolean).slice(0, ENSEMBLE_MAX);
        const orch = App.Storage.getCustom('mode_orch');
        if (orch && typeof orch === 'object') {
          if (typeof orch.template === 'string') _state.orch.template = orch.template;
          const n = parseInt(orch.subtasks, 10);
          if (Number.isFinite(n)) _state.orch.subtasks = Math.min(8, Math.max(1, n));
          if (Array.isArray(orch.candidates)) {
            _state.orch.candidates = orch.candidates.filter((c) => CANDIDATE_LABELS[c]);
          }
        }
        return;
      }
      // 降级到 localStorage
      const raw = localStorage.getItem('agent_mode_state');
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
      // 迁移旧键
      const legacyMode = localStorage.getItem('agent_mode');
      if (legacyMode && MODES[legacyMode]) _state.mode = legacyMode;
      _state.critic = localStorage.getItem('agent_critic') || '';
      _state.ensemble = (localStorage.getItem('agent_ensemble') || '')
        .split(',').map((s) => s.trim()).filter(Boolean).slice(0, ENSEMBLE_MAX);
    } catch (_) {}
    normalize();
    save();
  }

  function save() {
    try {
      if (App.Storage) {
        App.Storage.set('mode', _state.mode);
        App.Storage.setCustom('mode_critic', _state.critic);
        App.Storage.setCustom('mode_ensemble', _state.ensemble);
        App.Storage.setCustom('mode_orch', _state.orch);
      } else {
        localStorage.setItem('agent_mode_state', JSON.stringify(_state));
      }
    } catch (_) {}
  }

  function normalize() {
    if (!MODES[_state.mode]) _state.mode = 'auto';
    if (_state.orch.template !== 'custom' && !TEMPLATES[_state.orch.template]) _state.orch.template = '';
    if (_state.mode !== 'sequential' && _state.mode !== 'hierarchical' && _state.mode !== 'consensual') {
      _state.orch.template = ''; 
      return;
    }
    const t = TEMPLATES[_state.orch.template];
    if (t && t.mode !== _state.mode) _state.orch.template = '';
    if (!_state.orch.template) {
      const match = Object.entries(TEMPLATES).find(([, tpl]) =>
        tpl.mode === _state.mode && tpl.candidates.join(',') === _state.orch.candidates.join(','));
      if (match) _state.orch.template = match[0];
      else if (_state.orch.candidates.length) _state.orch.template = 'custom';
      else _state.orch.template = { sequential: 'pipeline', hierarchical: 'split', consensual: 'arena' }[_state.mode];
    }
  }

  function params() {
    return {
      mode: _state.mode,
      critic: _state.critic,
      ensemble: _state.ensemble,
      orch: _state.orch,
    };
  }

  function get() { return _state; }

  function setMode(mode) {
    if (MODES[mode]) {
      _state.mode = mode;
      if (MODES[mode].group === 'team' && !_state.orch.template) {
        _state.orch.template = { sequential: 'pipeline', hierarchical: 'split', consensual: 'arena' }[mode];
      }
      normalize();
      save();
      refreshUI();
    }
  }

  function refreshUI() {
    const badge = App.els.modeBadge;
    const sel = App.els.modeSelect;
    if (sel) sel.value = _state.mode;
    if (badge) {
      const meta = MODES[_state.mode];
      badge.textContent = meta ? meta.label : _state.mode;
      badge.title = meta ? meta.desc : '';
    }
  }

  return { load, save, get, setMode, params, normalize, refreshUI };
})();

/* 初始化 */
document.addEventListener('DOMContentLoaded', () => {
  App.Mode.load();
  App.Mode.refreshUI();
});
