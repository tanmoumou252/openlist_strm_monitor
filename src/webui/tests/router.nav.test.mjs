import test from 'node:test';
import assert from 'node:assert/strict';

import { buildNav } from '../modules/core/router.js';
import { ICONS } from '../modules/core/icons.js';

function anchors(html) {
  return [...html.matchAll(/<a href="([^"]*)" data-tab="([^"]*)"( class="active")?>/g)]
    .map((m) => ({ href: m[1], tab: m[2], active: Boolean(m[3]) }));
}

test('buildNav marks exactly one active anchor', () => {
  for (const tab of ['dashboard', 'area_b', 'tmdb', 'config']) {
    const list = anchors(buildNav(tab));
    const active = list.filter((a) => a.active);
    assert.equal(active.length, 1, `tab=${tab}`);
    assert.equal(active[0].tab, tab);
  }
});

test('buildNav emits data-tab for every link in order', () => {
  const list = anchors(buildNav('dashboard'));
  assert.deepEqual(list.map((a) => a.tab),
    ['dashboard', 'area_b', 'area_a', 'area_c', 'tmdb', 'logs', 'config']);
});

test('buildNav appends kind=anime to area links only', () => {
  const list = anchors(buildNav('dashboard'));
  const byTab = new Map(list.map((a) => [a.tab, a.href]));
  assert.equal(byTab.get('area_b'), '#area_b?kind=anime');
  assert.equal(byTab.get('area_a'), '#area_a?kind=anime');
  assert.equal(byTab.get('area_c'), '#area_c?kind=anime');
  assert.equal(byTab.get('dashboard'), '#dashboard');
  assert.equal(byTab.get('tmdb'), '#tmdb');
  assert.equal(byTab.get('logs'), '#logs');
  assert.equal(byTab.get('config'), '#config');
});

test('buildNav injects icon() output into every anchor', () => {
  const html = buildNav('dashboard');
  assert.ok(html.includes(ICONS.dashboard));
  assert.ok(html.includes(ICONS.area_a));
  assert.ok(html.includes(ICONS.area_c));
  assert.ok(html.includes(ICONS.tmdb));
  assert.ok(html.includes(ICONS.log));
  // area_b / config 已在 ICONS 注册专属条目；严禁回退 info（契约见 icons.test.mjs 的 buildNav 键需求测试）
  assert.ok(html.includes(ICONS.area_b));
  assert.ok(html.includes(ICONS.config));
});
