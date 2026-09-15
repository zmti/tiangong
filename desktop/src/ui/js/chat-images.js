/* 聊天图片生成模块:进度 UI / 结果展示 / 错误处理 */
App._imgGenCard = null;
App._dotsInterval = null;

App.showImageGenerating = function () {
  if (App._imgGenCard) return;
  const wrap = document.createElement('div');
  wrap.className = 'msg bot';
  const avatar = App.makeAvatar('bot');
  const content = document.createElement('div');
  content.className = 'msg-content';
  content.innerHTML = '<div class="img-gen-bubble">'
    + '<svg class="img-gen-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'
    + '  <rect x="3" y="3" width="18" height="18" rx="3" ry="3"></rect>'
    + '  <circle cx="8.5" cy="8.5" r="1.5"></circle>'
    + '  <path d="M21 15l-5-5L5 21"></path>'
    + '</svg>'
    + '<span class="img-gen-status">'
    + '  <span class="dot"></span>'
    + '  <span class="txt">AI 正在作画<span class="txt-dots"></span></span>'
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
  App._imgGenCard = wrap;
  App.scrollMessagesToBottom();
  // 点号动画:JS 驱动,避免 CSS content 动画不支持的问题
  App._dotsInterval = setInterval(() => {
    const dots = wrap.querySelector('.txt-dots');
    if (!dots) return;
    const count = (parseInt(dots.dataset.count || 0) + 1) % 4;
    dots.dataset.count = count;
    dots.textContent = '.'.repeat(count);
  }, 500);
};

App.finishImageGenerating = function (resultStr) {
  if (!App._imgGenCard) return;
  clearInterval(App._dotsInterval);
  App._dotsInterval = null;
  const bubble = App._imgGenCard.querySelector('.img-gen-bubble');
  const url = App.parseImageUrl(resultStr);
  if (url) {
    bubble.innerHTML = '<img class="img-gen-result" src="' + App.escapeHtml(url) + '" alt="生成结果">';
  } else {
    // 图片生成失败:显示错误信息而非静默删除
    const isErr = String(resultStr).includes('ConnectionResetError') ||
                  String(resultStr).includes('error') ||
                  String(resultStr).includes('失败');
    if (isErr) {
      bubble.innerHTML = '<div class="img-gen-error">'
        + '<svg class="img-gen-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>'
        + '<span class="img-gen-status">图片生成失败: ' + App.escapeHtml((resultStr || '').slice(0, 100)) + '</span>'
        + '</div>';
      App._imgGenCard.classList.add('img-gen-error');
    } else {
      App._imgGenCard.remove();
    }
  }
  App._imgGenCard = null;
};

App.clearImageGenerating = function () {
  clearInterval(App._dotsInterval);
  App._dotsInterval = null;
  if (App._imgGenCard) { App._imgGenCard.remove(); App._imgGenCard = null; }
};
