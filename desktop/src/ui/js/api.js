/* API 封装:统一请求处理、超时、鉴权、响应解析 */
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

  /* 统一响应解析:把 fetch Response 转为 {success, data, status} 格式
   * 后端直接返回 JSON(无 success 包装),此函数做兼容适配。
   * GET 请求: res.ok && res.json() → {success:true, data: <json body>}
   * POST/DELETE 请求: 同上
   * 错误: {success:false, data: null, status: <http status>}
   */
  async function parseRes(res) {
    if (!res.ok) {
      let detail = '';
      try {
        const d = await res.json().catch(() => ({}));
        detail = d.detail || JSON.stringify(d);
      } catch (_) { detail = 'HTTP ' + res.status; }
      return { success: false, data: null, status: res.status, detail: detail };
    }
    // 无 body 的 204 等
    if (res.status === 204 || res.headers.get('content-length') === '0') {
      return { success: true, data: null, status: res.status };
    }
    try {
      const data = await res.json();
      return { success: true, data: data, status: res.status };
    } catch (_) {
      return { success: true, data: null, status: res.status };
    }
  }

  /* 便捷调用:api + parse 一步到位 */
  async function apiJSON(path, opts = {}) {
    const res = await api(path, opts);
    return parseRes(res);
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
  App.apiJSON = apiJSON;
  App.parseRes = parseRes;
  App.pingBackend = pingBackend;
})();
