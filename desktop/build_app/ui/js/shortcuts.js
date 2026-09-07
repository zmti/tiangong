/* 快捷键提示面板 */
App.Shortcuts = {};

const KEYBOARD_SHORTCUTS = [
  { keys: ['Alt', 'Shift', 'Space'], desc: '唤出 / 隐藏窗口' },
  { keys: ['Ctrl', 'K'], desc: '打开命令面板' },
  { keys: ['Enter'], desc: '发送消息' },
  { keys: ['Shift', 'Enter'], desc: '换行' },
  { keys: ['/'], desc: '唤起提示词库' },
  { keys: ['Ctrl', 'V'], desc: '粘贴图片到聊天' },
  { keys: ['Esc'], desc: '关闭提示词候选 / 弹窗' },
  { keys: ['Tab'], desc: '选择提示词候选' },
  { keys: ['↑', '↓'], desc: '切换提示词候选' },
];

App.Shortcuts.render = function() {
  const el = document.getElementById('shortcutsList');
  if (!el) return;
  el.innerHTML = '';
  for (const s of KEYBOARD_SHORTCUTS) {
    const item = document.createElement('div');
    item.className = 'shortcut-item';
    const keys = document.createElement('div');
    keys.className = 'shortcut-keys';
    for (const k of s.keys) {
      const kbd = document.createElement('kbd');
      kbd.textContent = k;
      keys.appendChild(kbd);
    }
    const desc = document.createElement('span');
    desc.className = 'shortcut-desc';
    desc.textContent = s.desc;
    item.appendChild(keys);
    item.appendChild(desc);
    el.appendChild(item);
  }
};
