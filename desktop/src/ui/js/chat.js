/* 聊天模块:SSE 流式对话 / 工具事件 / 停止生成 / 图片识别 */
App.Chat = {};

/* 组装协作模式参数:统一从 App.Mode(单一状态源)读取,UI 控件的改动已同步进该状态 */
App.Chat.modeParams = function () {
  if (App.Mode && App.Mode.params) return App.Mode.params();
  return { mode: (App.els.modeSelect && App.els.modeSelect.value) || 'auto' };
};

App.Chat.send = async function () {
  // 已选图片时,发送即执行图片识别
  if (App.selectedImageFile) {
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
  if (!text || App.streaming) return;
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
    if (nodes[i].classList.contains('user')) {
      const b = nodes[i].querySelector('.answer-doc');
      userText = b ? (b.textContent || '').trim() : '';
      userNode = nodes[i];
      break;
    }
  }
  if (!userText) return;
  const cutFrom = userNode ? nodes.indexOf(userNode) + 1 : baseIdx + 1;
  for (let i = nodes.length - 1; i >= cutFrom; i--) nodes[i].remove();
  App.lastUserText = userText;
  App._lastUserWrap = userNode;
  App.showTyping();
  await App.Chat.doSend(userText, { skipAppendUser: true });
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
  input.addEventListener('blur', () => finish(true));
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

/* 结束工具分组:根据是否有答案/工具展示最终摘要 */
function _finishGroup(answerText) {
  if (App.Group && App.Group.finish) {
    App.Group.finish(answerText || '', false);
  }
  App._currentGroup = null;
}

/* 核心发送逻辑 */
App.Chat.doSend = async function (text, opts = {}) {
  // 没有当前会话时自动创建
  if (!App.sessionId || !App.sessionId.startsWith('desktop-')) {
    App.Session.newSession();
  }
  const input = App.els.input;
  input.value = '';
  if (App.syncComposer) App.syncComposer();
  App._planCard = null;
  App.clearImageGenerating();  // 清除上一轮可能残留的"生成中"卡片
  App.removeTyping();
  if (!opts.skipAppendUser) App.appendMessage(text, 'user');
  // 打字指示器延迟显示:等第一个 token 到达再显示,避免无响应时 indicator 闪烁
  App.Chat.setStreaming(true);
  _startGroup();  // 开始工具分组

  // 每次 doSend 前清空 mdBuf，避免多次 abort/resume 累积
  App._mdBuf = '';
  // 每轮重置思考卡/答案锚点,避免上一轮残留导致本轮 thinking 错位
  App._thinkingWrap = null;
  App._answerWrap = null;
  // 每轮取消上一轮的流式请求，避免并发 SSE 竞争
  if (App.abortController) {
    try { App.abortController.abort(); } catch (_) { /* 忽略 */ }
    App.abortController = null;
  }
  // thinking 标签跨 token 状态
  let inThinking = false;
  let thinkingBuf = '';
  App.abortController = new AbortController();
  const signal = App.abortController.signal;
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
    if (!inThinking) {
      const idx = chunk.indexOf('<thinking');
      if (idx === -1) return { text: chunk, thinking: '' };
      inThinking = true;
      thinkingBuf = chunk.slice(idx);
      return { text: chunk.slice(0, idx), thinking: '' };
    }
    thinkingBuf += chunk;
    const endIdx = thinkingBuf.indexOf('</thinking>');
    if (endIdx === -1) return { text: '', thinking: '' };
    const thinking = thinkingBuf.slice(0, endIdx + 11);
    const after = thinkingBuf.slice(endIdx + 11);
    inThinking = false;
    thinkingBuf = '';
    return { text: after, thinking };
  }

  const CONNECT_TIMEOUT_MS = 30000;
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
    if (renderTimer !== null) { clearTimeout(renderTimer); renderTimer = null; }
  }

  function scheduleRender() {
    if (renderTimer !== null) return;
    renderTimer = setTimeout(() => {
      renderTimer = null;
      if (!answerDiv) return;
      const md = App._mdBuf || '';
      let html = '';
      try {
        html = App.renderMarkdown(md);
      } catch (renderErr) {
        console.error('[renderMarkdown] scheduleRender ERROR:', renderErr);
        // fallback: 必须 escape,防止原始 JSON/HTML 含 < 被解析为元素或泄露事件内容
        html = '<p style="white-space:pre-wrap;word-break:break-word;">' + App.escapeHtml(md) + '</p>';
      }
      // 整段重渲染:保证跨切片边界的代码块/列表/表格等块级结构被正确解析
      answerDiv.innerHTML = html;
      // 仅当用户已在底部极近距离(<5%)时才跟随滚动,否则保持原位
      if (App.els.messages.scrollHeight - App.els.messages.scrollTop <= App.els.messages.clientHeight * 0.05) {
        App.els.messages.scrollTop = App.els.messages.scrollHeight;
      }
    }, 16);
  }

try {
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
            ...App.Chat.modeParams(),
          };
          res = await App.api('/v1/chat/stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(App.Attach ? App.Attach.patchBody(body) : body),
          signal,
        });
      } catch (fetchErr) {
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
    // 绝对上限:即使后端持续产生事件,也不得超过 10 分钟,防止永久挂起
    hardTimeout = setTimeout(() => {
      hardTimeout = null;
      if (!done && !isError) {
        done = true;
        isError = true;
        cleanupStream();
        if (App.abortController) App.abortController.abort(new DOMException('硬超时', 'TimeoutError'));
        if (typingShown) App.removeTyping();
        App.appendMessage('响应超时(超过上限),请稍后重试', 'error');
        _finishGroup();
      }
    }, 600000); // 10 分钟绝对上限

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
      }, 120000); // 120s 空闲间隙
    }
    resetStreamTimeout();

    while (true) {
      let isDone;
      try {
        const result = await reader.read();
        console.log('[SSE read]', 'done=', result.done, 'bytes=', result.value ? result.value.byteLength : 0);
        ({ done: isDone, value } = result);
      } catch (readErr) {
        // 连接异常(如用户停止、切换会话、后端非正常关闭).
        // 如果已经收到过合法 SSE 事件,不再标记为"响应中断",按正常结束处理(让 done/error 分支的提示保留).
        if (!receivedAnyEvent) isError = true;
        console.warn('[SSE read error]', readErr && readErr.message);
        break;
      }
      if (isDone) {
        console.log('[SSE] FINAL buffer:', buffer.substring(0, 500));
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
          console.log('[SSE]', evt.type, evt.subtype || '', evt.provider || '', evt.content ? evt.content.substring(0, 30) : '');
          // 调试: 记录心跳接收情况
          if (evt.type === 'heartbeat') {
            console.log('[SSE] Heartbeat received, resetting stream timeout');
          }
          resetStreamTimeout(); // 收到任意事件即续期空闲计时器,避免慢任务被误杀
          // heartbeat 事件:后端发送保活信号,前端仅续期超时但不显示
          if (evt.type === 'heartbeat') {
            // resetStreamTimeout() 已在上面调用,这里仅记录日志
            if (App.DEBUG) console.log('[SSE] Heartbeat processed');
            continue;
          }
          // 图片生成:尽早展示专属"生成中"进度卡。tool_start 为主信号,
          // tool_call_delta / tool_route 为兜底(二者都带工具名,且一定早于结果事件)
          const _toolName = evt.name || evt.tool || '';
          if ((evt.type === 'tool_start' && (evt.names || []).indexOf('generate_image') !== -1) ||
              (evt.type === 'tool_call_delta' && _toolName === 'generate_image') ||
              (evt.type === 'tool_route' && (evt.tools || []).indexOf('generate_image') !== -1)) {
            App.showImageGenerating();
          }
          // 执行轨迹:实时显示每个工具调用
          // 去重记录:避免同一工具结果重复显示(tool_done 和 tool 事件都可能触发)
          if (evt.type === 'tool_start') {
            // 检查是否已有该工具的运行中记录,避免重复添加
            const msgs = App.els.messages;
            const existingRunning = Array.from(msgs.children).reverse().find(n => {
              return n.classList.contains('msg') && n.classList.contains('tool') &&
                     n.querySelector('.tool-label') &&
                     n.querySelector('.tool-label').textContent.includes(evt.name || evt.tool || '') &&
                     !n.classList.contains('done') && !n.classList.contains('fail');
            });
            if (!existingRunning) {
              App.ExecTrace.addStep(evt.name || evt.tool || '工具', evt.arguments || '', 'running');
            }
          } else if (evt.type === 'tool') {
            // tool 事件:通过 inline 卡片在答案上方展示，避免主流重复卡片
            App.removeTyping();
            const ok = !String(evt.result || '').includes('Error') &&
                       !String(evt.result || '').includes('failed');
            App.ExecTrace.updateStep(_toolName, ok ? 'done' : 'fail', evt.result);
            // 创建工具 inline 卡片
            const toolEl = document.createElement('div');
            toolEl.className = 'tool-inline';
            let argsDisplay = '';
            try {
              const obj = typeof evt.arguments === 'string' ? JSON.parse(evt.arguments) : evt.arguments;
              if (obj && typeof obj === 'object') argsDisplay = String(JSON.stringify(obj, null, 2)).slice(0, 100);
            } catch (_) { /* 非 JSON 保持文本 */ }
            toolEl.innerHTML = `<div class="tool-label">🔧 ${App.escapeHtml(_toolName || '工具')}</div>`
                          + (argsDisplay ? `<div class="tool-args">${App.escapeHtml(argsDisplay)}</div>` : '')
                          + (evt.result ? `<div class="tool-result">${App.escapeHtml(String(evt.result).slice(0, 300))}</div>` : '');
            // 插入到最近的 answer doc 之前
            const lastMsg = App.els.messages.querySelector(`[class*="msg"]:last-child .answer-doc`);
            if (lastMsg) { lastMsg.parentNode.insertBefore(toolEl, lastMsg); }
            else { App.els.messages.insertBefore(toolEl, App.els.messages.firstChild); }
          } else if (evt.type === 'tool_done') {
            // tool_done 事件:仅更新轨迹状态,不重复展示结果
            // (如果前端已收到过 tool 事件,会跳过展示;否则作为兜底处理)
            const doneToolName = evt.tool || _toolName;
            // 检查是否已有该工具的显示记录,避免重复
            const msgs = App.els.messages;
            const lastToolMsg = Array.from(msgs.children).reverse().find(n => {
              return n.classList.contains('msg') && n.classList.contains('tool') &&
                     n.querySelector('.tool-label') &&
                     n.querySelector('.tool-label').textContent.includes(doneToolName);
            });
            if (!lastToolMsg) {
              // 没有重复记录,才显示结果(使用新的 inline 结构)
              App.ExecTrace.updateStep(doneToolName, 'done', '');
              const toolEl = document.createElement('div');
              toolEl.className = 'tool-inline';
              toolEl.innerHTML = `<div class="tool-label">🔧 ${App.escapeHtml(doneToolName)}</div>`
                                + (evt.result ? `<div class="tool-result">${App.escapeHtml(String(evt.result).slice(0, 300))}</div>` : '');
              // 插入到最近的 answer doc 之前
              const lastMsg = App.els.messages.querySelector(`[class*="msg"]:last-child .answer-doc`);
              if (lastMsg) { lastMsg.parentNode.insertBefore(toolEl, lastMsg); }
              else { App.els.messages.insertBefore(toolEl, App.els.messages.firstChild); }
            } else {
              // 已有记录,仅更新轨迹状态
              App.ExecTrace.updateStep(doneToolName, 'done', '');
            }
          } else if (evt.type === 'orchestration') {
            // 多智能体编排进度:更新过程轴摘要，不再作为独立消息刷屏
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
            // 更新最近的 process-rail 摘要
            const lastRail = App.els.messages.querySelector('.process-rail:last-child');
            if (lastRail && lastRail.querySelector('.rail-summary')) {
              lastRail.querySelector('.rail-summary').textContent = summary;
            } else if (lastRail) {
              lastRail.querySelector('.rail-timeline .rail-step').textContent = summary;
            }
          } else if (evt.type === 'plan') {
            if (typingShown) { App.removeTyping(); typingShown = false; }
            // 使用新的 inline 计划，插入到答案文档之前
            const planTitle = evt.plan?.steps?.[0]?.title || '规划步骤';
            const planEl = document.createElement('div');
            planEl.className = 'plan-inline';
            planEl.innerHTML = `<span class="plan-label">📋 ${planTitle}</span>`;
            // 插入到最近的 answer doc 之前
            const lastMsg = App.els.messages.querySelector(`[class*="msg"]:last-child .answer-doc`);
            if (lastMsg) { lastMsg.parentNode.insertBefore(planEl, lastMsg); }
            else { App.els.messages.insertBefore(planEl, App.els.messages.firstChild); }
          } else if (evt.type === 'thinking') {
            App.removeTyping();
            // thinking 使用新的 inline 卡片，插入到答案文档之前
            const thinkText = evt.content || '';
            const thinkEl = document.createElement('div');
            thinkEl.className = 'thinking-inline';
            thinkEl.textContent = thinkText.slice(0, 300);
            // 插入到最近的 answer doc 之前
            const lastMsg = App.els.messages.querySelector(`[class*="msg"]:last-child .answer-doc`);
            if (lastMsg) { lastMsg.parentNode.insertBefore(thinkEl, lastMsg); }
            else { App.els.messages.insertBefore(thinkEl, App.els.messages.firstChild); }
          } else if (evt.type === 'token') {
            // 第一次 token 到达:显示并移除打字指示器,创建答案容器
            if (!typingShown) { 
              App.removeTyping(); 
              typingShown = true; 
              // 移除打字指示器
              const msgs = App.els.messages;
              const waitMsg = Array.from(msgs.children).reverse().find(n => n.id === 'typing' || (n.classList.contains('msg') && n.classList.contains('bot') && n.textContent.trim() === '正在思考中...'));
              if (waitMsg) waitMsg.remove();
            }
            if (!answerDiv) {
              // 创建可见的空 bot 消息(先显示容器,后续增量追加内容)
              answerDiv = App.appendMessage('', 'bot');
              // 空文本时 wrapper 默认 display:none,这里解除隐藏以便流式内容可见
              if (answerDiv && answerDiv._wrap) answerDiv._wrap.style.display = '';
              // 记录答案容器锚点,thinking/tool/plan 卡需插入到它之前而非末尾(防止跑到答案后面错位)
              App._answerWrap = answerDiv._wrap || null;
            }
            receivedTokens = true;
            const { text, thinking } = processTokenChunk(evt.content || '');
            if (thinking) {
              const thinkText = App.stripThinkingTags ? App.stripThinkingTags(thinking) : thinking;
              App.appendThinking(thinkText);
            }
            if (text) {
              App._mdBuf += text;
              scheduleRender();
            }
          } else if (evt.type === 'error') {
            done = true;
            isError = true;
            App.clearImageGenerating();
            cleanupStream();
            if (typingShown) App.removeTyping();
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
            // 同一次请求里后端可能对同一失败发多条相同 error(多阶段各自上报),只提示一次
            const errMsg = evt.message || '发生错误';
            if (errMsg !== lastErrMsg) {
              lastErrMsg = errMsg;
              App.appendMessage(errMsg, 'error');
            }
            _finishGroup();
          } else if (evt.type === 'done') {
            done = true;
            App.clearImageGenerating();
            cleanupStream();
            if (typingShown) App.removeTyping();
            // 流式完成后整段重渲染,确保最终内容(含跨切片块级结构)正确
            if (answerDiv) {
              const finalMd = App._mdBuf || '';
              let finalHtml = '';
              try {
                finalHtml = App.renderMarkdown(finalMd);
              } catch (renderErr) {
                console.error('[renderMarkdown] done handler ERROR:', renderErr);
                finalHtml = '<p>' + App.escapeHtml(finalMd) + '</p>';  // fallback: 转义后再包裹,避免原始 JSON/脚本被当作 markdown 渲染
              }
              answerDiv.innerHTML = finalHtml;
            }
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
            // 检查是否有待确认的中断
            const intr = evt.interrupt;
            if (intr && intr.type === 'confirmation' && intr.requests) {
              if (App.Confirmation && App.Confirmation.show) {
                App.Confirmation.show(intr.requests, intr.run_id);
                // 记录中断集合,批准后自动续跑
                App.Confirmation.trackInterrupt({ run_id: intr.run_id, interrupt: intr });
              }
            } else if (!receivedTokens && (!evt.steps || !Array.isArray(evt.steps) || evt.steps.length === 0)) {
              // 没有 token,也没有 steps,也没有 interrupt:后端返回了完全空的响应
              App.refreshWelcomePage();
              App.appendMessage('未收到响应,请重试', 'error');
            }
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
            App.clearImageGenerating();
            cleanupStream();
            if (typingShown) App.removeTyping();
            if (answerDiv) {
              const finalMd = App._mdBuf || '';
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
            const intr = evt.interrupt;
            if (intr && intr.type === 'confirmation' && intr.requests) {
              if (App.Confirmation && App.Confirmation.show) {
                App.Confirmation.show(intr.requests, intr.run_id);
                App.Confirmation.trackInterrupt({ run_id: intr.run_id, interrupt: intr });
              }
            } else if (!receivedTokens && (!evt.steps || !Array.isArray(evt.steps) || evt.steps.length === 0)) {
              App.refreshWelcomePage();
              App.appendMessage('未收到响应,请重试', 'error');
            }
          } else if (evt.type === 'error' && !done && !isError) {
            done = true; isError = true;
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
            const errMsg = evt.message || '发生错误';
            if (errMsg !== lastErrMsg) { lastErrMsg = errMsg; App.appendMessage(errMsg, 'error'); }
            _finishGroup();
          } else if (evt.type === 'token') {
            // 残余 token 片段:执行最少必要的追加与渲染(不在此处创建 answerDiv,避免残留)
            if (answerDiv) {
              receivedTokens = true;
              const { text, thinking } = processTokenChunk(evt.content || '');
              if (thinking) {
                const thinkText = App.stripThinkingTags ? App.stripThinkingTags(thinking) : thinking;
                App.appendThinking(thinkText);
              }
              if (text) { App._mdBuf += text; scheduleRender(); }
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
      App.els.messages.scrollTop = App.els.messages.scrollHeight;
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
        App.els.messages.scrollTop = App.els.messages.scrollHeight;
        pill.remove();
      });
      // 移除现有的 pill (防止重复)
      const old = document.getElementById('scroll-pill');
      if (old) old.remove();
      document.body.appendChild(pill);
      setTimeout(() => pill.style.opacity = '1', 100);
    }

    // 流被异常中断:直接显示错误,不再回退非流式接口
    // 只要有收到过任何合法 SSE 事件(含后端只返回空 done),就不要再显示"响应中断"
    if (!done && !receivedTokens && !receivedAnyEvent && !isError) {
      App.removeTyping();
      if (renderTimer !== null) { clearTimeout(renderTimer); renderTimer = null; }
      App.appendMessage('响应中断,请稍后重试', 'error');
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
    clearTimeout(connectTimer); // 兜底清理建连超时
    // 确保分组被关闭
    if (App._currentGroup) {
      _finishGroup(answerDiv ? (answerDiv.textContent || '') : null);
    }
    // 重置 mdBuf 防止下次流式时累积
    App._mdBuf = '';
    App.Chat.setStreaming(false);
    App.abortController = null;
    if (App.Attach) App.Attach.reset();
    if (App.CtxMeter) App.CtxMeter.refresh();
    App.els.input.focus();
  }
};

App.Chat.stop = function (silent) {
  if (App.abortController) {
    try { App.abortController.abort(); } catch (_) { /* 忽略 */ }
  }
  App.Chat.setStreaming(false);
  if (silent) {
    App.removeTyping();
  } else {
    App.removeTyping();
    App.appendMessage('⏹ 已停止生成', 'bot');
  }
  // 停止时也完成分组
  if (App._currentGroup) _finishGroup();
  App.abortController = null;
};

App.Chat.setStreaming = function (val) {
  App.streaming = val;
  document.body.classList.toggle('streaming', val);
  App.els.sendBtn.disabled = val || !(App.els.input && App.els.input.value.trim());
};

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
  App.appendMessage(`🖼 识别图片: ${file.name}`, 'user');
  App.showTyping();
  App.Chat.setStreaming(true);
  try {
    const res = await App.api('/v1/vision', { method: 'POST', body: fd });
    const data = await res.json();
    App.removeTyping();
    if (data.error) {
      App.appendMessage(`❌ ${data.error}`, 'error');
    } else if (data.answer) {
      App.appendMessage(data.answer, 'bot');
    } else {
      App.appendMessage('未能识别图片内容', 'bot');
    }
  } catch (e) {
    App.removeTyping();
    App.appendMessage(`图片识别失败: ${e.message}`, 'error');
  } finally {
    App.Chat.setStreaming(false);
    App.selectedImageFile = null;
    App.els.imgPreviewBar.classList.remove('show');
    App.els.input.focus();
  }
};
