import { api } from '../core/api.js';
import { icon } from '../core/icons.js';
import { esc, renderTmdbResults } from '../core/utils.js';
import { showToast } from '../components/toast.js';
import { showCacheStaleModal } from '../components/dialog.js';
import { navigate, isRenderStale } from '../core/router.js';
import {
  CONFIG, _getCachedWatchlist, _setCachedWatchlist, _fetchPromises,
  _tmdbWebBase, _getUiConfig, _flippedCard, setFlippedCard,
  _getGenreCache, _setGenreCache
} from '../core/state.js';

const _POSTER_FALLBACK_SVG = '<svg class="tt-poster-fallback-svg" viewBox="0 0 60 90" fill="none"><rect x="2" y="2" width="56" height="86" rx="4" stroke="currentColor" stroke-width="1.5" opacity="0.35"/><path d="M22 32v26l18-13-18-13z" fill="currentColor" opacity="0.25"/></svg>';

function _renderSeasonBars(count) {
  if (count <= 1) return '';
  const palette = ['bar-green', 'bar-blue', 'bar-purple'];
  const numBars = count >= 5 ? 3 : (count >= 3 ? 2 : 1);
  return palette.slice(0, numBars).map(function (c) {
    return '<span class="tmdb-season-bar ' + c + '"></span>';
  }).join('');
}

async function _lazyLoadSeasonCount(wrapper) {
  const id = wrapper.dataset.tmdbId;
  const type = wrapper.dataset.tmdbType;
  if (type !== 'tv' || !id) return;
  if (wrapper.dataset.seasonLoaded) return;
  wrapper.dataset.seasonLoaded = '1';
  if (wrapper.querySelector('.tmdb-season-bars')) return;
try {
	    const data = await api('/api/tmdb/season-count/' + type + '/' + id);
	    const count = data.season_count || 0;
    if (count > 1) {
      const bars = document.createElement('div');
      bars.className = 'tmdb-season-bars';
      bars.innerHTML = _renderSeasonBars(count);
      wrapper.insertBefore(bars, wrapper.firstChild);
    }
  } catch (e) { /* silent */ }
}

function _loadPoster(wrapper) {
  const posterWrap = wrapper.querySelector('.tt-poster-wrap');
  if (!posterWrap) return;
  const posterUrl = posterWrap.dataset.poster;
  if (!posterUrl) {
    posterWrap.innerHTML = `<div class="tt-poster-error">${_POSTER_FALLBACK_SVG}<span class="tt-poster-retry-text">暂无海报</span></div>`;
    return;
  }
  posterWrap.innerHTML = '<div class="tt-poster-loading"><div class="spinner"></div></div>';
  const img = new Image();
  img.className = 'tt-poster';
  img.alt = 'poster';
  img.referrerPolicy = 'no-referrer';
  img.onload = () => {
    const loading = posterWrap.querySelector('.tt-poster-loading');
    if (loading) loading.replaceWith(img);
  };
  img.onerror = () => {
    posterWrap.innerHTML = `<div class="tt-poster-error" data-poster="${esc(posterUrl)}">${_POSTER_FALLBACK_SVG}<span class="tt-poster-retry-text">稍后重试</span></div>`;
  };
  img.src = posterUrl;
}

function _flipBack(id) {
  const wrapper = document.querySelector(`.tmdb-flip-wrapper[data-tmdb-id="${id}"]`);
  if (wrapper) {
    wrapper.classList.remove('flipped');
    const backFace = wrapper.querySelector('.tmdb-flip-back');
    if (backFace) backFace.style.backgroundImage = '';
  }
  if (_flippedCard === id) setFlippedCard(null);
}

async function _loadGenres(tmdbId, type) {
  const cacheKey = `${type}_${tmdbId}`;
  const container = document.getElementById(`genre-${tmdbId}`);
  if (!container) return;
  const cachedGenres = _getGenreCache(cacheKey);
  if (cachedGenres) { _renderGenres(container, cachedGenres); return; }
try {
	    const data = await api('/api/tmdb/genres/' + type + '/' + tmdbId);
	    const genres = data.genres || [];
    _setGenreCache(cacheKey, genres);
    _renderGenres(container, genres);
  } catch (e) {
    container.innerHTML = `<span class="tt-tag tt-empty">暂无信息</span>`;
  }
}

function _renderGenres(container, genres) {
  if (!genres.length) { container.innerHTML = `<span class="tt-tag tt-empty">暂无分类</span>`; return; }
  container.innerHTML = genres.map(g => {
    const truncated = g.length > 4 ? g.substring(0, 4) + '…' : g;
    const titleAttr = g.length > 4 ? ` title="${esc(g)}"` : '';
    return `<span class="tt-tag tt-genre"${titleAttr}>${esc(truncated)}</span>`;
  }).join('');
}

function _flipForward(wrapper, id, type) {
  wrapper.classList.add('flipped');
  setFlippedCard(id);
  _loadGenres(id, type);
  _loadPoster(wrapper);
  const backdropUrl = wrapper.dataset.backdrop;
  const backFace = wrapper.querySelector('.tmdb-flip-back');
  if (backdropUrl && backFace) backFace.style.backgroundImage = `url(${backdropUrl})`;
}

function _initFlipCards() {
  document.querySelectorAll('.tmdb-flip-wrapper').forEach(wrapper => {
    const id = wrapper.dataset.tmdbId;
    const type = wrapper.dataset.tmdbType;
    _lazyLoadSeasonCount(wrapper);
    wrapper.addEventListener('click', (e) => {
      if (e.target.closest('.tt-poster-error[data-poster]')) { _loadPoster(wrapper); return; }
      if (e.target.closest('a')) return;
      if (wrapper.classList.contains('flipped')) {
        _flipBack(id);
      } else {
        if (_flippedCard && _flippedCard !== id) _flipBack(_flippedCard);
        _flipForward(wrapper, id, type);
      }
    });
  });
}

export async function renderTmdb(el, params) {
  const [status, config] = await Promise.all([
    api('/api/tmdb/status'),
    api('/api/config')
  ]);

  const watchlistEnabledRaw = config.tmdb_watchlist_enabled;
  const watchlistDisabled = watchlistEnabledRaw === false || watchlistEnabledRaw === 'false';
  if (watchlistDisabled) {
    el.innerHTML = `
<h2 class="page-header">${icon('tmdb', 'ui-icon-lg')} TMDB 待看列表</h2>
<div class="tmdb-unconfigured-notice">
  ${icon('warn')} TMDB 待看列表已禁用。请在配置页 → WebUI/TMDB 中启用。
</div>`;
    return;
  }

  if (!status.configured) {
    el.innerHTML = `
<h2 class="page-header">${icon('tmdb', 'ui-icon-lg')} TMDB 待看列表</h2>
<div class="tmdb-unconfigured-notice">
  ${icon('warn')} 未配置 TMDB access_token，请在配置页面 → TMDB 设置中填入 access_token。
</div>`;
    return;
  }

  const neverRemind = _getUiConfig('tmdb_cache_never_remind');
  if (status.cache_stale && !neverRemind) {
    showCacheStaleModal(status.cache_item_count || 0, status.cache_last_sync || 0);
  }

  if (status.match_uncomputed > 0 && status.match_total > 0 && !_getUiConfig('tmdb_match_toast_disabled')) {
    showToast(`${status.match_uncomputed} / ${status.match_total} 个条目收录状态未计算，建议点击配置页「刷新收录状态」`, 'info');
  }

  const mediaType = params.type || 'movie';
  const page = parseInt(params.page) || 1;
  const q = params.q || '';
  const statusFilter = params.status || '';

  const apiType = mediaType === 'movie' ? 'movies' : mediaType;
  const cached = _getCachedWatchlist(apiType);
  let data;
  if (cached) {
    data = cached;
  } else if (_fetchPromises[apiType]) {
    try { data = await _fetchPromises[apiType]; }
    catch (e) {
      _fetchPromises[apiType] = null;
      const retryUrl = `/api/tmdb/watchlist/${apiType}?all=1`;
      data = await api(retryUrl);
      _setCachedWatchlist(apiType, data);
    }
  } else {
    const url = `/api/tmdb/watchlist/${apiType}?all=1`;
    const promise = api(url);
    _fetchPromises[apiType] = promise;
    try {
      data = await promise;
      _setCachedWatchlist(apiType, data);
    } finally {
      setTimeout(() => {
        if (_fetchPromises[apiType] === promise) _fetchPromises[apiType] = null;
      }, 1000);
    }
  }
  let items = data.results || [];

  // 使用后端 FTS5 搜索（带 LIKE 回退），避免前端内存过滤与后端分词语义不一致。
  // 注意：FTS5 过滤结果不写入缓存，缓存始终保存完整列表以保证状态统计准确。
  if (q) {
    try {
      const filtered = await api(`/api/tmdb/watchlist/${apiType}?all=1&q=${encodeURIComponent(q)}`);
      items = filtered.results || [];
    } catch (e) {
      // 后端搜索失败时回退到完整列表（不再做前端内存过滤）
      items = data.results || [];
    }
  }

  if (statusFilter && ['in', 'out', 'que'].includes(statusFilter)) {
    items = items.filter(it => (it._status || 'out') === statusFilter);
  }

  const pageSize = CONFIG.TMDB_PAGE_SIZE;
  const total = items.length;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const curPage = Math.min(Math.max(page, 1), totalPages);
  const pageItems = items.slice((curPage - 1) * pageSize, curPage * pageSize);

  const statusCounts = { in: 0, out: 0, que: 0 };
  (data.results || []).forEach(it => { statusCounts[it._status || 'out']++; });

  const _tmdbOfficialBase = 'https://www.themoviedb.org';
  const avatarHash = status.avatar_path || '';
  const avatarUrl = avatarHash ? `/api/tmdb/avatar?hash=${encodeURIComponent(avatarHash)}` : '';
  const username = status.username || data.account_id || '';
  const _TMDB_LOGO_HEADER_CDN = 'https://www.themoviedb.org/assets/2/v4/logos/v2/blue_square_2-d537fb228cf3ded904ef09b136fe3fec72548ebc1fea3fbbd1ad9e36364db38b.svg';
  const _TMDB_LOGO_CARD_CDN = 'https://www.themoviedb.org/assets/2/v4/logos/v2/blue_square_1-5bdc75aaebeb75dc7ae79426ddd9be3b2be1e342510f8202baf6bffa71d7f5c4.svg';
  const _TMDB_LOGO_FALLBACK = 'data:image/svg+xml,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" fill="none"><rect width="32" height="32" rx="4" fill="#01B4E4"/><text x="50%" y="54%" dominant-baseline="central" text-anchor="middle" fill="white" font-family="Arial,sans-serif" font-weight="700" font-size="12">TMDB</text></svg>');

  function _resolveTmdbLogoUrl(s, kind) {
    const asset = kind === 'header' ? _TMDB_LOGO_HEADER_CDN : _TMDB_LOGO_CARD_CDN;
    if (s.host && !s.host.startsWith('https://api.themoviedb.org')) {
      return s.host.replace(/\/+$/, '') + asset.replace('https://www.themoviedb.org', '');
    }
    return asset;
  }

  const tmdbLogoUrl = _resolveTmdbLogoUrl(status, 'header');
  const avatarLinkUrl = username ? `${_tmdbOfficialBase}/u/${username}/watchlist` : '#';

  let html = `<div class="status-legend" style="justify-content:flex-start;align-items:center;gap:8px;padding:10px 14px">
  <div style="display:flex;align-items:center;gap:8px;flex-shrink:0">
<h2 class="tmdb-header-title" style="font-size:17px;margin:0;color:var(--text-main);display:flex;align-items:center;gap:8px;white-space:nowrap"><span class="tmdb-header-logo-box"><img src="${esc(tmdbLogoUrl)}" alt="TMDB" class="tmdb-header-logo" onerror="this.src='${_TMDB_LOGO_FALLBACK}'" loading="lazy"></span><span>待看列表</span></h2>
    ${avatarUrl ? `<a href="${esc(avatarLinkUrl)}" target="_blank" rel="noopener" title="查看待看列表" style="line-height:0;display:flex"><img class="tmdb-avatar" src="${avatarUrl}" alt="avatar" referrerpolicy="no-referrer"></a>` : ''}
  </div>
  <div style="flex:1;display:flex;justify-content:center;gap:6px;flex-wrap:wrap">`;
  const legendItems = [['in', '已收录', 'badge_in'], ['out', '未收录', 'badge_out'], ['que', '存疑', 'badge_que']];
  legendItems.forEach(([sv, sl, si]) => {
    const activeCls = statusFilter === sv ? ' active' : '';
    const href = `#tmdb?type=${mediaType}&status=${sv}`;
    html += `<a class="legend-badge ${sv}${activeCls}" href="${href}">${icon(si)} ${sl} (${statusCounts[sv] || 0})</a>`;
  });
  html += '</div>';
  html += '<div style="display:flex;align-items:center;gap:6px;flex-shrink:0">';
  html += '<span style="width:1px;height:20px;background:color-mix(in srgb,var(--border-color) 30%,transparent)"></span>';
  html += `<a class="type-tab${mediaType === 'movie' ? ' active' : ''}" href="#tmdb?type=movie" style="padding:4px 10px">${icon('movie')} 电影</a>`;
  html += `<a class="type-tab${mediaType === 'tv' ? ' active' : ''}" href="#tmdb?type=tv" style="padding:4px 10px;margin-left:0">${icon('tv')} 剧集</a>`;
  html += `<span style="color:var(--text-muted);margin:0 4px">·</span>`;
  html += `<span class="tmdb-page-info" style="padding:4px 10px">第 ${curPage}/${totalPages} 页 · 共 ${total} 项</span>`;
  html += `<span style="width:1px;height:16px;background:color-mix(in srgb,var(--border-color) 30%,transparent);flex-shrink:0"></span>`;
html += `<button class="tmdb-export-btn" data-export="csv" title="导出 CSV">${icon('csv')}</button>`;
	  html += `<button class="tmdb-export-btn" data-export="json-movie" title="电影 JSON">${icon('json')}</button>`;
	  html += `<button class="tmdb-export-btn" data-export="json-tv" title="剧集 JSON">${icon('tv')}</button>`;
	  html += '</div></div>';

  html += `<div class="toolbar"><div class="search-wrap">${icon('search', 'search-prefix')}<input type="text" id="tmdb-search" placeholder="搜索待看列表..." value="${esc(q)}"></div><button class="search-btn" id="tmdb-search-btn">${icon('search')} 搜索</button></div>`;

  const stateMap = { in: '已收录', out: '未收录', que: '有疑问' };
  const stateIconMap = { in: 'badge_in', out: 'badge_out', que: 'badge_que' };
  const statusToMatchStatus = { in: 'matched', out: 'unmatched', que: 'fuzzy' };
  html += '<div class="tmdb-grid">';
  if (pageItems.length === 0 && q) {
    html += `<div class="empty-search-state" style="height:200px;display:flex;align-items:center;justify-content:center;color:var(--text-muted);font-size:14px;grid-column:1/-1">暂无搜索结果</div>`;
  } else {
  pageItems.forEach(item => {
    const title = item.title || item.name || 'N/A';
    const originalTitle = item.original_title || item.original_name || '';
    const date = item.release_date || item.first_air_date || '';
    const rating = item.vote_average || 0;
    const overview = item.overview || '';
    const posterPath = item.poster_path || '';
    const tmdbId = item.id;
    const st = item._status || 'out';
    const isManual = item._is_manual || false;
    const stLabel = stateMap[st] || '未收录';
    const stIcon = stateIconMap[st] || 'help';
    const seasonCount = item._season_count || 0;
    const seasonBarsHtml = seasonCount > 1 ? `<div class="tmdb-season-bars">${_renderSeasonBars(seasonCount)}</div>` : '';
    const backdropPath = item.backdrop_path || '';
    const posterUrl = posterPath ? `/api/tmdb/poster?path=${encodeURIComponent(posterPath)}&w=342` : '';
    const backdropUrl = backdropPath ? `/api/tmdb/poster?path=${encodeURIComponent(backdropPath)}&w=780` : '';
    const detailUrl = `${_tmdbOfficialBase}/${mediaType}/${tmdbId}`;
    const manualBadge = isManual ? `<span class="tmdb-manual-badge" title="手动设置">${icon('edit')}</span>` : '';
    html += `<div class="tmdb-flip-wrapper" data-tmdb-id="${tmdbId}" data-tmdb-type="${mediaType}" data-backdrop="${esc(backdropUrl || '')}">
  ${seasonBarsHtml}
  <!-- Front face -->
     <div class="tmdb-flip-front">
       <div class="title">${esc(title)}</div>
       <div class="meta">
         <span class="tmdb-rating-block">
           <span class="tmdb-rating-label">评分</span>
           <svg class="tmdb-rating-icon" viewBox="0 0 24 24" width="12" height="12" fill="currentColor"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>
           <span class="tmdb-rating-value">${rating.toFixed(1)}</span>
         </span>
         · ${esc(date)}
          <span class="tmdb-card-status-inline ${st}">${icon(stIcon)} ${esc(stLabel)}${manualBadge}</span>
          <a class="tmdb-jump-btn" href="${esc(detailUrl)}" target="_blank" rel="noopener" title="前往 TMDB 查看详情">
            <img class="tmdb-jump-logo" src="${esc(_resolveTmdbLogoUrl(status, 'card'))}" alt="TMDB" onerror="this.src='${_TMDB_LOGO_FALLBACK}'" loading="lazy">
           <svg class="tmdb-jump-icon" viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
         </a>
       </div>

    <div class="tmdb-overview">${esc(overview)}</div>
  </div>
  <!-- Back face (flip) -->
  <div class="tmdb-flip-back">
    <div class="tt-layout">
      <div class="tt-poster-wrap" data-poster="${posterUrl || ''}">
        ${posterUrl ? '<div class="tt-poster-placeholder"></div>' : ''}
      </div>
      <div class="tt-info">
        <div class="tt-title">${esc(title)}</div>
        ${originalTitle && originalTitle !== title ? `<div class="tt-original">${esc(originalTitle)}</div>` : ''}
        <div class="tt-tags">
          <span class="tt-tag">${esc(stLabel)}</span>
          ${rating > 0 ? `<span class="tt-tag">\u2605 ${rating.toFixed(1)}</span>` : ''}
          ${date ? `<span class="tt-tag">${esc(date)}</span>` : ''}
        </div>
        <div class="tt-genre-tags" id="genre-${tmdbId}">
          <span class="tt-tag tt-loading">加载中…</span>
        </div>
      </div>
    </div>
    <div class="tt-overview-wrap">
      <div class="tt-overview">${esc(overview)}</div>
    </div>
    <div class="tt-override-section">
      <div class="tt-override-label">手动设置收录状态：</div>
      <div class="tt-override-segmented" data-tmdb-id="${tmdbId}" data-tmdb-type="${mediaType}">
        <button class="seg-btn seg-in${st === 'in' ? ' active' : ''}" data-status="matched" title="标记为已收录">${icon('badge_in')} 已收录</button>
        <button class="seg-btn seg-que${st === 'que' ? ' active' : ''}" data-status="fuzzy" title="标记为存疑">${icon('badge_que')} 存疑</button>
        <button class="seg-btn seg-out${st === 'out' ? ' active' : ''}" data-status="unmatched" title="标记为未收录">${icon('badge_out')} 未收录</button>
      </div>
      ${isManual ? `<button class="tt-restore-auto-btn tmdb-restore-auto-btn" data-tmdb-id="${tmdbId}" data-tmdb-type="${mediaType}" title="清除人工覆盖，恢复自动判断">恢复自动判断</button>` : ''}
    </div>
  </div>
</div>`;
  });
  }
  html += '</div>';

  const sp = statusFilter ? '&status=' + statusFilter : '';
  const qp = q ? '&q=' + encodeURIComponent(q) : '';
  html += '<div class="pager">';
  if (curPage > 1) html += `<a class="pager-btn" href="#tmdb?type=${mediaType}&page=${curPage - 1}${qp}${sp}">${icon('chevron_l')} 上一页</a>`;
  html += `<span class="tmdb-page-info">第 ${curPage} / ${totalPages} 页</span>`;
  if (curPage < totalPages) html += `<a class="pager-btn" href="#tmdb?type=${mediaType}&page=${curPage + 1}${qp}${sp}">下一页 ${icon('chevron_r')}</a>`;
  html += '</div>';

  // TMDB 在线搜索结果容器（位于分页器之后，匹配 area.js 的 DOM 顺序）
  if (q) {
    html += `<div id="tmdb-search-results"></div>`;
  }

  el.innerHTML = html;

  document.getElementById('tmdb-search-btn').addEventListener('click', () => {
    const val = document.getElementById('tmdb-search').value.trim();
    let h = `#tmdb?type=${mediaType}`;
    if (val) h += '&q=' + encodeURIComponent(val);
    navigate(h);
  });
  document.getElementById('tmdb-search').addEventListener('keydown', e => {
    if (e.key === 'Enter') document.getElementById('tmdb-search-btn').click();
  });

  // 有搜索词时，同时查询 TMDB 在线结果
  if (q) {
    const searchContainer = document.getElementById('tmdb-search-results');
    api(`/api/tmdb/search?query=${encodeURIComponent(q)}`)
      .then(results => {
        if (isRenderStale()) return;  // 双保险 1：页面代际校验
        renderTmdbResults(results, "你可能还在找", q, searchContainer);  // 双保险 2：container.isConnected 在函数内校验
      })
      .catch(() => {
        if (isRenderStale()) return;
        showToast('TMDB 在线搜索失败，请稍后重试', 'error');
      });
  }

  _initFlipCards();

  // 手动覆盖按钮事件
  document.querySelectorAll('.tt-override-segmented').forEach(segmented => {
    segmented.addEventListener('click', async (e) => {
      const btn = e.target.closest('.seg-btn');
      if (!btn) return;
      e.stopPropagation(); // 防止触发卡片翻转
      
      const tmdbId = segmented.dataset.tmdbId;
      const mediaType = segmented.dataset.tmdbType;
      const newStatus = btn.dataset.status;
      
      // 更新 UI 状态
      segmented.querySelectorAll('.seg-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      
      // 调用 API
      try {
        const response = await api('/api/tmdb/watchlist/match/override', {
          method: 'POST',
          body: JSON.stringify({
            media_type: mediaType,
            id: parseInt(tmdbId),
            status: newStatus,
            reason: 'manual_override'
          })
        });
        
        if (response.success) {
          showToast('收录状态已更新', 'success');
          // 刷新页面数据
          setTimeout(() => window.location.reload(), 800);
        } else {
          showToast('更新失败: ' + (response.message || '未知错误'), 'error');
        }
      } catch (err) {
        showToast('更新失败: ' + err.message, 'error');
      }
    });
  });

  // "恢复自动判断"按钮事件
  document.querySelectorAll('.tt-restore-auto-btn').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const tmdbId = btn.dataset.tmdbId;
      const mediaType = btn.dataset.tmdbType;
      try {
        const response = await api('/api/tmdb/watchlist/match/clear', {
          method: 'POST',
          body: JSON.stringify({
            media_type: mediaType,
            id: parseInt(tmdbId)
          })
        });
        if (response.success) {
          showToast('人工覆盖已清除，将在下次刷新时重新计算', 'success');
          // 局部刷新：通过 hash 跳转触发 SPA 重新渲染当前页
          const cur = window.location.hash;
          window.location.hash = '#tmdb';
          if (cur !== '#tmdb') window.location.hash = cur;
          else window.dispatchEvent(new HashChangeEvent('hashchange'));
        } else {
          showToast('清除失败: ' + (response.message || '未知错误'), 'error');
        }
      } catch (err) {
        showToast('清除失败: ' + err.message, 'error');
      }
    });
  });

  // 导出按钮 — 使用带 token 的请求下载文件
  document.querySelectorAll('.tmdb-export-btn[data-export]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const exportType = btn.dataset.export;
      const token = localStorage.getItem('session_token');
      try {
        if (exportType === 'csv') {
          // CSV 导出：响应是 Blob，用 fetch 手动带 token
          const resp = await fetch('/api/tmdb/watchlist/export.csv', {
            headers: token ? { 'X-Session-Token': token } : {}
          });
          if (!resp.ok) throw new Error('导出失败');
          const blob = await resp.blob();
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          a.download = 'tmdb_watchlist.csv';
          a.click();
          URL.revokeObjectURL(url);
        } else {
          // JSON 导出：movies 或 tv
          const apiType = exportType === 'json-movie' ? 'movies' : 'tv';
          const data = await api(`/api/tmdb/watchlist/${apiType}?all=1`);
          const json = JSON.stringify(data, null, 2);
          const blob = new Blob([json], { type: 'application/json' });
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          a.download = `tmdb_watchlist_${apiType}.json`;
          a.click();
          URL.revokeObjectURL(url);
        }
      } catch (e) {
        showToast('导出失败: ' + e.message, 'error');
      }
    });
  });
}
