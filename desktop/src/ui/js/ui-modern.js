/**
 * 现代 UI 交互模块
 * 功能: 导航管理、面板切换、通知系统、模态框
 */

App.UI = App.UI || {};

// ========== 导航管理 ==========
App.UI.Nav = {
  currentView: 'chat',
  
  init: function() {
    this.bindEvents();
    this.updateActive();
  },
  
  bindEvents: function() {
    /* 修复"左侧栏点击错乱/白屏": 移除 .nav-btn → navigate 绑定。
       旧实现把 kb/memory/todo/tools/prompts/schedule 当 tabContent(找 tabKb 等
       不存在的 id), 且 navigate 无脑隐藏 chatView + 所有 .tab-content →
       点左侧后主区空白。左侧导航统一交给 renderer.js 的 showView(panelMap)
       (开右面板 + 保持聊天视图)。 */

    // 侧边栏折叠/展开
    const sidebarToggle = document.getElementById('sidebarToggle');
    if (sidebarToggle) {
      sidebarToggle.addEventListener('click', () => {
        document.body.classList.toggle('sidebar-collapsed');
        if (App.Session) App.Session._collapsed = document.body.classList.contains('sidebar-collapsed');
      });
    }

    // 新建会话
    const newSessionBtn = document.getElementById('newSessionBtn');
    if (newSessionBtn && App.Session) {
      newSessionBtn.addEventListener('click', App.Session.newSession);
    }
  },
  
  navigate: function(view) {
    /* 修复白屏 + 让 workflow/projects/briefing 可达: 仅当目标在 .tab-content 中
       真实存在时才切换 tab 并隐藏 chatView; kb/memory/todo 等无对应 tabContent
       (它们是右面板 side-content, 由 renderer.showView 处理), 此处直接返回不动主区 */
    const targetView = document.getElementById('tab' + view.charAt(0).toUpperCase() + view.slice(1));
    if (!targetView && view !== 'chat') return;
    // 隐藏所有 tab-content
    document.querySelectorAll('.tab-content').forEach(el => {
      el.classList.remove('active');
      el.style.display = 'none';
    });
    // 聊天视图特殊处理
    const chatView = document.getElementById('chatView');
    if (view === 'chat') {
      if (chatView) { chatView.style.display = 'flex'; chatView.classList.add('active'); }
      document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.view === view);
      });
      this.currentView = view;
      window.dispatchEvent(new CustomEvent('viewChange', { detail: { view } }));
      return;
    }
    if (chatView) chatView.style.display = 'none';
    // 显示目标视图
    if (targetView) {
      targetView.classList.add('active');
      targetView.style.display = 'flex';
      /* 修复"切到工作流/简报/项目后内容不刷新": 触发对应模块加载 */
      if (view === 'workflow' && App.Workflow && App.Workflow.loadList) App.Workflow.loadList();
      if (view === 'projects' && App.Projects && App.Projects.load) App.Projects.load();
      if (view === 'briefing' && App.Briefing && App.Briefing.load) App.Briefing.load();
    }

    // 更新导航状态
    document.querySelectorAll('.nav-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.view === view);
    });

    this.currentView = view;

    // 触发视图切换事件
    window.dispatchEvent(new CustomEvent('viewChange', { detail: { view } }));
  },
  
  updateActive: function() {
    this.navigate(this.currentView || 'chat');
  }
};

// ========== 通知系统 ==========
App.UI.Toast = {
  container: null,
  
  init: function() {
    this.container = document.createElement('div');
    this.container.className = 'toast-wrap';
    document.body.appendChild(this.container);
  },
  
  show: function(message, type = 'info', duration = 3000) {
    if (!this.container) {
      this.init();
    }
    
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    
    this.container.appendChild(toast);
    
    // 触发动画
    requestAnimationFrame(() => {
      toast.classList.add('show');
    });
    
    // 自动移除
    setTimeout(() => {
      toast.classList.remove('show');
      setTimeout(() => toast.remove(), 200);
    }, duration);
  },
  
  success: function(message) {
    this.show(message, 'ok');
  },
  
  error: function(message) {
    this.show(message, 'err');
  },
  
  warning: function(message) {
    this.show(message, 'warn');
  },
  
  info: function(message) {
    this.show(message, 'info');
  }
};

// ========== 模态框系统 ==========
App.UI.Modal = {
  instances: [],
  
  open: function(config) {
    const overlay = document.createElement('div');
    overlay.className = 'modal-mask';
    overlay.innerHTML = `
      <div class="confirm-dialog${config.wide ? ' wide' : ''}">
        <div class="cd-body"></div>
        ${config.actions ? `
          <div class="cd-actions">
            ${config.actions}
          </div>
        ` : ''}
      </div>
    `;

    const cdBody = overlay.querySelector('.cd-body');
    if (config.content instanceof Node) cdBody.appendChild(config.content);
    else cdBody.innerHTML = config.content || '';

    document.body.appendChild(overlay);
    this.instances.push(overlay);
    
    // 点击遮罩关闭
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) {
        this.close(overlay);
      }
    });
    
    // ESC 关闭
    const escHandler = (e) => {
      if (e.key === 'Escape') {
        this.close(overlay);
        document.removeEventListener('keydown', escHandler);
      }
    };
    document.addEventListener('keydown', escHandler);
    
    return new Promise((resolve) => {
      overlay._resolve = resolve;
    });
  },
  
  close: function(overlay) {
    if (!overlay || !this.instances.includes(overlay)) return;
    
    overlay.style.opacity = '0';
    setTimeout(() => {
      overlay.remove();
      this.instances = this.instances.filter(i => i !== overlay);
      if (overlay._resolve) overlay._resolve(false);
    }, 150);
  },
  
  confirm: function(message, options = {}) {
    const {
      okText = '确定',
      cancelText = '取消',
      okType = 'primary'
    } = options;
    
    const actions = `
      <button class="cd-cancel" onclick="App.UI.Modal.close(this.closest('.modal-mask'))">${cancelText}</button>
      <button class="cd-ok ${okType}" id="modalConfirmBtn">${okText}</button>
    `;
    
    return this.open({ content: message, actions }).then(() => {
      return new Promise((resolve) => {
        const btn = document.getElementById('modalConfirmBtn');
        if (btn) {
          btn.addEventListener('click', () => {
            this.close(btn.closest('.modal-mask'));
            resolve(true);
          }, { once: true });
        }
      });
    });
  }
};

// ========== 面板管理 ==========
App.UI.Panel = {
  panels: {},
  
  register: function(name, config) {
    this.panels[name] = {
      element: null,
      config: config,
      isOpen: false
    };
  },
  
  open: function(name) {
    const panel = this.panels[name];
    if (!panel || panel.isOpen) return;
    
    const el = document.getElementById(name + 'Panel');
    if (el) {
      el.classList.add('active');
      panel.isOpen = true;
      panel.element = el;
    }
  },
  
  close: function(name) {
    const panel = this.panels[name];
    if (!panel || !panel.isOpen) return;
    
    const el = document.getElementById(name + 'Panel');
    if (el) {
      el.classList.remove('active');
      panel.isOpen = false;
    }
  },
  
  toggle: function(name) {
    const panel = this.panels[name];
    if (!panel) return;
    
    if (panel.isOpen) {
      this.close(name);
    } else {
      this.open(name);
    }
  }
};

// ========== 回到顶部按钮 ==========
// 已在 renderer.js 中实现，避免重复的 scroll 事件监听器
App.UI.BackToBottom = {
  init: function() {
    // 空函数，实际实现在 renderer.js 中
  }
};

// ========== 消息操作增强 ==========
App.UI.MessageActions = {
  init: function() {
    // 事件委托处理消息操作
    document.addEventListener('click', (e) => {
      const msg = e.target.closest('.msg');
      if (!msg) return;
      
      // 复制按钮
      if (e.target.closest('[data-action="copy"]')) {
        const content = msg.querySelector('.answer-doc');
        if (content) {
          const text = content.textContent || '';
          navigator.clipboard.writeText(text).then(() => {
            App.UI.Toast.success('已复制到剪贴板');
          }).catch(() => {
            App.UI.Toast.error('复制失败');
          });
        }
      }
      
      // 回复按钮
      if (e.target.closest('[data-action="reply"]')) {
        const content = msg.querySelector('.answer-doc');
        if (content) {
          const text = content.textContent?.substring(0, 100) || '';
          App.Chat?.setReplyQuote(text + (content.textContent?.length > 100 ? '...' : ''));
          document.getElementById('input')?.focus();
          App.UI.Toast.info('已设置为回复引用');
        }
      }
      
      // 重新生成按钮
      if (e.target.closest('[data-action="regenerate"]')) {
        const wrap = e.target.closest('.msg');
        if (wrap) {
          App.Chat?.regenerate(wrap);
        }
      }
    });
  }
};

// ========== 工具栏管理 ==========
App.UI.Toolbar = {
  buttons: [],
  
  addButton: function(config) {
    const btn = document.createElement('button');
    btn.className = `tool-btn ${config.class || ''}`;
    btn.innerHTML = config.icon || '';
    btn.title = config.title || '';
    
    if (config.onclick) {
      btn.addEventListener('click', config.onclick);
    }
    
    this.buttons.push(btn);
    return btn;
  },
  
  render: function(container) {
    if (typeof container === 'string') {
      container = document.querySelector(container);
    }
    
    if (!container) return;
    
    container.innerHTML = '';
    this.buttons.forEach(btn => container.appendChild(btn));
  },
  
  clear: function() {
    this.buttons = [];
  }
};

// ========== 欢迎卡片快捷操作 ==========
App.UI.WelcomeChips = {
  init: function() {
    const chips = document.querySelectorAll('.welcome-chip');
    chips.forEach(chip => {
      chip.addEventListener('click', () => {
        const question = chip.dataset.q;
        if (question) {
          const input = document.getElementById('input');
          if (input) {
            input.value = question;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.focus();
          }
        }
      });
    });
  }
};

// ========== 初始化 ==========
document.addEventListener('DOMContentLoaded', () => {
  // 初始化各模块
  App.UI.Nav.init();
  App.UI.BackToBottom.init();
  App.UI.MessageActions.init();
  App.UI.WelcomeChips.init();
  
  // 注册侧边面板
  App.UI.Panel.register('exec', { title: '执行过程' });
  App.UI.Panel.register('todo', { title: '待办事项' });
  App.UI.Panel.register('kb', { title: '知识库' });
  App.UI.Panel.register('memory', { title: '长期记忆' });
  App.UI.Panel.register('runs', { title: '运行记录' });
  App.UI.Panel.register('telemetry', { title: '用量统计' });
  App.UI.Panel.register('schedule', { title: '定时任务' });
  App.UI.Panel.register('prompts', { title: '提示词库' });
  App.UI.Panel.register('tools', { title: '可用工具' });
  App.UI.Panel.register('settings', { title: '设置' });
  
  console.log('[UI] 现代 UI 模块已加载');
});

console.log('[UI] 现代 UI 模块已加载');
