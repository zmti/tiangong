/* 提示词库模块:本地持久化 / 面板渲染 / 点击填充 / 输入框 / 快捷唤起 */
App.Prompts = {};

const PROMPT_STORAGE_KEY = 'prompts';

const PROMPT_DEFAULTS = [
  { title: '代码审查', text: '请对以下代码进行审查,指出潜在 bug、安全隐患和可读性问题,并给出改进建议。' },
  { title: '解释代码', text: '请用通俗易懂的语言解释下面这段代码的作用和实现思路。' },
  { title: '编写测试', text: '请为下面的代码编写单元测试,覆盖正常、边界和异常情况。' },
  { title: '优化性能', text: '请分析下面代码的性能瓶颈,并给出优化后的实现。' },
  { title: '生成文档', text: '请为下面的代码生成一份简洁的文档,包含功能说明和用法示例。' },
];

App.Prompts.load = function () {
  try {
    // 优先从统一存储加载
    if (App.Storage) {
      const raw = App.Storage.getCustom(PROMPT_STORAGE_KEY);
      if (raw && Array.isArray(raw)) return raw;
    }
    // 降级到 localStorage
    const stored = localStorage.getItem('agent_prompts');
    if (stored) return JSON.parse(stored);
  } catch (_) { /* 忽略 */ }
  return PROMPT_DEFAULTS.slice();
};

App.Prompts.save = function (list) {
  try {
    if (App.Storage) {
      App.Storage.setCustom(PROMPT_STORAGE_KEY, list);
    } else {
      localStorage.setItem('agent_prompts', JSON.stringify(list));
    }
  } catch (_) { /* 忽略 */ }
};

App.Prompts.render = function () {
  const listEl = App.els.promptList;
  if (!listEl) return;
  const items = App.Prompts.load();
  listEl.innerHTML = '';
  if (!items.length) {
    listEl.innerHTML = '<div class="session-empty">暂无提示词,填写上方表单新增</div>';
    return;
  }
  for (const p of items) {
    const item = document.createElement('div');
    item.className = 'prompt-item';
    const title = document.createElement('div');
    title.className = 'prompt-title';
    title.textContent = p.title;
    title.title = '点击填入输入框';
    const text = document.createElement('div');
    text.className = 'prompt-text';
    text.textContent = p.text;
    const del = document.createElement('button');
    del.className = 'prompt-del';
    del.textContent = '🗑';
    del.title = '删除该提示词';
    del.addEventListener('click', (e) => {
      e.stopPropagation();
      const idx = items.indexOf(p);
      if (idx >= 0) {
        items.splice(idx, 1);
        App.Prompts.save(items);
        App.Prompts.render();
      }
    });
    item.appendChild(title);
    item.appendChild(text);
    item.appendChild(del);
    item.addEventListener('click', () => {
      if (App.els.input) {
        App.els.input.value = p.text;
        App.els.input.focus();
      }
    });
    listEl.appendChild(item);
  }
};

App.Prompts.add = function (title, text) {
  if (!title || !text) return;
  const items = App.Prompts.load();
  items.unshift({ title, text });
  App.Prompts.save(items);
  App.Prompts.render();
};

App.Prompts.clear = function () {
  if (App.Prompts.load().length === 0) return;
  App.Prompts.save([]);
  App.Prompts.render();
};

/* 初始化 */
document.addEventListener('DOMContentLoaded', () => {
  if (App.els.promptList) {
    App.Prompts.render();
  }
});
