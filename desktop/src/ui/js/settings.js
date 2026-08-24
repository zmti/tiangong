/* 设置模块:主题切换 / 后端信息展示 / provider·模型数据缓存
 * 协作模式配置已迁至 js/mode.js;主模型选择 UI 已迁至 js/model_picker.js */
App.Settings = {};

// 缓存已加载的provider/model数据
App.Settings._providers = [];
App.Settings._modelsByProvider = {};

// 获取当前选中的 provider:model(App.ModelPicker 的只读视图;
// 仅当 provider 与 model 都明确指定时返回非空,与旧语义一致)
App.Settings.getSelectedModel = function () {
  if (App.ModelPicker) {
    const s = App.ModelPicker.get();
    return s.provider && s.model ? `${s.provider}:${s.model}` : '';
  }
  return '';
};

// 应用主题 - 使用统一存储
App.Settings.applyTheme = function (theme) {
  document.body.dataset.theme = theme;
  if (App.Storage) {
    App.Storage.set('theme', theme);
  } else {
    try { localStorage.setItem('agent_theme', theme); } catch (_) {}
  }
  const icon = App.els.themeBtn;
  if (icon) {
    const moon = icon.querySelector('.icon-moon');
    const sun = icon.querySelector('.icon-sun');
    if (moon && sun) {
      moon.style.display = theme === 'dark' ? 'none' : '';
      sun.style.display = theme === 'dark' ? '' : 'none';
    }
  }
  // 同步设置页的 toggle 开关状态
  const themeToggle = App.els.themeToggle || document.getElementById('themeToggle');
  if (themeToggle) {
    themeToggle.classList.toggle('on', theme === 'dark');
  }
};

// 获取当前主题 - 使用统一存储
App.Settings.currentTheme = function () {
  if (App.Storage) {
    return App.Storage.get('theme') || 'light';
  }
  try { return localStorage.getItem('agent_theme') || document.body.dataset.theme || 'light'; } catch (_) { return 'light'; }
};

// 切换主题
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

App.Settings.setStatus = function (ok) {
  const dot = App.els.statusDot;
  const txt = App.els.statusText;
  if (dot) dot.className = ok ? 'status-dot connected' : 'status-dot disconnected';
  if (txt) txt.textContent = ok ? '已连接' : '未连接';
};

/* ---------- 主模型选择器辅助(loadModels / provider 缓存) ---------- */

// 缓存已加载的模型数据 { providerId -> [{id, name, speed, quality}] }
App._modelsCache = {};

// 加载所有可用 provider 列表并缓存(协作模式 popover 也从缓存读取)
App.Settings.loadProviders = async function () {
  try {
    const res = await App.api('/v1/providers');
    if (!res.ok) return;
    const data = await res.json();
    App.Settings._providers = data.providers || [];
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

/* 初始化 */
document.addEventListener('DOMContentLoaded', () => {
  if (App.els.themeBtn) {
    App.els.themeBtn.addEventListener('click', App.Settings.toggleTheme);
  }
  if (App.els.themeToggle) {
    App.els.themeToggle.addEventListener('click', () => {
      App.Settings.toggleTheme();
    });
  }
  // 应用初始主题
  const initialTheme = App.Storage ? App.Storage.get('theme') : 'light';
  if (initialTheme) {
    document.body.dataset.theme = initialTheme;
  }
  // 加载 providers
  App.Settings.loadProviders();
});
