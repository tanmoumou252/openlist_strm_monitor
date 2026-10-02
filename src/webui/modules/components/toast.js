import { icon } from '../core/icons.js';
import { esc } from '../core/utils.js';

export function showToast(msg, type = 'info') {
  const container = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  const iconMap = { success: 'check', error: 'error', info: 'info' };
  const toastIconName = iconMap[type] || 'info';
  el.innerHTML = `${icon(toastIconName)}<span>${esc(msg)}</span>`;
  container.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => el.remove(), 400);
  }, 3000);
}
