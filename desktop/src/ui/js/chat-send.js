/* 聊天核心发送模块:模式参数 / 发送 / 重新生成 / 编辑 */
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
