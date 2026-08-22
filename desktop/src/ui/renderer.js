/* 渲染进程入口:DOM 引用 / 事件绑定 / 面板切换 / 初始化 */
(() => {
  const $ = (id) => document.getElementById(id);

  App.els = {
    messages: $('messages'),
    input: $('input'),
    sendBtn: $('sendBtn'),
    stopBtn: $('stopBtn'),
    modelPickerBtn: $('modelPickerBtn'),
    modelPopover: $('modelPopover'),
    modeSelect: $('modeSelect'),
    modeBadge: $('modeBadge'),
    modePopover: $('modePopover'),
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
    if (view === 'session' || view === 'chat') {
      // 切换到会话相关面板时刷新列表（含 collapse 状态变化）
      if (App.Session) App.Session.renderList();
    }
    if (view === 'memory' && App.Memory) App.Memory.refresh();
    if (view === 'runs' && App.Runs) App.Runs.load();
    if (view === 'telemetry' && App.Telemetry) App.Telemetry.load();
    if (view === 'schedule' && App.Schedule) App.Schedule.load();
    if (view === 'prompts' && App.Prompts) App.Prompts.render();
    if (view === 'settings' && App.Settings) {
      App.Settings.refreshBackend();
      if (App.Settings.renderProviderAdmin) App.Settings.renderProviderAdmin();
    }
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
    if (btn) { App.copyCodeFromButton(btn); return; }
    const fold = e.target.closest('.code-fold-toggle');
    if (fold) {
      const block = fold.closest('.code-block');
      if (!block) return;
      const collapsed = block.classList.toggle('collapsed');
      fold.textContent = collapsed
        ? `⤵ 展开全部 ${block.querySelectorAll('.code-body pre code')[0].textContent.split('\n').length} 行`
        : '⤴ 收起';
    }
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

  /* ---------- 执行面板切换 ---------- */
  const toggleSidePanelBtn = document.getElementById('toggleSidePanel');
  const execPanelCloseBtn = document.getElementById('execPanelClose');
  if (toggleSidePanelBtn) {
    toggleSidePanelBtn.addEventListener('click', () => {
      document.body.classList.toggle('exec-panel-open');
      toggleSidePanelBtn.textContent = document.body.classList.contains('exec-panel-open') ? '⚡ 隐藏' : '⚡ 执行';
    });
  }
  if (execPanelCloseBtn) {
    execPanelCloseBtn.addEventListener('click', () => {
      document.body.classList.remove('exec-panel-open');
      if (toggleSidePanelBtn) toggleSidePanelBtn.textContent = '⚡ 执行';
    });
  }

  // 执行面板标签切换
  document.querySelectorAll('.exec-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.exec-tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.exec-tab-content').forEach(c => c.classList.remove('active'));
      tab.classList.add('active');
      const target = tab.dataset.tab;
      const content = document.getElementById(target + 'Tab');
      if (content) content.classList.add('active');
    });
  });

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
  /* ---------- 协作模式(单一状态源 + 就地配置,见 js/mode.js) ---------- */
  // App.Mode.init() 在 loadAllData 中调用(provider/model 数据就绪后)



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
    // 各步骤相互隔离:单点异常不再中断后续组件初始化(避免"部分组件不显示")
    const safe = async (label, fn) => {
      try { return await fn(); } catch (e) { console.error(`[init] ${label} 失败:`, e); }
    };
    const ids = (await safe('renderList', () => App.Session.renderList())) || [];
    safe('KB', () => App.KB.refresh());
    safe('Tools', () => App.Tools.load());
    safe('Todo', () => App.Todo.load());
    safe('Prompts', () => App.Prompts.render());
    // 先加载 provider/model 数据,再初始化选择器与协作模式(两者 popover 均依赖 provider 缓存)
    await safe('loadProviders', () => App.Settings.loadProviders());
    safe('ModelPicker', () => App.ModelPicker.init());
    safe('Mode', () => App.Mode.init());
    await safe('restoreLast', () => App.Session.restoreLast(ids));
    safe('CtxMeter', () => App.CtxMeter.refresh());
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
        // 后端恢复后刷新 provider（单线程，不重复 restore）
        if (App.Settings && App.Settings.loadProviders) await App.Settings.loadProviders();
      }
    }, 30000);
  }

  init();
})();
