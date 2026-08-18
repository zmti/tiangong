/* 用量统计模块:trace 聚合统计 + 最近运行列表 */
App.Telemetry = {};

App.Telemetry.load = async function () {
  await Promise.all([App.Telemetry.renderStats(), App.Telemetry.renderRuns()]);
};

App.Telemetry.renderStats = async function () {
  const el = App.els.telStats;
  if (!el) return;
  try {
    const res = await App.api('/v1/traces/stats');
    if (!res.ok) { el.innerHTML = '<div class="session-empty">用量统计不可用</div>'; return; }
    const s = await res.json();
    const cards = [
      ['LLM 调用', s.llm_calls ?? 0],
      ['工具调用', s.tool_calls ?? 0],
      ['Token 总量', (s.total_tokens ?? 0).toLocaleString()],
      ['成本 (USD)', (s.estimated_cost_usd ?? 0).toFixed(6)],
      ['错误', s.errors ?? 0],
      ['缓存命中', `${(s.cache_hit_rate ?? 0) * 100}%`],
    ];
    el.innerHTML = '';
    for (const [label, val] of cards) {
      const c = document.createElement('div');
      c.className = 'tel-card';
      const v = document.createElement('div');
      v.className = 'tel-card-val';
      v.textContent = val;
      const l = document.createElement('div');
      l.className = 'tel-card-label';
      l.textContent = label;
      c.appendChild(v);
      c.appendChild(l);
      el.appendChild(c);
    }
  } catch (_) {
    el.innerHTML = '<div class="session-empty">用量统计加载失败</div>';
  }
};

App.Telemetry.renderRuns = async function () {
  const el = App.els.telRuns;
  if (!el) return;
  try {
    const res = await App.api('/v1/traces/runs');
    if (!res.ok) { el.innerHTML = ''; return; }
    const d = await res.json();
    const runs = d.runs || [];
    el.innerHTML = '<div class="tel-runs-title">最近运行</div>';
    if (!runs.length) {
      el.innerHTML += '<div class="session-empty">暂无运行数据</div>';
      return;
    }
    for (const r of runs) el.appendChild(App.Telemetry.buildRun(r));
  } catch (_) {
    el.innerHTML = '<div class="session-empty">最近运行加载失败</div>';
  }
};

App.Telemetry.buildRun = function (r) {
  const item = document.createElement('div');
  item.className = 'tel-run';

  const head = document.createElement('div');
  head.className = 'tel-run-head';
  const status = document.createElement('span');
  status.className = 'tel-run-status ' + (r.ok === false ? 'err' : 'ok');
  status.textContent = r.ok === false ? '✗' : '✓';
  head.appendChild(status);
  const input = document.createElement('span');
  input.className = 'tel-run-input';
  input.textContent = r.input || r.run_id;
  input.title = r.input || r.run_id;
  head.appendChild(input);

  const meta = document.createElement('div');
  meta.className = 'tel-run-meta';
  const parts = [];
  if (r.duration != null) parts.push(`${r.duration.toFixed(2)}s`);
  parts.push(`LLM ${r.llm_calls ?? 0}`);
  parts.push(`工具 ${r.tools ?? 0}`);
  parts.push(`${(r.tokens ?? 0).toLocaleString()} tok`);
  if (r.cost) parts.push(`$${r.cost.toFixed(6)}`);
  if (r.error) parts.push(`⚠ ${r.error}`);
  meta.textContent = parts.join(' · ');

  item.appendChild(head);
  item.appendChild(meta);
  return item;
};
