import { icon } from './icons.js';
import { esc } from './utils.js';
import { _mainStatusTimer, setMainStatusTimer, stopUptimeTimer, _hasPassword, setHasPassword } from './state.js';
import { ApiAuthError } from './api.js';

export function buildNav(activeTab) {
  const links = [
    ['dashboard', '仪表盘', 'dashboard'],
    ['area_b', 'B 区', 'area_b'],
    ['area_a', 'A 区', 'area_a'],
    ['area_c', 'C 区', 'area_c'],
    ['tmdb', 'TMDB', 'tmdb'],
    ['logs', 'WebUI日志', 'log'],
    ['config', '配置', 'config'],
  ];
  const areaLinkMap = { area_b: '?kind=anime', area_a: '?kind=anime', area_c: '?kind=anime' };
  return links.map(([id, label, ic]) => {
    const cls = id === activeTab ? ' class="active"' : '';
    const href = areaLinkMap[id] ? `#${id}${areaLinkMap[id]}` : `#${id}`;
    return `<a href="${href}" data-tab="${id}"${cls}>${icon(ic)} ${label}</a>`;
  }).join('\n');
}

// 畸形编码（如 %zz）会抛 URIError 使 router() 整体中止，回退为原始字符串
const safeDecode = (s) => {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
};

export function parseHash() {
  const hash = location.hash.slice(1) || 'dashboard';
  const [page, qstr] = hash.split('?');
  const params = {};
  if (qstr) {
    for (const kv of qstr.split('&')) {
      const idx = kv.indexOf('=');
      const k = idx === -1 ? kv : kv.slice(0, idx);
      const v = idx === -1 ? '' : kv.slice(idx + 1);
      params[safeDecode(k)] = safeDecode((v || '').replace(/\+/g, '%20'));
    }
  }
  return { page, params };
}

export function navigate(hash) {
  location.hash = hash;
}

function normalizeSpaEntryPath() {
  if (location.pathname !== '/login' && location.pathname !== '/login/') {
    return;
  }
  const url = new URL(location.href);
  url.pathname = '/';
  history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
}

/**
 * 渲染代际：每次 router() 调用递增。
 * 页面渲染函数可在其内部 await 后判断是否已被更新导航取代。
 *
 * 原实现用模块级单变量 _pageRenderGen 表达"每个异步入口各自的代际"，
 * 赋 myGen 时恒 false（护栏空转），赋 -1 后恒 true（整站白屏），两者皆错。
 * 改为代际快照工厂 captureRenderGuard()：每个异步入口在同步起始处捕获
 * 当前代际，得到只对自身生效的 stale 判定闭包。后续 agent 勿再退回模块级
 * 单变量方案（无论赋 myGen 还是 -1 都是错的）。
 */
let _renderGen = 0;
let _lastPage = null;  // 跟踪上一个页面（不含参数）

/**
 * 代际快照工厂：在异步入口的同步起始处调用，返回 isStale() 判定闭包。
 * 该闭包捕获"调用时刻"的 _renderGen，此后若发生新导航（_renderGen 递增）
 * 即认为当前异步流程已过时，应放弃渲染。
 */
export function captureRenderGuard() {
  const g = _renderGen;
  return () => g !== _renderGen;
}

export async function router() {
  normalizeSpaEntryPath();
  const { page } = parseHash();
  const mainEl = document.getElementById('app-main');
  const navEl = document.getElementById('main-nav');
  // 渲染护栏：每次导航递增代际，被 await 挂起的旧渲染在恢复时
  // 发现代际不匹配即中止，避免快速切换页面时旧页覆盖新页 + 定时器泄漏。
  const myGen = ++_renderGen;
  const isStale = () => myGen !== _renderGen;

  // Auth guard：未初始化密码状态时，非登录页跳转至 login 等待初始化
  if (page !== 'login' && _hasPassword === null) {
    navigate('#login');
    return;
  }
  // Auth guard：未登录且有密码时跳转到登录页
  if (page !== 'login') {
    const token = localStorage.getItem('session_token');
    if (_hasPassword && !token) {
      navigate('#login');
      return;
    }
    // 如果有 token，尝试验证（可选：轻量验证）
    if (_hasPassword && token) {
      try {
        // 加超时，避免后端挂起导致导航永久卡死（原本用裸 fetch 无超时）
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 5000);
        const resp = await fetch('/api/admin/status', {
          headers: { 'X-Session-Token': token },
          signal: ctrl.signal,
        });
        clearTimeout(timer);
        if (isStale()) return;  // 验证期间用户已导航到其它页
        if (!resp.ok) {
          localStorage.removeItem('session_token');
          localStorage.setItem('session_token_expired', '1');
          navigate('#login');
          return;
        }
        // 同步前端 _hasPassword 与服务端 has_password 状态
        // 管理员重置密码后，前端需要感知并强制重新登录
        const data = await resp.json();
        if (data && typeof data.has_password === 'boolean' && data.has_password !== _hasPassword) {
          console.warn('[Auth] 服务端密码状态已变更，清除会话并重新登录');
          localStorage.removeItem('session_token');
          setHasPassword(data.has_password);
          navigate('#login');
          return;
        }
      } catch (e) {
        if (isStale()) return;  // stale validation catch → do not delete fresh token
        // 网络错误/超时时拒绝放行（fail-closed），跳转登录页
        console.warn('[Auth] 无法验证登录状态，跳转至登录页:', e.message);
        localStorage.removeItem('session_token');
        navigate('#login');
        return;
      }
    }
  }

  const { params } = parseHash();

  // 离开 dashboard 时停止主程序状态轮询与 uptime 计时器
  if (page !== 'dashboard') {
    if (_mainStatusTimer) {
      clearInterval(_mainStatusTimer);
      setMainStatusTimer(null);
    }
    stopUptimeTimer();
  }

  // Update nav (login page 不显示顶部导航栏)
  if (page !== 'login') {
    navEl.innerHTML = buildNav(page);
  } else {
    navEl.innerHTML = '';
  }

  // Update uptime only on dashboard page
  if (page === 'dashboard') {
    const { updateUptime } = await import('../pages/dashboard.js');
    if (isStale()) return;  // 期间发生了新导航，放弃本次渲染
    updateUptime();
  }

  // 只在页面真正变化时显示 loading spinner（问题3：同页面参数变化不刷新）
  const pageChanged = _lastPage !== page;
  if (pageChanged) {
    // Show loading
    mainEl.innerHTML = '<div class="loading"><div class="spinner"></div> 加载中...</div>';
    _lastPage = page;
  }

  try {
    if (page === 'login') {
      const { renderLogin } = await import('../pages/login.js');
      if (isStale()) return;
      await renderLogin(mainEl);
    } else if (page === 'dashboard') {
      const { renderDashboard } = await import('../pages/dashboard.js');
      if (isStale()) return;
      await renderDashboard(mainEl);
    } else if (page.startsWith('area_')) {
      const { renderArea } = await import('../pages/area.js');
      if (isStale()) return;
      await renderArea(mainEl, page.replace('area_', ''), params);
    } else if (page === 'tmdb') {
      const { renderTmdb } = await import('../pages/tmdb.js');
      if (isStale()) return;
      await renderTmdb(mainEl, params);
    } else if (page === 'logs') {
      const { renderLogs } = await import('../pages/logs.js');
      if (isStale()) return;
      await renderLogs(mainEl);
    } else if (page === 'config') {
      const { renderConfig } = await import('../pages/config.js');
      if (isStale()) return;
      await renderConfig(mainEl, params);
    } else {
      mainEl.innerHTML = '<div class="error-msg">页面不存在</div>';
    }
  } catch (e) {
    // api() 检测到 401 时已导航至 #login 并抛出 ApiAuthError，
    // 此处静默抑制，避免把会话过期渲染成错误页闪现。
    if (e instanceof ApiAuthError) return;
    mainEl.innerHTML = `<div class="error-msg">${icon('error')} ${esc(e.message)}</div>`;
  }
}
