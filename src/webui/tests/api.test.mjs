import { test } from 'node:test';
import assert from 'node:assert/strict';
import { api, ApiAuthError } from '../modules/core/api.js';

// api.js 函数体内引用 localStorage / fetch / location —— 以 globalThis 替身 stub
function installStubs() {
  const store = new Map();
  const calls = { removed: [], set: [] };
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { calls.set.push([k, String(v)]); store.set(k, String(v)); },
    removeItem: (k) => { calls.removed.push(k); store.delete(k); },
  };
  globalThis.__lsCalls = calls;
  globalThis.__lsStore = store;
}

async function withLocation(fn) {
  const prev = globalThis.location;
  globalThis.location = { hash: '' };
  try {
    await fn();
  } finally {
    globalThis.location = prev ?? undefined;
  }
}

test('api injects X-Session-Token header from localStorage', async () => {
  installStubs();
  globalThis.localStorage.setItem('session_token', 'tok-123');
  let seen = null;
  globalThis.fetch = async (path, options) => {
    seen = { path, options };
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };
  const data = await api('/api/x');
  assert.equal(seen.options.headers['X-Session-Token'], 'tok-123');
  assert.deepEqual(data, { ok: true });
});

test('api omits token header when localStorage has none', async () => {
  installStubs();
  let seen = null;
  globalThis.fetch = async (path, options) => {
    seen = { path, options };
    return { ok: true, status: 200, json: async () => ({}) };
  };
  await api('/api/x');
  assert.equal('X-Session-Token' in seen.options.headers, false);
});

// 401 四动作契约：①清 token ②写 session_token_expired ③navigate('#login') ④抛 ApiAuthError
test('401 triggers all four actions and throws ApiAuthError', async () => {
  installStubs();
  globalThis.localStorage.setItem('session_token', 'expired');
  globalThis.fetch = async () => ({ ok: false, status: 401, json: async () => ({}) });
  await withLocation(async () => {
    await assert.rejects(
      () => api('/api/x'),
      (e) => {
        assert.ok(e instanceof ApiAuthError, '必须抛出 ApiAuthError 实例');
        return true;
      },
    );
    const calls = globalThis.__lsCalls;
    assert.ok(calls.removed.includes('session_token'), '动作①：清除 session_token');
    const expiredWritten = calls.set.some(([k, v]) => k === 'session_token_expired' && v === '1');
    assert.ok(expiredWritten, '动作②：写入 session_token_expired=1');
    assert.equal(globalThis.location.hash, '#login', '动作③：跳转 #login');
  });
});

// router 对 ApiAuthError 的静默抑制依赖该类可被 instanceof 识别
test('ApiAuthError is an Error subclass identifiable via instanceof', () => {
  const e = new ApiAuthError();
  assert.ok(e instanceof Error);
  assert.equal(e.name, 'ApiAuthError');
});
