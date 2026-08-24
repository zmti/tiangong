/* 请求分组:将一次对话的工具调用/思考/计划折叠为单个卡片 */
(function () {
  // 图标映射
  const TOOL_ICONS = { read: '📖', write: '✏️', exec: '▶️', search: '🔍', browser: '🌐', edit: '🔧', system: '⚙️', tool: '🔨' };

  function getIcon(name) {
    const n = (name || '').toLowerCase();
    for (const [k, v] of Object.entries(TOOL_ICONS)) {
      if (n.includes(k)) return v;
    }
    return '🔨';
  }

  // 开始分组
  function start() {
    App._groupId++;
    const gid = App._groupId;
    const wrap = document.createElement('div');
    wrap.className = 'req-group';
    wrap.dataset.gid = gid;
    const head = document.createElement('div');
    head.className = 'req-group-head';
    head.innerHTML = '<span class="rg-pulse">⏳</span><span class="rg-label">执行中…</span>';
    const body = document.createElement('div');
    body.className = 'req-group-body';
    body.style.display = 'none';
    wrap.appendChild(head);
    wrap.appendChild(body);
    App.els.messages.appendChild(wrap);
    App.els.messages.scrollTop = App.els.messages.scrollHeight;
    App._currentGroup = { gid, wrap, head, body, tools: [], thinking: null, plan: null, started: Date.now() };
    return gid;
  }

  // 添加工具
  function addTool(name, args, result, success) {
    if (!App._currentGroup) {
      App.appendToolEvent(name, args, result);
      return;
    }
    const g = App._currentGroup;
    const ok = success === true;
    g.tools.push({ name, args, result, ok, time: Date.now() });
    const icon = getIcon(name);
    g.head.innerHTML = `<span class="rg-pulse">${icon}</span><span class="rg-label">${name} ${ok ? '✓' : '✗'}</span>`;
    if (!g._detailVisible) {
      const item = document.createElement('div');
      item.className = 'rg-tool-item' + (ok ? '' : ' rg-tool-fail');
      item.innerHTML = `<span class="rg-t-icon">${icon}</span><span class="rg-t-name">${App.escapeHtml(name)}</span><span class="rg-t-status">${ok ? '✓' : '✗'}</span>`;
      item.title = `工具: ${name}\n参数: ${(typeof args === 'string' ? args : JSON.stringify(args) || '').slice(0, 120)}`;
      g.body.appendChild(item);
      g._detailVisible = true;
      g.body.style.display = 'flex';
    } else {
      const item = document.createElement('div');
      item.className = 'rg-tool-item' + (ok ? '' : ' rg-tool-fail');
      item.innerHTML = `<span class="rg-t-icon">${icon}</span><span class="rg-t-name">${App.escapeHtml(name)}</span><span class="rg-t-status">${ok ? '✓' : '✗'}</span>`;
      g.body.appendChild(item);
    }
  }

  // 设置思考内容
  function setThinking(content) {
    if (!App._currentGroup) { 
      App.appendThinking(content); 
      return; 
    }
    const text = App.stripThinkingTags(content);
    if (!App._currentGroup.thinking) {
      const el = document.createElement('div');
      el.className = 'rg-thinking';
      el.textContent = text.slice(0, 300);
      el.title = text.slice(0, 2000);
      App._currentGroup.body.appendChild(el);
      App._currentGroup.thinking = el;
      if (!App._currentGroup._detailVisible) {
        App._currentGroup.body.style.display = 'flex';
        App._currentGroup._detailVisible = true;
      }
    } else {
      App._currentGroup.thinking.textContent = text.slice(0, 300);
      App._currentGroup.thinking.title = text.slice(0, 2000);
    }
  }

  // 添加计划步骤
  function addPlanStep(step, total) {
    if (!App._currentGroup) { 
      App.appendPlan({ plan: { steps: [step] } }); 
      return; 
    }
    const idx = App._currentGroup.planCount || 0;
    App._currentGroup.planCount = idx + 1;
    if (!App._currentGroup._detailVisible) {
      App._currentGroup.body.style.display = 'flex';
      App._currentGroup._detailVisible = true;
    }
  }

  // 结束分组
  function finish(answer, isError) {
    if (!App._currentGroup) return null;
    const g = App._currentGroup;
    const n = g.tools.length;
    const okCount = g.tools.filter(t => t.ok).length;
    const dur = Math.round((Date.now() - g.started) / 1000);
    let headHtml;
    if (isError) {
      headHtml = '<span class="rg-pulse">❌</span><span class="rg-label">执行失败</span>';
    } else if (n === 0) {
      headHtml = '<span class="rg-pulse">💬</span><span class="rg-label">直接回复</span>';
    } else {
      headHtml = `<span class="rg-pulse">✅</span><span class="rg-label">${n} 个工具 · ${okCount}/${n} 成功 · ${dur}s</span>`;
      const badge = document.createElement('span');
      badge.className = 'rg-toggle';
      badge.textContent = '▼';
      badge.addEventListener('click', () => {
        const open = g.body.style.display === 'block';
        g.body.style.display = open ? 'none' : 'block';
        badge.textContent = open ? '▼' : '▲';
      });
      g.head.appendChild(badge);
    }
    const closeBtn = document.createElement('span');
    closeBtn.className = 'rg-close';
    closeBtn.textContent = '✕';
    closeBtn.addEventListener('click', (e) => { e.stopPropagation(); g.wrap.remove(); });
    g.head.innerHTML = headHtml;
    g.head.appendChild(closeBtn);
    g.wrap.classList.add('req-group-done');
    App._currentGroup = null;
    return n;
  }

  window.App = window.App || {};
  App.Group = { start, addTool, setThinking, addPlanStep, finish };
})();
