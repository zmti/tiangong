/* 提示词库模块:本地持久化 / 面板渲染 / 点击填充 / 输入框 / 快捷唤起 */
App.Prompts = {};

const PROMPT_STORAGE_KEY = 'agent_prompts';

const PROMPT_DEFAULTS = [
  { title: '代码审查', text: '请对以下代码进行审查,指出潜在 bug、安全隐患和可读性问题,并给出改进建议。' },
  { title: '解释代码', text: '请用通俗易懂的语言解释下面这段代码的作用和实现思路。' },
  { title: '编写测试', text: '请为下面的代码编写单元测试,覆盖正常、边界和异常情况。' },
  { title: '优化性能', text: '请分析下面代码的性能瓶颈,并给出优化后的实现。' },
  { title: '生成文档', text: '请为下面的代码生成一份简洁的文档,包含功能说明和用法示例。' },
];

App.Prompts.load = function () {
  try {
    const raw = localStorage.getItem(PROMPT_STORAGE_KEY);
    if (raw) return JSON.parse(raw);
  } catch (_) { /* 忽略 */ }
  return PROMPT_DEFAULTS.slice();
};

App.Prompts.save = function (list) {
  try { localStorage.setItem(PROMPT_STORAGE_KEY, JSON.stringify(list)); } catch (_) { /* 忽略 */ }
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
      App.Prompts.remove(p.title);
    });
    item.appendChild(title);
    item.appendChild(text);
    item.appendChild(del);
    item.addEventListener('click', () => App.Prompts.insert(p.text));
    listEl.appendChild(item);
  }
};

App.Prompts.add = function () {
  const title = (App.els.promptTitleInput.value || '').trim();
  const text = (App.els.promptTextInput.value || '').trim();
  if (!title || !text) { App.toast('请填写标题和内容', 'err'); return; }
  const items = App.Prompts.load();
  if (items.some((p) => p.title === title)) { App.toast(`已存在同名提示词「${title}」`, 'err'); return; }
  items.push({ title, text });
  App.Prompts.save(items);
  App.els.promptTitleInput.value = '';
  App.els.promptTextInput.value = '';
  App.Prompts.render();
  App.toast('提示词已保存', 'ok');
};

App.Prompts.remove = function (title) {
  const items = App.Prompts.load().filter((p) => p.title !== title);
  App.Prompts.save(items);
  App.Prompts.render();
};

/* 点击提示词 → 填入输入框(追加到末尾) */
App.Prompts.insert = function (text) {
  const input = App.els.input;
  if (!input) return;
  const cur = input.value.trim();
  input.value = cur ? cur + '\n\n' + text : text;
  input.focus();
};

/* 输入框 / 快捷指令:匹配当前输入返回候选提示词 */
App.Prompts.candidates = function (query) {
  const q = (query || '').trim().toLowerCase();
  return App.Prompts.load().filter((p) =>
    p.title.toLowerCase().includes(q) || p.text.toLowerCase().includes(q)
  );
};
