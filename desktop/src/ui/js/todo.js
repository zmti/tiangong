/* 待办模块:面板加载 / 添加 / 切换 / 删除 */
App.Todo = {};

App.Todo.load = async function () {
  const listEl = App.els.todoList;
  const filter = (document.getElementById('todoFilter') || {}).value || 'all';
  if (!listEl) return;
  try {
    const res = await App.apiJSON(`/v1/todos?only_pending=${filter === 'pending'}`);
    if (!res.success) { listEl.innerHTML = '<div class="session-empty">加载失败</div>'; return; }
    const data = res.data;
    const todos = data.todos || [];
    listEl.innerHTML = '';
    if (!todos.length) {
      listEl.innerHTML = '<div class="session-empty">' + (filter === 'pending' ? '没有未完成待办' : '暂无待办') + '</div>';
      return;
    }
    for (const t of todos) {
      listEl.appendChild(App.Todo.buildItem(t));
    }
  } catch (_) {
    listEl.innerHTML = '<div class="session-empty">网络错误</div>';
  }
};

App.Todo.buildItem = function (t) {
  const row = document.createElement('div');
  row.className = 'todo-item' + (t.done ? ' done' : '');

  const check = document.createElement('button');
  check.className = 'todo-check';
  check.textContent = t.done ? '✅' : '○';
  check.title = '切换完成状态';
  check.addEventListener('click', async () => {
    await App.apiJSON('/v1/todos/toggle', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: t.id }),
    });
    App.Todo.load();
  });

  const body = document.createElement('div');
  body.className = 'todo-body';
  const title = document.createElement('div');
  title.className = 'todo-title';
  title.textContent = t.title;
  body.appendChild(title);
  if (t.category && t.category !== '默认') {
    const cat = document.createElement('span');
    cat.className = 'todo-cat';
    cat.textContent = t.category;
    body.appendChild(cat);
  }
  if (t.due) {
    const due = document.createElement('span');
    due.className = 'todo-due';
    due.textContent = t.due;
    body.appendChild(due);
  }

  const del = document.createElement('button');
  del.className = 'todo-del';
  del.textContent = '🗑';
  del.title = '删除';
  del.addEventListener('click', async () => {
    if (!(await App.confirm(`确定删除待办「${t.title}」吗?`, { danger: true, okText: '删除' }))) return;
    try {
      await App.apiJSON('/v1/todos/delete', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: t.id }),
      });
      App.toast('待办已删除', 'ok');
    } catch (e) {
      App.toast(`删除失败: ${e.message}`, 'err');
    }
    App.Todo.load();
  });

  row.appendChild(check);
  row.appendChild(body);
  row.appendChild(del);
  return row;
};

App.Todo.add = async function () {
  const input = App.els.todoInput;
  const cat = App.els.todoCatInput;
  const dueInput = document.getElementById('todoDueInput');
  const title = (input.value || '').trim();
  if (!title) { App.toast('请输入待办内容', 'err'); return; }
  const dueRaw = (dueInput && dueInput.value) || '';
  input.value = '';
  try {
    const res = await App.apiJSON('/v1/todos', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title,
        category: (cat.value || '').trim() || '默认',
        due_at: dueRaw,
      }),
    });
    if (!res.success) throw new Error(res.detail || `HTTP ${res.status}`);
    App.toast('待办已添加', 'ok');
  } catch (e) {
    App.toast(`添加失败: ${e.message}`, 'err');
  }
  if (dueInput) dueInput.value = '';
  App.Todo.load();
};
