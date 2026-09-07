/* 工作流设计器模块 */
App.WorkflowDesigner = (() => {
  const API = 'http://127.0.0.1:8517';
  let currentWorkflow = null;
  let nodes = [];
  let edges = [];
  let nodeElements = new Map();
  let svgWidth = 800;
  let svgHeight = 400;
  const nodeWidth = 100;
  const nodeHeight = 50;
  const padding = 40;

  function renderDesigner(wf) {
    const container = App.els.wfDesigner || document.getElementById('wfDesigner');
    if (!container) return;
    
    container.innerHTML = '';
    
    let svg = App.els.wfSvg || document.getElementById('wfSvg');
    if (!svg) {
      svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.id = 'wfSvg';
      svg.className = 'wf-svg';
      container.appendChild(svg);
    }
    
    const rect = container.getBoundingClientRect();
    svgWidth = rect.width || 800;
    svgHeight = rect.height || 400;
    svg.setAttribute('width', svgWidth);
    svg.setAttribute('height', svgHeight);
    svg.style.width = '100%';
    svg.style.height = '100%';
    
    svg.innerHTML = '';
    
    const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
    defs.innerHTML = `
      <marker id="arrowhead" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">
        <polygon points="0 0, 10 3.5, 0 7" fill="var(--accent)" />
      </marker>
    `;
    svg.appendChild(defs);
    
    const nodeMap = new Map();
    (wf.nodes || []).forEach((node, idx) => {
      nodeMap.set(node.id, node);
    });
    
    const nodeCount = wf.nodes?.length || 0;
    const cols = nodeCount > 3 ? Math.ceil(Math.sqrt(nodeCount)) : 2;
    const colWidth = nodeWidth + padding * 2;
    const rowHeight = nodeHeight + padding * 2;
    
    const rows = Math.ceil(nodeCount / cols);
    const neededHeight = padding * 2 + rows * rowHeight;
    svgHeight = Math.max(neededHeight, 300);
    svg.setAttribute('height', svgHeight);
    
    (wf.nodes || []).forEach((node, idx) => {
      const col = idx % cols;
      const row = Math.floor(idx / cols);
      node._x = padding + col * colWidth;
      node._y = padding + row * rowHeight;
      
      const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      g.setAttribute('transform', `translate(${node._x}, ${node._y})`);
      g.style.cursor = 'move';
      g.dataset.nodeId = node.id;
      
      const rectEl = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      rectEl.setAttribute('width', nodeWidth);
      rectEl.setAttribute('height', nodeHeight);
      rectEl.setAttribute('rx', '8');
      rectEl.setAttribute('class', 'node-rect');
      
      const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      text.setAttribute('x', nodeWidth / 2);
      text.setAttribute('y', nodeHeight / 2);
      text.setAttribute('class', 'node-text');
      text.textContent = node.name || node.type || '节点';
      
      g.appendChild(rectEl);
      g.appendChild(text);
      svg.appendChild(g);
      nodeElements.set(node.id, g);
      
      let isDragging = false;
      let startMouseX, startMouseY, startX, startY;
      
      g.addEventListener('mousedown', (e) => {
        isDragging = true;
        startMouseX = e.clientX;
        startMouseY = e.clientY;
        startX = node._x;
        startY = node._y;
        e.preventDefault();
        e.stopPropagation();
      });
      
      document.addEventListener('mousemove', (e) => {
        if (!isDragging) return;
        const dx = e.clientX - startMouseX;
        const dy = e.clientY - startMouseY;
        node._x = Math.max(0, startX + dx);
        node._y = Math.max(0, startY + dy);
        g.setAttribute('transform', `translate(${node._x}, ${node._y})`);
        renderEdges();
      });
      
      document.addEventListener('mouseup', () => {
        isDragging = false;
      });
    });
    
    renderEdges();
  }

  function renderEdges() {
    const svg = App.els.wfSvg || document.getElementById('wfSvg');
    if (!svg) return;
    
    svg.querySelectorAll('.edge-line').forEach(el => el.remove());
    
    const nodeMap = new Map();
    (currentWorkflow?.nodes || []).forEach((node, idx) => {
      nodeMap.set(node.id, node);
    });
    
    (currentWorkflow?.edges || []).forEach(edge => {
      const sourceNode = nodeMap.get(edge.source);
      const targetNode = nodeMap.get(edge.target);
      if (!sourceNode || !targetNode) return;
      
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('class', 'edge-line');
      line.setAttribute('x1', sourceNode._x + nodeWidth);
      line.setAttribute('y1', sourceNode._y + nodeHeight / 2);
      line.setAttribute('x2', targetNode._x);
      line.setAttribute('y2', targetNode._y + nodeHeight / 2);
      line.setAttribute('marker-end', 'url(#arrowhead)');
      svg.appendChild(line);
    });
  }

  return { renderDesigner, renderEdges };
})();
