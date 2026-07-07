export async function api(path, options = {}) {
  if (typeof options === 'number') {
    options = { timeoutMs: options };
  }
  const { method = 'GET', timeoutMs = 10000, body, headers } = options;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const fetchOptions = { method, signal: ctrl.signal };
    if (body !== undefined && body !== null) {
      fetchOptions.body = typeof body === 'string' ? body : JSON.stringify(body);
      fetchOptions.headers = { 'Content-Type': 'application/json', ...(headers || {}) };
    } else if (headers) {
      fetchOptions.headers = headers;
    }
    const resp = await fetch(path, fetchOptions);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${resp.status}`);
    }
    return resp.json();
  } finally {
    clearTimeout(timer);
  }
}
