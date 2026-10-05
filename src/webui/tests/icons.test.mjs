import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ICONS, FILLED_ICONS, BRAND_ICONS } from '../modules/core/icons.js';
import { buildNav, NAV_LINKS } from '../modules/core/router.js';

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

// 契约：buildNav 请求的每个图标键都必须在 ICONS 中注册。
// icon() 对缺失键静默回退 info，不报错——只有调用方键需求这层契约能拦住「导航图标悄悄错」。
// NAV_LINKS 为 router.js 导出的 links 数据源（buildNav 与本测试共同消费），杜绝源码正则解析的形态脆弱性。
test('every icon key requested by buildNav is registered in ICONS (no silent info fallback)', () => {
  assert.ok(Array.isArray(NAV_LINKS), 'NAV_LINKS 应为数组');
  assert.ok(NAV_LINKS.length >= 7, `NAV_LINKS 应至少含 7 条链接，实际 ${NAV_LINKS.length}`);
  for (const entry of NAV_LINKS) {
    const key = entry[2];
    assert.ok(key in ICONS, `buildNav 请求的图标键未注册: ${key}（icon() 会静默回退 info）`);
  }
});
