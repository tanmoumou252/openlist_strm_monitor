import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseHash } from '../modules/core/router.js';

// parseHash 读取 location.hash；Node 无 location，测试内注入替身
function withHash(hash, fn) {
  const prev = globalThis.location;
  globalThis.location = { hash };
  try {
    fn();
  } finally {
    globalThis.location = prev ?? undefined;
  }
}

// ① 空 hash 回退 dashboard
test('empty hash falls back to dashboard', () => {
  withHash('', () => {
    const { page, params } = parseHash();
    assert.equal(page, 'dashboard');
    assert.deepEqual(params, {});
  });
});

// ② value 中 + 视作 %20（仅 value，key 不替换）
test('plus in value is treated as space, key keeps plus', () => {
  withHash('#p?a=hello+world', () => {
    const { page, params } = parseHash();
    assert.equal(page, 'p');
    assert.equal(params.a, 'hello world');
  });
  withHash('#p?k+ey=v', () => {
    const { params } = parseHash();
    assert.ok('k+ey' in params, 'key 不做加号替换');
  });
});

// ③ kv 无 = 时 value 为空串
test('kv without = yields empty string value', () => {
  withHash('#p?flag', () => {
    const { params } = parseHash();
    assert.deepEqual(params, { flag: '' });
  });
});

// ④ 畸形编码经 safeDecode 回退原串不抛错
test('malformed percent encoding falls back to raw string without throwing', () => {
  withHash('#p?bad=%zz', () => {
    const { params } = parseHash();
    assert.equal(params.bad, '%zz');
  });
});

// ⑤ page 段不解码（原样返回 split 后首段）
test('page segment is not decoded', () => {
  withHash('#pa%20ge?x=1', () => {
    const { page } = parseHash();
    assert.equal(page, 'pa%20ge');
  });
});

// ⑥ value 解码前对 falsy 值归一为空串再走 +/解码管线
test('falsy value is normalized to empty string before decoding', () => {
  withHash('#p?k=', () => {
    const { params } = parseHash();
    assert.equal(params.k, '');
  });
  withHash('#p?x=1&k', () => {
    const { params } = parseHash();
    assert.equal(params.k, '');
  });
});
