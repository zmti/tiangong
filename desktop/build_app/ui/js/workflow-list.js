/* 工作流列表模块 */
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
    const listEl = App.els.wfList || document.getElementById('wfList');
    if (!listEl) return;
    
    if (!workflows || workflows.length === 0) {
      listEl.innerHTML = '<div class="wf-empty">暂无工作流，点击"新建"创建</div>';
      return;
    }

    listEl.innerHTML = workflows.map(wf => `
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
      const toolbar = App.els.wfToolbar || document.getElementById('wfToolbar');
      if (toolbar) toolbar.style.display = 'flex';
      
      const listEl = App.els.wfList || document.getElementById('wfList');
      if (listEl) {
        listEl.querySelectorAll('.wf-item').forEach(el => {
          el.classList.toggle('active', el.dataset.id === id);
        });
      }
    } catch (e) {
      console.error('加载工作流失败:', e);
      App.toast?.('加载工作流失败: ' + e.message, 'err');
    }
  }

  return { loadList, load, get current() { return currentWorkflow; }, get nodes() { return nodes; }, get edges() { return edges; } };
})();
