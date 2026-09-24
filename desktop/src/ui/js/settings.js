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
    if (App.DEBUG) console.log('[Settings] loadProviders res:', res);
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

      // 一键清空:只清"获取/添加"进来的扩展模型,代码内置的模型保留
      const clearBtn = document.createElement('button');
      clearBtn.className = 'prov-action danger';
      const CLEAR_ICON = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';
      // 成功刷新后本元素会被重建,所以只有"未成功"分支才需要还原按钮文本
      const flashThenRestore = (text) => {
        clearBtn.textContent = text;
        setTimeout(() => { clearBtn.innerHTML = CLEAR_ICON + ' 清空'; clearBtn.disabled = false; }, 1500);
      };
      clearBtn.innerHTML = CLEAR_ICON + ' 清空';
      clearBtn.title = '清空通过「获取/添加」加入的模型（内置模型保留）';
      clearBtn.addEventListener('click', async () => {
        if (!window.confirm(
          `清空「${p.name}」中通过「获取/添加」加入的模型？\n内置模型会保留。`
        )) return;
        clearBtn.disabled = true;
        clearBtn.textContent = '清空中';
        try {
          const res = await App.apiJSON(
            `/v1/providers/builtin/${encodeURIComponent(p.id)}/models`,
            { method: 'DELETE' }
          );
          if (!res || !res.success) { flashThenRestore('清空失败'); return; }
          if (!res.data?.removed) {
            // 本来就没有扩展模型,给个反馈免得用户以为按钮没生效
            flashThenRestore('无模型可清');
            return;
          }
          await App.Settings.refreshProvidersUI();
          if (App.ModelPicker?.refreshBadge) App.ModelPicker.refreshBadge();
        } catch (_) {
          flashThenRestore('清空失败');
        }
      });

      actions.append(fetchBtn, addBtn, clearBtn);
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
      // 获取回来的模型动辄几十个,默认只展示前几个,其余折叠,避免把设置页撑爆
      const DEFAULT_VISIBLE_MODELS = 6;
      const modelList = document.createElement('div');
      modelList.className = 'prov-card-models';
      const collapsedItems = [];
      models.forEach((m, idx) => {
        const item = document.createElement('div');
        item.className = 'prov-model-item' + (m.default ? ' active' : '');
        // 默认模型始终可见,折叠时也不该把"默认"这个关键信息藏起来
        if (idx >= DEFAULT_VISIBLE_MODELS && !m.default) {
          item.hidden = true;
          collapsedItems.push(item);
        }
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

      // 折叠开关:只在确实有隐藏项时出现
      if (collapsedItems.length) {
        const toggle = document.createElement('button');
        toggle.className = 'prov-models-toggle';
        let expanded = false;
        const setLabel = () => {
          toggle.textContent = expanded
            ? '收起'
            : `展开全部 ${models.length} 个模型`;
        };
        setLabel();
        toggle.addEventListener('click', () => {
          expanded = !expanded;
          collapsedItems.forEach((el) => { el.hidden = !expanded; });
          toggle.classList.toggle('expanded', expanded);
          setLabel();
        });
        card.appendChild(toggle);
      }
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

/* ---------- 智能体规则管理(/v1/rules/*) ---------- */

App.Settings._ruleSets = [];

const RULE_TYPE_LABELS = {
  instruction: '软规则·指令',
  tool_block: '硬规则·禁工具',
  term_block: '硬规则·禁词',
  path_block: '硬规则·禁路径',
};
const RULE_TYPES = ['instruction', 'tool_block', 'term_block', 'path_block'];

/* ---------- 生成模型配置(/v1/config/models) ----------
 * 图片/视频生成模型:桌面端覆盖 > .env/环境变量 > 代码默认值。
 * 覆盖值保存在后端 data 目录,保存后立即生效,无需重启。
 * 输入框留空并保存 = 清除覆盖,回退到 .env/默认值。
 */
App.Settings._genModels = null;

const _GEN_SRC_TEXT = { override: '桌面端覆盖', env: '环境变量', default: '代码默认' };

/* ---------- 生成模型「可搜索组合框」 ----------
 * 原生 <select> 在候选有几十个模型时:样式不可控(Windows 原生下拉很"老气"),
 * 且完全不能搜索。这里自绘一个 combobox:
 *   - 触发区(trigger)显示当前模型,点击展开;
 *   - 弹层挂到 document.body(传送门)+ position:fixed,避开 .settings-content
 *     的 overflow 裁切,也避开弹窗内部 stacking context;
 *   - 弹层内带搜索框(按 名称/模型id/提供商 过滤)+ 按提供商分组;
 *   - 键盘:↑↓ 移动、Enter 选择、Esc 关闭。
 * 每个 key('genImageModel' / 'genVideoModel')对应一组 DOM 与状态,缓存在 _genCombos。
 */
App.Settings._genCombos = Object.create(null);

const _GEN_CHECK_SVG =
  '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
  'stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>';
const _GEN_SEARCH_SVG =
  '<svg class="gen-combo-search-icon" width="13" height="13" viewBox="0 0 16 16" fill="currentColor">' +
  '<path d="M11.742 10.344a6.5 6.5 0 1 0-1.397 1.398l3.85 3.85a1 1 0 0 0 1.415-1.414l-3.85-3.85z' +
  'm-5.242.656a5 5 0 1 1 0-10 5 5 0 0 1 0 10z"/></svg>';

function _genEsc(s) {
  const t = String(s == null ? '' : s);
  return App.escapeHtml ? App.escapeHtml(t)
    : t.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// provider id -> 界面友好名:优先后端 provider_name;后端较旧(无该字段)时
// 退回「模型设置」已缓存的 provider 名称,保证分组标题仍是人话。
function _genProvLabel(c) {
  if (c.provider_name) return c.provider_name;
  const p = (App.Settings._providers || []).find((x) => x && x.id === c.provider);
  if (p && p.name) return p.name;
  return c.provider || '其他';
}

function _genComboEnsure(key) {
  let c = App.Settings._genCombos[key];
  if (c) return c;
  const combo = document.getElementById(key + 'Combo');
  const trigger = document.getElementById(key + 'Trigger');
  const textEl = document.getElementById(key + 'TriggerText');
  const hidden = document.getElementById(key + 'Select');
  if (!combo || !trigger || !hidden) return null;

  const pop = document.createElement('div');
  pop.className = 'gen-combo-pop';
  pop.hidden = true;
  pop.innerHTML =
    '<div class="gen-combo-search-wrap">' + _GEN_SEARCH_SVG +
    '<input type="text" class="gen-combo-search" placeholder="搜索模型名 / 提供商…" autocomplete="off" spellcheck="false"></div>' +
    '<div class="gen-combo-list" role="listbox"></div>' +
    '<div class="gen-combo-empty" hidden>没有匹配的模型</div>';
  document.body.appendChild(pop);

  c = {
    key, combo, trigger, textEl, hidden, pop,
    search: pop.querySelector('.gen-combo-search'),
    list: pop.querySelector('.gen-combo-list'),
    empty: pop.querySelector('.gen-combo-empty'),
    info: null, hi: -1, _visible: [],
  };
  App.Settings._genCombos[key] = c;

  trigger.addEventListener('click', (e) => { e.stopPropagation(); _genComboToggle(key); });
  // 弹层内点击不冒泡到 document,否则会被"点外关闭"立刻关掉
  pop.addEventListener('click', (e) => e.stopPropagation());
  c.search.addEventListener('input', () => _genComboRenderList(key, c.search.value));
  c.search.addEventListener('keydown', (e) => _genComboKeydown(key, e));
  c.list.addEventListener('click', (e) => {
    const btn = e.target.closest && e.target.closest('.gen-opt');
    if (btn) _genComboSelect(key, btn.dataset.value);
  });
  c.list.addEventListener('mousemove', (e) => {
    const btn = e.target.closest && e.target.closest('.gen-opt');
    if (btn) {
      const i = Number(btn.dataset.idx);
      if (i !== c.hi) { c.hi = i; _genComboHighlight(key); }
    }
  });

  if (!App.Settings._genComboDocBound) {
    App.Settings._genComboDocBound = true;
    document.addEventListener('click', () => _genComboCloseAll());
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') _genComboCloseAll(); });
    window.addEventListener('resize', () => _genComboCloseAll());
    // 设置内容区可滚动:滚动时弹层会错位,直接收起
    const scroller = document.querySelector('.settings-content');
    if (scroller) scroller.addEventListener('scroll', () => _genComboCloseAll(), { passive: true });
  }
  return c;
}

// 把候选拍成「行」描述:一行是分组标题,一行是选项
function _genComboRows(c) {
  const info = c.info || {};
  const cands = info.candidates || [];
  const rows = [{
    kind: 'opt', value: '', name: '跟随默认',
    sub: info.code_default || '', inherit: true, usable: true,
  }];
  const order = [];
  const buckets = Object.create(null);
  cands.forEach((x) => {
    const g = _genProvLabel(x);
    if (!buckets[g]) { buckets[g] = []; order.push(g); }
    buckets[g].push(x);
  });
  order.forEach((g) => {
    const items = buckets[g];
    // 整组都发不到端点时,在组标题上直接标出来,免得选了注定失败
    rows.push({ kind: 'group', label: g, bad: items.every((x) => x.usable === false) });
    items.forEach((x) => {
      rows.push({
        kind: 'opt', value: x.id, name: x.name || x.id, sub: x.id,
        usable: x.usable !== false, provider: g,
      });
    });
  });
  return rows;
}

function _genOptEl(r, idx, selected) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'gen-opt' + (r.inherit ? ' inherit' : '') + (selected ? ' selected' : '');
  btn.dataset.value = r.value == null ? '' : r.value;
  btn.dataset.idx = String(idx);
  btn.setAttribute('role', 'option');
  btn.setAttribute('aria-selected', selected ? 'true' : 'false');

  const main = document.createElement('span');
  main.className = 'gen-opt-main';
  const nm = document.createElement('span');
  nm.className = 'gen-opt-name';
  nm.textContent = r.inherit ? (r.name + (r.sub ? '（' + r.sub + '）' : '')) : r.name;
  main.appendChild(nm);
  if (!r.inherit && r.sub) {
    const idEl = document.createElement('span');
    idEl.className = 'gen-opt-id';
    idEl.textContent = r.sub;
    main.appendChild(idEl);
  }
  btn.appendChild(main);

  if (r.usable === false) {
    const bad = document.createElement('span');
    bad.className = 'gen-opt-bad';
    bad.textContent = '⚠ 不可用';
    bad.title = '该模型所属提供商无法承接此生成能力,选中后调用会失败';
    btn.appendChild(bad);
  }
  const ck = document.createElement('span');
  ck.className = 'gen-opt-check';
  ck.innerHTML = _GEN_CHECK_SVG;
  btn.appendChild(ck);
  return btn;
}

function _genComboRenderList(key, filter) {
  const c = App.Settings._genCombos[key];
  if (!c) return;
  const f = (filter || '').trim().toLowerCase();
  const cur = c.hidden.value || '';
  const match = (r) => !f ||
    (r.name + ' ' + (r.sub || '') + ' ' + (r.provider || '')).toLowerCase().includes(f);

  c.list.innerHTML = '';
  const visible = [];
  let pending = null;   // 待渲染的分组标题
  let shown = 0;

  _genComboRows(c).forEach((r) => {
    if (r.kind === 'group') { pending = r; return; }
    if (!match(r)) return;
    if (pending) {
      const gh = document.createElement('div');
      gh.className = 'gen-combo-group';
      gh.innerHTML = _genEsc(pending.label) +
        (pending.bad ? ' <span class="gen-combo-group-bad">⚠ 端点不匹配</span>' : '');
      c.list.appendChild(gh);
      pending = null;
    }
    c.list.appendChild(_genOptEl(r, visible.length, cur === r.value));
    visible.push(r);
    shown++;
  });

  c._visible = visible;
  c.empty.hidden = shown > 0;
  // 搜索时默认高亮第一项,方便直接回车
  c.hi = (f && visible.length) ? 0 : -1;
  _genComboHighlight(key);
}

function _genComboUpdateTrigger(key) {
  const c = App.Settings._genCombos[key];
  if (!c || !c.textEl) return;
  const info = c.info || {};
  const val = c.hidden.value || '';
  c.textEl.classList.remove('is-inherit');
  if (!val) {
    const def = info.code_default || '';
    c.textEl.textContent = '跟随默认' + (def ? '（' + def + '）' : '');
    c.textEl.classList.add('is-inherit');
    return;
  }
  const hit = (info.candidates || []).find((x) => x.id === val);
  c.textEl.textContent = hit ? (hit.name || hit.id) : val;
}

function _genComboHighlight(key) {
  const c = App.Settings._genCombos[key];
  if (!c) return;
  const els = c.list.querySelectorAll('.gen-opt');
  Array.prototype.forEach.call(els, (el) => {
    el.classList.toggle('hi', Number(el.dataset.idx) === c.hi);
  });
  const cur = els[c.hi];
  if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: 'nearest' });
}

function _genComboPosition(key) {
  const c = App.Settings._genCombos[key];
  if (!c) return;
  const r = c.trigger.getBoundingClientRect();
  const pop = c.pop;
  pop.style.width = Math.max(200, Math.round(r.width)) + 'px';
  pop.style.left = Math.round(r.left) + 'px';
  const h = pop.offsetHeight || 320;
  let top = r.bottom + 6;
  // 下方放不下就翻到上方
  if (top + h > window.innerHeight - 8) top = Math.max(8, Math.round(r.top - h - 6));
  pop.style.top = Math.round(top) + 'px';
}

function _genComboClose(key) {
  const c = App.Settings._genCombos[key];
  if (!c) return;
  c.pop.hidden = true;
  c.combo.classList.remove('open');
  c.trigger.setAttribute('aria-expanded', 'false');
}

function _genComboCloseAll(exceptKey) {
  Object.keys(App.Settings._genCombos).forEach((k) => {
    if (k !== exceptKey) _genComboClose(k);
  });
}

function _genComboOpen(key) {
  _genComboCloseAll(key);
  const c = App.Settings._genCombos[key];
  if (!c) return;
  c.pop.hidden = false;
  c.combo.classList.add('open');
  c.trigger.setAttribute('aria-expanded', 'true');
  c.search.value = '';
  _genComboRenderList(key, '');
  _genComboPosition(key);
  const sel = c.list.querySelector('.gen-opt.selected');
  if (sel && sel.scrollIntoView) sel.scrollIntoView({ block: 'nearest' });
  c.search.focus();
}

function _genComboToggle(key) {
  const c = App.Settings._genCombos[key];
  if (!c) return;
  if (c.pop.hidden) _genComboOpen(key);
  else _genComboClose(key);
}

function _genComboSelect(key, value) {
  const c = App.Settings._genCombos[key];
  if (!c) return;
  c.hidden.value = value == null ? '' : value;
  _genComboUpdateTrigger(key);
  Array.prototype.forEach.call(c.list.querySelectorAll('.gen-opt'), (el) => {
    const sel = (el.dataset.value || '') === c.hidden.value;
    el.classList.toggle('selected', sel);
    el.setAttribute('aria-selected', sel ? 'true' : 'false');
  });
  _genComboClose(key);
  if (c.trigger) c.trigger.focus();
}

function _genComboKeydown(key, e) {
  const c = App.Settings._genCombos[key];
  if (!c) return;
  const vis = c._visible || [];
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    if (!vis.length) return;
    if (c.hi < 0) c.hi = e.key === 'ArrowDown' ? 0 : vis.length - 1;
    else c.hi = e.key === 'ArrowDown'
      ? Math.min(vis.length - 1, c.hi + 1)
      : Math.max(0, c.hi - 1);
    _genComboHighlight(key);
    return;
  }
  if (e.key === 'Enter') {
    e.preventDefault();
    if (c.hi >= 0 && vis[c.hi]) _genComboSelect(key, vis[c.hi].value);
    return;
  }
  if (e.key === 'Escape') {
    e.preventDefault();
    _genComboClose(key);
    if (c.trigger) c.trigger.focus();
  }
}

App.Settings.loadGenModels = async function () {
  const imgSel = document.getElementById('genImageModelSelect');
  const vidSel = document.getElementById('genVideoModelSelect');
  const status = document.getElementById('genModelsStatus');
  const note = document.getElementById('genEndpointNote');
  if (!imgSel || !vidSel) return;

  // 一条模型配置 → 来源徽章 + 可搜索组合框 + 「当前生效」值
  const fill = (key, info, ui) => {
    const src = (info && info.source) || 'default';
    if (ui.badge) {
      ui.badge.dataset.source = src;
      ui.badge.textContent = _GEN_SRC_TEXT[src] || src;
      ui.badge.title = '代码默认: ' + ((info && info.code_default) || '-');
    }
    const c = _genComboEnsure(key);
    if (!c || !info) return;

    c.info = info;
    // 有覆盖则选中覆盖值,否则停在「跟随默认」
    c.hidden.value = info.overridden ? (info.value || '') : '';
    _genComboUpdateTrigger(key);
    if (!c.pop.hidden) _genComboRenderList(key, c.search.value);

    if (ui.value) ui.value.textContent = info.value || '-';

    // 覆盖值不在候选里(用户手填/旧配置):组合框仍能显示该值,但要留个说明
    const known = (info.candidates || []).some((x) => x.id === info.value);
    if (ui.warn) {
      const cur = (info.candidates || []).find((x) => x.id === info.value);
      const bad = !!(cur && cur.usable === false);
      const orphan = !!info.value && !known;
      ui.warn.hidden = !(bad || orphan);
      if (bad) {
        ui.warn.title =
          '「' + (cur.name || cur.id) + '」所属「' + _genProvLabel(cur) + '」' +
          (cur.provider_base_url ? '（' + cur.provider_base_url + '）' : '') +
          ' 无法承接该生成能力(端点/契约不匹配),选中后调用会失败。请改选没有 ⚠ 标记的模型。';
      } else if (orphan) {
        ui.warn.title =
          '「' + info.value + '」不在当前候选列表中(可能是手填或已下线的模型),' +
          '将按提供商解析端点;若调用失败请改选列表内的模型。';
      }
    }
  };

  try {
    const res = await App.apiJSON('/v1/config/models');
    if (!res.success) throw new Error(res.detail || ('HTTP ' + res.status));
    const data = res.data || {};
    const m = data.models || {};
    App.Settings._genModels = m;
    const ep = data.gen_endpoint || {};
    App.Settings._genEndpoint = (ep.base_url || '-') + (ep.provider_name ? '（' + ep.provider_name + '）' : '');

    fill('genImageModel', m.image_model, {
      badge: document.getElementById('genImageModelBadge'),
      value: document.getElementById('genImageModelValue'),
      warn: document.getElementById('genImageModelWarn'),
    });
    fill('genVideoModel', m.video_model, {
      badge: document.getElementById('genVideoModelBadge'),
      value: document.getElementById('genVideoModelValue'),
      warn: document.getElementById('genVideoModelWarn'),
    });
    if (note) {
      // 按 provider 路由后端点随模型所属提供商而变;这里只说明"回退端点"与优先级
      const epTxt = ep.base_url
        ? '未匹配到提供商时回退到 <code>' + _genEsc(ep.base_url) + '</code>' +
          (ep.provider_name ? '（' + _genEsc(ep.provider_name) + '）' : '') + ';'
        : '';
      note.innerHTML = '请求按所选模型所属提供商自动路由到对应端点;' +
        epTxt + '优先级:桌面端覆盖 &gt; .env/环境变量 &gt; 代码默认值。';
    }
    if (status) status.textContent = '';
  } catch (e) {
    ['genImageModelBadge', 'genVideoModelBadge'].forEach((id) => {
      const b = document.getElementById(id);
      if (b) { b.dataset.source = 'default'; b.textContent = '加载失败'; b.title = String(e.message || ''); }
    });
    ['genImageModelValue', 'genVideoModelValue'].forEach((id) => {
      const v = document.getElementById(id);
      if (v) v.textContent = '-';
    });
    ['genImageModelWarn', 'genVideoModelWarn'].forEach((id) => {
      const w = document.getElementById(id);
      if (w) w.hidden = true;
    });
    ['genImageModelTriggerText', 'genVideoModelTriggerText'].forEach((id) => {
      const t = document.getElementById(id);
      if (t) { t.textContent = '加载失败'; t.classList.add('is-inherit'); }
    });
    if (status) status.textContent = '加载失败: ' + e.message;
  }
};

App.Settings.saveGenModels = async function () {
  const imgSel = document.getElementById('genImageModelSelect');
  const vidSel = document.getElementById('genVideoModelSelect');
  const status = document.getElementById('genModelsStatus');
  if (!imgSel || !vidSel) return;
  try {
    if (status) status.textContent = '保存中...';
    const res = await App.apiJSON('/v1/config/models', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        image_model: (imgSel.value || '').trim(),
        video_model: (vidSel.value || '').trim(),
      }),
    });
    if (!res.success) throw new Error(res.detail || ('HTTP ' + res.status));
    // 顺序要紧:loadGenModels 结尾会清空状态栏,所以"已保存"必须写在它之后,
    // 否则保存成功的提示会被立刻抹掉(看起来像没保存成功)。
    await App.Settings.loadGenModels();
    if (status) status.textContent = '已保存,立即生效';
    setTimeout(() => { if (status && status.textContent === '已保存,立即生效') status.textContent = ''; }, 2500);
  } catch (e) {
    if (status) status.textContent = '保存失败: ' + e.message;
  }
};

// 收起生成模型下拉弹层。弹层挂在 document.body(传送门),不随设置弹窗的
// hidden 一起消失,故关闭弹窗时需显式调用(见 renderer.js 的 .close-modal/overlay)。
App.Settings.closeGenCombos = function () { _genComboCloseAll(); };

// 绑定按钮 + 首次加载(每次打开设置弹窗时调用,见 renderer.js openSettingsModal)
App.Settings.bindGenModels = function () {
  const save = document.getElementById('saveGenModelsBtn');
  const reload = document.getElementById('reloadGenModelsBtn');
  if (save && !save._bound) {
    save._bound = true;
    save.addEventListener('click', () => App.Settings.saveGenModels());
  }
  if (reload && !reload._bound) {
    reload._bound = true;
    reload.addEventListener('click', () => App.Settings.loadGenModels());
  }
  App.Settings.loadGenModels();
};

App.Settings.loadRules = async function () {
  try {
    const res = await App.apiJSON('/v1/rules');
    if (res.success) App.Settings._ruleSets = res.data?.sets || [];
  } catch (_) { /* 后端旧版无此接口时忽略 */ }
  App.Settings.renderRuleSets();
};

App.Settings.renderRuleSets = async function () {
  const box = document.getElementById('ruleSetsAdmin');
  if (!box) return;

  // 「新建规则集」按钮:先绑定(无论有没有规则集都要能点),再渲染列表。
  // 此前绑定逻辑放在函数末尾,空规则集时早 return 导致按钮从未绑定 → 点了无反应。
  const addBtn = document.getElementById('addRuleSetBtn');
  if (addBtn && !addBtn._bound) {
    addBtn._bound = true;
    addBtn.addEventListener('click', async () => {
      const n = await App.prompt('新规则集名称(如:代码项目规则 / 写作规则):');
      if (!n || !n.trim()) return;
      App.apiJSON('/v1/rules/sets', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: n.trim() }),
      }).then((res) => {
        if (res && res.success) {
          App.Settings.loadRules();
          if (App.toast) App.toast('已创建规则集', 'ok');
        } else {
          if (App.toast) App.toast('创建失败:' + (res && (res.detail || res.status)), 'err');
        }
      }).catch((e) => {
        if (App.toast) App.toast('创建失败:' + e.message, 'err');
      });
    });
  }

  box.textContent = '';
  const sets = App.Settings._ruleSets || [];
  if (!sets.length) {
    const empty = document.createElement('div');
    empty.className = 'prov-card-empty';
    empty.textContent = '暂无规则集,点击「新建规则集」创建';
    box.appendChild(empty);
    return;
  }
  sets.forEach((s) => {
    const card = document.createElement('div');
    card.className = 'prov-card';

    // 头部: 集名 + 计数 + 操作
    const header = document.createElement('div');
    header.className = 'prov-card-header';
    const left = document.createElement('div');
    left.className = 'prov-card-left';
    const nameEl = document.createElement('span');
    nameEl.className = 'prov-card-name';
    nameEl.textContent = s.set_name;
    const cnt = document.createElement('span');
    cnt.className = 'prov-card-tag custom';
    cnt.textContent = `规则 ${s.enabled_count || 0}/${s.rule_count || 0}`;
    left.append(nameEl, cnt);

    const actions = document.createElement('div');
    actions.className = 'prov-card-actions';
    const addRuleBtn = document.createElement('button');
    addRuleBtn.className = 'prov-action';
    addRuleBtn.textContent = '＋ 加规则';
    addRuleBtn.addEventListener('click', () => App.Settings._openRuleForm(s, null));
    const renameBtn = document.createElement('button');
    renameBtn.className = 'prov-action';
    renameBtn.textContent = '改名';
    renameBtn.addEventListener('click', async () => {
      const n = await App.prompt('规则集新名称:', s.set_name);
      if (n && n.trim()) {
        App.apiJSON(`/v1/rules/sets/${encodeURIComponent(s.set_id)}/rename`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: n.trim() }),
        }).then(() => App.Settings.loadRules()).catch(() => {});
      }
    });
    const delBtn = document.createElement('button');
    delBtn.className = 'prov-action danger';
    delBtn.textContent = '删除';
    delBtn.addEventListener('click', async () => {
      if (!await App.confirm(`删除规则集「${s.set_name}」及其全部规则?`)) return;
      try { await App.apiJSON(`/v1/rules/sets/${encodeURIComponent(s.set_id)}`, { method: 'DELETE' }); } catch (_) {}
      App.Settings.loadRules();
    });
    actions.append(addRuleBtn, renameBtn, delBtn);
    header.append(left, actions);
    card.appendChild(header);

    // 规则行
    const modelList = document.createElement('div');
    modelList.className = 'prov-card-models';
    (s.rules || []).forEach((r) => {
      if (r.name === '(empty)') return; // 跳过创建集时的占位行
      const item = document.createElement('div');
      item.className = 'rule-row' + (r.enabled ? ' active' : '');

      // 启用开关(toggle)
      const toggle = document.createElement('label');
      toggle.className = 'rule-toggle';
      const sw = document.createElement('input');
      sw.type = 'checkbox';
      sw.checked = !!r.enabled;
      sw.title = r.enabled ? '已启用' : '已停用';
      sw.addEventListener('change', () => {
        App.apiJSON(`/v1/rules/${encodeURIComponent(r.id)}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: sw.checked }),
        }).then(() => App.Settings.loadRules()).catch(() => {});
      });
      const track = document.createElement('span');
      track.className = 'track';
      toggle.append(sw, track);

      // 主体: 名称 + 类型 + 内容预览
      const main = document.createElement('div');
      main.className = 'rule-main';
      const nameLine = document.createElement('div');
      nameLine.className = 'rule-name-line';
      const nm = document.createElement('span');
      nm.className = 'rule-name';
      nm.textContent = r.name;
      nm.title = r.name;
      const typeEl = document.createElement('span');
      typeEl.className = 'rule-type';
      typeEl.textContent = RULE_TYPE_LABELS[r.type] || r.type;
      nameLine.append(nm, typeEl);
      const content = document.createElement('div');
      content.className = 'rule-content';
      content.textContent = r.content || '（无内容）';
      content.title = r.content || '';
      main.append(nameLine, content);

      // 操作: 编辑 / 删除(常驻可见)
      const actions = document.createElement('div');
      actions.className = 'rule-actions';
      const editBtn = document.createElement('button');
      editBtn.className = 'rule-act';
      editBtn.title = '编辑';
      editBtn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>';
      editBtn.addEventListener('click', (e) => { e.stopPropagation(); App.Settings._openRuleForm(s, r); });
      const delRBtn = document.createElement('button');
      delRBtn.className = 'rule-act danger';
      delRBtn.title = '删除';
      delRBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
      delRBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!await App.confirm(`删除规则「${r.name}」?`)) return;
        App.apiJSON(`/v1/rules/${encodeURIComponent(r.id)}`, { method: 'DELETE' })
          .then(() => App.Settings.loadRules()).catch(() => {});
      });
      actions.append(editBtn, delRBtn);

      item.append(toggle, main, actions);
      modelList.appendChild(item);
    });
    card.appendChild(modelList);
    box.appendChild(card);
  });
};

// 规则表单: s=所属集, r=null 新建 / r=编辑
App.Settings._openRuleForm = function (s, r) {
  const form = document.createElement('div');
  form.className = 'prov-form';
  const doClose = () => { const m = form.closest('.modal-mask'); if (m) App.UI.Modal.close(m); };

  const mkField = (labelText, inputEl) => {
    const label = document.createElement('label');
    const span = document.createElement('span');
    span.textContent = labelText;
    label.append(span, inputEl);
    return label;
  };
  const nameIn = document.createElement('input');
  nameIn.type = 'text';
  nameIn.placeholder = '规则名称';
  nameIn.value = r ? r.name : '';

  const typeSel = document.createElement('select');
  RULE_TYPES.forEach((t) => {
    const o = document.createElement('option');
    o.value = t;
    o.textContent = RULE_TYPE_LABELS[t] || t;
    typeSel.appendChild(o);
  });
  typeSel.value = r ? r.type : 'instruction';

  const contentIn = document.createElement('textarea');
  contentIn.rows = 3;
  contentIn.className = 'setting-textarea';
  contentIn.placeholder = '软规则填指令正文;硬规则填内容(工具/词/路径,逗号或换行分隔)';
  contentIn.value = r ? r.content : '';

  const status = document.createElement('div');
  status.className = 'prov-status muted';
  const ops = document.createElement('div');
  ops.className = 'prov-form-ops';

  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.className = 'primary';
  saveBtn.textContent = r ? '保存' : '新增';
  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.textContent = '取消';
  cancelBtn.addEventListener('click', doClose);
  saveBtn.addEventListener('click', async () => {
    const name = nameIn.value.trim();
    if (!name) { status.textContent = '请填写规则名称'; status.className = 'prov-status err'; return; }
    try {
      if (r) {
        const res = await App.apiJSON(`/v1/rules/${encodeURIComponent(r.id)}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, type: typeSel.value, content: contentIn.value }),
        });
        if (res.success) { doClose(); App.Settings.loadRules(); }
        else { status.textContent = '保存失败:' + (res.detail || ''); status.className = 'prov-status err'; }
      } else {
        const res = await App.apiJSON(`/v1/rules/sets/${encodeURIComponent(s.set_id)}`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ set_id: s.set_id, set_name: s.set_name, name, type: typeSel.value, content: contentIn.value, enabled: true, action: 'block' }),
        });
        if (res.success) { doClose(); App.Settings.loadRules(); }
        else { status.textContent = '保存失败:' + (res.detail || ''); status.className = 'prov-status err'; }
      }
    } catch (e) {
      status.textContent = '保存失败:' + e.message;
      status.className = 'prov-status err';
    }
  });
  ops.append(saveBtn, cancelBtn);
  form.append(mkField('名称', nameIn), mkField('类型', typeSel), mkField('内容', contentIn), status, ops);

  // 以居中弹窗展示表单(不再追加到设置面板底部)
  App.UI.Modal.open({ content: form, wide: true });
  nameIn.focus();
};

/* ---------- 技能管理(/v1/skills/*) ---------- */

App.Settings._skills = [];

App.Settings.loadSkills = async function () {
  try {
    const res = await App.apiJSON('/v1/skills?enabled_only=false');
    if (res.success) App.Settings._skills = res.data?.skills || [];
  } catch (_) { /* 后端旧版无此接口时忽略 */ }
  App.Settings.renderSkills();
};

App.Settings.renderSkills = async function () {
  const box = document.getElementById('skillsAdmin');
  if (!box) return;

  // 「刷新」按钮:先绑定(避免容器被清空后丢失事件)
  const refreshBtn = document.getElementById('refreshSkillsBtn');
  if (refreshBtn && !refreshBtn._bound) {
    refreshBtn._bound = true;
    refreshBtn.addEventListener('click', () => App.Settings.loadSkills());
  }

  box.textContent = '';
  const list = App.Settings._skills || [];
  if (!list.length) {
    const empty = document.createElement('div');
    empty.className = 'prov-card-empty';
    empty.textContent = '暂无技能';
    box.appendChild(empty);
    return;
  }
  list.forEach((s) => {
    const card = document.createElement('div');
    card.className = 'prov-card';

    const header = document.createElement('div');
    header.className = 'prov-card-header';
    const left = document.createElement('div');
    left.className = 'prov-card-left';
    const nameEl = document.createElement('span');
    nameEl.className = 'prov-card-name';
    nameEl.textContent = s.name;
    const tag = document.createElement('span');
    tag.className = 'prov-card-tag custom';
    tag.textContent = `${s.steps} 步` + (s.params && s.params.length ? ` · ${s.params.length} 参数` : '');
    left.append(nameEl, tag);

    const actions = document.createElement('div');
    actions.className = 'prov-card-actions';
    // 启用开关(toggle)
    const toggle = document.createElement('label');
    toggle.className = 'rule-toggle';
    const sw = document.createElement('input');
    sw.type = 'checkbox';
    sw.checked = !!s.enabled;
    sw.title = s.enabled ? '已启用' : '已停用';
    sw.addEventListener('change', async () => {
      try {
        const res = await App.apiJSON(`/v1/skills/${encodeURIComponent(s.name)}/toggle`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: sw.checked }),
        });
        if (res && res.success) {
          s.enabled = sw.checked;
          if (App.toast) App.toast(sw.checked ? '已启用技能' : '已停用技能', 'ok');
        } else {
          sw.checked = !sw.checked; // 回滚
          if (App.toast) App.toast('操作失败:' + (res && (res.detail || res.status)), 'err');
        }
      } catch (e) {
        sw.checked = !sw.checked;
        if (App.toast) App.toast('操作失败:' + e.message, 'err');
      }
    });
    const track = document.createElement('span');
    track.className = 'track';
    toggle.append(sw, track);
    // 详情
    const detailBtn = document.createElement('button');
    detailBtn.className = 'prov-action';
    detailBtn.textContent = '详情';
    detailBtn.addEventListener('click', () => App.Settings._openSkillDetail(s.name));
    actions.append(toggle, detailBtn);
    header.append(left, actions);
    card.appendChild(header);

    // 描述
    if (s.description) {
      const desc = document.createElement('div');
      desc.className = 'prov-card-desc';
      desc.textContent = s.description;
      desc.title = s.description;
      card.appendChild(desc);
    }
    box.appendChild(card);
  });
};

// 技能详情:展开步骤与参数(以居中弹窗展示,不再追加到设置面板底部)
App.Settings._openSkillDetail = async function (name) {
  const panel = document.createElement('div');
  panel.className = 'prov-form';
  const head = document.createElement('div');
  head.className = 'prov-form-title';
  head.textContent = `技能详情 · ${name}`;
  panel.appendChild(head);

  const loading = document.createElement('div');
  loading.className = 'prov-status muted';
  loading.textContent = '加载中…';
  panel.appendChild(loading);

  App.UI.Modal.open({
    content: panel,
    wide: true,
    actions: '<button class="cd-cancel" onclick="App.UI.Modal.close(this.closest(\'.modal-mask\'))">关闭</button>',
  });

  try {
    const res = await App.apiJSON(`/v1/skills/${encodeURIComponent(name)}`);
    if (!res || !res.success) {
      loading.textContent = '加载失败:' + (res && (res.detail || res.status));
      loading.className = 'prov-status err';
      return;
    }
    const d = res.data || {};
    panel.removeChild(loading);

    const descEl = document.createElement('div');
    descEl.className = 'prov-card-desc';
    descEl.textContent = d.description || '（无描述）';
    panel.appendChild(descEl);

    // 步骤
    const stepsTitle = document.createElement('div');
    stepsTitle.className = 'prov-form-subtitle';
    stepsTitle.textContent = `步骤（${d.steps ? d.steps.length : 0}）`;
    panel.appendChild(stepsTitle);
    (d.steps || []).forEach((st, i) => {
      const row = document.createElement('div');
      row.className = 'prov-step-row';
      const idx = document.createElement('span');
      idx.className = 'prov-step-idx';
      idx.textContent = `${i + 1}.`;
      const tool = document.createElement('span');
      tool.className = 'prov-step-tool';
      tool.textContent = st.tool;
      const args = document.createElement('code');
      args.className = 'prov-step-args';
      args.textContent = JSON.stringify(st.args || {});
      row.append(idx, tool, args);
      if (st.label) {
        const lbl = document.createElement('span');
        lbl.className = 'prov-step-label';
        lbl.textContent = st.label;
        row.appendChild(lbl);
      }
      panel.appendChild(row);
    });

    // 参数
    if (d.params && d.params.length) {
      const pTitle = document.createElement('div');
      pTitle.className = 'prov-form-subtitle';
      pTitle.textContent = `参数（${d.params.length}）`;
      panel.appendChild(pTitle);
      d.params.forEach((p) => {
        const row = document.createElement('div');
        row.className = 'prov-step-row';
        const nm = document.createElement('span');
        nm.className = 'prov-step-tool';
        nm.textContent = p.name + (p.required ? ' *' : '');
        const meta = document.createElement('span');
        meta.className = 'prov-step-label';
        meta.textContent = `${p.type || 'any'}${p.default != null ? ' 默认:' + p.default : ''}`;
        const dsc = document.createElement('div');
        dsc.className = 'prov-card-desc';
        dsc.textContent = p.description || '';
        row.append(nm, meta);
        panel.appendChild(row);
        if (dsc.textContent) panel.appendChild(dsc);
      });
    }
  } catch (e) {
    loading.textContent = '加载失败:' + e.message;
    loading.className = 'prov-status err';
  }
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
    // 加载智能体规则（设置面板 rules 分区数据源）
    App.Settings.loadRules();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', doInit);
  } else {
    doInit();
  }
})();
