/* 工作流操作模块 */
App.WorkflowOps = (() => {
  const API = 'http://127.0.0.1:8517';
  let currentWorkflow = null;
  let nodes = [];
  let edges = [];

  /* 修复"新建/刷新点一次弹两次": workflow-ops.js 与 workflow.js 都给
     wfNewBtn/wfRefreshBtn 绑定了监听器, 且 ops 用硬编码 8517 端口(用户改
     AGENT_PORT 后 404)。现 ops 不再自行 init 绑定, 按钮统一由 workflow.js
     (终极版, 用 App.backendUrl 动态端口) 的 init() 绑定。保留 create/run/
     deleteWorkflow 供 workflow.js 委托, 但端口改用 App.backendUrl。 */
  function _apiBase() { return App.backendUrl || 'http://127.0.0.1:8517'; }

  async function create() {
    const name = await App.prompt?.('请输入工作流名称:', '新工作流') || '新工作流';
    if (!name || name.trim() === '') return;

    try {
      const resp = await fetch(`${_apiBase()}/v1/workflows`, {
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
      const resp = await fetch(`${_apiBase()}/v1/workflows/${id}/run`, {
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
    /* 修复"新建/刷新点一次弹两次": 旧实现 ops + workflow.js 双重绑定按钮。
       现 ops 不再绑定 wfNewBtn/wfRefreshBtn/wfRunBtn/wfDeleteBtn,
       统一交给 workflow.js 的 init()(它用 App.backendUrl 动态端口)。
       保留本 init 仅为兼容旧调用方, 实际不再挂监听器。 */
    // 按钮绑定已全部迁移到 workflow.js(终极版), 此处不重复绑定
    if (!App._wfOpsInitDone) {
      App._wfOpsInitDone = true;
    }
    if (App.Workflow && App.Workflow.loadList) App.Workflow.loadList();
  }

  return { create, run, deleteWorkflow, init };
})();
