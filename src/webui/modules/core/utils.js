import { icon } from './icons.js';

export function esc(s) {
  if (s == null || s === '') return '';
  const str = String(s);
  const htmlEscapes = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  };
  return str.replace(/[&<>"']/g, c => htmlEscapes[c]);
}

export function fmtTime(ts) {
  if (!ts) return '-';
  const d = new Date(ts * 1000);
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  const hh = String(d.getHours()).padStart(2, '0');
  const mi = String(d.getMinutes()).padStart(2, '0');
  return `${mm}-${dd} ${hh}:${mi}`;
}

export function _formatTimeAgo(ts) {
  const sec = Math.floor(Date.now() / 1000 - ts);
  if (sec < 60) return `${sec} 秒前`;
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`;
  return `${Math.floor(sec / 86400)} 天前`;
}

export function createSortLink(area, sort, order, colName, colKey, params = {}) {
  const newOrder = (sort === colKey && order === 'asc') ? 'desc' : 'asc';
  const arrow = sort === colKey ? (order === 'asc' ? icon('arrow_up') : icon('arrow_down')) : '';
  let href = `#area_${area}?sort=${colKey}&order=${newOrder}`;
  if (params.kind) href += '&kind=' + encodeURIComponent(params.kind);
  if (params.q) href += '&q=' + encodeURIComponent(params.q);
  if (params.media) href += '&media=' + encodeURIComponent(params.media);
  return `<a href="${href}" class="sort-btn">${colName}${arrow}</a>`;
}

export function createField(id, label, value, options = {}) {
  const {
    placeholder,
    type = 'text',
    persistLabel = false,
    readOnly = false,
    helpIcon = '',
    htmlLabel = '',  // 可选：不经过 esc() 转义，直接渲染的 HTML（如配置状态徽章）
    helperText = ''  // 可选：字段下方的帮助说明文字
  } = options;
  const hasValue = value !== null && value !== undefined && String(value).trim() !== '';
  const inputClass = hasValue ? 'has-value' : '';
  const inputValue = esc(value || '');
  const floated = hasValue || persistLabel;
  const labelCls = floated
    ? `floating-label is-shown is-floating${hasValue ? ' is-filled' : ''}`
    : 'floating-label';
  const persistAttr = persistLabel ? ' data-persist-label="1"' : '';
  const disabledAttr = readOnly ? ' disabled' : '';
  const roClass = readOnly ? ' readonly-field' : '';
  return `
    <div class="floating-field" data-field="${id}">
      <div class="field-control">
        <label class="${labelCls}" data-role="label" for="${id}">${esc(label)}${htmlLabel}${helpIcon || ''}</label>
        <input type="${type}" id="${id}" class="${inputClass}${roClass}"${persistAttr}${disabledAttr} placeholder="${esc(placeholder || label)}" value="${inputValue}">
      </div>${helperText ? `<div class="field-helper-text">${esc(helperText)}</div>` : ''}
    </div>`;
}

export function copyPathBlock(el) {
  const texts = [];
  el.querySelectorAll('.path-line').forEach(line => {
    const label = line.querySelector('.path-label');
    const value = line.querySelector('.path-value');
    if (label && value) texts.push(label.textContent + ' ' + value.textContent);
  });
  const copyText = texts.join('\n');
  navigator.clipboard.writeText(copyText).then(() => {
    import('../components/toast.js').then(m => m.showToast('已复制路径到剪贴板', 'success'));
  }).catch(() => {
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    document.execCommand('copy');
    sel.removeAllRanges();
    import('../components/toast.js').then(m => m.showToast('已复制路径到剪贴板', 'success'));
  });
}

/**
 * 渲染 TMDB 搜索结果（双板块展示）
 * @param {Object} results - TMDB 搜索结果 {movies: [], tv_shows: []}
 * @param {string} title - 区块标题
 * @param {string} query - 搜索关键词（用于构造 TMDB 搜索链接）
 * @param {HTMLElement} container - 渲染目标容器元素。调用方应在发起异步请求前捕获
 *   该引用并传入；回调执行时本函数会校验 container.isConnected，若容器已脱离
 *   DOM（页面已切换）则中止渲染，避免异步结果污染当前页。函数内不负责查找容器，
 *   未传入或传入 null/已脱离 DOM 时一律不渲染。
 */
export function renderTmdbResults(results, title, query = '', container) {
  // 若未传入容器或容器已脱离 DOM（页面已切换），中止渲染，避免异步结果污染当前页
  if (!container || !container.isConnected) return;
  
  const { movies = [], tv_shows = [] } = results;
  const allResults = [
    ...movies.map(m => ({ ...m, type: 'movie' })),
    ...tv_shows.map(t => ({ ...t, type: 'tv' }))
  ];
  
  if (allResults.length === 0) {
    container.innerHTML = `
      <div class="tmdb-results-section" style="background:var(--bg-card);border:1px solid var(--border-color);border-radius:var(--radius-card);padding:16px;text-align:center">
        <div style="color:var(--text-muted);font-size:13px">
          未找到相关结果 · <a href="https://www.themoviedb.org/search?query=${encodeURIComponent(query || title)}" target="_blank" rel="noopener" style="color:var(--primary);text-decoration:none">在 TMDB 中搜索</a>
        </div>
      </div>
    `;
    return;
  }
  
  const html = `
    <div class="tmdb-results-section" style="background:var(--bg-card);border:1px solid var(--border-color);border-radius:var(--radius-card);padding:16px">
      <h3 class="tmdb-results-title" style="font-size:14px;font-weight:600;color:var(--text-main);margin:0 0 12px 0">${esc(title)}</h3>
      <div class="tmdb-results-list" style="display:flex;flex-direction:column;gap:8px">
        ${allResults.map(item => `
          <a class="tmdb-result-item" href="https://www.themoviedb.org/${item.type}/${item.id}" target="_blank" rel="noopener" style="display:flex;align-items:center;gap:12px;padding:8px;background:var(--bg-elevated);border-radius:var(--radius-control);border:1px solid var(--border-color);text-decoration:none;color:inherit;cursor:pointer;transition:background .18s ease" onmouseover="this.style.background='var(--bg-control)'" onmouseout="this.style.background='var(--bg-elevated)'">
            <span class="tmdb-result-type" style="font-size:11px;font-weight:600;color:var(--primary);background:color-mix(in srgb,var(--primary) 10%,transparent);padding:2px 8px;border-radius:var(--radius-pill);flex-shrink:0">${item.type === 'movie' ? '电影' : '电视剧'}</span>
            <span class="tmdb-result-name" style="flex:1;font-size:13px;font-weight:500;color:var(--text-main)">${esc(item.title || item.name)}</span>
            <span class="tmdb-result-date" style="font-size:12px;color:var(--text-muted);flex-shrink:0">${esc(item.release_date || item.first_air_date || '')}</span>
          </a>
        `).join('')}
      </div>
    </div>
  `;
  
  container.innerHTML = html;
}
