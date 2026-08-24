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
    const res = await App.apiJSON('/v1/providers');
    if (!res.success) return;
    App.Settings._providers = res.data?.providers || [];
  } catch (_) { /* 忽略 */ }
};

// 加载指定 provider 的模型列表
App.Settings.loadModels = async function (providerId, selectEl) {
  if (!providerId || !selectEl) return;
  try {
    const res = await App.apiJSON(`/v1/models?provider=${encodeURIComponent(providerId)}`);
    if (!res.success) { selectEl.innerHTML = '<option value="">加载失败</option>'; return; }
    const data = res.data;
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

/* ---------- 模型服务管理(自定义 OpenAI 兼容提供商) ---------- */

App.Settings._provCustoms = [];

// 后端缺新接口(404)= 旧版后端进程残留,给出可操作指引
function provStaleHint() {
  return '后端版本较旧(接口不存在)。请完全退出应用(托盘图标右键 → 退出)后重新打开,新版启动器会自动替换旧后端。';
}

App.Settings.renderProviderAdmin = async function () {
  const box = document.getElementById('providerAdmin');
  if (!box) return;
  try {
      const res = await App.apiJSON('/v1/providers/custom');
      if (res.success) App.Settings._provCustoms = (res.data?.providers) || [];
    } catch (_) { /* 忽略 */ }

  box.textContent = '';
  App.Settings._provCustoms.forEach((p) => {
    const row = document.createElement('div');
    row.className = 'prov-item';
    const info = document.createElement('div');
    info.className = 'prov-info';
    const name = document.createElement('b');
    name.textContent = p.name;
    const meta = document.createElement('span');
    meta.className = 'muted';
    meta.textContent = (p.model || '未设默认模型') + (p.api_key_masked ? ' · ' + p.api_key_masked : '');
    meta.title = p.base_url || '';
    info.append(name, meta);
    const ops = document.createElement('div');
    ops.className = 'prov-ops';
    const editBtn = document.createElement('button');
    editBtn.type = 'button';
    editBtn.textContent = '编辑';
    editBtn.addEventListener('click', () => App.Settings._openProvForm(p));
    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'danger';
    delBtn.textContent = '删除';
    delBtn.addEventListener('click', async () => {
      if (!window.confirm(`删除提供商「${p.name}」?正在使用它的会话将回退到全局配置。`)) return;
      try { await App.apiJSON('/v1/providers/custom/' + encodeURIComponent(p.id), { method: 'DELETE' }); } catch (_) { /* 忽略 */ }
      App.Settings.refreshProvidersUI();
    });
    ops.append(editBtn, delBtn);
    row.append(info, ops);
    box.appendChild(row);
  });

  const addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.className = 'lb-btn prov-add';
  addBtn.textContent = '＋ 添加 OpenAI 兼容提供商';
  addBtn.addEventListener('click', () => App.Settings._openProvForm(null));
  box.appendChild(addBtn);

  const formWrap = document.createElement('div');
  formWrap.id = 'provFormWrap';
  box.appendChild(formWrap);
};

App.Settings.refreshProvidersUI = async function () {
  await App.Settings.renderProviderAdmin();
  await App.Settings.loadProviders();
  App._modelsCache = {};
};

// 打开表单;p=null 新建,p=条目 编辑(Key 留空沿用旧值)
App.Settings._openProvForm = function (p) {
  const wrap = document.getElementById('provFormWrap');
  if (!wrap) return;
  wrap.textContent = '';

  const form = document.createElement('div');
  form.className = 'prov-form';

  const mkField = (labelText, inputEl) => {
    const label = document.createElement('label');
    const span = document.createElement('span');
    span.textContent = labelText;
    label.append(span, inputEl);
    return label;
  };

  const nameIn = document.createElement('input');
  nameIn.type = 'text';
  nameIn.placeholder = '如:硅基流动 / DeepSeek 中转 / 公司网关';
  nameIn.value = p ? p.name : '';

  const urlIn = document.createElement('input');
  urlIn.type = 'text';
  urlIn.placeholder = 'https://api.example.com/v1';
  urlIn.value = p ? p.base_url : '';

  const keyIn = document.createElement('input');
  keyIn.type = 'password';
  keyIn.placeholder = p && p.api_key_masked ? `已保存 ${p.api_key_masked},留空则不修改` : 'sk-...';
  keyIn.autocomplete = 'off';

  // 默认模型:datalist 兼顾手填与下拉选择
  const dlId = 'provModelDatalist';
  const modelIn = document.createElement('input');
  modelIn.type = 'text';
  modelIn.setAttribute('list', dlId);
  modelIn.placeholder = '获取模型列表后选择,或直接手填';
  modelIn.value = p ? (p.model || '') : '';
  const dl = document.createElement('datalist');
  dl.id = dlId;

  const status = document.createElement('div');
  status.className = 'prov-status muted';

  const ops = document.createElement('div');
  ops.className = 'prov-form-ops';

  const fetchBtn = document.createElement('button');
  fetchBtn.type = 'button';
  fetchBtn.textContent = '🔄 获取模型列表';
  const testBtn = document.createElement('button');
  testBtn.type = 'button';
  testBtn.textContent = '测试连接';
  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.className = 'primary';
  saveBtn.textContent = '保存';
  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.textContent = '取消';

  const baseUrl = () => {
    let u = urlIn.value.trim();
    if (u && !/^https?:\/\/$/i.test(u)) u = 'https://' + u;
    return u.replace(/\/+$/, '');
  };
  const apiKey = () => keyIn.value.trim() || (p ? '' : '');

  const probe = async () => {
    const base = baseUrl();
    if (!base) { status.textContent = '请先填写 Base URL'; status.className = 'prov-status err'; return null; }
    status.textContent = '请求中…';
    status.className = 'prov-status muted';
    try {
      const res = await App.apiJSON('/v1/providers/custom/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ base_url: base, api_key: keyIn.value.trim(), id: p ? p.id : null }),
      });
      if (!res.success && res.status === 404) {
        status.textContent = '失败:' + provStaleHint();
        status.className = 'prov-status err';
        return null;
      }
      if (!res.success) {
        status.textContent = '失败:' + (res.detail || '未知错误');
        status.className = 'prov-status err';
        return null;
      }
      const data = res.data;
      if (!data.ok) {
        status.textContent = '失败:' + (data.error || '未知错误');
        status.className = 'prov-status err';
        return null;
      }
      return data.models || [];
    } catch (e) {
      status.textContent = '失败:' + e.message;
      status.className = 'prov-status err';
      return null;
    }
  };

  fetchBtn.addEventListener('click', async () => {
    const models = await probe();
    if (models === null) return;
    dl.textContent = '';
    models.forEach((m) => {
      const opt = document.createElement('option');
      opt.value = m;
      dl.appendChild(opt);
    });
    status.textContent = `成功,发现 ${models.length} 个模型`;
    status.className = 'prov-status ok';
  });

  testBtn.addEventListener('click', async () => {
    const models = await probe();
    if (models === null) return;
    status.textContent = `连接正常(发现 ${models.length} 个模型)`;
    status.className = 'prov-status ok';
  });

  cancelBtn.addEventListener('click', () => { wrap.textContent = ''; });

  saveBtn.addEventListener('click', async () => {
    const name = nameIn.value.trim();
    const base = baseUrl();
    if (!name) { status.textContent = '请填写名称'; status.className = 'prov-status err'; return; }
    if (!base) { status.textContent = '请填写 Base URL'; status.className = 'prov-status err'; return; }
    try {
      const res = await App.apiJSON('/v1/providers/custom', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: p ? p.id : null,
          name,
          base_url: base,
          api_key: keyIn.value.trim(),
          model: modelIn.value.trim(),
        }),
      });
      if (!res.success) {
        status.textContent = res.status === 404
          ? '保存失败:' + provStaleHint()
          : '保存失败:' + (res.detail || res.status);
        status.className = 'prov-status err';
        return;
      }
    } catch (e) {
      status.textContent = '保存失败:' + e.message;
      status.className = 'prov-status err';
      return;
    }
    wrap.textContent = '';
    App.Settings.refreshProvidersUI();
  });

  ops.append(fetchBtn, testBtn, saveBtn, cancelBtn);
  form.append(
    mkField('名称', nameIn),
    mkField('Base URL(OpenAI 兼容)', urlIn),
    mkField('API Key', keyIn),
    mkField('默认模型', modelIn),
    dl,
    status,
    ops,
  );
  wrap.appendChild(form);
  nameIn.focus();
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
