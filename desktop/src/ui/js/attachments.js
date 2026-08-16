/* 聊天附件模块:上传 / 展示 / 移除 / 发送时携带 */
App.Attach = {};

App.Attach.currentIds = new Set();

App.Attach.init = function () {
  App.els.attachBtn.addEventListener('click', () => App.els.attachInput.click());
  App.els.attachInput.addEventListener('change', () => {
    if (App.els.attachInput.files && App.els.attachInput.files.length) {
      App.Attach.upload(App.els.attachInput.files);
    }
    App.els.attachInput.value = '';
  });
  App.els.attachClearBtn.addEventListener('click', () => App.Attach.clearAll());
};

App.Attach.upload = async function (files) {
  for (const file of Array.from(files)) {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('session_id', App.sessionId);
    try {
      const res = await App.api('/v1/attachments', { method: 'POST', body: fd });
      const d = await res.json();
      if (res.ok) {
        App.Attach.currentIds.add(d.id);
        App.appendMessage(`📎 已添加附件: ${d.name}`, 'bot');
      } else {
        App.appendMessage(`❌ 附件「${file.name}」上传失败: ${d.detail || res.status}`, 'error');
      }
    } catch (e) {
      App.appendMessage(`❌ 附件「${file.name}」上传失败: ${e.message}`, 'error');
    }
  }
  App.Attach.render();
};

App.Attach.render = async function () {
  const listEl = App.els.attachList;
  if (!listEl) return;
  listEl.innerHTML = '';
  if (!App.Attach.currentIds.size) {
    App.els.attachBar.classList.remove('show');
    return;
  }
  try {
    const res = await App.api(`/v1/attachments?session_id=${encodeURIComponent(App.sessionId)}`);
    const d = await res.json();
    const items = (d.attachments || []).filter((a) => App.Attach.currentIds.has(a.id));
    App.els.attachBar.classList.add('show');
    for (const a of items) {
      const chip = document.createElement('span');
      chip.className = 'attach-chip';
      chip.textContent = a.name;
      const del = document.createElement('button');
      del.className = 'attach-chip-del';
      del.textContent = '×';
      del.title = '移除附件';
      del.addEventListener('click', () => App.Attach.remove(a.id));
      chip.appendChild(del);
      listEl.appendChild(chip);
    }
  } catch (_) {
    App.els.attachBar.classList.add('show');
  }
};

App.Attach.remove = async function (id) {
  App.Attach.currentIds.delete(id);
  try {
    await App.api(`/v1/attachments/${encodeURIComponent(id)}?session_id=${encodeURIComponent(App.sessionId)}`, { method: 'DELETE' });
  } catch (_) { /* 忽略 */ }
  App.Attach.render();
};

App.Attach.clearAll = async function () {
  App.Attach.currentIds.clear();
  try {
    await App.api(`/v1/attachments?session_id=${encodeURIComponent(App.sessionId)}`, { method: 'DELETE' });
  } catch (_) { /* 忽略 */ }
  App.Attach.render();
};

App.Attach.reset = function () {
  App.Attach.currentIds.clear();
  App.Attach.render();
};

/* 发送时在请求体里带上附件 ID */
App.Attach.patchBody = function (body) {
  if (App.Attach.currentIds.size) body.attachments = Array.from(App.Attach.currentIds);
  return body;
};
