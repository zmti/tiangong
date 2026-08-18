/* 设置模块:主题切换 / 后端信息展示 */
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
