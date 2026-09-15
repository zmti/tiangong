/* projects.js — Project management tab */
(function () {
  'use strict';

  const $ = (sel, ctx) => (ctx || document).querySelector(sel);
  /* 修复"项目管理不可用": 旧实现调 GET /v1/projects + 裸 fetch 不带鉴权,
     但后端 project.py 路由前缀是 /v1/project(单数), 列表端点是 /v1/project/list,
     返回 {"projects":[...]} 而非裸数组 → 旧代码 404/解析失败 → 永远空态。
     现改走 App.apiJSON(带 Authorization) + 正确端点 + 正确字段。 */
  async function loadProjects() {
    try {
      const res = window.App?.apiJSON ? await window.App.apiJSON('/v1/project/list') : null;
      let projects = [];
      if (res && res.success) {
        projects = res.data?.projects || [];
      } else {
        /* 后端可能未启动该路由, 回退到旧 /v1/projects(兼容) */
        const resp = await window.App?.api?.('/v1/projects');
        if (resp && resp.ok) {
          const data = await resp.json();
          projects = Array.isArray(data) ? data : (data.projects || []);
        }
      }
      renderProjects(projects);
    } catch (e) {
      console.error('[projects] Failed to load:', e);
      showEmptyState();
    }
  }

  function showEmptyState() {
    const grid = $('#projectGrid');
    if (!grid) return;
    grid.innerHTML = `
      <div style="grid-column:1/-1;padding:48px;text-align:center;color:var(--muted);">
        <div style="font-size:48px;margin-bottom:16px;">📁</div>
        <div style="font-size:14px;font-weight:500;color:var(--text);margin-bottom:8px;">暂无项目</div>
        <div style="font-size:12px;">点击上方"新建项目"按钮创建第一个项目</div>
      </div>
    `;
  }

  function renderProjects(projects) {
    const grid = $('#projectGrid');
    if (!grid) return;

    grid.innerHTML = '';
    if (!projects || projects.length === 0) {
      showEmptyState();
      return;
    }

    projects.forEach(p => {
      const card = document.createElement('div');
      card.className = 'project-card';
      card.innerHTML = `
        <div class="pj-name">${escapeHtml(p.name || '未命名项目')}</div>
        <div class="pj-desc">${escapeHtml(p.description || '暂无描述')}</div>
        <div class="pj-meta">
          <span>${formatTime(p.updated_at || p.created_at)}</span>
          <span>${p.session_count || 0} 个会话</span>
        </div>
      `;
      card.addEventListener('click', () => {
        if (window.App?.Project?.select) {
          window.App.Project.select(p.id);
        }
      });
      grid.appendChild(card);
    });
  }

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  function formatTime(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    return d.toLocaleDateString('zh-CN');
  }

  // Auto-refresh when projects tab is shown
  const observer = new MutationObserver(() => {
    const tab = document.querySelector('#tabProjects');
    if (tab && tab.classList.contains('active')) {
      loadProjects();
    }
  });

  document.addEventListener('DOMContentLoaded', () => {
    const tab = document.querySelector('#tabProjects');
    if (tab) observer.observe(tab, { attributes: true, attributeFilter: ['class'] });

    const refreshBtn = $('#pjRefreshBtn');
    if (refreshBtn) refreshBtn.addEventListener('click', loadProjects);

    const newBtn = $('#pjNewBtn');
    if (newBtn) {
      newBtn.addEventListener('click', () => {
        const name = prompt('请输入项目名称:');
        if (!name) return;
        createProject(name);
      });
    }
  });

  async function createProject(name) {
    try {
      /* 修复: 后端是 /v1/project/switch(单数), 旧调 /v1/projects POST 404 */
      const resp = await window.App?.api?.('/v1/project/switch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: name })
      });
      if (resp && resp.ok) {
        const data = await resp.json();
        if (data.ok) {
          loadProjects();
          if (window.App?.toast) window.App.toast('项目创建成功', 'ok');
        } else {
          if (window.App?.toast) window.App.toast(data.error || '创建失败', 'err');
        }
      } else {
        /* 回退旧端点 */
        const r2 = await window.App?.api?.('/v1/projects', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name })
        });
        if (r2 && r2.ok) {
          loadProjects();
          if (window.App?.toast) window.App.toast('项目创建成功', 'ok');
        }
      }
    } catch (e) {
      console.error('[projects] Failed to create:', e);
      if (window.App?.toast) window.App.toast('创建失败', 'err');
    }
  }

  window.App = window.App || {};
  window.App.Projects = { load: loadProjects };
})();
