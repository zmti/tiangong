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

App.Settings.applyTheme = function (theme) {
  document.body.dataset.theme = theme;
  try { localStorage.setItem('agent_theme', theme); } catch (_) { /* 忽略 */ }
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
    const res = await App.api('/v1/providers/custom');
    if (res.ok) App.Settings._provCustoms = (await res.json()).providers || [];
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
    const count = p.model_count || (Array.isArray(p.models) ? p.models.length : 0) || (p.model ? 1 : 0);
    meta.textContent = `${count} 个模型 · 默认: ${p.model || '无'}` + (p.api_key_masked ? ' · ' + p.api_key_masked : '');
    meta.title = p.base_url || '';
    info.append(name, meta);
    const ops = document.createElement('div');
    ops.className = 'prov-ops';
    const modelBtn = document.createElement('button');
    modelBtn.type = 'button';
    modelBtn.textContent = '管理模型';
    modelBtn.addEventListener('click', () => App.Settings._toggleModelManager(p.id, mgrWrap));
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
      try { await App.api('/v1/providers/custom/' + encodeURIComponent(p.id), { method: 'DELETE' }); } catch (_) { /* 忽略 */ }
      App.Settings.refreshProvidersUI();
    });
    ops.append(modelBtn, editBtn, delBtn);
    row.append(info, ops);
    const mgrWrap = document.createElement('div');
    mgrWrap.className = 'prov-mgr';
    mgrWrap.style.display = 'none';
    row.append(mgrWrap);
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

  // 模型列表(一个厂商可挂多个模型):[ {id, name, default} ]
  const defModel = (p && (p.default_model || p.model)) || '';
  const modelsState = (p && Array.isArray(p.models) ? p.models : [])
    .map((m) => (typeof m === 'string' ? { id: m, name: m, _default: false } : {
      id: m.id || '', name: m.name || m.id || '', _default: !!(m.default || m.id === defModel),
    }))
    .filter((m) => m.id);
  // 兜底:后端 models 数组为空但配置了默认 model 时,用默认 model 占位,
  // 避免编辑表单看不到已有模型、保存时误把模型清空
  if (!modelsState.length && defModel) {
    modelsState.push({ id: defModel, name: defModel, _default: true });
  }
  if (modelsState.length && !modelsState.some((m) => m._default)) modelsState[0]._default = true;

  const modelBox = document.createElement('div');
  modelBox.className = 'prov-models';
  const modelHead = document.createElement('div');
  modelHead.className = 'prov-models-head';
  modelHead.innerHTML = '<span>模型列表(可多选,圆点标记默认)</span>';
  const addRowBtn = document.createElement('button');
  addRowBtn.type = 'button';
  addRowBtn.className = 'lb-btn';
  addRowBtn.textContent = '＋ 添加模型';
  modelHead.appendChild(addRowBtn);
  const modelList = document.createElement('div');
  modelList.className = 'prov-models-list';

  const renderRows = () => {
    modelList.textContent = '';
    if (!modelsState.length) {
      const empty = document.createElement('div');
      empty.className = 'muted';
      empty.textContent = '暂无模型,点"获取模型列表"自动填充,或手动添加。';
      modelList.appendChild(empty);
      return;
    }
    modelsState.forEach((m, idx) => {
      const row = document.createElement('div');
      row.className = 'prov-model-row';
      const dot = document.createElement('input');
      dot.type = 'radio';
      dot.name = 'provDefaultModel';
      dot.checked = !!m._default;
      dot.title = '设为默认';
      dot.addEventListener('change', () => {
        modelsState.forEach((x) => (x._default = false));
        m._default = true;
      });
      const idIn = document.createElement('input');
      idIn.type = 'text';
      idIn.placeholder = '模型 ID(调用 API 用)';
      idIn.value = m.id;
      idIn.addEventListener('input', () => { m.id = idIn.value.trim(); });
      const nameIn2 = document.createElement('input');
      nameIn2.type = 'text';
      nameIn2.placeholder = '展示名(可选)';
      nameIn2.value = m.name;
      nameIn2.addEventListener('input', () => { m.name = nameIn2.value.trim(); });
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'danger';
      del.textContent = '✕';
      del.title = '移除该模型';
      del.addEventListener('click', () => { modelsState.splice(idx, 1); renderRows(); });
      row.append(dot, idIn, nameIn2, del);
      modelList.appendChild(row);
    });
  };
  renderRows();

  addRowBtn.addEventListener('click', () => {
    modelsState.push({ id: '', name: '', _default: modelsState.length === 0 });
    renderRows();
  });

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
    if (u && !/^https?:\/\//i.test(u)) u = 'https://' + u;
    return u.replace(/\/+$/, '');
  };

  const probe = async () => {
    const base = baseUrl();
    if (!base) { status.textContent = '请先填写 Base URL'; status.className = 'prov-status err'; return null; }
    status.textContent = '请求中…';
    status.className = 'prov-status muted';
    try {
      const res = await App.api('/v1/providers/custom/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ base_url: base, api_key: keyIn.value.trim(), id: p ? p.id : null }),
      });
      if (res.status === 404) {
        status.textContent = '失败:' + provStaleHint();
        status.className = 'prov-status err';
        return null;
      }
      const data = await res.json();
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
    const have = new Set(modelsState.map((m) => m.id));
    (models || []).forEach((m) => {
      const id = typeof m === 'string' ? m : (m.id || '');
      const name = typeof m === 'string' ? m : (m.name || m.id || '');
      if (id && !have.has(id)) {
        modelsState.push({ id, name, _default: modelsState.length === 0 });
        have.add(id);
      }
    });
    renderRows();
    status.textContent = `成功,共 ${modelsState.length} 个模型`;
    status.className = 'prov-status ok';
    const name = nameIn.value.trim();
    const base = baseUrl();
    if (name && base && modelsState.length > 0) {
      const clean = modelsState
        .map((m) => ({ id: (m.id || '').trim(), name: (m.name || '').trim() }))
        .filter((m) => m.id);
      const defaultItem = modelsState.find((m) => m._default) || modelsState[0];
      const defaultId = (defaultItem ? defaultItem.id : '') || clean[0].id;
      try {
        const res = await App.api('/v1/providers/custom', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            id: p ? p.id : null,
            name,
            base_url: base,
            api_key: keyIn.value.trim(),
            model: defaultId,
            models: clean,
          }),
        });
        // 新建厂商时后端会生成 id;把返回 id 写回 p,使后续"保存"变更新而非再建一个
        if (res.ok) {
          const data = await res.json().catch(() => ({}));
          if (data && data.id) p = Object.assign({}, p, { id: data.id });
        }
        // 注意:此处【不能】调用 refreshProvidersUI(),它会重建厂商列表容器
        // 而编辑表单挂在容器内,一旦重建正在填写的表单(含刚拉取的模型)会被销毁,
        // 表现为"获取模型列表后模型消失"。数据已落库,表单保持打开即可,
        // 待用户点"保存/取消"关闭表单时列表自然重渲染、计数随之正确。
      } catch (_) { /* 忽略 */ }
    }
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
    const clean = modelsState
      .map((m) => ({ id: (m.id || '').trim(), name: (m.name || '').trim() }))
      .filter((m) => m.id);
    if (!clean.length) {
      status.textContent = '请至少添加一个模型';
      status.className = 'prov-status err';
      return;
    }
    const defaultItem = modelsState.find((m) => m._default) || modelsState[0];
    const defaultId = (defaultItem ? defaultItem.id : '') || clean[0].id;
    try {
      const res = await App.api('/v1/providers/custom', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: p ? p.id : null,
          name,
          base_url: base,
          api_key: keyIn.value.trim(),
          model: defaultId,
          models: clean,
        }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        status.textContent = res.status === 404
          ? '保存失败:' + provStaleHint()
          : '保存失败:' + (data.detail || res.status);
        status.className = 'prov-status err';
        return;
      }
      // 新建厂商时回写后端生成的 id,避免后续保存/获取模型列表再次新建导致重复
      const data = await res.json().catch(() => ({}));
      if (data && data.id) p = Object.assign({}, p, { id: data.id });
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
    modelHead,
    modelBox.appendChild(modelList),
    status,
    ops,
  );
  wrap.appendChild(form);
  nameIn.focus();
};

// 展开/收起某个厂商的"管理模型"面板(无需重建整个厂商即可增删/设默认)
App.Settings._toggleModelManager = function (pid, wrap) {
  if (wrap.style.display === 'none') {
    wrap.style.display = '';
    App.Settings._renderModelManager(pid, wrap);
  } else {
    wrap.style.display = 'none';
    wrap.textContent = '';
  }
};

// 渲染某个厂商下的模型清单,支持:设为默认 / 移除 / 新增
App.Settings._renderModelManager = async function (pid, wrap) {
  wrap.textContent = '';
  const status = document.createElement('div');
  status.className = 'prov-status muted';
  status.textContent = '加载中…';
  wrap.appendChild(status);

  const draw = (models) => {
    wrap.textContent = '';
    if (!models.length) {
      const empty = document.createElement('div');
      empty.className = 'muted';
      empty.textContent = '该厂商下暂无模型,可手动添加。';
      wrap.appendChild(empty);
    }
    models.forEach((m) => {
      const row = document.createElement('div');
      row.className = 'prov-model-row';
      const dot = document.createElement('input');
      dot.type = 'radio';
      dot.name = 'mgrDefault_' + pid;
      dot.checked = !!m.default;
      dot.title = '设为默认';
      dot.addEventListener('change', async () => {
        await App.api('/v1/providers/custom/' + encodeURIComponent(pid) + '/models/' + encodeURIComponent(m.id) + '/default', { method: 'POST' });
        App.Settings.loadProviders();
        App.Settings._renderModelManager(pid, wrap);
      });
      const idEl = document.createElement('span');
      idEl.className = 'prov-mgr-id';
      idEl.textContent = m.id;
      const nameEl = document.createElement('span');
      nameEl.className = 'muted';
      nameEl.textContent = (m.name && m.name !== m.id) ? ('· ' + m.name) : '';
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'danger';
      del.textContent = '✕';
      del.title = '移除该模型';
      del.addEventListener('click', async () => {
        if (!window.confirm(`移除模型「${m.id}」?`)) return;
        await App.api('/v1/providers/custom/' + encodeURIComponent(pid) + '/models/' + encodeURIComponent(m.id), { method: 'DELETE' });
        App.Settings.loadProviders();
        App.Settings._renderModelManager(pid, wrap);
      });
      row.append(dot, idEl, nameEl, del);
      wrap.appendChild(row);
    });

    const addRow = document.createElement('div');
    addRow.className = 'prov-model-row prov-mgr-add';
    const idIn = document.createElement('input');
    idIn.type = 'text';
    idIn.placeholder = '模型 ID';
    const nameIn = document.createElement('input');
    nameIn.type = 'text';
    nameIn.placeholder = '展示名(可选)';
    const addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.className = 'lb-btn';
    addBtn.textContent = '添加';
    addBtn.addEventListener('click', async () => {
      const id = idIn.value.trim();
      if (!id) { status.textContent = '请填写模型 ID'; status.className = 'prov-status err'; return; }
      const res = await App.api('/v1/providers/custom/' + encodeURIComponent(pid) + '/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, name: nameIn.value.trim() }),
      });
      if (!res.ok) { status.textContent = '添加失败'; status.className = 'prov-status err'; return; }
      App.Settings.loadProviders();
      App.Settings._renderModelManager(pid, wrap);
    });
    addRow.append(idIn, nameIn, addBtn);
    wrap.appendChild(addRow);
  };

  const res = await App.api('/v1/providers/custom/' + encodeURIComponent(pid) + '/models');
  if (!res.ok) {
    wrap.textContent = '';
    status.textContent = '加载失败';
    status.className = 'prov-status err';
    wrap.appendChild(status);
    return;
  }
  const data = await res.json();
  draw(data.models || []);
};

