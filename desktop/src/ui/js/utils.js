/* 工具函数:图片解析、欢迎页刷新等 */
(function () {
  // 后端时间统一是"秒级"Unix 时间戳(如 1789821286.59),而 JS Date 需要毫秒。
  // 历史数据里也可能混入毫秒值,这里同时兼容两种单位,避免出现 1970 年。
  App.toMs = function (ts) {
    const n = Number(ts);
    if (!n || !isFinite(n)) return 0;
    return n < 1e12 ? n * 1000 : n;
  };

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
      }
    } catch (_) {}
    // 3. Python dict repr
    if (!url) {
      try {
        const quoted = String(resultStr).replace(/'/g, '"');
        const obj = JSON.parse(quoted);
        url = obj.image_url || obj.url || '';
      } catch (_) {}
    }
    // 相对路径补全:后端返回 /images/xxx.png,Electron 页面协议是 file://,
    // 必须拼上 App.backendUrl 才能正确访问。
    if (url && url.startsWith('/') && App.backendUrl) {
      url = App.backendUrl + url;
    }
    return url;
  };

  // 从工具结果提取视频 URL(generate_video / enhance_video)
  App.parseVideoUrl = function (resultStr) {
    if (!resultStr) return '';
    let url = '';
    // 1. JSON 解析:video_url 优先(本地),remote_url 兜底(CDN,可能不可达)
    try {
      const obj = typeof resultStr === 'string' ? JSON.parse(resultStr) : resultStr;
      if (obj && typeof obj === 'object') {
        url = obj.video_url || obj.url || obj.remote_url || '';
      }
    } catch (_) {}
    // 2. 正则兜底:直接匹配 .mp4 链接
    if (!url) {
      const m = String(resultStr).match(/(https?:\/\/[^\s"'`()<>]+\.mp4)/i);
      if (m) url = m[1];
    }
    // 相对路径补全:后端返回 /videos/xxx.mp4,Electron 页面协议是 file://,
    // 必须拼上 App.backendUrl 才能正确访问(与 parseImageUrl 一致)。
    if (url && url.startsWith('/') && App.backendUrl) {
      url = App.backendUrl + url;
    }
    return url;
  };

  // 构建内联视频播放器 HTML(各渲染路径共用,样式保持一致)
  App.buildVideoHtml = function (url, cls) {
    return '<video class="' + (cls || 'step-video-result') + '" src="' + App.escapeHtml(url) + '"'
      + ' controls preload="metadata" playsinline'
      + ' style="max-width:100%;width:100%;max-height:420px;border-radius:10px;display:block;background:#000;margin:6px 0"></video>';
  };

  // 刷新欢迎页显隐
  App.refreshWelcomePage = function () {
    const msgs = App.els.messages;
    const hasUserMsg = msgs ? Array.from(msgs.querySelectorAll('.msg.user, .msg.msg-user')).length > 0 : false;
    if (hasUserMsg) {
      document.body.classList.add('has-messages');
    } else {
      document.body.classList.remove('has-messages');
    }
  };

  // 去除 thinking 标签(包括内容)
  App.stripThinkingTags = function (content) {
    if (!content) return '';
    return content
      // 成对剥离 thinking 变体标签及其内容:
      // <thinking>/<ant:thinking>/<antThinking>/<|thinking|>/<|/thinking|> 等模型私有变体统一处理,
      // 否则变体标签不被识别,整段思考原文会泄漏进主答案气泡。
      .replace(/<(?:\|)?\/?(?:\|)?(?:[a-zA-Z][\w.:-]*)?thinking[\w.:-]*(?:\|)?[^>]*>[\s\S]*?<(?:\|)?\/?(?:\|)?(?:[a-zA-Z][\w.:-]*)?thinking[\w.:-]*(?:\|)?[^>]*>/gi, '')
      .replace(/<(?:\|)?\/?(?:\|)?(?:[a-zA-Z][\w.:-]*)?thinking[\w.:-]*(?:\|)?[^>]*>/gi, '')
      .replace(/<thinking[\s\S]*?<\/thinking>/gi, '')
      .replace(/<\/?thinking[^>]*>/gi, '')
      .replace(/<\/?tool_call[^>]*>/gi, '')
      .replace(/<\/?function[^>]*>/gi, '')
      .replace(/<\/?parameter[^>]*>/gi, '')
      .replace(/<\/?summary[^>]*>/gi, '')
      .replace(/<\/?details[^>]*>/gi, '')
      .replace(/<detailsopen>/gi, '')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  };

  // 流式专用版本: 逐 token 分块调用, 只剥离残留标签, 绝不 trim、不折叠换行。
  // ⚠️ 不要在 token 分块路径使用 stripThinkingTags——其末尾 .trim() 会把
  // "独占一个 chunk 的 \n\n"(LLM 段落分隔的常见形态)整块清空,
  // 导致所有段落/列表换行丢失, 整篇答案挤成一个密集段落。
  App.stripThinkingTagsStream = function (content) {
    if (!content) return '';
    return content
      .replace(/<\/?(?:\|)?(?:[a-zA-Z][\w.:-]*)?thinking[\w.:-]*(?:\|)?[^>]*>/gi, '')
      .replace(/<\/?tool_call[^>]*>/gi, '')
      .replace(/<\/?function[^>]*>/gi, '')
      .replace(/<\/?parameter[^>]*>/gi, '');
  };

  // 设置状态
  App.setStatus = function (ok, text) {
    const dot = App.els.statusDot;
    const txt = App.els.statusText;
    if (dot) dot.className = ok ? 'status-dot ok' : 'status-dot err';
    if (txt) txt.textContent = ok ? '已连接' : (text || '未连接');
  };
})();
