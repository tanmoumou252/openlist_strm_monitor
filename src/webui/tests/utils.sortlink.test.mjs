import test from 'node:test';
import assert from 'node:assert/strict';

import { createSortLink, formatTimestamp } from '../modules/core/utils.js';
import { ICONS } from '../modules/core/icons.js';

test('createSortLink flips asc to desc on the active column', () => {
  const html = createSortLink('b', 'title', 'asc', '标题', 'title');
  assert.ok(html.includes('#area_b?sort=title&order=desc'));
  assert.ok(html.includes(ICONS.arrow_up));
});

test('createSortLink flips desc and foreign columns back to asc', () => {
  assert.ok(createSortLink('b', 'title', 'desc', '标题', 'title')
    .includes('#area_b?sort=title&order=asc'));
  const other = createSortLink('b', 'size', 'asc', '标题', 'title');
  assert.ok(other.includes('#area_b?sort=title&order=asc'));
  assert.ok(!other.includes(ICONS.arrow_up));
  assert.ok(!other.includes(ICONS.arrow_down));
});

test('createSortLink encodes query params', () => {
  const html = createSortLink('a', 'title', 'asc', '标题', 'title',
    { kind: '番剧', q: 'a b&c', media: 'tv' });
  assert.ok(html.includes('kind=' + encodeURIComponent('番剧')));
  assert.ok(html.includes('q=' + encodeURIComponent('a b&c')));
  assert.ok(html.includes('media=' + encodeURIComponent('tv')));
});

test('createSortLink emits page_size only for positive integers', () => {
  const cases = [
    [{ page_size: 30 }, true],
    [{ page_size: '30' }, true],
    [{ page_size: '30.5' }, false],
    [{ page_size: '-1' }, false],
    [{ page_size: 0 }, false],
    [{}, false],
  ];
  for (const [params, expected] of cases) {
    const html = createSortLink('b', 'title', 'asc', '标题', 'title', params);
    assert.equal(html.includes('page_size='), expected, JSON.stringify(params));
  }
});

test('formatTimestamp tiers relative labels', () => {
  const secAgo = (s) => Math.floor((Date.now() - s * 1000) / 1000);
  assert.equal(formatTimestamp(null), '未知');
  assert.equal(formatTimestamp(0), '未知');
  assert.equal(formatTimestamp(secAgo(2)), '刚刚');
  assert.equal(formatTimestamp(secAgo(90)), '1分钟前');
  assert.equal(formatTimestamp(secAgo(5400)), '1小时前');
  assert.equal(formatTimestamp(secAgo(3 * 86400 + 43200)), '3天前');
});

test('formatTimestamp falls back to a local date beyond 7 days', () => {
  const text = formatTimestamp(Math.floor((Date.now() - (8 * 86400 + 43200) * 1000) / 1000));
  assert.match(text, /\d{4}/);
  assert.ok(!text.endsWith('前'));
});
