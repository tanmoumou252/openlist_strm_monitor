import { icon } from './icons.js';
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

export function parseHash() {
  const hash = location.hash.slice(1) || 'dashboard';
  const [page, qstr] = hash.split('?');
  const params = {};
  if (qstr) {
    for (const kv of qstr.split('&')) {
      const [k, v] = kv.split('=');
      params[decodeURIComponent(k)] = decodeURIComponent(
        (v || '').replace(/\+/g, '%20'));
    }
  }
  return { page, params };
}

export function navigate(hash) {
  location.hash = hash;
}

/**
 * 渲染代际（F-3）：每次 router() 调用递增。
 * 页面渲染函数可在其内部 await 后调用 isRenderStale() 判断是否已被更新导航取代。
 */
let _renderGen = 0;
let _pageRenderGen = -1;
let _lastPage = null;  // 跟踪上一个页面（不含参数）
export function isRenderStale() {
  return _pageRenderGen !== _renderGen;
}

export async function router() {
  const { page } = parseHash();
  const mainEl = document.getElementById('app-main');
  const navEl = document.getElementById('main-nav');
  // 渲染护栏（F-3）：每次导航递增代际，被 await 挂起的旧渲染在恢复时
  // 发现代际不匹配即中止，避免快速切换页面时旧页覆盖新页 + 定时器泄漏。
  const myGen = ++_renderGen;
  _pageRenderGen = myGen;  // 同步当前渲染代际，使 isRenderStale() 正确工作
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
        // F-4：加超时，避免后端挂起导致导航永久卡死（原本用裸 fetch 无超时）
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 5000);
        const resp = await fetch('/api/admin/status', {
          headers: { 'X-Session-Token': token },
          signal: ctrl.signal,
        });
        clearTimeout(timer);
        if (isStale()) return;  // F-3：验证期间用户已导航到其它页
        if (!resp.ok) {
          localStorage.removeItem('session_token');
          navigate('#login');
          return;
        }
      } catch (e) {
        // 网络错误/超时时放行（后端仍会保护 API）
        console.warn('[Auth] 无法验证登录状态，往后端 API 调用将受限:', e.message);
      }
    }
  }

  const { page: currentPage, params } = parseHash();

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
    if (isStale()) return;  // F-3: 期间发生了新导航，放弃本次渲染
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
    // F-2：api() 检测到 401 时已导航至 #login 并抛出 ApiAuthError，
    // 此处静默抑制，避免把会话过期渲染成错误页闪现。
    if (e instanceof ApiAuthError) return;
    mainEl.innerHTML = `<div class="error-msg">${icon('error')} ${e.message}</div>`;
  }
}
