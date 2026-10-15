/* 聊天模块:SSE 流式对话 / 工具事件 / 停止生成 / 图片识别 */
App.Chat = App.Chat || {};

/* 本次流式所属会话。切换会话时它会连同已渲染内容被转入后台
   (App.Session.park),此时它的后续输出必须写进自己的离屏容器,
   而不是前台的消息区。 */
App.Chat.runSid = '';

/* 本次流式应该写入的消息容器。
   多会话并行下必读:若本次所属会话已切到后台(用户跑去聊别的会话了),
   返回它在 App.Session._runs 里的 pane;否则返回前台 live 容器。
   直接写 App.els.messages 会让后台会话的输出串到当前会话的界面上。 */
App.Chat.pane = function () {
  const sid = App.Chat.runSid;
  if (sid && sid !== App.sessionId && App.Session && App.Session.paneOf) {
    const p = App.Session.paneOf(sid);
    if (p) return p;
  }
  return App.els && App.els.messages;
};

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
  // 记录本次流式归属的会话:后续所有渲染都据此决定写前台还是写它的后台 pane
  App.Chat.runSid = App.sessionId;
  // 本次流式**所属会话**的快照:即便切换会话让 App.sessionId 改变,本流也始终知道
  // 自己属于哪个会话,用于事件循环的"前台才渲染 / 后台静默"判定与 finally 的归属修正。
  const mySid = App.sessionId;
  if (App.syncComposer) App.syncComposer();
  App._planCard = null;
  App.clearImageGenerating();  // 清除上一轮可能残留的"生成中"卡片
  if (App.Confirmation) App.Confirmation.clear();
  App.removeTyping();
  // 快照本次要发送的附件 ID:挂进用户气泡"显示在会话中"(此刻 currentIds 尚未被 finally reset)
  const _attachIds = (App.Attach && App.Attach.currentIds && App.Attach.currentIds.size)
    ? Array.from(App.Attach.currentIds) : [];
  let _userWrap = null;
  if (!opts.skipAppendUser) _userWrap = App.appendUserMessage(text);
  // 把发送的附件卡片(图片缩略图/文件图标+打开)挂进用户气泡,附件不止停在输入框
  if (_attachIds.length && _userWrap && App.Attach && App.Attach.renderToBubble) {
    App.Attach.renderToBubble(_userWrap._content || _userWrap, _attachIds, App.sessionId);
  }
  // 打字指示器延迟显示:等第一个 token 到达再显示,避免无响应时 indicator 闪烁
  // 【关键修复】将 setStreaming(true) 移至 try 块内，确保任何初始化异常都能触发 finally 重置
  _startGroup();  // 开始工具分组

  // 每次 doSend 前清空 mdBuf，避免多次 abort/resume 累积
  App._mdBuf = '';
  // 注意:思考流式状态(pendingTextHold / thinkingEmitted)在下方以 let 声明并初始化,
  // 每次 doSend 都是全新绑定,不存在跨轮残留,切勿在声明之前赋值(TDZ 会抛 ReferenceError)
  // 每轮重置思考卡/答案锚点,避免上一轮残留导致本轮 thinking 错位
  App._thinkingWrap = null;
  App._answerWrap = null;
  // ⚠️ 必须一并重置 _currentBotMsg:thinking 事件分支用 `!App._currentBotMsg`
  // 判断"要不要新建气泡",而 token 分支用的是局部 answerDiv(每轮全新)。
  // 上一轮结束后此值仍指向旧气泡 → 新一轮的思考块(先于首个 token 到达)
  // 会全部挂进上一轮的气泡里(实证 14:27 提问的思考显示在 14:24 的旧气泡顶部)。
  App._currentBotMsg = null;
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
  // 渐进式思考流:已从 thinkingBuf 发出的字符数。
  // 旧实现要等 </thinking> 闭合才一次性返回整个思考块,长思考期间思考区一直空白,
  // 用户会以为"思考内容不见了"。现在每个 chunk 到达即吐出思考增量。
  let thinkingEmitted = 0;
  // 文本模式尾部扣留:chunk 末尾出现未闭合的 '<'(标签被 chunk 边界劈开,如 "<thi")
  // 时先扣住,等下一个 chunk 拼齐后再识别,防止思考开启标签漏检导致思考原文泄漏进正文。
  let pendingTextHold = '';
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
  // 后端 reflect 判定需要重写时置位:保留上一版答案不动,直到新一轮 token 真正到达再替换,
  // 避免 answer_reset 立刻清空界面造成的"显示→消失→又显示"闪烁
  let rewritePending = false;
  // answer_reset 等事件复位思考状态机时,同步复位 system-reminder 吞块状态
  let inSysReminder = false;
  let sysRemLen = 0;
  // 建连超时:api() 自带 60s 超时仅在"未传 signal"时生效,而本函数发流必传 signal,
  // 导致连接建立阶段(后端不可达/未就绪/崩溃)无任何超时保护、永久挂起。
  // 单独加建连超时:超时即 abort 本次 fetch,走重试/报错流程。
  /* 从 token 流中提取 <thinking>...</thinking> 块，避免思考原文进入主答案气泡 */
  function processTokenChunk(chunk) {
    if (!chunk) return { text: '', thinking: '' };
    // 拼回上一轮扣留的未闭合 '<' 片段(标签劈开保护)
    chunk = pendingTextHold + chunk;
    pendingTextHold = '';
    // 剥离 LLM 内部 XML 标签
    // 注意:system 分支带负向先行(?![_-]?reminder)——<system-reminder> 整块由下方
    // 状态机连内容一起吞掉,若在这里只删标签,块内文字(如 "Plan mode: ...")会漏进正文。
    // 工具名伪标签(git_status/list_directory 等):模型以纯文本回显工具调用骨架,
    // 后端 _strip_thinking 的通用剥离会删掉它们,前端若不删会造成
    // "直播流带标签 vs 落库干净"的输出不一致(实测 2026-09-20)。
    chunk = chunk.replace(/<\/?(?:system(?![_-]?reminder)|plan|phase|analysis|reflection|context|instruction|output|reasoning|tool_call|tool_response|function|function_calls|function_results|parameter|observation|scratchpad|git_status|git_diff|git_log|list_directory|read_file|write_file|edit_file|run_terminal|file_history|search_files|web_search)[^>]*>/gi, '');
    // 归一化 thinking 变体标签:<ant:thinking>/<antThinking>/<|thinking|> 等 → 标准 <thinking>/</thinking>,
    // 否则下方 indexOf('<thinking') 识别不到变体,整段思考原文会泄漏进主答案。
    chunk = chunk
      .replace(/<(?:\|)?(?:[a-zA-Z][\w.:-]*)?thinking[\w.:-]*(?:\|)?[^>]*>/gi, '<thinking>')
      .replace(/<(?:\|)?\/(?:\|)?(?:[a-zA-Z][\w.:-]*)?thinking[\w.:-]*(?:\|)?[^>]*>/gi, '</thinking>')
      // 中文变体:部分模型(如 agnes 系列)用 <思考要点>/<思考过程> 等包裹思考内容,
      // 不归一化会原样漏进主答案。连同英文 <think> 一起映射到标准 thinking 标签。
      .replace(/<\s*(\/?)\s*(?:思考[\u4e00-\u9fa5]{0,4}|think\w*)\s*>/g, (_, slash) => slash ? '</thinking>' : '<thinking>')
      // system-reminder 变体归一化:<system_reminder>/<System-Reminder> 等 → 标准标签
      .replace(/<\s*(\/?)\s*system[_-]?reminder[^>]*>/gi, (_, slash) => slash ? '</system-reminder>' : '<system-reminder>');
    // ── 伪 <system-reminder> 指令回显吞块 ──
    // 模型偶尔把训练语料中的系统提醒原文当正文吐出(实测:
    // "<system-reminder>\nPlan mode: think carefully before committing to an approach.\n</system-reminder>"),
    // 整块(含内容)不属于答案,全部吞掉。注意:下方通用标签剥离只删标签本身,
    // 块内文字会漏进正文,必须像 thinking 一样做跨 chunk 状态机。
    if (inSysReminder) {
      sysRemLen += chunk.length;
      if (sysRemLen > 4000) {
        // 未闭合兜底:超过 4000 字符强制退出,防止后续内容被永远吞掉
        console.warn('[processTokenChunk] system-reminder exceeded 4000 chars without closing tag, forcing exit');
        inSysReminder = false;
        sysRemLen = 0;
        return { text: '', thinking: '' };
      }
      const srClose = chunk.indexOf('</system-reminder>');
      if (srClose === -1) {
        // 闭合标签可能被 chunk 边界劈开:末尾悬空 '<' 先扣住
        const lt = chunk.lastIndexOf('<');
        if (lt !== -1 && chunk.indexOf('>', lt) === -1 && (chunk.length - lt) <= 24) {
          pendingTextHold = chunk.slice(lt);
        }
        return { text: '', thinking: '' };
      }
      inSysReminder = false;
      sysRemLen = 0;
      chunk = chunk.slice(srClose + 18);
      if (!chunk) return { text: '', thinking: '' };
      // 闭合后剩余内容继续走下方正常流程
    }
    const srOpen = chunk.indexOf('<system-reminder>');
    if (srOpen !== -1) {
      const before = chunk.slice(0, srOpen);
      inSysReminder = true;
      sysRemLen = 0;
      const rest = processTokenChunk(chunk.slice(srOpen + 17));
      return { text: before + rest.text, thinking: rest.thinking };
    }
    if (!inThinking) {
      // 文本模式尾部扣留:末尾有未闭合的 '<' 时(可能是被劈开的标签开头),扣住等下一个 chunk
      const lt = chunk.lastIndexOf('<');
      if (lt !== -1 && chunk.indexOf('>', lt) === -1) {
        pendingTextHold = chunk.slice(lt);
        chunk = chunk.slice(0, lt);
        if (!chunk) return { text: '', thinking: '' };
      }
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
        // 只有开启标签: 进入 thinking 模式(thinkingBuf 跳过开启标签本身,
        // 后续增量直接是思考正文;闭合时由 thinkingEmitted 保证不重复)
        inThinking = true;
        thinkingBuf = body.slice(body.indexOf('>') + 1);
        thinkingEmitted = 0;
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
    // ── 闭合标签劈开保护 ──
    // 后端逐字符发射 token:'</思考要点>' 等变体闭合标签永远凑不进单条归一化正则
    // → 闭合失败,标签原文裸漏进思考正文,且后续答案内容全部被当成思考。
    // ① 末尾悬空 '<'(可能是被劈开的标签前缀)先扣住不吐;
    // ② '<...>' 凑齐后对这一小段做变体归一化,让下方 </thinking> 闭合检测命中。
    const hangLt = thinkingBuf.lastIndexOf('<');
    if (hangLt !== -1 && thinkingBuf.indexOf('>', hangLt) === -1 && (thinkingBuf.length - hangLt) <= 32) {
      const heldDelta = thinkingBuf.slice(thinkingEmitted, hangLt);
      thinkingEmitted = hangLt;
      return { text: '', thinking: heldDelta };
    }
    const lastGt = thinkingBuf.lastIndexOf('>');
    if (lastGt !== -1) {
      const lastLt = thinkingBuf.lastIndexOf('<', lastGt);
      if (lastLt !== -1 && lastGt - lastLt < 32) {
        const seg = thinkingBuf.slice(lastLt, lastGt + 1);
        const norm = seg
          .replace(/<(?:\|)?(?:[a-zA-Z][\w.:-]*)?thinking[\w.:-]*(?:\|)?[^>]*>/gi, '<thinking>')
          .replace(/<(?:\|)?\/(?:\|)?(?:[a-zA-Z][\w.:-]*)?thinking[\w.:-]*(?:\|)?[^>]*>/gi, '</thinking>')
          .replace(/<\s*(\/?)\s*(?:思考[\u4e00-\u9fa5]{0,4}|think\w*)\s*>/g, (_, s2) => s2 ? '</thinking>' : '<thinking>');
        if (norm !== seg) thinkingBuf = thinkingBuf.slice(0, lastLt) + norm + thinkingBuf.slice(lastGt + 1);
      }
    }
    // 安全限制:thinkingBuf 超过 8000 字符仍未闭合,强制退出 thinking 模式
    // 防止 </thinking> 丢失导致后续所有 token 被吞
    if (thinkingBuf.length > 8000) {
      console.warn('[processTokenChunk] thinkingBuf exceeded 8000 chars without closing tag, forcing exit');
      // 渐进式流已发出 thinkingEmitted 字符,只补发未发出的余量,避免重复
      const leftover = thinkingBuf.slice(thinkingEmitted);
      inThinking = false;
      inSysReminder = false; sysRemLen = 0;
      thinkingBuf = '';
      thinkingEmitted = 0;
      // 已积累的内容是思考原文,应路由到思考区(而非 text)——否则会整段泄漏进主答案
      return { text: '', thinking: leftover };
    }
    const endIdx = thinkingBuf.indexOf('</thinking>');
    if (endIdx === -1) {
      // 渐进式思考流:立即吐出本 chunk 带来的思考增量。
      // 末尾若恰好是 </thinking> 的前缀(闭合标签被 chunk 边界劈开),先扣住不发,
      // 等下一个 chunk 补齐后再发出,防止标签残片混进思考正文。
      const CLOSE = '</thinking>';
      let safeLen = thinkingBuf.length;
      for (let keep = Math.min(CLOSE.length - 1, thinkingBuf.length); keep > 0; keep--) {
        if (thinkingBuf.endsWith(CLOSE.slice(0, keep))) { safeLen = thinkingBuf.length - keep; break; }
      }
      const delta = thinkingBuf.slice(thinkingEmitted, safeLen);
      thinkingEmitted = safeLen;
      return { text: '', thinking: delta };
    }
    // 闭合:补发剩余思考增量(不含 </thinking> 标签本身),正文从闭合标签之后继续
    const thinking = thinkingBuf.slice(thinkingEmitted, endIdx);
    const after = thinkingBuf.slice(endIdx + 11);
    inThinking = false;
    inSysReminder = false; sysRemLen = 0;
    thinkingBuf = '';
    thinkingEmitted = 0;
    return { text: after, thinking };
  }

  const CONNECT_TIMEOUT_MS = 30000;

  // ── 辅助:找到当前 bot 答案卡片,在其之前插入元素 ──
  function _insertBeforeAnswer(el) {
    if (App._answerWrap && App._answerWrap._answer) {
      App._answerWrap._answer.parentNode.insertBefore(el, App._answerWrap._answer);
    } else {
      const _p0 = App.Chat.pane();
      _p0.insertBefore(el, _p0.firstChild);
    }
  }

  // 内联展示生成结果(图片/视频)。返回 true 表示已展示。
  // 用于"没有生成中进度卡"的情形:进度卡由 tool_call_start 触发,若该事件缺失
  // (页面刷新、断线重连、恢复运行等),只调 finishXxxGenerating 会静默什么都不做,
  // 用户就只看到 agent 那句"结果已展示"却空无一物。
  function _insertInlineMedia(toolName, resultStr, stepId) {
    const videoName = toolName === 'generate_video' || toolName === 'enhance_video';
    const imgUrl = (!videoName && App.parseImageUrl) ? App.parseImageUrl(resultStr) : '';
    const videoUrl = (videoName && App.parseVideoUrl) ? App.parseVideoUrl(resultStr) : '';
    if (!imgUrl && !videoUrl) return false;
    // 同一 step_id 只插一次(tool / tool_done 两个事件都可能触发)
    if (stepId && App.Chat.pane().querySelector('.tool-inline[data-step-id="' + stepId + '"]')) {
      return true;
    }
    const el = document.createElement('div');
    el.className = 'tool-inline';
    if (stepId) el.dataset.stepId = stepId;
    el.innerHTML = videoUrl
      ? App.buildVideoHtml(videoUrl, 'tool-result-video')
      : `<img class="tool-result-img" src="${App.escapeHtml(imgUrl)}" alt="生成图片">`;
    _insertBeforeAnswer(el);
    return true;
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

  // 节流渲染:多个 token 合并后再 re-render。
  // 注意:原来是 requestAnimationFrame(约 16ms 一次),对长答案而言等于每秒 60 次
  // 全量 Markdown 解析 + 整块 innerHTML 替换,直接造成输入/滚动卡顿。
  // 改成 100ms 时间片节流 + 内容未变化直接跳过,重排次数降到 1/6。
  let renderTimer = null;
  const RENDER_MIN_INTERVAL_MS = 100;
  let _lastRenderAt = 0;
  // 流读取超时(空闲间隙):后端在 120s 内无任何事件(判定为卡死)时主动断开
  let streamTimeout = null;
  // 绝对上限:即使后端持续产生事件,也不得超过 10 分钟,防止永久挂起
  let hardTimeout = null;

  function cleanupStream() {
    if (streamTimeout !== null) { clearTimeout(streamTimeout); streamTimeout = null; }
    if (hardTimeout !== null) { clearTimeout(hardTimeout); hardTimeout = null; }
    if (renderTimer !== null) { clearTimeout(renderTimer); renderTimer = null; }
  }

  let _lastRenderedHtml = '';
  let _lastRenderedMd = '';

  /* 思考面板收尾:把 "💭 思考中" 标签翻转为终态,防止流结束后面板仍停留在
   * "思考中" 的进行时观感(转圈/闪烁印象)。done/首段正文到达时调用。
   * 幂等:dataset.done 已置位则跳过;answer_reset 时清除以支持重写轮复位。 */
  App.markThinkingDone = function (mode, msg) {
    const m = msg || App._currentBotMsg;
    if (!m) return;
    const sec = m._thinkingSection;
    if (sec && !sec.dataset.done) {
      sec.dataset.done = '1';
      const label = sec.querySelector('.thinking-label');
      if (label) label.textContent = (mode === 'error') ? '💭 思考中断' : '💭 思考完成';
    }
    if (!mode && m._railSummary) m._railSummary.textContent = '✅ 回复完成';
  };
  App.resetThinkingState = function () {
    const m = App._currentBotMsg;
    const sec = m && m._thinkingSection;
    if (sec) {
      delete sec.dataset.done;
      const label = sec.querySelector('.thinking-label');
      if (label) label.textContent = '💭 思考中';
    }
  };

  /* 流式文本追加(2026-09-20 修复"标识符/数字被拆出空格"):
   * 旧逻辑在"上一字符是字母数字 && 下一字符是字母数字"时插一个空格,本意是修
   * "网关连空格一起吞掉"的模型;但 agnes-3.0-flash 这类会把单词/标识符切成子片段
   * (COMPLETED → COM + PLETED),于是每个片段边界都被插空格:
   *   "L97-103" → "L 9 7-1 0 3"、"400" → "4 0 0"、"ProductionStatus" → "P reduction Status"
   * 代码块/行内代码看起来像坏掉了。后端落库文本(与同一路流一致)空格是正常的,
   * 说明模型本身就带空格 → 默认原样拼接;确有个别模型需要补空格时置
   * App.STREAM_SPACE_FIX = true 即可恢复旧行为。 */
  function appendStreamText(cleanText) {
    if (!cleanText) return;
    if (App.STREAM_SPACE_FIX && App._mdBuf) {
      const lastChar = App._mdBuf.charAt(App._mdBuf.length - 1);
      const firstChar = cleanText.charAt(0);
      if (/[a-zA-Z0-9]/.test(lastChar) && /[a-zA-Z0-9]/.test(firstChar)) App._mdBuf += ' ';
    }
    App._mdBuf += cleanText;
  }

  function scheduleRender() {
    if (renderTimer !== null) return;
    if (renderDone) return;
    // 重写挂起时,缓冲区已被清空但旧答案仍显示在界面上,不要把它渲染成空白
    if (rewritePending) return;
    // 内容无变化则无需重排(后端偶发心跳/重复事件时省掉一次全量渲染)
    const pending = App._mdBuf || '';
    if (pending === _lastRenderedMd) return;
    const now = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
    const wait = Math.max(0, Math.min(RENDER_MIN_INTERVAL_MS, RENDER_MIN_INTERVAL_MS - (now - _lastRenderAt)));
    renderTimer = setTimeout(() => {
      renderTimer = null;
      _lastRenderAt = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
      if (!answerDiv) return;
      const md = App._mdBuf || '';
      if (!md) return;

      // 始终全量渲染,确保跨 token 的列表/段落/缩进结构正确
      let html = '';
      try {
        html = App.renderMarkdown(md);
      } catch (renderErr) {
        console.error('[renderMarkdown] scheduleRender ERROR:', renderErr);
        html = '<p style="white-space:pre-wrap;word-break:break-word;">' + App.escapeHtml(md) + '</p>';
      }
      answerDiv.innerHTML = html;
      _lastRenderedHtml = html;
      _lastRenderedMd = md;

      App.scrollMessagesToBottom();
    }, wait);
  }

  // ── 服务端兜底:用后端已落库的结果补全"前端中断"的流式输出 ──
  // 后端 agent 是独立后台任务(SSE 断开也会继续跑完并落库),所以前端流中断时
  // 服务端往往已有完整答案。以前只弹"执行中断"就把这份完整结果丢掉了,
  // 表现为"实时输出中断、重新打开历史却有完整内容"。
  async function _fetchServerAnswer() {
    try {
      if (!App.sessionId || !App.apiJSON) return '';
      // 【修复】recover 请求必须带超时,防止后端重启/无响应时永远 pending,
      // 导致 finally 块和 UI 清理被阻塞。
      const ac = new AbortController();
      const timer = setTimeout(() => ac.abort(new DOMException('RecoverTimeout', 'TimeoutError')), 5000);
      try {
        const res = await App.apiJSON(`/v1/chat/history?session_id=${encodeURIComponent(App.sessionId)}`, {
          signal: ac.signal,
        });
        if (!res || !res.success) return '';
        const msgs = (res.data && res.data.messages) || [];
        for (let i = msgs.length - 1; i >= 0; i--) {
          if (msgs[i] && msgs[i].role === 'assistant') {
            // 顺带带回 meta.thinking:流在思考阶段被中断时,思考区也要能从服务端恢复
            const meta = msgs[i].meta || {};
            App._serverThinking = typeof meta.thinking === 'string' ? meta.thinking : '';
            return String(msgs[i].content || '');
          }
        }
      } finally {
        clearTimeout(timer);
      }
    } catch (e) {
      console.warn('[recover] 拉取服务端结果失败:', e && e.message ? e.message : e);
    }
    return '';
  }

  async function _recoverFromServer() {
    // 本轮尚未产出任何内容时不采用服务端结果,避免误取上一轮的答案补到本轮
    if (!receivedTokens && !App._mdBuf) return false;
    const streamedNet = (App._mdBuf || '').replace(/\s/g, '').length;
    let server = await _fetchServerAnswer();
    if (!server || !server.trim()) {
      // 后端可能仍在收尾(最终保存发生在流结束之后),短暂等待后重试一次
      await new Promise(r => setTimeout(r, 3000));
      server = await _fetchServerAnswer();
    }
    // 思考区恢复(放在正文早退判断之前):流在思考阶段被限流中断时,
    // 本地思考区只有残段甚至为空,而正文可能为空导致下方直接 return。
    // 服务端 meta.thinking 存有完整思考(含未闭合标签兜底提取),补渲染一次。
    const botMsg0 = App._currentBotMsg;
    if (botMsg0 && App.addThinking) {
      const localThinkLen = (botMsg0._thinkingRaw || '').replace(/\s/g, '').length;
      const serverThink = App._serverThinking || '';
      if (serverThink && serverThink.replace(/\s/g, '').length > localThinkLen + 20) {
        App.addThinking(botMsg0, '\n\n' + serverThink);
        console.log('[recover] 已用服务端 meta.thinking 补全思考区, len:', serverThink.length);
      }
    }
    const cleaned = (App.stripThinkingTags ? App.stripThinkingTags(server) : server).trim();
    const serverNet = cleaned.replace(/\s/g, '').length;
    // 仅在"确实丢了大量内容"时才整体替换,避免轻微丢字/思考标签差异
    // 触发整段重渲染(造成"突然膨出全部内容"):
    //  - 本地已显示内容几乎为空(真实丢内容) → 允许恢复;
    //  - 本地已有较多内容且服务端仅略长(误差/少量丢字) → 不重渲染。
    if (!cleaned) return false;
    if (serverNet <= streamedNet + 20) return false;
    if (streamedNet >= 200 && serverNet <= streamedNet * 1.05) return false;

    const target = answerDiv || (App._currentBotMsg && App._currentBotMsg._answer);
    if (!target) return false;
    let html = '';
    try { html = App.renderMarkdown(cleaned); }
    catch (e) { html = '<p style="white-space:pre-wrap;">' + App.escapeHtml(cleaned) + '</p>'; }
    target.innerHTML = html;
    App._mdBuf = cleaned;
    renderDone = true;
    done = true;
    // 终态样式与正常完成保持一致:隐藏过程栏与步骤计数
    const botMsg = App._currentBotMsg;
    if (botMsg) {
      botMsg.classList.add('no-animation');
      const rail = botMsg.querySelector('.process-rail');
      if (rail) rail.classList.remove('visible');
      botMsg.querySelectorAll('.step-counter').forEach(el => { el.style.display = 'none'; });
    }
    if (typingShown) App.removeTyping();
    App.scrollMessagesToBottom(true);
    console.log('[recover] 已用服务端结果补全本轮输出, len:', cleaned.length);
    return true;
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
    // 请求已成功、SSE 连接已建立 → 附件已随 patchBody 发出去,此刻清掉 composer 待发区:
    // 否则卡片要等整条流跑完(可能数分钟)才在 finally 里被 reset,体感"发完还挂着"。
    // 用户气泡里已有渲染好的卡片(renderToBubble 用发送前快照,不依赖 currentIds),
    // 历史回放也靠 meta.attachments 重建,清 composer 都不影响。
    if (App.Attach && App.Attach.reset) App.Attach.reset();

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
          // 后端是后台任务,超时断开时它可能已跑完并落库 → 先尝试补全,再决定是否报错
          _recoverFromServer().then(recovered => {
            if (!recovered) App.appendMessage('响应超时,请稍后重试', 'error');
            _finishGroup();
          }).catch(() => {
            App.appendMessage('响应超时,请稍后重试', 'error');
            _finishGroup();
          });
        }
      }, 300000); // 300s 空闲间隙 (原 120s→180s→300s；后端断点续写/限流重试期间可能 90s+5s+90s 无 token，180s 会误杀)
    }
    resetStreamTimeout();
    // 绝对上限:即使后端持续发事件/心跳续期空闲计时器,超时仍未结束则强制断开,
    // 防止永久挂住(配合后端 SSE 哨兵修复,双保险)。收到任何事件时不清此计时器。
    // ⚠ 2026-09-23:原值 10 分钟会掐死长耗时生成(视频生成后端预算已放宽到 900s,
    // 加上轮询/重试可逼近 15 分钟),导致"视频还在生成,前端却先断开"。现抬到 30 分钟,
    // 仍高于后端最长工具预算,只作为真·卡死的兜底。
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
        // 同上:先用服务端已落库的结果补全,补不到再报错
        _recoverFromServer().then(recovered => {
          if (!recovered) App.appendMessage('会话超时(超过 30 分钟未完成),已中止。可重试。', 'error');
          _finishGroup();
        }).catch(() => {
          App.appendMessage('会话超时(超过 30 分钟未完成),已中止。可重试。', 'error');
          _finishGroup();
        });
      }
    }, 1800000);

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
          // 后台会话:静默消费事件、不渲染任何 UI,彻底避免与前台串台。
          // 仅当本流所属会话仍是前台时才处理 UI 事件;切走后其流仍在后端跑,
          // 但 UI 写目标应为它自己的离屏 pane —— 为安全起见一律不渲染,
          // 待用户切回时由 /chat/history 重新加载即可看到完整结果。
          if (mySid !== App.sessionId) continue;
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
          // 视频生成同样尽早展示"生成中"卡:视频通常要几分钟,没有进度提示时
          // 用户只会看到界面毫无反应(这正是"怎么没显示"的一半原因)
          const _vidNames = ['generate_video', 'enhance_video'];
          const _hasVid = _vidNames.indexOf(_toolName) !== -1 ||
                          _vidNames.some((n) => (_toolNames || []).indexOf(n) !== -1);
          if (_hasVid && (evt.type === 'tool_call_start' || evt.type === 'tool_start' ||
                          evt.type === 'tool_call_delta' || evt.type === 'tool_route')) {
            App.showVideoGenerating();
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
            const children = App.Chat.pane().children;
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
            // 携带后端 step_id:与 tool_call_start 预建的 running 行去重同一行,
            // 避免"同一工具出现 running/done 两行、running 行永不更新"的重复渲染。
            App.addToolStepIfNew(App._currentBotMsg, toolName, toolArgs, ok ? 'done' : 'fail', evt.result, evt.step_id);

            // 图片生成完成:立即展示图片到进度卡片
            if (toolName === 'generate_image' && App.finishImageGenerating) {
              App.finishImageGenerating(evt.result || '');
            }
            // 视频生成完成:此前**没有**任何实时展示路径(播放器只在后端根本不发的
            // tool_done 分支和历史渲染里),用户只看到"结果已展示"却什么都没有。
            if (toolName === 'generate_video' || toolName === 'enhance_video') {
              if (App._mediaCards && App._mediaCards.video && App.finishVideoGenerating) {
                App.finishVideoGenerating(evt.result || '');
              } else {
                _insertInlineMedia(toolName, evt.result || '', evt.step_id);
              }
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
            const msgs = App.Chat.pane();
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
              // 卡片内 step-row 去重:`tool` 事件(带 step_id)通常已把该工具渲染为
              // done/fail 行;tool_done 无 step_id,若盲目 addStep 会再建一行重复记录。
              // 规则:当前卡片最后一条 step-row 与本工具同名且已结束 → 已渲染过,跳过;
              // 同名但仍在 running → 交给 addToolStepIfNew 兜底更新该行。
              let alreadyRendered = false;
              if (App._currentBotMsg && App._currentBotMsg._stepsFlow) {
                const lastRow = App._currentBotMsg._stepsFlow.querySelector('.step-row:last-of-type');
                if (lastRow && lastRow.dataset.tool === doneToolName &&
                    !lastRow.classList.contains('step-running')) {
                  alreadyRendered = true;
                }
              }
              if (alreadyRendered) {
                // 已由 tool 事件渲染,仅更新轨迹状态
                App.ExecTrace.updateStep(doneToolName, 'done', '');
              } else {
                // 没有重复记录,才显示结果
                App.ExecTrace.updateStep(doneToolName, 'done', '');
                const imgUrl = App.parseImageUrl(evt.result);
                if (doneToolName === 'generate_image' && imgUrl) {
                  const toolEl = document.createElement('div');
                  toolEl.className = 'tool-inline';
                  toolEl.innerHTML = `<img class="tool-result-img" src="${App.escapeHtml(imgUrl)}" alt="生成图片">`;
                  _insertBeforeAnswer(toolEl);
                } else if (doneToolName === 'generate_video' || doneToolName === 'enhance_video') {
                  // 走统一的插入函数:带 step_id 去重,避免与 tool 事件重复插播放器
                  if (!_insertInlineMedia(doneToolName, evt.result || '', evt.step_id)) {
                    if (evt.result) {
                      App.addToolStepIfNew(App._currentBotMsg, doneToolName, evt.arguments || '', 'done', evt.result, evt.step_id);
                    }
                  }
                } else if (evt.result) {
                  // 统一使用 addStep 渲染,保持与历史消息一致(带 step_id 供去重)
                  App.addToolStepIfNew(App._currentBotMsg, doneToolName, evt.arguments || '', 'done', evt.result, evt.step_id);
                }
                _showRail();
              }
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
            const lastRail = App.Chat.pane().querySelector('.msg.bot .process-rail:last-child');
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
            // 也可能只返回纯思考文本。用流式版 strip:只剥标签壳、不 trim(保住换行),
            // 且不做成对剥离——整块 <thinking>...</thinking> 会被成对正则连内容一起删掉。
            const rawContent = evt.content || '';
            // prepare 阶段的状态行(如"准备中: 模型 xxx")是进度提示不是思考内容,
            // 混进思考区会顶在开头且与后续思考无分隔
            if (evt.phase === 'prepare') continue;
            const thinkText = App.stripThinkingTagsStream ? App.stripThinkingTagsStream(rawContent) : rawContent;
            // ⚠️ 判空不能用 .trim():换行符独占 chunk 时 '\n'.trim()==='' 会把换行丢掉
            if (!thinkText) {
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
            // 使用规范渲染器;独立的整块思考(reasoning)与已有内容之间补段落分隔
            if (App._currentBotMsg && App.addThinking) {
              const sep = (evt.reasoning && App._currentBotMsg._thinkingRaw) ? '\n\n' : '';
              App.addThinking(App._currentBotMsg, sep + thinkText);
            }
            App.updateRailStatus(App._currentBotMsg, 'thinking', '思考中...');
          } else if (evt.type === 'answer_reset') {
            // 后端 reflect 判定截断/严重不合格,准备重写。
            // 关键修复:不要立刻清空界面——直接 answerDiv.innerHTML='' 会造成
            // "答案显示→突然消失→新答案到达后再次出现"的闪烁观感。
            // 改为保留上一版答案在屏幕上,清空内部缓冲区并置 rewritePending,
            // 等新 token 真正开始到达时(见 token 分支)再整体替换。
            App._mdBuf = '';
            _lastRenderedHtml = '';
            rewritePending = true;
            pendingTextHold = '';
            // 思考状态机一并复位:重试轮会重新生成思考,不能拼在被丢弃的旧思考后面
            inThinking = false;
            inSysReminder = false; sysRemLen = 0;
            thinkingBuf = '';
            thinkingEmitted = 0;
            if (App._currentBotMsg) {
              App._currentBotMsg._thinkingRaw = '';
              if (App._currentBotMsg._thinkingBody) App._currentBotMsg._thinkingBody.innerHTML = '';
              // 思考面板复位:重写轮会重新思考,标签从"思考完成"回到"思考中"
              App.resetThinkingState();
            }
            // 取消待执行的渲染帧,避免把已清空的缓冲区渲染成空白
            if (renderTimer !== null) { clearTimeout(renderTimer); renderTimer = null; }
            // 过程栏给出"正在优化回复"提示,让用户知道这是有意为之而非卡死
            if (App._currentBotMsg) {
              App.updateRailStatus(App._currentBotMsg, 'thinking', '正在优化回复…');
            }
            if (App.DEBUG) console.log('[answer_reset] 标记重写,保留旧答案直至新 token 到达');
          } else if (evt.type === 'status') {
            // 后端过程状态(如"模型限流,正在重试…"):更新过程栏,让用户知道没卡死
            if (App._currentBotMsg) {
              App.updateRailStatus(App._currentBotMsg, 'thinking', evt.content || '处理中...');
              const rail = App._currentBotMsg.querySelector('.process-rail');
              if (rail) rail.classList.add('visible');
            }
            if (App.DEBUG) console.log('[status]', evt.content || '');
          } else if (evt.type === 'token') {
            // 第一次 token 到达:显示并移除打字指示器,创建答案容器
            if (!typingShown) {
              App.removeTyping();
              typingShown = true;
              // 移除打字指示器
              const msgs = App.Chat.pane();
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
              // 流式版本: 不 trim(否则独占 chunk 的 \n\n 段落换行被清空), 只剥标签壳,
              // 保留思考正文(旧 stripThinkingTags 的成对正则会连内容一起删掉)
              const thinkText = App.stripThinkingTagsStream ? App.stripThinkingTagsStream(thinking) : thinking;
              if (App._currentBotMsg && App.addThinking) {
                App.addThinking(App._currentBotMsg, thinkText);
              }
            }
            if (text) {
              // 重写挂起:新的一轮答案从此刻开始,现在才把上一版答案从界面上替换掉。
              // 此前的 answer_reset 只清空了缓冲区,屏幕上一直保留旧答案,避免空白闪烁。
              if (rewritePending) {
                rewritePending = false;
                App._mdBuf = '';
                if (answerDiv) answerDiv.innerHTML = '';
              }
              // 首段正文到达:思考阶段结束,面板标签从"思考中"翻转为"思考完成"
              App.markThinkingDone();
              // 防护: 即使 processTokenChunk 未捕获, 也剥离残留 thinking 标签。
              // 必须用流式版本: stripThinkingTags 末尾 .trim() 会吃掉每个 chunk 首尾换行,
              // 独占一个 chunk 的 "\n\n" 段落分隔被整块丢弃 → 全文挤成一个密集段落。
              const cleanText = App.stripThinkingTagsStream ? App.stripThinkingTagsStream(text) : text;
              appendStreamText(cleanText);
              // 防止 _mdBuf 无限增长:超过 200KB 时截断头部(保留最近内容)
              const MAX_MD_BUF = 200000;
              if (App._mdBuf.length > MAX_MD_BUF) {
                App._mdBuf = App._mdBuf.slice(App._mdBuf.length - MAX_MD_BUF);
              }
              if (App.DEBUG) console.log('[token] text_len:', cleanText.length, 'buf_len:', App._mdBuf.length);
              scheduleRender();
            }
          } else if (evt.type === 'error') {
            done = true;
            isError = true;
            rewritePending = false;
            inThinking = false;
            inSysReminder = false; sysRemLen = 0;
            thinkingBuf = '';
            thinkingEmitted = 0;
            App.clearImageGenerating();
            cleanupStream();
            if (typingShown) App.removeTyping();
            const errBotMsg = App._currentBotMsg || (App.Chat.pane() && App.Chat.pane().querySelector('.msg.bot:last-child, .msg.msg-bot:last-child'));
            if (errBotMsg) {
              errBotMsg.classList.add('no-animation');
              // 保留 steps-flow(工具步骤可见,与历史一致);仅隐藏过程栏与 step-counter
              const rail = errBotMsg.querySelector('.process-rail');
              if (rail) rail.classList.remove('visible');
              errBotMsg.querySelectorAll('.step-counter').forEach(el => { el.style.display = 'none'; });
              App.markThinkingDone('error', errBotMsg);
            }
            if (answerDiv) {
              // 冲刷扣留的未闭合 '<' 片段(流结束不会再有后续 chunk 拼接)
              if (pendingTextHold) { App._mdBuf += pendingTextHold; pendingTextHold = ''; }
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
          } else if (evt.type === 'answer') {
            // verify 节点内联重写:后端一次性下发完整(新)答案,而非逐 token 流。
            // 直接以该内容替换当前缓冲并渲染,避免在 done 之后再走 _recoverFromServer
            // 全量重渲染,造成"突然膨出全部内容"。
            const raw = typeof evt.content === 'string' ? evt.content : '';
            if (raw) {
              receivedTokens = true;
              rewritePending = false;
              App._mdBuf = raw;
              if (answerDiv) {
                const md = (App.stripThinkingTags ? App.stripThinkingTags(raw) : raw)
                  .replace(/\n{3,}/g, '\n\n').trim();
                let html = '';
                try { html = App.renderMarkdown(md); }
                catch (e) { html = '<p style="white-space:pre-wrap;">' + App.escapeHtml(md) + '</p>'; }
                answerDiv.innerHTML = html;
              }
            }
            // 不 break:verify 之后仍会发 done,继续处理以收尾。
          } else if (evt.type === 'done') {
            done = true;
            rewritePending = false;
            // 注意:renderDone 延迟到最终渲染之后设置,避免跳过待处理的 scheduleRender
            // 强制退出 thinking 模式:防止 </thinking> 丢失导致内容被吞
            inThinking = false;
            inSysReminder = false; sysRemLen = 0;
            thinkingBuf = '';
            thinkingEmitted = 0;
            App.clearImageGenerating();
            cleanupStream();
            if (typingShown) App.removeTyping();
            // 与历史回放统一:终态隐藏 process-rail(历史卡片的 rail 无 .visible,默认 display:none),
            // 保留 steps-flow(工具步骤可见)——仅隐藏纯流式态的 step-counter。
            const botMsg = App._currentBotMsg || (App.Chat.pane() && App.Chat.pane().querySelector('.msg.bot:last-child, .msg.msg-bot:last-child'));
            if (botMsg) {
              botMsg.classList.add('no-animation');
              const rail = botMsg.querySelector('.process-rail');
              if (rail) rail.classList.remove('visible'); // 取消流式期添加的 .visible,回到历史同款隐藏态
              botMsg.querySelectorAll('.step-counter').forEach(el => { el.style.display = 'none'; });
              const think = botMsg.querySelector('.thinking-section');
              if (think && !think.classList.contains('open')) think.style.display = 'none';
            }
            // 思考面板收尾:标签翻转为"思考完成",过程栏置为完成态
            App.markThinkingDone(null, botMsg);
            // 流式完成后整段重渲染,确保最终内容(含跨切片块级结构)正确
            if (answerDiv) {
              // 冲刷扣留的未闭合 '<' 片段(流结束不会再有后续 chunk 拼接)
              if (pendingTextHold) { App._mdBuf += pendingTextHold; pendingTextHold = ''; }
              const rawMd = App._mdBuf || '';
              const finalMd = (App.stripThinkingTags ? App.stripThinkingTags(rawMd) : rawMd)
                .replace(/\n{3,}/g, '\n\n').trim();
              let finalHtml = '';
              try {
                finalHtml = App.renderMarkdown(finalMd);
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
            // 兜底对齐:流式过程中若丢过内容(标签状态机吞掉/事件丢失),
            // 服务端落库的是完整结果,用它与界面对齐一次(内容不明显更长时不会改动界面)
            _recoverFromServer().catch(() => {});
            break; // 流已完成,退出事件处理循环
          } else if (evt.type === 'cancelled') {
            done = true;
            inThinking = false;
            inSysReminder = false; sysRemLen = 0;
            thinkingBuf = '';
            thinkingEmitted = 0;
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
            inSysReminder = false; sysRemLen = 0;
            thinkingBuf = '';
            thinkingEmitted = 0;
            App.clearImageGenerating();
            cleanupStream();
            if (typingShown) App.removeTyping();
            if (answerDiv) {
              if (pendingTextHold) { App._mdBuf += pendingTextHold; pendingTextHold = ''; }
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
            inSysReminder = false; sysRemLen = 0;
            thinkingBuf = '';
            thinkingEmitted = 0;
            App.clearImageGenerating(); cleanupStream();
            if (typingShown) App.removeTyping();
            if (answerDiv) {
              if (pendingTextHold) { App._mdBuf += pendingTextHold; pendingTextHold = ''; }
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
                const thinkText = App.stripThinkingTagsStream ? App.stripThinkingTagsStream(thinking) : thinking;
                App.appendThinking(thinkText);
              }
              if (text) { const cleanText = App.stripThinkingTagsStream ? App.stripThinkingTagsStream(text) : text; appendStreamText(cleanText); if (App._mdBuf.length > 200000) { App._mdBuf = App._mdBuf.slice(-200000); } scheduleRender(); }
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

    // 前后台都先清理流超时计时器,防止定时器泄漏
    cleanupStream();
    App.clearImageGenerating(); // 任何流终止都清理图片生成卡片,避免残留

    // 仅**前台**会话渲染滚动锚定 / 回到底部提示 / 异常恢复 UI。
    // 后台会话的流被静默消费(见事件循环顶部的 foreground guard),不渲染任何 UI,
    // 彻底避免与前台串台;切回后台会话时由 /chat/history 重新加载即可看到完整结果。
    if (mySid === App.sessionId) {
      // 记录流开始前的滚动比例，用于流式完成后判断是否应跟随滚动
      const _pane1 = App.Chat.pane();
      const scrollRatioBefore = _pane1.scrollHeight === 0 ? 1 : _pane1.scrollTop / (_pane1.scrollHeight - _pane1.clientHeight);

      // ── 滚动锚定：只有用户已在底部(<48px)时才跟随，否则显示“回到底部”提示 ──
      const _pane2 = App.Chat.pane();
      const atBottomNow = _pane2.scrollHeight - _pane2.scrollTop <= _pane2.clientHeight * 0.95;
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
    }

    // 流被异常中断:直接显示错误,不再回退非流式接口
    // 场景1: 从未收到任何事件 → "响应中断"
    // 场景2: 收到过事件(工具执行等)但流未正常完成(连接中断/后端异常) → "执行中断"
    // 【关键修复】recover 可能因后端重启/网络抖动而长时间挂起,必须**先清理 UI**
    // 再异步尝试服务端补全,否则 typing 指示器和 streaming 状态会永远卡住。
    // 仅前台会话走 recover/错误提示;后台会话的流被静默消费,不向前台注入错误信息。
    if (mySid === App.sessionId && !done && !isError) {
      App.removeTyping();
      if (App._currentGroup) _finishGroup();
      if (renderTimer !== null) { clearTimeout(renderTimer); renderTimer = null; }
      // 异步 recover:不阻塞 finally 的后续清理
      _recoverFromServer().then(recovered => {
        if (!recovered) {
          if (receivedAnyEvent) {
            App.appendMessage('执行中断,结果可能不完整。请检查上下文长度后重试', 'error');
          } else {
            App.appendMessage('响应中断,请稍后重试', 'error');
          }
        }
      }).catch(recErr => {
        console.warn('[recover] 补全失败:', recErr && recErr.message ? recErr.message : recErr);
        if (receivedAnyEvent) {
          App.appendMessage('执行中断,结果可能不完整。请检查上下文长度后重试', 'error');
        } else {
          App.appendMessage('响应中断,请稍后重试', 'error');
        }
      });
    }
  } catch (e) {
    // AbortError 由 stop() 处理(已追加提示),这里仅做静默清理
    cleanupStream();
    App.removeTyping();
    if (App._currentGroup) _finishGroup();
    if (e.name !== 'AbortError') {
      App.appendMessage(`请求失败: ${e.message}`, 'error');
    }
  } finally {
    /* 代次保护: 仅当本轮仍是"最新发送"时才重置全局 streaming 状态。
       若已被更新的 doSend 取代(旧流被 abort), 跳过 setStreaming(false)/stopBtn/
       sendBtn 强制复位, 避免旧 finally 覆盖新流的 streaming=true + stopBtn.show
       状态 —— 这正是"再次发送后停止按钮消失、不知可否发送"的根因。 */
    const isStale = (App.Chat._sendSeq || 0) !== mySeq;
    App.Chat._streamingStart = null;
    /* 多会话并行: 无论是否 stale, 本轮所属会话自己的 streaming 标记都必须清除,
       否则后台会话的流结束后其 r.streaming 永久残留 —— 侧边栏"后台生成中"呼吸点错乱,
       且切回时误判仍在生成。全局/前台 UI 仍受 isStale 保护: 被更新的发送取代的旧流
       不得复位前台状态(否则会把新流的 stopBtn/输入锁定覆盖掉)。 */
    if (streamingSet) {
      if (!isStale) {
        App.Chat.setStreaming(false, mySid);
      } else if (App.Session && App.Session._runs && App.Session._runs.has(mySid)) {
        App.Session._runs.get(mySid).streaming = false;
      }
    }
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
    inSysReminder = false; sysRemLen = 0;
    thinkingBuf = '';
    thinkingEmitted = 0;
    pendingTextHold = '';
    thinkingEmitted = 0;
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
    /* 本次流到此结束。清除该会话的"运行中"标记:
       - 若是前台会话结束,顺带刷新列表让高亮/时间更新;
       - 若它在后台跑完(用户早切走了),额外给个提示,否则用户不知道那边好了。 */
    const _finSid = mySid;
    if (_finSid && App.Session) {
      App.Session._runs.delete(_finSid);
      if (_finSid !== App.sessionId && !isError) {
        if (App.toast) App.toast('后台会话已生成完毕,可切回查看', 'ok');
        if (App.Session.refreshList) {
          try { await App.Session.refreshList(); } catch (_) { /* 忽略 */ }
        }
      }
    }
    if (App.Attach) App.Attach.reset();
    if (App.CtxMeter) App.CtxMeter.refresh();
    // 新建的会话在发出第一条消息前后端没有记录(侧边栏列表里不存在),
    // 消息落库后把它补进列表;已存在的会话不会触发额外请求。
    if (!isStale && App.Session && App.Session.refreshIfNeeded) {
      try { await App.Session.refreshIfNeeded(); } catch (_) {}
    }
    if (!isStale) App.els.input.focus();
  }
};

App.Chat.stop = function (silent) {
  App.Chat._streamingStart = null;
  // 显式通知后端取消后台 agent:仅断连(abort)不会被后端当作取消,
  // agent 会继续跑完并落库,以便前端从服务端补全结果。
  try {
    if (App.sessionId && App.apiJSON) {
      App.apiJSON('/v1/chat/cancel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: App.sessionId }),
      }).catch(() => {});
    }
  } catch (_) { /* 忽略:取消失败不影响前端停止 */ }
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

/* 多会话并行: streaming 状态改为"每会话一份 + 全局仅代表前台"。
   - 传入 sid 时同步该会话自己的 r.streaming(仅当该会话已 parked/在 _runs 中才写,
     避免给前台活跃会话注入多余 _runs 条目)。
   - 全局 App.streaming 始终只代表"当前前台会话"是否在生成,供输入锁定 / UI 使用。
   这样后台会话的流结束不会影响前台的可发送判定,真正支持多会话同时运行。 */
/* 统一刷新"全局流式 UI":body.streaming class / stopBtn(.show) / sendBtn(disabled) /
   状态条。完全由 App.streaming(前台会话是否在生成)驱动,供 setStreaming / park / unpark 共用,
   避免"切会话后执行中视觉状态残留 / 切回后不恢复"。 */
App.Chat.applyStreamingVisual = function () {
  const on = !!App.streaming;
  /* 修复"发送后不能停止/不知道是否可发送": 用 classList 精确增删 body.streaming,
     绝不动其他 class(sidebar-collapsed/side-open/has-messages 等),防止布局塌掉。 */
  if (on) {
    document.body.classList.add('streaming');
  } else {
    document.body.classList.remove('streaming');
  }
  /* 修复"不能停止": stopBtn 显隐由 JS 直接控制 .show class, 与 body class 解耦。 */
  if (App.els.stopBtn) {
    if (on) App.els.stopBtn.classList.add('show');
    else App.els.stopBtn.classList.remove('show');
  }
  // on=true 时禁用 sendBtn; on=false 时按输入内容启用 sendBtn
  const inp = App.els.input;
  if (App.els.sendBtn) {
    App.els.sendBtn.disabled = on ? true : !!(inp && inp.value.trim());
  }
  // 更新加载状态
  if (App.UI && App.UI.Status) {
    if (on) App.UI.Status.showLoading('思考中...');
    else App.UI.Status.clearStatus();
  }
};

App.Chat.setStreaming = function (val, sid) {
  const targetSid = sid || App.sessionId;
  if (App.Session && App.Session._runs && App.Session._runs.has(targetSid)) {
    App.Session._runs.get(targetSid).streaming = val;
  }
  if (targetSid !== App.sessionId) return; // 非前台会话:只更新其自身记录,不动全局/UI
  App.streaming = val;
  App.Chat.applyStreamingVisual();
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
    const data = await res.json().catch(() => ({}));
    App.removeTyping();
    // ⚠️ 非 2xx 时响应体是 {"detail": "..."},既没有 answer 也没有 error。
    // 以前一律兜底成"请确认已选择支持视觉的模型",把 401/400/413/422/500
    // 全部伪装成"模型不支持视觉",误导排查。必须先看 HTTP 状态。
    if (!res.ok) {
      // FastAPI 的 422 detail 是数组(每项含 loc/msg),直接进模板会变成 [object Object]
      const detail = App.formatApiDetail ? App.formatApiDetail(data) : JSON.stringify(data);
      App.appendMessage(`❌ 图片识别失败(HTTP ${res.status})：${detail || '服务端未给出原因'}`, 'error');
    } else if (data.error) {
      App.appendMessage(`❌ ${data.error}`, 'error');
    } else if (data.answer) {
      App.appendMessage(data.answer, 'bot');
    } else {
      const m = data.model ? `（模型 ${data.model}）` : '';
      App.appendMessage(`未能识别图片内容${m}，请确认已选择支持视觉的模型(如 qwen-vl、gpt-4o)`, 'bot');
    }
  } catch (e) {
    App.removeTyping();
    // api.js 有 60s 超时保护,超时会 abort —— 文案要把"超时"说清楚,
    // 否则用户只看到 "The user aborted a request."(浏览器原生文案)
    const aborted = e && (e.name === 'AbortError' || /abort/i.test(e.message || ''));
    App.appendMessage(
      aborted ? '❌ 图片识别超时(超过 60 秒未返回),请换一张更小的图片或稍后重试'
              : `❌ 图片识别失败: ${e.message}`,
      'error');
  } finally {
    // 无论成功/失败都必须重置 streaming 状态,防止按钮卡死
    App.Chat._streamingStart = null;
    if (streamingSet) App.Chat.setStreaming(false);
    App.selectedImageFile = null;
    App.els.imgPreviewBar.classList.remove('show');
    App.els.input.focus();
  }
};
