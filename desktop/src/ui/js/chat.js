/* 聊天模块:SSE 流式对话 / 工具事件 / 停止生成 / 图片识别 */
App.Chat = App.Chat || {};

/* 统一滚动到底部工具函数 - 防抖优化 */
App._scrollToBottomTimer = null;
App.scrollMessagesToBottom = function(force = false) {
  if (App._scrollToBottomTimer && !force) return;
  App._scrollToBottomTimer = requestAnimationFrame(() => {
    App._scrollToBottomTimer = null;
    if (!App.els || !App.els.messages) return;
    const el = App.els.messages;
    // 仅当用户已在底部极近距离(<10%)时才跟随滚动,否则保持原位
    const threshold = force ? 100 : el.clientHeight * 0.1;
    if (force || el.scrollHeight - el.scrollTop <= el.clientHeight + threshold) {
      el.scrollTop = el.scrollHeight;
    }
  });
};

/* 引用回复状态 */
App._replyQuote = null;

App.Chat.setReplyQuote = function(text) {
  App._replyQuote = text;
  App.updateReplyIndicator && App.updateReplyIndicator(text);
};

App.Chat.clearReplyQuote = function() {
  App._replyQuote = null;
  App.updateReplyIndicator && App.updateReplyIndicator(null);
};

// 当前请求的步骤 ID 计数器(每次新请求重置)
App.Chat._stepIdCounter = 0;
App.Chat.nextStepId = function () { return ++App.Chat._stepIdCounter; };
App.Chat.resetStepIdCounter = function () { App.Chat._stepIdCounter = 0; };

/* 从断点恢复运行:用户批准/拒绝工具确认后,续跑中断的任务 */
App.Chat.resume = async function (runId, resumeValue) {
  if (!runId) return;
  try {
    const res = await App.apiJSON('/v1/chat/resume', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId, resume_value: resumeValue, stream: true }),
    });
    return res;
  } catch (e) {
    App.appendMessage('恢复失败: ' + e.message, 'error');
  }
};

/* 组装协作模式参数:统一从 App.Mode(单一状态源)读取,UI 控件的改动已同步进该状态 */
App.Chat.modeParams = function () {
  if (App.Mode && App.Mode.params) {
    const p = App.Mode.params();
    // 【修复】前端参数名映射到后端 ChatRequest 字段
    return {
      mode: p.mode || 'auto',
      critic_provider: p.critic || '',
      critic_model: '', // 前端暂不支持单独设置评审模型
      models: p.ensemble || [],
      max_output_tokens: 0,
      orch_subtasks: p.orch?.subtasks || 0,
      orch_candidates: p.orch?.candidates || [],
    };
  }
  return { mode: 'auto' };
};

App.Chat.send = async function () {
  // 已选图片时,发送即执行图片识别
  if (App.selectedImageFile) {
    if (App.streaming) return; // 流式中不可并发发送(与文本一致, 避免双 SSE 竞争)
    const f = App.selectedImageFile;
    App.selectedImageFile = null;
    App.els.imgPreviewBar.classList.remove('show');
    App._planCard = null;
    _startGroup();
    App.Chat.recognizeImage(f);
    return;
  }
  const input = App.els.input;
  const text = input.value.trim();
  /* 统一判断"是否可发送": 非流式 且 有内容 */
  if (!App.canSend()) {
    if (text && App.streaming) App.toast && App.toast('正在生成中, 点停止按钮后再发新消息', 'info');
    return;
  }
  App.lastUserText = text;
  App._lastUserWrap = null;
  await App.Chat.doSend(text);
};

/* 重新生成:找到该回复对应的最后一条用户消息,裁剪其后的所有消息并重发 */
App.Chat.regenerate = async function (wrap) {
  if (App.streaming) return;
  const msgs = App.els.messages;
  const nodes = Array.from(msgs.children);
  const targetIdx = nodes.indexOf(wrap);
  const baseIdx = targetIdx >= 0 ? targetIdx : nodes.length - 1;
  let userText = '';
  let userNode = null;
  for (let i = baseIdx; i >= 0; i--) {
    if (nodes[i].classList.contains('user') || nodes[i].classList.contains('msg-user')) {
      const b = nodes[i].querySelector('.answer-doc');
      userText = b ? (b.textContent || '').trim() : '';
      userNode = nodes[i];
      break;
    }
  }
  if (!userText) return;
  const confirm = App.confirm;
  if (confirm) {
    const ok = await confirm('将删除此回复后的所有消息，确定重新生成？', { okText: '重新生成' });
    if (!ok) return;
  }
  const cutFrom = userNode ? nodes.indexOf(userNode) + 1 : baseIdx + 1;
  for (let i = nodes.length - 1; i >= cutFrom; i--) nodes[i].remove();
  App.lastUserText = userText;
  App._lastUserWrap = userNode;
  App.showTyping();
  App.Chat.doSend(userText, { skipAppendUser: true });
};

/* 编辑用户消息:气泡转输入框,保存后重发 */
App.Chat.editMessage = async function (wrap, bubble) {
  if (App.streaming) return;
  const input = document.createElement('textarea');
  input.className = 'edit-input';
  input.value = (bubble.textContent || '').trim();
  input.rows = Math.min(6, Math.max(2, (input.value.split('\n').length || 1)));
  wrap.classList.add('editing');
  (bubble.parentNode || wrap).insertBefore(input, bubble.nextSibling);
  bubble.style.display = 'none';
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);

  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const val = input.value.trim();
    input.remove();
    bubble.style.display = '';
    wrap.classList.remove('editing');
    if (!save || !val) return;
    const msgs = App.els.messages;
    const nodes = Array.from(msgs.children);
    const idx = nodes.indexOf(wrap);
    for (let i = nodes.length - 1; i > idx; i--) nodes[i].remove();
    bubble.textContent = val;
    App.lastUserText = val;
    App._lastUserWrap = wrap;
    App.showTyping();
    await App.Chat.doSend(val, { skipAppendUser: true });
  };
  input.addEventListener('keydown', (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    e.stopPropagation();
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { finish(false); }
  });
};

/* 启动工具分组:把本次回答的工具调用/思考/计划折叠到单个卡片中 */
function _startGroup() {
  if (App._currentGroup) {
    App._currentGroup.wrap.remove();
    App._currentGroup = null;
  }
  if (App.Group && App.Group.start) App.Group.start();
  if (App.ExecTrace && App.ExecTrace.start) App.ExecTrace.start();
}

/* 更新回复引用指示器 */
App.updateReplyIndicator = function(text) {
  const indicator = document.getElementById('replyIndicator');
  if (!indicator) return;
  if (text) {
    indicator.querySelector('.reply-indicator-text').textContent = text;
    indicator.hidden = false;
    // 绑定取消按钮
    const removeBtn = indicator.querySelector('.remove-reply');
    if (removeBtn) {
      removeBtn.onclick = function() {
        App.Chat.clearReplyQuote();
      };
    }
  } else {
    indicator.hidden = true;
  }
};

/* 结束工具分组:根据是否有答案/工具展示最终摘要 */
function _finishGroup(answerText) {
  if (App.Group && App.Group.finish) {
    App.Group.finish(answerText || '', false);
  }
  App._currentGroup = null;
}

/* 全局安全网:防止 streaming 状态永久卡死 */
App.Chat._streamingSafetyTimer = null;
App.Chat._installStreamingGuard = function () {
  if (App.Chat._streamingGuardInstalled) return;
  App.Chat._streamingGuardInstalled = true;
  setInterval(() => {
    if (App.streaming) {
      // streaming 状态超过 10 分钟未解除,强制重置(兜底防卡死)
      if (!App.Chat._streamingStart) App.Chat._streamingStart = Date.now();
      if (Date.now() - App.Chat._streamingStart > 600000) {
        console.warn('[StreamingGuard] streaming stuck > 10min, force resetting');
        // 关键:abort fetch 连接,防止僵尸连接
        if (App.abortController) {
          try { App.abortController.abort(); } catch (_) {}
          App.abortController = null;
        }
        App.Chat.setStreaming(false);
        App.Chat._streamingStart = null;
      }
    } else {
      App.Chat._streamingStart = null;
    }
  }, 5000);
};
App.Chat._installStreamingGuard();

/* 统一判断是否可发送:非流式 且 有输入内容 */
App.canSend = function () {
  return !App.streaming && App.els.input && App.els.input.value.trim().length > 0;
};

/* 核心发送逻辑 */
App.Chat.doSend = async function (text, opts = {}) {
  // 附加引用回复内容
  if (App._replyQuote) {
    text = `> ${App._replyQuote.trim()}\n\n${text}`;
  }
  // 没有当前会话时自动创建
  if (!App.sessionId || !App.sessionId.startsWith('desktop-')) {
    App.Session.newSession();
  }
  const input = App.els.input;
  input.value = '';
  if (App.syncComposer) App.syncComposer();
  App._planCard = null;
  App.clearImageGenerating();  // 清除上一轮可能残留的"生成中"卡片
  if (App.Confirmation) App.Confirmation.clear();
  App.removeTyping();
  if (!opts.skipAppendUser) App.appendUserMessage(text);
  // 打字指示器延迟显示:等第一个 token 到达再显示,避免无响应时 indicator 闪烁
  // 【关键修复】将 setStreaming(true) 移至 try 块内，确保任何初始化异常都能触发 finally 重置
  _startGroup();  // 开始工具分组

  // 每次 doSend 前清空 mdBuf，避免多次 abort/resume 累积
  App._mdBuf = '';
  // 每轮重置思考卡/答案锚点,避免上一轮残留导致本轮 thinking 错位
  App._thinkingWrap = null;
  App._answerWrap = null;
  /* 修复"再次发送后状态错乱/不能停止/不知可否发送": 引入发送代次(stamp)。
     旧实现中, 当新 doSend abort 旧流时, 旧流的 reader.read() 抛 AbortError,
     其 finally 会异步执行 setStreaming(false) + 强制 sendBtn.disabled=false,
     把新流刚设好的 streaming=true / stopBtn.show 覆盖掉 → 停止按钮消失、
     发送按钮状态错乱。现每轮记录 stamp, finally 只在与当前代次匹配时才
     重置全局 streaming 状态, 旧代次直接跳过, 不再干扰新流。 */
  App.Chat._sendSeq = (App.Chat._sendSeq || 0) + 1;
  const mySeq = App.Chat._sendSeq;
  // 标记是否已完成最终渲染(防止 scheduleRender 在 done 后重复渲染)
  let renderDone = false;
  // 每轮取消上一轮的流式请求，避免并发 SSE 竞争
  if (App.abortController) {
    try { App.abortController.abort(); } catch (_) { /* 忽略 */ }
    App.abortController = null;
  }
  // thinking 标签跨 token 状态
  let inThinking = false;
  // 重置步骤 ID 计数器(每次请求独立)
  App.Chat.resetStepIdCounter();
  let thinkingBuf = '';
  App.abortController = new AbortController();
  const signal = App.abortController.signal;
  // 【关键修复】跟踪 streaming 是否已设置,确保 finally 块能正确重置
  let streamingSet = false;
  // 立即显示打字指示器(三个跳动圆点),缓解首 token 延迟期间的空白感
  App.showTyping();
  let receivedTokens = false;
  // 是否收到过任何合法 SSE 事件(含 done/error/tool/plan/thinking),用于区分"连接异常"与"后端只返回了空 done"
  let receivedAnyEvent = false;
  let isError = false;   // 必须声明,超时回调会检查它
  // 打字指示器延迟显示:等第一个 token 到达再显示,避免无响应时 indicator 闪烁
  let typingShown = false;
  // answerDiv 提升到 try 外层,finally 块可安全引用
  let answerDiv = null;
  let res = null;
  let retries = 0;
  // 同一请求内已提示过的错误文本(连续重复的 error 事件只渲染一次)
  let lastErrMsg = null;
  // done 提升到函数作用域,connectTimer 回调和 finally 都需要读取
  let done = false;
  // 建连超时:api() 自带 60s 超时仅在"未传 signal"时生效,而本函数发流必传 signal,
  // 导致连接建立阶段(后端不可达/未就绪/崩溃)无任何超时保护、永久挂起。
  // 单独加建连超时:超时即 abort 本次 fetch,走重试/报错流程。
  /* 从 token 流中提取 <thinking>...</thinking> 块，避免思考原文进入主答案气泡 */
  function processTokenChunk(chunk) {
    if (!chunk) return { text: '', thinking: '' };
    // 剥离 LLM 内部 XML 标签
    chunk = chunk.replace(/<\/?(?:system|plan|phase|analysis|reflection|context|instruction|output|reasoning|tool_call|function|parameter)[^>]*>/gi, '');
    if (!inThinking) {
      // 先检查是否有完整的 <thinking>...</thinking> 块在同一 chunk 内
      const openIdx = chunk.indexOf('<thinking');
      if (openIdx !== -1) {
        const body = chunk.slice(openIdx);
        const closeIdx = body.indexOf('</thinking>');
        if (closeIdx !== -1) {
          // 完整块在同一 chunk 内: 提取 thinking, 返回前后文本
          const thinking = body.slice(0, closeIdx + 11);
          const after = body.slice(closeIdx + 11);
          return { text: chunk.slice(0, openIdx) + after, thinking };
        }
        // 只有开启标签: 进入 thinking 模式
        inThinking = true;
        thinkingBuf = body;
        return { text: chunk.slice(0, openIdx), thinking: '' };
      }
      // 无开启标签: 检查是否有残留闭合标签(跨 chunk 丢失开启的情况)
      if (chunk.indexOf('</thinking>') !== -1) {
        // 只丢弃孤立的 </thinking> 标签本身, 保留其前后的正常文本
        const cleaned = chunk.replace(/<\/thinking>/gi, '');
        return { text: cleaned, thinking: '' };
      }
      return { text: chunk, thinking: '' };
    }
    // 已在 thinking 模式
    thinkingBuf += chunk;
    // 安全限制:thinkingBuf 超过 8000 字符仍未闭合,强制退出 thinking 模式
    // 防止 </thinking> 丢失导致后续所有 token 被吞
    if (thinkingBuf.length > 8000) {
      console.warn('[processTokenChunk] thinkingBuf exceeded 8000 chars without closing tag, forcing exit');
      const leftover = thinkingBuf;
      inThinking = false;
      thinkingBuf = '';
      return { text: leftover, thinking: '' };
    }
    const endIdx = thinkingBuf.indexOf('</thinking>');
    if (endIdx === -1) return { text: '', thinking: '' };
    const thinking = thinkingBuf.slice(0, endIdx + 11);
    const after = thinkingBuf.slice(endIdx + 11);
    inThinking = false;
    thinkingBuf = '';
    return { text: after, thinking };
  }

  const CONNECT_TIMEOUT_MS = 30000;

  // ── 辅助:找到当前 bot 答案卡片,在其之前插入元素 ──
  function _insertBeforeAnswer(el) {
    if (App._answerWrap && App._answerWrap._answer) {
      App._answerWrap._answer.parentNode.insertBefore(el, App._answerWrap._answer);
    } else {
      App.els.messages.insertBefore(el, App.els.messages.firstChild);
    }
  }

  // ── 辅助:显示 process-rail ──
  function _showRail() {
    if (App._answerWrap && App._answerWrap._rail) {
      App._answerWrap._rail.classList.add('visible');
    }
  }
  const connectTimer = setTimeout(() => {
    if (!res && !done) {
      isError = true;
      if (App.abortController) App.abortController.abort(new DOMException('连接超时', 'TimeoutError'));
      if (typingShown) App.removeTyping();
      App.appendMessage('连接超时,请检查后端服务是否已启动', 'error');
      _finishGroup();
    }
  }, CONNECT_TIMEOUT_MS);

  // 节流渲染:多个 token 合并后每帧一次 re-render,避免 O(n²) 全量 DOM 重排
  let renderTimer = null;
  // 流读取超时(空闲间隙):后端在 120s 内无任何事件(判定为卡死)时主动断开
  let streamTimeout = null;
  // 绝对上限:即使后端持续产生事件,也不得超过 10 分钟,防止永久挂起
  let hardTimeout = null;

  function cleanupStream() {
    if (streamTimeout !== null) { clearTimeout(streamTimeout); streamTimeout = null; }
    if (hardTimeout !== null) { clearTimeout(hardTimeout); hardTimeout = null; }
    if (renderTimer !== null) { cancelAnimationFrame(renderTimer); renderTimer = null; }
  }

  let _lastRenderedHtml = '';
  
  function scheduleRender() {
    if (renderTimer !== null) { console.log('[scheduleRender] skipped: renderTimer pending'); return; }
    if (renderDone) { console.log('[scheduleRender] skipped: renderDone=true'); return; }
    renderTimer = requestAnimationFrame(() => {
      renderTimer = null;
      if (!answerDiv) { console.log('[scheduleRender] skipped: no answerDiv'); return; }
      const md = App._mdBuf || '';
      if (!md) { console.log('[scheduleRender] skipped: empty _mdBuf'); return; }
      console.log('[scheduleRender] rendering, md_len:', md.length);
      
      // 始终全量渲染,确保跨 token 的列表/段落/缩进结构正确
      let html = '';
      try {
        html = App.renderMarkdown(md);
        console.log('[scheduleRender] renderMarkdown ok, html_len:', html.length, 'preview:', html.slice(0, 150));
      } catch (renderErr) {
        console.error('[renderMarkdown] scheduleRender ERROR:', renderErr);
        html = '<p style="white-space:pre-wrap;word-break:break-word;">' + App.escapeHtml(md) + '</p>';
      }
      answerDiv.innerHTML = html;
      _lastRenderedHtml = html;
      
      App.scrollMessagesToBottom();
    });
  }

  try {
    // 【关键修复】在 try 块内设置 streaming 状态，确保 finally 一定能执行
    streamingSet = true;
    App.Chat._streamingStart = Date.now();
    App.Chat.setStreaming(true);
      while (!res && retries < 3) {
        try {
          // 【修复】安全获取 ModelPicker 状态(优先组件 → 备用 localStorage → 最后空值)
          let pickProvider = '', pickModel = '';
          if (App.ModelPicker && App.ModelPicker.get) {
            const state = App.ModelPicker.get();
            pickProvider = state.provider || '';
            pickModel = state.model || '';
          } else {
            // 备用：直接从 localStorage 读取(防止 ModelPicker 组件未初始化)
            try {
              const raw = localStorage.getItem('agent_model_state');
              if (raw) {
                const saved = JSON.parse(raw);
                if (saved && typeof saved === 'object') {
                  pickProvider = typeof saved.provider === 'string' ? saved.provider : '';
                  pickModel = typeof saved.model === 'string' ? saved.model : '';
                }
              }
            } catch (_) {}
          }
          // 【修复】只在真正空时才置 null，避免把 "" 误转为 null
          const finalProvider = pickProvider ? pickProvider : null;
          const finalModel = pickModel ? pickModel : null;

          // 组装请求体(聊天界面为单模型选择)
          const body = {
            message: text,
            session_id: App.sessionId,
            stream: true,
            provider: finalProvider,
            model: finalModel,
            project_path: App.currentProject?.path || '',
            ...App.Chat.modeParams(),
          };
          res = await App.api('/v1/chat/stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(App.Attach ? App.Attach.patchBody(body) : body),
            signal,
          });
      } catch (fetchErr) {
        if (signal.aborted) throw fetchErr; // 用户主动停止，不重试
        retries++;
        if (retries >= 3) throw fetchErr;
        await new Promise(r => setTimeout(r, 1000 * retries));
      }
    }
    clearTimeout(connectTimer); // 连接已建立,取消建连超时
    if (!res.ok || !res.body) {
      console.error('[doSend] HTTP error:', res.status, res.statusText);
      // 后端对"所选模型不可用"等业务错误返回 400+中文 detail,优先展示
      let detail = '';
      try {
        const data = await res.json();
        if (data && data.detail) detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
      } catch (_) { /* 无 JSON 体 */ }
      throw new Error(detail || `HTTP ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let value = null; // reader.read() 返回的 Uint8Array 块,显式声明避免隐式全局变量
    // 不设硬超时:agent 可能需要很长时间,让 SSE 连接一直开着
    // 心跳机制(60 秒)保证空闲时不断线,agent 完成后自然结束

    // 重置空闲计时器:每次收到事件都续期,区分"卡死"与"正在干重活(慢但持续活跃)"
    function resetStreamTimeout() {
      if (streamTimeout !== null) clearTimeout(streamTimeout);
      streamTimeout = setTimeout(() => {
        streamTimeout = null;
        if (!done && !isError) {
          done = true;
          isError = true;
          cleanupStream();
          if (App.abortController) App.abortController.abort(new DOMException('StreamTimeout', 'TimeoutError'));
          App.clearImageGenerating();
          if (typingShown) App.removeTyping();
          App.appendMessage('响应超时,请稍后重试', 'error');
          _finishGroup();
        }
      }, 180000); // 180s 空闲间隙 (原 120s，搜索等耗时操作需要更多时间)
    }
    resetStreamTimeout();
    // 绝对上限:即使后端持续发事件/心跳续期空闲计时器,10 分钟仍未结束则强制断开,
    // 防止永久挂住(配合后端 SSE 哨兵修复,双保险)。收到任何事件时不清此计时器。
    if (hardTimeout !== null) clearTimeout(hardTimeout);
    hardTimeout = setTimeout(() => {
      hardTimeout = null;
      if (!done && !isError) {
        done = true;
        isError = true;
        cleanupStream();
        if (App.abortController) App.abortController.abort(new DOMException('HardTimeout', 'TimeoutError'));
        App.clearImageGenerating();
        if (typingShown) App.removeTyping();
        App.appendMessage('会话超时(超过 10 分钟未完成),已中止。可重试。', 'error');
        _finishGroup();
      }
    }, 600000);

    while (true) {
      let isDone;
      try {
        const result = await reader.read();
        if (App.DEBUG) console.log('[SSE read]', 'done=', result.done, 'bytes=', result.value ? result.value.byteLength : 0);
        ({ done: isDone, value } = result);
      } catch (readErr) {
        // 连接异常(如用户停止、切换会话、后端非正常关闭、代理超时).
        // 即使收到过事件,连接中断仍意味着流未正常完成,标记为错误以便 finally 块提示用户.
        if (!receivedAnyEvent) isError = true;
        else if (!done && !isError) {
          // 已收到过事件但连接中断(如代理超时):流未正常结束,需提示用户
          isError = true;
        }
        console.warn('[SSE read error]', readErr && readErr.message);
        break;
      }
      if (isDone) {
        if (App.DEBUG) console.log('[SSE] FINAL buffer:', buffer.substring(0, 500));
        break;
      }
      buffer += decoder.decode(value, { stream: true });

      const parts = buffer.split('\n\n');
      buffer = parts.pop();
      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith('data: ')) continue;
        try {
          const evt = JSON.parse(line.slice(6));
          receivedAnyEvent = true; // 标记已收到合法 SSE 事件
          if (App.DEBUG) console.log('[SSE]', evt.type, evt.subtype || '', evt.provider || '', evt.content ? evt.content.substring(0, 30) : '');
          // 调试: 记录心跳接收情况
          if (evt.type === 'heartbeat') {
            if (App.DEBUG) console.log('[SSE] Heartbeat received, resetting stream timeout');
          }
          resetStreamTimeout(); // 收到任意事件即续期空闲计时器,避免慢任务被误杀
          // heartbeat 事件:后端发送保活信号,前端仅续期超时但不显示
          if (evt.type === 'heartbeat') {
            // resetStreamTimeout() 已在上面调用,这里仅记录日志
            if (App.DEBUG) console.log('[SSE] Heartbeat processed');
            continue;
          }
          // 图片生成:尽早展示专属"生成中"进度卡。tool_call_start/tool_start 为主信号,
          // tool_call_delta / tool_route 为兜底(二者都带工具名,且一定早于结果事件)
          const _toolName = evt.name || evt.tool || '';
          const _toolNames = evt.tools || evt.names || [];
          if ((evt.type === 'tool_call_start' && _toolNames.indexOf('generate_image') !== -1) ||
              (evt.type === 'tool_start' && _toolNames.indexOf('generate_image') !== -1) ||
              (evt.type === 'tool_call_delta' && _toolName === 'generate_image') ||
              (evt.type === 'tool_route' && _toolNames.indexOf('generate_image') !== -1)) {
            App.showImageGenerating();
          }
          // 执行轨迹:实时显示每个工具调用
          // tool_call_start: 批量预填充工具开始状态(并行调用时提前显示)
          if (evt.type === 'tool_call_start') {
            const tools = evt.tools || [];
            for (const t of tools) {
              const tName = t.name || '';
              const tArgs = t.arguments || '';
              App.addToolStepIfNew(App._currentBotMsg, tName, tArgs, 'running', null);
            }
            _showRail();
          }
          // 去重记录:避免同一工具结果重复显示(tool_done 和 tool 事件都可能触发)
          if (evt.type === 'tool_start') {
            // tool_start 是兼容旧事件的兜底处理
            const children = App.els.messages.children;
            let existingRunning = null;
            for (let i = children.length - 1; i >= 0; i--) {
              const n = children[i];
              if (n.classList.contains('msg') && n.classList.contains('tool') &&
                  n.querySelector('.tool-label') &&
                  n.querySelector('.tool-label').textContent.includes(evt.name || evt.tool || '') &&
                  !n.classList.contains('done') && !n.classList.contains('fail')) {
                existingRunning = n;
                break;
              }
            }
            if (!existingRunning) {
              App.ExecTrace.addStep(evt.name || evt.tool || '工具', evt.arguments || '', 'running');
            }
          } else if (evt.type === 'tool') {
            // tool 事件: 显示工具调用步骤
            App.removeTyping();
            const ok = !String(evt.result || '').includes('Error') &&
                       !String(evt.result || '').includes('failed');
            const toolName = evt.name || evt.tool || _toolName;
            const toolArgs = evt.arguments || '';
            App.addToolStepIfNew(App._currentBotMsg, toolName, toolArgs, ok ? 'done' : 'fail', evt.result);

            // 图片生成完成:立即展示图片到进度卡片
            if (toolName === 'generate_image' && App.finishImageGenerating) {
              App.finishImageGenerating(evt.result || '');
            }
            App.updateRailStatus(App._currentBotMsg, ok ? 'done' : 'error',
              `${toolName} ${ok ? '✓' : '✗'}`);
            _showRail();
          } else if (evt.type === 'tool_done') {
            // tool_done 事件:仅更新轨迹状态,不重复展示结果
            // (如果前端已收到过 tool 事件,会跳过展示;否则作为兜底处理)
            const doneToolName = evt.tool || _toolName;
            // 图片生成完成:立即展示图片到进度卡片
            if (doneToolName === 'generate_image' && App.finishImageGenerating) {
              App.finishImageGenerating(evt.result || '');
            }
            // 检查是否已有该工具的显示记录,避免重复
            const msgs = App.els.messages;
            const toolChildren = msgs.children;
            let lastToolMsg = null;
            for (let i = toolChildren.length - 1; i >= 0; i--) {
              const n = toolChildren[i];
              if (n.classList.contains('msg') && n.classList.contains('tool') &&
                  n.querySelector('.tool-label') &&
                  n.querySelector('.tool-label').textContent.includes(doneToolName)) {
                lastToolMsg = n;
                break;
              }
            }
            if (!lastToolMsg) {
              // 没有重复记录,才显示结果
              App.ExecTrace.updateStep(doneToolName, 'done', '');
              const imgUrl = App.parseImageUrl(evt.result);
              if (doneToolName === 'generate_image' && imgUrl) {
                const toolEl = document.createElement('div');
                toolEl.className = 'tool-inline';
                toolEl.innerHTML = `<img class="tool-result-img" src="${App.escapeHtml(imgUrl)}" alt="生成图片">`;
                _insertBeforeAnswer(toolEl);
              } else if (evt.result) {
                // 统一使用 addStep 渲染,保持与历史消息一致
                App.addToolStepIfNew(App._currentBotMsg, doneToolName, evt.arguments || '', 'done', evt.result);
              }
              _showRail();
            } else {
              // 已有记录,仅更新轨迹状态
              App.ExecTrace.updateStep(doneToolName, 'done', '');
            }
          } else if (evt.type === 'orchestration') {
            // 多智能体编排进度:更新 process-rail 摘要
            App.removeTyping();
            const flowName = { sequential: '顺序编排', hierarchical: '层级编排', consensual: '共识编排' };
            const p = evt.phase;
            let summary = '';
            if (p === 'start') {
              summary = `🔀 启动${flowName[evt.flow] || evt.flow} · 编排进行中`;
            } else if (p === 'task_start') {
              summary = `🔀 子任务 ${evt.index + 1}/${evt.total} · ${evt.agent}`;
            } else if (p === 'task_done') {
              summary = `✅ 子任务 ${evt.index + 1} 完成`;
            } else if (p === 'parallel_start') {
              summary = `🔀 并行 ${evt.candidates ? evt.candidates.length : '?'} 体运行中`;
            } else if (p === 'voting') {
              summary = `🗳️ 投票择优中…`;
            } else if (p === 'vote_result') {
              summary = `🏆 ${evt.winner || '已完成'}`;
            } else if (p === 'task_error') {
              summary = `⚠️ 子任务失败`;
            } else if (p === 'end') {
              summary = `✅ ${flowName[evt.flow] || evt.flow} 编排完成`;
            }
            _showRail();
            const lastRail = App.els.messages.querySelector('.msg.bot .process-rail:last-child');
            if (lastRail && lastRail._summary) {
              lastRail._summary.textContent = summary;
            } else if (lastRail) {
              lastRail.querySelector('.rail-summary').textContent = summary;
            }
          } else if (evt.type === 'plan') {
            if (typingShown) { App.removeTyping(); typingShown = false; }
            _showRail();
            const planTitle = evt.plan?.steps?.[0]?.title || '规划步骤';
            const planEl = document.createElement('div');
            planEl.className = 'plan-inline';
            planEl.innerHTML = `<span class="plan-label">📋 ${App.escapeHtml(planTitle)}</span>`;
            _insertBeforeAnswer(planEl);
          } else if (evt.type === 'tool_route') {
            // 工具路由: 显示预选了哪些工具
            if (typingShown) { App.removeTyping(); typingShown = false; }
            _showRail();
            const routeTools = (evt.tools || []).slice(0, 5);
            const routeEl = document.createElement('div');
            routeEl.className = 'tool-inline';
            routeEl.innerHTML = `<div class="tool-label">🔀 工具路由 · ${routeTools.length} 个工具</div>`
              + (routeTools.length ? `<div class="tool-result" style="max-height:none;">${routeTools.map(t => App.escapeHtml(t)).join(', ')}</div>` : '')
              + (evt.reason ? `<div class="tool-args">${App.escapeHtml(String(evt.reason).slice(0, 100))}</div>` : '');
            _insertBeforeAnswer(routeEl);
          } else if (evt.type === 'thinking') {
            App.removeTyping();
            _showRail();
            // ⚠️ 后端可能把 <thinking>...</thinking> 整块放在 content 里一起返回,
            // 也可能只返回纯思考文本。统一 strip 掉标签, 防止 </thinking> 残留到渲染层。
            const rawContent = evt.content || '';
            const thinkText = App.stripThinkingTags ? App.stripThinkingTags(rawContent) : rawContent;
            if (!thinkText.trim()) {
              // 思考为空:确保 bot 消息已创建, 否则后续 token 无法附着
              if (!App._currentBotMsg) {
                const botMsg = App.createBotMessage();
                answerDiv = botMsg ? botMsg._answer : null;
                App._currentBotMsg = botMsg || null;
                if (answerDiv) answerDiv.style.display = '';
                App._answerWrap = answerDiv ? { _answer: answerDiv, _rail: botMsg?._rail } : null;
              }
              App.updateRailStatus(App._currentBotMsg, 'thinking', '思考中...');
              continue;
            }
            // 确保 bot 消息已创建, 否则思考内容无处附着
            if (!App._currentBotMsg) {
              const botMsg = App.createBotMessage();
              answerDiv = botMsg ? botMsg._answer : null;
              App._currentBotMsg = botMsg || null;
              if (answerDiv) answerDiv.style.display = '';
              App._answerWrap = answerDiv ? { _answer: answerDiv, _rail: botMsg?._rail } : null;
            }
            // 使用规范渲染器
            if (App._currentBotMsg && App.addThinking) {
              App.addThinking(App._currentBotMsg, thinkText);
            }
            App.updateRailStatus(App._currentBotMsg, 'thinking', '思考中...');
          } else if (evt.type === 'token') {
            // 第一次 token 到达:显示并移除打字指示器,创建答案容器
            if (!typingShown) {
              App.removeTyping();
              typingShown = true;
              // 移除打字指示器
              const msgs = App.els.messages;
              const typingEl = msgs.querySelector('#typing');
              if (typingEl) {
                typingEl.remove();
              } else {
                const msgChildren = msgs.children;
                for (let i = msgChildren.length - 1; i >= 0; i--) {
                  const n = msgChildren[i];
                  if (n.classList.contains('msg') && n.classList.contains('bot') && n.textContent.trim() === '正在思考中...') {
                    n.remove();
                    break;
                  }
                }
              }
            }
            if (!answerDiv) {
              // 创建可见的空 bot 消息 (使用规范渲染器)
              const botMsg = App.createBotMessage();
              answerDiv = botMsg ? botMsg._answer : null;
              App._currentBotMsg = botMsg || null;
              if (answerDiv) answerDiv.style.display = '';
              App._answerWrap = answerDiv ? { _answer: answerDiv, _rail: botMsg?._rail } : null;
            }
            receivedTokens = true;
            const { text, thinking } = processTokenChunk(evt.content || '');
            if (thinking) {
              const thinkText = App.stripThinkingTags ? App.stripThinkingTags(thinking) : thinking;
              if (App._currentBotMsg && App.addThinking) {
                App.addThinking(App._currentBotMsg, thinkText);
              }
            }
            if (text) {
              // 防护: 即使 processTokenChunk 未捕获, 也剥离残留 thinking 标签
              const cleanText = App.stripThinkingTags ? App.stripThinkingTags(text) : text;
              // 英文 token 间距修复:LLM token 之间可能无空格,在拼接时自动补空格
              if (cleanText && App._mdBuf) {
                const lastChar = App._mdBuf.charAt(App._mdBuf.length - 1);
                const firstChar = cleanText.charAt(0);
                const isLetterOrDigit = c => /[a-zA-Z0-9]/.test(c);
                if (isLetterOrDigit(lastChar) && isLetterOrDigit(firstChar)) {
                  App._mdBuf += ' ';
                }
              }
              App._mdBuf += cleanText;
              // 防止 _mdBuf 无限增长:超过 200KB 时截断头部(保留最近内容)
              const MAX_MD_BUF = 200000;
              if (App._mdBuf.length > MAX_MD_BUF) {
                App._mdBuf = App._mdBuf.slice(App._mdBuf.length - MAX_MD_BUF);
              }
              console.log('[token] text_len:', cleanText.length, 'buf_len:', App._mdBuf.length, 'calling scheduleRender');
              scheduleRender();
            }
          } else if (evt.type === 'error') {
            done = true;
            isError = true;
            inThinking = false;
            thinkingBuf = '';
            App.clearImageGenerating();
            cleanupStream();
            if (typingShown) App.removeTyping();
            const errBotMsg = App._currentBotMsg || (App.els && App.els.messages && App.els.messages.querySelector('.msg.bot:last-child, .msg.msg-bot:last-child'));
            if (errBotMsg) {
              errBotMsg.classList.add('no-animation');
              errBotMsg.querySelectorAll('.process-rail, .step-counter, .steps-flow').forEach(el => { el.style.display = 'none'; });
            }
            if (answerDiv) {
              const md = App._mdBuf || '';
              let h = '';
              try { h = App.renderMarkdown(md); }
              catch (e) {
                console.error('[error handler renderMarkdown ERROR:', e);
                h = '<p style="white-space:pre-wrap;">' + App.escapeHtml(md) + '</p>';
              }
              answerDiv.innerHTML = h;
            }
            App.ExecTrace.finish(answerDiv ? (answerDiv.textContent || '') : null);
            renderDone = true; // 错误路径最终渲染完成后标记
            // 同一次请求里后端可能对同一失败发多条相同 error(多阶段各自上报),只提示一次
            const errMsg = evt.message || '发生错误';
            if (errMsg !== lastErrMsg) {
              lastErrMsg = errMsg;
              App.appendMessage(errMsg, 'error');
            }
            _finishGroup();
            break; // 流已结束,退出事件处理循环
          } else if (evt.type === 'done') {
            done = true;
            // 注意:renderDone 延迟到最终渲染之后设置,避免跳过待处理的 scheduleRender
            // 强制退出 thinking 模式:防止 </thinking> 丢失导致内容被吞
            inThinking = false;
            thinkingBuf = '';
            App.clearImageGenerating();
            cleanupStream();
            if (typingShown) App.removeTyping();
            // 无条件隐藏流式专属元素,与历史消息样式统一
            const botMsg = App._currentBotMsg || (App.els && App.els.messages && App.els.messages.querySelector('.msg.bot:last-child, .msg.msg-bot:last-child'));
            if (botMsg) {
              botMsg.classList.add('no-animation');
              botMsg.querySelectorAll('.process-rail, .step-counter, .steps-flow').forEach(el => { el.style.display = 'none'; });
              const think = botMsg.querySelector('.thinking-section');
              if (think && !think.classList.contains('open')) think.style.display = 'none';
            }
            // 流式完成后整段重渲染,确保最终内容(含跨切片块级结构)正确
            if (answerDiv) {
              const rawMd = App._mdBuf || '';
              const finalMd = (App.stripThinkingTags ? App.stripThinkingTags(rawMd) : rawMd)
                .replace(/\n{3,}/g, '\n\n').trim();
              let finalHtml = '';
              try {
                finalHtml = App.renderMarkdown(finalMd);
                console.log('[done] renderMarkdown ok, html_len:', finalHtml.length, 'preview:', finalHtml.slice(0, 150));
              } catch (renderErr) {
                console.error('[renderMarkdown] done handler ERROR:', renderErr);
                finalHtml = '<p>' + App.escapeHtml(finalMd) + '</p>';
              }
              answerDiv.innerHTML = finalHtml;
              if (App._currentBotMsg) {
                App._currentBotMsg._answer = answerDiv;
              }
            } else if (App._currentBotMsg) {
              const placeholder = App._currentBotMsg.querySelector('.answer-doc');
              if (placeholder && !placeholder.textContent.trim()) {
                placeholder.innerHTML = '<p style="color:var(--text-tertiary);font-style:italic;">思考完成, 等待回答...</p>';
              }
            }
            renderDone = true; // 最终渲染完成后才标记,防止跳过待处理的 scheduleRender
            // ── 从 done 事件的 steps 中补全执行轨迹(后端聚合结果,兜底未增量上报的步骤) ──
            if (evt.steps && Array.isArray(evt.steps) && evt.steps.length > 0) {
              for (const step of evt.steps) {
                if (!step || typeof step !== 'object') continue;
                const sName = step.name || step.tool || (typeof step === 'string' ? step : '步骤');
                const sArgs = step.arguments || step.args || '';
                let sStatus = step.status || (step.result !== undefined ? 'done' : 'running');
                if (sStatus === 'failed' || sStatus === 'error') sStatus = 'fail';
                else if (sStatus !== 'running' && sStatus !== 'fail') sStatus = 'done';
                App.ExecTrace.addStep(sName, sArgs, sStatus);
              }
            }
            // 关闭分组并传入最终答案文本
            _finishGroup(answerDiv ? (answerDiv.textContent || '') : null);
            // 关闭执行轨迹面板
            App.ExecTrace.finish(answerDiv ? (answerDiv.textContent || '') : null);
            // 截断检测:finish_reason=length 表示 LLM 输出被 max_tokens 截断
            if (evt.finish_reason === 'length' && answerDiv) {
              const truncDiv = document.createElement('div');
              truncDiv.className = 'truncation-warning';
              truncDiv.style.cssText = 'margin-top:8px;padding:8px 12px;background:rgba(251,191,36,0.1);border-left:3px solid #f59e0b;border-radius:4px;font-size:13px;color:#f59e0b;';
              truncDiv.textContent = '⚠ 回复被截断:已达到输出长度上限。可尝试缩短上下文或增加 max_output_tokens。';
              answerDiv.appendChild(truncDiv);
            }
            // 检查是否有待确认的中断
            const intr = evt.interrupt;
            if (intr && intr.type === 'confirmation' && intr.requests) {
              if (App.Confirmation && App.Confirmation.show) {
                App.Confirmation.show(intr.requests, intr.run_id);
                // 记录中断集合,批准后自动续跑
                App.Confirmation.trackInterrupt({ run_id: intr.run_id, interrupt: intr });
              }
            } else if (evt.error) {
              // 后端报告了错误(如 LLM 上下文溢出、节点失败等)
              App.appendMessage(`执行出错: ${evt.error}`, 'error');
            } else if (!receivedTokens && (!evt.steps || !Array.isArray(evt.steps) || evt.steps.length === 0)) {
              // 没有 token,也没有 steps,也没有 interrupt:后端返回了完全空的响应
              App.refreshWelcomePage();
              App.appendMessage('未收到响应,请重试', 'error');
            }
            break; // 流已完成,退出事件处理循环
          } else if (evt.type === 'cancelled') {
            done = true;
            inThinking = false;
            thinkingBuf = '';
            App.clearImageGenerating();
            cleanupStream();
            if (typingShown) App.removeTyping();
            renderDone = true;
            App.appendMessage('已取消', 'error');
            break;
          } else {
            // 兜底:未识别的事件类型 → 仅日志,绝不允许原始事件字符串进入聊天内容或 mdBuf
            if (App.DEBUG) console.log('[SSE] unhandled event type:', evt.type, 'keys:', Object.keys(evt));
          }
        } catch (e) {
          // JSON 解析或事件分发失败:仅日志,绝不把原始 buffer/line 追加到聊天 UI 或 mdBuf
          console.warn('[SSE] 解析事件失败:', e && e.message ? e.message : e);
          // 只记录末尾 500 字符,避免超长日志污染控制台(substring 负数按 0 处理,改用 slice)
          const tail = buffer.length > 500 ? '...' + buffer.slice(-500) : buffer;
          console.warn('  相关片段:', tail);
        }
      }
    }

    // ── 流关闭后:处理未以 \n\n 结尾的残余事件(部分代理/上游会省略最后一个分隔符) ──
    // 典型场景:后端 StreamingResponse 直接关闭连接前未输出尾部 \n\n,最后一个 done/error 事件被遗漏
    if (buffer && buffer.trim()) {
      try {
        buffer += '\n\n';  // 人为补齐分隔符,保证 split 能切出最后一条
        const tailParts = buffer.split('\n\n');
        buffer = '';
        for (const part of tailParts) {
          const line = part.trim();
          if (!line) continue;
          // 解析:接受 'data: {json}' 或裸 '{json}' 两种(极少数不规范 SSE 会省略 data: 前缀)
          let jsonStr = line;
          if (jsonStr.startsWith('data: ')) {
            jsonStr = jsonStr.slice(6);
          } else if (!jsonStr.startsWith('{') && !jsonStr.startsWith('[')) {
            continue;  // SSE comment / id / event 字段,跳过
          }
          let evt;
          try { evt = JSON.parse(jsonStr); }
          catch (parseErr) {
            // 非 JSON 残余 → 丢弃,不泄露到 UI
            if (App.DEBUG) console.log('[SSE flush] 丢弃非 JSON 残余:', jsonStr.substring(0, 100));
            continue;
          }
          if (!evt || typeof evt !== 'object' || !evt.type) continue;
          receivedAnyEvent = true;
          // 残余数据的分发:仅处理影响最终状态的关键事件
          // 中间事件(tool_start/plan/thinking/orchestration 等)流结束后补处理已无意义
          if (evt.type === 'done' && !done) {
            done = true;
            renderDone = true; // 必须在 cleanupStream 之前设置
            inThinking = false;
            thinkingBuf = '';
            App.clearImageGenerating();
            cleanupStream();
            if (typingShown) App.removeTyping();
            if (answerDiv) {
              const rawMd = App._mdBuf || '';
              const finalMd = (App.stripThinkingTags ? App.stripThinkingTags(rawMd) : rawMd)
                .replace(/\n{3,}/g, '\n\n').trim();
              let finalHtml = '';
              try { finalHtml = App.renderMarkdown(finalMd); }
              catch (e) {
                console.error('[SSE flush done renderMarkdown ERROR:', e);
                finalHtml = '<p>' + App.escapeHtml(finalMd) + '</p>';
              }
              answerDiv.innerHTML = finalHtml;
            }
            // 补全执行轨迹
            if (evt.steps && Array.isArray(evt.steps) && evt.steps.length > 0) {
              for (const step of evt.steps) {
                if (!step || typeof step !== 'object') continue;
                const sName = step.name || step.tool || '步骤';
                const sArgs = step.arguments || step.args || '';
                let sStatus = step.status || (step.result !== undefined ? 'done' : 'running');
                if (sStatus === 'failed' || sStatus === 'error') sStatus = 'fail';
                else if (sStatus !== 'running' && sStatus !== 'fail') sStatus = 'done';
                App.ExecTrace.addStep(sName, sArgs, sStatus);
              }
            }
            _finishGroup(answerDiv ? (answerDiv.textContent || '') : null);
            App.ExecTrace.finish(answerDiv ? (answerDiv.textContent || '') : null);
            // 截断检测(残余 buffer 路径)
            if (evt.finish_reason === 'length' && answerDiv) {
              const truncDiv = document.createElement('div');
              truncDiv.className = 'truncation-warning';
              truncDiv.style.cssText = 'margin-top:8px;padding:8px 12px;background:rgba(251,191,36,0.1);border-left:3px solid #f59e0b;border-radius:4px;font-size:13px;color:#f59e0b;';
              truncDiv.textContent = '⚠ 回复被截断:已达到输出长度上限。可尝试缩短上下文或增加 max_output_tokens。';
              answerDiv.appendChild(truncDiv);
            }
            const intr = evt.interrupt;
            if (intr && intr.type === 'confirmation' && intr.requests) {
              if (App.Confirmation && App.Confirmation.show) {
                App.Confirmation.show(intr.requests, intr.run_id);
                App.Confirmation.trackInterrupt({ run_id: intr.run_id, interrupt: intr });
              }
            } else if (evt.error) {
              App.appendMessage(`执行出错: ${evt.error}`, 'error');
            } else if (!receivedTokens && (!evt.steps || !Array.isArray(evt.steps) || evt.steps.length === 0)) {
              App.refreshWelcomePage();
              App.appendMessage('未收到响应,请重试', 'error');
            }
            break; // 流已完成,退出残余处理循环
          } else if (evt.type === 'error' && !done && !isError) {
            done = true; isError = true;
            inThinking = false;
            thinkingBuf = '';
            App.clearImageGenerating(); cleanupStream();
            if (typingShown) App.removeTyping();
            if (answerDiv) {
              const md = App._mdBuf || '';
              let h = '';
              try { h = App.renderMarkdown(md); }
              catch (e) {
                console.error('[flush error renderMarkdown ERROR:', e);
                h = '<p style="white-space:pre-wrap;">' + App.escapeHtml(md) + '</p>';
              }
              answerDiv.innerHTML = h;
            }
            App.ExecTrace.finish(answerDiv ? (answerDiv.textContent || '') : null);
            renderDone = true;
            const errMsg = evt.message || '发生错误';
            if (errMsg !== lastErrMsg) { lastErrMsg = errMsg; App.appendMessage(errMsg, 'error'); }
            _finishGroup();
            break; // 流已完成,退出残余处理循环
          } else if (evt.type === 'token') {
            // 残余 token 片段:执行最少必要的追加与渲染(不在此处创建 answerDiv,避免残留)
            if (answerDiv) {
              receivedTokens = true;
              const { text, thinking } = processTokenChunk(evt.content || '');
              if (thinking) {
                const thinkText = App.stripThinkingTags ? App.stripThinkingTags(thinking) : thinking;
                App.appendThinking(thinkText);
              }
              if (text) { const cleanText = App.stripThinkingTags ? App.stripThinkingTags(text) : text; if (cleanText && App._mdBuf) { const lastChar = App._mdBuf.charAt(App._mdBuf.length - 1); const firstChar = cleanText.charAt(0); if (/[a-zA-Z0-9]/.test(lastChar) && /[a-zA-Z0-9]/.test(firstChar)) { App._mdBuf += ' '; } } App._mdBuf += cleanText; if (App._mdBuf.length > 200000) { App._mdBuf = App._mdBuf.slice(-200000); } scheduleRender(); }
            }
          } else if (evt.type === 'heartbeat') {
            // no-op
          } else if (!done) {
            if (App.DEBUG) console.log('[SSE flush] 跳过残余中间事件:', evt.type);
          }
        }
      } catch (flushErr) {
        console.warn('[SSE flush] 残余处理异常:', flushErr && flushErr.message ? flushErr.message : flushErr);
        buffer = '';
      }
    }

    // 记录流开始前的滚动比例，用于流式完成后判断是否应跟随滚动
    const scrollRatioBefore = App.els.messages.scrollHeight === 0 ? 1 : App.els.messages.scrollTop / (App.els.messages.scrollHeight - App.els.messages.clientHeight);

    // 清理流超时计时器
    cleanupStream();
    App.clearImageGenerating(); // 任何流终止都清理图片生成卡片,避免残留

    // ── 滚动锚定：只有用户已在底部(<48px)时才跟随，否则显示“回到底部”提示 ──
    const atBottomNow = App.els.messages.scrollHeight - App.els.messages.scrollTop <= App.els.messages.clientHeight * 0.95;
    const wasAtBottomBefore = scrollRatioBefore > 0.95; // 流开始前用户是否在底部
    if (wasAtBottomBefore || atBottomNow) {
      // 用户在流开始前或当前就在底部，允许自动跟随
      App.scrollMessagesToBottom(true);
    } else {
      // 用户在上方浏览，保持位置不变，显示返回按钮
      const pill = document.createElement('div');
      pill.id = 'scroll-pill';
      pill.style.cssText = `
        position: fixed; right: 24px; bottom: 24px;
        width: 40px; height: 40px; border-radius: 50%;
        background: var(--primary); display: flex; align-items: center; justify-content: center;
        font-size: 16px; color: #fff; box-shadow: var(--shadow-sm);
        z-index: 100; opacity: 0; transition: opacity 0.3s;
        cursor: pointer;
      `;
      pill.textContent = '↓';
      pill.title = '回到底部';
      pill.addEventListener('click', () => {
        App.scrollMessagesToBottom(true);
        pill.remove();
      });
      // 移除现有的 pill (防止重复)
      const old = document.getElementById('scroll-pill');
      if (old) old.remove();
      document.body.appendChild(pill);
      setTimeout(() => pill.style.opacity = '1', 100);
    }

    // 流被异常中断:直接显示错误,不再回退非流式接口
    // 场景1: 从未收到任何事件 → "响应中断"
    // 场景2: 收到过事件(工具执行等)但流未正常完成(连接中断/后端异常) → "执行中断"
    if (!done && !isError) {
      App.removeTyping();
      if (renderTimer !== null) { cancelAnimationFrame(renderTimer); renderTimer = null; }
      if (receivedAnyEvent) {
        App.appendMessage('执行中断,结果可能不完整。请检查上下文长度后重试', 'error');
      } else {
        App.appendMessage('响应中断,请稍后重试', 'error');
      }
      _finishGroup();
    }
  } catch (e) {
    // AbortError 由 stop() 处理(已追加提示),这里仅做静默清理
    cleanupStream();
    if (typingShown) App.removeTyping();
    if (e.name !== 'AbortError') {
      App.appendMessage(`请求失败: ${e.message}`, 'error');
      _finishGroup();
    } else {
      _finishGroup();
    }
  } finally {
    /* 代次保护: 仅当本轮仍是"最新发送"时才重置全局 streaming 状态。
       若已被更新的 doSend 取代(旧流被 abort), 跳过 setStreaming(false)/stopBtn/
       sendBtn 强制复位, 避免旧 finally 覆盖新流的 streaming=true + stopBtn.show
       状态 —— 这正是"再次发送后停止按钮消失、不知可否发送"的根因。 */
    const isStale = (App.Chat._sendSeq || 0) !== mySeq;
    App.Chat._streamingStart = null;
    if (streamingSet && !isStale) App.Chat.setStreaming(false);
    clearTimeout(connectTimer); // 兜底清理建连超时(仅本轮 timer, 幂等)
    // 确保分组被关闭
    if (App._currentGroup) {
      try { _finishGroup(answerDiv ? (answerDiv.textContent || '') : null); } catch (_) {}
    }
    // 重置 mdBuf 防止下次流式时累积
    App._mdBuf = '';
    renderDone = false;
    _lastRenderedHtml = '';
    inThinking = false;
    thinkingBuf = '';
    /* 代次保护: 旧流不得强制复位 sendBtn/streaming, 否则新流刚显示的 stopBtn
       会被 setStreaming(false) 隐藏。仅最新代次才启用按钮 + 同步 streaming。 */
    if (!isStale) {
      if (App.els.sendBtn) {
        App.els.sendBtn.disabled = !((App.els.input) && App.els.input.value.trim());
        setTimeout(() => {
          if (App.els.sendBtn && !App.streaming && (App.Chat._sendSeq || 0) === mySeq) {
            App.els.sendBtn.disabled = !((App.els.input) && App.els.input.value.trim());
          }
        }, 0);
      }
    }
    if (!isStale) App.abortController = null;
    if (App.Attach) App.Attach.reset();
    if (App.CtxMeter) App.CtxMeter.refresh();
    if (!isStale) App.els.input.focus();
  }
};

App.Chat.stop = function (silent) {
  App.Chat._streamingStart = null;
  if (App.abortController) {
    try { App.abortController.abort(); } catch (_) { /* 忽略 */ }
  }
  /* 代次保护: stop() 会主动 abort 当前流, 该 doSend 的 finally 因 isStale 判定
     不会复位 streaming(避免与 stop 竞争)。这里由 stop() 自己负责复位全局状态。 */
  App.Chat.setStreaming(false);
  if (App.els.sendBtn) {
    App.els.sendBtn.disabled = !((App.els.input) && App.els.input.value.trim());
  }
  if (!silent) {
    App.removeTyping();
    App.appendMessage('⏹ 已停止生成', 'bot');
  }
  // 停止时也完成分组
  if (App._currentGroup) _finishGroup();
  App.abortController = null;
};

App.Chat.setStreaming = function (val) {
  App.streaming = val;
  /* 修复"发送后不能停止/不知道是否可发送": 旧实现用
     document.body.className = className.replace(...) 同步 class,
     会把 body 上所有其他 class(sidebar-collapsed/side-open/has-messages 等)全部清掉,
     导致布局塌掉且后续 class 判断失效。现改用 classList 精确增删, 绝不动其他 class */
  if (val) {
    document.body.classList.add('streaming');
  } else {
    document.body.classList.remove('streaming');
  }
  /* 修复"不能停止": stopBtn 显隐改由 JS 直接控制 .show class, 不依赖 body.streaming,
     彻底与 body class 同步解耦(旧实现在 streaming class 被清掉后 stopBtn 永远不可见) */
  if (val) {
    if (App.els.stopBtn) App.els.stopBtn.classList.add('show');
  } else {
    if (App.els.stopBtn) App.els.stopBtn.classList.remove('show');
  }
  // val=true 时禁用 sendBtn + 显示 stopBtn; val=false 时按输入内容启用 sendBtn + 隐藏 stopBtn
  const inp = App.els.input;
  if (val) {
    if (App.els.sendBtn) App.els.sendBtn.disabled = true;
  } else {
    if (App.els.sendBtn) App.els.sendBtn.disabled = !(inp && inp.value.trim());
  }

  // 更新加载状态
  if (App.UI && App.UI.Status) {
    if (val) {
      App.UI.Status.showLoading('思考中...');
    } else {
      App.UI.Status.clearStatus();
    }
  }
};

/* 输入框自动高度：根据内容行数调整，最多 6 行 */
App.Chat.autoResize = function () {
  const input = App.els.input;
  if (!input) return;
  // 先重置为 auto 以便准确测量 scrollHeight
  input.style.height = 'auto';
  const lh = parseFloat(getComputedStyle(input).lineHeight) || 23;
  const rows = Math.min(6, Math.max(1, Math.ceil(input.scrollHeight / lh)));
  input.rows = rows;
  // 不再设 style.height，让 rows 控制高度
};

/* 监听输入框变化，自动调整高度 — 延迟到 App.els 就绪后执行 */
(function () {
  const setup = () => {
    const input = App.els && App.els.input;
    if (input) {
      input.addEventListener('input', () => App.Chat.autoResize());
      input.addEventListener('focus', () => App.Chat.autoResize());
      App.Chat.autoResize();
    }
  };
  if (App.els && App.els.input) {
    setup();
  } else if (App.UI) {
    // ui-modules.js 已加载，等 cacheElements() 完成后执行
    const origCache = App.UI.cacheElements;
    App.UI.cacheElements = function () {
      origCache.apply(this, arguments);
      setup();
    };
  } else {
    // 兜底：等待 App.UI 出现
    const check = () => {
      if (App.UI) {
        const origCache = App.UI.cacheElements;
        App.UI.cacheElements = function () {
          origCache.apply(this, arguments);
          setup();
        };
      } else {
        setTimeout(check, 50);
      }
    };
    check();
  }
})();

/* ---------- 截屏识图 ---------- */
App.Chat.captureScreen = async function () {
  if (App.streaming) return;
  try {
    if (!(window.agentApp && window.agentApp.captureScreen)) {
      App.appendMessage('当前环境不支持截屏(需 Electron 桌面端)', 'error');
      return;
    }
    App.appendMessage('📸 正在截屏...', 'bot');
    const dataUrl = await window.agentApp.captureScreen();
    if (!dataUrl) {
      App.appendMessage('截屏失败:未获取到屏幕画面', 'error');
      return;
    }
    const res = await fetch(dataUrl);
    const blob = await res.blob();
    const file = new File([blob], `screenshot-${Date.now()}.png`, { type: 'image/png' });
    await App.Chat.recognizeImage(file);
  } catch (e) {
    App.appendMessage(`截屏失败: ${e.message}`, 'error');
  }
};

/* ---------- 图片识别 ---------- */
App.Chat.selectImage = function (file) {
  App.selectedImageFile = file;
  const bar = App.els.imgPreviewBar;
  if (!file) {
    bar.classList.remove('show');
    return;
  }
  const reader = new FileReader();
  reader.onload = (e) => {
    App.previewUrl = e.target.result;
    App.els.previewImg.src = e.target.result;
    App.els.previewName.textContent = file.name;
    bar.classList.add('show');
  };
  reader.readAsDataURL(file);
};

App.Chat.recognizeImage = async function (file) {
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  fd.append('prompt', '请描述这张图片的内容');
  fd.append('session_id', App.sessionId);
  // 与聊天一致:使用左下角模型选择器的当前选择(留空回退后端全局配置)
  const pickBase = App.ModelPicker ? App.ModelPicker.get() : { provider: '', model: '' };
  fd.append('provider', pickBase.provider || '');
  fd.append('model', pickBase.model || '');
  App.appendUserMessage(`🖼 识别图片: ${file.name}`);
  App.showTyping();
  let streamingSet = false;
  try {
    streamingSet = true;
    App.Chat._streamingStart = Date.now();
    App.Chat.setStreaming(true);
    const res = await App.api('/v1/vision', { method: 'POST', body: fd });
    const data = await res.json();
    App.removeTyping();
    if (data.error) {
      App.appendMessage(`❌ ${data.error}`, 'error');
    } else if (data.answer) {
      App.appendMessage(data.answer, 'bot');
    } else {
      App.appendMessage('未能识别图片内容，请确认已选择支持视觉的模型(如 qwen-vl、gpt-4o)', 'bot');
    }
  } catch (e) {
    App.removeTyping();
    App.appendMessage(`图片识别失败: ${e.message}`, 'error');
  } finally {
    // 无论成功/失败都必须重置 streaming 状态,防止按钮卡死
    App.Chat._streamingStart = null;
    if (streamingSet) App.Chat.setStreaming(false);
    App.selectedImageFile = null;
    App.els.imgPreviewBar.classList.remove('show');
    App.els.input.focus();
  }
};
