/* API 封装:统一请求处理、超时、鉴权、响应解析 */
(function () {
  const DEFAULT_TIMEOUT = 60000; // 60s 默认超时

  // 这些 body 类型必须由浏览器设置 Content-Type(带 boundary),代码不能代劳
  function _needsBrowserContentType(body) {
    if (body == null) return false;
    if (typeof FormData !== 'undefined' && body instanceof FormData) return true;
    if (typeof Blob !== 'undefined' && body instanceof Blob) return true;
    if (typeof URLSearchParams !== 'undefined' && body instanceof URLSearchParams) return true;
    if (typeof ArrayBuffer !== 'undefined' && body instanceof ArrayBuffer) return true;
    if (ArrayBuffer.isArrayBuffer && body.buffer instanceof ArrayBuffer) return true; // TypedArray
    return false;
  }

  async function api(path, opts = {}) {
    opts = opts || {};
    const headers = opts.headers || {};

    // POST/PUT 有 body 时自动加 Content-Type（除非调用方已指定）
    // ⚠️ FormData / Blob / URLSearchParams 必须让浏览器自己带
    //    multipart/form-data; boundary=xxx —— 手动写成 application/json 会让
    //    后端整个 body 解析失败(实测 /v1/vision 返回 422 "body.file Field required"),
    //    影响全部上传类接口:附件、知识库、视频、语音、图片识别。
    if (opts.body && !headers['Content-Type'] && !headers['content-type']
        && !_needsBrowserContentType(opts.body)) {
      headers['Content-Type'] = 'application/json';
    }

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

  /* 把 FastAPI 的错误响应体转成人话:
   * - {"detail":"文本"}            → 文本
   * - {"detail":[{"loc":[...],"msg":"..."}]} → "body.file: Field required"
   * - 其它 / 空                    → 紧凑 JSON,绝不出现 [object Object]
   */
  function formatApiDetail(data) {
    const d = (data && data.detail !== undefined) ? data.detail : data;
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) {
      return d.map((x) => {
        if (!x || typeof x !== 'object') return String(x);
        const loc = Array.isArray(x.loc) ? x.loc.filter((p) => p !== 'body').join('.') : '';
        return loc ? `${loc}: ${x.msg}` : (x.msg || JSON.stringify(x));
      }).join('; ');
    }
    if (d && typeof d === 'object') return JSON.stringify(d);
    return d ? String(d) : '';
  }

  window.App = window.App || {};
  App.api = api;
  App.formatApiDetail = formatApiDetail;
  App.apiJSON = apiJSON;
  App.parseRes = parseRes;
  App.pingBackend = pingBackend;
})();
