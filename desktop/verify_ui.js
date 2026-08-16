const wsUrl = process.argv[2];
const ws = new WebSocket(wsUrl);
let id = 0;
const pending = new Map();
function send(method, params = {}) {
  return new Promise((res, rej) => {
    const mid = ++id;
    pending.set(mid, { res, rej });
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
}
const ev = (m, p = {}) => ws.send(JSON.stringify({ id: ++id, method: m, params: p }));
ws.onmessage = (evt) => {
  const msg = JSON.parse(evt.data);
  if (msg.id && pending.has(msg.id)) {
    const p = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? p.rej(new Error(msg.error.message)) : p.res(msg.result);
  } else if (msg.method === 'Runtime.exceptionThrown') {
    console.log('[EXCEPTION]', msg.params.exceptionDetails.text);
  } else if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') {
    console.log('[console.error]', msg.params.args.map((a) => a.value ?? a.description ?? '').join(' '));
  }
};
ws.onerror = (e) => { console.error('ws error', e.message); process.exit(1); };
(async () => {
  await new Promise((res) => (ws.onopen = res));
  await send('Runtime.enable');

  const evl = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    return r.result.value;
  };

  // 1. 输入框/输入坞布局
  const dock = await evl(`(() => {
    const d = document.querySelector('.input-dock');
    const input = document.getElementById('input');
    const dr = d.getBoundingClientRect(), ir = input.getBoundingClientRect();
    return {dockWidth: Math.round(dr.width), dockHeight: Math.round(dr.height),
      inputWidth: Math.round(ir.width), footerBottom: Math.round(window.innerHeight - dr.bottom),
      theme: document.body.dataset.theme};
  })()`);
  console.log('DOCK', JSON.stringify(dock));

  // 2. 触发一次真实发送,检查消息结构
  await evl(`App.els.input.value='你好,验证新版界面'; App.Chat.send(); true`);
  await new Promise((r) => setTimeout(r, 15000));

  const msgs = await evl(`(() => {
    const ms = document.querySelectorAll('#messages .msg');
    const out = [];
    ms.forEach((m) => {
      out.push({
        cls: m.className,
        avatar: m.querySelector('.avatar') ? m.querySelector('.avatar').textContent : null,
        bubble: m.querySelector('.bubble') ? 'yes' : null,
        text: (m.textContent || '').slice(0, 60),
      });
    });
    return out;
  })()`);
  console.log('MESSAGES', JSON.stringify(msgs, null, 1));

  // 3. 计算工具/错误/普通消息的宽度与对齐,检查是否挤压
  const layout = await evl(`(() => {
    const mm = document.getElementById('messages').getBoundingClientRect();
    const results = [];
    document.querySelectorAll('#messages .msg').forEach((m) => {
      const r = m.getBoundingClientRect();
      results.push({cls: m.className, left: Math.round(r.left - mm.left), right: Math.round(mm.right - r.right), w: Math.round(r.width)});
    });
    return {areaW: Math.round(mm.width), results};
  })()`);
  console.log('LAYOUT', JSON.stringify(layout));

  process.exit(0);
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
