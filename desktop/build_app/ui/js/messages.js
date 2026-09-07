/* 消息 DOM 操作:appendMessage, appendToolEvent 等 */
(function () {
  // 头像生成
  function makeAvatar(type) {
    const a = document.createElement('div');
    a.className = 'avatar';
    a.textContent = type === 'user' ? '👤' : '🤖';
    return a;
  }

  // 时间显示
  function currentTime() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, '0');
    return `${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  // 主消息渲染
  function appendMessage(text, type = 'bot') {
    const wrap = document.createElement('div');
    wrap.className = `msg ${type}`;

    if (type === 'user' || type === 'bot') {
      if (type === 'bot' && !text) {
        wrap.style.display = 'none';
      }
      const avatar = makeAvatar(type);
      const rail = document.createElement('div');
      rail.className = 'process-rail';
      rail.innerHTML = '<span class="rail-summary">⚙ 已完成</span>';
      rail.title = '点击展开过程';
      rail.addEventListener('click', () => rail.classList.toggle('open'));
      const answer = document.createElement('div');
      answer.className = 'answer-doc';
      if (type === 'bot') {
        if (text) answer.innerHTML = App.renderMarkdown(text);
        else answer.textContent = '';
      } else {
        answer.textContent = text;
      }
      const act = document.createElement('div');
      act.className = 'msg-actions';
      const timeEl = document.createElement('div');
      timeEl.className = 'msg-time';
      timeEl.textContent = currentTime();

      if (type === 'bot') {
        const mk = (title, label, fn) => {
          const b = document.createElement('button');
          b.className = 'msg-action';
          b.title = title;
          b.textContent = label;
          b.addEventListener('click', fn);
          act.appendChild(b);
        };
        mk('复制回复', '复制', () => {
          const txt = (answer.textContent || answer.innerText || '').trim();
          if (txt) navigator.clipboard.writeText(txt).catch(() => {});
        });
        mk('重新生成', '重新生成', () => { if (App.Chat) App.Chat.regenerate(wrap); });
        mk('朗读该回复', '🔊', () => {
          const txt = (answer.textContent || answer.innerText || '').trim();
          if (txt) App.speak(txt);
        });
      } else {
        const mk = (title, label, fn) => {
          const b = document.createElement('button');
          b.className = 'msg-action';
          b.title = title;
          b.textContent = label;
          b.addEventListener('click', fn);
          act.appendChild(b);
        };
        mk('复制这条消息', '复制', () => {
          const txt = (answer.textContent || '').trim();
          if (txt) navigator.clipboard.writeText(txt).catch(() => {});
        });
        mk('编辑这条消息并重新发送', '编辑', () => { if (App.Chat) App.Chat.editMessage(wrap, answer); });
      }

      wrap.appendChild(avatar);
      const body = document.createElement('div');
      body.className = 'msg-body';
      body.appendChild(rail);
      body.appendChild(answer);
      body.appendChild(act);
      body.appendChild(timeEl);
      wrap.appendChild(body);
      App.els.messages.appendChild(wrap);
      App.els.messages.scrollTop = App.els.messages.scrollHeight;
      if (type === 'user') App.refreshWelcomePage();
      if (answer) answer._wrap = wrap;
      return answer;
    }

    // 非 user/bot 类型
    if (type === 'error') {
      const el = document.createElement('div');
      el.className = 'msg error';
      el.textContent = text || '';
      App.els.messages.appendChild(el);
      App.els.messages.scrollTop = App.els.messages.scrollHeight;
      return el;
    }
    if (type === 'system') {
      const el = document.createElement('div');
      el.className = 'msg system';
      el.textContent = text || '';
      App.els.messages.appendChild(el);
      App.els.messages.scrollTop = App.els.messages.scrollHeight;
      return el;
    }
    // thinking / tool / plan
    const lastMsg = App.els.messages.querySelector(`[class*="msg"]:last-child`);
    const target = lastMsg ? (lastMsg.querySelector('.answer-doc') || lastMsg) : App.els.messages;
    if (type === 'thinking') {
      const thinkingEl = document.createElement('div');
      thinkingEl.className = 'thinking-inline';
      thinkingEl.textContent = text || '';
      target.prepend(thinkingEl);
      return thinkingEl;
    } else if (type === 'tool') {
      const toolEl = document.createElement('div');
      toolEl.className = 'tool-inline';
      toolEl.innerHTML = `<div class="tool-label">🔧 ${text || '工具'}</div>`;
      target.prepend(toolEl);
      return toolEl;
    } else if (type === 'plan') {
      const planEl = document.createElement('div');
      planEl.className = 'plan-inline';
      planEl.innerHTML = `<span class="plan-label">📋 ${text || '计划'}</span>`;
      target.prepend(planEl);
      return planEl;
    }
    // 兜底
    const fb = document.createElement('div');
    fb.className = 'msg';
    fb.textContent = text || '';
    App.els.messages.appendChild(fb);
    return fb;
  }

  // 工具事件渲染
  function appendToolEvent(name, args, result, appendTo = null) {
    // 图片生成特殊处理
    if (name === 'generate_image') {
      const url = App.parseImageUrl(result);
      if (url) {
        const wrap = document.createElement('div');
        wrap.className = 'msg bot';
        const avatar = makeAvatar('bot');
        const body = document.createElement('div');
        body.className = 'msg-body';
        body.innerHTML = `<div class="img-gen-bubble img-gen-history"><img class="img-gen-result" src="${App.escapeHtml(url)}" alt="生成结果"></div>`;
        wrap.appendChild(avatar);
        wrap.appendChild(body);
        if (appendTo) {
          if (Array.isArray(appendTo)) appendTo.push(wrap);
          else appendTo.appendChild(wrap);
        } else {
          App.els.messages.appendChild(wrap);
          App.els.messages.scrollTop = App.els.messages.scrollHeight;
        }
        return wrap;
      }
    }

    const div = document.createElement('div');
    div.className = 'msg tool';

    const label = document.createElement('div');
    label.className = 'tool-label';
    label.textContent = `🔧 工具调用: ${name}`;
    div.appendChild(label);

    if (args) {
      let argsText = args;
      try {
        const obj = typeof args === 'string' ? JSON.parse(args) : args;
        if (obj && typeof obj === 'object') argsText = JSON.stringify(obj, null, 2);
      } catch (_) {}
      if (argsText && argsText !== '{}' && argsText !== '""') {
        const argsEl = document.createElement('div');
        argsEl.className = 'tool-args';
        argsEl.textContent = String(argsText).slice(0, 200);
        div.appendChild(argsEl);
      }
    }

    // 工具结果
    let text = result || '';
    let parsedObj = null;
    try {
      const obj = typeof result === 'string' ? JSON.parse(result) : result;
      if (obj && typeof obj === 'object') {
        parsedObj = obj;
        text = JSON.stringify(obj, null, 2).slice(0, 600);
      }
    } catch (_) {}

    if (text) {
      const body = document.createElement('div');
      body.className = 'tool-result';
      body.textContent = text.slice(0, 400);
      div.appendChild(body);
    }

    if (appendTo) {
      if (Array.isArray(appendTo)) appendTo.push(div);
      else appendTo.appendChild(div);
    } else {
      App.els.messages.appendChild(div);
      App.els.messages.scrollTop = App.els.messages.scrollHeight;
    }
    return div;
  }

  // 打字指示器
  function showTyping() {
    const wrap = document.createElement('div');
    wrap.className = 'msg bot';
    wrap.id = 'typing';
    const avatar = makeAvatar('bot');
    const body = document.createElement('div');
    body.className = 'msg-body';
    const bubble = document.createElement('div');
    bubble.className = 'answer-doc';
    bubble.innerHTML = '<div class="typing-indicator"><span></span><span></span><span></span></div>';
    body.appendChild(bubble);
    wrap.appendChild(avatar);
    wrap.appendChild(body);
    App.els.messages.appendChild(wrap);
    App.els.messages.scrollTop = App.els.messages.scrollHeight;
  }

  function removeTyping() {
    const t = document.getElementById('typing');
    if (t) t.remove();
  }

  window.App = window.App || {};
  App.makeAvatar = makeAvatar;
  App.appendMessage = appendMessage;
  App.appendToolEvent = appendToolEvent;
  App.showTyping = showTyping;
  App.removeTyping = removeTyping;
})();
