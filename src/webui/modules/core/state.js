// ============================================================
// Shared application state (singleton module)
// ============================================================

// Configuration constants
export const CONFIG = {
  MAIN_STATUS_POLL_INTERVAL: 5000,
  UPTIME_UPDATE_INTERVAL: 1000,
  TMDB_PAGE_SIZE: 50,
  MAX_GENRE_CACHE_SIZE: 1000,
  WATCHLIST_FETCH_RETRY_DELAY: 1000,
};

// OpenList state namespace
export const OpenListState = {
  strmEngines: [{ engine: '', monitored_paths: [] }],
  availableEngines: [],
  apiStatus: 'checking',
  refreshPaths: [],
  abMappings: [],
  configured: false,
};

// Timer handles (reassignable via setters)
export let _serverStartTime = null;
export let _mainStatusTimer = null;
export let _uptimeTimer = null;

export function setServerStartTime(v) { _serverStartTime = v; }
export function setMainStatusTimer(v) { _mainStatusTimer = v; }
export function setUptimeTimer(v) { _uptimeTimer = v; }

// TMDB state
export const _tmdbCache = { movies: null, tv: null };
export const _tmdbCacheTTL = 30 * 60 * 1000;
export const _fetchPromises = {};
export let _uiConfig = {};

export function setUiConfig(v) { _uiConfig = v; }

// Flipped card state (TMDB)
export let _flippedCard = null;
export function setFlippedCard(v) { _flippedCard = v; }

// Auth state (null = uninitialized, false = no password, true = password set)
export let _hasPassword = null;
export function setHasPassword(v) { _hasPassword = v; }
export function setToken(token) {
  if (token) {
    localStorage.setItem('session_token', token);
  } else {
    localStorage.removeItem('session_token');
  }
}

// Cached watchlist helpers
export function _getCachedWatchlist(type) {
  const c = _tmdbCache[type];
  if (c && (Date.now() - c.ts) < _tmdbCacheTTL) return c.data;
  return null;
}
export function _setCachedWatchlist(type, data) {
  _tmdbCache[type] = { data, ts: Date.now() };
}

// UI Config helpers — 使用 AbortController 避免快速连点时回滚覆盖正确值
// 添加版本号机制解决竞态：abort 后不会错误回滚已完成的请求
let _uiConfigController = null;
let _uiConfigVersion = 0;  // 版本号 - 每次成功保存递增，防止 abort 后错误回滚
export async function _loadUiConfig() {
  try {
    const resp = await fetch('/api/webui/config/ui');
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.success && data.config) _uiConfig = data.config;
  } catch (e) { /* ignore */ }
}
export function _getUiConfig(key) {
  return _uiConfig[key] === '1';
}
export function _setUiConfig(key, val) {
  const oldVal = _uiConfig[key];
  const versionBefore = _uiConfigVersion;  // 记录保存前的版本号
  _uiConfig[key] = val;
  // 取消前一次未完成的保存请求
  if (_uiConfigController) {
    // 被新请求 abort 时，前一次请求的乐观更新需要回滚，
    // 否则如果前一次请求在后端实际失败了，前端状态会一直保持乐观更新的错误值。
    // 只有当新请求的值与前一次请求的值不同时，才需要回滚。
    _uiConfigController.abort();
  }
  const ctrl = new AbortController();
  _uiConfigController = ctrl;
  // 超时防护——若后端无响应，10 秒后主动终止，避免无限挂起。
  // 捕获局部 ctrl 而非模块级 _uiConfigController，防止期间新调用
  // 替换控制器后超时误 abort 新请求。
  const timeoutId = setTimeout(() => ctrl.abort(), 10000);
  // 缩进对齐（原列 0，现缩进 2 空格）
  fetch('/api/webui/config/ui', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Session-Token': localStorage.getItem('session_token') || ''
    },
    body: JSON.stringify({ [key]: val }),
    signal: ctrl.signal
  }).then(resp => {
    if (!resp.ok) {
      console.warn('[UI Config] 保存失败:', resp.status, resp.statusText);
      // 只有版本号未变化时才回滚（防止 abort 后的旧请求错误回滚新值）
      if (_uiConfigVersion === versionBefore) {
        if (oldVal === undefined) delete _uiConfig[key]; else _uiConfig[key] = oldVal;
      }
    } else {
      // 保存成功，递增版本号
      _uiConfigVersion++;
    }
  }).catch(err => {
    if (err.name === 'AbortError') {
      // 被新请求 abort 时，若当前值仍是本请求设置的乐观值，则回滚到旧值。
      // 若新请求已覆盖为不同值，则保留新值（避免回滚覆盖新请求的乐观更新）。
      if (_uiConfigVersion === versionBefore && _uiConfig[key] === val) {
        if (oldVal === undefined) delete _uiConfig[key]; else _uiConfig[key] = oldVal;
      }
      return; // 被取消的是上一次请求，忽略
    }
    console.warn('[UI Config] 保存请求失败:', err);
    // 只有版本号未变化时才回滚
    if (_uiConfigVersion === versionBefore) {
      if (oldVal === undefined) delete _uiConfig[key]; else _uiConfig[key] = oldVal;
    }
  }).finally(() => {
    clearTimeout(timeoutId);
  });
}

// Genre cache with LRU eviction
export const _genreCache = new Map();

export function _getGenreCache(key) {
  const val = _genreCache.get(key);
  if (val !== undefined) {
    // 读取时重新插入，更新访问顺序实现真 LRU 淘汰
    _genreCache.delete(key);
    _genreCache.set(key, val);
  }
  return val;
}

export function _setGenreCache(key, value) {
  if (_genreCache.size >= CONFIG.MAX_GENRE_CACHE_SIZE) {
    const firstKey = _genreCache.keys().next().value;
    _genreCache.delete(firstKey);
  }
  _genreCache.set(key, value);
}

// ============================================================
// Uptime timer functions (shared between dashboard and main)
// ============================================================

export function startUptimeTimer() {
  if (!_uptimeTimer) {
    _uptimeTimer = setInterval(updateUptime, CONFIG.UPTIME_UPDATE_INTERVAL);
    setUptimeTimer(_uptimeTimer);
  }
}

export function stopUptimeTimer() {
  if (_uptimeTimer) {
    clearInterval(_uptimeTimer);
    setUptimeTimer(null);
  }
}

export function updateUptime() {
  const el = document.getElementById('uptime-val');
  if (!el) return;
  if (_serverStartTime == null) {
    el.textContent = '-';
    return;
  }
  const sec = Math.floor((Date.now() - _serverStartTime) / 1000);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  el.textContent = `${h}h ${m}m ${s}s`;
}
