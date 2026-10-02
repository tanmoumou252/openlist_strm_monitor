import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ICONS, FILLED_ICONS, BRAND_ICONS } from '../modules/core/icons.js';

// 契约：图标 key 集非空且无 undefined 值（防漏注册）
test('ICONS exports a non-empty key set with no undefined values', () => {
  const keys = Object.keys(ICONS);
  assert.ok(keys.length > 0, 'ICONS 不应为空');
  for (const key of keys) {
    assert.notEqual(ICONS[key], undefined, `图标 ${key} 值缺失`);
    assert.equal(typeof ICONS[key], 'string', `图标 ${key} 应为字符串 SVG`);
    assert.ok(ICONS[key].includes('<svg'), `图标 ${key} 应为内联 SVG`);
  }
});

test('FILLED_ICONS and BRAND_ICONS only reference existing ICONS keys', () => {
  for (const key of FILLED_ICONS) {
    assert.ok(key in ICONS, `FILLED_ICONS 引用了未注册图标: ${key}`);
  }
  for (const key of BRAND_ICONS) {
    assert.ok(key in ICONS, `BRAND_ICONS 引用了未注册图标: ${key}`);
  }
});
