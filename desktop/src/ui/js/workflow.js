/**
 * 工作流 UI 模块 - 桌面端集成
 */
const App = window.App || {};

// 自定义 prompt 对话框（替代原生 prompt）
App.prompt = function(message, defaultValue) {
  return new Promise((resolve) => {
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
    
    function done(val) {
      mask.remove();
      resolve(val);
    }
    
    cancel.addEventListener('click', () => done(null));
    ok.addEventListener('click', () => done(input.value));
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') done(input.value);
      if (e.key === 'Escape') done(null);
    });
    mask.addEventListener('click', (e) => { if (e.target === mask) done(null); });
  });
};

App.Workflow = (() => {
  const API = 'http://127.0.0.1:8517';
  let currentWorkflow = null;
  let nodes = [];
  let edges = [];
  let selectedNode = null;
  let svgEl = null;
  let nodeElements = new Map();
  let svgWidth = 800;
  let svgHeight = 400;

  function $(id) { return document.getElementById(id); }

  async function loadList() {
    try {
      const resp = await fetch(`${API}/v1/workflows`);
      const data = await resp.json();
      renderList(data.workflows || []);
    } catch (e) {
      console.error('加载工作流列表失败:', e);
    }
  }

  function renderList(workflows) {
    const list = $('wfList');
    if (!list) return;
    
    if (!workflows || workflows.length === 0) {
      list.innerHTML = '<div class="wf-empty">暂无工作流，点击"新建"创建</div>';
      return;
    }

    list.innerHTML = workflows.map(wf => `
      <div class="wf-item ${currentWorkflow?.id === wf.id ? 'active' : ''}" data-id="${wf.id}">
        <div class="wf-item-header">
          <span class="wf-item-name">${wf.name || '未命名'}</span>
          <span class="wf-item-meta">${wf.nodes_count || 0} 节点 · ${wf.edges_count || 0} 边</span>
        </div>
        <div class="wf-item-actions">
          <button class="mini-btn" onclick="event.stopPropagation(); App.Workflow.load('${wf.id}')">编辑</button>
          <button class="mini-btn" onclick="event.stopPropagation(); App.Workflow.run('${wf.id}')">运行</button>
          <button class="mini-btn danger" onclick="event.stopPropagation(); App.Workflow.deleteWorkflow('${wf.id}')">删除</button>
        </div>
      </div>
    `).join('');
  }

  async function load(id) {
    try {
      const resp = await fetch(`${API}/v1/workflows/${id}`);
      const data = await resp.json();
      
      currentWorkflow = data;
      nodes = data.nodes || [];
      edges = data.edges || [];
      
      renderDesigner(currentWorkflow);
      $('wfToolbar').style.display = 'flex';
      
      $('wfList').querySelectorAll('.wf-item').forEach(el => {
        el.classList.toggle('active', el.dataset.id === id);
      });
    } catch (e) {
      console.error('加载工作流失败:', e);
      App.toast('加载工作流失败: ' + e.message, 'err');
    }
  }

  async function create() {
    const name = await App.prompt('请输入工作流名称:', '新工作流');
    if (!name || name.trim() === '') return;
    
    try {
      const resp = await fetch(`${API}/v1/workflows`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: name.trim(),
          description: '',
          nodes: [
            { id: 'start_1', type: 'start', name: '开始' },
            { id: 'end_1', type: 'end', name: '结束' }
          ],
          edges: [{ source: 'start_1', target: 'end_1' }],
          entry_node: 'start_1'
        })
      });
      
      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail || '创建失败');
      }
      
      const data = await resp.json();
      App.toast('工作流创建成功', 'ok');
      
      await loadList();
      const newId = data.id || data.workflow?.id;
      if (newId) {
        setTimeout(() => load(newId), 100);
      }
    } catch (e) {
      App.toast('创建失败: ' + e.message, 'err');
    }
  }

  async function run(id) {
    if (!id) id = currentWorkflow?.id;
    if (!id) { App.toast('请先选择一个工作流', 'err'); return; }
    
    try {
      const resp = await fetch(`${API}/v1/workflows/${id}/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ input: '测试输入' })
      });
      
      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail || '运行失败');
      }
      
      const data = await resp.json();
      App.toast('工作流运行成功', 'ok');
    } catch (e) {
      App.toast('运行失败: ' + e.message, 'err');
    }
  }

  async function deleteWorkflow(id) {
    if (!id) id = currentWorkflow?.id;
    if (!id) { App.toast('请先选择一个工作流', 'err'); return; }
    
    if (!await App.confirm('确定删除此工作流？')) return;
    
    try {
      const resp = await fetch(`${API}/v1/workflows/${id}`, { method: 'DELETE' });
      
      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail || '删除失败');
      }
      
      App.toast('工作流已删除', 'ok');
      currentWorkflow = null;
      nodes = [];
      edges = [];
      $('wfToolbar').style.display = 'none';
      loadList();
    } catch (e) {
      App.toast('删除失败: ' + e.message, 'err');
    }
  }

  function renderDesigner(wf) {
    const container = $('wfDesigner');
    if (!container) return;
    
    // 清空容器，但保留 SVG 元素
    container.innerHTML = '';
    
    // 创建或获取 SVG
    let svg = $('wfSvg');
    if (!svg) {
      svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.id = 'wfSvg';
      svg.className = 'wf-svg';
      container.appendChild(svg);
    }
    
    // 设置 SVG 尺寸
    const rect = container.getBoundingClientRect();
    svgWidth = rect.width || 800;
    svgHeight = rect.height || 400;
    svg.setAttribute('width', svgWidth);
    svg.setAttribute('height', svgHeight);
    svg.style.width = '100%';
    svg.style.height = '100%';
    
    // 清空现有内容
    svg.innerHTML = '';
    
    // 添加 defs
    const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
    defs.innerHTML = `
      <marker id="arrowhead" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">
        <polygon points="0 0, 10 3.5, 0 7" fill="var(--accent)" />
      </marker>
    `;
    svg.appendChild(defs);
    
    // 计算布局
    const nodeMap = new Map();
    (wf.nodes || []).forEach((node, idx) => {
      nodeMap.set(node.id, node);
    });
    
    // 计算网格布局
    const nodeCount = wf.nodes?.length || 0;
    const cols = nodeCount > 3 ? Math.ceil(Math.sqrt(nodeCount)) : 2;
    const nodeWidth = 100;
    const nodeHeight = 50;
    const padding = 40;
    const colWidth = nodeWidth + padding * 2;
    const rowHeight = nodeHeight + padding * 2;
    
    // 根据节点数量调整 SVG 高度
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
      
      // 节点背景
      const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      rect.setAttribute('width', nodeWidth);
      rect.setAttribute('height', nodeHeight);
      rect.setAttribute('rx', '8');
      rect.setAttribute('class', 'node-rect');
      
      // 节点文字
      const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      text.setAttribute('x', nodeWidth / 2);
      text.setAttribute('y', nodeHeight / 2);
      text.setAttribute('class', 'node-text');
      text.textContent = node.name || node.type || '节点';
      
      g.appendChild(rect);
      g.appendChild(text);
      svg.appendChild(g);
      nodeElements.set(node.id, g);
      
      // 拖拽事件
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
    
    // 绘制边
    function renderEdges() {
      // 清除旧边
      svg.querySelectorAll('.edge-line').forEach(el => el.remove());
      
      (wf.edges || []).forEach(edge => {
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
    
    renderEdges();
  }

  function init() {
    const newBtn = $('wfNewBtn');
    const refreshBtn = $('wfRefreshBtn');
    
    if (newBtn) {
      newBtn.addEventListener('click', create);
    }
    if (refreshBtn) {
      refreshBtn.addEventListener('click', loadList);
    }
    
    // 绑定设计器按钮
    const runBtn = $('wfRunBtn');
    const deleteBtn = $('wfDeleteBtn');
    if (runBtn) {
      runBtn.addEventListener('click', () => run(currentWorkflow?.id));
    }
    if (deleteBtn) {
      deleteBtn.addEventListener('click', () => deleteWorkflow(currentWorkflow?.id));
    }
    
    loadList();
  }

  return {
    loadList,
    load,
    create,
    run,
    deleteWorkflow,
    renderDesigner,
    init
  };
})();
