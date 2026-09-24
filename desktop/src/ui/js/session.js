/* 会话模块:列表加载 / 搜索 / 切换 / 新建 / 折叠 */
App.Session = {};

/* ═══════════════════════════════════════════════════════════════════════
   多会话并行
   ───────────────────────────────────────────────────────────────────────
   旧行为:切换会话时 `App.Chat.stop()` + `App.abortController.abort()`。
   stop() 会 POST /v1/chat/cancel,后端据此真正 cancel 掉后台 agent ——
   用户看到的就是「切走了,上一个会话就停了」。

   现状:后端本来就支持并行(_RUNNING_TASKS 按 session_id 存各自的 asyncio.Task,
   且 SSE 断连也不会取消 agent)。所以只要前端不再主动掐就行。

   难点在于渲染:多个任务同时往同一个 `App.els.messages` 写会串台。
   解决办法是给每个会话一个离屏容器(pane):
     - 切走时把该会话的消息节点整批移进 pane(live 容器清空);
     - 后台会话的流式输出继续写进它自己的 pane,完全不与前台交错;
     - 切回来时把 pane 的子节点整批搬回 live,内容连续无缺失。
   ═══════════════════════════════════════════════════════════════════════ */

/* sid -> { streaming, abort, pane, handles } */
App.Session._runs = new Map();

App.Session.runOf = function (sid) {
  let r = App.Session._runs.get(sid);
  if (!r) {
    r = { streaming: false, abort: null, pane: null, handles: null };
    App.Session._runs.set(sid, r);
  }
  return r;
};

/* 会话是否在后台继续生成(供侧边栏打标记) */
App.Session.isRunning = function (sid) {
  const r = App.Session._runs.get(sid);
  return !!(r && r.streaming);
};

App.Session.runningIds = function () {
  const out = [];
  App.Session._runs.forEach((r, sid) => { if (r.streaming) out.push(sid); });
  return out;
};

/* 本次写入应落到哪个容器:后台会话写自己的 pane,前台写 live */
App.Session.paneOf = function (sid) {
  const live = App.els && App.els.messages;
  if (!sid) return live;
  if (sid !== App.sessionId) {
    const r = App.Session._runs.get(sid);
    if (r && r.pane) return r.pane;
  }
  return live;
};

/* 把一个会话转入后台:消息区搬进离屏容器,流式状态寄存起来(不取消、不 abort) */
App.Session.park = function (sid) {
  if (!sid) return;
  const r = App.Session.runOf(sid);
  const live = App.els && App.els.messages;
  r.streaming = !!App.streaming;

  if (live) {
    // 只要有内容就搬走(哪怕不在流式):切回来时统一由 unpark 搬回,
    // 避免"重新加载历史"导致正在进行的那一轮被重复渲染
    const store = r.pane || document.createElement('div');
    while (live.firstChild) store.appendChild(live.firstChild);
    r.pane = store;
  }
  // abort 控制器按会话寄存:否则新会话发起请求时会去 abort 掉别人的流
  if (App.abortController) {
    r.abort = App.abortController;
    App.abortController = null;
  }
  // 流式渲染句柄同理:它们指向已搬进 pane 的节点,必须随会话一起寄存,
  // 后台会话继续用它写 pane;前台新会话会拿到全新句柄。
  r.handles = {
    _currentBotMsg: App._currentBotMsg,
    _thinkingWrap: App._thinkingWrap,
    _answerWrap: App._answerWrap,
    _mdBuf: App._mdBuf,
    _planCard: App._planCard,
    _currentGroup: App._currentGroup,
  };
  App._currentBotMsg = null;
  App._thinkingWrap = null;
  App._answerWrap = null;
  App._mdBuf = '';
  App._planCard = null;
  App._currentGroup = null;
  App.streaming = false;
};

/* 切回某会话:把内容搬回 live,恢复它自己的流式状态 */
App.Session.unpark = function (sid) {
  if (!sid) return false;
  const r = App.Session._runs.get(sid);
  if (!r) return false;
  const live = App.els && App.els.messages;
  // 是否有该会话转入后台时的现场内容
  const restored = !!(r.pane && r.pane.firstChild);
  if (r.pane && live) {
    while (r.pane.firstChild) live.appendChild(r.pane.firstChild);
  }
  if (r.pane) r.pane = null;
  if (r.abort) {
    App.abortController = r.abort;
    r.abort = null;
  }
  if (r.handles) {
    App._currentBotMsg = r.handles._currentBotMsg;
    App._thinkingWrap = r.handles._thinkingWrap;
    App._answerWrap = r.handles._answerWrap;
    App._mdBuf = r.handles._mdBuf || '';
    App._planCard = r.handles._planCard;
    App._currentGroup = r.handles._currentGroup;
    r.handles = null;
  }
  if (r.streaming) {
    App.streaming = true;
    document.body.classList.add('streaming');
  }
  if (!r.streaming) App.Session._runs.delete(sid);
  return restored;
};

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
  const d = new Date(App.toMs ? App.toMs(ts) : ts * 1000);
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
      historyEl.innerHTML = '<div class="session-empty"><div class="se-title">未找到匹配内容</div></div>';
      return;
    }
    const results = res.data.results;
    // 注意后端 /v1/chat/search 返回的是 session_id(不是 id);原代码写
    // r.id → data-id 变成 "undefined",点击搜索结果会切到一个不存在的会话。
    // 同时补上 data-title,保证搜索结果与会话列表的标题口径一致。
    historyEl.innerHTML = results.map(r => `
      <div class="session-item search-hit" data-id="${App.escapeHtml(r.session_id)}"
           data-title="${App.escapeHtml(r.title || '新会话')}">
        <div class="s-title">${App.escapeHtml(r.title || '新会话')} · ${r.snippets?.length || 0} 处命中</div>
      </div>
    `).join('');
    historyEl.querySelectorAll('.session-item').forEach(item => {
      item.addEventListener('click', () => App.Session.select(item.dataset.id));
    });
  } catch (_) {
    historyEl.innerHTML = '<div class="session-error">网络错误</div>';
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

App.Session._renderingList = false;
App.Session.renderList = async function () {
  if (App.Session._renderingList) return [];
  App.Session._renderingList = true;
  const listEl = App.els.sidebarSessionHistory || document.getElementById('sidebarSessionHistory');
  if (!listEl) { App.Session._renderingList = false; return []; }
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
             data-id="${s.id}" data-title="${App.escapeHtml(s.title || '新会话')}">
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
    App.Session._renderingList = false;
    return ids;
  } catch (err) {
    listEl.innerHTML = '<div class="empty-hint">加载失败</div>';
    console.error('[Session] 加载失败:', err);
    App.Session._renderingList = false;
    return [];
  }
};

/* 渲染会话卡片视图(新布局) */
App.Session._rendering = false;
App.Session.renderCards = async function () {
  if (App.Session._rendering) return; // 防止递归
  App.Session._rendering = true;
  const listEl = App.els.sidebarSessionHistory || document.getElementById('sidebarSessionHistory');
  if (!listEl) { App.Session._rendering = false; return; }
  try {
    const res = await App.apiJSON('/v1/chat/sessions');
    if (!res.success) {
      listEl.innerHTML = '<div class="session-error">加载失败</div>';
      return;
    }
    const sessions = res.data?.sessions || [];
    const pins = App.Session.loadPins();
    const query = (document.getElementById('sessionSearch')?.value || '').trim().toLowerCase();
    const filtered = sessions.filter(s =>
      !query || (s.title || '').toLowerCase().includes(query)
    );
    if (!filtered.length) {
      listEl.innerHTML = '<div class="session-empty">'
        + '<div class="se-icon">💬</div>'
        + '<div class="se-title">暂无会话</div>'
        + '<div class="se-hint">点击"新建对话"开始</div>'
        + '</div>';
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
      const isRunning = App.Session.isRunning(s.id);
      const timeStr = App.Session.formatTime(s.updated_at);
      // 后台仍在生成的会话加个呼吸点,让用户知道切走之后任务没停
      const dot = isRunning ? '<span class="s-running-dot" title="后台生成中"></span>' : '';
      return `
        <div class="session-item ${isActive ? 'active' : ''} ${isPinned ? 'pinned' : ''} ${isRunning ? 'running' : ''}"
             data-id="${s.id}" data-title="${App.escapeHtml(s.title || '新会话')}">
          <div class="s-title">${dot}${App.escapeHtml(s.title || '新会话')}</div>
          <div class="s-meta">
            <span class="s-time">${timeStr}</span>
            <span class="s-count">${s.messages || 0} 条</span>
            <span class="s-actions">
              <span class="s-pin ${isPinned ? 'active' : ''}" title="${isPinned ? '取消置顶' : '置顶会话'}" role="button">📌</span>
              <span class="s-edit" title="重命名" role="button">✏️</span>
              <span class="s-del" title="删除会话" role="button">🗑</span>
            </span>
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
      item.querySelector('.s-edit')?.addEventListener('click', (e) => {
        e.stopPropagation();
        App.Session.enterRename(sorted.find(s => s.id === item.dataset.id), item);
      });
      item.querySelector('.s-del')?.addEventListener('click', (e) => {
        e.stopPropagation();
        const sess = sorted.find(s => s.id === item.dataset.id);
        App.Session.remove(item.dataset.id, (sess && sess.title) || '');
      });
      item.querySelector('.s-title')?.addEventListener('dblclick', (e) => {
        e.stopPropagation();
        App.Session.enterRename(sorted.find(s => s.id === item.dataset.id), item);
      });
    });
    // 记录本次列表缓存:供 refreshIfNeeded 判断"当前会话是否已在列表里"
    // (原本只有遗留的 renderList 会写 _listCards,renderCards 不写 → 缓存永远是 null)
    App.Session._listCache = { ids: sorted.map(s => s.id), ts: Date.now() };
  } catch (err) {
    listEl.innerHTML = '<div class="session-error">加载失败</div>';
    console.error('[Session] renderCards failed:', err);
  }
  App.Session._rendering = false;
};

/* 强制刷新会话列表(清缓存 + 重新拉取渲染)。
   切换会话 / 新建会话 / 删除会话后必须调用,否则侧边栏的"当前会话"高亮与
   列表内容都是旧的(表现为:切了会话但侧边栏高亮不变、新会话不出现在列表)。 */
App.Session.refreshList = async function () {
  if (!App.Session.renderCards) return;
  App.Session._listCache = { ids: null, ts: 0 };
  await App.Session.renderCards();
};

/* 按需刷新:仅当"当前会话不在列表缓存里"时才真正拉取。
   典型场景:新建会话在发出第一条消息前后端没有记录(列表里没有它),
   消息落库后需要补进去;而普通会话继续聊天不该每轮都多打一次列表请求。 */
App.Session.refreshIfNeeded = async function () {
  const ids = App.Session._listCache && App.Session._listCache.ids;
  if (!ids || !ids.length) {
    await App.Session.refreshList();
    return;
  }
  if (App.sessionId && ids.indexOf(App.sessionId) === -1) {
    await App.Session.refreshList();
  }
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

  // IME 输入法状态追踪:比 e.isComposing 更可靠
  let composing = false;
  input.addEventListener('compositionstart', () => { composing = true; });
  input.addEventListener('compositionend', () => { composing = false; });
  input.addEventListener('keydown', (e) => {
    if (composing || e.isComposing || e.keyCode === 229) return;
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
App.Session.remove = async function (id, displayName) {
  // 确认框展示会话标题而非原始 id(UUID), 否则用户无法辨认要删的是哪个会话
  const label = displayName || id;
  const ok = await (App.confirm
    ? App.confirm(`确定删除会话「${label}」的全部记录吗?此操作不可恢复。`, { danger: true, okText: '删除' })
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
  // 多会话并行:把当前会话转入后台(它的后台任务继续跑),不再 App.Chat.stop()。
  // 必须在清空消息区**之前** park,否则已渲染的内容会被清掉还没进 pane。
  const prevSid = App.sessionId;
  if (prevSid) App.Session.park(prevSid);
  App.Chat.runSid = '';
  if (App._currentGroup) {
    App._currentGroup.wrap.remove();
    App._currentGroup = null;
  }
  App.sessionId = 'desktop-' + Date.now().toString(36);
  App.Session.saveSessionId(App.sessionId);
  // 【修复】新建会话同样要重置项目:否则会沿用上一个会话的项目(project_path 指错目录)。
  // 新会话无绑定时继承 default_project,并落到本会话。
  if (App.Project && App.Project.switchToNewSession) App.Project.switchToNewSession(App.sessionId);
  App.els.sessionLabel.textContent = '新会话';
  App.els.messages.innerHTML = '';
  // 显示聊天视图，隐藏会话列表
  // 刷新侧边栏:同步"当前会话"高亮(新会话此刻后端还没记录,需等第一条消息
  // 落库后由 refreshIfNeeded 补进列表,见 chat.js 流结束的 finally)
  if (App.Session.refreshList) await App.Session.refreshList();
  App.els.input.focus();
  if (App.refreshWelcomePage) App.refreshWelcomePage();
  if (App.Attach) App.Attach.reset();
  if (App.CtxMeter) App.CtxMeter.refresh();
};

/* 切换会话 */
App.Session.select = async function (id) {
  // 同一会话且消息区已有内容,跳过重复加载
  if (id === App.sessionId && App.els.messages && App.els.messages.querySelector('.msg')) {
    return;
  }
  // 多会话并行:切走时把上一个会话转入后台(保留它的流式任务继续跑),
  // 不再 App.Chat.stop() —— stop 会 POST /v1/chat/cancel 让后端真的取消 agent。
  const prevSid = App.sessionId;
  if (prevSid && id !== prevSid) App.Session.park(prevSid);
  // 【修复】切换会话必须同步全局 App.sessionId:否则后续发送 / 渲染 / 标题仍指向旧会话,
  // 造成"点 A 看 B 的内容、发的消息却存到 A"的记忆错乱(多会话并行重构时误删了这行)。
  App.sessionId = id;
  App.Session.saveSessionId(id);
  // 【修复】切换会话必须同步项目:项目是 per-session 绑定(project.js 的 session_projects),
  // 但此前 session.js 从未调用过 App.Project.switchToSession,导致 App.currentProject 一直
  // 停留在上一个会话 → ①项目选择器不随会话切换 ②发出的 project_path 是上个会话的目录,
  // 后端 agent 在错误项目里执行(表现:"识别不出这个会话的项目")。
  if (App.Project && App.Project.switchToSession) App.Project.switchToSession(id);

  const msgs = App.els.messages;
  msgs.innerHTML = '';

  // 该会话此前被转入后台 → 现场内容在它的 pane 里,整批搬回即可。
  // 这里刻意**不重新拉 history**:这一轮的 assistant 消息要等流结束才落库,
  // 此刻拉历史会缺掉正在进行的那一轮,并把 pane 里已有的连续内容冲掉。
  if (App.Session.unpark(id)) {
    // 该会话自己的流还在跑 → 恢复流式归属,后续增量正常渲染到前台
    App.Chat.runSid = id;
    App.scrollMessagesToBottom(true);
    if (App.refreshWelcomePage) App.refreshWelcomePage();
    if (App.Session.refreshList) await App.Session.refreshList();
    const back = document.querySelector(`.sidebar-session-history .session-item[data-id="${CSS.escape(id)}"]`);
    App.els.sessionLabel.textContent = (back && back.dataset.title) || '新会话';
    if (App.Attach) App.Attach.reset();
    if (App.CtxMeter) App.CtxMeter.refresh();
    App.els.input.focus();
    return;
  }

  // 渲染历史不是流式输出,必须清空 runSid —— 否则 App.Chat.pane() 会认为
  // "本次仍属于那个已转入后台的会话",把历史节点写进它的离屏容器里去。
  App.Chat.runSid = '';

  try {
    const res = await App.apiJSON(`/v1/chat/history?session_id=${encodeURIComponent(id)}`);
    if (!res.success) throw new Error(res.detail || `HTTP ${res.status}`);
    const data = res.data;
    const allMessages = data.messages || [];

    // 滚动分页加载状态
    App._sessionAllMsgs = allMessages;
    App._sessionLoadIdx = allMessages.length; // 从末尾往前加载
    App._sessionLoadStep = 15;
    App._sessionLoadingMore = false;

    // 修复: 不能用 m.kind 兜底——kind 是消息类型标记(值为 'text'),
    // 空正文消息会把字面 'text' 渲染到气泡里。空正文交给空回复占位逻辑处理。
    function msgText(m) {
      const c = m.content;
      if (typeof c === 'string') return c;
      if (c && typeof c === 'object') return String(c.content || c.result || '');
      return '';
    }

    // 库里 timestamp 是"秒级"Unix 时间戳(1789821286.59),直接 new Date(ts)
    // 会得到 1970 年;统一走 App.toMs 做单位归一化。
    function msgTimeText(ts) {
      const ms = App.toMs ? App.toMs(ts) : 0;
      const d = new Date(ms || Date.now());
      return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    }

    // 按"轮"切分历史:一轮 = 从一条 user 消息开始,到下一 user 消息之前结束。
    // 关键原因:库里工具消息(role='tool')通常排在**本轮 assistant 消息之前**
    // (生成过程中先落库工具调用,最后才写入 assistant)。若按固定条数切片,
    // 工具行会在其所属 assistant 卡片存在之前就被渲染 → 落单到顶层,
    // 退化成无序号圈/无缩进/不可折叠的扁平节点(观感:"历史里工具行样式不对")。
    // 因此分页单位改为"整轮",并在渲染时把工具行缓冲到本轮 assistant 卡片。
    const roundStarts = [];
    for (let i = 0; i < allMessages.length; i++) {
      if (allMessages[i].role === 'user') roundStarts.push(i);
    }
    if (!roundStarts.length || roundStarts[0] !== 0) roundStarts.unshift(0);

    // 取 <= idx 的最后一个轮起点
    function roundStart(idx) {
      let s = 0;
      for (const r of roundStarts) { if (r <= idx) s = r; else break; }
      return s;
    }
    // 取 < idx 的上一个轮起点(用于向上滚动加载上一轮)
    function prevRoundStart(idx) {
      let s = 0;
      for (const r of roundStarts) { if (r < idx) s = r; else break; }
      return s;
    }

    // 渲染一组消息为 DOM 元素数组;baseIdx = 该批消息在 allMessages 中的起始下标,
    // 用于给每个顶层节点打 data-idx,支撑滚动时的 DOM 上限裁剪(保留最新、推进加载游标)。
    function buildMsgElements(msgs, baseIdx) {
      const frags = [];
      let lastPlanContent = '';
      let currentStepsFlow = null; // 当前 assistant 卡片的步骤容器,工具步骤嵌套于此
      const pendingTools = []; // 尚未归属的工具步骤(其 assistant 卡片还没渲染到)
      let _curAbsIdx = 0; // 当前正在处理的消息的绝对下标(供工具宿主卡打标)
      // 给顶层消息节点打 data-idx 并压入 frags
      const pushIdx = (el) => { el.dataset.idx = String(_curAbsIdx); frags.push(el); return el; };

      // 把缓冲的工具步骤灌进目标 steps-flow;target 为空时建独立 bot 卡片承载,
      // 保证 step-row 的序号圈/缩进/可折叠样式与实时路径一致。
      function flushPendingTools(target) {
        if (!pendingTools.length) return;
        let flow = target;
        if (!flow) {
          const host = document.createElement('div');
          host.className = 'msg bot no-animation';
          const av = document.createElement('div');
          av.className = 'avatar';
          av.textContent = '🤖';
          const mc = document.createElement('div');
          mc.className = 'msg-content';
          flow = document.createElement('div');
          flow.className = 'steps-flow';
          mc.appendChild(flow);
          host.appendChild(av);
          host.appendChild(mc);
          pushIdx(host);
        }
        for (const te of pendingTools) {
          App.appendToolEvent(te.name || '工具', te.arguments, te.result || '', flow);
        }
        pendingTools.length = 0;
      }
      for (let i = 0; i < msgs.length; i++) {
        const m = msgs[i];
        _curAbsIdx = (baseIdx || 0) + i;
        if (m.role === 'user') {
          // 上一轮遗留的工具步骤(该轮没有 assistant 承接,如被中断)先落盘,
          // 再开始新的一轮
          flushPendingTools(null);
          currentStepsFlow = null; // 新的一轮对话:步骤容器归零,避免挂到上一轮 assistant 卡片
          // 与实时路径(message-renderer.appendUserMessage)对齐:
          // 多行文本 \n 转 <br>,避免"看着折叠成一行"的观感错位。
          const div = document.createElement('div');
          div.className = 'msg user no-animation';
          const avatar = document.createElement('div');
          avatar.className = 'avatar';
          avatar.textContent = '👤';
          const content = document.createElement('div');
          content.className = 'msg-content';
          const bubble = document.createElement('div');
          bubble.className = 'answer-doc';
          const rawText = msgText(m);
          bubble.innerHTML = App.escapeHtml ? App.escapeHtml(rawText).replace(/\n/g, '<br>') : rawText;
          content.appendChild(bubble);
          const footer = document.createElement('div');
          footer.className = 'msg-footer';
          const act = document.createElement('div');
          act.className = 'msg-actions';
          const timeEl = document.createElement('div');
          timeEl.className = 'msg-time';
          timeEl.textContent = msgTimeText(m.timestamp);
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
          footer.appendChild(act);
          footer.appendChild(timeEl);
          content.appendChild(footer);
          div.appendChild(avatar);
          div.appendChild(content);
          pushIdx(div);
          continue;
        }
        if (m.role === 'assistant') {
          // 与实时路径(message-renderer.createBotMessage)对齐:同样带
          // msg-header/process-rail/step-counter/steps-flow,使历史消息与流式消息样式一致。
          const div = document.createElement('div');
          div.className = 'msg bot no-animation';
          const avatar = document.createElement('div');
          avatar.className = 'avatar';
          avatar.textContent = '🤖';
          const content = document.createElement('div');
          content.className = 'msg-content';

          // 状态栏(实时路径在答案生成前显示,历史路径渲染为"已完成"终态)
          const header = document.createElement('div');
          header.className = 'msg-header';
          const rail = document.createElement('div');
          rail.className = 'process-rail';
          rail.innerHTML = '<span class="rail-summary">✅ 已完成</span>';
          header.appendChild(rail);
          const headTime = document.createElement('span');
          headTime.className = 'msg-time';
          headTime.textContent = msgTimeText(m.timestamp);
          header.appendChild(headTime);
          content.appendChild(header);

          // 思考区域。存量数据兜底:agnes 等模型的思考曾以 <思考要点>/<thinking> 形式
          // 混在 content 里(后端旧版未提取),这里提取进思考区,正文不再出现原始标签
          let thinkingText = m.meta?.thinking || m.meta?.thinking_content || '';
          let raw = msgText(m).replace(
            /<(?:思考[\u4e00-\u9fa5]{0,4}|thinking)>([\s\S]*?)<\/(?:思考[\u4e00-\u9fa5]{0,4}|thinking)>/g,
            (_, inner) => { thinkingText += (thinkingText ? '\n\n' : '') + inner.trim(); return ''; }
          ).trim();
          if (thinkingText) {
            const thinkingDiv = document.createElement('div');
            thinkingDiv.className = 'thinking-section';
            const tLabel = document.createElement('div');
            tLabel.className = 'thinking-label';
            tLabel.textContent = '💭 思考过程';
            const tBody = document.createElement('div');
            tBody.className = 'thinking-body';
            // 思考内容含 Markdown(**加粗**/###/列表),必须走渲染器,
            // textContent 会把语法符号原样裸露出来
            if (App.renderMarkdown) {
              tBody.classList.add('md');
              try { tBody.innerHTML = App.renderMarkdown(thinkingText); }
              catch (e) { tBody.textContent = thinkingText; }
            } else {
              tBody.textContent = thinkingText;
            }
            thinkingDiv.appendChild(tLabel);
            thinkingDiv.appendChild(tBody);
            tLabel.addEventListener('click', () => thinkingDiv.classList.toggle('open'));
            content.appendChild(thinkingDiv);
          }

          // 与实时路径(message-renderer.createBotMessage)对齐:assistant 卡片内建 steps-flow,
          // 工具步骤(role==='tool')将嵌套其中,使历史回放与实时输出结构一致。
          const stepsFlow = document.createElement('div');
          stepsFlow.className = 'steps-flow';
          content.appendChild(stepsFlow);
          currentStepsFlow = stepsFlow;
          // 库里工具消息常排在 assistant 之前,此时它们暂存在 pendingTools 里,
          // 现在卡片就绪,灌进 steps-flow(与实时渲染的结构完全一致)
          flushPendingTools(stepsFlow);

          // 答案区域
          const bubble = document.createElement('div');
          bubble.className = 'answer-doc';
          if (raw && App.renderMarkdown) {
            try { bubble.innerHTML = App.renderMarkdown(raw); }
            catch (e) {
              console.error('[session] renderMarkdown ERROR:', e);
              bubble.innerHTML = '<p style="white-space:pre-wrap;">' + App.escapeHtml(raw) + '</p>';
            }
          } else if (raw) {
            bubble.innerHTML = '<p style="white-space:pre-wrap;">' + App.escapeHtml(raw) + '</p>';
          } else if (!thinkingText) {
            // 空回复占位:流被停止/异常中断时会留下 content 为空的消息,
            // 渲染成完全空白的气泡,看起来像"内容怎么会为空"
            bubble.innerHTML = '<p style="color:var(--text-tertiary);font-style:italic;">(此轮无回复内容——可能已被停止或中断)</p>';
          }
          content.appendChild(bubble);

          const footer = document.createElement('div');
          footer.className = 'msg-footer';
          const act = document.createElement('div');
          act.className = 'msg-actions';
          const timeEl = document.createElement('div');
          timeEl.className = 'msg-time';
          timeEl.textContent = msgTimeText(m.timestamp);
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
          const speakBtn = document.createElement('button');
          speakBtn.className = 'msg-action';
          speakBtn.title = '朗读该回复';
          speakBtn.textContent = '🔊';
          speakBtn.addEventListener('click', () => {
            if (App.isSpeaking && App.isSpeaking()) {
              App.stopSpeak();
              speakBtn.textContent = '🔊'; speakBtn.title = '朗读该回复';
              return;
            }
            const txt = (bubble.textContent || '').trim();
            if (!txt) return;
            speakBtn.textContent = '⏹'; speakBtn.title = '停止朗读';
            App.speak?.(txt);
            const iv = setInterval(() => {
              if (!(App.isSpeaking && App.isSpeaking())) {
                clearInterval(iv);
                speakBtn.textContent = '🔊'; speakBtn.title = '朗读该回复';
              }
            }, 200);
          });
          act.appendChild(speakBtn);
          footer.appendChild(act);
          footer.appendChild(timeEl);
          content.appendChild(footer);
          div.appendChild(avatar);
          div.appendChild(content);
          pushIdx(div);
          continue;
        }
        if (m.role === 'error') {
          const div = document.createElement('div');
          div.className = 'msg error';
          div.textContent = msgText(m);
          pushIdx(div);
          continue;
        }
        if (m.role === 'plan' && App.appendPlan) {
          try {
            const plan = JSON.parse(msgText(m));
            if (JSON.stringify(plan) !== lastPlanContent) {
              lastPlanContent = JSON.stringify(plan);
              const p = App.appendPlan({ action: 'create', plan }, frags);
              if (p) pushIdx(p);
            }
          } catch (_) { /* 忽略 */ }
          continue;
        }
        if (m.role === 'tool' && App.appendToolEvent) {
          let te = null;
          try { te = JSON.parse(msgText(m)); } catch (_) { te = null; }
          if (te) {
            const isImage = te.name === 'generate_image';
            if (isImage) {
              // 图片生成沿用专属卡片(img-gen-bubble),作为兄弟节点
              App.appendToolEvent(te.name || '工具', te.arguments, te.result || '', frags);
              const _imgNode = frags[frags.length - 1];
              if (_imgNode) _imgNode.dataset.idx = String(_curAbsIdx);
            } else if (currentStepsFlow) {
              // assistant 卡片已渲染(新数据顺序:assistant 在 tool 之前)→ 直接嵌套
              App.appendToolEvent(te.name || '工具', te.arguments, te.result || '', currentStepsFlow);
            } else {
              // assistant 卡片还没渲染(存量数据顺序:tool 排在 assistant 之前)
              // → 先缓冲,等本轮 assistant 卡片建好后由 flushPendingTools 灌入
              pendingTools.push(te);
            }
          }
          continue;
        }
      }
      // 收尾:最后一轮若没有 assistant 承接(被中断),遗留工具步骤单独建卡,
      // 避免它们被丢成无样式的扁平节点
      flushPendingTools(null);
      return frags;
    }

    // 首次加载:从"最后 15 条所在那一轮"的起点开始(整轮加载,不切断一轮对话)
    const initStart = roundStart(allMessages.length - App._sessionLoadStep);
    const initMsgs = allMessages.slice(initStart);
    App._sessionLoadIdx = initStart;
    const frag = document.createDocumentFragment();
    for (const el of buildMsgElements(initMsgs, initStart)) frag.appendChild(el);
    msgs.appendChild(frag);
    App.scrollMessagesToBottom(true);

    // 滚动加载更多:监听 scroll 事件
    const MAX_DOM_MSGS = 100; // DOM 中最多保留的消息数,防止内存溢出
    function onScroll() {
      if (App._sessionLoadingMore || App._sessionLoadIdx <= 0) return;
      // 距顶部不足 200px 时触发加载
      if (msgs.scrollTop > 200) return;
      App._sessionLoadingMore = true;
      const oldScrollHeight = msgs.scrollHeight;
      const prevStart = App._sessionLoadIdx;
      // 向上加载"上一整轮"(而非固定条数),保证工具步骤与其 assistant 同批渲染
      const newStart = prevRoundStart(prevStart);
      const olderMsgs = allMessages.slice(newStart, prevStart);
      App._sessionLoadIdx = newStart;
      const moreFrag = document.createDocumentFragment();
      for (const el of buildMsgElements(olderMsgs, newStart)) moreFrag.appendChild(el);
      msgs.insertBefore(moreFrag, msgs.firstChild);
      // 保持滚动位置不跳动
      msgs.scrollTop = msgs.scrollHeight - oldScrollHeight + msgs.scrollTop;
      // DOM 节点上限:超过限制时移除顶部(最旧)的消息节点,始终保留底部(最新)可见;
      // 并把加载游标推进到剩余最旧节点的真实下标,避免向上滚动时重复加载已被裁掉的消息。
      const msgNodes = Array.from(msgs.querySelectorAll('.msg')); // 静态快照, 移除后不会失效
      const excess = msgNodes.length - MAX_DOM_MSGS;
      if (excess > 0) {
        for (let k = 0; k < excess; k++) msgNodes[k].remove();
        const topNode = msgNodes[excess];
        if (topNode && topNode.dataset && topNode.dataset.idx !== undefined) {
          const v = parseInt(topNode.dataset.idx, 10);
          if (!Number.isNaN(v)) App._sessionLoadIdx = v;
        }
      }
      App._sessionLoadingMore = false;
    }
    // 先移除旧监听器再绑定
    if (App._sessionScrollHandler) msgs.removeEventListener('scroll', App._sessionScrollHandler);
    App._sessionScrollHandler = onScroll;
    msgs.addEventListener('scroll', onScroll);

    if (App.refreshWelcomePage) App.refreshWelcomePage();

    // 会话标题:一律以**后端权威标题**为准(data.title 由 /chat/history 返回,
    // 与会话列表共用同一套「自定义名 > 首条用户消息」解析)。
    // 旧实现先从侧边栏 DOM 的 data-title 取、取不到就退化成首条用户消息截断,
    // 导致用户 rename 之后「列表是新名字、标题栏是旧名字」(典型场景:从搜索结果
    // 点进去时,搜索结果项压根没有 data-title 属性)。
    App.els.sessionLabel.textContent = data.title || '新会话';
  } catch (e) {
    if (App.appendMessage) App.appendMessage(`加载会话历史失败: ${e.message}`, 'error');
  }

  // 切换后刷新侧边栏:更新 active 高亮(select 只改了 App.sessionId,
  // 不重渲染的话侧边栏仍高亮在旧会话上,看上去像"没切过去")
  if (App.Session.refreshList) await App.Session.refreshList();

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
  // 会话删了就一并解绑它的项目,避免 session_projects 里留下孤儿绑定
  if (App.Project && App.Project.clearForSession) App.Project.clearForSession(id);
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

/* 注意: App._renderSidebarHistory 由 renderer.js 定义,
   不可在此处覆盖,否则会导致 renderCards → _renderSidebarHistory → renderCards 无限递归 */

/* 初始化 */
document.addEventListener('DOMContentLoaded', () => {
  // 加载侧边栏折叠状态
  if (App.Session) App.Session.loadCollapse();
  // 新建会话按钮
  if (App.els.newSessionBtn) {
    App.els.newSessionBtn.addEventListener('click', App.Session.newSession);
  }
});
