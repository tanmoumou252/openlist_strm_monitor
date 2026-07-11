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
    htmlLabel = ''  // 可选：不经过 esc() 转义，直接渲染的 HTML（如配置状态徽章）
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
      </div>
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
