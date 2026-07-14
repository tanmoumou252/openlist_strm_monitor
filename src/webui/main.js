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

  // Bind onboarding quick button
  const onboardingBtn = document.getElementById('onboarding-quick-btn');
  if (onboardingBtn) {
    onboardingBtn.addEventListener('click', async () => {
      // Reset onboarding status
      try {
        await fetch('/api/webui/config/ui', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ onboarding_completed: '0' })
        });
      } catch (e) {
        console.error('Failed to reset onboarding:', e);
      }
      // Navigate to dashboard to show guide
      navigate('#dashboard');
      // Reload onboarding after a short delay
      setTimeout(() => {
        import('./modules/pages/dashboard.js').then(m => {
          if (m._loadOnboarding) m._loadOnboarding();
        });
      }, 100);
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
          setMainStatusTimer(setInterval(m.updateMainStatus, CONFIG.MAIN_STATUS_POLL_INTERVAL));
        });
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
  fetch('/api/admin/status').then(r => r.json()).then(d => {
    setHasPassword(d.has_password);
  }).catch(() => {
    setHasPassword(false);
  }).finally(() => {
    // 密码状态就绪后，再激活 hashchange 路由，避免 auth guard 竞争条件
    _bindHashchange();
    router();
  });
});
