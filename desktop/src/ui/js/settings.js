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
  // 无独立的 themeBtn，主题切换通过设置面板完成
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

/* ---------- 主模型选择器辅助(loadModels / provider 缓存) ---------- */

// 缓存已加载的模型数据 { providerId -> [{id, name, speed, quality}] }
App._modelsCache = {};

// 加载所有可用 provider 列表并缓存(协作模式 popover 也从缓存读取)
App.Settings.loadProviders = async function () {
  try {
    const res = await App.apiJSON('/v1/providers');
    console.log('[Settings] loadProviders res:', res);
    if (!res.success) return;
    App.Settings._providers = res.data?.providers || [];
    // 存储后端当前激活的 provider 和其默认模型
    App.Settings._activeProvider = res.data?.active || '';
    const activeProv = App.Settings._providers.find(p => p.id === App.Settings._activeProvider);
    App.Settings._activeModel = activeProv?.default_model || '';
    console.log('[Settings] providers loaded:', App.Settings._providers.length, 'active:', App.Settings._activeProvider, 'model:', App.Settings._activeModel);
    // 通知协作模式组件:provider 数据已就绪,可刷新
    if (App.Mode && App.Mode.reinitAfterProviders) App.Mode.reinitAfterProviders();
    // 刷新模型选择器徽标(使用后端激活模型信息)
    if (App.ModelPicker) App.ModelPicker.refreshBadge && App.ModelPicker.refreshBadge();
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

  box.textContent = '';

  // 合并所有提供商
  const builtIn = (App.Settings._providers || []).filter((p) => p.configured && p.group !== 'custom');
  const customOnly = (App.Settings._providers || []).filter((p) => p.group === 'custom');
  const allProviders = [
    ...builtIn.map((p) => ({ ...p, _type: 'builtin' })),
    ...customOnly.map((p) => ({ ...p, _type: 'custom' })),
  ];

  allProviders.forEach((p) => {
    const models = p.models || [];
    const defaultModel = p.default_model || models[0]?.id || '';
    const isBuiltin = p._type === 'builtin';

    const card = document.createElement('div');
    card.className = 'prov-card';

    // 顶部：提供商名称 + 标签 + 操作按钮
    const header = document.createElement('div');
    header.className = 'prov-card-header';
    const left = document.createElement('div');
    left.className = 'prov-card-left';
    const nameEl = document.createElement('span');
    nameEl.className = 'prov-card-name';
    nameEl.textContent = p.name;
    const typeTag = document.createElement('span');
    typeTag.className = 'prov-card-tag' + (isBuiltin ? ' builtin' : ' custom');
    typeTag.textContent = isBuiltin ? '内置' : '自定义';
    left.append(nameEl, typeTag);

    const actions = document.createElement('div');
    actions.className = 'prov-card-actions';
    if (isBuiltin) {
      const fetchBtn = document.createElement('button');
      fetchBtn.className = 'prov-action';
      fetchBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg> 获取';
      fetchBtn.title = '从API获取模型列表';
      fetchBtn.addEventListener('click', async () => {
        fetchBtn.disabled = true;
        fetchBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/></svg> 获取中';
        try {
          const res = await App.apiJSON('/v1/providers/fetch_models', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pid: p.id, base_url: p.base_url || '', api_key: '' }),
          });
          if (res.success && res.data?.ok) {
            fetchBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg> 已获取';
            setTimeout(() => { fetchBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg> 获取'; fetchBtn.disabled = false; }, 1500);
            App.Settings.refreshProvidersUI();
            if (App.ModelPicker?.refreshBadge) App.ModelPicker.refreshBadge();
          } else {
            fetchBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg> 失败';
            setTimeout(() => { fetchBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg> 获取'; fetchBtn.disabled = false; }, 1500);
          }
        } catch (_) {
          fetchBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg> 失败';
          setTimeout(() => { fetchBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg> 获取'; fetchBtn.disabled = false; }, 1500);
        }
      });
      const addBtn = document.createElement('button');
      addBtn.className = 'prov-action';
      addBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg> 添加';
      addBtn.title = '手动添加模型';
      addBtn.addEventListener('click', () => App.Settings._openBuiltinModelForm(p));
      actions.append(fetchBtn, addBtn);
    } else {
      const editBtn = document.createElement('button');
      editBtn.className = 'prov-action';
      editBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg> 编辑';
      editBtn.addEventListener('click', () => App.Settings._openProvForm(p));
      const delBtn = document.createElement('button');
      delBtn.className = 'prov-action danger';
      delBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';
      delBtn.title = '删除提供商';
      delBtn.addEventListener('click', async () => {
        if (!window.confirm(`删除「${p.name}」？`)) return;
        try { await App.apiJSON('/v1/providers/custom/' + encodeURIComponent(p.id), { method: 'DELETE' }); } catch (_) {}
        App.Settings.refreshProvidersUI();
      });
      actions.append(editBtn, delBtn);
    }
    header.append(left, actions);
    card.appendChild(header);

    // 模型列表
    if (models.length) {
      const modelList = document.createElement('div');
      modelList.className = 'prov-card-models';
      models.forEach((m) => {
        const item = document.createElement('div');
        item.className = 'prov-model-item' + (m.default ? ' active' : '');
        const modelName = document.createElement('span');
        modelName.className = 'prov-model-name';
        modelName.textContent = m.name || m.id;
        modelName.title = m.id;
        if (m.default) {
          const badge = document.createElement('span');
          badge.className = 'prov-model-badge';
          badge.textContent = '默认';
          modelName.appendChild(badge);
        }
        const delBtn = document.createElement('button');
        delBtn.className = 'prov-model-del';
        delBtn.innerHTML = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
        delBtn.title = '删除模型';
        delBtn.addEventListener('click', async (e) => {
          e.stopPropagation();
          if (!confirm('删除模型 ' + (m.name || m.id) + '？')) return;
          const endpoint = isBuiltin
            ? `/v1/providers/builtin/${p.id}/models/${m.id}`
            : `/v1/providers/custom/${p.id}/models/${m.id}`;
          try { await App.apiJSON(endpoint, { method: 'DELETE' }); } catch (_) {}
          App.Settings.refreshProvidersUI();
        });
        item.append(modelName, delBtn);
        modelList.appendChild(item);
      });
      card.appendChild(modelList);
    } else {
      const empty = document.createElement('div');
      empty.className = 'prov-card-empty';
      empty.textContent = '暂无模型，点击上方按钮添加';
      card.appendChild(empty);
    }

    box.appendChild(card);
  });

  // 添加提供商按钮
  const addBtn = document.createElement('button');
  addBtn.className = 'prov-add-btn';
  addBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg> 添加 OpenAI 兼容提供商';
  addBtn.addEventListener('click', () => App.Settings._openProvForm(null));
  box.appendChild(addBtn);

  const formWrap = document.createElement('div');
  formWrap.id = 'provFormWrap';
  box.appendChild(formWrap);
};

App.Settings.refreshProvidersUI = async function () {
  await App.Settings.loadProviders();
  await App.Settings.renderProviderAdmin();
  App._modelsCache = {};
};

// 为内置提供商新增自定义模型
App.Settings._openBuiltinModelForm = function (p) {
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

  const modelIn = document.createElement('input');
  modelIn.type = 'text';
  modelIn.placeholder = '输入模型 ID(如 gpt-4o, deepseek-chat)';
  modelIn.value = '';

  const nameIn = document.createElement('input');
  nameIn.type = 'text';
  nameIn.placeholder = '显示名称(可选，留空与ID相同)';
  nameIn.value = '';

  const status = document.createElement('div');
  status.className = 'prov-status muted';

  const ops = document.createElement('div');
  ops.className = 'prov-form-ops';

  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.className = 'primary';
  saveBtn.textContent = '保存';
  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.textContent = '取消';

  cancelBtn.addEventListener('click', () => { wrap.textContent = ''; });

  saveBtn.addEventListener('click', async () => {
    const modelId = modelIn.value.trim();
    if (!modelId) { status.textContent = '请填写模型 ID'; status.className = 'prov-status err'; return; }
    const displayName = nameIn.value.trim() || modelId;
    try {
      const res = await App.apiJSON(`/v1/providers/builtin/${p.id}/add-model`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: modelId, name: displayName }),
      });
      if (res.success && res.data?.ok) {
        wrap.textContent = '';
        App.Settings.refreshProvidersUI();
      } else {
        status.textContent = '保存失败:' + (res.data?.error || '未知错误');
        status.className = 'prov-status err';
      }
    } catch (e) {
      status.textContent = '保存失败:' + e.message;
      status.className = 'prov-status err';
    }
  });

  ops.append(saveBtn, cancelBtn);
  form.append(mkField('模型 ID', modelIn), mkField('显示名称', nameIn), status, ops);
  wrap.appendChild(form);
  modelIn.focus();
};

// 为自定义提供商新增模型
App.Settings._openCustomModelForm = function (p) {
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

  const modelIn = document.createElement('input');
  modelIn.type = 'text';
  modelIn.placeholder = '输入模型 ID(如 gpt-4o, deepseek-chat)';
  modelIn.value = '';

  const nameIn = document.createElement('input');
  nameIn.type = 'text';
  nameIn.placeholder = '显示名称(可选，留空与ID相同)';
  nameIn.value = '';

  const status = document.createElement('div');
  status.className = 'prov-status muted';

  const ops = document.createElement('div');
  ops.className = 'prov-form-ops';

  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.className = 'primary';
  saveBtn.textContent = '保存';
  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.textContent = '取消';

  cancelBtn.addEventListener('click', () => { wrap.textContent = ''; });

  saveBtn.addEventListener('click', async () => {
    const modelId = modelIn.value.trim();
    if (!modelId) { status.textContent = '请填写模型 ID'; status.className = 'prov-status err'; return; }
    const displayName = nameIn.value.trim() || modelId;
    try {
      const res = await App.apiJSON(`/v1/providers/custom/${p.id}/models`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: modelId, name: displayName }),
      });
      if (res.success && res.data?.ok) {
        wrap.textContent = '';
        App.Settings.refreshProvidersUI();
        if (App.ModelPicker && App.ModelPicker.refreshBadge) App.ModelPicker.refreshBadge();
      } else {
        status.textContent = '保存失败:' + (res.data?.error || '未知错误');
        status.className = 'prov-status err';
      }
    } catch (e) {
      status.textContent = '保存失败:' + e.message;
      status.className = 'prov-status err';
    }
  });

  ops.append(saveBtn, cancelBtn);
  form.append(mkField('模型 ID', modelIn), mkField('显示名称', nameIn), status, ops);
  wrap.appendChild(form);
  modelIn.focus();
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
    if (u && !/^https?:\/\//i.test(u)) u = 'https://' + u;
    return u.replace(/\/+$/, '');
  };
  const apiKey = () => keyIn.value.trim() || (p ? '' : '');

  let fetchedModels = [];

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
    fetchedModels = models;
    dl.textContent = '';
    models.forEach((m) => {
      const opt = document.createElement('option');
      opt.value = (m.id || m);
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
          models: fetchedModels.length > 0 ? fetchedModels : undefined,
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

/* 初始化 — 等待 App.els 就绪后再执行 */
(function initSettings() {
  function doInit() {
    // 应用初始主题
    const initialTheme = App.Storage ? App.Storage.get('theme') : 'light';
    if (initialTheme) {
      document.body.dataset.theme = initialTheme;
    }
    // 加载 providers（不依赖 App.els）
    App.Settings.loadProviders();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', doInit);
  } else {
    doInit();
  }
})();
