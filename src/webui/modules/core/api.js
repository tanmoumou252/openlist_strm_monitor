import { navigate } from './router.js';

/**
 * 401 未授权错误。api() 在检测到 401 后会：
 * 1. 清除本地 token
 * 2. 导航至 #login
 * 3. 抛出此错误（而非静默 return null），让调用方与 router 可识别。
 *
 * router 在渲染期捕获此错误时会静默抑制（导航已完成），避免
 * 调用方解构 null 引起的 TypeError 闪现为错误页。
 */
export class ApiAuthError extends Error {
  constructor() {
    super('会话已过期，请重新登录');
    this.name = 'ApiAuthError';
  }
}

export async function api(path, options = {}) {
  if (typeof options === 'number') {
    options = { timeoutMs: options };
  }
  const { method = 'GET', timeoutMs = 10000, body, headers } = options;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const fetchOptions = { method, signal: ctrl.signal };
    // 自动附加 session token
    const allHeaders = { ...(headers || {}) };
    const token = localStorage.getItem('session_token');
    if (token) {
      allHeaders['X-Session-Token'] = token;
    }
    if (body !== undefined && body !== null) {
      fetchOptions.body = typeof body === 'string' ? body : JSON.stringify(body);
      allHeaders['Content-Type'] = 'application/json';
    }
    fetchOptions.headers = allHeaders;
    const resp = await fetch(path, fetchOptions);
    if (resp.status === 401) {
      localStorage.removeItem('session_token');
      localStorage.setItem('session_token_expired', '1');
      navigate('#login');
      // 抛出可识别错误：调用方解构会自然中断，router 抑制此错误，
      // 避免旧的 return null 导致所有调用方解构 null 触发 TypeError。
      throw new ApiAuthError();
    }
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      // 回退链：部分端点用 error 承载原因，命令型端点用 message；
      // 两者都缺失时才退化为状态码，避免真实原因在传输层被丢弃。
      throw new Error(err.error || err.message || `HTTP ${resp.status}`);
    }
    return resp.json();
  } finally {
    clearTimeout(timer);
  }
}
