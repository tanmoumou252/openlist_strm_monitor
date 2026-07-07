import { icon } from './icons.js';
import { _mainStatusTimer, setMainStatusTimer, stopUptimeTimer } from './state.js';

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
      params[decodeURIComponent(k)] = decodeURIComponent(v || '');
    }
  }
  return { page, params };
}

export function navigate(hash) {
  location.hash = hash;
}

export async function router() {
  const { page, params } = parseHash();
  const mainEl = document.getElementById('app-main');
  const navEl = document.getElementById('main-nav');

  // 离开 dashboard 时停止主程序状态轮询与 uptime 计时器
  if (page !== 'dashboard') {
    if (_mainStatusTimer) {
      clearInterval(_mainStatusTimer);
      setMainStatusTimer(null);
    }
    stopUptimeTimer();
  }

  // Update nav
  navEl.innerHTML = buildNav(page);

  // Update uptime only on dashboard page
  if (page === 'dashboard') {
    const { updateUptime } = await import('../pages/dashboard.js');
    updateUptime();
  }

  // Show loading
  mainEl.innerHTML = '<div class="loading"><div class="spinner"></div> 加载中...</div>';

  try {
    if (page === 'dashboard') {
      const { renderDashboard } = await import('../pages/dashboard.js');
      await renderDashboard(mainEl);
    } else if (page.startsWith('area_')) {
      const { renderArea } = await import('../pages/area.js');
      await renderArea(mainEl, page.replace('area_', ''), params);
    } else if (page === 'tmdb') {
      const { renderTmdb } = await import('../pages/tmdb.js');
      await renderTmdb(mainEl, params);
    } else if (page === 'logs') {
      const { renderLogs } = await import('../pages/logs.js');
      await renderLogs(mainEl);
    } else if (page === 'config') {
      const { renderConfig } = await import('../pages/config.js');
      await renderConfig(mainEl, params);
    } else {
      mainEl.innerHTML = '<div class="error-msg">页面不存在</div>';
    }
  } catch (e) {
    mainEl.innerHTML = `<div class="error-msg">${icon('error')} ${e.message}</div>`;
  }
}
