/* API 封装:统一请求处理、超时、鉴权 */
(function () {
  const DEFAULT_TIMEOUT = 60000; // 60s 默认超时

  async function api(path, opts = {}) {
    opts = opts || {};
    const headers = opts.headers || {};
    
    // 鉴权
    if (App.backendApiKey) {
      headers['Authorization'] = 'Bearer ' + App.backendApiKey;
    }
    opts.headers = headers;

    // 超时保护
    if (!opts.signal) {
      const ctl = new AbortController();
      opts.signal = ctl.signal;
      const timeout = opts.timeout || DEFAULT_TIMEOUT;
      setTimeout(() => ctl.abort(), timeout);
    }

    // 防止缓存 SSE
    if (!opts.cache) opts.cache = 'no-store';

    const url = (App.backendUrl || '') + path;
    
    // DEBUG 日志
    if (App.DEBUG) {
      console.log(`[API] ${opts.method || 'GET'} ${path}`);
    }

    return fetch(url, opts);
  }

  async function pingBackend(timeout = 3000) {
    try {
      const res = await fetch(`${App.backendUrl}/health`, { 
        signal: AbortSignal.timeout(timeout) 
      });
      return res.ok;
    } catch (_) {
      return false;
    }
  }

  window.App = window.App || {};
  App.api = api;
  App.pingBackend = pingBackend;
})();
