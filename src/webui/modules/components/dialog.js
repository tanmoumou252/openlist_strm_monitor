import { icon } from '../core/icons.js';
import { esc, _formatTimeAgo } from '../core/utils.js';
import { showToast } from './toast.js';
import { _setUiConfig } from '../core/state.js';
import { api } from '../core/api.js';

export function showCacheStaleModal(itemCount, lastSync) {
  const existing = document.getElementById('cache-stale-modal');
  if (existing) existing.remove();
  const ago = lastSync ? _formatTimeAgo(lastSync) : '未知';
  const overlay = document.createElement('div');
  overlay.id = 'cache-stale-modal';
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal-box">
      <div class="modal-title">${icon('warn')} TMDB 缓存已过期</div>
      <div class="modal-body">
        <p>当前缓存 ${itemCount} 项，上次同步：${ago}</p>
        <p>是否后台刷新？（约 30 秒，期间可正常使用页面）</p>
        <label class="modal-checkbox">
          <input type="checkbox" id="cache-never-remind"> 不再提醒
        </label>
      </div>
      <div class="modal-actions">
        <button class="modal-btn secondary" id="cache-modal-cancel">取消</button>
        <button class="modal-btn primary" id="cache-modal-ok">好的</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  document.getElementById('cache-modal-cancel').onclick = () => {
    const cb = document.getElementById('cache-never-remind');
    if (cb.checked) _setUiConfig('tmdb_cache_never_remind', '1');
    overlay.remove();
  };
  document.getElementById('cache-modal-ok').onclick = async () => {
    const cb = document.getElementById('cache-never-remind');
    if (cb.checked) _setUiConfig('tmdb_cache_never_remind', '1');
    overlay.remove();
    showToast('后台同步已启动...', 'info');
    // 缩进对齐（原列 0，现缩进 4 空格）
    try {
      const data = await api('/api/tmdb/watchlist/sync', { method: 'POST' });
      if (data.success) showToast('同步完成后刷新页面即可看到最新数据', 'success');
      else showToast(data.message || '启动同步失败', 'error');
    } catch (e) {
      showToast('启动同步失败: ' + e.message, 'error');
    }
  };
}

export function showConfirmDialog(title, message, onConfirm, onCancel, options = {}) {
  const existing = document.getElementById('confirm-dialog');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'confirm-dialog';
  overlay.className = 'modal-overlay';
  // htmlContent 直接注入 innerHTML - 调用者须确保 content 为可信数据（非用户输入）。
  // 当前仅用于内部富文本预检消息；如需支持用户内容，应使用 textContent 或 esc() 转义。
  if (options.htmlContent) {
    // 运行时守卫：检测高风险 XSS 向量（script/iframe/object/embed/img+事件/javascript:），
    // 捕获开发失误传入未转义用户内容的场景。结构化标签（div/span/p/a/br）为已知安全路径。
    if (typeof message !== 'string' || /<script|<iframe|<object|<embed|<img[^>]+\bon\w|javascript:/i.test(message)) {
      console.error('[dialog.js] htmlContent 检测到高风险标签，降级为转义模式');
      message = esc(message);
    }
  }
  // L3: 非 htmlContent 路径（默认）直接拼接 message/title 到 innerHTML，
  // 调用方若传入用户可控内容即构成 XSS。统一用 esc() 转义；htmlContent 分支
  // 仍按 S7 约定由调用者保证内容可信（上方 console.assert 已守卫）。
  const bodyContent = options.htmlContent ? message : `<p>${esc(message)}</p>`;
  overlay.innerHTML = `
    <div class="modal-box">
      <div class="modal-title">${icon('warn')} ${esc(title)}</div>
      <div class="modal-body">
        ${bodyContent}
      </div>
      <div class="modal-actions">
        <button class="modal-btn secondary" id="confirm-cancel">${esc(options.cancelText || '取消')}</button>
        <button class="modal-btn primary" id="confirm-ok">${esc(options.confirmText || '确定')}</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  document.getElementById('confirm-cancel').onclick = () => {
    overlay.remove();
    if (onCancel) onCancel();
  };
  document.getElementById('confirm-ok').onclick = () => {
    overlay.remove();
    // 将 onConfirm 包裹在 Promise 链中，异步回调的异常不会冒泡到事件处理器外部
    Promise.resolve()
      .then(() => onConfirm && onConfirm())
      .catch(err => {
        console.error('[dialog.js] onConfirm 回调异常:', err);
        showToast('操作失败: ' + (err && err.message ? err.message : '未知错误'), 'error');
      });
  };
}
