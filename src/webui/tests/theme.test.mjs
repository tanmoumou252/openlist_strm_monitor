import test from 'node:test';
import assert from 'node:assert/strict';

// theme.js 函数体内引用 document / localStorage / window —— 以 globalThis
// 替身 stub（沿用 api.test.mjs 的替身先例，零 npm 依赖）。

function makeEl(opts = {}) {
  const el = {
    id: opts.id || '',
    role: opts.role || '',
    dataset: opts.dataset || {},
    classSet: new Set(opts.classes || []),
    innerHTML: '',
    style: {},
    listeners: {},
    children: opts.children || [],
    addEventListener(type, fn) {
      (el.listeners[type] = el.listeners[type] || []).push(fn);
    },
    querySelector(sel) {
      if (sel === '.dropdown-btn') {
        return el.children.find((c) => c.role === 'btn') || null;
      }
      return null;
    },
    querySelectorAll(sel) {
      if (sel === '.dropdown-item') {
        return el.children.filter((c) => c.role === 'item');
      }
      return [];
    },
  };
  el.classList = {
    add: (c) => el.classSet.add(c),
    remove: (c) => el.classSet.delete(c),
    contains: (c) => el.classSet.has(c),
    toggle: (c, force) => {
      const target = force === undefined ? !el.classSet.has(c) : force;
      if (target) el.classSet.add(c);
      else el.classSet.delete(c);
      return el.classSet.has(c);
    },
  };
  return el;
}

function item(menu, val) {
  return makeEl({ role: 'item', dataset: { val }, menu });
}

// 搭建 syncTheme / initDropdowns 所需的最小 DOM 替身并装载模块。
async function loadTheme(dom) {
  const calls = { wallpaperResize: 0 };
  globalThis.document = {
    documentElement: dom.root,
    querySelectorAll: (sel) => {
      // bySelector 的值可以是静态数组，也可以是动态解析函数
      //（如 '.dropdown-wrap.open' 依赖当前 open 类集合，不能注册时快照）
      const v = dom.bySelector.get(sel);
      return typeof v === 'function' ? v() : (v || []);
    },
    getElementById: (id) => dom.byId.get(id) || null,
    addEventListener(type, fn) {
      (this.docListeners = this.docListeners || []);
      this.docListeners.push(fn);
    },
    createElement: () => makeEl(),
  };
  const store = new Map();
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
  };
  globalThis.window = {
    _wallpaperResize: () => { calls.wallpaperResize += 1; },
  };
  const mod = await import('../modules/core/theme.js');
  return { mod, calls, store };
}

function baseDom() {
  const root = { dataset: { system: 'material', color: 'blue', font: 'sm' } };
  const sysItems = [item('system', 'material'), item('system', 'fluent')];
  const colorItems = [item('color', 'blue'), item('color', 'purple')];
  const fontItems = [item('font', 'sm'), item('font', 'lg')];
  const bySelector = new Map([
    ['#theme-system-menu .dropdown-item', sysItems],
    ['#theme-color-menu .dropdown-item', colorItems],
    ['#theme-fontsize-menu .dropdown-item', fontItems],
  ]);
  const colorDot = makeEl();
  const fsIcon = makeEl();
  const presetIcon = makeEl();
  const byId = new Map([
    ['color-dot-icon', colorDot],
    ['fontsize-icon', fsIcon],
    ['theme-preset-icon', presetIcon],
  ]);
  return { root, bySelector, byId, sysItems, colorItems, fontItems,
    colorDot, fsIcon, presetIcon };
}

const GRID_MARKER = 'M120-520';
const PALETTE_MARKER = 'M480-80q';

test('syncTheme marks only the active dropdown items', async () => {
  const dom = baseDom();
  const { mod } = await loadTheme(dom);
  mod.syncTheme();
  assert.ok(dom.sysItems[0].classList.contains('active'));
  assert.ok(!dom.sysItems[1].classList.contains('active'));
  assert.ok(dom.colorItems[0].classList.contains('active'));
  assert.ok(!dom.colorItems[1].classList.contains('active'));
  assert.ok(dom.fontItems[0].classList.contains('active'));
  assert.ok(!dom.fontItems[1].classList.contains('active'));
});

test('syncTheme follows root dataset changes', async () => {
  const dom = baseDom();
  const { mod } = await loadTheme(dom);
  dom.root.dataset.system = 'fluent';
  dom.root.dataset.font = 'lg';
  mod.syncTheme();
  assert.ok(dom.sysItems[1].classList.contains('active'));
  assert.ok(!dom.sysItems[0].classList.contains('active'));
  assert.ok(dom.fontItems[1].classList.contains('active'));
});

test('syncTheme persists theme selections to localStorage', async () => {
  const dom = baseDom();
  const { mod, store } = await loadTheme(dom);
  mod.syncTheme();
  assert.equal(store.get('webui_theme_system'), 'material');
  assert.equal(store.get('webui_theme_color'), 'blue');
  assert.equal(store.get('webui_theme_fontsize'), 'sm');
});

test('syncTheme applies known color and falls back for unknown', async () => {
  const dom = baseDom();
  const { mod } = await loadTheme(dom);
  mod.syncTheme();
  assert.equal(dom.colorDot.style.background, '#1a73e8');
  dom.root.dataset.color = 'pink';
  mod.syncTheme();
  assert.equal(dom.colorDot.style.background, 'var(--primary)');
});

test('syncTheme applies font size map with fallback', async () => {
  const dom = baseDom();
  const { mod } = await loadTheme(dom);
  dom.root.dataset.font = 'lg';
  mod.syncTheme();
  assert.equal(dom.fsIcon.style.fontSize, '15px');
  dom.root.dataset.font = 'xxl';
  mod.syncTheme();
  assert.equal(dom.fsIcon.style.fontSize, '13px');
});

test('syncTheme switches preset icon between fluent and material', async () => {
  const dom = baseDom();
  const { mod } = await loadTheme(dom);
  dom.root.dataset.system = 'fluent';
  mod.syncTheme();
  assert.ok(dom.presetIcon.innerHTML.includes(GRID_MARKER));
  dom.root.dataset.system = 'material';
  mod.syncTheme();
  assert.ok(dom.presetIcon.innerHTML.includes(PALETTE_MARKER));
  assert.ok(!dom.presetIcon.innerHTML.includes(GRID_MARKER));
});

test('syncTheme triggers wallpaper resize hook when present', async () => {
  const dom = baseDom();
  const { mod, calls } = await loadTheme(dom);
  mod.syncTheme();
  assert.equal(calls.wallpaperResize, 1);
});

function dropdownDom() {
  const dom = baseDom();
  const wrap = (id, ddId) => makeEl({
    id,
    children: [makeEl({ role: 'btn' }),
      item(ddId, 'material'), item(ddId, 'fluent')],
  });
  dom.wraps = [
    wrap('theme-system-dd', 'system'),
    wrap('theme-color-dd', 'color'),
  ];
  dom.bySelector.set('.dropdown-wrap', dom.wraps);
  // theme.js 的关兄弟（按钮 click 处理器）与外点关闭（document 监听器）
  // 均按 '.dropdown-wrap.open' 查询而非 '.dropdown-wrap'，须动态解析
  // 当前开着的 wrap 集合，静态数组无法反映运行期类变化。
  dom.bySelector.set('.dropdown-wrap.open',
    () => dom.wraps.filter((w) => w.classSet.has('open')));
  return dom;
}

test('initDropdowns button click toggles wrap and closes siblings', async () => {
  const dom = dropdownDom();
  const { mod } = await loadTheme(dom);
  mod.initDropdowns();
  const [first, second] = dom.wraps;
  second.classSet.add('open');
  first.children[0].listeners.click[0]({ stopPropagation() {} });
  assert.ok(first.classSet.has('open'));
  assert.ok(!second.classSet.has('open'));
  first.children[0].listeners.click[0]({ stopPropagation() {} });
  assert.ok(!first.classSet.has('open'));
});

test('initDropdowns item click writes dataset and closes the wrap', async () => {
  const dom = dropdownDom();
  const { mod } = await loadTheme(dom);
  mod.initDropdowns();
  const sysWrap = dom.wraps[0];
  sysWrap.classSet.add('open');
  const fluentItem = sysWrap.children[2];
  fluentItem.listeners.click[0]({ stopPropagation() {} });
  assert.equal(dom.root.dataset.system, 'fluent');
  assert.ok(!sysWrap.classSet.has('open'));
});

test('initDropdowns outside click closes all open dropdowns', async () => {
  const dom = dropdownDom();
  const { mod } = await loadTheme(dom);
  mod.initDropdowns();
  dom.wraps.forEach((w) => w.classSet.add('open'));
  const outside = globalThis.document.docListeners[0];
  outside({});
  dom.wraps.forEach((w) => assert.ok(!w.classSet.has('open')));
});
