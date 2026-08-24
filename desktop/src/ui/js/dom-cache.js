/* 统一 DOM 元素缓存:避免重复查询 */
(function () {
  // 元素定义: { id: 元素ID, name: App.els 中的名称 }
  const ELEMENTS = [
    // 主区域
    { id: 'messages', name: 'messages' },
    { id: 'input', name: 'input' },
    { id: 'sendBtn', name: 'sendBtn' },
    { id: 'sessionList', name: 'sessionList' },
    { id: 'sessionLabel', name: 'sessionLabel' },
    { id: 'sessionSearch', name: 'sessionSearch' },

    // 主题
    { id: 'themeBtn', name: 'themeBtn' },
    { id: 'themeToggle', name: 'themeToggle' },

    // 后端
    { id: 'backendUrlText', name: 'backendUrlText' },
    { id: 'backendStatusText', name: 'backendStatusText' },
    { id: 'backendUrlInput', name: 'backendUrlInput' },

    // 模式选择
    { id: 'modeSelect', name: 'modeSelect' },
    { id: 'modeBadge', name: 'modeBadge' },
    { id: 'modePopover', name: 'modePopover' },

    // 模型选择
    { id: 'modelPickerBtn', name: 'modelPickerBtn' },
    { id: 'modelPopover', name: 'modelPopover' },

    // 图片
    { id: 'imgPreviewBar', name: 'imgPreviewBar' },
    { id: 'previewImg', name: 'previewImg' },
    { id: 'previewName', name: 'previewName' },

    // 附件
    { id: 'attachBtn', name: 'attachBtn' },
    { id: 'attachInput', name: 'attachInput' },
    { id: 'attachClearBtn', name: 'attachClearBtn' },
    { id: 'attachList', name: 'attachList' },
    { id: 'attachBar', name: 'attachBar' },

    // 工具
    { id: 'toolGrid', name: 'toolGrid' },

    // 记忆
    { id: 'memoryStats', name: 'memoryStats' },
    { id: 'memoryList', name: 'memoryList' },
    { id: 'memoryInput', name: 'memoryInput' },
    { id: 'memoryKwInput', name: 'memoryKwInput' },

    // 知识库
    { id: 'docStats', name: 'docStats' },
    { id: 'docList', name: 'docList' },

    // Todo
    { id: 'todoList', name: 'todoList' },
    { id: 'todoInput', name: 'todoInput' },
    { id: 'todoCatInput', name: 'todoCatInput' },
    { id: 'todoFilter', name: 'todoFilter' },
    { id: 'todoDueInput', name: 'todoDueInput' },

    // 运行记录
    { id: 'runsList', name: 'runsList' },

    // 定时任务
    { id: 'schedList', name: 'schedList' },
    { id: 'schedNameInput', name: 'schedNameInput' },
    { id: 'schedCronInput', name: 'schedCronInput' },
    { id: 'schedIntervalInput', name: 'schedIntervalInput' },
    { id: 'schedFuncInput', name: 'schedFuncInput' },
    { id: 'schedAddBtn', name: 'schedAddBtn' },

    // 提示词
    { id: 'promptList', name: 'promptList' },
    { id: 'promptTitleInput', name: 'promptTitleInput' },
    { id: 'promptTextInput', name: 'promptTextInput' },

    // 终端日志
    { id: 'terminalPanel', name: 'terminalPanel' },
    { id: 'terminalLogList', name: 'terminalLogList' },

    // 状态
    { id: 'statusDot', name: 'statusDot' },
    { id: 'statusText', name: 'statusText' },

    // 轻预览
    { id: 'lbImg', name: 'lbImg' },
    { id: 'lightbox', name: 'lightbox' },

    // 其他
    { id: 'scrollPill', name: 'scrollPill' },
    { id: 'typing', name: 'typing' },
    { id: 'videoBtn', name: 'videoBtn' },
    { id: 'videoInput', name: 'videoInput' },
    { id: 'micBtn', name: 'micBtn' },
    { id: 'providerAdmin', name: 'providerAdmin' },
    { id: 'provFormWrap', name: 'provFormWrap' },
    { id: 'shortcutsList', name: 'shortcutsList' },
    { id: 'decayList', name: 'decayList' },
    { id: 'telStats', name: 'telStats' },
    { id: 'telRuns', name: 'telRuns' },
    { id: '_bgTaskList', name: '_bgTaskList' },
  ];

  function init() {
    const els = {};
    ELEMENTS.forEach(({ id, name }) => {
      els[name] = document.getElementById(id) || null;
    });
    return els;
  }

  window.App = window.App || {};
  App.Elements = { init };
})();
