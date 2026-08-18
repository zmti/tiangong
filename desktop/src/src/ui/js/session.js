/* 会话模块:列表加载 / 搜索 / 切换 / 新建 / 折叠 */
App.Session = {};

/* 置顶会话(localStorage 持久化) */
App.Session.loadPins = function () {
  try {
    const raw = localStorage.getItem('agent_pinned_sessions');
    const arr = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(arr) ? arr : []);
  } catch (_) { return new Set(); }
};

App.Session.savePins = function (set) {
  try {
    localStorage.setItem('agent_pinned_sessions', JSON.stringify([...set]));
  } catch (_) { /* 忽略 */ }
};

App.Session.togglePin = async function (id) {
  const pins = App.Session.loadPins();
  if (pins.has(id)) pins.delete(id); else pins.add(id);
  App.Session.savePins(pins);
  App.Session.renderList();
};

/* 最近使用的会话(localStorage 持久化),重启应用后自动恢复 */
App.Session.lastSessionId = function () {
  try { return localStorage.getItem('agent_session') || ''; } catch (_) { return ''; }
};

App.Session.saveSessionId = function (id) {
  try { localStorage.setItem('agent_session', id); } catch (_) { /* 忽略 */ }
};

/* 需要恢复的会话仍存在时才切换,避免重建一个空会话 */
App.Session.restoreLast = async function (ids) {
  const last = App.Session.lastSessionId();
  if (last && Array.isArray(ids) && ids.length && ids.includes(last)) {
    await App.Session.select(last);
  }
};

App.Session.renderList = async function () {
  const listEl = App.els.sessionList;
  try {
    const res = await App.api('/v1/chat/sessions');
    if (!res.ok) {
      listEl.innerHTML = '<div class="session-empty">会话加载失败</div>';
      return [];
    }
    const data = await res.json();
    const sessions = data.sessions || [];
    listEl.innerHTML = '';
    if (!sessions.length) {
      listEl.innerHTML = '<div class="session-empty">暂无会话,点击 ＋ 新建</div>';
      return [];
    }
    const pinned = App.Session.loadPins();
    const sorted = [...sessions].sort((a, b) => {
      const ap = pinned.has(a.id) ? 0 : 1;
      const bp = pinned.has(b.id) ? 0 : 1;
      if (ap !== bp) return ap - bp;
      return (b.updated_at || 0) - (a.updated_at || 0);
    });
    const query = (App.els.sessionSearch.value || '').trim().toLowerCase();
    let count = 0;
    for (const s of sorted) {
      const title = (s.title || '新会话').trim();
      if (query && !title.toLowerCase().includes(query)) continue;
      count++;
      listEl.appendChild(App.Session.buildItem(s, title));
    }
    if (!count) {
      listEl.innerHTML = '<div class="session-empty">未找到匹配的会话</div>';
    }
    return sorted.map((s) => s.id);
  } catch (_) {
    listEl.innerHTML = '<div class="session-empty">网络错误</div>';
    return [];
  }
};

App.Session.buildItem = function (s, title) {
  const btn = document.createElement('button');
  btn.className = 'session-item';
  btn.dataset.id = s.id;
  btn.dataset.title = title;
  if (s.id === App.sessionId) btn.classList.add('active');
  const isPinned = App.Session.loadPins().has(s.id);
  if (isPinned) btn.classList.add('pinned');

  const t = document.createElement('div');
  t.className = 's-title';
  t.textContent = title;
  t.title = '双击重命名';

  const meta = document.createElement('div');
  meta.className = 's-meta';
  const time = document.createElement('span');
  time.textContent = App.Session.formatTime(s.updated_at);
  const right = document.createElement('span');
  right.style.cssText = 'display:flex;align-items:center;gap:6px;';
  const pin = document.createElement('span');
  pin.className = 's-pin';
  pin.title = isPinned ? '取消置顶' : '置顶会话';
  pin.textContent = '📌';
  pin.addEventListener('click', (e) => {
    e.stopPropagation();
    App.Session.togglePin(s.id);
  });
  const count = document.createElement('span');
  count.textContent = `${s.messages || 0} 条`;
  const edit = document.createElement('span');
  edit.className = 's-edit';
  edit.title = '重命名会话';
  edit.textContent = '✏️';
  edit.addEventListener('click', (e) => {
    e.stopPropagation();
    App.Session.enterRename(s, btn);
  });
  const del = document.createElement('span');
  del.className = 's-del';
  del.title = '删除该会话';
  del.textContent = '🗑';
  del.addEventListener('click', (e) => {
    e.stopPropagation();
    App.Session.remove(s.id);
  });
  right.appendChild(pin);
  right.appendChild(count);
  right.appendChild(edit);
  right.appendChild(del);
  meta.appendChild(time);
  meta.appendChild(right);

  t.addEventListener('dblclick', (e) => {
    e.stopPropagation();
    App.Session.enterRename(s, btn);
  });

  btn.appendChild(t);
  btn.appendChild(meta);
  return btn;
};

App.Session.enterRename = function (s, btn) {
  const titleEl = btn.querySelector('.s-title');
  const input = document.createElement('input');
  input.className = 's-edit-input';
  input.value = (s.custom_title || btn.dataset.title || '').trim();
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
    if (save && val && val !== input.dataset.orig) {
      await App.Session.rename(s.id, val);
    } else {
      App.Session.renderList();
    }
  };
  input.dataset.orig = input.value;

  input.addEventListener('keydown', async (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); await finish(true); }
    else if (e.key === 'Escape') { await finish(false); }
  });
  input.addEventListener('blur', () => { finish(true); });
};

App.Session.rename = async function (id, title) {
  try {
    const res = await App.api('/v1/chat/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: id, title }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (id === App.sessionId) App.els.sessionLabel.textContent = title;
  } catch (e) {
    App.toast(`重命名失败: ${e.message}`, 'err');
  }
  App.Session.renderList();
};

App.Session.remove = async function (id) {
  const ok = await App.confirm(`确定删除会话「${id}」的全部记录吗?此操作不可恢复。`, { danger: true, okText: '删除' });
  if (!ok) return;
  try {
    await App.api(`/v1/chat/clear?session_id=${encodeURIComponent(id)}`, { method: 'POST' });
    if (id === App.sessionId) {
      App.Session.newSession();
    } else {
      App.Session.renderList();
    }
  } catch (e) {
    App.toast(`删除失败: ${e.message}`, 'err');
  }
};

/* 清空当前会话的对话内容(保留会话本身) */
App.Session.clearCurrent = async function () {
  if (!App.sessionId) return;
  const ok = await App.confirm('确定清空当前会话的对话内容吗?', { danger: true, okText: '清空' });
  if (!ok) return;
  try {
    const res = await App.api(`/v1/chat/clear?session_id=${encodeURIComponent(App.sessionId)}`, { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    App.els.messages.innerHTML = '';
    App._planCard = null;
    if (App._currentGroup) { App._currentGroup.wrap.remove(); App._currentGroup = null; }
    if (App.Attach) App.Attach.reset();
    if (App.CtxMeter) App.CtxMeter.refresh();
    App.Session.renderList();
    App.toast('当前会话已清空', 'ok');
  } catch (e) {
    App.toast(`清空失败: ${e.message}`, 'err');
  }
};

App.Session.select = async function (id) {
  if (id === App.sessionId) return;
  if (App.streaming) App.Chat.stop(true);
  // 切换会话时关闭当前分组,丢弃未完成的操作记录
  if (App._currentGroup) {
    const g = App._currentGroup;
    g.head.innerHTML = `<span class="rg-pulse">📂</span><span class="rg-label">已切换会话(取消)</span>`;
    g.wrap.classList.add('req-group-done');
    App._currentGroup = null;
  }
  App.sessionId = id;
  App.Session.saveSessionId(id);
  // 切换会话时清空计划卡片引用,避免复用旧会话的 DOM
  App._planCard = null;
  document.querySelectorAll('#sessionList .session-item').forEach((el) => {
    el.classList.toggle('active', el.dataset.id === id);
  });
  const msgs = App.els.messages;
  msgs.innerHTML = '';
  try {
    const res = await App.api(`/v1/chat/history?session_id=${encodeURIComponent(id)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    // 批量构建 HTML 再一次性插入,避免每条消息都触发 DOM append + scroll
    const fragments = [];
    let lastPlanContent = '';
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
        bubble.className = 'bubble';
        bubble.textContent = m.content || '';
        body.appendChild(bubble);
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
        bubble.className = 'bubble';
        bubble.innerHTML = App.renderMarkdown(m.content || '');
        body.appendChild(bubble);
        div.appendChild(avatar);
        div.appendChild(body);
        fragments.push(div);
        continue;
      }
      if (m.role === 'error') {
        const div = document.createElement('div');
        div.className = 'msg error';
        div.textContent = m.content || '';
        fragments.push(div);
        continue;
      }
      if (m.role === 'plan') {
        try {
          const plan = JSON.parse(m.content);
          if (JSON.stringify(plan) !== lastPlanContent) {
            lastPlanContent = JSON.stringify(plan);
            App.appendPlan({ action: 'create', plan });
          }
        } catch (_) { /* 忽略损坏的计划记录 */ }
        continue;
      }
      if (m.role === 'tool') {
        let te = null;
        try { te = JSON.parse(m.content); } catch (_) { te = null; }
        if (te) App.appendToolEvent(te.name || '工具', te.arguments, te.result || '');
        continue;
      }
    }
    // 批量插入,只触发一次重排
    const fragment = document.createDocumentFragment();
    for (const el of fragments) fragment.appendChild(el);
    msgs.appendChild(fragment);
    msgs.scrollTop = msgs.scrollHeight;
    // 确保欢迎页隐藏
    document.body.classList.add('has-messages');
    let title = '会话';
    const item = document.querySelector(`#sessionList .session-item[data-id="${CSS.escape(id)}"]`);
    if (item && item.dataset.title) {
      title = item.dataset.title;
    } else if (data.messages && data.messages.length) {
      const first = data.messages.find((m) => m.role === 'user');
      if (first) title = String(first.content).slice(0, 30);
    }
    App.els.sessionLabel.textContent = title;
  } catch (e) {
    App.appendMessage(`加载会话历史失败: ${e.message}`, 'error');
  }
  if (App.Attach) App.Attach.reset();
  if (App.CtxMeter) App.CtxMeter.refresh();
  App.els.input.focus();
};

App.Session.newSession = async function () {
  if (App.streaming) App.Chat.stop(true);
  // 清除当前分组状态
  if (App._currentGroup) {
    App._currentGroup.wrap.remove();
    App._currentGroup = null;
  }
  App.sessionId = 'desktop-' + Date.now().toString(36);
  App.Session.saveSessionId(App.sessionId);
  App.els.sessionLabel.textContent = '新会话';
  App.els.messages.innerHTML = '';
  document.querySelectorAll('#sessionList .session-item').forEach((el) => {
    el.classList.toggle('active', el.dataset.id === App.sessionId);
  });
  App.els.input.focus();
  App.Session.renderList();
  if (App.Attach) App.Attach.reset();
  if (App.CtxMeter) App.CtxMeter.refresh();
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
    await App.api('/v1/chat/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: App.sessionId, title }),
    });
    // 更新会话列表显示
    const item = document.querySelector(`#sessionList .session-item[data-id="${CSS.escape(App.sessionId)}"]`);
    if (item) { item.dataset.title = title; const t = item.querySelector('.s-title'); if (t) t.textContent = title; }
    App.els.sessionLabel.textContent = title;
  } catch (_) { /* 忽略命名失败,不影响体验 */ }
};

App.Session.toggleCollapse = function () {
  document.body.classList.toggle('session-collapsed');
};

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

App.Session.search = async function () {
  const query = (App.els.sessionSearch.value || '').trim();
  if (!query) {
    App.Session.renderList();
    return;
  }
  // 全文搜索:遍历所有会话内容,返回命中标题与片段
  const listEl = App.els.sessionList;
  try {
    const res = await App.api(`/v1/chat/search?q=${encodeURIComponent(query)}`);
    if (!res.ok) { listEl.innerHTML = '<div class="session-empty">搜索失败</div>'; return; }
    const data = await res.json();
    listEl.innerHTML = '';
    if (!data.results || !data.results.length) {
      listEl.innerHTML = '<div class="session-empty">未找到匹配内容</div>';
      return;
    }
    for (const r of data.results) {
      listEl.appendChild(App.Session.buildSearchItem(r));
    }
  } catch (_) {
    listEl.innerHTML = '<div class="session-empty">网络错误</div>';
  }
};

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

App.Session.exportCurrent = async function () {
  try {
    const res = await App.api(`/v1/chat/export?session_id=${encodeURIComponent(App.sessionId)}&format=md`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
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
    App.appendMessage(`导出失败: ${e.message}`, 'error');
  }
};
