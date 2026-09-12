/* 项目选择器模块 - 支持按项目分类会话 */
(function () {
    window.__PROJECT_LOGS = window.__PROJECT_LOGS || [];
  var LOG = function () { 
    var args = Array.prototype.slice.call(arguments);
    window.__PROJECT_LOGS.push({ t: Date.now(), m: args.map(function(a){ try { return typeof a==='object'?JSON.stringify(a).substring(0,200):String(a) } catch(e){String(a)} }).join(' ') });
    if (window.__PROJECT_LOGS.length > 200) window.__PROJECT_LOGS.shift();
    console.log.apply(console, ['[Project]'].concat(args)); 
  };

  /* ====== per-session 项目存储 ======
   * 存储结构: localStorage.session_projects = {
   *   "desktop-abc123": { name, path, ts },
   *   "desktop-def456": { name, path, ts },
   *   ...
   * }
   * 同时保留 default_project 作为新建会话时的继承值(=上一个会话的项目)。
   */
  var LS_SESSION_MAP = 'session_projects';
  var LS_DEFAULT_KEY = 'default_project';
  var LS_OLD_KEY = 'current_project';

  function _readMap() {
    try {
      var raw = localStorage.getItem(LS_SESSION_MAP);
      if (raw) return JSON.parse(raw);
    } catch (e) { /* ignore */ }
    // 兼容旧版本: 把 current_project 迁移过来
    var old = null;
    try { old = localStorage.getItem(LS_OLD_KEY); } catch (e) {}
    if (old) {
      try {
        var map = {};
        var sessionId = App.sessionId || App.Storage && App.Storage.get('session_id') || 'default';
        map[sessionId] = JSON.parse(old);
        localStorage.setItem(LS_SESSION_MAP, JSON.stringify(map));
        localStorage.setItem(LS_DEFAULT_KEY, old);
        localStorage.removeItem(LS_OLD_KEY);
        return map;
      } catch (e) {}
    }
    return {};
  }

  function _writeMap(map) {
    try { localStorage.setItem(LS_SESSION_MAP, JSON.stringify(map)); } catch (e) {}
  }

  /* 读取某 session 的项目 */
  function _getProjectFor(sessionId) {
    var map = _readMap();
    return map[sessionId] || null;
  }

  /* 把项目绑定到某 session */
  function _setProjectFor(sessionId, project) {
    var map = _readMap();
    if (project) {
      map[sessionId] = { name: project.name, path: project.path, ts: Date.now() };
    } else {
      delete map[sessionId];
    }
    _writeMap(map);
    // 同时更新 default_project (新建会话继承用)
    if (project) {
      try { localStorage.setItem(LS_DEFAULT_KEY, JSON.stringify(project)); } catch (e) {}
    }
  }

  /* 对外 API: 切换当前 project 并同步到当前 session */
  function switchCurrentProject(project) {
    App.currentProject = project;
    if (App.sessionId) {
      _setProjectFor(App.sessionId, project);
    } else {
      // sessionId 还没设好, 先存默认值 (等 session.js 设好后会再次绑定)
      try { localStorage.setItem(LS_DEFAULT_KEY, JSON.stringify(project)); } catch (e) {}
    }
  }

  /* 对外 API: 根据 sessionId 加载对应项目 (切换会话时调用)
   * 严格 per-session: 仅从 session_projects[sessionId] 读取,
   * 不再继承 default_project —— 每个会话独立绑定项目。
   * default_project 仅在 _setProjectFor 中同步更新,用于启动时
   * 还没 sessionId 的兜底场景(loadCurrentProject)。
   */
  function loadForSession(sessionId) {
    var map = _readMap();
    var p = map[sessionId] || null;
    App.currentProject = p || null;
    updateProjectButton();
    console.log('[Project.loadForSession] sessionId=' + sessionId + ', 项目=' + (p ? p.name : '(无)'));
  }

  /* 删除某 session 的项目绑定 (删除会话时调用) */
  function clearForSession(sessionId) {
    var map = _readMap();
    delete map[sessionId];
    _writeMap(map);
  }

  App.currentProject = null;
  App.projectHistory = [];

  var projectBtn = null;
  var projectDropdown = null;
  var projectSearch = null;
  var projectList = null;

  // 请求序列号：只处理最新一次请求的返回，避免旧请求覆盖新数据
  var _reqSeq = 0;

  function init() {
    LOG('init start, backendUrl=', App.backendUrl);
    createProjectButton();
    // 根据 sessionId 加载绑定项目, sessionId 可能还没设好——稍后由 session.js 再次触发
    var sessionId = App.sessionId || App.Storage && App.Storage.get('session_id') || null;
    if (sessionId) {
      loadForSession(sessionId);
    } else {
      loadCurrentProject(); // fallback: 读 default_project
    }
    LOG('currentProject after load:', App.currentProject);
    waitBackendAndSync();
  }

  /* 等 backendUrl 设置好 + 后端真的能连通（ping /health），再同步历史 */
  function waitBackendAndSync() {
    var waited = 0;
    var timer = setInterval(function () {
      waited += 300;
      if (!App.backendUrl) return; // 还没设好地址，继续等

      // 地址有了，再 ping 一下后端真的能响应
      fetch(App.backendUrl + '/health', { signal: AbortSignal.timeout(1500) })
        .then(function (r) { return r.ok; })
        .then(function (ok) {
          if (ok) {
            clearInterval(timer);
            LOG('backend ready (after ' + waited + 'ms):', App.backendUrl);
            refreshHistory();
            syncCurrentToBackend();
          }
        })
        .catch(function () { /* 还没起来，继续等 */ });
    }, 300);
  }

  /* 让 localStorage 的 currentProject 也出现在后端历史里 */
  function syncCurrentToBackend() {
    if (!App.currentProject || !App.backendUrl) return;
    LOG('syncCurrentToBackend:', App.currentProject);
    App.apiJSON('/v1/project/switch', {
      method: 'POST',
      body: JSON.stringify({ path: App.currentProject.path }),
    }).catch(function () { /* 静默失败 */ });
  }

  // 创建项目选择按钮
  function createProjectButton() {
    // 兼容: 旧版容器叫 .composer-left, 现版本已改名为 .composer-selects。
    // 只认旧名会导致项目选择按钮永远创建不出来(静默 return), 故保留两者 fallback。
    var leftGroup = document.querySelector('.composer-left')
      || document.querySelector('.composer-selects');
    if (!leftGroup) { LOG('createProjectButton: 容器未找到(.composer-left / .composer-selects)!'); return; }

    projectBtn = document.createElement('button');
    projectBtn.className = 'project-selector-btn';
    projectBtn.id = 'projectSelectorBtn';
    projectBtn.title = '选择项目文件夹';
    projectBtn.innerHTML = '<span class="project-icon">📁</span><span class="project-name">选择项目</span>';

    projectDropdown = document.createElement('div');
    projectDropdown.className = 'project-dropdown';
    projectDropdown.id = 'projectDropdown';
    projectDropdown.hidden = true;

    projectSearch = document.createElement('input');
    projectSearch.className = 'project-search';
    projectSearch.placeholder = '搜索项目...';
    projectSearch.id = 'projectSearch';

    projectList = document.createElement('div');
    projectList.className = 'project-list';
    projectList.id = 'projectList';

    var empty = document.createElement('div');
    empty.className = 'project-empty';
    empty.textContent = '暂无项目历史';
    projectList.appendChild(empty);

    var browseBtn = document.createElement('button');
    browseBtn.className = 'project-browse-btn';
    browseBtn.textContent = '📂 选择文件夹...';
    browseBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      browseFolder();
    });

    projectDropdown.appendChild(projectSearch);
    projectDropdown.appendChild(projectList);
    projectDropdown.appendChild(browseBtn);

    // 插到 .composer-left 里，跟模型按钮并排靠左
    leftGroup.appendChild(projectBtn);
    document.body.appendChild(projectDropdown);

    projectBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      toggleDropdown();
    });
    projectSearch.addEventListener('input', filterProjects);
    document.addEventListener('click', function (e) {
      if (projectDropdown && !projectDropdown.contains(e.target) && e.target !== projectBtn) {
        projectDropdown.hidden = true;
      }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && projectDropdown && !projectDropdown.hidden) {
        projectDropdown.hidden = true;
      }
    });

    LOG('createProjectButton done, btn=', projectBtn ? 'OK' : 'NULL');
    App.projectSelector = { btn: projectBtn, dropdown: projectDropdown, search: projectSearch, list: projectList };
  }

  function positionDropdown() {
    if (!projectBtn || !projectDropdown) return;
    var r = projectBtn.getBoundingClientRect();
    var top = r.bottom + 4;
    var left = r.left;
    var w = projectDropdown.offsetWidth || 300;
    if (left + w > window.innerWidth - 12) {
      left = window.innerWidth - w - 12;
    }
    var h = projectDropdown.offsetHeight || 200;
    if (top + h > window.innerHeight - 12) {
      top = r.top - h - 4;
    }
    projectDropdown.style.top = top + 'px';
    projectDropdown.style.left = left + 'px';
  }

  function toggleDropdown() {
    LOG('toggleDropdown, hidden=', projectDropdown.hidden);
    projectDropdown.hidden = !projectDropdown.hidden;
    if (!projectDropdown.hidden) {
      positionDropdown();
      projectSearch.value = '';
      renderProjectList();
      projectSearch.focus();
    }
  }

  /* 带序列号的历史刷新：只接受最新一次请求的结果，避免竞态覆盖 */
  async function refreshHistory() {
    if (!App.backendUrl) { LOG('refreshHistory: no backendUrl'); return; }
    var mySeq = ++_reqSeq;
    try {
      var res = await App.apiJSON('/v1/project/history');
      // 如果在等待期间又发起了更新请求，这次结果过期，丢弃
      if (mySeq !== _reqSeq) {
        LOG('refreshHistory: seq ' + mySeq + ' superseded by ' + _reqSeq);
        return;
      }
      if (res.success && res.data && Array.isArray(res.data.projects)) {
        App.projectHistory = res.data.projects;
        LOG('refreshHistory OK: ' + App.projectHistory.length + ' projects');
      } else {
        LOG('refreshHistory: bad response', res);
      }
    } catch (e) {
      LOG('refreshHistory FAILED:', e);
    }
  }

  function loadCurrentProject() {
    try {
      // 优先读 default_project (新建会话的继承值)
      var saved = localStorage.getItem(LS_DEFAULT_KEY);
      LOG('loadCurrentProject: default_project=', saved);
      if (saved) {
        App.currentProject = JSON.parse(saved);
        updateProjectButton();
      }
    } catch (e) { LOG('loadCurrentProject FAILED:', e); }
  }

  async function renderProjectList(filter) {
    filter = filter || '';
    projectList.innerHTML = '';

    // 优先用 App.projectHistory（上一次成功的数据）
    // 如果还没从后端加载过历史（长度 0 且 backendUrl 已就绪），先刷新再渲染
    if (App.backendUrl && App.projectHistory.length === 0) {
      await refreshHistory();
    }

    // 兜底：后端空但 localStorage 有当前项目
    var source = App.projectHistory;
    if (source.length === 0 && App.currentProject) {
      LOG('renderProjectList: fallback to currentProject');
      source = [App.currentProject];
    }

    LOG('renderProjectList: source.length=', source.length);

    var filtered = filter
      ? source.filter(function (p) {
          return (p.path || '').toLowerCase().includes(filter.toLowerCase()) ||
                 (p.name || '').toLowerCase().includes(filter.toLowerCase());
        })
      : source;

    if (filtered.length === 0) {
      var empty = document.createElement('div');
      empty.className = 'project-empty';
      empty.textContent = App.backendUrl ? (filter ? '无匹配项目' : '暂无项目历史') : '后端连接中...';
      projectList.appendChild(empty);
      return;
    }

    for (var i = 0; i < filtered.length; i++) {
      projectList.appendChild(createProjectItem(filtered[i]));
    }
  }

  function createProjectItem(project) {
    var item = document.createElement('div');
    item.className = 'project-item';
    if (App.currentProject && App.currentProject.path === project.path) {
      item.classList.add('active');
    }

    var name = document.createElement('span');
    name.className = 'project-name';
    name.textContent = project.name || (project.path || '').split(/[\/\\]/).pop() || project.path;

    var path = document.createElement('span');
    path.className = 'project-path';
    path.textContent = project.path;

    item.appendChild(name);
    item.appendChild(path);

    item.addEventListener('click', function (e) {
      e.stopPropagation();
      selectProject(project);
    });
    item.addEventListener('contextmenu', function (e) {
      e.preventDefault();
      e.stopPropagation();
      showProjectContextMenu(e, project);
    });

    return item;
  }

  async function selectProject(project) {
    LOG('selectProject:', project);
    switchCurrentProject(project); // ✅ per-session 存储
    updateProjectButton();
    LOG('selectProject: button updated, btn.innerHTML=', projectBtn ? projectBtn.innerHTML.substring(0, 60) : 'NULL');
    projectDropdown.hidden = true;

    // 确保 backendUrl 就绪后再发请求（最多等 5s）
    var waited = 0;
    while (!App.backendUrl && waited < 5000) {
      await new Promise(function (r) { setTimeout(r, 100); });
      waited += 100;
    }
    if (!App.backendUrl) {
      LOG('selectProject: backendUrl 仍未就绪，跳过 API');
      return;
    }

    try {
      var res = await App.apiJSON('/v1/project/switch', {
        method: 'POST',
        body: JSON.stringify({ path: project.path }),
      });
      LOG('switch API result:', res);
      await refreshHistory();
    } catch (e) {
      LOG('switch FAILED:', e);
    }

    if (App.Session && App.Session.renderCards) {
      App.Session.renderCards();
    }
  }

  function updateProjectButton() {
    if (!projectBtn) { LOG('updateProjectButton: projectBtn is NULL!'); return; }
    var nameSpan = projectBtn.querySelector('.project-name');
    if (!nameSpan) { LOG('updateProjectButton: .project-name not found in btn! btn.innerHTML=', projectBtn.innerHTML); return; }
    if (App.currentProject) {
      var displayName = App.currentProject.name || App.currentProject.path.split(/[\/\\]/).pop();
      LOG('updateProjectButton: setting text to "' + displayName + '"');
      nameSpan.textContent = displayName;
      projectBtn.title = App.currentProject.path;
    } else {
      nameSpan.textContent = '选择项目';
      projectBtn.title = '选择项目文件夹';
    }
  }

  async function browseFolder() {
    try {
      // 优先使用 Electron 原生对话框
      if (window.agentApp && window.agentApp.openFolderDialog) {
        LOG('browseFolder: using Electron dialog');
        var res = await window.agentApp.openFolderDialog();
        LOG('browseFolder dialog result:', res);
        if (res.canceled) return;
        var path = res.path;
        if (!path) return;
        await selectProject({
          path: path,
          name: path.split(/[\/\\]/).pop() || path,
        });
        return;
      }
      LOG('browseFolder: agentApp/openFolderDialog not available, fallback to API');
      // 回退到 API（仅用于 Web 环境）
      var apiRes = await App.apiJSON('/v1/project/browse');
      if (apiRes.success && apiRes.data && apiRes.data.path) {
        await selectProject({
          path: apiRes.data.path,
          name: apiRes.data.name || apiRes.data.path.split(/[\/\\]/).pop(),
        });
        return;
      }
    } catch (e) {
      console.error('[Project] 浏览文件夹失败:', e);
    }
    // 最终回退到 prompt
    var path = prompt('请输入项目文件夹路径:', '');
    if (path) {
      await selectProject({ path: path, name: path.split(/[\/\\]/).pop() || path });
    }
  }

  async function removeProject(path) {
    if (!confirm('确定删除这个项目吗？')) return;
    try {
      var res = await App.apiJSON('/v1/project/remove', {
        method: 'POST',
        body: JSON.stringify({ path: path }),
      });
      if (res.success) {
        if (App.currentProject && App.currentProject.path === path) {
          App.currentProject = null;
          if (App.sessionId) _setProjectFor(App.sessionId, null); // ✅ per-session 清理
          updateProjectButton();
        }
        await refreshHistory();
        renderProjectList();
      }
    } catch (e) {
      console.error('[Project] 删除失败:', e);
    }
  }

  // 右键上下文菜单
  var _contextMenu = null;
  function showProjectContextMenu(e, project) {
    e.preventDefault();
    e.stopPropagation();
    // 移除旧菜单
    if (_contextMenu) {
      document.body.removeChild(_contextMenu);
      _contextMenu = null;
    }
    var menu = document.createElement('div');
    menu.className = 'project-context-menu';
    menu.style.position = 'fixed';
    menu.style.left = e.clientX + 'px';
    menu.style.top = e.clientY + 'px';
    menu.style.zIndex = '9999';

    var isActive = App.currentProject && App.currentProject.path === project.path;

    // 打开文件夹
    var openItem = document.createElement('div');
    openItem.className = 'project-context-item';
    openItem.textContent = '📂 打开文件夹';
    openItem.addEventListener('click', function () {
      if (window.agentApp && window.agentApp.revealFile) {
        window.agentApp.revealFile(project.path);
      }
      menu.remove();
    });
    menu.appendChild(openItem);

    // 复制路径
    var copyItem = document.createElement('div');
    copyItem.className = 'project-context-item';
    copyItem.textContent = '📋 复制路径';
    copyItem.addEventListener('click', function () {
      try {
        navigator.clipboard.writeText(project.path);
      } catch (e) { /* ignore */ }
      menu.remove();
    });
    menu.appendChild(copyItem);

    // 设为活跃项目
    var activateItem = document.createElement('div');
    activateItem.className = 'project-context-item' + (isActive ? ' active' : '');
    activateItem.textContent = isActive ? '✓ 当前项目' : '▶ 设为当前项目';
    activateItem.addEventListener('click', function () {
      if (!isActive) {
        selectProject(project);
      }
      menu.remove();
    });
    menu.appendChild(activateItem);

    // 删除
    var deleteItem = document.createElement('div');
    deleteItem.className = 'project-context-item delete';
    deleteItem.textContent = '🗑 从历史删除';
    deleteItem.addEventListener('click', function () {
      menu.remove();
      removeProject(project.path);
    });
    menu.appendChild(deleteItem);

    document.body.appendChild(menu);
    _contextMenu = menu;

    // 点击外部关闭
    var closeMenu = function (ev) {
      if (!menu.contains(ev.target)) {
        menu.remove();
        _contextMenu = null;
        document.removeEventListener('click', closeMenu);
      }
    };
    setTimeout(function () {
      document.addEventListener('click', closeMenu);
    }, 0);
  }

  function filterProjects() {
    clearTimeout(filterProjects._timer);
    filterProjects._timer = setTimeout(function () {
      renderProjectList(projectSearch.value);
    }, 200);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.App = window.App || {};
  App.Project = {
    init: init,
    selectProject: selectProject,
    refreshHistory: refreshHistory,
    getCurrent: function () { return App.currentProject; },
    browseFolder: browseFolder,
    // per-session API, 由 session.js 调用
    switchToSession: loadForSession,
    clearForSession: clearForSession,
  };
})();
