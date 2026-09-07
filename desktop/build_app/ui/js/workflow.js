/**
 * 工作流 UI 模块 (终极优化版) — 拖拽模板、自动布局、缩放控制、完整历史
 */
const App = window.App || {};

App.prompt = function(message, defaultValue) {
  return new Promise(resolve => {
    const mask = document.createElement('div');
    mask.className = 'modal-mask';
    const card = document.createElement('div');
    card.className = 'confirm-dialog';
    const body = document.createElement('div');
    body.className = 'cd-body';
    body.textContent = message;
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'cd-input';
    input.value = defaultValue || '';
    const actions = document.createElement('div');
    actions.className = 'cd-actions';
    const cancel = document.createElement('button');
    cancel.className = 'lb-btn cd-cancel';
    cancel.textContent = '取消';
    const ok = document.createElement('button');
    ok.className = 'lb-btn cd-ok';
    ok.textContent = '确定';
    actions.appendChild(cancel);
    actions.appendChild(ok);
    card.appendChild(body);
    card.appendChild(input);
    card.appendChild(actions);
    mask.appendChild(card);
    document.body.appendChild(mask);
    input.focus();
    input.select();
    function done(val) { mask.remove(); resolve(val); }
    cancel.addEventListener('click', () => done(null));
    ok.addEventListener('click', () => done(input.value));
    input.addEventListener('keydown', e => { if (e.key === 'Enter') done(input.value); if (e.key === 'Escape') done(null); });
    mask.addEventListener('click', e => { if (e.target === mask) done(null); });
  });
};

App.Workflow = (() => {
  // ── API ───────────────────────────────────────────────────────
  function _apiBase() { return App.backendUrl || 'http://127.0.0.1:8517'; }
  function _apiKey()  { return App.backendApiKey || ''; }

  async function _fetch(path, opts = {}) {
    const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
    const key = _apiKey();
    if (key) headers['Authorization'] = 'Bearer ' + key;
    opts.headers = headers;
    const resp = await fetch(_apiBase() + path, opts);
    if (!resp.ok) {
      let detail = '请求失败';
      try { detail = (await resp.json()).detail || detail; } catch(_) {}
      throw new Error(detail);
    }
    return resp.json();
  }

  // ── 状态 ──────────────────────────────────────────────────────
  let currentWorkflow = null;
  let nodes = [];
  let edges = [];
  let nodeElements = new Map();
  let selectedNodeId = null;
  let svgEl = null;
  let activeRunId = null;
  let runHistory = [];
  let viewScale = 1;
  let viewOffset = { x: 0, y: 0 };

  const NODE_W = 140;
  const NODE_H = 64;
  const PAD    = 60;
  const GRID   = 20;

  function $(id) { return document.getElementById(id); }

  // ── 节点模板配置 ──────────────────────────────────────────────
  const NODE_TEMPLATES = [
    { type: 'start',    icon: '▶',  label: '开始',    color: '#22c55e', desc: '工作流入口' },
    { type: 'end',      icon: '⏹',  label: '结束',    color: '#f59e0b', desc: '工作流出口' },
    { type: 'agent',    icon: '🤖', label: 'Agent',   color: '#6366f1', desc: '调用 AI 智能体' },
    { type: 'tool',     icon: '🔧', label: '工具',    color: '#06b6d4', desc: '执行工具调用' },
    { type: 'condition',icon: '⚡', label: '条件',    color: '#8b5cf6', desc: '条件分支判断' },
    { type: 'loop',     icon: '🔁', label: '循环',    color: '#f97316', desc: '循环执行' },
    { type: 'http',     icon: '🌐', label: 'HTTP',    color: '#14b8a6', desc: '发送 HTTP 请求' },
    { type: 'script',   icon: '📜', label: '脚本',    color: '#eab308', desc: '执行 Python 脚本' },
    { type: 'sleep',    icon: '⏸',  label: '等待',    color: '#64748b', desc: '延时等待' },
  ];

  function nodeStyle(type) {
    return NODE_TEMPLATES.find(t => t.type === type) || { icon: '●', color: '#94a3b8', label: type };
  }

  function snap(v) { return Math.round(v / GRID) * GRID; }
  function genId(prefix) { return prefix + '_' + Date.now(); }

  // ── 拖拽状态 ──────────────────────────────────────────────────
  let _dragNode = null, _dragEl = null, _dragRect = null;
  let _mx0 = 0, _my0 = 0, _ox0 = 0, _oy0 = 0;
  let _edgeDraw = null;
  let _tempLine = null;
  let _hoveredPort = null;
  let _isConnecting = false;
  let _templateDrag = null; // 从模板面板拖拽

  // ── SVG 工具 ──────────────────────────────────────────────────
  function ensureSvg() {
    const container = $('wfDesigner');
    if (!container) return null;
    let svg = $('wfSvg');
    if (!svg) {
      svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.id = 'wfSvg';
      svg.className = 'wf-svg';
      container.appendChild(svg);
    }
    svgEl = svg;
    _updateSvgSize();
    return svg;
  }

  function _updateSvgSize() {
    if (!svgEl || !svgEl.parentElement) return;
    const rect = svgEl.parentElement.getBoundingClientRect();
    const h = Math.max(rect.height || 400, 200);
    svgEl.setAttribute('height', h);
    svgEl.style.width = '100%';
    svgEl.style.height = '100%';
  }

  function buildDefs(svg) {
    svg.querySelectorAll('defs').forEach(d => d.remove());
    const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
    defs.innerHTML = `
      <marker id="arrowhead" markerWidth="12" markerHeight="8" refX="10" refY="4" orient="auto">
        <path d="M0,0 L12,4 L0,8 Z" fill="var(--accent)" />
      </marker>
      <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
        <feDropShadow dx="0" dy="2" stdDeviation="4" flood-opacity="0.12"/>
      </filter>
      <pattern id="grid" width="${GRID}" height="${GRID}" patternUnits="userSpaceOnUse">
        <path d="M ${GRID} 0 L 0 0 0 ${GRID}" fill="none" stroke="var(--border)" stroke-width="0.5" opacity="0.25"/>
      </pattern>`;
    svg.appendChild(defs);
  }

  function _svgCoord(e) {
    if (!svgEl) return { x: 0, y: 0 };
    const r = svgEl.getBoundingClientRect();
    const vb = svgEl.viewBox.baseVal;
    if (!vb.width) return { x: e.clientX - r.left, y: e.clientY - r.top };
    return {
      x: (e.clientX - r.left) * (vb.width  / r.width),
      y: (e.clientY - r.top)  * (vb.height / r.height),
    };
  }

  function _snapToPort(cx, cy) {
    for (const n of nodes) {
      const px = n._x + NODE_W;
      const py = n._y + NODE_H / 2;
      if (Math.hypot(cx - px, cy - py) < 32) return n.id;
    }
    return null;
  }

  // ── 事件监听 ──────────────────────────────────────────────────
  document.addEventListener('mousemove', e => {
    // 模板拖拽预览
    if (_templateDrag) {
      // 可以显示预览 ghost
      return;
    }
    // 节点拖拽
    if (_dragNode) {
      if (!svgEl) return;
      const c = _svgCoord(e);
      _dragNode._x = snap(Math.max(0, _ox0 + (c.x - _mx0)));
      _dragNode._y = snap(Math.max(0, _oy0 + (c.y - _my0)));
      _dragEl.setAttribute('transform', `translate(${_dragNode._x}, ${_dragNode._y})`);
      renderEdges();
      return;
    }
    // 连线预览
    if (_edgeDraw) {
      if (!_tempLine) {
        _tempLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        _tempLine.setAttribute('class', 'edge-temp');
        if (svgEl) svgEl.appendChild(_tempLine);
      }
      const c = _svgCoord(e);
      _tempLine.setAttribute('x1', _edgeDraw.startX);
      _tempLine.setAttribute('y1', _edgeDraw.startY);
      _tempLine.setAttribute('x2', c.x);
      _tempLine.setAttribute('y2', c.y);
    }
    // 端口悬停
    _updateHoveredPort(e);
  });

  document.addEventListener('mouseup', e => {
    // 结束模板拖拽
    if (_templateDrag) {
      if (svgEl) {
        const c = _svgCoord(e);
        const newNode = _createNode(_templateDrag.type, c.x - NODE_W/2, c.y - NODE_H/2);
        _templateDrag = null;
      } else {
        _templateDrag = null;
      }
      return;
    }
    // 结束节点拖拽
    if (_dragNode) {
      _dragRect?.classList.remove('dragging');
      _savePositions();
      _dragNode = null; _dragEl = null; _dragRect = null;
      return;
    }
    // 结束连线
    if (_edgeDraw) {
      if (_tempLine) { _tempLine.remove(); _tempLine = null; }
      if (!svgEl) { _edgeDraw = null; return; }
      const c = _svgCoord(e);
      const targetId = _snapToPort(c.x, c.y);
      if (targetId && targetId !== _edgeDraw.sourceId) {
        _addEdge(_edgeDraw.sourceId, targetId);
      }
      _edgeDraw = null;
      _isConnecting = false;
    }
    _hoveredPort = null;
  });

  // 滚轮缩放
  document.addEventListener('wheel', e => {
    if (!e.target.closest('.wf-svg') && !e.target.closest('.wf-designer')) return;
    e.preventDefault();
    const delta = e.deltaY > 0 ? 0.9 : 1.1;
    viewScale = Math.min(2, Math.max(0.3, viewScale * delta));
    if (svgEl) {
      svgEl.style.transform = `scale(${viewScale})`;
      svgEl.style.transformOrigin = 'top left';
    }
    renderEdges();
  }, { passive: false });

  // 键盘快捷键
  document.addEventListener('keydown', e => {
    if (!currentWorkflow) return;
    // 删除选中节点
    if ((e.key === 'Delete' || e.key === 'Backspace') && selectedNodeId) {
      if (document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {
        deleteNode(selectedNodeId);
      }
    }
    // Ctrl+A 全选
    if ((e.ctrlKey || e.metaKey) && e.key === 'a') {
      e.preventDefault();
      // 可以添加全选逻辑
    }
    // Ctrl+Z 撤销（预留）
    if ((e.ctrlKey || e.metaKey) && e.key === 'z' && !e.shiftKey) {
      e.preventDefault();
      // 预留撤销功能
    }
    // Ctrl+S 保存
    if ((e.ctrlKey || e.metaKey) && e.key === 's') {
      e.preventDefault();
      saveWorkflow();
    }
    // Ctrl+R 运行
    if ((e.ctrlKey || e.metaKey) && e.key === 'r') {
      e.preventDefault();
      run(currentWorkflow?.id);
    }
    // Escape 取消选择
    if (e.key === 'Escape') {
      selectedNodeId = null;
      nodeElements.forEach(g => g.querySelector('.node-rect')?.classList.remove('selected'));
      closeNodePanel();
    }
  });

  function _updateHoveredPort(e) {
    if (!svgEl || _dragNode || _edgeDraw) return;
    const c = _svgCoord(e);
    const newHover = _snapToPort(c.x, c.y);
    if (newHover !== _hoveredPort) {
      if (_hoveredPort) {
        const g = nodeElements.get(_hoveredPort);
        g?.querySelector('.node-port')?.classList.remove('hovered');
      }
      _hoveredPort = newHover;
      if (_hoveredPort) {
        const g = nodeElements.get(_hoveredPort);
        g?.querySelector('.node-port')?.classList.add('hovered');
      }
    }
  }

  // ── 创建节点 ──────────────────────────────────────────────────
  async function _createNode(type, x, y, name) {
    if (!currentWorkflow) return;
    const nodeId = genId('node');
    try {
      await _fetch(`/v1/workflows/${currentWorkflow.id}/nodes`, {
        method: 'POST',
        body: JSON.stringify({
          node_id: nodeId,
          type: type,
          name: name || nodeStyle(type).label,
          config: { timeout: 60 },
        }),
      });
      await _reloadWorkflow();
      // 设置位置
      const newNode = nodes.find(n => n.id === nodeId);
      if (newNode) {
        newNode._x = x;
        newNode._y = y;
        const g = nodeElements.get(nodeId);
        if (g) g.setAttribute('transform', `translate(${x}, ${y})`);
        renderEdges();
      }
      return nodeId;
    } catch (e) {
      App.toast?.('添加节点失败: ' + e.message, 'err');
      return null;
    }
  }

  // ── 渲染节点 ──────────────────────────────────────────────────
  function renderNodes(svg, wf) {
    const count = (wf.nodes || []).length;
    const cols = count > 4 ? 3 : count > 2 ? 2 : 1;
    const colW = NODE_W + PAD * 2;
    const rowH = NODE_H + PAD * 2;
    const h = Math.max(PAD * 2 + Math.ceil(count / cols) * rowH, 400);
    svg.setAttribute('height', h);
    svg.style.height = h + 'px';

    svg.querySelectorAll('.wf-node-group').forEach(el => el.remove());
    nodeElements.clear();

    (wf.nodes || []).forEach((node, idx) => {
      if (node._x == null) {
        node._x = PAD + (idx % cols) * colW;
        node._y = PAD + Math.floor(idx / cols) * rowH;
      }

      const style = nodeStyle(node.type);
      const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      g.setAttribute('transform', `translate(${node._x}, ${node._y})`);
      g.setAttribute('class', `wf-node-group node-type-${node.type || ''}`);
      g.dataset.nodeId = node.id;

      // 阴影
      const shadow = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      shadow.setAttribute('width', NODE_W);
      shadow.setAttribute('height', NODE_H);
      shadow.setAttribute('rx', '12');
      shadow.setAttribute('fill', 'var(--bg)');
      shadow.setAttribute('filter', 'url(#shadow)');

      // 主矩形
      const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      rect.setAttribute('width', NODE_W);
      rect.setAttribute('height', NODE_H);
      rect.setAttribute('rx', '12');
      rect.setAttribute('class', 'node-rect');
      rect.setAttribute('stroke', style.color);
      rect.setAttribute('fill', style.bg || '#fff');
      if (wf.entry_node === node.id) rect.classList.add('entry-node');
      if (selectedNodeId === node.id) rect.classList.add('selected');

      // 图标
      const icon = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      icon.setAttribute('x', 18);
      icon.setAttribute('y', 28);
      icon.setAttribute('class', 'node-icon');
      icon.setAttribute('font-size', '16');
      icon.textContent = style.icon;

      // 名称
      const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      text.setAttribute('x', 40);
      text.setAttribute('y', 26);
      text.setAttribute('class', 'node-text');
      text.textContent = node.name || style.label;

      // 类型标签
      const typeLabel = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      typeLabel.setAttribute('x', 40);
      typeLabel.setAttribute('y', 44);
      typeLabel.setAttribute('class', 'node-type-label');
      typeLabel.setAttribute('fill', style.color);
      typeLabel.setAttribute('font-size', '10');
      typeLabel.textContent = style.label;

      // 端口
      const port = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      port.setAttribute('cx', NODE_W);
      port.setAttribute('cy', NODE_H / 2);
      port.setAttribute('r', '8');
      port.setAttribute('class', 'node-port');
      port.setAttribute('fill', style.color);
      port.setAttribute('stroke', 'var(--bg)');
      port.setAttribute('stroke-width', '3');

      g.appendChild(shadow);
      g.appendChild(rect);
      g.appendChild(icon);
      g.appendChild(text);
      g.appendChild(typeLabel);
      g.appendChild(port);
      svg.appendChild(g);
      nodeElements.set(node.id, g);

      // 点击选中
      g.addEventListener('click', e => { e.stopPropagation(); selectNode(node.id); });
      // 双击编辑
      g.addEventListener('dblclick', e => { e.stopPropagation(); openNodePanel(node.id); });
      // 端口拖拽连线
      port.addEventListener('mousedown', e => {
        e.stopPropagation();
        e.preventDefault();
        _isConnecting = true;
        _edgeDraw = { sourceId: node.id, startX: node._x + NODE_W, startY: node._y + NODE_H / 2 };
      });
      // 节点拖拽
      rect.addEventListener('mousedown', e => {
        if (e.button !== 0) return;
        _dragNode = node; _dragEl = g; _dragRect = rect;
        const c = _svgCoord(e);
        _mx0 = c.x; _my0 = c.y;
        _ox0 = node._x; _oy0 = node._y;
        rect.classList.add('dragging');
        e.preventDefault();
        e.stopPropagation();
      });
    });
  }

  // ── 渲染边 ────────────────────────────────────────────────────
  function renderEdges() {
    if (!svgEl) return;
    svgEl.querySelectorAll('.edge-line').forEach(el => el.remove());
    const nodeMap = new Map();
    nodes.forEach(n => nodeMap.set(n.id, n));
    (edges || []).forEach(edge => {
      const src = nodeMap.get(edge.source);
      const tgt = nodeMap.get(edge.target);
      if (!src || !tgt) return;
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('class', 'edge-line');
      line.setAttribute('x1', src._x + NODE_W);
      line.setAttribute('y1', src._y + NODE_H / 2);
      line.setAttribute('x2', tgt._x);
      line.setAttribute('y2', tgt._y + NODE_H / 2);
      line.setAttribute('marker-end', 'url(#arrowhead)');
      line.dataset.edgeId = edge.id;
      line.addEventListener('dblclick', e => { e.stopPropagation(); _deleteEdge(edge.id); });
      svgEl.appendChild(line);
    });
  }

  // ── 边操作 ────────────────────────────────────────────────────
  async function _addEdge(sourceId, targetId) {
    if (!currentWorkflow) return;
    try {
      await _fetch(`/v1/workflows/${currentWorkflow.id}/edges`, {
        method: 'POST', body: JSON.stringify({ source: sourceId, target: targetId }),
      });
      await _reloadWorkflow();
    } catch (e) { App.toast?.('添加边失败: ' + e.message, 'err'); }
  }

  async function _deleteEdge(edgeId) {
    if (!currentWorkflow) return;
    if (!await App.confirm?.('删除此连接？')) return;
    try {
      await _fetch(`/v1/workflows/${currentWorkflow.id}/edges/${edgeId}`, { method: 'DELETE' });
      await _reloadWorkflow();
    } catch (e) { App.toast?.('删除边失败: ' + e.message, 'err'); }
  }

  function _savePositions() {
    if (!currentWorkflow) return;
    nodes.forEach(n => {
      const src = currentWorkflow.nodes.find(x => x.id === n.id);
      if (src) { src._x = n._x; src._y = n._y; }
    });
  }

  // ── 节点选择 ──────────────────────────────────────────────────
  function selectNode(nodeId) {
    selectedNodeId = nodeId;
    nodeElements.forEach((g, id) => {
      const rect = g.querySelector('.node-rect');
      if (rect) rect.classList.toggle('selected', id === nodeId);
    });
    if (nodeId) openNodePanel(nodeId);
  }

  document.addEventListener('click', e => {
    if (e.target.closest('.wf-node-group')) return;
    if (e.target.closest('.wf-node-panel')) return;
    selectedNodeId = null;
    nodeElements.forEach(g => g.querySelector('.node-rect')?.classList.remove('selected'));
    closeNodePanel();
  });

  // ── 节点配置面板 ──────────────────────────────────────────────
  function openNodePanel(nodeId) {
    const panel = $('wfNodePanel');
    if (!panel) return;
    panel.classList.add('open');
    const node = nodes.find(n => n.id === nodeId);
    if (!node) return;

    const typeOptions = NODE_TEMPLATES.map(t =>
      `<option value="${t.type}" ${node.type === t.type ? 'selected' : ''}>${t.icon} ${t.label}</option>`
    ).join('');

    let configHtml = '';
    if (node.type === 'tool') {
      configHtml = `
        <div class="np-field"><label>工具名称</label>
          <input type="text" id="np-tool-name" value="${node.tool_name || node.config?.tool_name || ''}" placeholder="如: search_web"></div>
        <div class="np-field"><label>参数 (JSON)</label>
          <textarea id="np-tool-params" rows="3" placeholder='{"keyword": "hello"}'>${JSON.stringify(node.config?.params || node.config?.arguments || {}, null, 2)}</textarea></div>
        <div class="np-field"><label>超时 (秒)</label>
          <input type="number" id="np-timeout" value="${node.timeout || 60}" min="1" max="300"></div>
        <div class="np-field"><label>错误策略</label>
          <select id="np-error-strategy">
            <option value="fail_fast" ${node.error_strategy === 'fail_fast' ? 'selected' : ''}>失败即停</option>
            <option value="continue" ${node.error_strategy === 'continue' ? 'selected' : ''}>跳过继续</option>
            <option value="retry" ${node.error_strategy === 'retry' ? 'selected' : ''}>重试</option>
            <option value="fallback" ${node.error_strategy === 'fallback' ? 'selected' : ''}>回退节点</option>
          </select></div>
        <div class="np-field"><label>重试次数</label>
          <input type="number" id="np-retry-count" value="${node.retry_count || 0}" min="0" max="5"></div>
        <div class="np-field"><label>回退节点 ID (可选)</label>
          <input type="text" id="np-fallback-node" value="${node.fallback_node_id || ''}" placeholder="如: node_fallback"></div>`;
    } else if (node.type === 'condition') {
      configHtml = `
        <div class="np-field"><label>条件表达式</label>
          <input type="text" id="np-condition" value="${node.condition_expr || ''}" placeholder="如: result > 10"></div>
        <div class="np-field"><label>True 分支节点 (逗号分隔)</label>
          <input type="text" id="np-branch-true" value="${(node.branches?.true || []).join(', ')}" placeholder="node_a, node_b"></div>
        <div class="np-field"><label>False 分支节点 (逗号分隔)</label>
          <input type="text" id="np-branch-false" value="${(node.branches?.false || []).join(', ')}" placeholder="node_c"></div>
        <div class="np-hint">示例: result > 10 && status == "ok"</div>`;
    } else if (node.type === 'agent') {
      const inputVal = (node.agent_config || node.config || {}).input || '';
      configHtml = `
        <div class="np-field"><label>提示词 (支持 {{变量名}})</label>
          <textarea id="np-agent-input" rows="4" placeholder="输入 Agent 任务描述">${inputVal}</textarea></div>
        <div class="np-field"><label>超时 (秒)</label>
          <input type="number" id="np-timeout" value="${node.timeout || 60}" min="1" max="300"></div>
        <div class="np-field"><label>错误策略</label>
          <select id="np-agent-error-strategy">
            <option value="fail_fast" ${node.error_strategy === 'fail_fast' ? 'selected' : ''}>失败即停</option>
            <option value="continue" ${node.error_strategy === 'continue' ? 'selected' : ''}>跳过继续</option>
            <option value="retry" ${node.error_strategy === 'retry' ? 'selected' : ''}>重试</option>
          </select></div>
        <div class="np-field"><label>重试次数</label>
          <input type="number" id="np-agent-retry-count" value="${node.retry_count || 0}" min="0" max="5"></div>
        <div class="np-hint">可用变量: {{result}}, {{input}}, {{node_id}} 等</div>`;
    } else if (node.type === 'http') {
      const hc = node.http_config || {};
      configHtml = `
        <div class="np-field"><label>URL</label>
          <input type="text" id="np-http-url" value="${hc.url || ''}" placeholder="https://api.example.com/data"></div>
        <div class="np-field"><label>Method</label>
          <select id="np-http-method">${['GET','POST','PUT','DELETE'].map(m => `<option ${hc.method===m?'selected':''}>${m}</option>`).join('')}</select></div>
        <div class="np-field"><label>Body (JSON)</label>
          <textarea id="np-http-body" rows="2" placeholder='{"key":"value"}'>${JSON.stringify(hc.body || {}, null, 2)}</textarea></div>`;
    } else if (node.type === 'script') {
      configHtml = `
        <div class="np-field"><label>脚本代码 (Python)</label>
          <textarea id="np-script-code" rows="5" placeholder="# 可访问 run.variables\ncount = len(items)">${node.script_code || ''}</textarea></div>`;
    } else if (node.type === 'loop') {
      const lc = node.loop_config || {};
      configHtml = `
        <div class="np-field"><label>可迭代对象 (JSON 数组)</label>
          <textarea id="np-loop-iterable" rows="2" placeholder='["a","b","c"]'>${JSON.stringify(lc.iterable || [], null, 2)}</textarea></div>
        <div class="np-field"><label>最大迭代次数</label>
          <input type="number" id="np-loop-max" value="${lc.max || 10}" min="1" max="100"></div>
        <div class="np-field"><label>循环模式</label>
          <select id="np-loop-mode">
            <option value="sequential" ${node.loop_mode === 'sequential' ? 'selected' : ''}>顺序执行</option>
            <option value="parallel" ${node.loop_mode === 'parallel' ? 'selected' : ''}>并行执行</option>
          </select></div>
        <div class="np-field"><label>子图节点 IDs (逗号分隔，可选)</label>
          <input type="text" id="np-subgraph-ids" value="${(node.subgraph_ids || []).join(', ')}" placeholder="node_a, node_b"></div>
        <div class="np-hint">子图：循环内对每个元素执行的节点 ID 列表</div>`;
    } else if (node.type === 'sleep') {
      configHtml = `<div class="np-field"><label>等待秒数</label>
        <input type="number" id="np-sleep-secs" value="${node.config?.seconds || 1}" min="0.1" max="3600" step="0.1"></div>`;
    }

    panel.innerHTML = `
      <div class="np-header">
        <span>${nodeStyle(node.type).icon} ${node.name || '节点'}</span>
        <button class="np-close" onclick="App.Workflow.closeNodePanel()">×</button>
      </div>
      <div class="np-body">
        <div class="np-field"><label>节点 ID</label>
          <input type="text" id="np-id" value="${node.id}" disabled class="np-disabled"></div>
        <div class="np-field"><label>名称</label>
          <input type="text" id="np-name" value="${node.name || ''}"></div>
        <div class="np-field"><label>类型</label>
          <select id="np-type">${typeOptions}</select></div>
        ${configHtml}
        <div class="np-actions">
          <button class="np-btn np-btn-save" onclick="App.Workflow.saveNodeConfig('${node.id}')">保存</button>
          <button class="np-btn np-btn-delete" onclick="App.Workflow.deleteNode('${node.id}')">删除</button>
        </div>
      </div>`;
  }

  function closeNodePanel() {
    const panel = $('wfNodePanel');
    if (panel) { panel.classList.remove('open'); setTimeout(() => { panel.innerHTML = ''; }, 200); }
  }

  async function saveNodeConfig(nodeId) {
    if (!currentWorkflow) return;
    const name = $('np-name')?.value?.trim() || nodeId;
    const type = $('np-type')?.value || 'agent';
    const timeout = parseInt($('np-timeout')?.value) || 60;
    const updates = { name, type };
    const config = {};

    if (type === 'tool') {
      config.tool_name = $('np-tool-name')?.value?.trim() || '';
      try { config.params = JSON.parse($('np-tool-params')?.value || '{}'); } catch(_) {}
      config.timeout = timeout;
      // 错误恢复配置
      config.error_strategy = $('np-error-strategy')?.value || 'fail_fast';
      config.retry_count = parseInt($('np-retry-count')?.value) || 0;
      config.fallback_node_id = $('np-fallback-node')?.value?.trim() || null;
    } else if (type === 'condition') {
      config.condition_expr = $('np-condition')?.value?.trim() || '';
      const t = ($('np-branch-true')?.value || '').split(',').map(s=>s.trim()).filter(Boolean);
      const f = ($('np-branch-false')?.value || '').split(',').map(s=>s.trim()).filter(Boolean);
      config.branches = { true: t, false: f };
    } else if (type === 'agent') {
      config.input = $('np-agent-input')?.value?.trim() || '';
      config.error_strategy = $('np-agent-error-strategy')?.value || 'fail_fast';
      config.retry_count = parseInt($('np-agent-retry-count')?.value) || 0;
    } else if (type === 'http') {
      config.url = $('np-http-url')?.value?.trim() || '';
      config.method = $('np-http-method')?.value || 'GET';
      try { config.body = JSON.parse($('np-http-body')?.value || '{}'); } catch(_) {}
    } else if (type === 'script') {
      config.script_code = $('np-script-code')?.value || '';
    } else if (type === 'loop') {
      try { config.iterable = JSON.parse($('np-loop-iterable')?.value || '[]'); } catch(_) { config.iterable = []; }
      config.max = parseInt($('np-loop-max')?.value) || 10;
      config.loop_mode = $('np-loop-mode')?.value || 'sequential';
      // 子图配置
      const subgraphStr = $('np-subgraph-ids')?.value || '';
      config.subgraph_ids = subgraphStr.split(',').map(s => s.trim()).filter(Boolean);
    } else if (type === 'sleep') {
      config.seconds = parseFloat($('np-sleep-secs')?.value) || 1;
    }

    updates.config = config;
    if (type === 'tool') {
      updates.tool_name = config.tool_name;
      updates.error_strategy = config.error_strategy;
      updates.retry_count = config.retry_count;
      updates.retry_delay = config.retry_delay;
      updates.fallback_node_id = config.fallback_node_id;
    }
    if (type === 'agent') {
      updates.error_strategy = config.error_strategy;
      updates.retry_count = config.retry_count;
    }
    if (type === 'condition') { updates.condition_expr = config.condition_expr; updates.branches = config.branches; }
    if (type === 'http') updates.http_config = config;
    if (type === 'script') updates.script_code = config.script_code;
    if (type === 'loop') updates.loop_config = config;

    try {
      await _fetch(`/v1/workflows/${currentWorkflow.id}/nodes/${nodeId}`, {
        method: 'PATCH', body: JSON.stringify(updates),
      });
      App.toast?.('节点已更新', 'ok');
      closeNodePanel();
      await _reloadWorkflow();
    } catch (e) { App.toast?.('保存失败: ' + e.message, 'err'); }
  }

  async function deleteNode(nodeId) {
    if (!currentWorkflow) return;
    if (currentWorkflow.entry_node === nodeId) { App.toast?.('不能删除入口节点', 'err'); return; }
    if (!await App.confirm?.('确定删除节点 "' + (nodes.find(n => n.id === nodeId)?.name || nodeId) + '"？关联的边也会删除。')) return;
    try {
      await _fetch(`/v1/workflows/${currentWorkflow.id}/nodes/${nodeId}`, { method: 'DELETE' });
      App.toast?.('节点已删除', 'ok');
      closeNodePanel();
      await _reloadWorkflow();
    } catch (e) { App.toast?.('删除失败: ' + e.message, 'err'); }
  }

  // ── 重新加载 ──────────────────────────────────────────────────
  async function _reloadWorkflow() {
    if (!currentWorkflow) return;
    try {
      const data = await _fetch(`/v1/workflows/${currentWorkflow.id}`);
      renderDesigner(data);
    } catch (e) { App.toast?.('刷新失败: ' + e.message, 'err'); }
  }

  // ── 渲染设计器 ────────────────────────────────────────────────
  function renderDesigner(wf) {
    currentWorkflow = wf;
    nodes = wf.nodes ? wf.nodes.map(n => ({ ...n })) : [];
    edges = wf.edges ? [...wf.edges] : [];
    selectedNodeId = null;
    viewScale = 1;
    const svg = ensureSvg();
    if (!svg) return;
    buildDefs(svg);
    renderNodes(svg, wf);
    nodes.forEach(n => {
      const srcNode = wf.nodes.find(x => x.id === n.id);
      if (srcNode && srcNode._x != null) { n._x = srcNode._x; n._y = srcNode._y; }
    });
    renderEdges();
    setTimeout(_updateSvgSize, 50);
    if (svgEl && typeof ResizeObserver !== 'undefined') {
      if (svgEl._resizeObserver) svgEl._resizeObserver.disconnect();
      svgEl._resizeObserver = new ResizeObserver(() => { _updateSvgSize(); renderEdges(); });
      svgEl._resizeObserver.observe($('wfDesigner'));
    }
  }

  // ── 列表 ──────────────────────────────────────────────────────
  async function loadList() {
    try {
      const data = await _fetch('/v1/workflows');
      renderList(data.workflows || []);
    } catch (e) { console.error('加载工作流列表失败:', e); }
  }

  function renderList(workflows) {
    const listEl = $('wfList');
    if (!listEl) return;
    if (!workflows || workflows.length === 0) {
      listEl.innerHTML = `
        <div class="wf-empty">
          <div class="wf-empty-icon">⚙️</div>
          <div>暂无工作流</div>
          <div class="wf-empty-hint">点击"+ 新建"创建第一个工作流</div>
        </div>
        <div class="wf-empty-tip">
          <div class="wf-tip-title">快速开始</div>
          <div class="wf-tip-item">1️⃣ 点击"+ 新建"或"✨ AI 生成"创建工作流</div>
          <div class="wf-tip-item">2️⃣ 双击画布添加节点</div>
          <div class="wf-tip-item">3️⃣ 从端口拖拽创建连线</div>
          <div class="wf-tip-item">4️⃣ 点击"▶ 运行"执行</div>
        </div>
      `;
      return;
    }
    listEl.innerHTML = workflows.map(wf => `
      <div class="wf-item ${currentWorkflow?.id === wf.id ? 'active' : ''}" data-id="${wf.id}">
        <div class="wf-item-header">
          <span class="wf-item-name">${wf.name || '未命名'}</span>
          <span class="wf-item-meta">${wf.nodes_count || 0} 节点</span>
        </div>
        <div class="wf-item-actions">
          <button class="mini-btn" onclick="event.stopPropagation(); App.Workflow.load('${wf.id}')">编辑</button>
          <button class="mini-btn" onclick="event.stopPropagation(); App.Workflow.run('${wf.id}')">▶ 运行</button>
          <button class="mini-btn danger" onclick="event.stopPropagation(); App.Workflow.deleteWorkflow('${wf.id}')">删除</button>
        </div>
      </div>`).join('');
  }

  async function load(id) {
    try {
      const data = await _fetch(`/v1/workflows/${id}`);
      renderDesigner(data);
      $('wfToolbar')?.style.setProperty('display', 'flex');
      $('wfList')?.querySelectorAll('.wf-item').forEach(el => {
        el.classList.toggle('active', el.dataset.id === id);
      });
      loadRunHistory();
    } catch (e) { App.toast?.('加载失败: ' + e.message, 'err'); }
  }

  async function create() {
    const name = await App.prompt?.('工作流名称:', '新工作流') || '新工作流';
    if (!name?.trim()) return;
    try {
      const data = await _fetch('/v1/workflows', {
        method: 'POST',
        body: JSON.stringify({
          name: name.trim(), description: '',
          nodes: [
            { id: 'start_1', type: 'start', name: '开始' },
            { id: 'agent_1', type: 'agent', name: 'Agent 处理' },
            { id: 'end_1',   type: 'end',   name: '结束' },
          ],
          edges: [
            { source: 'start_1', target: 'agent_1' },
            { source: 'agent_1', target: 'end_1' },
          ],
          entry_node: 'start_1'
        })
      });
      App.toast?.('工作流创建成功', 'ok');
      await loadList();
      if (data.id) setTimeout(() => load(data.id), 100);
    } catch (e) { App.toast?.('创建失败: ' + e.message, 'err'); }
  }

  // ── AI 描述性工作流生成 ─────────────────────────────────────────
  async function generateByDescription() {
    const name = await App.prompt?.('工作流名称:', 'AI 生成工作流');
    if (!name?.trim()) return;
    const description = await App.prompt?.('描述工作流(自然语言):',
      '例如:先用搜索查询天气,再根据天气推荐穿衣建议');
    if (!description?.trim()) return;
    try {
      const data = await _fetch('/v1/workflows/generate', {
        method: 'POST',
        body: JSON.stringify({
          name: name.trim(),
          description: description.trim(),
          input_variables: {},
        }),
      });
      App.toast?.(`✨ 工作流 "${data.name}" 生成成功`, 'ok');
      await loadList();
      if (data.id) setTimeout(() => load(data.id), 100);
    } catch (e) { App.toast?.('AI 生成失败: ' + e.message, 'err'); }
  }

  // ── 运行 ──────────────────────────────────────────────────────
  async function run(id) {
    if (!id) id = currentWorkflow?.id;
    if (!id) { App.toast?.('请先选择工作流', 'err'); return; }
    activeRunId = null; runHistory = [];
    clearNodeStates();
    showRunStatus('▶ 运行中...');

    try {
      const resp = await fetch(_apiBase() + `/v1/workflows/${id}/run/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(_apiKey() ? { 'Authorization': 'Bearer ' + _apiKey() } : {}) },
        body: JSON.stringify({ inputs: {}, stream_detail: true }),
      });
      if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || '运行失败');

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let lineEnd;
        while ((lineEnd = buffer.indexOf('\n')) !== -1) {
          const line = buffer.substring(0, lineEnd).trim();
          buffer = buffer.substring(lineEnd + 1);
          if (line.startsWith('data: ')) {
            try { handleStreamEvent(JSON.parse(line.substring(6))); } catch (_) {}
          }
        }
      }
    } catch (e) {
      showRunStatus('❌ 运行失败', 'error');
      App.toast?.('运行失败: ' + e.message, 'err');
    }
  }

  function handleStreamEvent(data) {
    if (data.run_id) activeRunId = data.run_id;
    if (data.node_id && data.status) {
      applyNodeStates({ [data.node_id]: { status: data.status } });
      showRunStatus(`▶ ${data.node_id} 执行中...`);
    }
    if (data.status === 'completed') {
      showRunStatus('✅ 运行完成', 'success');
      if (data.node_states) applyNodeStates(data.node_states);
      runHistory.push({ run_id: data.run_id, status: 'completed', duration: data.duration,
        output: data.output, node_states: data.node_states, history: data.history, timestamp: Date.now() });
      renderRunHistory();
    } else if (data.status === 'failed') {
      showRunStatus('❌ 运行失败', 'error');
      runHistory.push({ run_id: data.run_id, status: 'failed', duration: data.duration,
        error: data.error, timestamp: Date.now() });
      renderRunHistory();
    }
  }

  function clearNodeStates() {
    nodeElements.forEach(g => {
      g.classList.remove('running', 'success', 'failed', 'skipped');
      const node = nodes.find(n => n.id === g.dataset.nodeId);
      const icon = g.querySelector('.node-icon');
      if (icon && node) icon.textContent = nodeStyle(node.type).icon;
    });
  }

  function applyNodeStates(states) {
    Object.entries(states).forEach(([nid, state]) => {
      const g = nodeElements.get(nid);
      if (!g) return;
      const status = state?.status;
      const icon = g.querySelector('.node-icon');
      g.classList.remove('running', 'success', 'failed', 'skipped');
      if (status === 'running') { g.classList.add('running'); if (icon) icon.textContent = '⟳'; }
      else if (status === 'success') { g.classList.add('success'); if (icon) icon.textContent = '✓'; }
      else if (status === 'failed') { g.classList.add('failed'); if (icon) icon.textContent = '✗'; }
      else if (status === 'skipped') { g.classList.add('skipped'); if (icon) icon.textContent = '—'; }
    });
  }

  function showRunStatus(msg, cls) {
    const bar = $('wfRunStatus');
    if (!bar) return;
    bar.textContent = msg;
    bar.className = 'wf-run-status' + (cls ? ' ' + cls : '');
  }

  // ── 运行历史 ──────────────────────────────────────────────────
  async function loadRunHistory() {
    if (!currentWorkflow) return;
    try {
      const data = await _fetch(`/v1/workflows/${currentWorkflow.id}/runs`);
      runHistory = (data.runs || []).map(r => ({
        run_id: r.run_id, status: r.status, duration: r.duration,
        output: r.output, node_states: r.node_states, history: r.history,
        error: r.error, timestamp: r.finished_at ? r.finished_at * 1000 : Date.now(),
      }));
      renderRunHistory();
    } catch (e) { console.error('加载历史失败:', e); }
  }

  function renderRunHistory() {
    const el = $('wfRunHistory');
    if (!el) return;
    if (!runHistory.length) {
      el.innerHTML = `<div class="wf-empty">
        <div class="wf-empty-icon">📊</div>
        <div>暂无运行记录</div>
        <div class="wf-empty-hint">点击"▶ 运行"执行工作流</div>
      </div>`;
      return;
    }
    el.innerHTML = runHistory.slice(-8).reverse().map(r => `
      <div class="wf-run-item ${r.status}" onclick="App.Workflow.showRunDetail('${r.run_id}')">
        <span class="wf-run-icon">${r.status === 'completed' ? '✅' : '❌'}</span>
        <span class="wf-run-time">${formatTime(r.timestamp)}</span>
        <span class="wf-run-id">${r.run_id.slice(-8)}</span>
        <span class="wf-run-dur">${r.duration ? (r.duration * 1000).toFixed(0) + 'ms' : ''}</span>
        <span class="wf-run-status-badge">${r.status === 'completed' ? '成功' : '失败'}</span>
      </div>`).join('');
  }

  function formatTime(ts) {
    if (!ts) return '';
    return new Date(ts).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  }

  async function showRunDetail(runId) {
    const run = runHistory.find(r => r.run_id === runId);
    if (!run) return;
    try {
      const data = await _fetch(`/v1/workflows/${currentWorkflow.id}/runs/${runId}`);
      renderRunDetail(data);
    } catch (e) { console.error('加载详情失败:', e); }
  }

  function renderRunDetail(run) {
    const panel = $('wfRunPanel');
    if (!panel) return;
    panel.style.display = 'flex';
    const history = run.history || [];
    const stateRows = Object.entries(run.node_states || {}).map(([nid, state]) => {
      const node = nodes.find(n => n.id === nid);
      const color = state.status === 'success' ? 'var(--ok)' : state.status === 'failed' ? 'var(--danger)' : 'var(--muted)';
      const icon = state.status === 'success' ? '✓' : state.status === 'failed' ? '✗' : state.status === 'skipped' ? '—' : '?';
      return `<tr><td>${node?.name || nid}</td><td style="color:${color}">${icon} ${state.status}</td><td>${state.duration ? (state.duration*1000).toFixed(0)+'ms' : '-'}</td><td>${state.error || '-'}</td></tr>`;
    }).join('');
    const outputJson = run.output ? escapeHtml(JSON.stringify(run.output, null, 2)) : '-';
    panel.innerHTML = `
      <div class="wf-run-panel-header"><span>📋 运行详情 · ${run.run_id.slice(-8)}</span><button class="np-close" onclick="App.Workflow.closeRunPanel()">×</button></div>
      <div class="wf-run-detail-tabs">
        <button class="wf-tab active" onclick="App.Workflow.switchRunTab('nodes')">节点状态</button>
        <button class="wf-tab" onclick="App.Workflow.switchRunTab('output')">输出结果</button>
      </div>
      <div id="wfRunNodes" class="wf-run-tab-content active">
        <table class="wf-state-table"><thead><tr><th>节点</th><th>状态</th><th>耗时</th><th>错误</th></tr></thead><tbody>${stateRows || '<tr><td colspan="4">无数据</td></tr>'}</tbody></table>
      </div>
      <div id="wfRunOutput" class="wf-run-tab-content"><pre class="wf-output-pre">${outputJson}</pre></div>`;
  }

  function switchRunTab(tab) {
    document.querySelectorAll('.wf-run-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.wf-run-tab-content').forEach(t => t.classList.remove('active'));
    event.target.classList.add('active');
    document.getElementById('wfRun' + tab.charAt(0).toUpperCase() + tab.slice(1))?.classList.add('active');
  }

  function closeRunPanel() { $('wfRunPanel')?.style.setProperty('display', 'none'); }
  function escapeHtml(t) { return t?.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') || ''; }

  // ── 自动布局 ──────────────────────────────────────────────────
  function autoLayout() {
    if (!currentWorkflow || nodes.length === 0) return;
    const count = nodes.length;
    const cols = count > 4 ? 3 : count > 2 ? 2 : 1;
    const colW = NODE_W + PAD * 2;
    const rowH = NODE_H + PAD * 2;
    nodes.forEach((node, idx) => {
      node._x = PAD + (idx % cols) * colW;
      node._y = PAD + Math.floor(idx / cols) * rowH;
    });
    _savePositions();
    renderNodes(svgEl, currentWorkflow);
    renderEdges();
    App.toast?.('已自动布局', 'ok');
  }

  // ── 添加节点 ──────────────────────────────────────────────────
  async function addNode() {
    if (!currentWorkflow) return;
    // 可视化节点选择器
    const types = [
      { type: 'start', icon: '▶', label: '开始' },
      { type: 'end', icon: '⏹', label: '结束' },
      { type: 'agent', icon: '🤖', label: 'Agent' },
      { type: 'tool', icon: '🔧', label: '工具' },
      { type: 'condition', icon: '⚡', label: '条件' },
      { type: 'loop', icon: '🔁', label: '循环' },
      { type: 'http', icon: '🌐', label: 'HTTP' },
      { type: 'script', icon: '📜', label: '脚本' },
      { type: 'sleep', icon: '⏸', label: '等待' },
    ];
    const choice = await App.prompt?.(
      '选择节点类型:\n\n' +
      types.map((t, i) => `${i + 1}. ${t.icon} ${t.label}`).join('\n') +
      '\n\n输入编号 (1-' + types.length + '):',
      '2'
    );
    if (!choice) return;
    const idx = parseInt(choice) - 1;
    if (isNaN(idx) || idx < 0 || idx >= types.length) {
      App.toast?.('无效选择', 'err');
      return;
    }
    const selected = types[idx];
    const name = await App.prompt?.('节点名称:', selected.label + ' ' + (nodes.length + 1));
    if (!name) return;
    const nodeId = genId('node');
    try {
      await _fetch(`/v1/workflows/${currentWorkflow.id}/nodes`, {
        method: 'POST',
        body: JSON.stringify({
          node_id: nodeId,
          type: selected.type,
          name: name.trim(),
          config: { timeout: 60 }
        }),
      });
      await _reloadWorkflow();
    } catch (e) { App.toast?.('添加失败: ' + e.message, 'err'); }
  }

  // ── 保存/删除 ─────────────────────────────────────────────────
  async function saveWorkflow() {
    if (!currentWorkflow) return;
    _savePositions();
    try {
      await _fetch(`/v1/workflows/${currentWorkflow.id}`, {
        method: 'PUT',
        body: JSON.stringify({
          name: currentWorkflow.name,
          nodes: nodes.map(n => ({
            id: n.id, type: n.type, name: n.name,
            config: n.config || {},
            tool_name: n.tool_name,
            agent_config: n.agent_config,
            condition_expr: n.condition_expr,
            branches: n.branches,
            timeout: n.timeout,
            error_strategy: n.error_strategy,
            retry_count: n.retry_count,
            retry_delay: n.retry_delay,
            fallback_node_id: n.fallback_node_id,
            loop_mode: n.loop_mode,
            subgraph_ids: n.subgraph_ids,
          })),
          edges: edges.map(e => ({ source: e.source, target: e.target, condition: e.condition, condition_expr: e.condition_expr })),
          entry_node: currentWorkflow.entry_node,
          error_mode: currentWorkflow.error_mode,
        }),
      });
      App.toast?.('已保存', 'ok');
    } catch (e) { App.toast?.('保存失败: ' + e.message, 'err'); }
  }

  async function deleteWorkflow(id) {
    if (!id) id = currentWorkflow?.id;
    if (!id) { App.toast?.('请先选择工作流', 'err'); return; }
    if (!await App.confirm?.('确定删除此工作流？所有运行记录也将删除。')) return;
    try {
      await _fetch(`/v1/workflows/${id}`, { method: 'DELETE' });
      App.toast?.('已删除', 'ok');
      currentWorkflow = null; nodes = []; edges = []; nodeElements.clear();
      selectedNodeId = null; activeRunId = null; runHistory = [];
      $('wfToolbar')?.style.setProperty('display', 'none');
      $('wfSvg')?.remove(); svgEl = null;
      $('wfRunStatus').textContent = '';
      $('wfRunHistory').innerHTML = '';
      loadList();
    } catch (e) { App.toast?.('删除失败: ' + e.message, 'err'); }
  }

  // ── 初始化 ────────────────────────────────────────────────────
  function init() {
    const btns = {
      new: ['wfNewBtn', create],
      refresh: ['wfRefreshBtn', loadList],
      run: ['wfRunBtn', () => run(currentWorkflow?.id)],
      delete: ['wfDeleteBtn', () => deleteWorkflow(currentWorkflow?.id)],
      save: ['wfSaveBtn', saveWorkflow],
      addNode: ['wfAddNodeBtn', addNode],
      layout: ['wfLayoutBtn', autoLayout],
      zoomIn: ['wfZoomInBtn', () => { viewScale = Math.min(2, viewScale * 1.2); applyZoom(); }],
      zoomOut: ['wfZoomOutBtn', () => { viewScale = Math.max(0.3, viewScale * 0.8); applyZoom(); }],
      zoomReset: ['wfZoomResetBtn', () => { viewScale = 1; applyZoom(); }],
      aiGen: ['wfAiGenBtn', generateByDescription],
    };
    Object.entries(btns).forEach(([id, [elId, fn]]) => {
      const el = $(elId);
      if (el) el.addEventListener('click', fn);
    });

    loadList();
    showWorkflowHint();
  }

  function applyZoom() {
    if (svgEl) {
      svgEl.style.transform = `scale(${viewScale})`;
      svgEl.style.transformOrigin = 'top left';
      renderEdges();
    }
    showZoomStatus();
  }

  function showZoomStatus() {
    const status = $('wfRunStatus');
    if (status && !status.textContent.includes('▶') && !status.textContent.includes('❌') && !status.textContent.includes('✅')) {
      // 不覆盖运行状态
    }
  }

  // ── 操作提示 ──────────────────────────────────────────────────
  function showWorkflowHint() {
    if (document.querySelector('.wf-hint')) return;
    const hint = document.createElement('div');
    hint.className = 'wf-hint';
    hint.innerHTML = `
      <div class="wf-hint-title">⚙️ 工作流操作指南</div>
      <div class="wf-hint-body">
        <p>🖱️ <strong>拖拽节点</strong> — 按住节点拖动调整位置</p>
        <p>🔗 <strong>添加连线</strong> — 从右侧端口拖到目标节点</p>
        <p>✏️ <strong>编辑节点</strong> — 双击节点打开配置</p>
        <p>🗑️ <strong>删除节点</strong> — 选中后按 Delete 键</p>
        <p>🔍 <strong>缩放画布</strong> — 滚轮缩放或使用工具栏按钮</p>
        <p>📐 <strong>自动布局</strong> — 点击"布局"自动排列节点</p>
        <p>💾 <strong>保存</strong> — Ctrl + S</p>
        <p>▶ <strong>运行</strong> — Ctrl + R</p>
        <p>⎋ <strong>取消选择</strong> — Esc 键</p>
      </div>
      <button class="wf-hint-close" onclick="this.parentElement.remove()">我知道了</button>
    `;
    const container = $('wfDesigner');
    if (container) container.appendChild(hint);
  }

  return {
    loadList, load, create, run, deleteWorkflow, renderDesigner, init,
    openNodePanel, closeNodePanel, saveNodeConfig, deleteNode, addNode, saveWorkflow,
    loadRunHistory, showRunDetail, closeRunPanel, switchRunTab,
    autoLayout, generateByDescription,
    get current() { return currentWorkflow; }
  };
})();
