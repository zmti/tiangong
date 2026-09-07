/* 工作流操作模块 */
App.WorkflowOps = (() => {
  const API = 'http://127.0.0.1:8517';
  let currentWorkflow = null;
  let nodes = [];
  let edges = [];

  async function create() {
    const name = await App.prompt?.('请输入工作流名称:', '新工作流') || '新工作流';
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
      App.toast?.('工作流创建成功', 'ok');
      
      await App.Workflow.loadList();
      const newId = data.id || data.workflow?.id;
      if (newId) {
        setTimeout(() => App.Workflow.load(newId), 100);
      }
    } catch (e) {
      App.toast?.('创建失败: ' + e.message, 'err');
    }
  }

  async function run(id) {
    if (!id) id = currentWorkflow?.id;
    if (!id) { App.toast?.('请先选择一个工作流', 'err'); return; }
    
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
      App.toast?.('工作流运行成功', 'ok');
    } catch (e) {
      App.toast?.('运行失败: ' + e.message, 'err');
    }
  }

  async function deleteWorkflow(id) {
    if (!id) id = currentWorkflow?.id;
    if (!id) { App.toast?.('请先选择一个工作流', 'err'); return; }
    
    const confirmed = await App.confirm?.('确定删除此工作流？') || false;
    if (!confirmed) return;
    
    try {
      const resp = await fetch(`${API}/v1/workflows/${id}`, { method: 'DELETE' });
      
      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail || '删除失败');
      }
      
      App.toast?.('工作流已删除', 'ok');
      currentWorkflow = null;
      nodes = [];
      edges = [];
      const toolbar = App.els.wfToolbar || document.getElementById('wfToolbar');
      if (toolbar) toolbar.style.display = 'none';
      await App.Workflow.loadList();
    } catch (e) {
      App.toast?.('删除失败: ' + e.message, 'err');
    }
  }

  function init() {
    const newBtn = App.els.wfNewBtn || document.getElementById('wfNewBtn');
    const refreshBtn = App.els.wfRefreshBtn || document.getElementById('wfRefreshBtn');
    
    if (newBtn) {
      newBtn.addEventListener('click', create);
    }
    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => App.Workflow.loadList());
    }
    
    const runBtn = App.els.wfRunBtn || document.getElementById('wfRunBtn');
    const deleteBtn = App.els.wfDeleteBtn || document.getElementById('wfDeleteBtn');
    if (runBtn) {
      runBtn.addEventListener('click', () => run(currentWorkflow?.id));
    }
    if (deleteBtn) {
      deleteBtn.addEventListener('click', () => deleteWorkflow(currentWorkflow?.id));
    }
    
    App.Workflow.loadList();
  }

  return { create, run, deleteWorkflow, init };
})();
