/* 聊天附件模块:上传 / 展示 / 移除 / 发送时携带 */
App.Attach = {};

App.Attach.currentIds = new Set();
App.Attach._toggleBtn = null;
App.Attach._popover = null;

App.Attach.init = function () {
  // 兼容新旧元素名
  App.Attach._toggleBtn = App.els.attachBtn || App.els.attachToggle || document.getElementById('attachBtn');
  App.Attach._popover = App.els.attachPopover || document.getElementById('attachPopover');
  // 如果没有 popover DOM，创建一个
  if (!App.Attach._popover) {
    const pop = document.createElement('div');
    pop.id = 'attachPopover';
    pop.className = 'mode-popover';
    pop.hidden = true;
    pop.innerHTML = `
      <div class="mpl-list">
        <div class="attach-opt" data-action="attach" style="padding:8px 12px;cursor:pointer;font-size:13px;">📎 添加文件</div>
        <div class="attach-opt" data-action="image" style="padding:8px 12px;cursor:pointer;font-size:13px;">🖼 上传图片</div>
        <div class="attach-opt" data-action="video" style="padding:8px 12px;cursor:pointer;font-size:13px;">🎬 上传视频</div>
        <div class="attach-opt" data-action="screenshot" style="padding:8px 12px;cursor:pointer;font-size:13px;">📸 截屏识图</div>
        <div class="attach-opt" data-action="voice" style="padding:8px 12px;cursor:pointer;font-size:13px;">🎤 语音输入</div>
      </div>
    `;
    document.body.appendChild(pop);
    App.Attach._popover = pop;
    App.els.attachPopover = pop;
  }
  if (App.Attach._toggleBtn) App.Attach._toggleBtn.addEventListener('click', e => {
    e.stopPropagation();
    const popover = App.Attach._popover;
    if (!popover) return;
    if (popover.hidden) {
      // 定位到按钮附近
      const r = App.Attach._toggleBtn.getBoundingClientRect();
      popover.style.left = Math.min(Math.max(8, r.left), window.innerWidth - 160) + 'px';
      popover.style.bottom = (window.innerHeight - r.top + 8) + 'px';
      popover.style.top = 'auto';
    }
    popover.hidden = !popover.hidden;
  });
  if (App.Attach._popover) {
    App.Attach._popover.addEventListener('click', e => {
      const opt = e.target.closest('.attach-opt');
      if (!opt) return;
      e.stopPropagation();
      const action = opt.dataset.action;
      if (App.Attach._popover) App.Attach._popover.hidden = true;
      if (action === 'attach') App.els.attachInput?.click();
      else if (action === 'image') App.els.imageInput?.click();
      else if (action === 'screenshot') App.Chat.captureScreen?.();
      else if (action === 'voice') App.Voice?.startRecording?.();
      else if (action === 'video') App.els.videoInput?.click();
    });
  }
  // 点击外部关闭
  document.addEventListener('click', e => {
    if (App.Attach._popover && !App.Attach._popover.hidden &&
        !App.Attach._toggleBtn?.contains(e.target) &&
        !App.Attach._popover.contains(e.target)) {
      App.Attach._popover.hidden = true;
    }
  });
  // ESC 关闭
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && App.Attach._popover && !App.Attach._popover.hidden) {
      App.Attach._popover.hidden = true;
    }
  });
  if (App.els.attachInput) App.els.attachInput.addEventListener('change', () => {
    if (App.els.attachInput.files && App.els.attachInput.files.length) {
      App.Attach.upload(App.els.attachInput.files);
    }
    App.els.attachInput.value = '';
  });
  if (App.els.attachClearBtn) App.els.attachClearBtn.addEventListener('click', () => App.Attach.clearAll());
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
        App.toast(`已添加附件: ${d.name}`, 'ok');
      } else {
        App.toast(`附件「${file.name}」上传失败: ${d.detail || res.status}`, 'err');
      }
    } catch (e) {
      App.toast(`附件「${file.name}」上传失败: ${e.message}`, 'err');
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
