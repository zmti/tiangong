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
  let _posCache = {};
  let _versionHistory = [];
  let _scheduleList = [];
  let _statsData = null;

  const NODE_W = 140;
  const NODE_H = 64;
  const PAD = 60;
  const GRID = 20;

  function $(id) { return document.getElementById(id); }

  function _capturePositions() {
    nodes.forEach(n => {
      if (n.id != null && n._x != null && n._y != null) {
        _posCache[n.id] = { x: n._x, y: n._y };
      }
    });
  }

  function _layoutPosition(node, idx, total) {
    if (node._x != null) return;
    const cached = _posCache[node.id];
    if (cached) { node._x = cached.x; node._y = cached.y; return; }
    if (node.position && typeof node.position.x === 'number') {
      node._x = node.position.x; node._y = node.position.y; return;
    }
    const cols = total > 4 ? 3 : total > 2 ? 2 : 1;
    const colW = NODE_W + PAD * 2;
    const rowH = NODE_H + PAD * 2;
    node._x = PAD + (idx % cols) * colW;
    node._y = PAD + Math.floor(idx / cols) * rowH;
  }

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
  let _templateDrag = null;
  let _snapLines = { h: [], v: [] };
  const SNAP_THRESHOLD = 10;

  // ── SVG 工具 ──────────────────────────────────────────────────
  function ensureSvg() {
    const container = document.getElementById('workflowDesigner') || $('wfDesigner');
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
    if (!svgEl) return;
    svgEl.style.width = '100%';
    svgEl.style.height = '100%';
  }

  function _designerEl() {
    return document.getElementById('workflowDesigner') || document.getElementById('wfDesigner');
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
    // SVG 用 CSS transform: scale() 做缩放(见 applyZoom), getBoundingClientRect 返回
    // 的是缩放后的尺寸, 而节点 _x/_y 是缩放前(用户坐标系)的值。要把鼠标屏坐标换算回
    // 用户坐标系, 必须除以 viewScale; 否则一缩放, 端口拖线/落点/双击加节点全对不齐
    // (表现为"画线画不出来/画到错位"。旧版误以为要按 viewBox 算比例, 但本 SVG 从不
    // 设 viewBox, 实际缩放系数一直是 1, 缩放后坐标就漂了)。
    const scale = viewScale || 1;
    return {
      x: (e.clientX - r.left) / scale,
      y: (e.clientY - r.top)  / scale,
    };
  }

  const PORT_SNAP = 32;      // 端口吸附半径
  const NODE_DROP_PAD = 12;  // 连线落点放宽到"节点整体"时的额外容差

  /**
   * 找出落点命中的节点 ID。
   *
   * 修复前只匹配"输出端口"(右缘中点) 32px 以内 —— 但连线时用户是往目标节点身上、
   * 或它的左缘(输入口)拖放。NODE_W=140 时,节点中心距右缘有 70px,远超 32px;
   * 落在左缘更是相距 140px。结果是:除了把光标精确丢在目标节点最右侧那条边上,
   * 其它任何落点都连不上 —— 表现为"节点之间不能连接"。
   *
   * 现在:①优先命中输入/输出端口;②连线过程中(forConnect)放宽到整个节点范围。
   */
  function _snapToPort(cx, cy, forConnect = false) {
    let best = null;
    let bestDist = Infinity;
    for (const n of nodes) {
      const py = n._y + NODE_H / 2;
      // 输入端口(左缘)优先 —— 连线最常见的落点
      const dIn = Math.hypot(cx - n._x, cy - py);
      const dOut = Math.hypot(cx - (n._x + NODE_W), cy - py);
      const d = Math.min(dIn, dOut);
      if (d < PORT_SNAP && d < bestDist) {
        best = n.id;
        bestDist = d;
      }
    }
    if (best) return best;

    if (forConnect) {
      // 拖到对方节点身上即视为连到该节点
      for (const n of nodes) {
        if (cx >= n._x - NODE_DROP_PAD && cx <= n._x + NODE_W + NODE_DROP_PAD &&
            cy >= n._y - NODE_DROP_PAD && cy <= n._y + NODE_H + NODE_DROP_PAD) {
          return n.id;
        }
      }
    }
    return null;
  }

  function _getSnapOffset(direction, currentVal, otherVal) {
    let offset = 0;
    nodes.forEach(n => {
      if (n.id === _dragNode?.id) return;
      let alignVal;
      if (direction === 'v') {
        const left = n._x;
        const center = n._x + NODE_W / 2;
        const right = n._x + NODE_W;
        [left, center, right].forEach(v => {
          if (Math.abs(currentVal - v) < SNAP_THRESHOLD) {
            offset = v - currentVal;
          }
        });
      } else {
        const top = n._y;
        const middle = n._y + NODE_H / 2;
        const bottom = n._y + NODE_H;
        [top, middle, bottom].forEach(v => {
          if (Math.abs(currentVal - v) < SNAP_THRESHOLD) {
            offset = v - currentVal;
          }
        });
      }
    });
    return offset;
  }

  function _renderSnapLines(x, y) {
    _clearSnapLines();
    if (!svgEl) return;
    const lines = [];
    nodes.forEach(n => {
      if (n.id === _dragNode?.id) return;
      [n._x, n._x + NODE_W / 2, n._x + NODE_W].forEach(lineX => {
        if (Math.abs(x + NODE_W / 2 - lineX) < SNAP_THRESHOLD) {
          lines.push({ type: 'v', pos: lineX });
        }
      });
      [n._y, n._y + NODE_H / 2, n._y + NODE_H].forEach(lineY => {
        if (Math.abs(y + NODE_H / 2 - lineY) < SNAP_THRESHOLD) {
          lines.push({ type: 'h', pos: lineY });
        }
      });
    });
    lines.forEach(line => {
      const el = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      el.setAttribute('class', 'wf-snap-line');
      if (line.type === 'v') {
        el.setAttribute('x1', line.pos); el.setAttribute('y1', 0);
        el.setAttribute('x2', line.pos); el.setAttribute('y2', '100%');
      } else {
        el.setAttribute('x1', 0); el.setAttribute('y1', line.pos);
        el.setAttribute('x2', '100%'); el.setAttribute('y2', line.pos);
      }
      svgEl.appendChild(el);
      _snapLines[line.type].push(el);
    });
  }

  function _clearSnapLines() {
    _snapLines.h.forEach(el => el.remove());
    _snapLines.v.forEach(el => el.remove());
    _snapLines = { h: [], v: [] };
  }

  // ── 事件监听 ──────────────────────────────────────────────────
  document.addEventListener('mousemove', e => {
    if (_templateDrag) {
      if (svgEl && e.target && e.target.closest && (e.target.closest('#wfSvg') || e.target === svgEl)) {
        const c = _svgCoord(e);
        if (!_tempGhost) {
          _tempGhost = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
          _tempGhost.setAttribute('width', NODE_W);
          _tempGhost.setAttribute('height', NODE_H);
          _tempGhost.setAttribute('rx', '12');
          _tempGhost.setAttribute('class', 'edge-temp');
          _tempGhost.style.fill = 'var(--primary-soft)';
          svgEl.appendChild(_tempGhost);
        }
        _tempGhost.style.display = '';
        _tempGhost.setAttribute('transform', `translate(${c.x - NODE_W/2}, ${c.y - NODE_H/2})`);
      } else if (_tempGhost) {
        _tempGhost.style.display = 'none';
      }
      return;
    }
    if (_dragNode) {
      if (!svgEl) return;
      const c = _svgCoord(e);
      const newX = snap(Math.max(0, _ox0 + (c.x - _mx0)));
      const newY = snap(Math.max(0, _oy0 + (c.y - _my0)));
      const snapX = _getSnapOffset('v', newX, newY);
      const snapY = _getSnapOffset('h', newY, newX);
      _dragNode._x = newX + snapX;
      _dragNode._y = newY + snapY;
      _dragEl.setAttribute('transform', `translate(${_dragNode._x}, ${_dragNode._y})`);
      _renderSnapLines(_dragNode._x, _dragNode._y);
      renderEdges();
      return;
    }
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
    _updateHoveredPort(e);
  });

  let _tempGhost = null;

  document.addEventListener('mouseup', e => {
    if (_templateDrag) {
      if (_tempGhost) { _tempGhost.remove(); _tempGhost = null; }
      const onCanvas = svgEl && e.target && e.target.closest && (e.target.closest('#wfSvg') || e.target === svgEl);
      if (onCanvas) {
        const c = _svgCoord(e);
        _createNode(_templateDrag.type, c.x - NODE_W/2, c.y - NODE_H/2, null);
      }
      _templateDrag = null;
      return;
    }
    if (_dragNode) {
      _dragRect?.classList.remove('dragging');
      _capturePositions();
      _clearSnapLines();
      _dragNode = null; _dragEl = null; _dragRect = null;
      return;
    }
    if (_edgeDraw) {
      if (_tempLine) { _tempLine.remove(); _tempLine = null; }
      if (!svgEl) { _edgeDraw = null; return; }
      const c = _svgCoord(e);
      const targetId = _snapToPort(c.x, c.y, true);
      if (targetId && targetId !== _edgeDraw.sourceId) {
        _addEdge(_edgeDraw.sourceId, targetId);
      }
      _edgeDraw = null;
      _isConnecting = false;
    }
    _hoveredPort = null;
  });

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

  document.addEventListener('keydown', e => {
    if (!currentWorkflow) return;
    if ((e.key === 'Delete' || e.key === 'Backspace') && selectedNodeId) {
      if (document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {
        deleteNode(selectedNodeId);
      }
    }
    if ((e.ctrlKey || e.metaKey) && e.key === 's') {
      e.preventDefault();
      if (!e.repeat) saveWorkflow();   // 长按会连发 keydown,不加 e.repeat 会被反复保存
    }
    if ((e.ctrlKey || e.metaKey) && e.key === 'r') {
      e.preventDefault();
      if (!e.repeat) run(currentWorkflow?.id);  // 同上:否则长按 Ctrl+R 一秒能连跑十几次
    }
    if (e.key === 'Escape') {
      selectedNodeId = null;
      nodeElements.forEach(g => g.querySelector('.node-rect')?.classList.remove('selected'));
      closeNodePanel();
    }
  });

  function _updateHoveredPort(e) {
    // 连线过程中也要高亮:让用户看得出"现在松手会连到哪个节点"
    if (!svgEl || _dragNode) return;
    const c = _svgCoord(e);
    let newHover = _snapToPort(c.x, c.y, !!_edgeDraw);
    // 连线时源节点自身不该显示为可连目标(自连会被 _addEdge 前的守卫拒绝)
    if (_edgeDraw && newHover === _edgeDraw.sourceId) newHover = null;
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

  async function _createNode(type, x, y, name) {
    if (!currentWorkflow) return;
    const nodeId = genId('node');
    _posCache[nodeId] = { x: snap(x), y: snap(y) };
    try {
      await _fetch(`/v1/workflows/${currentWorkflow.id}/nodes`, {
        method: 'POST',
        body: JSON.stringify({
          node_id: nodeId,
          type: type,
          name: name || nodeStyle(type).label,
          config: { timeout: 60, position: { x: snap(x), y: snap(y) } },
        }),
      });
      await _reloadWorkflow();
      return nodeId;
    } catch (e) {
      App.toast?.('添加节点失败: ' + e.message, 'err');
      delete _posCache[nodeId];
      return null;
    }
  }

  function renderNodes(svg, wf) {
    svg.querySelectorAll('.wf-node-group').forEach(el => el.remove());
    nodeElements.clear();
    // 注意:必须遍历模块级 nodes(renderDesigner 里由 wf.nodes 克隆而来),而不是 wf.nodes。
    // _layoutPosition 只在这里被调用,坐标 _x/_y 写在谁身上,谁才有坐标。以前这里传 wf.nodes,
    // 于是 _x/_y 被写到"原始对象"上,而 renderEdges / _snapToPort / _getSnapOffset /
    // _capturePositions 一律读模块级 nodes(克隆对象, _x 是 undefined)——结果:
    //   ① renderEdges 算出 M NaN NaN 的路径 => 连线永远不可见(表现为"节点之间连不上");
    //   ② _snapToPort 里 Math.hypot(cx - undefined) 恒为 NaN,任何比较都不成立 => 拖拽永远连不上;
    //   ③ 拖完鼠标松开 _capturePositions 存进 _posCache 的也是空值 => 位置不落盘。
    // 统一到同一批对象后,三处一起恢复。
    const total = nodes.length;
    nodes.forEach((node, idx) => {
      _layoutPosition(node, idx, total);
      const style = nodeStyle(node.type);
      const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      g.setAttribute('transform', `translate(${node._x}, ${node._y})`);
      g.setAttribute('class', `wf-node-group node-type-${node.type || ''}`);
      g.dataset.nodeId = node.id;

      const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      rect.setAttribute('width', NODE_W);
      rect.setAttribute('height', NODE_H);
      rect.setAttribute('rx', '14');
      rect.setAttribute('ry', '14');
      rect.setAttribute('class', 'node-rect');
      if (wf.entry_node === node.id) rect.classList.add('entry-node');
      if (selectedNodeId === node.id) rect.classList.add('selected');

      const statusDot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      statusDot.setAttribute('cx', NODE_W - 14);
      statusDot.setAttribute('cy', 14);
      statusDot.setAttribute('r', '4');
      statusDot.setAttribute('class', 'node-status-dot');

      const iconBg = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      iconBg.setAttribute('cx', 28);
      iconBg.setAttribute('cy', 32);
      iconBg.setAttribute('r', '16');
      iconBg.setAttribute('fill', style.color);
      iconBg.setAttribute('opacity', '0.15');

      const icon = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      icon.setAttribute('x', 28);
      icon.setAttribute('y', 37);
      icon.setAttribute('class', 'node-icon');
      icon.setAttribute('font-size', '16');
      icon.setAttribute('text-anchor', 'middle');
      icon.textContent = style.icon;

      const rawName = node.name || style.label;
      const displayName = rawName.length > 9 ? rawName.slice(0, 8) + '…' : rawName;
      const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      text.setAttribute('x', 52);
      text.setAttribute('y', 28);
      text.setAttribute('class', 'node-text');
      text.textContent = displayName;
      if (displayName !== rawName) {
        const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
        title.textContent = rawName;
        text.appendChild(title);
      }

      const typeLabel = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      typeLabel.setAttribute('x', 52);
      typeLabel.setAttribute('y', 48);
      typeLabel.setAttribute('class', 'node-type-label');
      typeLabel.textContent = style.label;

      const port = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      port.setAttribute('cx', NODE_W);
      port.setAttribute('cy', NODE_H / 2);
      port.setAttribute('r', '8');
      port.setAttribute('class', 'node-port');
      port.style.fill = style.color;
      
      const portGlow = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      portGlow.setAttribute('cx', NODE_W);
      portGlow.setAttribute('cy', NODE_H / 2);
      portGlow.setAttribute('r', '12');
      portGlow.setAttribute('fill', 'none');
      portGlow.setAttribute('stroke', style.color);
      portGlow.setAttribute('stroke-width', '1');
      portGlow.setAttribute('opacity', '0.3');
      portGlow.classList.add('port-glow');

      g.appendChild(rect);
      g.appendChild(statusDot);
      g.appendChild(iconBg);
      g.appendChild(icon);
      g.appendChild(text);
      g.appendChild(typeLabel);
      g.appendChild(portGlow);
      g.appendChild(port);
      svg.appendChild(g);
      nodeElements.set(node.id, g);

      g.addEventListener('click', e => { e.stopPropagation(); selectNode(node.id); });
      g.addEventListener('dblclick', e => { e.stopPropagation(); openNodePanel(node.id); });
      port.addEventListener('mousedown', e => {
        if (e.button !== 0) return;
        e.stopPropagation();
        e.preventDefault();
        _isConnecting = true;
        _edgeDraw = { sourceId: node.id, startX: node._x + NODE_W, startY: node._y + NODE_H / 2 };
      });
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

  function renderEdges() {
    if (!svgEl) return;
    svgEl.querySelectorAll('.edge-line').forEach(el => el.remove());
    const nodeMap = new Map();
    nodes.forEach(n => nodeMap.set(n.id, n));
    (edges || []).forEach(edge => {
      const src = nodeMap.get(edge.source);
      const tgt = nodeMap.get(edge.target);
      if (!src || !tgt) return;
      const startX = src._x + NODE_W;
      const startY = src._y + NODE_H / 2;
      const endX = tgt._x;
      const endY = tgt._y + NODE_H / 2;
      const dx = Math.abs(endX - startX) * 0.5;
      const ctrlOffset = Math.min(dx, 100);
      let path;
      if (startX < endX) {
        path = `M ${startX} ${startY} C ${startX + ctrlOffset} ${startY}, ${endX - ctrlOffset} ${endY}, ${endX} ${endY}`;
      } else {
        path = `M ${startX} ${startY} C ${startX - ctrlOffset} ${startY}, ${endX + ctrlOffset} ${endY}, ${endX} ${endY}`;
      }
      const pathEl = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      pathEl.setAttribute('d', path);
      pathEl.setAttribute('class', 'edge-line');
      pathEl.setAttribute('fill', 'none');
      pathEl.setAttribute('stroke', 'transparent');
      pathEl.setAttribute('stroke-width', '12');
      pathEl.dataset.edgeId = edge.id;
      pathEl.addEventListener('dblclick', e => { e.stopPropagation(); _deleteEdge(edge.id); });
      svgEl.appendChild(pathEl);
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      line.setAttribute('d', path);
      line.setAttribute('class', 'edge-line');
      line.setAttribute('fill', 'none');
      line.setAttribute('marker-end', 'url(#arrowhead)');
      line.dataset.edgeId = edge.id;
      svgEl.appendChild(line);
    });
  }

  async function _addEdge(sourceId, targetId) {
    if (!currentWorkflow) return;
    _capturePositions();
    try {
      await _fetch(`/v1/workflows/${currentWorkflow.id}/edges`, {
        method: 'POST', body: JSON.stringify({ source: sourceId, target: targetId }),
      });
      await _reloadWorkflow();
    } catch (e) { App.toast?.('添加边失败: ' + e.message, 'err'); }
  }

  async function _deleteEdge(edgeId) {
    if (!currentWorkflow) return;
    if (!edgeId) { App.toast?.('边 ID 缺失', 'err'); return; }
    if (!await App.confirm?.('删除此连接？')) return;
    _capturePositions();
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
    /* 修复"点节点信息里的输入框,面板就消失":
       面板实际类名是 .wf-properties(id=wfProperties), 而这里白名单写的却是历史遗留的
       .wf-node-panel(页面里已经不存在)。于是点面板内任何控件,事件冒泡到 document 都被
       判为"点了空白处" → closeNodePanel()。现直接按"事件目标是否在面板内"判定。 */
    if (e.target.closest('.wf-properties')
      || e.target.closest('.wf-node-panel')          /* 历史遗留类名,保留兼容 */
      || e.target.closest('.wf-secondary-panels')    /* 版本/调度侧栏,点它不该顺带关掉节点面板 */
      || e.target.closest('.wf-run-panel')) return;  /* 运行详情面板,同上 */
    selectedNodeId = null;
    nodeElements.forEach(g => g.querySelector('.node-rect')?.classList.remove('selected'));
    closeNodePanel();
  });

  function openNodePanel(nodeId) {
    const panel = document.getElementById('wfProperties') || $('wfNodePanel');
    if (!panel) return;
    panel.removeAttribute('hidden');
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
        <button class="np-close wf-close-panel-btn" data-node-id="${node.id}">×</button>
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
          <button class="np-btn np-btn-save wf-save-node-btn" data-node-id="${node.id}">保存</button>
          <button class="np-btn np-btn-delete wf-delete-node-btn" data-node-id="${node.id}">删除</button>
        </div>
      </div>`;
    
    // 绑定按钮事件（避免内联onclick被CSP阻止）
    const closeBtn = panel.querySelector('.wf-close-panel-btn');
    if (closeBtn) closeBtn.addEventListener('click', () => closeNodePanel());
    const saveBtn = panel.querySelector('.wf-save-node-btn');
    if (saveBtn) saveBtn.addEventListener('click', () => saveNodeConfig(saveBtn.dataset.nodeId));
    const deleteBtn = panel.querySelector('.wf-delete-node-btn');
    if (deleteBtn) deleteBtn.addEventListener('click', () => deleteNode(deleteBtn.dataset.nodeId));
  }

  function closeNodePanel() {
    const panel = document.getElementById('wfProperties') || $('wfNodePanel');
    if (panel) { panel.classList.remove('open'); panel.setAttribute('hidden', ''); setTimeout(() => { panel.innerHTML = ''; }, 200); }
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
      config.error_strategy = $('np-error-strategy')?.value || 'fail_fast';
      config.retry_count = parseInt($('np-retry-count')?.value) || 0;
      config.fallback_node_id = $('np-fallback-node')?.value?.trim() || null;
      updates.error_strategy = config.error_strategy;
      updates.retry_count = config.retry_count;
      updates.fallback_node_id = config.fallback_node_id;
      updates.tool_name = config.tool_name;
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
      const subgraphStr = $('np-subgraph-ids')?.value || '';
      config.subgraph_ids = subgraphStr.split(',').map(s => s.trim()).filter(Boolean);
    } else if (type === 'sleep') {
      config.seconds = parseFloat($('np-sleep-secs')?.value) || 1;
    }
    updates.config = config;
    if (type === 'condition') { updates.condition_expr = config.condition_expr; updates.branches = config.branches; }
    if (type === 'http') updates.http_config = config;
    if (type === 'script') updates.script_code = config.script_code;
    if (type === 'loop') updates.loop_config = config;
    try {
      await _fetch(`/v1/workflows/${currentWorkflow.id}/nodes/${nodeId}`, {
        method: 'PATCH', body: JSON.stringify(updates),
      });
      const nodeObj = nodes.find(n => n.id === nodeId);
      if (nodeObj && nodeObj._x != null) {
        try {
          await _fetch(`/v1/workflows/${currentWorkflow.id}/nodes/${nodeId}`, {
            method: 'PATCH',
            body: JSON.stringify({ position: { x: nodeObj._x, y: nodeObj._y } }),
          });
        } catch (_) {}
      }
      App.toast?.('节点已更新', 'ok');
      closeNodePanel();
      await _reloadWorkflow();
    } catch (e) { App.toast?.('保存失败: ' + e.message, 'err'); }
  }

  async function deleteNode(nodeId) {
    if (!currentWorkflow) return;
    if (currentWorkflow.entry_node === nodeId) { App.toast?.('不能删除入口节点', 'err'); return; }
    if (!await App.confirm?.('确定删除节点 "' + (nodes.find(n => n.id === nodeId)?.name || nodeId) + '"？关联的边也会删除。')) return;
    _capturePositions();
    delete _posCache[nodeId];
    try {
      await _fetch(`/v1/workflows/${currentWorkflow.id}/nodes/${nodeId}`, { method: 'DELETE' });
      App.toast?.('节点已删除', 'ok');
      closeNodePanel();
      await _reloadWorkflow();
    } catch (e) { App.toast?.('删除失败: ' + e.message, 'err'); }
  }

  async function _reloadWorkflow() {
    if (!currentWorkflow) return;
    _capturePositions();
    try {
      const data = await _fetch(`/v1/workflows/${currentWorkflow.id}`);
      renderDesigner(data, true);  // 保留当前缩放/视图, 避免保存后画面跳变
    } catch (e) { App.toast?.('刷新失败: ' + e.message, 'err'); }
  }

  // 区分两种渲染:
  //  - load/切换工作流: keepView=false, 重置缩放为 1(全新画布)。
  //  - 保存/增删节点后的 _reloadWorkflow: keepView=true, 保留当前 viewScale,
  //    避免"保存后缩放跳回 100% / 画布跳动"造成"画线对不上/丢布局"的错觉。
  function renderDesigner(wf, keepView = false) {
    currentWorkflow = wf;
    nodes = wf.nodes ? wf.nodes.map(n => ({ ...n })) : [];
    edges = (wf.edges || []).map(e => ({ ...e }));
    selectedNodeId = null;
    if (!keepView) viewScale = 1;
    const svg = ensureSvg();
    if (!svg) return;
    buildDefs(svg);
    renderNodes(svg, wf);
    renderEdges();
    applyZoom();  // 同步 transform, 保证缩放状态下重绘后坐标一致
    setTimeout(_updateSvgSize, 50);
    if (svgEl && typeof ResizeObserver !== 'undefined') {
      if (svgEl._resizeObserver) svgEl._resizeObserver.disconnect();
      const designerEl = _designerEl();
      if (designerEl) {
        svgEl._resizeObserver = new ResizeObserver(() => { _updateSvgSize(); renderEdges(); });
        svgEl._resizeObserver.observe(designerEl);
      }
    }
  }

  async function loadList() {
    try {
      const data = await _fetch('/v1/workflows');
      renderList(data.workflows || []);
    } catch (e) { console.error('加载工作流列表失败:', e); }
  }

  function renderList(workflows) {
    const listEl = document.getElementById('workflowList') || $('wfList');
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
          <button class="mini-btn wf-edit-btn" data-id="${wf.id}">编辑</button>
          <button class="mini-btn wf-run-btn" data-id="${wf.id}">▶ 运行</button>
          <button class="mini-btn danger wf-delete-btn" data-id="${wf.id}">删除</button>
        </div>
      </div>`).join('');
    
    listEl.addEventListener('click', e => {
      const btn = e.target.closest('.wf-item-actions .mini-btn');
      if (!btn) return;
      e.stopPropagation();
      const id = btn.dataset.id;
      if (btn.classList.contains('wf-edit-btn')) App.Workflow.load(id);
      else if (btn.classList.contains('wf-run-btn')) App.Workflow.run(id);
      else if (btn.classList.contains('wf-delete-btn')) App.Workflow.deleteWorkflow(id);
    });
  }

  async function load(id) {
    try {
      const data = await _fetch(`/v1/workflows/${id}`);
      renderDesigner(data);
      const toolbar = $('wfToolbar');
      if (toolbar) toolbar.hidden = false;
      const historyWrap = $('wfRunHistoryWrap');
      if (historyWrap) historyWrap.hidden = false;
      (document.getElementById('workflowList') || $('wfList'))?.querySelectorAll('.wf-item').forEach(el => {
        el.classList.toggle('active', el.dataset.id === id);
      });
      loadRunHistory();
      loadVersions();
      loadSchedules();
      const wrap = $('wfSecondaryPanels');
      if (wrap) wrap.hidden = true;
      const panels = ['wfVersionPanel', 'wfSchedulePanel', 'wfStatsPanel'];
      panels.forEach(p => { if ($(p)) $(p).style.display = 'none'; });
    } catch (e) { App.toast?.('加载失败: ' + e.message, 'err'); }
  }

  async function create() {
    const name = await App.prompt?.('工作流名称:', '新建工作流');
    if (!name) return;
    try {
      // 后端 create_workflow 有两道校验: ①entry_node 必须存在于 nodes 里;
      // ②除 start 外的节点必须已有入边。所以旧的 { nodes: [], entry_node: '' } 必然 400
      // ("入口节点  不存在"),表现为点"新建"永远创建失败。
      // 最少合法形态就是"只带一个 start 节点",顺带让新画布自带"开始"节点。
      const startId = 'start';
      const data = await _fetch('/v1/workflows', {
        method: 'POST',
        body: JSON.stringify({
          name,
          description: '',
          entry_node: startId,
          edges: [],
          nodes: [{
            id: startId, type: 'start', name: '开始',
            config: { timeout: 60 }, position: { x: 80, y: 80 },
          }],
        }),
      });
      App.toast?.('工作流已创建', 'ok');
      await load(data.id);
    } catch (e) { App.toast?.('创建失败: ' + e.message, 'err'); }
  }

  let _runAborter = null;
  async function run(id) {
    if (_runAborter) { _runAborter.abort(); _runAborter = null; showRunStatus('⏹ 已取消', ''); return; }
    _runAborter = new AbortController();
    const signal = _runAborter.signal;
    activeRunId = null;
    clearNodeStates();
    showRunStatus('▶ 运行中...', 'running');
    try {
      const resp = await fetch(_apiBase() + `/v1/workflows/${id}/run/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(_apiKey() ? { 'Authorization': 'Bearer ' + _apiKey() } : {}) },
        body: JSON.stringify({ inputs: {}, stream_detail: true }),
        signal,
      });
      if (!resp.ok) {
        let detail = '运行失败';
        try { const d = await resp.json(); detail = d.detail || detail; } catch(_) {}
        throw new Error(detail);
      }
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
            try { handleStreamEvent(JSON.parse(line.substring(6))); } catch(_) {}
          }
        }
      }
      _runAborter = null;
    } catch (e) {
      _runAborter = null;
      if (e && e.name === 'AbortError') { showRunStatus('⏹ 已取消', ''); return; }
      showRunStatus('❌ 运行失败', 'error');
      App.toast?.('运行失败: ' + e.message, 'err');
    }
  }

  function handleStreamEvent(data) {
    if (!data || typeof data !== 'object') return;
    if (data.run_id) activeRunId = data.run_id;
    if (data.node_id && data.node_id !== '__run__' && data.status) {
      applyNodeStates({ [data.node_id]: { status: data.status } });
      const node = nodes.find(n => n.id === data.node_id);
      const label = node ? node.name : data.node_id;
      if (data.status === 'running') showRunStatus(`▶ ${label} 执行中...`, '');
      else if (data.status === 'success') showRunStatus(`✓ ${label} 完成`, 'success');
      else if (data.status === 'failed') showRunStatus(`✗ ${label} 失败`, 'error');
      else if (data.status === 'skipped') showRunStatus(`— ${label} 已跳过`, '');
    }
    if (data.terminal) {
      if (data.status === 'completed') {
        showRunStatus('✅ 运行完成', 'success');
        if (data.node_states) applyNodeStates(data.node_states);
        runHistory.push({ run_id: data.run_id, status: 'completed', duration: data.duration,
          output: data.output, node_states: data.node_states, history: data.history, error: data.error, timestamp: Date.now() });
        renderRunHistory();
      } else {
        showRunStatus('❌ 运行失败', 'error');
        runHistory.push({ run_id: data.run_id, status: data.status || 'failed', duration: data.duration,
          output: data.output, node_states: data.node_states, history: data.history,
          error: data.error, timestamp: Date.now() });
        renderRunHistory();
      }
      return;
    }
    if (data.node_states !== undefined && data.status) {
      if (data.status === 'completed') {
        showRunStatus('✅ 运行完成', 'success');
        runHistory.push({ run_id: data.run_id, status: 'completed', duration: data.duration,
          output: data.output, node_states: data.node_states, history: data.history, timestamp: Date.now() });
        renderRunHistory();
      } else if (data.status === 'failed') {
        showRunStatus('❌ 运行失败', 'error');
        runHistory.push({ run_id: data.run_id, status: 'failed', duration: data.duration,
          output: data.output, node_states: data.node_states, error: data.error, timestamp: Date.now() });
        renderRunHistory();
      }
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
    const wrap = $('wfRunHistoryWrap');
    if (wrap) wrap.hidden = !runHistory.length;
    if (!runHistory.length) return;
    el.innerHTML = runHistory.slice(-8).reverse().map(r => `
      <div class="wf-run-item ${r.status === 'completed' ? 'completed' : 'failed'} wf-run-item-clickable" data-run-id="${r.run_id}">
        <span class="wf-run-icon">${r.status === 'completed' ? '✅' : '❌'}</span>
        <span class="wf-run-time">${formatTime(r.timestamp)}</span>
        <span class="wf-run-id">${r.run_id.slice(-8)}</span>
        <span class="wf-run-dur">${r.duration != null ? (r.duration * 1000).toFixed(0) + 'ms' : ''}</span>
        <span class="wf-run-status-badge">${r.status === 'completed' ? '成功' : r.status}</span>
      </div>`).join('');
    el.addEventListener('click', e => {
      const item = e.target.closest('.wf-run-item-clickable');
      if (item) App.Workflow.showRunDetail(item.dataset.runId);
    });
  }

  function formatTime(ts) {
    if (!ts) return '';
    const asMs = ts < 1e12 ? ts * 1000 : ts;
    return new Date(asMs).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
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
    panel.hidden = false;
    const stateRows = Object.entries(run.node_states || {}).map(([nid, state]) => {
      const node = nodes.find(n => n.id === nid);
      const color = state.status === 'success' ? 'var(--ok)' : state.status === 'failed' ? 'var(--err)' : 'var(--muted)';
      const icon = state.status === 'success' ? '✓' : state.status === 'failed' ? '✗' : state.status === 'skipped' ? '—' : '?';
      const errText = state.error ? escapeHtml(String(state.error).slice(0, 120)) : '-';
      return `<tr><td>${escapeHtml(node?.name || nid)}</td><td style="color:${color}">${icon} ${state.status}</td><td>${state.duration != null ? (state.duration*1000).toFixed(0)+'ms' : '-'}</td><td>${errText}</td></tr>`;
    }).join('');
    const outputJson = run.output ? escapeHtml(JSON.stringify(run.output, null, 2)) : '-';
    panel.innerHTML = `
      <div class="wf-run-panel-header"><span>📋 运行详情 · ${run.run_id.slice(-8)}</span><button class="np-close wf-close-run-panel-btn">×</button></div>
      <div class="wf-run-detail-tabs">
        <button class="wf-tab wf-run-tab-btn active" data-tab="nodes">节点状态</button>
        <button class="wf-tab wf-run-tab-btn" data-tab="output">输出结果</button>
      </div>
      <div id="wfRunNodes" class="wf-run-tab-content active">
        <table class="wf-state-table"><thead><tr><th>节点</th><th>状态</th><th>耗时</th><th>错误</th></tr></thead><tbody>${stateRows || '<tr><td colspan="4">无数据</td></tr>'}</tbody></table>
      </div>
      <div id="wfRunOutput" class="wf-run-tab-content"><pre class="wf-output-pre">${outputJson}</pre></div>`;
    
    // 绑定事件（避免内联onclick被CSP阻止）
    const closeBtn = panel.querySelector('.wf-close-run-panel-btn');
    if (closeBtn) closeBtn.addEventListener('click', closeRunPanel);
    panel.addEventListener('click', e => {
      const tabBtn = e.target.closest('.wf-run-tab-btn');
      if (tabBtn) switchRunTab(tabBtn.dataset.tab, tabBtn);
    });
  }

  function switchRunTab(tab, btn) {
    document.querySelectorAll('.wf-run-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.wf-run-tab-content').forEach(t => t.classList.remove('active'));
    if (btn) btn.classList.add('active');
    document.getElementById('wfRun' + tab.charAt(0).toUpperCase() + tab.slice(1))?.classList.add('active');
  }

  function closeRunPanel() { const p = $('wfRunPanel'); if (p) p.hidden = true; }
  function escapeHtml(t) {
    if (t == null) return '';
    return String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  async function autoLayout() {
    if (!currentWorkflow || nodes.length === 0) return;
    const count = nodes.length;
    const cols = count > 4 ? 3 : count > 2 ? 2 : 1;
    const colW = NODE_W + PAD * 2;
    const rowH = NODE_H + PAD * 2;
    nodes.forEach((node, idx) => {
      node._x = PAD + (idx % cols) * colW;
      node._y = PAD + Math.floor(idx / cols) * rowH;
    });
    _capturePositions();
    _savePositions();
    renderNodes(svgEl, currentWorkflow);
    renderEdges();
    try {
      await saveWorkflow();
      App.toast?.('已自动布局并保存', 'ok');
    } catch (_) {}
  }

  async function addNode() {
    if (!currentWorkflow) { App.toast?.('请先选择一个工作流', 'err'); return; }
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
    if (isNaN(idx) || idx < 0 || idx >= types.length) { App.toast?.('无效选择', 'err'); return; }
    const selected = types[idx];
    const name = await App.prompt?.('节点名称:', selected.label + ' ' + (nodes.length + 1));
    if (!name) return;
    const x = Math.max(60, ...nodes.map(n => n._x || 0)) + NODE_W + 40;
    const y = Math.max(60, ...nodes.map(n => n._y || 0));
    await _createNode(selected.type, x, y, name.trim());
  }

  async function saveWorkflow() {
    if (!currentWorkflow) return;
    _capturePositions();
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
            position: n._x != null ? { x: n._x, y: n._y } : (n.position || null),
          })),
          edges: edges.map(e => ({ id: e.id, source: e.source, target: e.target, condition: e.condition, condition_expr: e.condition_expr })),
          entry_node: currentWorkflow.entry_node,
          error_mode: currentWorkflow.error_mode,
        }),
      });
      App.toast?.('已保存', 'ok');
      await _reloadWorkflow();
    } catch (e) { App.toast?.('保存失败: ' + e.message, 'err'); }
  }

  async function deleteWorkflow(id) {
    if (!id) id = currentWorkflow?.id;
    if (!id) { App.toast?.('请先选择工作流', 'err'); return; }
    if (!await App.confirm?.('确定删除此工作流？所有运行记录也将删除。')) return;
    try {
      await _fetch(`/v1/workflows/${id}`, { method: 'DELETE' });
      App.toast?.('已删除', 'ok');
      Object.keys(_posCache).forEach(k => { delete _posCache[k]; });
      currentWorkflow = null; nodes = []; edges = []; nodeElements.clear();
      selectedNodeId = null; activeRunId = null; runHistory = [];
      const historyWrap = $('wfRunHistoryWrap');
      if (historyWrap) historyWrap.hidden = true;
      const svg = $('wfSvg');
      if (svg) svg.querySelectorAll('.wf-node-group, .edge-line, .edge-temp').forEach(el => el.remove());
      svgEl = null;
      const status = $('wfRunStatus');
      if (status) status.textContent = '';
      loadList();
    } catch (e) { App.toast?.('删除失败: ' + e.message, 'err'); }
  }

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
      runHistoryRefresh: ['wfRunHistoryRefresh', loadRunHistory],
      addNodeAlt: ['wfAddNodeBtn2', addNode],
      saveVersion: ['wfSaveVersionBtn', () => { _toggleSecondaryPanel('wfVersionPanel'); saveVersion(); }],
      exportWf: ['wfExportBtn', exportWorkflow],
      importWf: ['wfImportBtn', importWorkflow],
      createSchedule: ['wfCreateScheduleBtn', () => { _toggleSecondaryPanel('wfSchedulePanel'); createSchedule(); }],
      loadStats: ['wfStatsBtn', () => { _toggleSecondaryPanel('wfStatsPanel'); if (typeof loadStats === 'function') loadStats(); else console.warn('loadStats not ready'); }],
    };
    Object.entries(btns).forEach(([id, [elId, fn]]) => {
      const el = $(elId);
      if (el) el.addEventListener('click', fn);
    });

    const closeVersionBtn = $('closeVersionPanel');
    if (closeVersionBtn) closeVersionBtn.addEventListener('click', () => { App.Workflow._closeSecondaryPanel(); });
    const closeScheduleBtn = $('closeSchedulePanel');
    if (closeScheduleBtn) closeScheduleBtn.addEventListener('click', () => { App.Workflow._closeSecondaryPanel(); });
    const hintCloseBtn = $('wfHintClose');
    if (hintCloseBtn) hintCloseBtn.addEventListener('click', () => { hintCloseBtn.parentElement.remove(); });

    _initNodeTemplateList();
    const svg = $('wfSvg');
    if (svg) {
      svg.addEventListener('dblclick', e => {
        if (e.target !== svg && e.target.tagName !== 'svg') return;
        if (!currentWorkflow) return;
        const c = _svgCoord(e);
        _createNode('agent', c.x - NODE_W / 2, c.y - NODE_H / 2);
      });
    }
    loadList();
    showWorkflowHint();
  }

  function _initNodeTemplateList() {
    const list = $('wfNodeList');
    if (!list || list.children.length) return;
    NODE_TEMPLATES.forEach(tpl => {
      const chip = document.createElement('div');
      chip.className = 'wf-node-chip';
      chip.dataset.type = tpl.type;
      chip.title = tpl.desc;
      chip.innerHTML = `<span class="chip-icon">${tpl.icon}</span><span class="chip-label">${tpl.label}</span><span class="chip-desc">${tpl.desc}</span>`;
      chip.addEventListener('mousedown', e => {
        if (e.button !== 0) return;
        e.preventDefault();
        _templateDrag = { type: tpl.type };
      });
      chip.addEventListener('click', () => {
        if (!currentWorkflow) { App.toast?.('请先在下方列表选择一个工作流', 'info'); return; }
        const x = Math.max(60, ...nodes.map(n => n._x || 0)) + NODE_W + 40;
        const y = Math.max(60, ...nodes.map(n => n._y || 0));
        _createNode(tpl.type, x, y, null);
      });
      list.appendChild(chip);
    });
    const search = $('wfSearch');
    if (search) {
      search.addEventListener('input', () => {
        const kw = search.value.trim().toLowerCase();
        list.querySelectorAll('.wf-node-chip').forEach(chip => {
          const tpl = NODE_TEMPLATES.find(t => t.type === chip.dataset.type);
          const hit = !kw || (tpl.label + tpl.desc + tpl.type).toLowerCase().includes(kw);
          chip.style.display = hit ? '' : 'none';
        });
      });
    }
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
    if (!status) return;
    const running = /运行中|完成|失败|取消/.test(status.textContent);
    if (running) return;
    status.textContent = viewScale === 1 ? '' : `缩放 ${Math.round(viewScale * 100)}%`;
    status.classList.remove('success', 'error');
  }

  function showWorkflowHint() {
    if (document.querySelector('.wf-hint')) return;
    const hint = document.createElement('div');
    hint.className = 'wf-hint';
    hint.innerHTML = `
      <div class="wf-hint-title">⚙️ 工作流操作指南</div>
      <div class="wf-hint-body">
        <p>🖱️ <strong>拖拽节点</strong> — 按住节点拖动调整位置，松开时自动吸附对齐</p>
        <p>🔗 <strong>添加连线</strong> — 从右侧端口拖到目标节点</p>
        <p>✏️ <strong>编辑节点</strong> — 双击节点打开配置面板</p>
        <p>🗑️ <strong>删除节点/连线</strong> — 选中后按 Delete 键</p>
        <p>🔍 <strong>缩放画布</strong> — 滚轮缩放或使用工具栏按钮</p>
        <p>📐 <strong>自动布局</strong> — 点击"布局"自动排列节点</p>
        <p>🧲 <strong>智能对齐</strong> — 拖拽时自动吸附到网格和其他节点边缘</p>
        <p>💾 <strong>保存</strong> — Ctrl + S</p>
        <p>▶ <strong>运行</strong> — Ctrl + R</p>
        <p>⎋ <strong>取消选择</strong> — Esc 键</p>
      </div>
      <button class="wf-hint-close" id="wfHintClose">我知道了</button>
    `;
    const container = document.getElementById('workflowDesigner') || $('wfDesigner');
    if (container) container.appendChild(hint);
    const closeBtn = container.querySelector('#wfHintClose');
    if (closeBtn) closeBtn.addEventListener('click', () => { hint.remove(); });
  }

  async function saveVersion() {
    if (!currentWorkflow) return;
    const version = await App.prompt?.('版本号 (如 v1.1):', 'v' + (currentWorkflow.version || '1.0'));
    if (!version) return;
    const comment = await App.prompt?.('版本说明 (可选):', '') || '';
    try {
      const data = await _fetch(`/v1/workflows/${currentWorkflow.id}/versions`, {
        method: 'POST', body: JSON.stringify({ version: version.trim(), comment }),
      });
      App.toast?.(`版本 ${version} 已保存`, 'ok');
      loadVersions();
    } catch (e) { App.toast?.('保存版本失败: ' + e.message, 'err'); }
  }

  async function loadVersions() {
    if (!currentWorkflow) return;
    try {
      const data = await _fetch(`/v1/workflows/${currentWorkflow.id}/versions`);
      _versionHistory = data.versions || [];
      renderVersionHistory();
    } catch (e) { console.error('加载版本历史失败:', e); }
  }

  function renderVersionHistory() {
    const el = $('wfVersionList');
    if (!el) return;
    if (!_versionHistory.length) { el.innerHTML = '<div class="wf-empty-hint">暂无版本历史</div>'; return; }
    el.innerHTML = _versionHistory.map(v => `
      <div class="wf-version-item">
        <span class="wf-version-tag">${v.version}</span>
        <span class="wf-version-time">${formatTime(v.created_at * 1000)}</span>
        <button class="mini-btn wf-rollback-btn" data-version-id="${v.version_id}">回滚</button>
      </div>`).join('');
    el.addEventListener('click', e => {
      const btn = e.target.closest('.wf-rollback-btn');
      if (btn) App.Workflow.rollbackVersion(btn.dataset.versionId);
    });
  }

  async function rollbackVersion(versionId) {
    if (!currentWorkflow) return;
    if (!await App.confirm?.('确定回滚到此版本？当前版本将自动备份。')) return;
    try {
      const data = await _fetch(`/v1/workflows/${currentWorkflow.id}/versions/${versionId}/rollback`, { method: 'POST' });
      App.toast?.(`已回滚到版本 ${data.version || versionId}`, 'ok');
      await _reloadWorkflow(); loadVersions();
    } catch (e) { App.toast?.('回滚失败: ' + e.message, 'err'); }
  }

  async function exportWorkflow() {
    if (!currentWorkflow) { App.toast?.('请先选择工作流', 'err'); return; }
    try {
      const data = await _fetch(`/v1/workflows/${currentWorkflow.id}/export`);
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a'); a.href = url;
      a.download = `${data.name || currentWorkflow.id}.json`; a.click();
      URL.revokeObjectURL(url);
      App.toast?.('工作流已导出', 'ok');
    } catch (e) { App.toast?.('导出失败: ' + e.message, 'err'); }
  }

  async function importWorkflow() {
    const input = document.createElement('input'); input.type = 'file'; input.accept = '.json';
    input.onchange = async (e) => {
      const file = e.target.files[0]; if (!file) return;
      try {
        const text = await file.text(); const data = JSON.parse(text);
        if (!data.nodes || !data.edges) throw new Error('无效的工作流文件');
        const result = await _fetch('/v1/workflows/import', {
          method: 'POST', body: JSON.stringify({ workflow: data, import_as: 'new' }),
        });
        App.toast?.(`已导入: ${result.name}`, 'ok');
        await loadList(); setTimeout(() => load(result.id), 100);
      } catch (err) { App.toast?.('导入失败: ' + err.message, 'err'); }
    };
    input.click();
  }

  async function generateByDescription() {
    if (!currentWorkflow) { App.toast?.('请先选择工作流', 'err'); return; }
    const desc = await App.prompt?.('请输入工作流描述:', '');
    if (!desc) return;
    try {
      const data = await _fetch('/v1/workflows/generate', {
        method: 'POST',
        body: JSON.stringify({ name: desc.slice(0, 30), description: desc }),
      });
      App.toast?.(`工作流已生成: ${data.name}`, 'ok');
      await load(data.id);
    } catch (e) { App.toast?.('生成失败: ' + e.message, 'err'); }
  }

  async function loadSchedules() {
    if (!currentWorkflow) return;
    try {
      const data = await _fetch(`/v1/workflows/${currentWorkflow.id}/schedules`);
      _scheduleList = data.schedules || [];
      renderSchedules();
    } catch (e) { console.error('加载调度失败:', e); }
  }

  function renderSchedules() {
    const el = $('wfScheduleList'); if (!el) return;
    if (!_scheduleList.length) { el.innerHTML = '<div class="wf-empty-hint">暂无定时调度</div>'; return; }
    el.innerHTML = _scheduleList.map(s => `
      <div class="wf-schedule-item ${s.enabled ? '' : 'disabled'}">
        <span class="wf-schedule-icon">${s.enabled ? '⏰' : '⏸'}</span>
        <div class="wf-schedule-info">
          <div class="wf-schedule-name">${s.name}</div>
          <div class="wf-schedule-cron">${s.cron_expr}</div>
          <div class="wf-schedule-next">下次: ${s.next_run_at ? formatTime(s.next_run_at * 1000) : '-'}</div>
        </div>
        <button class="mini-btn ${s.enabled ? 'danger' : ''} wf-toggle-schedule" data-id="${s.schedule_id}">
          ${s.enabled ? '禁用' : '启用'}
        </button>
      </div>`).join('');
    el.addEventListener('click', e => {
      const btn = e.target.closest('.wf-toggle-schedule');
      if (btn) App.Workflow.toggleSchedule(btn.dataset.id);
    });
  }

  async function createSchedule() {
    if (!currentWorkflow) { App.toast?.('请先选择工作流', 'err'); return; }
    const name = await App.prompt?.('调度名称:', '定时运行') || '定时运行';
    const cronExpr = await App.prompt?.('Cron 表达式:', '0 9 * * *') || '0 9 * * *';
    const session = await App.prompt?.('会话 ID (可选):', 'default') || 'default';
    try {
      const data = await _fetch(`/v1/workflows/${currentWorkflow.id}/schedules`, {
        method: 'POST', body: JSON.stringify({ name, cron_expr: cronExpr, inputs: {}, session_id: session }),
      });
      App.toast?.('调度已创建', 'ok'); loadSchedules();
    } catch (e) { App.toast?.('创建调度失败: ' + e.message, 'err'); }
  }

  async function toggleSchedule(scheduleId) {
    try {
      await _fetch(`/v1/workflows/${currentWorkflow.id}/schedules/${scheduleId}/toggle`, { method: 'POST' });
      loadSchedules();
    } catch (e) { App.toast?.('操作失败: ' + e.message, 'err'); }
  }

  async function deleteSchedule(scheduleId) {
    if (!await App.confirm?.('确定删除此调度？')) return;
    try {
      await _fetch(`/v1/workflows/${currentWorkflow.id}/schedules/${scheduleId}`, { method: 'DELETE' });
      App.toast?.('调度已删除', 'ok'); loadSchedules();
    } catch (e) { App.toast?.('删除失败: ' + e.message, 'err'); }
  }

  async function loadStats() {
    if (!currentWorkflow) return;
    try {
      const data = await _fetch(`/v1/workflows/${currentWorkflow.id}/stats?days=30`);
      _statsData = data;
      renderStats();
    } catch (e) { console.error('加载统计失败:', e); }
  }

  function renderStats() {
    const el = $('wfStatsContent'); if (!el) return;
    if (!_statsData) { el.innerHTML = '<div class="wf-empty-hint">暂无统计数据</div>'; return; }
    const d = _statsData;
    el.innerHTML = `
      <div class="wf-stats-grid">
        <div class="wf-stat-card">
          <div class="wf-stat-value">${d.total_runs ?? 0}</div>
          <div class="wf-stat-label">总运行次数</div>
        </div>
        <div class="wf-stat-card success">
          <div class="wf-stat-value">${d.success_count ?? 0}</div>
          <div class="wf-stat-label">成功</div>
        </div>
        <div class="wf-stat-card fail">
          <div class="wf-stat-value">${d.failed_count ?? 0}</div>
          <div class="wf-stat-label">失败</div>
        </div>
        <div class="wf-stat-card">
          <div class="wf-stat-value">${d.success_rate ?? 0}%</div>
          <div class="wf-stat-label">成功率</div>
        </div>
        <div class="wf-stat-card">
          <div class="wf-stat-value">${d.avg_duration_sec ?? 0}s</div>
          <div class="wf-stat-label">平均耗时</div>
        </div>
      </div>
      ${d.latest_run ? `
      <div class="wf-stats-latest">
        <div class="wf-stats-title">最近运行</div>
        <div class="wf-stats-row">
          <span>Run: ${d.latest_run.run_id}</span>
          <span class="wf-status-${d.latest_run.status}">${d.latest_run.status === 'completed' ? '✓ 成功' : '✗ 失败'}</span>
          <span>耗时: ${d.latest_run.duration ? d.latest_run.duration.toFixed(1) + 's' : '-'}</span>
        </div>
      </div>` : ''}
    `;
  }

  function _toggleSecondaryPanel(panelId) {
    const wrap = $('wfSecondaryPanels');
    const panels = ['wfVersionPanel', 'wfSchedulePanel', 'wfStatsPanel'];
    if (!wrap || !panels.includes(panelId)) return;
    const isVisible = !wrap.hidden;
    panels.forEach(p => {
      const el = $(p);
      if (el) el.style.display = (p === panelId && !isVisible) ? '' : 'none';
    });
    wrap.hidden = isVisible;
  }

  window.App = window.App || {};
  App.Workflow = App.Workflow || {};
  App.Workflow._closeSecondaryPanel = function() {
    const wrap = $('wfSecondaryPanels');
    if (wrap) wrap.hidden = true;
  };

  const _origReload = _reloadWorkflow;
  window._reloadWorkflow = async function() {
    await _origReload.apply(this, arguments);
    if (currentWorkflow) { loadVersions(); loadSchedules(); }
  };

  return {
    loadList, load, create, run, deleteWorkflow, renderDesigner, init,
    openNodePanel, closeNodePanel, saveNodeConfig, deleteNode, addNode, saveWorkflow,
    loadRunHistory, showRunDetail, closeRunPanel, switchRunTab,
    autoLayout, generateByDescription,
    saveVersion, loadVersions, rollbackVersion,
    exportWorkflow, importWorkflow,
    loadSchedules, renderSchedules, createSchedule, toggleSchedule, deleteSchedule,
    loadStats, renderStats,
    get current() { return currentWorkflow; }
  };
})();
