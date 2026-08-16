const wsUrl = process.argv[2];
const out = process.argv[3];
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
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const p = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? p.rej(new Error(msg.error.message)) : p.res(msg.result);
  }
};
ws.onerror = (e) => { console.error(e.message); process.exit(1); };
(async () => {
  await new Promise((res) => (ws.onopen = res));
  const r = await send('Page.captureScreenshot', { format: 'png' });
  const fs = require('fs');
  fs.writeFileSync(out, Buffer.from(r.data, 'base64'));
  console.log('saved', out);
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
