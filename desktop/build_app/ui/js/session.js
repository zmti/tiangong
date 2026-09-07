/* 会话模块:列表加载 / 搜索 / 切换 / 新建 / 折叠 */
App.Session = {};

/* 会话列表缓存:5s 内不重复请求 */
App.Session._listCache = { ids: null, ts: 0 };

/* 会话侧边栏折叠状态 */
App.Session._collapsed = false;

/* 会话侧边栏折叠/展开 - 使用新的 sidebar 结构 */
App.Session.toggleCollapse = function () {
  App.Session._collapsed = !App.Session._collapsed;
  document.body.classList.toggle('sidebar-collapsed', App.Session._collapsed);
  if (App.Storage) App.Storage.set('sidebar_collapsed', App.Session._collapsed);
};

App.Session.loadCollapse = function () {
  try {
    if (App.Storage) {
      App.Session._collapsed = !!App.Storage.get('sidebar_collapsed');
    } else {
      App.Session._collapsed = localStorage.getItem('agent_sidebar_collapsed') === '1';
    }
  } catch (_) {}
  if (App.Session._collapsed) {
    document.body.classList.add('sidebar-collapsed');
  }
};

/* 置顶会话 - 使用统一存储 */
App.Session.loadPins = function () {
  if (App.Storage) {
    const arr = App.Storage.get('pinned_sessions') || [];
    return new Set(arr);
  }
  try {
    const raw = localStorage.getItem('agent_pinned_sessions');
    const arr = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(arr) ? arr : []);
  } catch (_) { return new Set(); }
};

App.Session.savePins = function (set) {
  const arr = [...set];
  if (App.Storage) {
    App.Storage.set('pinned_sessions', arr);
  } else {
    try { localStorage.setItem('agent_pinned_sessions', JSON.stringify(arr)); } catch (_) {}
  }
  // 会话操作后清缓存,强制下次渲染时重新拉取
  App.Session._listCache = { ids: null, ts: 0 };
};

App.Session.togglePin = async function (id) {
  const pins = App.Session.loadPins();
  if (pins.has(id)) pins.delete(id); else pins.add(id);
  App.Session.savePins(pins);
  App.Session.renderCards();
};

/* 最近使用的会话 - 使用统一存储 */
App.Session.lastSessionId = function () {
  if (App.Storage) {
    return App.Storage.get('last_session') || '';
  }
  try { return localStorage.getItem('agent_session') || ''; } catch (_) { return ''; }
};

App.Session.saveSessionId = function (id) {
  if (App.Storage) {
    App.Storage.set('last_session', id);
  } else {
    try { localStorage.setItem('agent_session', id); } catch (_) {}
  }
};

/* 需要恢复的会话仍存在时才切换,避免重建一个空会话 */
App.Session.restoreLast = async function (ids) {
  const last = App.Session.lastSessionId();
  if (last && Array.isArray(ids) && ids.length && ids.includes(last)) {
    await App.Session.select(last);
  }
};

/* 格式化时间显示 */
App.Session.formatTime = function (ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const pad = (n) => String(n).padStart(2, '0');
  if (sameDay) return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const diff = now.getTime() - d.getTime();
  if (diff < 7 * 86400000) return `${d.getMonth() + 1}-${d.getDate()}`;
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};

/* 根据第一条用户消息自动生成会话标题(本地截取,无网络请求) */
App.Session.autoTitleFromMessage = function (text) {
  const t = String(text || '')
    .replace(/\r?\n/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/[?？!！:：;；]/g, ' ')
    .slice(0, 28);
  return t || '新会话';
};

/* 流结束后自动重命名当前会话(异步,不阻塞) */
App.Session.autoRename = async function (userText) {
  if (!userText || !App.sessionId) return;
  try {
    const title = App.Session.autoTitleFromMessage(userText);
    if (title === '新会话') return; // 跳过空内容
    await App.apiJSON('/v1/chat/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: App.sessionId, title }),
    });
    // 更新会话列表显示
    const card = document.querySelector(`.sidebar-session-history .session-item[data-id="${CSS.escape(App.sessionId)}"]`);
    if (card) { card.dataset.title = title; const t = card.querySelector('.s-title'); if (t) t.textContent = title; }
    if (App.els.sessionLabel) App.els.sessionLabel.textContent = title;
    App.Session._listCache = { ids: null, ts: 0 };
  } catch (_) { /* 忽略命名失败,不影响体验 */ }
};

/* 会话列表搜索(全文搜索) - 搜索侧边栏会话历史 */
App.Session.search = async function () {
  const historyEl = App.els.sidebarSessionHistory || document.getElementById('sidebarSessionHistory');
  const query = (document.getElementById('sessionSearch')?.value || '').trim();
  if (!query || !historyEl) {
    App.Session.renderCards();
    return;
  }
  try {
    const res = await App.apiJSON(`/v1/chat/search?q=${encodeURIComponent(query)}`);
    if (!res.success || !res.data?.results?.length) {
      historyEl.innerHTML = '<div style="padding:12px;text-align:center;color:var(--muted);font-size:12px;">未找到匹配内容</div>';
      return;
    }
    const results = res.data.results;
    historyEl.innerHTML = results.map(r => `
      <div class="session-item search-hit" data-id="${r.id}">
        <div class="s-title">${App.escapeHtml(r.title || '新会话')} · ${r.snippets?.length || 0} 处命中</div>
      </div>
    `).join('');
    historyEl.querySelectorAll('.session-item').forEach(item => {
      item.addEventListener('click', () => App.Session.select(item.dataset.id));
    });
  } catch (_) {
    historyEl.innerHTML = '<div style="padding:12px;text-align:center;color:var(--muted);font-size:12px;">网络错误</div>';
  }
};

/* 构建搜索结果项 */
App.Session.buildSearchItem = function (r) {
  const btn = document.createElement('button');
  btn.className = 'session-item search-hit';
  btn.dataset.id = r.session_id;
  btn.dataset.title = r.title;
  if (r.session_id === App.sessionId) btn.classList.add('active');

  const t = document.createElement('div');
  t.className = 's-title';
  t.textContent = `${r.title} · ${r.snippets.length} 处命中`;

  const meta = document.createElement('div');
  meta.className = 's-meta';
  const snips = document.createElement('div');
  snips.className = 's-snips';
  for (const s of r.snippets.slice(0, 2)) {
    const p = document.createElement('div');
    p.className = 's-snip';
    p.textContent = s.snippet;
    snips.appendChild(p);
  }

  btn.appendChild(t);
  btn.appendChild(snips);
  btn.appendChild(meta);
  return btn;
};

App.Session.renderList = async function () {
  const listEl = App.els.sidebarSessionHistory || document.getElementById('sidebarSessionHistory');
  if (!listEl) return [];
  try {
    const res = await App.apiJSON('/v1/chat/sessions');
    console.log('[Session] renderList res:', res);
    if (!res.success) {
      listEl.innerHTML = '<div class="empty-hint">加载失败</div>';
      App.Session._listCache = { ids: [], ts: Date.now() };
      return [];
    }
    console.log('[Session] sessions raw data:', JSON.stringify(res.data).slice(0, 200));
    const sessions = res.data?.sessions || [];
    const pins = App.Session.loadPins();
    const query = (document.getElementById('sessionSearch')?.value || '').trim().toLowerCase();
    const filtered = sessions.filter(s =>
      !query || (s.title || '').toLowerCase().includes(query)
    );
    if (!filtered.length) {
      listEl.innerHTML = '<div class="empty-hint">暂无会话</div>';
      App.Session._listCache = { ids: [], ts: Date.now() };
      return [];
    }
    // 按置顶和时间排序
    const sorted = [...filtered].sort((a, b) => {
      const ap = pins.has(a.id) ? 0 : 1;
      const bp = pins.has(b.id) ? 0 : 1;
      if (ap !== bp) return ap - bp;
      return (b.updated_at || 0) - (a.updated_at || 0);
    });

    listEl.innerHTML = sorted.map(s => {
      const isPinned = pins.has(s.id);
      const isActive = s.id === App.sessionId;
      const timeStr = App.Session.formatTime(s.updated_at);
      return `
        <div class="session-item ${isActive ? 'active' : ''} ${isPinned ? 'pinned' : ''}"
             data-id="${s.id}" data-title="${App.escapeHtml(s.title || '未命名')}">
          <div class="s-title">${App.escapeHtml(s.title || '新会话')}</div>
          <div class="s-meta">
            <span class="s-time">${timeStr}</span>
            <span class="s-count">${s.messages || 0} 条</span>
            <span class="s-pin ${isPinned ? 'active' : ''}" title="${isPinned ? '取消置顶' : '置顶会话'}">📌</span>
          </div>
        </div>
      `;
    }).join('');

    listEl.querySelectorAll('.session-item').forEach(el => {
      el.addEventListener('click', () => App.Session.select(el.dataset.id));
      el.querySelector('.s-pin')?.addEventListener('click', (e) => {
        e.stopPropagation();
        App.Session.togglePin(el.dataset.id);
      });
      // 双击重命名
      el.querySelector('.s-title')?.addEventListener('dblclick', (e) => {
        e.stopPropagation();
        App.Session.enterRename(sorted.find(s => s.id === el.dataset.id), el);
      });
    });

    const ids = sorted.map(s => s.id);
    App.Session._listCache = { ids, ts: Date.now() };
    return ids;
  } catch (err) {
    listEl.innerHTML = '<div class="empty-hint">加载失败</div>';
    console.error('[Session] 加载失败:', err);
    return [];
  }
};

/* 渲染会话卡片视图(新布局) */
App.Session.renderCards = async function () {
  const listEl = App.els.sidebarSessionHistory || document.getElementById('sidebarSessionHistory');
  if (!listEl) return;
  try {
    const res = await App.apiJSON('/v1/chat/sessions');
    if (!res.success) {
      listEl.innerHTML = '<div style="padding:48px;text-align:center;color:var(--muted);">加载失败</div>';
      return;
    }
    const sessions = res.data?.sessions || [];
    const pins = App.Session.loadPins();
    const query = (document.getElementById('sessionSearch')?.value || '').trim().toLowerCase();
    const filtered = sessions.filter(s =>
      !query || (s.title || '').toLowerCase().includes(query)
    );
    if (!filtered.length) {
      listEl.innerHTML = '<div style="padding:12px;text-align:center;color:var(--muted);font-size:12px;"><div style="font-size:28px;margin-bottom:8px;">💬</div><div style="font-size:13px;font-weight:500;color:var(--text);margin-bottom:4px;">暂无会话</div><div style="font-size:11px;">点击"新建对话"开始</div></div>';
      return;
    }
    const sorted = [...filtered].sort((a, b) => {
      const ap = pins.has(a.id) ? 0 : 1;
      const bp = pins.has(b.id) ? 0 : 1;
      if (ap !== bp) return ap - bp;
      return (b.updated_at || 0) - (a.updated_at || 0);
    });
    listEl.innerHTML = sorted.map(s => {
      const isActive = s.id === App.sessionId;
      const isPinned = pins.has(s.id);
      const timeStr = App.Session.formatTime(s.updated_at);
      return `
        <div class="session-item ${isActive ? 'active' : ''} ${isPinned ? 'pinned' : ''}"
             data-id="${s.id}" data-title="${App.escapeHtml(s.title || '未命名')}">
          <div class="s-title">${isPinned ? '📌 ' : ''}${App.escapeHtml(s.title || '新会话')}</div>
          <div class="s-meta">
            <span class="s-time">${timeStr}</span>
            <span class="s-count">${s.messages || 0} 条</span>
            <span class="s-pin ${isPinned ? 'active' : ''}" title="${isPinned ? '取消置顶' : '置顶会话'}">📌</span>
          </div>
        </div>
      `;
    }).join('');
    listEl.querySelectorAll('.session-item').forEach(item => {
      item.addEventListener('click', () => App.Session.select(item.dataset.id));
      item.querySelector('.s-pin')?.addEventListener('click', (e) => {
        e.stopPropagation();
        App.Session.togglePin(item.dataset.id);
      });
      item.querySelector('.s-title')?.addEventListener('dblclick', (e) => {
        e.stopPropagation();
        App.Session.enterRename(sorted.find(s => s.id === item.dataset.id), item);
      });
    });
  } catch (err) {
    listEl.innerHTML = '<div style="padding:48px;text-align:center;color:var(--muted);">加载失败</div>';
    console.error('[Session] renderCards failed:', err);
  }
  // 同步刷新侧边栏会话历史
  if (typeof App._renderSidebarHistory === 'function') App._renderSidebarHistory();
};

/* 进入重命名模式 */
App.Session.enterRename = function (s, btn) {
  if (!s) return;
  const titleEl = btn.querySelector('.s-title');
  const input = document.createElement('input');
  input.className = 's-edit-input';
  input.value = (s.title || '').trim();
  input.maxLength = 100;
  btn.insertBefore(input, btn.firstChild);
  titleEl.style.display = 'none';
  input.focus();
  input.select();

  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const val = input.value.trim();
    if (save && val && val !== (s.title || '')) {
      await App.Session.rename(s.id, val);
    } else {
      App.Session.renderCards();
    }
  };
  input.dataset.orig = input.value;

  input.addEventListener('keydown', (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
};

/* 重命名会话 */
App.Session.rename = async function (id, title) {
  try {
    const res = await App.apiJSON('/v1/chat/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: id, title }),
    });
    if (!res.success) throw new Error(res.detail || `HTTP ${res.status}`);
    if (id === App.sessionId) App.els.sessionLabel.textContent = title;
  } catch (e) {
    if (App.toast) App.toast(`重命名失败: ${e.message}`, 'err');
  }
  App.Session.renderCards();
};

/* 删除会话 */
App.Session.remove = async function (id) {
  const ok = await (App.confirm
    ? App.confirm(`确定删除会话「${id}」的全部记录吗?此操作不可恢复。`, { danger: true, okText: '删除' })
    : true);
  if (!ok) return;
  try {
    await App.apiJSON(`/v1/chat/clear?session_id=${encodeURIComponent(id)}`, { method: 'POST' });
    if (id === App.sessionId) {
      App.Session.newSession();
    } else {
      App.Session._listCache = { ids: null, ts: 0 };
      App.Session.renderCards();
    }
  } catch (e) {
    if (App.toast) App.toast(`删除失败: ${e.message}`, 'err');
  }
};

/* 清空当前会话的对话内容(保留会话本身) */
App.Session.clearCurrent = async function () {
  if (!App.sessionId) return;
  const ok = await (App.confirm
    ? App.confirm('确定清空当前会话的对话内容吗?', { danger: true, okText: '清空' })
    : true);
  if (!ok) return;
  try {
    // 清空后端记录,并保留会话本身
    await App.apiJSON(`/v1/chat/clear?session_id=${encodeURIComponent(App.sessionId)}`, { method: 'POST' });
    // 只清空内存中的消息
    App.els.messages.innerHTML = '';
    App._planCard = null;
    if (App._currentGroup) { App._currentGroup.wrap.remove(); App._currentGroup = null; }
    if (App.Attach) App.Attach.reset();
    if (App.CtxMeter) App.CtxMeter.refresh();
    App.Session.renderCards();
    if (App.toast) App.toast('当前会话已清空', 'ok');
  } catch (e) {
    if (App.toast) App.toast(`清空失败: ${e.message}`, 'err');
  }
};

/* 新建会话 */
App.Session.newSession = async function () {
  if (App.streaming && App.Chat?.stop) App.Chat.stop(true);
  if (App._currentGroup) {
    App._currentGroup.wrap.remove();
    App._currentGroup = null;
  }
  App.sessionId = 'desktop-' + Date.now().toString(36);
  App.Session.saveSessionId(App.sessionId);
  App.els.sessionLabel.textContent = '新会话';
  App.els.messages.innerHTML = '';
  // 显示聊天视图，隐藏会话列表
  App.els.input.focus();
  if (App.refreshWelcomePage) App.refreshWelcomePage();
  if (App.Attach) App.Attach.reset();
  if (App.CtxMeter) App.CtxMeter.refresh();
};

/* 切换会话 */
App.Session.select = async function (id) {
  if (id === App.sessionId) return;
  if (App.streaming && App.Chat?.stop) App.Chat.stop(true);
  // 切换会话时关闭当前分组
  if (App._currentGroup) {
    const g = App._currentGroup;
    if (g.head) g.head.innerHTML = `<span class="rg-pulse">📂</span><span class="rg-label">已切换会话(取消)</span>`;
    if (g.wrap) g.wrap.classList.add('req-group-done');
    App._currentGroup = null;
  }
  // 切换会话时重置所有状态
  if (App.clearImageGenerating) App.clearImageGenerating();
  App._thinkingWrap = null;
  App._answerWrap = null;
  App._mdBuf = '';
  if (App.ExecTrace) App.ExecTrace.clear();
  if (App.abortController) {
    try { App.abortController.abort(); } catch (_) { /* 忽略 */ }
    App.abortController = null;
  }
  App.sessionId = id;
  App.Session.saveSessionId(id);
  // 清空消息容器
  const msgs = App.els.messages;
  msgs.innerHTML = '';
  try {
    const res = await App.apiJSON(`/v1/chat/history?session_id=${encodeURIComponent(id)}`);
    if (!res.success) throw new Error(res.detail || `HTTP ${res.status}`);
    const data = res.data;
    // 批量构建 HTML 再一次性插入
    const fragments = [];
    let lastPlanContent = '';

    // 后端可能将文本存在 kind 或 content 字段
    function msgText(m) { return m.content || m.kind || ''; }

    // 预渲染所有 assistant 消息
    const assistantContents = [];
    for (const m of data.messages || []) {
      if (m.role === 'assistant') assistantContents.push(msgText(m));
    }
    const renderedAssistant = assistantContents.map((c) => App.renderMarkdown(c));

    let aiIdx = 0;
    for (const m of data.messages || []) {
      if (m.role === 'user') {
        const div = document.createElement('div');
        div.className = 'msg user';
        const avatar = document.createElement('div');
        avatar.className = 'avatar';
        avatar.textContent = '我';
        const body = document.createElement('div');
        body.className = 'msg-body';
        const bubble = document.createElement('div');
        bubble.className = 'answer-doc';
        bubble.textContent = msgText(m);
        body.appendChild(bubble);

        // 添加操作按钮
        const act = document.createElement('div');
        act.className = 'msg-actions';
        const timeEl = document.createElement('div');
        timeEl.className = 'msg-time';
        timeEl.textContent = new Date(m.timestamp || Date.now()).toLocaleTimeString();

        const mkAction = (title, label, fn) => {
          const b = document.createElement('button');
          b.className = 'msg-action';
          b.title = title;
          b.textContent = label;
          b.addEventListener('click', fn);
          act.appendChild(b);
        };
        mkAction('复制消息', '复制', () => {
          const txt = (bubble.textContent || '').trim();
          if (txt) navigator.clipboard.writeText(txt).catch(() => {});
        });
        if (App.Chat?.editMessage) {
          mkAction('编辑并重发', '编辑', () => App.Chat.editMessage(div, bubble));
        }

        body.appendChild(act);
        body.appendChild(timeEl);
        div.appendChild(avatar);
        div.appendChild(body);
        fragments.push(div);
        continue;
      }
      if (m.role === 'assistant') {
        const div = document.createElement('div');
        div.className = 'msg bot';
        const avatar = document.createElement('div');
        avatar.className = 'avatar';
        avatar.textContent = 'AI';
        const body = document.createElement('div');
        body.className = 'msg-body';
        const bubble = document.createElement('div');
        bubble.className = 'answer-doc';
        bubble.innerHTML = renderedAssistant[aiIdx++] || '';
        body.appendChild(bubble);

        const act = document.createElement('div');
        act.className = 'msg-actions';
        const timeEl = document.createElement('div');
        timeEl.className = 'msg-time';
        timeEl.textContent = new Date(m.timestamp || Date.now()).toLocaleTimeString();

        const mkAction = (title, label, fn) => {
          const b = document.createElement('button');
          b.className = 'msg-action';
          b.title = title;
          b.textContent = label;
          b.addEventListener('click', fn);
          act.appendChild(b);
        };
        mkAction('复制回复', '复制', () => {
          const txt = (bubble.textContent || '').trim();
          if (txt) navigator.clipboard.writeText(txt).catch(() => {});
        });
        if (App.Chat?.regenerate) {
          mkAction('重新生成', '重新生成', () => App.Chat.regenerate(div));
        }
        mkAction('朗读该回复', '🔊', () => {
          const txt = (bubble.textContent || '').trim();
          if (txt) App.speak?.(txt);
        });

        body.appendChild(act);
        body.appendChild(timeEl);
        div.appendChild(avatar);
        div.appendChild(body);
        fragments.push(div);
        continue;
      }
      if (m.role === 'error') {
        const div = document.createElement('div');
        div.className = 'msg error';
        div.textContent = msgText(m);
        fragments.push(div);
        continue;
      }
      if (m.role === 'plan' && App.appendPlan) {
        try {
          const plan = JSON.parse(msgText(m));
          if (JSON.stringify(plan) !== lastPlanContent) {
            lastPlanContent = JSON.stringify(plan);
            const p = App.appendPlan({ action: 'create', plan }, fragments);
            if (p) fragments.push(p);
          }
        } catch (_) { /* 忽略 */ }
        continue;
      }
      if (m.role === 'tool' && App.appendToolEvent) {
        let te = null;
        try { te = JSON.parse(msgText(m)); } catch (_) { te = null; }
        if (te) {
          const t = App.appendToolEvent(te.name || '工具', te.arguments, te.result || '', fragments);
          if (t) fragments.push(t);
        }
        continue;
      }
    }
    // 批量插入
    const fragment = document.createDocumentFragment();
    for (const el of fragments) fragment.appendChild(el);
    msgs.appendChild(fragment);
    msgs.scrollTop = msgs.scrollHeight;
    
    if (App.refreshWelcomePage) App.refreshWelcomePage();
    
    // 设置会话标题
    let title = '新会话';
    const item = document.querySelector(`.sidebar-session-history .session-item[data-id="${CSS.escape(id)}"]`);
    if (item && item.dataset.title) {
      title = item.dataset.title;
    } else if (data.messages && data.messages.length) {
      const first = data.messages.find((m) => m.role === 'user');
      if (first) title = String(msgText(first)).slice(0, 30);
    }
    App.els.sessionLabel.textContent = title;
  } catch (e) {
    if (App.appendMessage) App.appendMessage(`加载会话历史失败: ${e.message}`, 'error');
  }
  
  if (App.Attach) App.Attach.reset();
  if (App.CtxMeter) App.CtxMeter.refresh();
  App.els.input.focus();
};

/* 创建会话(后端无 POST /v1/chat/sessions,本地创建) */
App.Session.create = async function () {
  // 直接本地创建,无需网络请求
  App.Session.newSession();
};

/* 删除会话(调用后端 DELETE /v1/chat/session/{session_id}) */
App.Session.deleteSession = async function (id) {
  if (id === App.sessionId) {
    console.warn('[Session] 不能删除当前会话');
    return;
  }
  try {
    await App.apiJSON(`/v1/chat/session/${encodeURIComponent(id)}`, { method: 'DELETE' });
  } catch (_) { /* 忽略删除失败 */ }
  App.Session._listCache = { ids: null, ts: 0 };
  App.Session.renderCards();
};

/* 清空消息(简化版) */
App.Session.clearMessages = function () {
  if (App.els.messages) {
    App.els.messages.innerHTML = '';
  }
  if (App.els.sessionLabel) App.els.sessionLabel.textContent = '新会话';
  if (App.els.input) App.els.input.focus();
};

/* 导出会话为 Markdown */
App.Session.exportCurrent = async function () {
  try {
    const res = await App.apiJSON(`/v1/chat/export?session_id=${encodeURIComponent(App.sessionId)}&format=md`);
    if (!res.success) throw new Error(res.detail || `HTTP ${res.status}`);
    const data = res.data;
    const blob = new Blob([data.content || ''], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `会话-${App.sessionId}.md`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    if (App.appendMessage) App.appendMessage(`导出失败: ${e.message}`, 'error');
  }
};

/* 渲染侧边栏会话历史 (别名,供外部调用) */
App._renderSidebarHistory = function () {
  App.Session.renderCards();
};

/* 初始化 */
document.addEventListener('DOMContentLoaded', () => {
  // 加载侧边栏折叠状态
  if (App.Session) App.Session.loadCollapse();
  // 新建会话按钮
  if (App.els.newSessionBtn) {
    App.els.newSessionBtn.addEventListener('click', App.Session.newSession);
  }
});
