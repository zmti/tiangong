/* 知识库模块:文档统计 / 列表 / 上传 / 单个删除 / 清空 / 拖拽 */
App.KB = {};

App.KB.refresh = async function () {
  await Promise.all([App.KB.renderStats(), App.KB.renderDocs()]);
};

App.KB.renderStats = async function () {
  const el = App.els.docStats;
  try {
    const res = await App.apiJSON('/v1/documents/stats');
    if (!res.success) { el.innerHTML = '知识库: 不可用'; return; }
    const d = res.data;
    el.innerHTML = `📄 文档 <b>${d.documents || 0}</b> · 片段 <b>${d.chunks || 0}</b>`;
  } catch (_) {
    el.textContent = '知识库: 加载失败';
  }
};

App.KB.renderDocs = async function () {
  const listEl = App.els.docList;
  try {
    const res = await App.apiJSON('/v1/documents/stats');
    if (!res.success) { listEl.innerHTML = ''; return; }
    const d = res.data;
    const sources = d.by_source || {};
    const names = Object.keys(sources).sort();
    listEl.innerHTML = '';
    if (!names.length) {
      listEl.innerHTML = '<div class="session-empty">知识库为空,拖拽或点击上传文档</div>';
      return;
    }
    for (const name of names) {
      const item = document.createElement('div');
      item.className = 'doc-item';
      const icon = document.createElement('span');
      icon.className = 'd-icon';
      icon.textContent = App.KB.iconFor(name);
      const nm = document.createElement('span');
      nm.className = 'd-name';
      nm.textContent = name;
      nm.title = name;
      const chunks = document.createElement('span');
      chunks.className = 'd-chunks';
      chunks.textContent = `${sources[name]} 片段`;
      const del = document.createElement('button');
      del.className = 'd-del';
      del.title = '删除该文档';
      del.textContent = '🗑';
      del.addEventListener('click', () => App.KB.deleteDoc(name));
      item.appendChild(icon);
      item.appendChild(nm);
      item.appendChild(chunks);
      item.appendChild(del);
      listEl.appendChild(item);
    }
  } catch (_) {
    listEl.innerHTML = '<div class="session-empty">文档列表加载失败</div>';
  }
};

App.KB.iconFor = function (name) {
  const ext = (name || '').split('.').pop().toLowerCase();
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'].includes(ext)) return '🖼';
  if (['pdf'].includes(ext)) return '📕';
  if (['doc', 'docx'].includes(ext)) return '📘';
  if (['xls', 'xlsx', 'csv'].includes(ext)) return '📊';
  if (['ppt', 'pptx'].includes(ext)) return '📙';
  if (['py', 'js', 'ts', 'java', 'c', 'cpp', 'go', 'rs'].includes(ext)) return '💻';
  return '📄';
};

App.KB.upload = async function (files) {
  if (!files || !files.length) return;
  let success = 0;
  for (const file of Array.from(files)) {
    const fd = new FormData();
    fd.append('file', file);
    try {
      const res = await App.api('/v1/documents/upload', { method: 'POST', body: fd });
      const data = await res.json();
      if (res.ok) {
        success++;
        App.toast(`已索引「${file.name}」(${data.chunks || 0} 片段)`, 'ok');
      } else {
        App.toast(`「${file.name}」上传失败: ${data.detail || data.error || res.status}`, 'err');
      }
    } catch (e) {
      App.toast(`「${file.name}」网络错误: ${e.message}`, 'err');
    }
  }
  if (success > 0) App.KB.refresh();
};

App.KB.deleteDoc = async function (source) {
  if (!(await App.confirm(`确定从知识库删除「${source}」吗?`, { danger: true, okText: '删除' }))) return;
  try {
    const res = await App.apiJSON(`/v1/documents?source=${encodeURIComponent(source)}`, { method: 'DELETE' });
    if (res.success) {
      const d = res.data || {};
      App.toast(`已删除「${source}」(${d.deleted_chunks || 0} 片段)`, 'ok');
      App.KB.refresh();
    } else {
      App.toast(`删除失败: ${res.detail || res.status}`, 'err');
    }
  } catch (e) {
    App.toast(`删除失败: ${e.message}`, 'err');
  }
};

App.KB.clearAll = async function () {
  if (!(await App.confirm('确定清空整个知识库吗?此操作不可恢复。', { danger: true, okText: '清空' }))) return;
  try {
    const res = await App.apiJSON('/v1/documents/all', { method: 'DELETE' });
    if (res.success) {
      const d = res.data || {};
      App.toast(`知识库已清空(${d.chunks || 0} 片段)`, 'ok');
      App.KB.refresh();
    } else {
      App.toast('清空知识库失败', 'err');
    }
  } catch (e) {
    App.toast(`清空知识库失败: ${e.message}`, 'err');
  }
};
