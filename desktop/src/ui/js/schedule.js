/* 定时任务模块:创建 / 列表 / 暂停 / 恢复 / 立即执行 / 删除 */
App.Schedule = {};

const SCHED_STATUS_MAP = {
  pending: ['○', '待执行'],
  running: ['⏳', '执行中'],
  completed: ['✅', '已完成'],
  failed: ['❌', '失败'],
  paused: ['⏸', '已暂停'],
};

App.Schedule.load = async function () {
  const listEl = App.els.schedList;
  if (!listEl) return;
  try {
    const res = await App.api('/v1/schedules');
    if (!res.ok) { listEl.innerHTML = '<div class="session-empty">定时任务加载失败</div>'; return; }
    const d = await res.json();
    const tasks = d.tasks || [];
    listEl.innerHTML = '';
    if (!tasks.length) {
      listEl.innerHTML = '<div class="session-empty">暂无定时任务,填写上方表单创建一个</div>';
      return;
    }
    for (const t of tasks) listEl.appendChild(App.Schedule.buildItem(t));
  } catch (_) {
    listEl.innerHTML = '<div class="session-empty">网络错误</div>';
  }
};

App.Schedule.buildItem = function (t) {
  const item = document.createElement('div');
  item.className = 'sched-item' + (t.status === 'paused' ? ' paused' : '');

  const head = document.createElement('div');
  head.className = 'sched-head';
  const st = SCHED_STATUS_MAP[t.status] || ['·', t.status || ''];
  const badge = document.createElement('span');
  badge.className = 'sched-badge';
  badge.textContent = `${st[0]} ${st[1]}`;
  head.appendChild(badge);
  const name = document.createElement('span');
  name.className = 'sched-name';
  name.textContent = t.name || t.task_id;
  head.appendChild(name);

  const body = document.createElement('div');
  body.className = 'sched-body';
  const parts = [];
  parts.push(`函数: ${t.func_name || '-'}`);
  parts.push(t.cron_expr ? `cron: ${t.cron_expr}` : `每 ${Math.round(t.interval_seconds || 0)}s`);
  if (t.description) parts.push(t.description);
  if (t.last_error) parts.push(`⚠ ${t.last_error}`);
  body.textContent = parts.join(' · ');

  const foot = document.createElement('div');
  foot.className = 'sched-foot';
  const meta = document.createElement('span');
  meta.className = 'sched-meta';
  const when = t.next_run
    ? `下次 ${App.Schedule.fmtTime(t.next_run)} · 已执行 ${t.run_count || 0} 次`
    : `已执行 ${t.run_count || 0} 次`;
  meta.textContent = when;
  foot.appendChild(meta);

  const runBtn = document.createElement('button');
  runBtn.className = 'sched-act';
  runBtn.textContent = '▶';
  runBtn.title = '立即执行';
  runBtn.addEventListener('click', () => App.Schedule.runNow(t.task_id));

  const toggleBtn = document.createElement('button');
  toggleBtn.className = 'sched-act';
  toggleBtn.textContent = t.status === 'paused' ? '▶▶' : '⏸';
  toggleBtn.title = t.status === 'paused' ? '恢复' : '暂停';
  toggleBtn.addEventListener('click', () => App.Schedule.toggle(t));

  const editBtn = document.createElement('button');
  editBtn.className = 'sched-act';
  editBtn.textContent = '✏️';
  editBtn.title = '编辑任务';
  editBtn.addEventListener('click', () => App.Schedule.edit(t));

  const del = document.createElement('button');
  del.className = 'sched-del';
  del.textContent = '🗑';
  del.title = '删除';
  del.addEventListener('click', () => App.Schedule.deleteOne(t.task_id));

  foot.appendChild(runBtn);
  foot.appendChild(toggleBtn);
  foot.appendChild(editBtn);
  foot.appendChild(del);

  item.appendChild(head);
  item.appendChild(body);
  item.appendChild(foot);
  return item;
};

App.Schedule.add = async function () {
  const name = (App.els.schedNameInput.value || '').trim();
  const cron = (App.els.schedCronInput.value || '').trim();
  const interval = parseFloat(App.els.schedIntervalInput.value) || 0;
  if (!name) { App.toast('请输入任务名称', 'err'); return; }
  if (!cron && !(interval > 0)) { App.toast('请输入 Cron 表达式或执行间隔', 'err'); return; }
  const body = {
    name,
    func_name: App.els.schedFuncInput.value,
    cron_expr: cron,
    interval_seconds: interval,
  };
  const editingId = App.Schedule._editingId;
  try {
    if (editingId) {
      // 编辑:先删旧任务再重建(后端无 update 接口)
      await App.api(`/v1/schedules/${encodeURIComponent(editingId)}`, { method: 'DELETE' });
    }
    const res = await App.api('/v1/schedules', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await res.json();
    if (res.ok) {
      App.toast(editingId ? `已更新任务「${name}」` : `已创建定时任务「${name}」`, 'ok');
      App.Schedule.resetForm();
      App.Schedule.load();
    } else {
      App.toast(`创建失败: ${d.detail || res.status}`, 'err');
    }
  } catch (e) {
    App.toast(`创建失败: ${e.message}`, 'err');
  }
};

App.Schedule.resetForm = function () {
  App.Schedule._editingId = null;
  App.els.schedNameInput.value = '';
  App.els.schedCronInput.value = '';
  App.els.schedIntervalInput.value = '';
  const btn = App.els.schedAddBtn;
  if (btn) { btn.textContent = '创建'; btn.classList.remove('danger'); }
};

/* 编辑:载入任务到表单,保存后删除重建 */
App.Schedule.edit = function (t) {
  App.Schedule._editingId = t.task_id;
  App.els.schedNameInput.value = t.name || '';
  App.els.schedCronInput.value = t.cron_expr || '';
  App.els.schedIntervalInput.value = t.interval_seconds ? String(Math.round(t.interval_seconds)) : '';
  App.els.schedFuncInput.value = t.func_name || 'get_current_time';
  const btn = App.els.schedAddBtn;
  if (btn) { btn.textContent = '保存修改'; btn.classList.add('danger'); }
  App.els.schedNameInput.focus();
}

App.Schedule.runNow = async function (taskId) {
  try {
    const res = await App.api(`/v1/schedules/${encodeURIComponent(taskId)}/run`, { method: 'POST' });
    const d = await res.json();
    if (res.ok) {
      const result = typeof d.result === 'string' ? d.result : JSON.stringify(d.result || '');
      App.toast(`任务 ${taskId.slice(0, 8)} 已执行`, 'ok');
      if (result && result !== '{}' && result !== '""') App.appendMessage(result.slice(0, 500), 'bot');
    } else {
      App.toast(`执行失败: ${d.detail || d.error || res.status}`, 'err');
    }
    App.Schedule.load();
  } catch (e) {
    App.toast(`执行失败: ${e.message}`, 'err');
  }
};

App.Schedule.toggle = async function (t) {
  const paused = t.status === 'paused';
  const ep = paused ? 'resume' : 'pause';
  try {
    const res = await App.api(`/v1/schedules/${encodeURIComponent(t.task_id)}/${ep}`, { method: 'POST' });
    if (res.ok) {
      App.toast(paused ? '任务已恢复' : '任务已暂停', 'ok');
      App.Schedule.load();
    } else {
      App.toast('操作失败', 'err');
    }
  } catch (e) {
    App.toast(`操作失败: ${e.message}`, 'err');
  }
};

App.Schedule.deleteOne = async function (taskId) {
  if (!(await App.confirm('确定删除这个定时任务吗?', { danger: true, okText: '删除' }))) return;
  try {
    const res = await App.api(`/v1/schedules/${encodeURIComponent(taskId)}`, { method: 'DELETE' });
    if (res.ok) {
      if (App.Schedule._editingId === taskId) App.Schedule.resetForm();
      App.toast('已删除定时任务', 'ok');
      App.Schedule.load();
    } else {
      App.toast('删除失败', 'err');
    }
  } catch (e) {
    App.toast(`删除失败: ${e.message}`, 'err');
  }
};

App.Schedule.fmtTime = function (iso) {
  try { return new Date(iso).toLocaleString(); } catch (_) { return ''; }
};
