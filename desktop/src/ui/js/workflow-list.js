/* 工作流列表模块(历史兼容层)
   修复"工作流不可用": 旧实现有两个致命问题:
   1) 顶部 const API = App.backendUrl —— 脚本加载时 App.backendUrl 尚未由
      renderer.js init() 赋值 → API 拿到 undefined fallback, 请求 404。
   2) 本脚本之后加载的 workflow.js 会整体覆盖 App.Workflow,
      使本模块的 loadList/load 成死代码。
   现改为: 仅在 App.Workflow.loadList 缺失时补最小实现(使用 App.api 带鉴权);
   workflow.js(终极版)注册后自然接管, 不再冲突。
   注意: 不使用内联 onclick, 全部通过事件委托绑定, 避免 CSP 阻止。 */
(() => {
  window.App = window.App || {};

  /* 延迟到 DOM 就绪且 App.backendUrl 已初始化后再判断是否补实现 */
  function ensureWorkflowApi() {
    // 如果 workflow.js 已经注册了 loadList, 直接返回
    if (App.Workflow && App.Workflow.loadList) return;
    
    App.Workflow = App.Workflow || {};
    
    App.Workflow.loadList = async function () {
      // 确保 backendUrl 已设置
      if (!App.backendUrl) {
        console.warn('[workflow-list] backendUrl 尚未初始化, 等待...');
        setTimeout(() => App.Workflow.loadList && App.Workflow.loadList(), 500);
        return;
      }
      try {
        const res = await App.apiJSON('/v1/workflows');
        if (!res.success) { console.error('加载工作流失败:', res.detail); return; }
        const workflows = res.data?.workflows || [];
        const listEl = document.getElementById('workflowList') || document.getElementById('wfList');
        if (!listEl) return;
        if (!workflows.length) {
          listEl.innerHTML = '<div class="wf-empty">暂无工作流, 点击"新建工作流"创建</div>';
          return;
        }
        // 不使用内联 onclick, 改用 data-* 属性 + 事件委托
        listEl.innerHTML = workflows.map(wf => `
          <div class="wf-item" data-id="${wf.id}">
            <div class="wf-item-header">
              <span class="wf-item-name">${wf.name || '未命名'}</span>
              <span class="wf-item-meta">${wf.nodes_count || 0} 节点</span>
            </div>
            <div class="wf-item-actions">
              <button class="mini-btn wf-list-edit-btn" data-id="${wf.id}">编辑</button>
            </div>
          </div>`).join('');
        
        // 事件委托绑定（避免内联 onclick 被 CSP 阻止）
        listEl.addEventListener('click', e => {
          const btn = e.target.closest('.wf-list-edit-btn');
          if (btn && App.Workflow.load) {
            e.stopPropagation();
            App.Workflow.load(btn.dataset.id);
          }
        });
      } catch (e) { 
        console.error('加载工作流列表失败:', e); 
      }
    };
    
    // 延迟执行, 等待 App.backendUrl 初始化
    if (App.backendUrl) {
      App.Workflow.loadList();
    } else {
      const checkUrl = () => {
        if (App.backendUrl) {
          App.Workflow.loadList();
        } else {
          setTimeout(checkUrl, 500);
        }
      };
      checkUrl();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', ensureWorkflowApi);
  } else {
    setTimeout(ensureWorkflowApi, 0);
  }
})();
