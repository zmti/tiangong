/* 思考过程渲染 */
(function () {
  const PLAN_ICON = { pending: '○', in_progress: '◐', done: '✓', failed: '✕', skipped: '—' };

  function planStatusText(s) {
    return { pending: '待办', in_progress: '进行中', done: '已完成', failed: '失败', skipped: '跳过' }[s] || s;
  }

  // 找插入锚点:优先找 .answer-doc,其次找 bot msg wrapper,最后找 messages
  function _findInsertTarget() {
    // _answerWrap 可能是 DOM 元素或 { _answer, _rail } 对象
    const wrap = App._answerWrap;
    if (!wrap) return null;
    // 如果是带 _answer 属性的对象，取 _answer 元素
    const el = wrap._answer || wrap;
    if (el && el.nodeType === 1) {
      // 新结构: .answer-doc 在 .msg-content 内,不在 messages 直接子级
      if (el.classList && el.classList.contains('answer-doc')) return el;
      const doc = el.querySelector ? el.querySelector('.answer-doc') : null;
      if (doc) return doc;
      if (el.parentNode === App.Chat.pane()) return el;
    }
    return null;
  }

  function appendThinking(content) {
    if (App._currentGroup && App.Group && App.Group.setThinking) {
      App.Group.setThinking(content);
      return null;
    }
    if (!App._thinkingWrap) {
      const div = document.createElement('div');
      div.className = 'msg thinking';
      const label = document.createElement('div');
      label.className = 'think-label';
      label.textContent = '💭 思考过程';
      const body = document.createElement('div');
      body.className = 'think-body';
      label.addEventListener('click', () => div.classList.toggle('open'));
      div.appendChild(label);
      div.appendChild(body);
      // 插入到当前 bot 答案之前
      const insertTarget = _findInsertTarget();
      if (insertTarget) {
        insertTarget.parentNode.insertBefore(div, insertTarget);
      } else {
        App.Chat.pane().appendChild(div);
      }
      App._thinkingWrap = div;
    }
    const body = App._thinkingWrap.querySelector('.think-body');
    // 流式安全:追加而非替换,不 trim(保住 chunk 边界的换行/段落分隔)
    const clean = App.stripThinkingTagsStream ? App.stripThinkingTagsStream(content) : content;
    // ⚠️ 判空不能用 .trim():换行符独占 chunk 时会被整块丢弃
    if (!clean) { App.scrollMessagesToBottom(); return App._thinkingWrap; }
    App._thinkingRawAlt = ((App._thinkingRawAlt || '') + clean).slice(0, 2000);
    // Markdown 渲染:**加粗**/###/列表等语法不能裸露(textContent 会原样显示)
    if (App.renderMarkdown) {
      body.classList.add('md');
      try { body.innerHTML = App.renderMarkdown(App._thinkingRawAlt); }
      catch (e) { body.textContent = App._thinkingRawAlt; }
    } else {
      body.textContent = App._thinkingRawAlt;
    }
    App.scrollMessagesToBottom();
    return App._thinkingWrap;
  }

  function appendPlan(evt, appendTo = null) {
    const plan = evt.plan;
    if (!plan) return null;
    let div;
    const isNew = evt.action === 'create' || !App._planCard;
    if (isNew) {
      div = document.createElement('div');
      div.className = 'msg plan';
      const label = document.createElement('div');
      label.className = 'plan-label';
      label.textContent = '📋 任务计划';
      div.appendChild(label);
      if (appendTo) {
        if (Array.isArray(appendTo)) appendTo.push(div);
        else appendTo.appendChild(div);
      } else {
        App.Chat.pane().appendChild(div);
      }
      App._planCard = div;
    } else {
      div = App._planCard;
    }
    renderPlanCard(div, plan);
    // 插入到当前 bot 答案之前
    if (!appendTo) {
      const insertTarget = _findInsertTarget();
      if (insertTarget) {
        insertTarget.parentNode.insertBefore(div, insertTarget);
      } else {
        App.Chat.pane().appendChild(div);
      }
    }
    App.scrollMessagesToBottom();
    return div;
  }

  function renderPlanCard(div, plan) {
    div.querySelectorAll('.plan-steps').forEach((el) => el.remove());
    const list = document.createElement('div');
    list.className = 'plan-steps';
    (plan.steps || []).forEach((s, i) => {
      const row = document.createElement('div');
      row.className = `plan-step ${s.status}`;
      const icon = document.createElement('span');
      icon.className = 'plan-icon';
      icon.textContent = PLAN_ICON[s.status] || '○';
      const txt = document.createElement('span');
      txt.className = 'plan-text';
      txt.innerHTML = `<strong>${i + 1}. ${App.escapeHtml(s.title || '')}</strong>`
        + (s.description ? `<span class="plan-desc">${App.escapeHtml(s.description)}</span>` : '');
      const st = document.createElement('span');
      st.className = 'plan-status';
      st.textContent = planStatusText(s.status);
      row.appendChild(icon);
      row.appendChild(txt);
      row.appendChild(st);
      list.appendChild(row);
    });
    if (plan.completed) {
      const ok = document.createElement('div');
      ok.className = 'plan-done';
      ok.textContent = '✅ 计划全部完成';
      list.appendChild(ok);
    }
    div.appendChild(list);
  }

  window.App = window.App || {};
  App.appendThinking = appendThinking;
  App.appendPlan = appendPlan;
  App.renderPlanCard = renderPlanCard;
})();
