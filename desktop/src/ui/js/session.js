/* 会话模块:列表加载 / 搜索 / 切换 / 新建 / 折叠 */
App.Session = {};

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
  App.Session.renderList();
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

App.Session.renderList = async function () {
  const listEl = App.els.sessionList;
  try {
    const res = await App.api('/v1/chat/sessions');
    if (!res.success || !Array.isArray(res.data)) {
      listEl.innerHTML = '<div class="empty-hint">暂无会话</div>';
      return;
    }
    const sessions = res.data;
    const pins = App.Session.loadPins();
    const query = (App.els.sessionSearch.value || '').trim().toLowerCase();
    const filtered = sessions.filter(s => 
      !query || (s.title || '').toLowerCase().includes(query)
    );
    if (!filtered.length) {
      listEl.innerHTML = '<div class="empty-hint">暂无会话</div>';
      return;
    }
    listEl.innerHTML = filtered.map(s => {
      const isPinned = pins.has(s.id);
      const isActive = s.id === App.sessionId;
      return `
        <div class="session-item ${isActive ? 'active' : ''} ${isPinned ? 'pinned' : ''}" 
             data-id="${s.id}" data-title="${App.escapeHtml(s.title || '未命名')}">
          <span class="s-title">${App.escapeHtml(s.title || '未命名')}</span>
          <span class="s-pin ${isPinned ? 'active' : ''}">📌</span>
        </div>
      `;
    }).join('');
    listEl.querySelectorAll('.session-item').forEach(el => {
      el.addEventListener('click', () => App.Session.select(el.dataset.id));
      el.querySelector('.s-pin')?.addEventListener('click', (e) => {
        e.stopPropagation();
        App.Session.togglePin(el.dataset.id);
      });
    });
  } catch (err) {
    listEl.innerHTML = '<div class="empty-hint">加载失败</div>';
    console.error('[Session] 加载失败:', err);
  }
};

App.Session.search = function () {
  App.Session.renderList();
};

App.Session.create = async function () {
  try {
    const res = await App.api('/v1/chat/sessions', { method: 'POST' });
    if (res.success && res.data?.id) {
      App.sessionId = res.data.id;
      if (App.Storage) {
        App.Storage.set('last_session', res.data.id);
      }
      App.Session.renderList();
      App.Session.clearMessages();
      App.els.sessionLabel.textContent = '新会话';
      App.els.input.focus();
    }
  } catch (err) {
    console.error('[Session] 创建失败:', err);
  }
};

App.Session.select = async function (id) {
  if (id === App.sessionId) return;
  App.sessionId = id;
  if (App.Storage) {
    App.Storage.set('last_session', id);
  }
  try {
    const res = await App.api(`/v1/chat/sessions/${id}/messages`);
    if (res.success && Array.isArray(res.data)) {
      App.els.messages.innerHTML = '';
      res.data.forEach(msg => {
        if (msg.role === 'user') {
          App.appendMessage(msg.role, msg.content, false);
        } else if (msg.role === 'assistant') {
          App.appendMessage(msg.role, msg.content, false);
        }
      });
      App.els.messages.scrollTop = App.els.messages.scrollHeight;
      const title = res.data.find(m => m.role === 'assistant')?.title || '未命名';
      App.els.sessionLabel.textContent = title;
      App.Session.renderList();
    }
    App.els.input.focus();
  } catch (err) {
    console.error('[Session] 加载失败:', err);
  }
};

App.Session.clearMessages = function () {
  if (App.els.messages) {
    App.els.messages.innerHTML = '';
  }
  App.els.sessionLabel.textContent = '新会话';
  App.els.input.focus();
};

App.Session.deleteSession = async function (id) {
  if (id === App.sessionId) {
    console.warn('[Session] 不能删除当前会话');
    return;
  }
  try {
    await App.api(`/v1/chat/sessions/${id}`, { method: 'DELETE' });
    App.Session.renderList();
  } catch (err) {
    console.error('[Session] 删除失败:', err);
  }
};

/* 初始化 */
document.addEventListener('DOMContentLoaded', () => {
  if (App.els.sessionList) {
    App.els.sessionList.addEventListener('click', (e) => {
      const item = e.target.closest('.session-item');
      if (item) App.Session.select(item.dataset.id);
    });
  }
  if (App.els.sessionSearch) {
    App.els.sessionSearch.addEventListener('input', App.Session.search);
  }
});
