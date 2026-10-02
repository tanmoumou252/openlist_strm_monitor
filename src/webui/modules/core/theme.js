// Theme module: syncTheme and initDropdowns
// Router is injected via setRouterFn to avoid circular dependency

let _routerFn = null;
export function setRouterFn(fn) { _routerFn = fn; }

export function syncTheme() {
  const root = document.documentElement;
  const system = root.dataset.system || 'material';
  const color = root.dataset.color || 'blue';
  const font = root.dataset.font || 'sm';
  // Update dropdown active states
  document.querySelectorAll('#theme-system-menu .dropdown-item').forEach(el => {
    el.classList.toggle('active', el.dataset.val === system);
  });
  document.querySelectorAll('#theme-color-menu .dropdown-item').forEach(el => {
    el.classList.toggle('active', el.dataset.val === color);
  });
  document.querySelectorAll('#theme-fontsize-menu .dropdown-item').forEach(el => {
    el.classList.toggle('active', el.dataset.val === font);
  });
  // Update preset icon
  const presetIcon = document.getElementById('theme-preset-icon');
  if (presetIcon) {
    const gridSvg = '<svg viewBox="0 -960 960 960"><path d="M120-520v-320h320v320H120Zm0 400v-320h320v320H120Zm400-400v-320h320v320H520Zm0 400v-320h320v320H520ZM200-600h160v-160H200v160Zm400 0h160v-160H600v160Zm0 400h160v-160H600v160Zm-400 0h160v-160H200v160Zm400-400Zm0 240Zm-240 0Zm0-240Z"/></svg>';
    const paletteSvg = '<svg viewBox="0 -960 960 960"><path d="M480-80q-82 0-155-31.5t-127.5-86Q143-252 111.5-325T80-480q0-83 32.5-156t88-127Q256-817 330-848.5T488-880q80 0 151 27.5t124.5 76q53.5 48.5 85 115T880-518q0 115-70 176.5T640-280h-74q-9 0-12.5 5t-3.5 11q0 12 15 34.5t15 51.5q0 50-27.5 74T480-80Zm0-400Zm-177 23q17-17 17-43t-17-43q-17-17-43-17t-43 17q-17 17-17 43t17 43q17 17 43 17t43-17Zm120-160q17-17 17-43t-17-43q-17-17-43-17t-43 17q-17 17-17 43t17 43q17 17 43 17t43-17Zm200 0q17-17 17-43t-17-43q-17-17-43-17t-43 17q-17 17-17 43t17 43q17 17 43 17t43-17Zm120 160q17-17 17-43t-17-43q-17-17-43-17t-43 17q-17 17-17 43t17 43q17 17 43 17t43-17ZM480-160q9 0 14.5-5t5.5-13q0-14-15-33t-15-57q0-42 29-67t71-25h70q66 0 113-38.5T800-518q0-121-92.5-201.5T488-800q-136 0-232 93t-96 227q0 133 93.5 226.5T480-160Z"/></svg>';
    presetIcon.innerHTML = system === 'fluent'
      ? `<span class="material-icon" style="font-size:18px;color:var(--primary)">${gridSvg}</span>`
      : `<span class="material-icon filled" style="font-size:18px;color:var(--primary)">${paletteSvg}</span>`;
  }
  // Update color dot
  const colorDot = document.getElementById('color-dot-icon');
  if (colorDot) {
    const colorMap = { blue: '#1a73e8', purple: '#7b1fa2', green: '#2e7d32', orange: '#e65100' };
    colorDot.style.background = colorMap[color] || 'var(--primary)';
  }
  // Update font size icon
  const fsIcon = document.getElementById('fontsize-icon');
  if (fsIcon) {
    const fsMap = { lg: '15px', sm: '13px', xs: '11px' };
    fsIcon.style.fontSize = fsMap[font] || '13px';
  }
  try {
    localStorage.setItem('webui_theme_system', system);
    localStorage.setItem('webui_theme_color', color);
    localStorage.setItem('webui_theme_fontsize', font);
  } catch (e) { }
  if (typeof window._wallpaperResize === 'function') window._wallpaperResize();
}

export function initDropdowns() {
  document.querySelectorAll('.dropdown-wrap').forEach(wrap => {
    const btn = wrap.querySelector('.dropdown-btn');
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      // Close other dropdowns
      document.querySelectorAll('.dropdown-wrap.open').forEach(w => { if (w !== wrap) w.classList.remove('open'); });
      wrap.classList.toggle('open');
    });
    wrap.querySelectorAll('.dropdown-item').forEach(item => {
      item.addEventListener('click', (e) => {
        e.stopPropagation();
        const val = item.dataset.val;
        const root = document.documentElement;
        if (wrap.id === 'theme-system-dd') {
          root.dataset.system = val;
        } else if (wrap.id === 'theme-color-dd') {
          root.dataset.color = val;
        } else if (wrap.id === 'theme-fontsize-dd') {
          root.dataset.font = val;
        }
        wrap.classList.remove('open');
        syncTheme();
        if (_routerFn) _routerFn();
      });
    });
  });
  // Close dropdowns on outside click
  document.addEventListener('click', () => {
    document.querySelectorAll('.dropdown-wrap.open').forEach(w => w.classList.remove('open'));
  });
}
