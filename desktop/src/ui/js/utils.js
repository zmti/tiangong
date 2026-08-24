/* 工具函数:图片解析、欢迎页刷新等 */
(function () {
  // 从工具结果提取图片 URL
  App.parseImageUrl = function (resultStr) {
    if (!resultStr) return '';
    let url = '';
    // 1. 直接匹配 URL
    const m = String(resultStr).match(/(https?:\/\/[^\s"'`()<>]+\.(?:png|jpe?g|webp|gif|jpeg))/i);
    if (m) return m[1] || '';
    // 2. JSON 解析
    try {
      const obj = typeof resultStr === 'string' ? JSON.parse(resultStr) : resultStr;
      if (obj && typeof obj === 'object') {
        url = obj.image_url || obj.url || '';
        if (url) return url;
      }
    } catch (_) {}
    // 3. Python dict repr
    try {
      const quoted = String(resultStr).replace(/'/g, '"');
      const obj = JSON.parse(quoted);
      url = obj.image_url || obj.url || '';
      if (url) return url;
    } catch (_) {}
    return '';
  };

  // 刷新欢迎页显隐
  App.refreshWelcomePage = function () {
    const msgs = App.els.messages;
    const hasUserMsg = msgs ? Array.from(msgs.querySelectorAll('.msg.user')).length > 0 : false;
    if (hasUserMsg) {
      document.body.classList.add('has-messages');
    } else {
      document.body.classList.remove('has-messages');
    }
  };

  // 去除 thinking 标签
  App.stripThinkingTags = function (content) {
    if (!content) return '';
    return content
      .replace(/<\/?thinking[^>]*>/gi, '')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  };

  // 设置状态
  App.setStatus = function (ok, text) {
    const dot = App.els.statusDot;
    const txt = App.els.statusText;
    if (dot) dot.className = ok ? 'status-dot ok' : 'status-dot err';
    if (txt) txt.textContent = ok ? '已连接' : (text || '未连接');
  };
})();
