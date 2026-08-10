import './styles/main.css';
import { syncTheme, initDropdowns, setRouterFn } from './modules/core/theme.js';
import { router, navigate } from './modules/core/router.js';
import { initWallpaperReveal } from './modules/core/wallpaper.js';
import { _loadUiConfig, setTmdbWebBase, _mainStatusTimer, setMainStatusTimer, stopUptimeTimer, startUptimeTimer, CONFIG, setHasPassword } from './modules/core/state.js';
import { _checkApiStatus } from './modules/pages/openlist.js';

// Inject router into theme module to avoid circular dependency
setRouterFn(router);

// Bind hashchange event (will be activated after auth init)
let _hashchangeBound = false;
let _visibilityGeneration = 0;
function _bindHashchange() {
  if (!_hashchangeBound) {
    window.addEventListener('hashchange', router);
    _hashchangeBound = true;
  }
}

// DOMContentLoaded initialization
document.addEventListener('DOMContentLoaded', () => {
  const root = document.documentElement;
  const savedSys = localStorage.getItem('webui_theme_system');
  const savedCol = localStorage.getItem('webui_theme_color');
  const savedFont = localStorage.getItem('webui_theme_fontsize');
  if (savedSys) root.dataset.system = savedSys;
  if (savedCol) root.dataset.color = savedCol;
  if (savedFont && ['lg', 'sm', 'xs'].includes(savedFont)) root.dataset.font = savedFont;
  syncTheme();
  initDropdowns();

  // Bind gear quick button (replaces inline onclick)
  const gearBtn = document.getElementById('gear-quick-btn');
  if (gearBtn) {
    gearBtn.addEventListener('click', () => {
      navigate('#config?sub=openlist');
    });
  }

  // Load UI config from DB
  _loadUiConfig();

  // Load TMDB web base from config
  fetch('/api/config').then(r => r.json()).then(cfg => {
    setTmdbWebBase(cfg.tmdb_host && !cfg.tmdb_host.startsWith('https://api.themoviedb.org')
      ? cfg.tmdb_host : 'https://www.themoviedb.org');
  }).catch(() => { });

  // 页面可见性优化：隐藏时暂停定时器，恢复时重启
  document.addEventListener('visibilitychange', () => {
    const generation = ++_visibilityGeneration;
    if (document.hidden) {
      if (_mainStatusTimer) {
        clearInterval(_mainStatusTimer);
        setMainStatusTimer(null);
      }
      stopUptimeTimer();
    } else if (document.getElementById('uptime-val')) {
      // 仅在 dashboard 页面恢复定时器
      if (!_mainStatusTimer) {
        import('./modules/pages/dashboard.js').then(m => {
          if (generation !== _visibilityGeneration || document.hidden || !document.getElementById('uptime-val')) return;
          setMainStatusTimer(setInterval(m.updateMainStatus, CONFIG.MAIN_STATUS_POLL_INTERVAL));
        }).catch(() => console.warn('[Main] 加载 dashboard 模块失败，uptime 轮询中断'));
      }
      startUptimeTimer();
    }
  });

  // 初始化水墨晕染壁纸遮罩
  initWallpaperReveal();

  // Delay API status check to avoid blocking page render
  setTimeout(() => {
    _checkApiStatus();
  }, 0);

  // 检查管理员密码状态（必须在 router() 之前完成，确保 auth guard 正确）
  // 引导 fetch 无超时 + fail-open
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 10000);

  fetch('/api/admin/status', { signal: controller.signal }).then(r => r.json()).then(d => {
    clearTimeout(timeoutId);
    setHasPassword(d.has_password);
  }).catch(() => {
    clearTimeout(timeoutId);
    setHasPassword(null);
  }).finally(() => {
    // 密码状态就绪后，再激活 hashchange 路由，避免 auth guard 竞争条件
    _bindHashchange();
    router();
  });
});
