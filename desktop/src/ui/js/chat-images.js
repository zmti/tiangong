/* 聊天媒体生成模块:图片 / 视频生成的进度 UI、结果展示、错误处理
 *
 * 历史问题:只有 generate_image 有专属进度卡与内联结果展示;
 * generate_video 的结果只在 tool_done 分支(后端已不发该事件)和历史渲染路径
 * 才会出现播放器 → 实时运行时用户只看到 agent 那句"已通过工具 generate_video
 * 完成任务,结果已展示",画面上什么都没有。现在统一走 _mediaCards。
 */
App._mediaCards = { image: null, video: null };
App._dotsInterval = null;

const _MEDIA_META = {
  image: {
    label: 'AI 正在作画',
    failLabel: '图片生成失败',
    offLabel: '图片生成工具当前不可用(已被禁用)',
    icon: '<rect x="3" y="3" width="18" height="18" rx="3" ry="3"></rect>'
        + '<circle cx="8.5" cy="8.5" r="1.5"></circle>'
        + '<path d="M21 15l-5-5L5 21"></path>',
    parse: function (r) { return App.parseImageUrl ? App.parseImageUrl(r) : ''; },
    render: function (url) {
      return '<img class="img-gen-result" src="' + App.escapeHtml(url) + '" alt="生成结果">';
    },
  },
  video: {
    label: 'AI 正在生成视频',
    failLabel: '视频生成失败',
    offLabel: '视频生成工具当前不可用(已被禁用)',
    icon: '<rect x="2" y="5" width="14" height="14" rx="2.5" ry="2.5"></rect>'
        + '<path d="M16 10l6-3.5v11L16 14z"></path>',
    parse: function (r) { return App.parseVideoUrl ? App.parseVideoUrl(r) : ''; },
    render: function (url) { return App.buildVideoHtml(url, 'img-gen-video'); },
  },
};

/** 展示"生成中"进度卡(kind: 'image' | 'video')。已存在则复用。 */
App.showMediaGenerating = function (kind) {
  kind = _MEDIA_META[kind] ? kind : 'image';
  if (App._mediaCards[kind]) return;
  const meta = _MEDIA_META[kind];
  const wrap = document.createElement('div');
  wrap.className = 'msg bot';
  const avatar = App.makeAvatar('bot');
  const content = document.createElement('div');
  content.className = 'msg-content';
  content.innerHTML = '<div class="img-gen-bubble">'
    + '<svg class="img-gen-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'
    + meta.icon
    + '</svg>'
    + '<span class="img-gen-status">'
    + '  <span class="dot"></span>'
    + '  <span class="txt">' + meta.label + '<span class="txt-dots"></span></span>'
    + '</span>'
    + '</div>';
  wrap.appendChild(avatar);
  wrap.appendChild(content);
  // 插入到当前答案气泡后面(若有),否则追加到末尾
  const anchor = App._answerWrap;
  if (anchor && anchor.parentNode === App.els.messages) {
    App.els.messages.insertBefore(wrap, anchor.nextSibling);
  } else {
    App.els.messages.appendChild(wrap);
  }
  App.scrollMessagesToBottom();
  // 点号动画:JS 驱动,避免 CSS content 动画不支持的问题
  const rec = { wrap: wrap, timer: null };
  rec.timer = setInterval(() => {
    const dots = wrap.querySelector('.txt-dots');
    if (!dots) return;
    const count = (parseInt(dots.dataset.count || 0) + 1) % 4;
    dots.dataset.count = count;
    dots.textContent = '.'.repeat(count);
  }, 500);
  App._mediaCards[kind] = rec;
};

/** 生成结束:把进度卡换成结果(或人话错误)。kind: 'image' | 'video' */
App.finishMediaGenerating = function (kind, resultStr) {
  kind = _MEDIA_META[kind] ? kind : 'image';
  const rec = App._mediaCards[kind];
  if (!rec) return;
  const meta = _MEDIA_META[kind];
  clearInterval(rec.timer);
  const card = rec.wrap;
  const bubble = card.querySelector('.img-gen-bubble');
  App._mediaCards[kind] = null;
  const url = meta.parse(resultStr);
  if (url) {
    // 视频用播放器替换整块内容,图片沿用原有 <img>
    bubble.innerHTML = meta.render(url);
    if (kind === 'video') App.scrollMessagesToBottom();
  } else if (/"disabled"\s*:\s*true/.test(String(resultStr || ''))) {
    // 工具被禁用(如被用户规则拦截):这不是"生成失败",别把裸 JSON 甩成红色报错,
    // 换成一句人话 + 中性配色。注:「设置 → 生成模型」的开关不会再走到这里 ——
    // 那个开关只是改用当前对话模型,能力照常可用。
    bubble.innerHTML = '<div class="img-gen-off">'
      + '<svg class="img-gen-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>'
      + '<span class="img-gen-status">' + meta.offLabel + '</span>'
      + '</div>';
    card.classList.add('img-gen-off');
  } else {
    // 生成失败:显示错误信息而非静默删除
    const isErr = String(resultStr).includes('ConnectionResetError') ||
                  String(resultStr).includes('error') ||
                  String(resultStr).includes('失败');
    if (isErr) {
      bubble.innerHTML = '<div class="img-gen-error">'
        + '<svg class="img-gen-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>'
        + '<span class="img-gen-status">' + meta.failLabel + ': ' + App.escapeHtml((resultStr || '').slice(0, 100)) + '</span>'
        + '</div>';
      card.classList.add('img-gen-error');
    } else {
      card.remove();
    }
  }
};

/** 清理所有"生成中"卡片(流终止/新一轮/报错时调用)。 */
App.clearMediaGenerating = function () {
  clearInterval(App._dotsInterval);
  App._dotsInterval = null;
  Object.keys(App._mediaCards).forEach((k) => {
    const rec = App._mediaCards[k];
    if (rec) {
      clearInterval(rec.timer);
      rec.wrap.remove();
      App._mediaCards[k] = null;
    }
  });
};

/* ── 兼容旧调用点(chat.js 有 8 处 clearImageGenerating) ── */
App.showImageGenerating = function () { App.showMediaGenerating('image'); };
App.finishImageGenerating = function (resultStr) { App.finishMediaGenerating('image', resultStr); };
App.showVideoGenerating = function () { App.showMediaGenerating('video'); };
App.finishVideoGenerating = function (resultStr) { App.finishMediaGenerating('video', resultStr); };
// 名字保留(兼容),但已改为清理全部媒体卡片 —— 否则要新加 8 处清视频卡
App.clearImageGenerating = function () { App.clearMediaGenerating(); };
