import { api } from '../core/api.js';
import { icon } from '../core/icons.js';
import { esc } from '../core/utils.js';
import { showToast } from '../components/toast.js';
import { showConfirmDialog } from '../components/dialog.js';
import {
  CONFIG, _serverStartTime, setServerStartTime,
  _mainStatusTimer, setMainStatusTimer,
  startUptimeTimer, stopUptimeTimer, updateUptime
} from '../core/state.js';

export { startUptimeTimer, stopUptimeTimer, updateUptime };

export async function updateMainStatus() {
  try {
    const status = await api('/api/main/status');
    const dot = document.getElementById('main-status-dot');
    const text = document.getElementById('main-status-text');
    const uptimeText = document.getElementById('main-uptime-text');
    const startBtn = document.getElementById('main-start-btn');
    const stopBtn = document.getElementById('main-stop-btn');

    if (!dot || !text) return;

    if (status.running) {
      dot.style.background = '#4caf50';
      dot.style.boxShadow = '0 0 12px rgba(76,175,80,0.6)';
      text.textContent = '主程序运行中';
      text.style.color = 'var(--text-main)';
      if (status.uptime) {
        const hours = Math.floor(status.uptime / 3600);
        const mins = Math.floor((status.uptime % 3600) / 60);
        const secs = status.uptime % 60;
        uptimeText.textContent = `已运行 ${hours}小时 ${mins}分 ${secs}秒`;
      }
      if (startBtn) startBtn.style.display = 'none';
      if (stopBtn) stopBtn.style.display = 'inline-flex';
    } else {
      dot.style.background = '#f44336';
      dot.style.boxShadow = '0 0 12px rgba(244,67,54,0.6)';
      text.textContent = '主程序已停止';
      text.style.color = 'var(--text-main)';
      uptimeText.textContent = '点击启动按钮开始同步服务';
      if (startBtn) startBtn.style.display = 'inline-flex';
      if (stopBtn) stopBtn.style.display = 'none';
    }
  } catch (e) {
    // 静默处理状态获取失败
  }
}

export async function startMainProgram() {
  showConfirmDialog('启动主程序', '确定要启动主程序吗？这将开始 STRM 同步服务。', async () => {
    const startBtn = document.getElementById('main-start-btn');
    if (startBtn) {
      startBtn.disabled = true;
      startBtn.innerHTML = '<span class="spinner-small"></span> 启动中...';
    }
    try {
      const result = await api('/api/main/start', { method: 'POST' });
      if (result.success) {
        showToast('主程序已启动', 'success');
        updateMainStatus();
      } else {
        showToast('启动失败: ' + (result.message || '未知错误'), 'error');
        if (startBtn) {
          startBtn.disabled = false;
          startBtn.innerHTML = `${icon('refresh')} 启动主程序`;
        }
      }
    } catch (e) {
      showToast('启动请求失败: ' + e.message, 'error');
      if (startBtn) {
        startBtn.disabled = false;
        startBtn.innerHTML = `${icon('refresh')} 启动主程序`;
      }
    }
  });
}

export async function stopMainProgram() {
  showConfirmDialog('停止主程序', '确定要停止主程序吗？这将停止所有 STRM 同步服务。', async () => {
    const stopBtn = document.getElementById('main-stop-btn');
    if (stopBtn) {
      stopBtn.disabled = true;
      stopBtn.innerHTML = '<span class="spinner-small"></span> 停止中...';
    }
    try {
      const result = await api('/api/main/stop', { method: 'POST' });
      if (result.success) {
        showToast('主程序已停止', 'success');
        updateMainStatus();
      } else {
        showToast('停止失败: ' + (result.message || '未知错误'), 'error');
        if (stopBtn) {
          stopBtn.disabled = false;
          stopBtn.innerHTML = `${icon('check')} 停止主程序`;
        }
      }
    } catch (e) {
      showToast('停止请求失败: ' + e.message, 'error');
      if (stopBtn) {
        stopBtn.disabled = false;
        stopBtn.innerHTML = `${icon('check')} 停止主程序`;
      }
    }
  });
}

export async function renderDashboard(el) {
  const d = await api('/api/dashboard');
  if (d.uptime != null) {
    setServerStartTime(Date.now() - d.uptime * 1000);
  }
  el.innerHTML = `
<h2 class="page-header">${icon('dashboard', 'ui-icon-lg')} 仪表盘</h2>

<!-- 主程序控制区 -->
<div class="main-control-card">
  <div class="status-info">
    <div class="main-status-dot" id="main-status-dot"></div>
    <div>
      <div class="main-status-text" id="main-status-text">检查中...</div>
      <div class="main-uptime-text" id="main-uptime-text">-</div>
    </div>
  </div>
  <div class="status-actions">
    <button class="md3-btn filled" id="main-start-btn" style="display:none">${icon('refresh')} 启动主程序</button>
    <button class="md3-btn tonal" id="main-stop-btn" style="display:none">${icon('check')} 停止主程序</button>
  </div>
</div>

<div class="stat-grid">
  <div class="stat-card"><div class="label">${icon('movie')} A 区 STRM</div><div class="value">${d.a_count}</div></div>
  <div class="stat-card"><div class="label">${icon('tv')} B 区 STRM</div><div class="value">${d.b_count}</div></div>
  <div class="stat-card"><div class="label">${icon('area_c')} C 区幽灵</div><div class="value">${d.c_count}</div></div>
  <div class="stat-card"><div class="label">B - valid</div><div class="value stat-value-primary">${d.b_valid}</div></div>
  <div class="stat-card"><div class="label">B - orphan</div><div class="value stat-value-warning">${d.b_orphan}</div></div>
  <div class="stat-card"><div class="label">B - unknown</div><div class="value stat-value-error">${d.b_unknown}</div></div>
  <div class="stat-card"><div class="label">${icon('tmdb')} TMDB</div><div class="value stat-value-large">${d.tmdb_configured ? '已配置' : '未配置'}</div></div>
  <div class="stat-card"><div class="label">WebUI 运行时间</div><div class="value stat-value-large" id="uptime-val">-</div></div>
</div>`;

  // Bind start/stop buttons (replaces inline onclick)
  document.getElementById('main-start-btn')?.addEventListener('click', startMainProgram);
  document.getElementById('main-stop-btn')?.addEventListener('click', stopMainProgram);

  // 初始化主程序状态轮询与 uptime 计时器
  updateMainStatus();
  startUptimeTimer();
  if (_mainStatusTimer) clearInterval(_mainStatusTimer);
  setMainStatusTimer(setInterval(updateMainStatus, CONFIG.MAIN_STATUS_POLL_INTERVAL));
}
