/* 渲染进程入口:DOM 引用 / 事件绑定 / 面板切换 / 初始化 */
(() => {
  const $ = (id) => document.getElementById(id);

  App.els = {
    messages: $('messages'),
    input: $('input'),
    sendBtn: $('sendBtn'),
    stopBtn: $('stopBtn'),
    providerSelect: $('providerSelect'),
    modelSelect: $('modelSelect'),
    modeSelect: $('modeSelect'),
    criticModelInput: $('criticModelInput'),
    ensembleModelsInput: $('ensembleModelsInput'),
    // 可视化协作模式选择器
    criticProviderSelect: $('criticProviderSelect'),
    criticModelSelect: $('criticModelSelect'),
    ensembleList: $('ensembleList'),
    ensembleProvSelect: $('ensembleProvSelect'),
    ensembleModelSelect: $('ensembleModelSelect'),
    ensembleAddBtn: $('ensembleAddBtn'),
    imageBtn: $('imageBtn'),
    imageInput: $('imageInput'),
    shotBtn: $('shotBtn'),
    imgPreviewBar: $('imgPreviewBar'),
    previewImg: $('previewImg'),
    previewName: $('previewName'),
    previewCancel: $('previewCancel'),
    sessionList: $('sessionList'),
    sessionSearch: $('sessionSearch'),
    sessionLabel: $('sessionLabel'),
    statusDot: $('statusDot'),
    statusText: $('statusText'),
    ctxMeter: $('ctxMeter'),
    ctxFill: $('ctxFill'),
    ctxText: $('ctxText'),
    attachBtn: $('attachBtn'),
    attachInput: $('attachInput'),
    attachBar: $('attachBar'),
    attachList: $('attachList'),
    attachClearBtn: $('attachClearBtn'),
    kbUploadBtn: $('kbUploadBtn'),
    kbClearBtn: $('kbClearBtn'),
    dropZone: $('dropZone'),
    docStats: $('docStats'),
    docList: $('docList'),
    fileInput: $('fileInput'),
    memoryRefreshBtn: $('memoryRefreshBtn'),
    memoryClearBtn: $('memoryClearBtn'),
    memoryAddBtn: $('memoryAddBtn'),
    memoryInput: $('memoryInput'),
    memoryKwInput: $('memoryKwInput'),
    memoryStats: $('memoryStats'),
    memoryList: $('memoryList'),
    runsRefreshBtn: $('runsRefreshBtn'),
    runsList: $('runsList'),
    telRefreshBtn: $('telRefreshBtn'),
    telStats: $('telStats'),
    telRuns: $('telRuns'),
    schedRefreshBtn: $('schedRefreshBtn'),
    schedNameInput: $('schedNameInput'),
    schedFuncInput: $('schedFuncInput'),
    schedCronInput: $('schedCronInput'),
    schedIntervalInput: $('schedIntervalInput'),
    schedAddBtn: $('schedAddBtn'),
    schedList: $('schedList'),
    promptAddBtn: $('promptAddBtn'),
    promptSaveBtn: $('promptSaveBtn'),
    promptTitleInput: $('promptTitleInput'),
    promptTextInput: $('promptTextInput'),
    promptList: $('promptList'),
    toolGrid: $('toolGrid'),
    themeToggle: $('themeToggle'),
    themeBtn: $('themeBtn'),
    backendUrlText: $('backendUrlText'),
    backendStatusText: $('backendStatusText'),
    todoList: $('todoList'),
    todoInput: $('todoInput'),
    todoCatInput: $('todoCatInput'),
    todoAddBtn: $('todoAddBtn'),
    exportBtn: $('exportBtn'),
    lightbox: $('lightbox'),
    lbImg: $('lbImg'),
    lbClose: $('lbClose'),
    lbDownload: $('lbDownload'),
    lbCopy: $('lbCopy'),
  };

  /* ---------- 面板切换 ---------- */
  let currentView = 'chat';
  const panelMap = { todo: 'todoPanel', kb: 'kbPanel', memory: 'memoryPanel', runs: 'runsPanel', telemetry: 'telemetryPanel', schedule: 'schedulePanel', prompts: 'promptsPanel', tools: 'toolsPanel', settings: 'settingsPanel' };

  function showView(view) {
    document.querySelectorAll('.nav-btn').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
    if (view === 'chat') {
      document.body.classList.remove('side-open');
      currentView = 'chat';
      return;
    }
    const panelId = panelMap[view];
    if (!panelId) return;
    document.querySelectorAll('.side-content').forEach((el) => el.classList.toggle('active', el.id === panelId));
    document.body.classList.add('side-open');
    currentView = view;
    // 懒加载:进入面板时刷新数据
    if (view === 'memory' && App.Memory) App.Memory.refresh();
    if (view === 'runs' && App.Runs) App.Runs.load();
    if (view === 'telemetry' && App.Telemetry) App.Telemetry.load();
    if (view === 'schedule' && App.Schedule) App.Schedule.load();
    if (view === 'prompts' && App.Prompts) App.Prompts.render();
  }

  function closePanel() {
    document.body.classList.remove('side-open');
    document.querySelectorAll('.nav-btn').forEach((b) => b.classList.toggle('active', b.dataset.view === 'chat'));
    currentView = 'chat';
  }

  document.querySelectorAll('.nav-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const view = btn.dataset.view;
      if (currentView === view && view !== 'chat') {
        closePanel();
      } else {
        showView(view);
      }
    });
  });
  document.querySelectorAll('.close-panel').forEach((b) => b.addEventListener('click', closePanel));

  /* ---------- 会话 ---------- */
  $('collapseBtn').addEventListener('click', () => App.Session.toggleCollapse());
  const reopenBtn = document.getElementById('sessionReopen');
  if (reopenBtn) reopenBtn.addEventListener('click', () => App.Session.toggleCollapse());
  $('newSessionBtn').addEventListener('click', () => App.Session.newSession());
  $('sessionSearch').addEventListener('input', debounce(() => App.Session.search(), 200));
  const clearSessionBtn = document.getElementById('clearSessionBtn');
  if (clearSessionBtn) clearSessionBtn.addEventListener('click', () => App.Session.clearCurrent());
  $('sessionList').addEventListener('click', (e) => {
    const item = e.target.closest('.session-item');
    if (item) App.Session.select(item.dataset.id);
  });
  $('exportBtn').addEventListener('click', () => App.Session.exportCurrent());

  /* 回到最新(滚动远离底部时显示) */
  const backToBottomBtn = document.getElementById('backToBottom');
  if (backToBottomBtn) {
    backToBottomBtn.addEventListener('click', () => {
      App.els.messages.scrollTop = App.els.messages.scrollHeight;
    });
    App.els.messages.addEventListener('scroll', () => {
      const nearBottom = App.els.messages.scrollHeight - App.els.messages.scrollTop - App.els.messages.clientHeight < 120;
      document.body.classList.toggle('show-back', !nearBottom);
    });
  }

  function debounce(fn, ms) {
    let t = null;
    return function (...args) {
      if (t) clearTimeout(t);
      t = setTimeout(() => fn.apply(this, args), ms);
    };
  }

  /* ---------- 聊天 ---------- */
  App.els.sendBtn.addEventListener('click', () => App.Chat.send());
  App.els.stopBtn.addEventListener('click', () => App.Chat.stop(false));
  App.els.input.addEventListener('keydown', (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      App.Chat.send();
    }
  });

  /* 粘贴图片:输入框内 Ctrl+V 携带图片时直接选作识别图片 */
  App.els.input.addEventListener('paste', (e) => {
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    for (const it of items) {
      if (it.type && it.type.startsWith('image/')) {
        const file = it.getAsFile();
        if (file) {
          e.preventDefault();
          App.Chat.selectImage(file);
          App.appendMessage(`📋 已粘贴图片: ${file.name || 'clipboard.png'}`, 'bot');
        }
        break;
      }
    }
  });

  /* 拖拽附件:拖到聊天区松开即上传为附件 */
  const main = document.getElementById('main');
  let dragDepth = 0;
  const draggingOverInput = (e) => {
    const dt = e.dataTransfer;
    return !!(dt && dt.types && dt.types.indexOf('Files') !== -1);
  };
  main.addEventListener('dragenter', (e) => {
    if (!draggingOverInput(e)) return;
    e.preventDefault();
    dragDepth++;
    main.classList.add('chat-dragging');
  });
  main.addEventListener('dragover', (e) => {
    if (!draggingOverInput(e)) return;
    e.preventDefault();
  });
  main.addEventListener('dragleave', (e) => {
    if (!draggingOverInput(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) main.classList.remove('chat-dragging');
  });
  main.addEventListener('drop', (e) => {
    if (!draggingOverInput(e)) return;
    e.preventDefault();
    dragDepth = 0;
    main.classList.remove('chat-dragging');
    const files = e.dataTransfer && e.dataTransfer.files;
    if (files && files.length && App.Attach) App.Attach.upload(files);
  });

  /* 代码块复制按钮(事件委托) */
  App.els.messages.addEventListener('click', (e) => {
    const btn = e.target.closest('.code-copy');
    if (btn) App.copyCodeFromButton(btn);
  });

  /* ---------- 输入区自适应:自动增高 + 空内容禁用发送 ---------- */
  App.syncComposer = function () {
    const inp = App.els.input;
    if (!inp) return;
    inp.style.height = 'auto';
    inp.style.height = Math.min(inp.scrollHeight, 164) + 'px';
    App.els.sendBtn.disabled = !inp.value.trim() || App.streaming;
  };

  /* ---------- 图片识别 ---------- */
  App.els.imageBtn.addEventListener('click', () => {
    if (App.selectedImageFile) { App.Chat.recognizeImage(App.selectedImageFile); return; }
    App.els.imageInput.click();
  });
  App.els.imageInput.addEventListener('change', () => {
    if (App.els.imageInput.files && App.els.imageInput.files.length) {
      App.Chat.selectImage(App.els.imageInput.files[0]);
    }
    App.els.imageInput.value = '';
  });
  App.els.previewCancel.addEventListener('click', () => {
    App.selectedImageFile = null;
    App.previewUrl = '';
    App.els.imgPreviewBar.classList.remove('show');
  });
  App.els.shotBtn.addEventListener('click', () => App.Chat.captureScreen());
  App.els.previewImg.addEventListener('click', () => {
    if (App.previewUrl) App.openLb(App.previewUrl, App.els.previewName.textContent || '图片');
  });

  /* ---------- 知识库 ---------- */
  App.els.kbUploadBtn.addEventListener('click', () => App.els.fileInput.click());
  App.els.kbClearBtn.addEventListener('click', () => App.KB.clearAll());
  App.els.fileInput.addEventListener('change', () => {
    App.KB.upload(App.els.fileInput.files);
    App.els.fileInput.value = '';
  });
  App.els.dropZone.addEventListener('click', () => App.els.fileInput.click());
  App.els.dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    App.els.dropZone.classList.add('drag');
  });
  App.els.dropZone.addEventListener('dragleave', () => App.els.dropZone.classList.remove('drag'));
  App.els.dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    App.els.dropZone.classList.remove('drag');
    if (e.dataTransfer && e.dataTransfer.files.length) App.KB.upload(e.dataTransfer.files);
  });

  /* ---------- 待办 ---------- */
  App.els.todoAddBtn.addEventListener('click', () => App.Todo.add());
  App.els.todoInput.addEventListener('keydown', (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === 'Enter') { e.preventDefault(); App.Todo.add(); }
  });
  const todoFilter = document.getElementById('todoFilter');
  if (todoFilter) todoFilter.addEventListener('change', () => App.Todo.load());

  /* ---------- 记忆管理 ---------- */
  App.els.memoryAddBtn.addEventListener('click', () => App.Memory.add());
  App.els.memoryInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) App.Memory.add();
  });
  App.els.memoryRefreshBtn.addEventListener('click', () => App.Memory.refresh());
  App.els.memoryClearBtn.addEventListener('click', () => App.Memory.clearAll());
  const memorySearch = document.getElementById('memorySearch');
  if (memorySearch) memorySearch.addEventListener('input', debounce(() => App.Memory.renderList(), 200));
  const memoryScope = document.getElementById('memoryScope');
  if (memoryScope) memoryScope.addEventListener('change', () => { if (App.Memory) App.Memory.renderList(); });

  /* ---------- 运行记录 ---------- */
  App.els.runsRefreshBtn.addEventListener('click', () => App.Runs.load());

  /* ---------- 用量统计 ---------- */
  App.els.telRefreshBtn.addEventListener('click', () => App.Telemetry.load());

  /* ---------- 定时任务 ---------- */
  App.els.schedRefreshBtn.addEventListener('click', () => App.Schedule.load());
  App.els.schedAddBtn.addEventListener('click', () => App.Schedule.add());
  App.els.schedCronInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') App.Schedule.add(); });

  /* ---------- 提示词库 ---------- */
  App.els.promptAddBtn.addEventListener('click', () => App.Prompts.add());
  App.els.promptSaveBtn.addEventListener('click', () => App.Prompts.add());

  /* ---------- 快捷指令:输入框输入 / 唤起提示词候选 ---------- */
  let promptHint = null;
  let promptHintIndex = -1;
  function renderPromptHint() {
    const input = App.els.input;
    const raw = input.value;
    const m = raw.match(/(?:^|\s)\/([^\s/]*)$/);
    if (!m) { closePromptHint(); return; }
    const cands = App.Prompts.candidates(m[1]);
    if (!cands.length) { closePromptHint(); return; }
    if (!promptHint) {
      promptHint = document.createElement('div');
      promptHint.className = 'prompt-hint';
      document.body.appendChild(promptHint);
    }
    promptHint.innerHTML = '';
    cands.forEach((p, i) => {
      const row = document.createElement('div');
      row.className = 'prompt-hint-item' + (i === promptHintIndex ? ' active' : '');
      const t = document.createElement('span');
      t.className = 'ph-title';
      t.textContent = p.title;
      const s = document.createElement('span');
      s.className = 'ph-text';
      s.textContent = p.text.slice(0, 40);
      row.appendChild(t);
      row.appendChild(s);
      row.addEventListener('mousedown', (e) => { e.preventDefault(); applyPromptHint(p); });
      row.addEventListener('mouseenter', () => { promptHintIndex = i; renderPromptHint(); });
      promptHint.appendChild(row);
    });
    const r = input.getBoundingClientRect();
    promptHint.style.left = (r.left + 8) + 'px';
    promptHint.style.bottom = (window.innerHeight - r.top + 6) + 'px';
    promptHint.style.display = 'block';
  }
  function closePromptHint() {
    if (promptHint) { promptHint.remove(); promptHint = null; }
    promptHintIndex = -1;
  }
  function applyPromptHint(p) {
    const input = App.els.input;
    const raw = input.value;
    const idx = raw.lastIndexOf('/');
    input.value = (raw.slice(0, idx) + p.text).trim();
    closePromptHint();
    App.syncComposer();
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }
  App.els.input.addEventListener('input', () => {
    promptHintIndex = -1;
    renderPromptHint();
    App.syncComposer();
  });
  App.els.input.addEventListener('keydown', (e) => {
    if (!promptHint) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const items = promptHint.querySelectorAll('.prompt-hint-item');
      const n = items.length;
      if (!n) return;
      promptHintIndex = e.key === 'ArrowDown'
        ? (promptHintIndex + 1) % n
        : (promptHintIndex <= 0 ? n - 1 : promptHintIndex - 1);
      renderPromptHint();
    } else if (e.key === 'Tab' || e.key === 'Enter') {
      const items = promptHint.querySelectorAll('.prompt-hint-item');
      const active = promptHintIndex >= 0 ? items[promptHintIndex] : items[0];
      if (active) {
        e.preventDefault();
        const cands = App.Prompts.candidates((App.els.input.value.match(/\/\s*([^\s/]*)$/) || [])[1] || '');
        const p = cands[promptHintIndex >= 0 ? promptHintIndex : 0];
        if (p) applyPromptHint(p);
      }
    } else if (e.key === 'Escape') {
      closePromptHint();
    }
  });
  document.addEventListener('click', (e) => {
    if (promptHint && !promptHint.contains(e.target)) closePromptHint();
  });

  /* ---------- 聊天附件 ---------- */
  if (App.Attach) App.Attach.init();

  /* ---------- 上下文用量 ---------- */
  App.CtxMeter = {
    async refresh() {
      if (!App.els.ctxFill) return;
      try {
        const res = await App.api(`/v1/chat/context?session_id=${encodeURIComponent(App.sessionId)}`);
        if (!res.ok) { this.set(0, '上下文不可用'); return; }
        const d = await res.json();
        const pct = d.usage_pct ?? 0;
        const text = `${d.tokens.toLocaleString()} / ${d.token_budget.toLocaleString()} tok · ${d.messages} 条`;
        this.set(pct, text, d.over_budget);
      } catch (_) { this.set(0, '-'); }
    },
    set(pct, text, over) {
      App.els.ctxFill.style.width = Math.min(100, Math.max(0, pct)) + '%';
      App.els.ctxFill.style.background = over
        ? 'var(--err)' : pct > 80
          ? 'var(--warn)' : 'var(--primary)';
      App.els.ctxText.textContent = text;
    },
  };

  /* ---------- 设置 ---------- */
  App.els.themeBtn.addEventListener('click', () => App.Settings.toggleTheme());
  App.els.themeToggle.addEventListener('click', () => App.Settings.toggleTheme());

  /* ---------- 灯箱 ---------- */
  App.els.lbClose.addEventListener('click', () => App.closeLb());
  App.els.lbDownload.addEventListener('click', () => App.downloadLb());
  App.els.lbCopy.addEventListener('click', () => App.copyLb());
  App.els.lightbox.addEventListener('click', (e) => {
    if (e.target === App.els.lightbox) App.closeLb();
  });
  // Markdown/历史中渲染的图片点击放大(事件委托)
  App.els.messages.addEventListener('click', (e) => {
    if (e.target && e.target.tagName === 'IMG') App.openLb(e.target.src, e.target.alt || '图片');
  });
  // 加载失败的图片自动隐藏(捕获阶段委托,替代内联 onerror,修复 XSS 风险)
  App.els.messages.addEventListener('error', (e) => {
    if (e.target && e.target.tagName === 'IMG') e.target.style.display = 'none';
  }, true);

  /* ---------- 模型 Provider ---------- */
  async function loadProviders() {
    try {
      const res = await App.api('/v1/providers');
      if (!res.ok) return;
      const data = await res.json();
      App.els.providerSelect.innerHTML = '';
      (data.providers || []).forEach((p) => {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = p.name + (p.configured ? '' : ' (未配置)');
        opt.disabled = !p.configured;
        App.els.providerSelect.appendChild(opt);
      });
      if (data.default) App.els.providerSelect.value = data.default;
      // Load models for default provider
      if (data.default) {
        await loadModels(data.default);
      }
      // Add change listener for provider selection
      App.els.providerSelect.onchange = async (e) => {
        await loadModels(e.target.value);
      };
    } catch (_) { /* 忽略 */ }
  }
  /* ---------- 加载模型列表 ---------- */
  async function loadModels(providerId) {
    if (!providerId) {
      App.els.modelSelect.innerHTML = '<option value="">请先选择提供商</option>';
      return;
    }
    try {
      const res = await App.api('/v1/models?provider=' + encodeURIComponent(providerId));
      if (!res.ok) {
        App.els.modelSelect.innerHTML = '<option value="">加载失败</option>';
        return;
      }
      const data = await res.json();
      App.els.modelSelect.innerHTML = '';
      if (!data.models || data.models.length === 0) {
        const opt = document.createElement('option');
        opt.value = data.default_model || '';
        opt.textContent = data.default_model ? data.default_model + ' (默认)' : '无可用模型';
        App.els.modelSelect.appendChild(opt);
      } else {
        data.models.forEach((m) => {
          const opt = document.createElement('option');
          opt.value = m.id;
          opt.textContent = m.name + (m.speed ? ' [' + m.speed + ']' : '');
          opt.title = (m.quality || '') + (m.speed ? ' | Speed: ' + m.speed : '');
          if (m.id === data.default_model) opt.selected = true;
          App.els.modelSelect.appendChild(opt);
        });
      }
      App._modelsData = data;
    } catch (_) {
      App.els.modelSelect.innerHTML = '<option value="">加载失败</option>';
    }
  }


  /* ---------- 协作模式 / 评审 / 多模型清单(本地持久化) ---------- */
  function loadModePrefs() {
    try {
      App.els.modeSelect.value = localStorage.getItem('agent_mode') || 'auto';
    } catch (_) { /* 忽略 */ }
  }
  function saveModePrefs() {
    try {
      localStorage.setItem('agent_mode', App.els.modeSelect.value);
    } catch (_) { /* 忽略 */ }
  }
  App.els.modeSelect.addEventListener('change', saveModePrefs);
  // 可视化 critic 选择器
  App.els.criticProviderSelect?.addEventListener('change', () => {
    const pid = App.els.criticProviderSelect.value;
    App.Settings.loadModels(pid, App.els.criticModelSelect);
    App.Settings.saveCriticFromUI();
  });
  App.els.criticModelSelect?.addEventListener('change', () => App.Settings.saveCriticFromUI());
  // 可视化 ensemble 列表: 提供商变化时同步加载模型
  App.els.ensembleProvSelect?.addEventListener('change', () => {
    const pid = App.els.ensembleProvSelect.value;
    App.els.ensembleModelSelect.innerHTML = '<option value="">选择模型</option>';
    if (pid) App.Settings.loadModels(pid, App.els.ensembleModelSelect);
  });
  App.els.ensembleAddBtn?.addEventListener('click', () => {
    const p = App.els.ensembleProvSelect?.value || '';
    const m = App.els.ensembleModelSelect?.value || '';
    if (!p || !m) { App.toast('请先选择提供商和模型', 'err'); return; }
    // 最多 3 个
    const currentCount = App.els.ensembleList?.querySelectorAll('.ensemble-item').length || 0;
    if (currentCount >= 3) { App.toast('最多添加 3 个模型', 'err'); return; }
    const item = document.createElement('div');
    item.className = 'ensemble-item';
    const provSel = document.createElement('select');
    provSel.className = 'set-select set-select-sm ens-prov';
    provSel.innerHTML = '<option value="">提供商</option>';
    (App.els.ensembleProvSelect?.options || []).forEach((opt) => {
      if (opt.value) { const o = opt.cloneNode(); provSel.appendChild(o); }
    });
    const modSel = document.createElement('select');
    modSel.className = 'set-select set-select-sm ens-model';
    modSel.innerHTML = '<option value="">选择模型</option>';
    const countBadge = document.createElement('span');
    countBadge.className = 'ens-count';
    countBadge.textContent = `#${currentCount + 1}`;
    const rmBtn = document.createElement('button');
    rmBtn.className = 'ens-remove';
    rmBtn.textContent = '×';
    rmBtn.title = '移除此模型';
    rmBtn.addEventListener('click', () => { item.remove(); App.Settings.saveEnsembleFromUI(); });
    item.appendChild(provSel);
    item.appendChild(modSel);
    item.appendChild(countBadge);
    item.appendChild(rmBtn);
    App.els.ensembleList.appendChild(item);
    provSel.value = p;
    modSel.value = m;
    provSel.addEventListener('change', () => {
      const pid = provSel.value;
      modSel.innerHTML = '<option value="">选择模型</option>';
      if (pid) App.Settings.loadModels(pid, modSel);
      App.Settings.saveEnsembleFromUI();
    });
    modSel.addEventListener('change', () => { App.Settings.saveEnsembleFromUI(); });
    App.Settings.loadModels(p, modSel);
    App.Settings.saveEnsembleFromUI();
  });

  /* ---------- 后端地址解析 ---------- */
  async function resolveBackendUrl() {
    try {
      if (window.agentApp && window.agentApp.getBackendUrl) {
        return await window.agentApp.getBackendUrl();
      }
    } catch (_) { /* 非 Electron 环境 */ }
    return 'http://127.0.0.1:8517';
  }

  /* ---------- 后端 API Key 解析(可选鉴权) ---------- */
  async function resolveBackendApiKey() {
    try {
      if (window.agentApp && window.agentApp.getBackendApiKey) {
        return (await window.agentApp.getBackendApiKey()) || '';
      }
    } catch (_) { /* 非 Electron 环境 */ }
    return '';
  }

  /* ---------- 后端状态推送(主进程自动重启提醒) ---------- */
  if (window.agentApp && window.agentApp.onBackendStatus) {
    window.agentApp.onBackendStatus((data) => {
      if (!data) return;
      if (data.running) {
        App.setStatus(true);
        if (data.restarted && data.restarts > 0) {
          App.toast(`后端已自动重启(第 ${data.restarts} 次)`, 'ok');
        }
      } else {
        App.setStatus(false, data.gaveUp
          ? `后端重启失败(已尝试 ${data.restarts} 次)`
          : '后端异常,正在自动重启...');
      }
    });
  }

  /* 设置页:重启后端按钮 */
  const restartBackendBtn = document.getElementById('restartBackendBtn');
  if (restartBackendBtn && window.agentApp && window.agentApp.restartBackend) {
    restartBackendBtn.addEventListener('click', async () => {
      if (!(await App.confirm('确定重启后端服务吗?正在进行的会话将中断。'))) return;
      restartBackendBtn.disabled = true;
      restartBackendBtn.textContent = '重启中...';
      const r = await window.agentApp.restartBackend();
      restartBackendBtn.disabled = false;
      restartBackendBtn.textContent = '重启后端';
      App.toast(r && r.ok ? '后端已重启' : (r && r.reason ? r.reason : '重启失败'), r && r.ok ? 'ok' : 'err');
      if (App._dataLoaded) App.Session.renderList();
    });
  }

  /* ---------- 空状态引导 / 欢迎页 ---------- */
  function syncWelcome() {
    const has = !!App.els.messages.querySelector('.msg');
    document.body.classList.toggle('has-messages', has);
  }
  if (window.MutationObserver) {
    new MutationObserver(syncWelcome).observe(App.els.messages, { childList: true });
  }
  syncWelcome();

  /* ---------- 初始化 ---------- */
  // 后端可能仍在启动(窗口先于后端出现), 轮询等待就绪再加载数据
  async function waitBackendReady() {
    for (let i = 0; i < 60; i++) {
      const ok = await App.Settings.refreshBackend();
      if (ok) return true;
      await new Promise((r) => setTimeout(r, 500));
    }
    return false;
  }

  function setupWelcomeChips() {
    document.querySelectorAll('.welcome-chip').forEach((c) => {
      c.addEventListener('click', () => {
        App.els.input.value = c.dataset.q || '';
        App.syncComposer();
        App.Chat.send();
      });
    });
  }

  async function loadAllData() {
    App._dataLoaded = true;
    const ids = (await App.Session.renderList()) || [];
    App.KB.refresh();
    App.Tools.load();
    App.Todo.load();
    App.Prompts.render();
    loadProviders();
    loadModePrefs();
    // 初始化协作模式可视化选择器（await 保证顺序）
    await App.Settings.loadProviders();
    await App.Settings.restoreCritic();
    await App.Settings.restoreEnsemble();
    await App.Session.restoreLast(ids);
    if (App.CtxMeter) App.CtxMeter.refresh();
  }

  async function init() {
    App.Settings.applyTheme(App.Settings.currentTheme());
    setupWelcomeChips();

    try {
      App.backendUrl = await resolveBackendUrl();
    } catch (e) {
      App.backendUrl = 'http://127.0.0.1:8517';
    }
    try {
      App.backendApiKey = await resolveBackendApiKey();
    } catch (e) {
      App.backendApiKey = '';
    }

    const ok = await waitBackendReady();
    if (ok) await loadAllData();
    App.els.input.focus();
    App.syncComposer();

    // 每 1.5s 轮询待确认的高风险操作(安全门禁批准卡片)
    setInterval(() => {
      if (App.Confirmation) App.Confirmation.poll();
    }, 1500);

    // 每 20s 刷新上下文用量
    setInterval(() => {
      if (App.CtxMeter) App.CtxMeter.refresh();
    }, 20000);

    // 每 30s 刷新连接状态; 若首次未就绪而后端恢复, 补载全部数据
    setInterval(async () => {
      const okNow = await App.Settings.refreshBackend();
      if (okNow && !App._dataLoaded) {
        App._dataLoaded = true;
        await loadAllData();
      } else if (okNow && App._dataLoaded) {
        // 后端恢复后刷新协作模式选择器
        await App.Settings.loadProviders();
        await App.Settings.restoreCritic();
        await App.Settings.restoreEnsemble();
      }
    }, 30000);
  }

  init();
})();
