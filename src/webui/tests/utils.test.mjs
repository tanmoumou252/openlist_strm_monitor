import { test } from 'node:test';
import assert from 'node:assert/strict';
import { esc, createField, _formatTimeAgo, fmtTime } from '../modules/core/utils.js';

// esc：契约 = HTML 实体转义 & < > " ' 五字符
test('esc escapes the five HTML entities', () => {
  assert.equal(esc('&'), '&amp;');
  assert.equal(esc('<'), '&lt;');
  assert.equal(esc('>'), '&gt;');
  assert.equal(esc('"'), '&quot;');
  assert.equal(esc("'"), '&#39;');
});

test('esc escapes a mixed string without leaving raw metacharacters', () => {
  const out = esc(`<a href="x">&'y'`);
  assert.equal(out, '&lt;a href=&quot;x&quot;&gt;&amp;&#39;y&#39;');
  // 无裸标签与裸引号残留（实体内的 & 不算未转义）
  assert.doesNotMatch(out, /<(?![a-z]+;)/);
});

test('esc null and empty return empty string', () => {
  assert.equal(esc(null), '');
  assert.equal(esc(undefined), '');
  assert.equal(esc(''), '');
});

// createField：契约 = label + input 结构，数值属性四项透传
test('createField renders label and input structure', () => {
  const html = createField('f1', '标签', 'v');
  assert.match(html, /<label[^>]*data-role="label"/);
  assert.match(html, /<input[^>]*id="f1"/);
});

test('createField passes through min/max/step/inputMode attributes', () => {
  const html = createField('f2', '标签', '10', {
    min: '1', max: '100', step: '1', inputMode: 'numeric',
  });
  assert.match(html, /min="1"/);
  assert.match(html, /max="100"/);
  assert.match(html, /step="1"/);
  assert.match(html, /inputmode="numeric"/);
});

test('createField omits numeric attributes when not provided', () => {
  const html = createField('f3', '标签', '');
  assert.doesNotMatch(html, /min="/);
  assert.doesNotMatch(html, /max="/);
  assert.doesNotMatch(html, /step="/);
  assert.doesNotMatch(html, /inputmode="/);
});

// _formatTimeAgo：契约 = 秒/分钟/小时/天 四档
test('_formatTimeAgo tiers by elapsed seconds', () => {
  const now = Math.floor(Date.now() / 1000);
  assert.equal(_formatTimeAgo(now - 30), '30 秒前');
  assert.equal(_formatTimeAgo(now - 59), '59 秒前');
  assert.equal(_formatTimeAgo(now - 60), '1 分钟前');
  assert.equal(_formatTimeAgo(now - 120), '2 分钟前');
  assert.equal(_formatTimeAgo(now - 3600), '1 小时前');
  assert.equal(_formatTimeAgo(now - 7200), '2 小时前');
  assert.equal(_formatTimeAgo(now - 86400), '1 天前');
  assert.equal(_formatTimeAgo(now - 172800), '2 天前');
});

// fmtTime：契约 = falsy 返回 '-'，其余格式化为 MM-DD HH:mm 展示
test('fmtTime returns dash for falsy input', () => {
  assert.equal(fmtTime(0), '-');
  assert.equal(fmtTime(null), '-');
});

test('fmtTime produces MM-DD HH:mm display format', () => {
  assert.match(fmtTime(1700000000), /^\d{2}-\d{2} \d{2}:\d{2}$/);
});
