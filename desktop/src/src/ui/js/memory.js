/* 记忆管理模块:列表 / 新增 / 删除 / 清空(长期记忆面板) */
App.Memory = {};

App.Memory.refresh = async function () {
  await Promise.all([App.Memory.renderStats(), App.Memory.renderList()]);
};

App.Memory.renderStats = async function () {
  const el = App.els.memoryStats;
  if (!el) return;
  try {
    const res = await App.api('/v1/memory');
    if (!res.ok) { el.innerHTML = '记忆: 不可用'; return; }
    const d = await res.json();
    const s = d.stats || {};
    el.innerHTML = `🧠 记忆 <b>${s.total || 0}</b> · 手动 <b>${s.manual || 0}</b> · 自动 <b>${s.auto || 0}</b> · 命中 <b>${s.hits || 0}</b>`;
  } catch (_) {
    el.textContent = '记忆: 加载失败';
  }
};

App.Memory.renderList = async function () {
  const listEl = App.els.memoryList;
  if (!listEl) return;
  try {
    const res = await App.api('/v1/memory');
    if (!res.ok) { listEl.innerHTML = '<div class="session-empty">记忆加载失败</div>'; return; }
    const d = await res.json();
    let mems = d.memories || [];
    const kw = (document.getElementById('memorySearch') || {}).value || '';
    const scope = (document.getElementById('memoryScope') || {}).value || 'all';
    if (kw.trim()) {
      const q = kw.trim().toLowerCase();
      mems = mems.filter((m) =>
        String(m.content || '').toLowerCase().includes(q) ||
        (m.keywords || []).some((k) => String(k).toLowerCase().includes(q))
      );
    }
    if (scope !== 'all') mems = mems.filter((m) => (scope === 'session' ? m.scope === 'session' : m.scope !== 'session'));
    listEl.innerHTML = '';
    if (!mems.length) {
      listEl.innerHTML = '<div class="session-empty">' + (kw.trim() ? '未找到匹配的记忆' : '暂无长期记忆,对话结束后 Agent 会自动沉淀') + '</div>';
      return;
    }
    for (const m of mems) listEl.appendChild(App.Memory.buildItem(m));
  } catch (_) {
    listEl.innerHTML = '<div class="session-empty">网络错误</div>';
  }
};

App.Memory.buildItem = function (m) {
  const item = document.createElement('div');
  item.className = 'memory-item';

  const head = document.createElement('div');
  head.className = 'memory-head';
  const scope = document.createElement('span');
  scope.className = 'memory-scope' + (m.scope === 'session' ? ' session' : '');
  scope.textContent = m.scope === 'session' ? '会话级' : '全局';
  head.appendChild(scope);
  const src = document.createElement('span');
  src.className = 'memory-src';
  src.textContent = m.source === 'auto' ? '自动' : '手动';
  head.appendChild(src);
  const time = document.createElement('span');
  time.className = 'memory-time';
  time.textContent = App.Memory.fmtTime(m.updated_at);
  head.appendChild(time);
  if (m.session_id) {
    const sid = document.createElement('span');
    sid.className = 'memory-sid';
    sid.textContent = m.session_id.slice(0, 10);
    sid.title = m.session_id;
    head.appendChild(sid);
  }

  const body = document.createElement('div');
  body.className = 'memory-body';
  body.textContent = m.content || '';
  body.title = m.content || '';

  const foot = document.createElement('div');
  foot.className = 'memory-foot';
  if (m.keywords && m.keywords.length) {
    for (const kw of m.keywords.slice(0, 5)) {
      const k = document.createElement('span');
      k.className = 'memory-kw';
      k.textContent = kw;
      foot.appendChild(k);
    }
  }
  if (m.hit_count) {
    const hits = document.createElement('span');
    hits.className = 'memory-hits';
    hits.textContent = `🔗 ${m.hit_count}`;
    foot.appendChild(hits);
  }
  const del = document.createElement('button');
  del.className = 'memory-del';
  del.textContent = '🗑';
  del.title = '删除这条记忆';
  del.addEventListener('click', () => App.Memory.deleteOne(m.id));
  foot.appendChild(del);

  item.appendChild(head);
  item.appendChild(body);
  item.appendChild(foot);
  return item;
};

App.Memory.deleteOne = async function (id) {
  if (!(await App.confirm('确定删除这条记忆吗?', { danger: true, okText: '删除' }))) return;
  try {
    const res = await App.api(`/v1/memory/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (res.ok) {
      App.toast('已删除一条记忆', 'ok');
      App.Memory.refresh();
    } else {
      const d = await res.json();
      App.toast(`删除失败: ${d.detail || res.status}`, 'err');
    }
  } catch (e) {
    App.toast(`删除失败: ${e.message}`, 'err');
  }
};

App.Memory.clearAll = async function () {
  if (!(await App.confirm('确定清空全部长期记忆吗?此操作不可恢复。', { danger: true, okText: '清空' }))) return;
  try {
    const res = await App.api('/v1/memory/clear', { method: 'POST' });
    const d = await res.json();
    if (res.ok) {
      App.toast(`已清空长期记忆(${d.deleted || 0} 条)`, 'ok');
      App.Memory.refresh();
    } else {
      App.toast('清空记忆失败', 'err');
    }
  } catch (e) {
    App.toast(`清空记忆失败: ${e.message}`, 'err');
  }
};

App.Memory.add = async function () {
  const input = App.els.memoryInput;
  const content = (input.value || '').trim();
  if (!content) return;
  const kwRaw = (App.els.memoryKwInput.value || '').trim();
  const keywords = kwRaw ? kwRaw.split(/[,，]/).map((s) => s.trim()).filter(Boolean) : [];
  input.value = '';
  App.els.memoryKwInput.value = '';
  try {
    const res = await App.api('/v1/memory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, keywords }),
    });
    const d = await res.json();
    if (res.ok) {
      App.toast('已保存新记忆', 'ok');
      App.Memory.refresh();
    } else {
      App.toast(`保存失败: ${d.detail || res.status}`, 'err');
    }
  } catch (e) {
    App.toast(`保存失败: ${e.message}`, 'err');
  }
};

App.Memory.fmtTime = function (ts) {
  if (!ts) return '';
  try { return new Date(ts * 1000).toLocaleString(); } catch (_) { return ''; }
};
