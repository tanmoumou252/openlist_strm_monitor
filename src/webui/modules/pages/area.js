import { api } from '../core/api.js';
import { icon } from '../core/icons.js';
import { esc, fmtTime, createSortLink, renderTmdbResults } from '../core/utils.js';
import { navigate, isRenderStale } from '../core/router.js';
import { showToast } from '../components/toast.js';
import { showConfirmDialog } from '../components/dialog.js';

/** 根据 B 区状态返回 CSS class 名称 */
function _statusClass(status) {
  const map = { valid: 'status-valid', duplicate: 'status-duplicate', quarantined: 'status-quarantined' };
  return map[status] || '';
}

export async function renderArea(el, area, params) {
  const media = params.media || '';
  if (media) {
    await renderAreaDetail(el, area, params);
  } else {
    await renderAreaList(el, area, params);
  }
}

async function renderAreaList(el, area, params) {
  const kind = params.kind || 'anime';
  const q = params.q || '';
  const sort = params.sort || 'name';
  const order = params.order || 'asc';
  const page = parseInt(params.page) || 1;
  const pageSize = parseInt(params.page_size) || 50;
  let url = `/api/area/${area}`;
  const qs = [];
  qs.push('kind=' + encodeURIComponent(kind));
  if (q) qs.push('q=' + encodeURIComponent(q));
  if (sort) qs.push('sort=' + encodeURIComponent(sort));
  if (order) qs.push('order=' + encodeURIComponent(order));
  qs.push('page=' + page);
  qs.push('page_size=' + pageSize);
  if (qs.length) url += '?' + qs.join('&');

  const d = await api(url);
  const kindLabel = d.kind_label;

  // Category tabs
  // 根据是否有搜索词动态生成 tab 列表：搜索时增加"全部"tab（跨分类），非搜索时只有番剧/电影
  const kinds = q
    ? [
        { v: 'all', l: '全部', i: 'grid_view' },
        { v: 'anime', l: '番剧', i: 'tv' },
        { v: 'movie', l: '电影', i: 'movie' }
      ]
    : [
        { v: 'anime', l: '番剧', i: 'tv' },
        { v: 'movie', l: '电影', i: 'movie' }
      ];
  const tabsHtml = kinds.map(k => {
    const active = (kind || '') === k.v ? ' active' : '';
    // tab href 保留搜索词，点击 tab 切分类时 q 不丢失
    const href = `#area_${area}?kind=${k.v}&sort=${sort}&order=${order}${q ? '&q=' + encodeURIComponent(q) : ''}`;
    // "全部"tab 计数用 d.total（跨分类去重总数）；后端 kind_counts 无 all 键，直接读会恒为 0
    const count = k.v === 'all' ? (d.total || 0) : (d.kind_counts[k.v] || 0);
    return `<button class="category-tab${active}" data-kind-href="${href}">${icon(k.i)} ${k.l} <span class="count">${count}</span></button>`;
  }).join('');

  // Sort link helper
  function sortLink(colName, colKey) {
    return createSortLink(area, sort, order, colName, colKey, { kind, q });
  }

  // Media cards
  const _kindIconMap = { '番剧': 'tv', '动漫': 'tv', '动画': 'tv', '电影': 'movie' };
  const areaLabels = { a: 'A 区', b: 'B 区', c: 'C 区' };
  const areaLabel = areaLabels[area] || area.toUpperCase() + ' 区';
  let cardsHtml = '';
  if (d.media_items.length === 0 && q) {
    cardsHtml = `<div class="empty-search-state" style="height:200px;display:flex;align-items:center;justify-content:center;color:var(--text-muted);font-size:14px">${esc(areaLabel)}暂无搜索结果</div>`;
  } else {
    cardsHtml = d.media_items.map(item => {
      const href = `#area_${area}?media=${encodeURIComponent(item.name)}${kind ? '&kind=' + encodeURIComponent(kind) : ''}${q ? '&q=' + encodeURIComponent(q) : ''}`;
      const cardIcon = _kindIconMap[item.kind] || 'tv';
      return `<a class="media-card" href="${href}">${icon(cardIcon)}<div class="title">${esc(item.name)}</div><div class="meta">${item.count} 个文件</div></a>`;
    }).join('');
  }

  // Pager
  let pagerHtml = '';
  if (d.total_pages > 1) {
    pagerHtml = '<div class="pager">';
    if (d.page > 1) {
      pagerHtml += `<a href="#area_${area}?page=${d.page - 1}&sort=${sort}&order=${order}${kind ? '&kind=' + encodeURIComponent(kind) : ''}${q ? '&q=' + encodeURIComponent(q) : ''}">${icon('chevron_l')} 上一页</a>`;
    }
    pagerHtml += `<span class="current">第 ${d.page} / ${d.total_pages} 页 (共 ${d.total} 项)</span>`;
    if (d.page < d.total_pages) {
      pagerHtml += `<a href="#area_${area}?page=${d.page + 1}&sort=${sort}&order=${order}${kind ? '&kind=' + encodeURIComponent(kind) : ''}${q ? '&q=' + encodeURIComponent(q) : ''}">下一页 ${icon('chevron_r')}</a>`;
    }
    pagerHtml += '</div>';
  }

  el.innerHTML = `
<h2 class="page-header">${icon(kind === 'movie' ? 'movie' : 'tv', 'ui-icon-lg')} ${esc(kindLabel)} 媒体浏览</h2>
<div class="category-tabs">${tabsHtml}</div>
<div class="toolbar">
  <div class="search-wrap">${icon('search', 'search-prefix')}<input type="text" id="media-search" placeholder="搜索媒体名..." value="${esc(q)}"></div>
  <button class="search-btn" id="area-search-btn">${icon('search')} 搜索</button>
  <span style="font-size:calc(var(--font-base) - 1px);color:var(--text-muted);margin-left:8px">排序:</span>
  ${sortLink('名称', 'name')}
  ${sortLink('文件数', 'count')}
  ${sortLink('时间', 'time')}
  <select id="page-size-select" style="margin-left:auto;padding:4px 8px;border:1px solid var(--border-color);border-radius:var(--radius-control);background:var(--bg-control);color:var(--text-main);font-size:calc(var(--font-base) - 1px)">
    <option value="50"${d.page_size === 50 ? ' selected' : ''}>50 条/页</option>
    <option value="100"${d.page_size === 100 ? ' selected' : ''}>100 条/页</option>
    <option value="200"${d.page_size === 200 ? ' selected' : ''}>200 条/页</option>
  </select>
</div>
<div class="media-grid">${cardsHtml}</div>
${pagerHtml}
<div id="tmdb-search-results"></div>`;

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

  // Bind search
  document.getElementById('area-search-btn').addEventListener('click', () => {
    const val = document.getElementById('media-search').value.trim();
    if (val === (q || '')) return;  // 值未变化则不导航（原守卫位于 navigate 之后为死代码，已移到前面）
    let h = `#area_${area}?sort=${sort}&order=${order}`;
    const p = [];
    if (val) {
      // 有搜索词 → 跨分类搜索（全部 tab）
      p.push('kind=all');
      p.push('q=' + encodeURIComponent(val));
    } else {
      // 清空搜索 → 回到当前分类浏览（非"全部"）
      p.push('kind=' + encodeURIComponent(kind === 'all' ? 'anime' : kind));
    }
    if (p.length) h += '&' + p.join('&');
    navigate(h);
  });
  document.getElementById('media-search').addEventListener('keydown', e => {
    if (e.key === 'Enter') document.getElementById('area-search-btn').click();
  });
  // Bind category tabs
  document.querySelectorAll('.category-tab[data-kind-href]').forEach(tab => {
    tab.addEventListener('click', (e) => {
      e.preventDefault();
      navigate(tab.dataset.kindHref);
    });
  });
  // Bind page size selector
  document.getElementById('page-size-select')?.addEventListener('change', (e) => {
    const newSize = e.target.value;
    const newHash = `#area_${area}?kind=${encodeURIComponent(kind)}&sort=${sort}&order=${order}&page_size=${newSize}&page=1`;
    navigate(newHash);
  });
}

async function renderAreaDetail(el, area, params) {
  const media = params.media;
  const kind = params.kind || '';
  const sort = params.sort || 'name';
  const order = params.order || 'asc';
  const page = parseInt(params.page) || 1;
  const q = params.q || '';
  const mappingIdParam = params.mapping_id || '';

  let url = `/api/area/${area}/detail?media=${encodeURIComponent(media)}`;
  if (sort) url += '&sort=' + encodeURIComponent(sort);
  if (order) url += '&order=' + encodeURIComponent(order);
  if (kind) url += '&kind=' + encodeURIComponent(kind);
  url += '&page=' + page;
  if (mappingIdParam) url += '&mapping_id=' + encodeURIComponent(mappingIdParam);

  const d = await api(url);

  function stripPath(p, root) {
    if (root && p.startsWith(root)) return p.slice(root.length);
    return p;
  }

  const kindPart = kind ? '?kind=' + encodeURIComponent(kind) : '';
  const areaLabels = { a: 'A 区', b: 'B 区', c: 'C 区' };
  const areaLabel = areaLabels[area] || area.toUpperCase() + ' 区';
  
  // Task 2: 检测是否为多 mapping 场景
  const isMultiMapping = d.mappings && Array.isArray(d.mappings) && d.mappings.length > 0;
  
  // Fix R4: expandBtns 提到分支外统一渲染一次
  const expandBtns = `<div class="detail-actions">
  <button class="toolbar-btn" id="expand-all-btn" style="display:inline-flex;align-items:center;gap:4px;background:color-mix(in srgb,var(--primary) 10%,transparent);border:1px solid color-mix(in srgb,var(--primary) 30%,transparent);border-radius:var(--radius-control);padding:6px 14px;color:var(--primary);font-size:calc(var(--font-base) - 1px);font-weight:500;cursor:pointer;font-family:inherit">${icon('expand')} 展开全部</button>
  <button class="toolbar-btn" id="collapse-all-btn" style="display:inline-flex;align-items:center;gap:4px;background:color-mix(in srgb,var(--primary) 10%,transparent);border:1px solid color-mix(in srgb,var(--primary) 30%,transparent);border-radius:var(--radius-control);padding:6px 14px;color:var(--primary);font-size:calc(var(--font-base) - 1px);font-weight:500;cursor:pointer;font-family:inherit">${icon('collapse')} 折叠全部</button>
  </div>`;

  let html = `
<div class="toolbar" style="gap:12px">
  <a href="#area_${area}${kindPart}" class="back-icon-btn" title="返回列表">${icon('back')}</a>
  <span style="color:var(--text-main);font-size:14px;font-weight:600">${esc(media)}</span>
  <span style="color:var(--text-muted);font-size:calc(var(--font-base) - 1px)">· ${d.total} 个文件</span>
  ${(area === 'a' || area === 'b') ? `<button class="toolbar-btn" id="refresh-media-btn" data-mapping-id="${mappingIdParam}" style="display:inline-flex;align-items:center;gap:4px;background:color-mix(in srgb,var(--primary) 10%,transparent);border:1px solid color-mix(in srgb,var(--primary) 30%,transparent);border-radius:var(--radius-control);padding:6px 14px;color:var(--primary);font-size:calc(var(--font-base) - 1px);font-weight:500;cursor:pointer;font-family:inherit">${icon('refresh')} 刷新</button>` : ''}
</div>`;

  // Task 2: 多 mapping 场景渲染
  if (isMultiMapping) {
    // 为每个 mapping 渲染独立分区
    for (const mapping of d.mappings) {
      const mappingId = mapping.mapping_id || 'unknown';
      const localRoot = mapping.local_root || '';
      const webdavRoot = mapping.webdav_root || '';
      const strmEngineRoot = mapping.strm_engine_root || '';
      const indexMetadata = mapping.index_metadata;
      
      // Mapping 分区标题
      html += `<div style="margin:16px 0 8px;padding:12px;background:var(--bg-surface-variant);border-radius:8px;border:1px solid var(--border-subtle)">`;
      html += `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">`;
      html += `<span style="font-weight:600;color:var(--text-primary)">${mappingId === 'unknown' ? '未知映射' : '映射 ' + mappingId}</span>`;
      if (indexMetadata && indexMetadata.mapping_index_generation) {
        html += `<span style="font-size:11px;color:var(--text-muted)">索引 #${indexMetadata.mapping_index_generation} · ${indexMetadata.mapping_index_generation_at ? _formatTimestamp(indexMetadata.mapping_index_generation_at) : '未索引'}</span>`;
      }
      html += `</div>`;
      
      // 路径信息
      html += `<div class="area-path-block" style="font-size:12px">`;
      if (localRoot) html += `<div class="path-line"><span class="path-label">${areaLabel} 本地根：</span><span class="path-value mono">${esc(localRoot)}</span></div>`;
      if (webdavRoot) html += `<div class="path-line"><span class="path-label">WebDAV 根：</span><span class="path-value mono">${esc(webdavRoot)}</span></div>`;
      if (strmEngineRoot) html += `<div class="path-line"><span class="path-label">STRM 入口：</span><span class="path-value mono">${esc(strmEngineRoot)}</span></div>`;
      html += `</div>`;
      
      // Fix R5: 多 mapping 模式下每个分区渲染独立刷新按钮
      if (area === 'a' || area === 'b') {
        html += `<div class="toolbar" style="margin-top:8px;justify-content:flex-end"><button class="toolbar-btn" id="refresh-media-btn" data-mapping-id="${mappingId}" style="display:inline-flex;align-items:center;gap:4px;background:color-mix(in srgb,var(--primary) 10%,transparent);border:1px solid color-mix(in srgb,var(--primary) 30%,transparent);border-radius:var(--radius-control);padding:6px 14px;color:var(--primary);font-size:calc(var(--font-base) - 1px);font-weight:500;cursor:pointer;font-family:inherit">${icon('refresh')} 刷新</button></div>`;
      }
      html += `</div>`;
      
      // 季分组（独立）
      html += _renderSeasons(area, mapping.seasons || [], sort, order, kind, q, media, localRoot, webdavRoot, mappingId);
      
      // 分页（独立）
      if (mapping.total_pages > 1) {
        html += '<div class="pager">';
        if (mapping.page > 1) {
          html += `<a href="#area_${area}?media=${encodeURIComponent(media)}&page=${mapping.page - 1}&sort=${sort}&order=${order}${kind ? '&kind=' + encodeURIComponent(kind) : ''}&mapping_id=${mappingId}">${icon('chevron_l')} 上一页</a>`;
        }
        html += `<span class="current">第 ${mapping.page} / ${mapping.total_pages} 页</span>`;
        if (mapping.page < mapping.total_pages) {
          html += `<a href="#area_${area}?media=${encodeURIComponent(media)}&page=${mapping.page + 1}&sort=${sort}&order=${order}${kind ? '&kind=' + encodeURIComponent(kind) : ''}&mapping_id=${mappingId}">下一页 ${icon('chevron_r')}</a>`;
        }
        html += '</div>';
      }
    }
  } else {
    // 单 mapping 或旧 API（向后兼容）
    const localRoot = d.local_root || '';
    const webdavRoot = d.webdav_root || '';
    const strmEngineRoot = d.strm_engine_root || '';
    const mappingId = d.mapping_id || '';
    const indexMetadata = d.index_metadata;
    
    // 索引元数据（单 mapping）
    if (indexMetadata && indexMetadata.mapping_index_generation) {
      html += `<div style="margin:8px 0;padding:8px 12px;background:var(--bg-surface-variant);border-radius:6px;font-size:12px;color:var(--text-secondary)">`;
      html += `索引代次 #${indexMetadata.mapping_index_generation} · `;
      html += `最近索引: ${indexMetadata.mapping_index_generation_at ? _formatTimestamp(indexMetadata.mapping_index_generation_at) : '未索引'}`;
      html += `</div>`;
    }
    
    // 路径信息
    if (localRoot || webdavRoot || strmEngineRoot) {
      html += `<div class="area-detail-head"><div class="area-path-block">`;
      if (localRoot) html += `<div class="path-line"><span class="path-label">${areaLabel} 本地根：</span><span class="path-value mono">${esc(localRoot)}</span></div>`;
      if (webdavRoot) html += `<div class="path-line"><span class="path-label">WebDAV 根：</span><span class="path-value mono">${esc(webdavRoot)}</span></div>`;
      if (strmEngineRoot) html += `<div class="path-line"><span class="path-label">STRM 入口：</span><span class="path-value mono">${esc(strmEngineRoot)}</span></div>`;
      html += `</div>${expandBtns}</div>`;
    } else {
      html += `<div class="area-detail-head" style="justify-content:flex-end">${expandBtns}</div>`;
    }
    
    // 季分组
    html += _renderSeasons(area, d.seasons || [], sort, order, kind, q, media, localRoot, webdavRoot, mappingId);
    
    // 分页
    if (d.total_pages > 1) {
      html += '<div class="pager">';
      if (d.page > 1) {
        html += `<a href="#area_${area}?media=${encodeURIComponent(media)}&page=${d.page - 1}&sort=${sort}&order=${order}${kind ? '&kind=' + encodeURIComponent(kind) : ''}">${icon('chevron_l')} 上一页</a>`;
      }
      html += `<span class="current">第 ${d.page} / ${d.total_pages} 页</span>`;
      if (d.page < d.total_pages) {
        html += `<a href="#area_${area}?media=${encodeURIComponent(media)}&page=${d.page + 1}&sort=${sort}&order=${order}${kind ? '&kind=' + encodeURIComponent(kind) : ''}">下一页 ${icon('chevron_r')}</a>`;
      }
      html += '</div>';
    }
  }

  el.innerHTML = html;

  // Bind expand/collapse
  const expandAllBtn = document.getElementById('expand-all-btn');
  const collapseAllBtn = document.getElementById('collapse-all-btn');
  const setDetailToggleState = (opened) => {
    expandAllBtn?.classList.toggle('is-active', opened);
    collapseAllBtn?.classList.toggle('is-active', !opened);
  };
  expandAllBtn?.addEventListener('click', () => {
    document.querySelectorAll('.season-details').forEach(d => d.setAttribute('open', ''));
    setDetailToggleState(true);
  });
  collapseAllBtn?.addEventListener('click', () => {
    document.querySelectorAll('.season-details').forEach(d => d.removeAttribute('open'));
    setDetailToggleState(false);
  });
  setDetailToggleState(document.querySelectorAll('.season-details').length > 0);

  // 绑定刷新按钮事件（支持多 mapping 模式下的多个刷新按钮）
  document.querySelectorAll('#refresh-media-btn').forEach(refreshBtn => {
    const btnMappingId = refreshBtn.dataset.mappingId || '';
    const doRefresh = async () => {
      refreshBtn.disabled = true;
      refreshBtn.innerHTML = `${icon('loading')} 刷新中...`;
      try {
        const body = { media };
        if (btnMappingId) body.mapping_id = btnMappingId;
        const result = await api(`/api/area/${area}/refresh`, {
          method: 'POST',
          body: JSON.stringify(body)
        });

        if (result.ok) {
          const msg = result.message || '刷新完成';
          showToast(msg, 'success');
          // 自动刷新页面数据
          await renderAreaDetail(el, area, params);
        } else {
          showToast(`刷新失败：${result.error || '未知错误'}`, 'error');
        }
      } catch (err) {
        showToast(`刷新请求失败：${err.message}`, 'error');
      } finally {
        refreshBtn.disabled = false;
        refreshBtn.innerHTML = `${icon('refresh')} 刷新`;
      }
    };

    refreshBtn.addEventListener('click', () => {
      showConfirmDialog(
        '刷新媒体数据',
        `将触发 STRM 引擎重新生成并同步。<br><br>媒体：${esc(media)}<br><br>是否继续？`,
        async () => { await doRefresh(); },
        null
      );
    });
  });
}

// Task 2: 渲染季分组和记录表
function _renderSeasons(area, seasons, sort, order, kind, q, media, localRoot, webdavRoot, mappingId) {
  function stripPath(p, root) {
    if (root && p.startsWith(root)) return p.slice(root.length);
    return p;
  }
  
  function sortLink(colName, colKey) {
    const params = { kind, q, media };
    if (mappingId) params.mapping_id = mappingId;
    return createSortLink(area, sort, order, colName, colKey, params);
  }
  
  let html = '';
  for (const season of seasons) {
    html += `<details class="season-details" open><summary>${esc(season.label)} <span style="font-size:calc(var(--font-base) - 1px);color:var(--text-muted)">(${season.records.length} 个文件)</span></summary>`;
    html += '<div class="table-wrap"><table><thead><tr><th>序号</th>';

    if (area === 'a') {
      html += `<th>${sortLink('本地路径', 'local_path')}</th><th>WebDAV 路径</th><th>${sortLink('时间', 'updated_at')}</th>`;
    } else if (area === 'b') {
      html += `<th>${sortLink('本地路径', 'local_path')}</th><th>WebDAV 路径</th><th>指纹</th><th>状态</th><th>${sortLink('时间', 'updated_at')}</th>`;
    } else if (area === 'c') {
      html += `<th>${sortLink('本地路径', 'local_path')}</th><th>WebDAV 路径</th><th>原 B 路径</th><th>幽灵根</th><th>${sortLink('时间', 'moved_at')}</th>`;
    }

    html += '</tr></thead><tbody>';

    const recordParts = [];
    season.records.forEach((r, i) => {
      let row = '<tr>';
      row += `<td>${i + 1}</td>`;
      if (area === 'a') {
        row += `<td class="mono" title="${esc(r.local_path)}">${esc(stripPath(r.local_path, localRoot))}</td><td class="mono" title="${esc(r.webdav_path)}">${esc(stripPath(r.webdav_path, webdavRoot))}</td><td>${fmtTime(r.updated_at)}</td>`;
      } else if (area === 'b') {
        const fp = r.fingerprint || '-'; const fpShort = fp.length > 5 ? fp.substring(0, 5) + '...' : fp;
        row += `<td class="mono" title="${esc(r.local_path)}">${esc(stripPath(r.local_path, localRoot))}</td><td class="mono" title="${esc(r.webdav_path)}">${esc(stripPath(r.webdav_path, webdavRoot))}</td><td class="mono" style="font-size:calc(var(--font-base) - 2px);cursor:default" title="${esc(fp)}">${esc(fpShort)}</td><td class="${_statusClass(r.status || '-')}">${esc(r.status || '-')}</td><td>${fmtTime(r.updated_at)}</td>`;
      } else if (area === 'c') {
        row += `<td class="mono" title="${esc(r.local_path)}">${esc(stripPath(r.local_path, localRoot))}</td><td class="mono" title="${esc(r.webdav_path)}">${esc(stripPath(r.webdav_path, webdavRoot))}</td><td class="mono">${esc(r.original_b_path || '-')}</td><td class="mono">${esc(r.ghost_root || '-')}</td><td>${fmtTime(r.moved_at)}</td>`;
      }
      row += '</tr>';
      recordParts.push(row);
    });

    html += recordParts.join('');
    html += '</tbody></table></div></details>';
  }
  return html;
}

// Task 2: 时间戳格式化辅助函数
function _formatTimestamp(timestamp) {
  if (!timestamp || timestamp === 0) return '未知';
  try {
    const date = new Date(timestamp * 1000);
    const now = new Date();
    const diffMs = now - date;
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);
    
    if (diffMins < 1) return '刚刚';
    if (diffMins < 60) return `${diffMins}分钟前`;
    if (diffHours < 24) return `${diffHours}小时前`;
    if (diffDays < 7) return `${diffDays}天前`;
    
    return date.toLocaleDateString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch (e) {
    return '未知';
  }
}
