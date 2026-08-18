/* 工具模块:工具卡片网格 */
App.Tools = {};

App.Tools.load = async function () {
  const grid = App.els.toolGrid;
  try {
    const res = await App.api('/v1/tools');
    if (!res.ok) { grid.innerHTML = '<div class="session-empty">工具加载失败</div>'; return; }
    const data = await res.json();
    const tools = data.tools || [];
    grid.innerHTML = '';
    if (!tools.length) {
      grid.innerHTML = '<div class="session-empty">暂无可用的工具</div>';
      return;
    }
    for (const t of tools) grid.appendChild(App.Tools.buildCard(t));
  } catch (_) {
    grid.innerHTML = '<div class="session-empty">网络错误</div>';
  }
};

App.Tools.buildCard = function (t) {
  const card = document.createElement('button');
  card.className = 'tool-card';
  card.title = '点击查看参数';

  const name = document.createElement('div');
  name.className = 't-name';
  const flag = document.createElement('span');
  flag.className = 't-flag' + (t.enabled === false ? ' off' : '');
  flag.textContent = t.enabled === false ? '停用' : '可用';
  name.textContent = t.name;
  name.appendChild(flag);
  if (t.dangerous) {
    const d = document.createElement('span');
    d.className = 't-flag t-dangerous';
    d.textContent = '🔒 危险';
    d.title = '危险工具,执行这类操作前需要你在窗口中确认';
    name.appendChild(d);
  }

  const desc = document.createElement('div');
  desc.className = 't-desc';
  desc.textContent = t.description || '';

  const params = document.createElement('div');
  params.className = 't-params';
  let paramText = '无参数';
  try {
    const schema = t.parameters || {};
    if (schema.properties && Object.keys(schema.properties).length) {
      const props = schema.properties;
      const required = new Set(schema.required || []);
      const lines = Object.keys(props).map((k) => {
        const p = props[k];
        const type = p.type || 'any';
        const req = required.has(k) ? ' (必填)' : '';
        const hint = p.description ? ` - ${p.description}` : '';
        return `• ${k}${req}: ${type}${hint}`;
      });
      paramText = lines.join('\n');
    } else {
      paramText = '无参数';
    }
  } catch (_) { paramText = '参数格式未知'; }
  params.textContent = paramText;

  card.appendChild(name);
  card.appendChild(desc);
  card.appendChild(params);

  card.addEventListener('click', () => {
    card.classList.toggle('expanded');
  });
  return card;
};
