/* 聊天模块:SSE 流式对话 / 工具事件 / 停止生成 / 图片识别 */
App.Chat = {};

/* 组装协作模式参数:mode / 评审模型 / 多模型清单(从 UI 读取,本地持久化) */
App.Chat.modeParams = function () {
  const mode = (App.els.modeSelect && App.els.modeSelect.value) || 'auto';
  const critic = (App.els.criticModelInput && App.els.criticModelInput.value.trim()) || '';
  const ensembleRaw = (App.els.ensembleModelsInput && App.els.ensembleModelsInput.value.trim()) || '';
  let criticProvider = '';
  let criticModel = '';
  if (critic) {
    const [p, m] = critic.split(':');
    criticProvider = p || '';
    criticModel = m || '';
  }
  const models = ensembleRaw.split(',').map((s) => s.trim()).filter(Boolean);
  return {
    mode,
    ...(criticProvider ? { critic_provider: criticProvider, critic_model: criticModel } : {}),
    ...(models.length ? { models } : {}),
  };
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
      const b = nodes[i].querySelector('.bubble');
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
  const input = App.els.input;
  input.value = '';
  if (App.syncComposer) App.syncComposer();
  App._planCard = null;
  App.removeTyping();
  if (!opts.skipAppendUser) App.appendMessage(text, 'user');
  App.showTyping();
  App.Chat.setStreaming(true);
  _startGroup();  // 开始工具分组

  App.abortController = new AbortController();
  const signal = App.abortController.signal;
  let receivedTokens = false;
  let isError = false;   // 必须声明,超时回调会检查它

  let res = null;
  let retries = 0;
  try {
    while (!res && retries < 3) {
      try {
        res = await App.api('/v1/chat/stream', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(App.Attach ? App.Attach.patchBody({
            message: text,
            session_id: App.sessionId,
            stream: true,
            provider: App.selectedProvider() || null,
            ...App.Chat.modeParams(),
          }) : {
            message: text,
            session_id: App.sessionId,
            stream: true,
            provider: App.selectedProvider() || null,
            ...App.Chat.modeParams(),
          }),
          signal,
        });
      } catch (fetchErr) {
        retries++;
        if (retries >= 3) throw fetchErr;
        await new Promise(r => setTimeout(r, 1000 * retries));
      }
    }
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let answerDiv = null;
    let mdBuf = '';
    let buffer = '';
    let done = false;
    // 节流渲染:多个 token 合并后每帧一次 re-render,避免 O(n²) 全量 DOM 重排
    let renderTimer = null;
    // 流读取超时:后端卡住时主动断开,避免"执行中"永久不消失
    let streamTimeout = setTimeout(() => {
      streamTimeout = null;
      if (!done && !isError) {
        done = true;
        isError = true;
        App.removeTyping();
        if (renderTimer !== null) { clearTimeout(renderTimer); renderTimer = null; }
        App.appendMessage('响应超时,请稍后重试', 'error');
        _finishGroup();
      }
    }, 120000); // 120s 整体超时

    function scheduleRender() {
      if (renderTimer !== null) return;
      renderTimer = setTimeout(() => {
        renderTimer = null;
        if (answerDiv) {
          // 流式渲染：使用简化模式，避免重复解析
          answerDiv.textContent = mdBuf;
          App.els.messages.scrollTop = App.els.messages.scrollHeight;
        }
      }, 16);
    }

    while (true) {
      let isDone;
      try {
        const result = await reader.read();
        ({ done: isDone, value } = result);
      } catch (_) {
        // 连接中断（如用户停止/切换会话）
        isError = true;
        break;
      }
      if (isDone) break;
      buffer += decoder.decode(value, { stream: true });

      const parts = buffer.split('\n\n');
      buffer = parts.pop();
      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith('data: ')) continue;
        try {
          const evt = JSON.parse(line.slice(6));
          if (evt.type === 'tool') {
            App.removeTyping();
            // 使用 Group 收集工具事件,避免散落显示
            if (App.Group && App.Group.addTool) {
              const ok = !String(evt.result || '').includes('Error') &&
                         !String(evt.result || '').includes('failed');
              App.Group.addTool(evt.name || evt.tool || '工具', evt.arguments, evt.result || '', ok);
            } else {
              App.appendToolEvent(evt.name || evt.tool || '工具', evt.arguments, evt.result || '');
            }
          } else if (evt.type === 'plan') {
            App.removeTyping();
            if (App.Group && App.Group.addPlanStep) {
              App.Group.addPlanStep({ title: evt.plan?.steps?.[0]?.title || '规划步骤' }, 0);
            }
            App.appendPlan(evt);
          } else if (evt.type === 'thinking') {
            App.removeTyping();
            if (App.Group && App.Group.setThinking) {
              App.Group.setThinking(evt.content || '');
            }
            App.appendThinking(evt.content || '');
          } else if (evt.type === 'token') {
            if (!answerDiv) {
              App.removeTyping();
              answerDiv = App.appendMessage('', 'bot');
            }
            receivedTokens = true;
            mdBuf += evt.content;
            scheduleRender();
          } else if (evt.type === 'error') {
            done = true;
            isError = true;
            App.removeTyping();
            if (renderTimer !== null) { clearTimeout(renderTimer); renderTimer = null; }
            if (answerDiv) { answerDiv.innerHTML = App.renderMarkdown(mdBuf); }
            App.appendMessage(evt.message || '发生错误', 'error');
            _finishGroup();
          } else if (evt.type === 'done') {
            done = true;
            App.removeTyping();
            if (renderTimer !== null) { clearTimeout(renderTimer); renderTimer = null; }
            if (answerDiv) { answerDiv.innerHTML = App.renderMarkdown(mdBuf); }
            // 关闭分组并传入最终答案文本
            _finishGroup(answerDiv ? (answerDiv.textContent || '') : null);
            // 检查是否有待确认的中断
            const intr = evt.interrupt;
            if (intr && intr.type === 'confirmation' && intr.requests && !receivedTokens) {
              if (App.Confirmation && App.Confirmation.show) App.Confirmation.show(intr.requests, intr.run_id);
            } else if (!receivedTokens) {
              App.appendMessage('(无内容)', 'bot');
            }
          }
        } catch (e) {
          console.warn('parse sse error', e);
        }
      }
    }

    // 清理流超时计时器
    if (streamTimeout !== null) { clearTimeout(streamTimeout); streamTimeout = null; }

    // 未收到任何 token 且流被异常中断:回退非流式接口
    if (!done && !receivedTokens) {
      App.removeTyping();
      if (renderTimer !== null) { clearTimeout(renderTimer); renderTimer = null; }
      const fallback = await App.api('/v1/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(App.Attach ? App.Attach.patchBody({
          message: text,
          session_id: App.sessionId,
          provider: App.selectedProvider() || null,
          ...App.Chat.modeParams(),
        }) : {
          message: text,
          session_id: App.sessionId,
          provider: App.selectedProvider() || null,
          ...App.Chat.modeParams(),
        }),
      });
      const data = await fallback.json();
      const div = App.appendMessage(data.answer || data.detail || '请求失败', data.detail ? 'error' : 'bot');
      if (data.answer && !data.detail) div.innerHTML = App.renderMarkdown(data.answer);
      // 回退路径也完成分组
      _finishGroup(data.answer || null);
    }
  } catch (e) {
    // AbortError 由 stop() 处理(已追加提示),这里仅做静默清理
    if (renderTimer !== null) { clearTimeout(renderTimer); renderTimer = null; }
    if (e.name !== 'AbortError') {
      App.removeTyping();
      App.appendMessage(`请求失败: ${e.message}`, 'error');
      _finishGroup();
    } else {
      App.removeTyping();
      _finishGroup();
    }
  } finally {
    // 确保分组被关闭
    if (App._currentGroup) {
      _finishGroup(answerDiv ? (answerDiv.textContent || '') : null);
    }
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
      const div = App.appendMessage(data.answer, 'bot');
      div.innerHTML = App.renderMarkdown(data.answer);
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
