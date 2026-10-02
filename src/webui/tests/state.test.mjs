import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  CONFIG, _genreCache, _getGenreCache, _setGenreCache,
} from '../modules/core/state.js';

// 契约：_genreCache 只有上限淘汰（LRU），无 TTL 过期 —— 仅测上限淘汰，不作 TTL 断言
test('genreCache evicts oldest entry when capacity is exceeded', () => {
  _genreCache.clear();
  const cap = CONFIG.MAX_GENRE_CACHE_SIZE;
  for (let i = 0; i < cap; i++) _setGenreCache(`k${i}`, i);
  assert.equal(_genreCache.size, cap);
  _setGenreCache('overflow', -1);
  assert.equal(_genreCache.size, cap);
  assert.equal(_genreCache.has('k0'), false, '最旧条目 k0 应被逐出');
  assert.equal(_genreCache.has('overflow'), true);
});

test('genreCache is true LRU: a read refreshes recency', () => {
  _genreCache.clear();
  const cap = CONFIG.MAX_GENRE_CACHE_SIZE;
  for (let i = 0; i < cap; i++) _setGenreCache(`k${i}`, i);
  // 读取 k0 使其变为最新，再写入新键 → 逐出的应是 k1 而非 k0
  _getGenreCache('k0');
  _setGenreCache('overflow', -1);
  assert.equal(_genreCache.has('k0'), true, '刚被读取的 k0 不应被逐出');
  assert.equal(_genreCache.has('k1'), false, '最久未访问的 k1 应被逐出');
});
