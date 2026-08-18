/* 后台任务模块:创建 / 列表 / 监控 / 停止 */
App.BackendTasks = {};

const TASK_STATUS_MAP = {
  pending:    ['○', '待执行'],
  running:    ['⏳', '执行中'],
  completed:  ['✅', '已完成'],
  failed:     ['❌', '失败'],
  cancelled:  ['⛔', '已取消'],
};

App.BackendTasks.load = async function () {
  const listEl = App.els._bgTaskList;
  if (!listEl) return;
  try {
    const res = await App.api('/v1/tasks');
    if (!res.ok) { listEl.innerHTML = '<div class="session-empty">后台任务加载失败</div>'; return; }
    const d = await res.json();
    const tasks = d.tasks || [];
    listEl.innerHTML = '';
    if (!tasks.length) {
      listEl.innerHTML = '<div class="session-empty">暂无后台任务</div>';
      return;
    }
    for (const t of tasks) listEl.appendChild(App.BackendTasks.buildItem(t));
  } catch (_) {
    listEl.innerHTML = '<div class="session-empty">网络错误</div>';
  }
};

App.BackendTasks.buildItem = function (t) {
  const item = document.createElement('div');
  item.className = 'bg-task-item' + (t.status === 'running' ? ' running' : '');

  const head = document.createElement('div');
  head.className = 'bg-task-head';
  const st = TASK_STATUS_MAP[t.status] || ['·', t.status || ''];
  const badge = document.createElement('span');
  badge.className = 'bg-task-badge';
  badge.textContent = `${st[0]} ${st[1]}`;
  head.appendChild(badge);
  const name = document.createElement('span');
  name.className = 'bg-task-name';
  name.textContent = t.name || t.task_id;
  name.title = t.name || '';
  head.appendChild(name);
  const time = document.createElement('span');
  time.className = 'bg-task-time';
  time.textContent = t.started_at
    ? App.BackendTasks.fmtTime(t.started_at)
    : App.BackendTasks.fmtTime(t.created_at);
  head.appendChild(time);

  const body = document.createElement('div');
  body.className = 'bg-task-body';
  const parts = [];
  if (t.plan?.steps) parts.push(`计划 ${t.plan.steps.length} 步`);
  if (t.steps_executed != null) parts.push(`已执行 ${t.steps_executed} 步`);
  if (t.last_error) parts.push(`⚠ ${t.last_error.slice(0, 60)}`);
  body.textContent = parts.join(' · ') || '等待执行...';

  const foot = document.createElement('div');
  foot.className = 'bg-task-foot';
  if (t.status === 'running') {
    const stopBtn = document.createElement('button');
    stopBtn.className = 'sched-act';
    stopBtn.textContent = '⏹';
    stopBtn.title = '停止任务';
    stopBtn.addEventListener('click', () => App.BackendTasks.stop(t.task_id, stopBtn));
    foot.appendChild(stopBtn);
  }
  const del = document.createElement('button');
  del.className = 'sched-del';
  del.textContent = '🗑';
  del.title = '删除';
  del.addEventListener('click', () => App.BackendTasks.deleteOne(t.task_id));
  foot.appendChild(del);

  item.appendChild(head);
  item.appendChild(body);
  item.appendChild(foot);
  return item;
};

App.BackendTasks.stop = async function (taskId, btn) {
  try {
    const res = await App.api(`/v1/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' });
    if (res.ok) {
      App.toast('任务已停止', 'ok');
      App.BackendTasks.load();
    } else {
      App.toast('停止失败', 'err');
    }
  } catch (e) {
    App.toast(`停止失败: ${e.message}`, 'err');
  }
};

App.BackendTasks.deleteOne = async function (taskId) {
  if (!(await App.confirm('确定删除这个后台任务吗?', { danger: true, okText: '删除' }))) return;
  try {
    const res = await App.api(`/v1/tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE' });
    if (res.ok) { App.toast('已删除', 'ok'); App.BackendTasks.load(); }
    else App.toast('删除失败', 'err');
  } catch (e) {
    App.toast(`删除失败: ${e.message}`, 'err');
  }
};

App.BackendTasks.fmtTime = function (ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  if (typeof ts === 'number' && ts > 1e12) return d.toLocaleString();
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
};
