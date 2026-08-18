/* 运行记录模块:checkpoint 列表 / 断点续跑 / 删除 */
App.Runs = {};

const RUN_STATUS_MAP = {
  running: ['⏳', '进行中'],
  interrupted: ['⏸', '已暂停'],
  done: ['✅', '已完成'],
  failed: ['❌', '失败'],
  pending: ['○', '待执行'],
};

App.Runs.load = async function () {
  const listEl = App.els.runsList;
  if (!listEl) return;
  try {
    const res = await App.api('/v1/runs');
    if (!res.ok) { listEl.innerHTML = '<div class="session-empty">运行记录加载失败</div>'; return; }
    const d = await res.json();
    const runs = d.runs || [];
    listEl.innerHTML = '';
    if (!runs.length) {
      listEl.innerHTML = '<div class="session-empty">暂无运行记录,执行任务后这里会显示 checkpoint</div>';
      return;
    }
    for (const r of runs) listEl.appendChild(App.Runs.buildItem(r));
  } catch (_) {
    listEl.innerHTML = '<div class="session-empty">网络错误</div>';
  }
};

App.Runs.buildItem = function (r) {
  const item = document.createElement('div');
  item.className = 'runs-item' + (r.status === 'interrupted' ? ' interrupted' : '');

  const head = document.createElement('div');
  head.className = 'runs-head';
  const st = RUN_STATUS_MAP[r.status] || ['·', r.status || ''];
  const badge = document.createElement('span');
  badge.className = 'runs-badge';
  badge.textContent = `${st[0]} ${st[1]}`;
  head.appendChild(badge);
  const time = document.createElement('span');
  time.className = 'runs-time';
  time.textContent = new Date((r.updated_at || 0) * 1000).toLocaleString();
  head.appendChild(time);

  const summary = r.summary || {};
  const body = document.createElement('div');
  body.className = 'runs-body';
  body.textContent = summary.input || r.run_id;
  body.title = summary.input || r.run_id;

  const foot = document.createElement('div');
  foot.className = 'runs-foot';
  const meta = document.createElement('span');
  meta.className = 'runs-meta';
  meta.textContent = `run_id: ${(r.run_id || '').slice(0, 12)} · 步骤 ${summary.steps ?? 0} · 工具 ${summary.tools ?? 0}`;
  foot.appendChild(meta);

  if (r.status === 'interrupted') {
    const resume = document.createElement('button');
    resume.className = 'lb-btn runs-resume';
    resume.textContent = '▶ 继续';
    resume.addEventListener('click', () => App.Runs.resume(r.run_id, resume));
    foot.appendChild(resume);
  }

  const del = document.createElement('button');
  del.className = 'runs-del';
  del.textContent = '🗑';
  del.title = '删除运行记录';
  del.addEventListener('click', () => App.Runs.deleteOne(r.run_id));
  foot.appendChild(del);

  item.appendChild(head);
  item.appendChild(body);
  item.appendChild(foot);
  return item;
};

App.Runs.resume = async function (runId, btn) {
  if (!(await App.confirm('从该断点继续执行?结果将写入对应会话。'))) return;
  if (btn) {
    btn.textContent = '…';
    btn.disabled = true;
  }
  try {
    const res = await App.api(`/v1/runs/${encodeURIComponent(runId)}/resume`, { method: 'POST' });
    const d = await res.json();
    if (res.ok) {
      if (d.interrupted) {
        App.appendMessage(`⏸ 运行继续后再次暂停: ${d.interrupt?.message || ''}`, 'bot');
      } else {
        App.toast('已继续运行', 'ok');
        if (d.answer) App.appendMessage(d.answer, 'bot');
      }
      App.Runs.load();
    } else {
      App.toast(`续跑失败: ${d.detail || res.status}`, 'err');
      if (btn) { btn.textContent = '▶ 继续'; btn.disabled = false; }
    }
  } catch (e) {
    App.toast(`续跑失败: ${e.message}`, 'err');
    if (btn) { btn.textContent = '▶ 继续'; btn.disabled = false; }
  }
};

App.Runs.deleteOne = async function (runId) {
  if (!(await App.confirm('确定删除这条运行记录吗?', { danger: true, okText: '删除' }))) return;
  try {
    const res = await App.api(`/v1/runs/${encodeURIComponent(runId)}`, { method: 'DELETE' });
    if (res.ok) {
      App.toast('已删除运行记录', 'ok');
      App.Runs.load();
    } else {
      const d = await res.json();
      App.toast(`删除失败: ${d.detail || res.status}`, 'err');
    }
  } catch (e) {
    App.toast(`删除失败: ${e.message}`, 'err');
  }
};
