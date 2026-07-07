import { icon } from '../core/icons.js';
import { esc, _formatTimeAgo } from '../core/utils.js';
import { showToast } from './toast.js';
import { _setUiConfig } from '../core/state.js';

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
    try {
      const resp = await fetch('/api/tmdb/watchlist/sync', { method: 'POST' });
      const data = await resp.json();
      if (data.success) showToast('同步完成后刷新页面即可看到最新数据', 'success');
      else showToast(data.message || '启动同步失败', 'error');
    } catch (e) {
      showToast('启动同步失败: ' + e.message, 'error');
    }
  };
}

export function showConfirmDialog(title, message, onConfirm, onCancel) {
  const existing = document.getElementById('confirm-dialog');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'confirm-dialog';
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal-box">
      <div class="modal-title">${icon('warn')} ${title}</div>
      <div class="modal-body">
        <p>${message}</p>
      </div>
      <div class="modal-actions">
        <button class="modal-btn secondary" id="confirm-cancel">取消</button>
        <button class="modal-btn primary" id="confirm-ok">确定</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  document.getElementById('confirm-cancel').onclick = () => {
    overlay.remove();
    if (onCancel) onCancel();
  };
  document.getElementById('confirm-ok').onclick = () => {
    overlay.remove();
    if (onConfirm) onConfirm();
  };
}
