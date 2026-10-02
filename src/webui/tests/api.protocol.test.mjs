import test from 'node:test';
import assert from 'node:assert/strict';

// api.js 协议层契约：错误回退链、body 序列化、旧式数字签名与超时 abort。

function stubStorage(token = null) {
  const store = new Map();
  if (token !== null) store.set('session_token', token);
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  };
}

function recordingFetch(handler) {
  const calls = [];
  const fn = async (path, opts = {}) => {
    calls.push({ path, opts });
    return handler(path, opts, calls.length - 1);
  };
  fn.calls = calls;
  return fn;
}

const { api } = await import('../modules/core/api.js');

test('error message fallback chain: error then message then status', async () => {
  stubStorage();
  for (const payload of [
    { error: 'E1', message: 'M1' },
    { message: 'M2' },
    {},
  ]) {
    globalThis.fetch = recordingFetch(() => ({
      ok: false, status: 500, statusText: 'oops',
      json: async () => payload,
    }));
    await assert.rejects(api('/x'), (e) => {
      const expected = payload.error || payload.message || 'HTTP 500';
      assert.equal(e.message, expected);
      return true;
    });
  }
});

test('non-JSON error body falls back to the status code', async () => {
  stubStorage();
  globalThis.fetch = recordingFetch(() => ({
    ok: false, status: 502,
    json: async () => { throw new SyntaxError('not json'); },
  }));
  await assert.rejects(api('/x'), (e) => e.message === 'HTTP 502');
});

test('object body is JSON serialized with json content-type and token', async () => {
  stubStorage('tok');
  let captured;
  globalThis.fetch = recordingFetch((path, opts) => {
    captured = opts;
    return { ok: true, json: async () => ({ v: 1 }) };
  });
  await api('/x', { method: 'POST', body: { a: 1 } });
  assert.equal(captured.body, '{"a":1}');
  assert.equal(captured.headers['Content-Type'], 'application/json');
  assert.equal(captured.headers['X-Session-Token'], 'tok');
});

test('string body passes through as-is with json content-type', async () => {
  stubStorage();
  let captured;
  globalThis.fetch = recordingFetch((path, opts) => {
    captured = opts;
    return { ok: true, json: async () => ({}) };
  });
  await api('/x', { method: 'POST', body: 'raw-string' });
  assert.equal(captured.body, 'raw-string');
  assert.equal(captured.headers['Content-Type'], 'application/json');
});

test('legacy numeric signature maps to timeoutMs and aborts fetch', async () => {
  stubStorage();
  globalThis.fetch = (path, opts = {}) => new Promise((resolve, reject) => {
    opts.signal.addEventListener('abort', () => {
      const err = new Error('aborted');
      err.name = 'AbortError';
      reject(err);
    });
  });
  await assert.rejects(api('/x', 50), (e) => e.name === 'AbortError');
});

test('successful responses return parsed json', async () => {
  stubStorage();
  globalThis.fetch = recordingFetch(
    () => ({ ok: true, json: async () => ({ v: 7 }) }));
  assert.deepEqual(await api('/x'), { v: 7 });
});
