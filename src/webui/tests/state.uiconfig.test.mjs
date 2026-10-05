import test from 'node:test';
import assert from 'node:assert/strict';

// state.js _setUiConfig/_loadUiConfig 乐观更新与版本回滚竞态契约。
// fetch / localStorage 用替身；AbortController 为 Node 20+ 原生全局，直接使用真实现。

function seqFetch() {
  const calls = [];
  const fn = (path, opts = {}) => new Promise((resolve, reject) => {
    calls.push({ path, opts, resolve, reject });
  });
  fn.calls = calls;
  return fn;
}

function okResp(json = { success: true }) {
  return { ok: true, status: 200, json: async () => json };
}

function badResp(status = 500) {
  return { ok: false, status, statusText: 'boom', json: async () => ({}) };
}

function stubStorage() {
  const store = new Map();
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  };
  return store;
}

function drain() {
  return new Promise((resolve) => setImmediate(resolve));
}

const state = await import('../modules/core/state.js');
const {
  _setUiConfig, _loadUiConfig, _getUiConfig, setUiConfig,
  _tmdbCache, _setCachedWatchlist, _getCachedWatchlist, _tmdbCacheTTL,
  setToken,
} = state;
// _uiConfig 是 export let 重赋值绑定（state.js:37-39，setUiConfig(v) 换新
// 对象、_loadUiConfig 成功分支同样换新）：模块顶部一次性解构只会拿到初始
// {} 的值快照，ESM live binding 不会回溯更新已解构的局部常量。凡断言
// _uiConfig 内容处必须经命名空间属性访问 state._uiConfig 逐次求值。

test('successful save keeps the optimistic value', async () => {
  stubStorage();
  setUiConfig({});
  const f = seqFetch();
  globalThis.fetch = f;
  _setUiConfig('watchlist_anime_only', '1');
  f.calls[0].resolve(okResp());
  await drain();
  assert.equal(state._uiConfig.watchlist_anime_only, '1');
});

test('non-2xx rolls back to the previous value', async () => {
  stubStorage();
  setUiConfig({ watchlist_anime_only: '0' });
  const f = seqFetch();
  globalThis.fetch = f;
  _setUiConfig('watchlist_anime_only', '1');
  f.calls[0].resolve(badResp());
  await drain();
  assert.equal(state._uiConfig.watchlist_anime_only, '0');
});

test('rollback deletes the key when the old value was absent', async () => {
  stubStorage();
  setUiConfig({});
  const f = seqFetch();
  globalThis.fetch = f;
  _setUiConfig('fresh_key', '1');
  f.calls[0].resolve(badResp());
  await drain();
  assert.equal('fresh_key' in state._uiConfig, false);
});

test('abort of a superseded request never rolls back the newer value', async () => {
  stubStorage();
  setUiConfig({});
  const f = seqFetch();
  globalThis.fetch = f;
  _setUiConfig('k', '1'); // A：将被 B abort
  _setUiConfig('k', '0'); // B：abort A 并成为当前乐观值
  f.calls[0].reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
  await drain();
  assert.equal(state._uiConfig.k, '0', 'A 被 abort 后不得回滚覆盖 B 的新值');
  f.calls[1].resolve(okResp());
  await drain();
  assert.equal(state._uiConfig.k, '0');
});

test('a lone AbortError rolls back when value and version are unchanged', async () => {
  stubStorage();
  setUiConfig({ k: 'old' });
  const f = seqFetch();
  globalThis.fetch = f;
  _setUiConfig('k', '1');
  f.calls[0].reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
  await drain();
  assert.equal(state._uiConfig.k, 'old', '孤立 AbortError 且值/版本未变时应回滚');
});

test('a failed save after a successful one rolls back to the last good value', async () => {
  stubStorage();
  setUiConfig({});
  const f = seqFetch();
  globalThis.fetch = f;
  _setUiConfig('a', '1');
  f.calls[0].resolve(okResp());
  await drain();
  _setUiConfig('a', '0');
  f.calls[1].resolve(badResp());
  await drain();
  assert.equal(state._uiConfig.a, '1', '失败请求应回滚到上一成功值');
});

test('_loadUiConfig stores config on success and ignores failure', async () => {
  stubStorage();
  setUiConfig({});
  globalThis.fetch = async () => ({
    ok: true,
    json: async () => ({ success: true, config: { onboarded: '1' } }),
  });
  await _loadUiConfig();
  assert.equal(state._uiConfig.onboarded, '1');
  setUiConfig({});
  globalThis.fetch = async () => ({ ok: false });
  await _loadUiConfig();
  assert.equal('onboarded' in state._uiConfig, false);
});

test('_getUiConfig treats only the string 1 as true', () => {
  setUiConfig({ a: '1', b: '0', c: 1, d: 'x' });
  assert.equal(_getUiConfig('a'), true);
  assert.equal(_getUiConfig('b'), false);
  assert.equal(_getUiConfig('c'), false);
  assert.equal(_getUiConfig('d'), false);
  assert.equal(_getUiConfig('missing'), false);
});

test('watchlist cache honours the 30 minute TTL boundary', () => {
  _setCachedWatchlist('movies', { id: 313369 });
  assert.deepEqual(_getCachedWatchlist('movies'), { id: 313369 });
  _tmdbCache.movies.ts = Date.now() - _tmdbCacheTTL - 1;
  assert.equal(_getCachedWatchlist('movies'), null, '超 TTL 应视为过期');
  _tmdbCache.movies.ts = Date.now() - _tmdbCacheTTL + 1000;
  assert.deepEqual(_getCachedWatchlist('movies'), { id: 313369 }, 'TTL 之内仍新鲜');
});

test('setToken stores a truthy token and removes falsy ones', () => {
  const store = stubStorage();
  setToken('abc');
  assert.equal(store.get('session_token'), 'abc');
  setToken('');
  assert.equal(store.has('session_token'), false);
  setToken(null);
  assert.equal(store.has('session_token'), false);
});
