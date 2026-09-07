/* 命令面板模块 - Cmd/Ctrl+K 快速导航 */
(function () {
  var commandPalette = null;
  var commandInput = null;
  var commandList = null;
  var commandItems = [];
  var selectedIdx = 0;

  // 命令定义
  var COMMANDS = [
    { id: 'chat', label: '💬 对话', icon: '💬', action: function() { switchToPanel('chat'); } },
    { id: 'todo', label: '📋 待办事项', icon: '📋', action: function() { switchToPanel('todo'); } },
    { id: 'kb', label: '📚 知识库', icon: '📚', action: function() { switchToPanel('kb'); } },
    { id: 'memory', label: '🧠 长期记忆', icon: '🧠', action: function() { switchToPanel('memory'); } },
    { id: 'runs', label: '🕘 运行记录', icon: '🕘', action: function() { switchToPanel('runs'); } },
    { id: 'telemetry', label: '📊 用量统计', icon: '📊', action: function() { switchToPanel('telemetry'); } },
    { id: 'schedule', label: '⏰ 定时任务', icon: '⏰', action: function() { switchToPanel('schedule'); } },
    { id: 'prompts', label: '📝 提示词库', icon: '📝', action: function() { switchToPanel('prompts'); } },
    { id: 'tools', label: '🔧 工具管理', icon: '🔧', action: function() { switchToPanel('tools'); } },
    { id: 'workflow', label: '🔀 工作流', icon: '🔀', action: function() { switchToPanel('workflow'); } },
    { id: 'settings', label: '⚙️ 设置', icon: '⚙️', action: function() { switchToPanel('settings'); } },
    { id: 'new-session', label: '➕ 新建会话', icon: '➕', action: function() { App.NewSession && App.NewSession(); } },
    { id: 'theme', label: '🌓 切换主题', icon: '🌓', action: function() { App.ToggleTheme && App.ToggleTheme(); } },
    { id: 'project', label: '📁 项目选择', icon: '📁', action: function() { App.Project && App.Project.refreshHistory && App.Project.refreshHistory(); } },
  ];

  function switchToPanel(panelId) {
    var btn = document.querySelector('[data-view="' + panelId + '"]');
    if (btn) {
      btn.click();
    }
    closePalette();
  }

  function renderCommands(filter) {
    var filtered = COMMANDS;
    if (filter) {
      var q = filter.toLowerCase();
      filtered = COMMANDS.filter(function(c) {
        return c.label.toLowerCase().indexOf(q) !== -1 || 
               c.id.toLowerCase().indexOf(q) !== -1;
      });
    }
    return filtered;
  }

  function renderItem(cmd, index) {
    var item = document.createElement('div');
    item.className = 'command-item' + (index === selectedIdx ? ' selected' : '');
    item.innerHTML = '<span class="command-icon">' + cmd.icon + '</span><span class="command-label">' + 
                     App.escapeHtml(cmd.label) + '</span>';
    item.addEventListener('click', function() {
      cmd.action();
    });
    item.addEventListener('mouseenter', function() {
      selectedIdx = index;
      updateSelection();
    });
    return item;
  }

  function updateSelection() {
    var items = commandList.querySelectorAll('.command-item');
    items.forEach(function(item, i) {
      item.classList.toggle('selected', i === selectedIdx);
    });
    if (items[selectedIdx]) {
      items[selectedIdx].scrollIntoView({ block: 'nearest' });
    }
  }

  function openPalette() {
    if (commandPalette) return;
    
    commandPalette = document.createElement('div');
    commandPalette.className = 'command-palette';
    
    var overlay = document.createElement('div');
    overlay.className = 'command-overlay';
    overlay.addEventListener('click', closePalette);
    
    var container = document.createElement('div');
    container.className = 'command-container';
    
    var header = document.createElement('div');
    header.className = 'command-header';
    header.innerHTML = '<span class="command-search-icon">🔍</span>';
    
    commandInput = document.createElement('input');
    commandInput.className = 'command-input';
    commandInput.placeholder = '搜索命令...';
    commandInput.setAttribute('autocomplete', 'off');
    
    header.appendChild(commandInput);
    
    commandList = document.createElement('div');
    commandList.className = 'command-list';
    
    var footer = document.createElement('div');
    footer.className = 'command-footer';
    footer.innerHTML = '<span class="command-hint">↑↓ 导航</span><span class="command-hint">Enter 执行</span><span class="command-hint">Esc 关闭</span>';
    
    container.appendChild(header);
    container.appendChild(commandList);
    container.appendChild(footer);
    
    commandPalette.appendChild(overlay);
    commandPalette.appendChild(container);
    
    document.body.appendChild(commandPalette);
    
    commandItems = renderCommands('');
    renderCommandList();
    
    setTimeout(function() {
      commandInput.focus();
    }, 10);
    
    commandInput.addEventListener('input', function() {
      commandItems = renderCommands(commandInput.value);
      selectedIdx = 0;
      renderCommandList();
    });
    
    commandInput.addEventListener('keydown', function(e) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        selectedIdx = Math.min(selectedIdx + 1, commandItems.length - 1);
        updateSelection();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        selectedIdx = Math.max(selectedIdx - 1, 0);
        updateSelection();
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (commandItems[selectedIdx]) {
          commandItems[selectedIdx].action();
        }
      } else if (e.key === 'Escape') {
        e.preventDefault();
        closePalette();
      }
    });
  }

  function renderCommandList() {
    commandList.innerHTML = '';
    if (commandItems.length === 0) {
      var empty = document.createElement('div');
      empty.className = 'command-empty';
      empty.textContent = '未找到匹配的命令';
      commandList.appendChild(empty);
      return;
    }
    commandItems.forEach(function(cmd, index) {
      commandList.appendChild(renderItem(cmd, index));
    });
  }

  function closePalette() {
    if (commandPalette) {
      commandPalette.remove();
      commandPalette = null;
      commandInput = null;
      commandList = null;
      commandItems = [];
      selectedIdx = 0;
    }
  }

  document.addEventListener('keydown', function(e) {
    if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
      e.preventDefault();
      if (commandPalette) {
        closePalette();
      } else {
        openPalette();
      }
    }
    if (e.key === 'Escape' && commandPalette) {
      e.preventDefault();
      closePalette();
    }
  });

  window.App = window.App || {};
  App.CommandPalette = {
    open: openPalette,
    close: closePalette,
    toggle: function() {
      if (commandPalette) {
        closePalette();
      } else {
        openPalette();
      }
    }
  };
})();
