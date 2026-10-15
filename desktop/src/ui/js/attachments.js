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

/* 构建单张附件卡片。showDel=true 带"移除"按钮(composer 待发区用),
 * false 仅保留"打开"(会话气泡里的已发送附件,历史不可移除)。返回 card DOM。 */
App.Attach._makeCard = function (a, sid, showDel) {
  const k = App.Attach._kind(a);
  const card = document.createElement('div');
  card.className = 'attach-card' + (k.img ? ' attach-card-img' : '');
  // 图片类:左侧缩略图(走后端 /images/ 接口),其余类:图标+类型标签
  if (k.img) {
    const img = document.createElement('img');
    img.className = 'attach-thumb';
    img.src = `${App.backendUrl || ''}/v1/attachments/images/${a.id}?session_id=${sid}`;
    img.alt = a.name || '图片附件';
    img.onerror = () => { img.style.display = 'none'; };
    card.appendChild(img);
  }
  const meta = document.createElement('div');
  meta.className = 'attach-card-meta';
  const nameEl = document.createElement('div');
  nameEl.className = 'attach-card-name';
  nameEl.textContent = `${k.icon} ${a.name || '附件'}`;
  nameEl.title = a.name || '';
  meta.appendChild(nameEl);
  const sizeTxt = a.size > 1048576 ? (a.size / 1048576).toFixed(1) + ' MB'
               : a.size >= 1024 ? Math.round(a.size / 1024) + ' KB' : a.size + ' B';
  const sub = document.createElement('div');
  sub.className = 'attach-card-sub';
  // 图片交给视觉模型,非图片已解析进上下文供分析
  sub.textContent = [k.label, sizeTxt || undefined, k.img ? '图片·视觉模型识别' : '已解析·加入分析上下文']
    .filter(Boolean).join(' · ');
  meta.appendChild(sub);
  card.appendChild(meta);
  const open = document.createElement('button');
  open.className = 'attach-card-open';
  open.title = '用系统程序打开';
  open.textContent = '打开';
  open.addEventListener('click', () => App.Attach.openAttachment(a));
  card.appendChild(open);
  if (showDel) {
    const del = document.createElement('button');
    del.className = 'attach-card-del';
    del.title = '移除附件';
    del.textContent = '×';
    del.addEventListener('click', () => App.Attach.remove(a.id));
    card.appendChild(del);
  }
  return card;
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
    const sid = encodeURIComponent(App.sessionId);
    for (const a of items) listEl.appendChild(App.Attach._makeCard(a, sid, true));
  } catch (_) {
    App.els.attachBar.classList.add('show');
  }
};

/* 把"已发送"的附件卡片挂进用户消息气泡(container 为其 .msg-content)。
 * ids 由调用方在 reset 前快照传入(不依赖 currentIds,避免发送完成后清空而丢失),
 * 让附件不止停在输入框,也真正"显示在会话中"。 */
App.Attach.renderToBubble = async function (container, ids, sid) {
  if (!container || !ids || !ids.length) return;
  sid = sid || App.sessionId;
  const sidEnc = encodeURIComponent(sid);
  try {
    const res = await App.api(`/v1/attachments?session_id=${sidEnc}`);
    const d = await res.json();
    const byId = {};
    (d.attachments || []).forEach((a) => { byId[a.id] = a; });
    const host = document.createElement('div');
    host.className = 'attach-bubble-cards';
    let added = 0;
    for (const id of ids) {
      const a = byId[id];
      if (!a) continue;
      host.appendChild(App.Attach._makeCard(a, sidEnc, false));
      added++;
    }
    if (added) {
      const footer = container.querySelector('.msg-footer');
      if (footer) container.insertBefore(host, footer);
      else container.appendChild(host);
    }
  } catch (_) { /* 拉取失败不阻塞消息展示 */ }
};

/* 按文件名/mime 判定附件类型,返回 {icon,label,img}。img=true 时渲染缩略图。 */
App.Attach._kind = function (a) {
  const n = (a.name || '').toLowerCase();
  const m = a.mime || '';
  if (m.startsWith('image/') || /\.(png|jpe?g|gif|webp|bmp|svg|ico|heic)$/.test(n)) return { label: '图片', icon: '🖼', img: true };
  if (/\.(xlsx?|csv|tsv)$/.test(n) || m.includes('spreadsheet') || m.includes('excel') || /csv/.test(m)) return { label: '表格', icon: '📊' };
  if (/\.(docx?|wps|odt|rtf|pages)$/.test(n) || m.includes('word') || m.includes('document')) return { label: '文档', icon: '📄' };
  if (/\.(pptx?|dps|odp|key)$/.test(n) || m.includes('powerpoint') || m.includes('presentation')) return { label: '演示', icon: '📽' };
  if (n.endsWith('.pdf') || m === 'application/pdf') return { label: 'PDF', icon: '📕' };
  if (m.startsWith('video/') || /\.(mp4|mov|webm|avi|mkv|flv)$/.test(n)) return { label: '视频', icon: '🎬' };
  if (m.startsWith('audio/') || /\.(mp3|wav|ogg|m4a|flac|aac)$/.test(n)) return { label: '音频', icon: '🎵' };
  if (/\.(zip|rar|7z|tar|gz)$/.test(n)) return { label: '压缩包', icon: '🗜' };
  if (/\.(txt|md|log|ini|xml|toml|json|ya?ml|py|js|ts|go|rs|java|c|cpp|h|sh|ps1|bat)$/.test(n) || m.startsWith('text/')) return { label: '文本', icon: '📝' };
  return { label: '文件', icon: '📎' };
};

/* 用系统程序打开附件:优先 electron 主进程 shell.openPath(白名单校验),
 * web/纯浏览器模式退化为新窗口下载。 */
App.Attach.openAttachment = async function (a) {
  const sid = encodeURIComponent(App.sessionId);
  const url = `${App.backendUrl || ''}/v1/attachments/file/${encodeURIComponent(a.id)}?session_id=${sid}`;
  try {
    if (window.agentApp && window.agentApp.openLocalFile) {
      const res = await App.api(`/v1/attachments/file/${encodeURIComponent(a.id)}/path?session_id=${sid}`);
      const d = await res.json().catch(() => ({}));
      if (res.ok && d.path) {
        const r = await window.agentApp.openLocalFile(d.path);
        if (r && !r.ok) App.toast(`打开失败: ${r.reason || '未知原因'}`, 'err');
        else App.toast(`已用系统程序打开「${a.name}」`, 'ok');
        return;
      }
    }
    // 回退:新窗口直接下载
    window.open(url, '_blank');
  } catch (e) {
    App.toast(`打开失败: ${e.message}`, 'err');
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
