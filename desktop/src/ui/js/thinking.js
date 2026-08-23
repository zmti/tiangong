/* 思考过程渲染 */
(function () {
  const PLAN_ICON = { pending: '○', in_progress: '◐', done: '✓', failed: '✕', skipped: '—' };

  function planStatusText(s) {
    return { pending: '待办', in_progress: '进行中', done: '已完成', failed: '失败', skipped: '跳过' }[s] || s;
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
      const anchor = App._answerWrap;
      if (anchor && anchor.parentNode === App.els.messages) {
        App.els.messages.insertBefore(div, anchor);
      } else {
        App.els.messages.appendChild(div);
      }
      App._thinkingWrap = div;
    }
    const body = App._thinkingWrap.querySelector('.think-body');
    body.textContent = App.stripThinkingTags(content).slice(0, 2000);
    App.els.messages.scrollTop = App.els.messages.scrollHeight;
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
        App.els.messages.appendChild(div);
      }
      App._planCard = div;
    } else {
      div = App._planCard;
    }
    renderPlanCard(div, plan);
    if (!appendTo) App.els.messages.scrollTop = App.els.messages.scrollHeight;
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
