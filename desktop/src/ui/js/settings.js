/* 设置模块:主题切换 / 后端信息展示 - 使用统一存储 */
App.Settings = {};

// 获取当前选中的 provider:model
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
  const themeToggle = document.getElementById('themeToggle');
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
};

App.Settings.setStatus = function (ok) {
  const dot = App.els.statusDot;
  const txt = App.els.statusText;
  if (dot) dot.className = ok ? 'status-dot connected' : 'status-dot disconnected';
  if (txt) txt.textContent = ok ? '已连接' : '未连接';
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
});
