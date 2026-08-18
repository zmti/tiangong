/* 设置模块:主题切换 / 后端信息展示 / 协作模式可视化选择器 */
App.Settings = {};

App.Settings.applyTheme = function (theme) {
  document.body.dataset.theme = theme;
  try { localStorage.setItem('agent_theme', theme); } catch (_) { /* 忽略 */ }
  const icon = App.els.themeBtn;
  if (icon) icon.textContent = theme === 'dark' ? '☀️' : '🌙';
};

App.Settings.currentTheme = function () {
  try { return localStorage.getItem('agent_theme') || document.body.dataset.theme || 'light'; } catch (_) { return 'light'; }
};

App.Settings.toggleTheme = function () {
  App.Settings.applyTheme(App.Settings.currentTheme() === 'dark' ? 'light' : 'dark');
};

App.Settings.refreshBackend = async function () {
  const ok = await App.pingBackend();
  App.setStatus(ok);
  if (App.els.backendUrlText) App.els.backendUrlText.textContent = App.backendUrl || '-';
  if (App.els.backendStatusText) App.els.backendStatusText.textContent = ok ? '已连接' : '未连接';
  return ok;
};

/* ---------- 协作模式可视化选择器 ---------- */

// 从 provider:model 字符串解析出 provider 和 model
function _parseProviderModel(s) {
  if (!s) return { provider: '', model: '' };
  const idx = s.indexOf(':');
  return idx >= 0 ? { provider: s.slice(0, idx).trim(), model: s.slice(idx + 1).trim() } : { provider: s.trim(), model: '' };
}

// 将 provider:model 格式化回字符串
function _formatProviderModel(p, m) {
  return [p, m].filter(Boolean).join(':') || '';
}

// 缓存已加载的模型数据 { providerId -> [{id, name, speed, quality}] }
App._modelsCache = {};

// 加载所有可用 provider 列表
App.Settings.loadProviders = async function () {
  try {
    const res = await App.api('/v1/providers');
    if (!res.ok) return;
    const data = await res.json();
    const providers = data.providers || [];
    // 填充 critic provider 下拉
    if (App.els.criticProviderSelect) {
      App.els.criticProviderSelect.innerHTML = '<option value="">提供商</option>';
      providers.forEach((p) => {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = (p.configured ? '' : '(未配置) ') + p.name;
        opt.disabled = !p.configured;
        App.els.criticProviderSelect.appendChild(opt);
      });
    }
    // 填充 ensemble 添加行 provider 下拉
    if (App.els.ensembleProvSelect) {
      App.els.ensembleProvSelect.innerHTML = '<option value="">提供商</option>';
      providers.forEach((p) => {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = (p.configured ? '' : '(未配置) ') + p.name;
        opt.disabled = !p.configured;
        App.els.ensembleProvSelect.appendChild(opt);
      });
    }
  } catch (_) { /* 忽略 */ }
};

// 加载指定 provider 的模型列表
App.Settings.loadModels = async function (providerId, selectEl) {
  if (!providerId || !selectEl) return;
  try {
    const res = await App.api(`/v1/models?provider=${encodeURIComponent(providerId)}`);
    if (!res.ok) { selectEl.innerHTML = '<option value="">加载失败</option>'; return; }
    const data = await res.json();
    selectEl.innerHTML = '<option value="">选择模型</option>';
    (data.models || []).forEach((m) => {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.name + (m.speed ? ' [' + m.speed + ']' : '');
      opt.title = (m.quality || '') + (m.speed ? ' | Speed: ' + m.speed : '');
      selectEl.appendChild(opt);
    });
    // 缓存
    App._modelsCache[providerId] = data.models || [];
  } catch (_) {
    selectEl.innerHTML = '<option value="">加载失败</option>';
  }
};

// 恢复 critic 选择器的已保存值
App.Settings.restoreCritic = function () {
  const saved = localStorage.getItem('agent_critic') || '';
  const { provider, model } = _parseProviderModel(saved);
  if (App.els.criticProviderSelect) App.els.criticProviderSelect.value = provider;
  if (App.els.criticModelSelect) App.els.criticModelSelect.value = model;
  if (provider) App.Settings.loadModels(provider, App.els.criticModelSelect);
};

// 恢复 ensemble 列表的已保存值
App.Settings.restoreEnsemble = function () {
  const saved = localStorage.getItem('agent_ensemble') || '';
  const items = saved.split(',').map((s) => s.trim()).filter(Boolean);
  App.Settings.renderEnsembleList(items);
};

// 渲染 ensemble 模型列表 UI
App.Settings.renderEnsembleList = function (items) {
  if (!App.els.ensembleList) return;
  App.els.ensembleList.innerHTML = '';
  (items || []).forEach((item, idx) => {
    const { provider, model } = _parseProviderModel(item);
    const row = document.createElement('div');
    row.className = 'ensemble-item';
    // provider 下拉
    const provSel = document.createElement('select');
    provSel.className = 'set-select set-select-sm ens-prov';
    provSel.innerHTML = '<option value="">提供商</option>';
    row.appendChild(provSel);
    // model 下拉
    const modSel = document.createElement('select');
    modSel.className = 'set-select set-select-sm ens-model';
    modSel.innerHTML = '<option value="">选择模型</option>';
    row.appendChild(modSel);
    // 数量角标
    const countBadge = document.createElement('span');
    countBadge.className = 'ens-count';
    countBadge.textContent = `#${idx + 1}`;
    row.appendChild(countBadge);
    // 删除按钮
    const rmBtn = document.createElement('button');
    rmBtn.className = 'ens-remove';
    rmBtn.textContent = '×';
    rmBtn.title = '移除此模型';
    rmBtn.addEventListener('click', () => {
      row.remove();
      App.Settings.saveEnsembleFromUI();
    });
    row.appendChild(rmBtn);
    App.els.ensembleList.appendChild(row);
    // 恢复值
    provSel.value = provider;
    modSel.value = model;
    // provider 变更时加载对应模型
    provSel.addEventListener('change', () => {
      const pid = provSel.value;
      modSel.innerHTML = '<option value="">选择模型</option>';
      if (pid) App.Settings.loadModels(pid, modSel);
      App.Settings.saveEnsembleFromUI();
    });
    modSel.addEventListener('change', () => { App.Settings.saveEnsembleFromUI(); });
    // 初始加载
    if (provider) App.Settings.loadModels(provider, modSel);
  });
};

// 从 UI 收集 ensemble 值并持久化
App.Settings.saveEnsembleFromUI = function () {
  if (!App.els.ensembleList) return;
  const items = [];
  App.els.ensembleList.querySelectorAll('.ensemble-item').forEach((row) => {
    const p = (row.querySelector('.ens-prov')?.value) || '';
    const m = (row.querySelector('.ens-model')?.value) || '';
    const val = _formatProviderModel(p, m);
    if (val) items.push(val);
  });
  const raw = items.join(',');
  localStorage.setItem('agent_ensemble', raw);
  // 同步隐藏的 input(兼容 chat.js 读取)
  if (App.els.ensembleModelsInput) App.els.ensembleModelsInput.value = raw;
};

// 从 UI 收集 critic 值并持久化
App.Settings.saveCriticFromUI = function () {
  const p = App.els.criticProviderSelect?.value || '';
  const m = App.els.criticModelSelect?.value || '';
  const val = _formatProviderModel(p, m);
  localStorage.setItem('agent_critic', val);
  if (App.els.criticModelInput) App.els.criticModelInput.value = val;
};
