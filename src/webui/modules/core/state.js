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
export let _tmdbWebBase = 'https://www.themoviedb.org';
export let _uiConfig = {};

export function setTmdbWebBase(v) { _tmdbWebBase = v; }
export function setUiConfig(v) { _uiConfig = v; }

// Flipped card state (TMDB)
export let _flippedCard = null;
export function setFlippedCard(v) { _flippedCard = v; }

// Cached watchlist helpers
export function _getCachedWatchlist(type) {
  const c = _tmdbCache[type];
  if (c && (Date.now() - c.ts) < _tmdbCacheTTL) return c.data;
  return null;
}
export function _setCachedWatchlist(type, data) {
  _tmdbCache[type] = { data, ts: Date.now() };
}

// UI Config helpers
export async function _loadUiConfig() {
  try {
    const resp = await fetch('/api/webui/config/ui');
    const data = await resp.json();
    if (data.success && data.config) _uiConfig = data.config;
  } catch (e) { /* ignore */ }
}
export function _getUiConfig(key) {
  return _uiConfig[key] === '1';
}
export function _setUiConfig(key, val) {
  _uiConfig[key] = val;
  fetch('/api/webui/config/ui', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ [key]: val })
  }).catch(() => { });
}

// Genre cache with LRU eviction
export const _genreCache = new Map();

export function _getGenreCache(key) {
  return _genreCache.get(key);
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
