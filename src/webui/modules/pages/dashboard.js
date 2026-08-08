import { api } from '../core/api.js';
import { captureRenderGuard } from '../core/router.js';
import { icon } from '../core/icons.js';
import { esc, formatTimestamp } from '../core/utils.js';
import { showToast } from '../components/toast.js';
import { showConfirmDialog } from '../components/dialog.js';
import {
  CONFIG, _serverStartTime, setServerStartTime,
  _mainStatusTimer, setMainStatusTimer,
  startUptimeTimer, stopUptimeTimer, updateUptime
} from '../core/state.js';

export { startUptimeTimer, stopUptimeTimer, updateUptime, _loadOnboarding };

// ============================================================
// 首次配置引导（Onboarding Guide）
// ============================================================

async function _fetchConfigStatus() {
  try {
    return await api('/api/config/status');
  } catch (e) {
    return null;
  }
}

async function _markOnboardingCompleted() {
  try {
    await api('/api/webui/config/ui', {
      method: 'POST',
      body: JSON.stringify({ onboarding_completed: '1' })
    });
  } catch (e) {
    // 静默处理
  }
}

async function _resetOnboarding() {
  try {
    await api('/api/webui/config/ui', {
      method: 'POST',
      body: JSON.stringify({ onboarding_completed: '0' })
    });
  } catch (e) {
    // 静默处理
  }
}

function _renderOnboardingCard(status) {
  if (!status) return '';

  // 引导已完成/跳过 → 不渲染卡片，由 renderDashboard 中的按钮处理
  if (status.onboarding_completed === '1') {
    return '';
  }

  const steps = [
    {
      key: 'password',
      label: '确认管理员密码',
      done: status.password_set,
      link: '#config',
      linkText: '前往配置',
      message: '首次启动时系统已自动生成随机密码并打印到控制台（仅显示一次，不写入日志）。遗忘或需自定义密码，请运行 reset_admin.py。'
    },
    {
      key: 'tmdb',
      label: '配置 TMDB',
      done: status.tmdb_configured,
      link: '#config?sub=config',
      linkText: '前往配置',
      message: '配置 TMDB API Token 以启用待看列表和影视信息获取功能（可选）。'
    },
    {
      key: 'openlist',
      label: '配置 OpenList',
      done: status.openlist_configured,
      link: '#config?sub=openlist',
      linkText: '前往配置',
      message: '填写 OpenList WebDAV 地址、用户名和密码，以连接 STRM 引擎。'
    },
    {
      key: 'main',
      label: '启动主程序',
      done: status.main_running,
      link: null,
      linkText: '点击下方启动按钮',
      message: '完成以上配置后，点击「启动主程序」按钮开始同步服务。'
    },
    {
      key: 'view_ab',
      label: '查看 A/B 分区',
      done: status.view_ab_completed || false,
      link: '#area_a',
      linkText: '前往查看',
      message: '浏览 A 区和 B 区的文件列表，了解同步状态。'
    },
    {
      key: 'tmdb_refresh',
      label: '刷新 TMDB 待看列表',
      done: status.tmdb_refresh_completed || false,
      link: '#config?sub=config',
      linkText: '前往刷新',
      message: '点击「刷新待看列表」按钮，从 TMDB 获取最新数据。'
    },
    {
      key: 'tmdb_match',
      label: '检测 TMDB 收录状态',
      done: status.tmdb_match_completed || false,
      link: '#config?sub=config',
      linkText: '前往检测',
      message: '点击「刷新收录状态」按钮，检测本地文件是否已收录到 TMDB。'
    }
  ];

  const pendingCount = steps.filter(s => !s.done).length;
  const allDone = pendingCount === 0;

  const stepsHtml = steps.map((s, i) => `
    <div class="onboarding-step ${s.done ? 'done' : ''}">
      <div class="onboarding-step-indicator">
        ${s.done ? icon('check') : `<span>${i + 1}</span>`}
      </div>
      <div class="onboarding-step-content">
        <div class="onboarding-step-label">${esc(s.label)}</div>
        <div class="onboarding-step-message">${esc(s.message)}</div>
        ${!s.done && s.link ? `<a href="${s.link}" class="onboarding-step-link">${esc(s.linkText)} →</a>` : ''}
        ${!s.done && !s.link ? `<span class="onboarding-step-hint">${esc(s.linkText)}</span>` : ''}
        ${!s.done && s.key !== 'password' && s.key !== 'tmdb' && s.key !== 'openlist' && s.key !== 'main' ? `<button class="onboarding-step-complete-btn" data-step="${s.key}">标记完成</button>` : ''}
      </div>
    </div>
  `).join('');

  return `
    <div class="onboarding-card" id="onboarding-card">
      <div class="onboarding-header">
        <div class="onboarding-title">
          ${icon('menu_book', 'ui-icon-lg')} 初次使用
        </div>
        <div class="onboarding-progress">
          ${steps.length - pendingCount} / ${steps.length} 已完成
        </div>
      </div>
      <div class="onboarding-steps">
        ${stepsHtml}
      </div>
      <div class="onboarding-footer">
        ${allDone
          ? `<button class="md3-btn filled" id="onboarding-complete-btn">${icon('check')} 完成引导</button>`
          : `<button class="md3-btn tonal" id="onboarding-skip-btn">跳过引导</button>`
        }
      </div>
    </div>
  `;
}

function _bindOnboardingEvents() {
  const skipBtn = document.getElementById('onboarding-skip-btn');
  const completeBtn = document.getElementById('onboarding-complete-btn');
  const restartBtn = document.getElementById('onboarding-restart-btn');

  if (skipBtn) {
    skipBtn.addEventListener('click', async () => {
      await _markOnboardingCompleted();
      const card = document.getElementById('onboarding-card');
      if (card) card.remove();
      const quickBtn = document.getElementById('onboarding-quick-btn');
      if (quickBtn) quickBtn.style.display = 'inline-flex';
      showToast('已跳过引导，可随时在仪表盘重新显示', 'info');
    });
  }

  if (completeBtn) {
    completeBtn.addEventListener('click', async () => {
      await _markOnboardingCompleted();
      const card = document.getElementById('onboarding-card');
      if (card) card.remove();
      const quickBtn = document.getElementById('onboarding-quick-btn');
      if (quickBtn) quickBtn.style.display = 'inline-flex';
      showToast('引导已完成', 'success');
    });
  }

  if (restartBtn) {
    restartBtn.addEventListener('click', async () => {
      await _resetOnboarding();
      _loadOnboarding();
      showToast('引导已重新开始', 'success');
    });
  }

  // 绑定"标记完成"按钮
  document.querySelectorAll('.onboarding-step-complete-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const step = btn.dataset.step;
      try {
        await api('/api/onboarding/complete-step', {
          method: 'POST',
          body: JSON.stringify({ step })
        });
        // 重新加载引导状态
        await _loadOnboarding();
        showToast('步骤已标记完成', 'success');
      } catch (e) {
        showToast('标记失败: ' + e.message, 'error');
      }
    });
  });
}

async function _loadOnboarding() {
  const status = await _fetchConfigStatus();
  const container = document.getElementById('onboarding-container');
  if (container) {
    container.innerHTML = _renderOnboardingCard(status);
    _bindOnboardingEvents();
  }
  
  // Update header quick button visibility
  const quickBtn = document.getElementById('onboarding-quick-btn');
  if (quickBtn) {
    if (status && status.onboarding_completed === '1') {
      quickBtn.style.display = 'inline-flex';
    } else {
      quickBtn.style.display = 'none';
    }
  }
}

// ============================================================
// 启动预检（Preflight Check）
// ============================================================

async function _runPreflightCheck() {
  try {
    const result = await api('/api/config/validate', { method: 'POST' });
    return result;
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

function _renderPreflightDialog(result) {
  if (result.ok) return null;

  const checksHtml = (result.checks || []).map(c => {
    const statusIcon = c.status === 'ok' ? icon('check')
      : c.status === 'warning' ? icon('warn')
      : c.status === 'skipped' ? icon('info')
      : icon('error');
    const statusClass = `preflight-${c.status}`;
    return `
      <div class="preflight-check ${statusClass}">
        <div class="preflight-check-icon">${statusIcon}</div>
        <div class="preflight-check-content">
          <div class="preflight-check-label">${esc(c.label)}</div>
          <div class="preflight-check-message">${esc(c.message)}</div>
          ${c.suggestion ? `<div class="preflight-check-suggestion">${esc(c.suggestion)}</div>` : ''}
        </div>
      </div>
    `;
  }).join('');

  return `
    <div class="preflight-dialog">
      <div class="preflight-header">
        ${icon('warn')} 启动前检查未通过
      </div>
      <div class="preflight-checks">
        ${checksHtml}
      </div>
      <div class="preflight-footer">
        请修复以上问题后再启动主程序。
      </div>
    </div>
  `;
}

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

    // [已修复] P7b: 轮询更新 watcher 健康横幅，后端恢复时隐藏 banner
    if (status.watchers_healthy !== false) {
      const banner = document.querySelector('.dashboard-warning-banner');
      if (banner) banner.remove();
    } else {
      // 后端降级时显示 banner（如果不存在）
      if (!document.querySelector('.dashboard-warning-banner')) {
        const mainControlCard = document.querySelector('.main-control-card');
        if (mainControlCard) {
          const bannerHtml = `<div class="dashboard-warning-banner" style="margin:12px 0;padding:10px 14px;background:color-mix(in srgb,var(--error) 12%,transparent);border:1px solid color-mix(in srgb,var(--error) 40%,transparent);border-radius:var(--radius-control);color:var(--error);font-size:13px;display:flex;align-items:center;gap:8px">${icon('warn')} watchdog 监视器降级：部分区域事件可能未同步，请检查 WebUI 日志</div>`;
          mainControlCard.insertAdjacentHTML('afterend', bannerHtml);
        }
      }
    }
  } catch (e) {
    // 静默处理状态获取失败
  }
}

export async function startMainProgram() {
  // 启动前预检
  const preflight = await _runPreflightCheck();
  if (!preflight.ok) {
    // 显示预检失败对话框
    const preflightHtml = _renderPreflightDialog(preflight);
    if (preflightHtml) {
      showConfirmDialog(
        '启动前检查未通过',
        preflightHtml,
        null,
        null,
        { htmlContent: true, confirmText: '知道了', cancelText: '取消' }
      );
    }
    return;
  }

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
        // 刷新引导状态
        _loadOnboarding();
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
  // N0: 代际快照工厂——在首次 await 前捕获，供其后所有 isStale() 判定
  const isStale = captureRenderGuard();
  const d = await api('/api/dashboard');
  // F-3: await 期间若发生新导航，放弃渲染，避免旧页覆盖 + setInterval 泄漏
  if (isStale()) return;
  if (d.uptime != null) {
    setServerStartTime(Date.now() - d.uptime * 1000);
  }
  el.innerHTML = `
<div class="dashboard-header-row" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
  <h2 class="page-header" style="margin:0">${icon('dashboard', 'ui-icon-lg')} 仪表盘</h2>
  <button class="onboarding-quick-btn" id="onboarding-quick-btn" title="初次使用" style="display:none">
    ${icon('menu_book')} <span>初次使用</span>
  </button>
</div>

<!-- 首次配置引导 -->
<div id="onboarding-container"></div>

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

<!-- N1: watchdog 降级指示（后端 _watchers_healthy 标志） -->
${d.watchers_healthy === false ? `<div class="dashboard-warning-banner" style="margin:12px 0;padding:10px 14px;background:color-mix(in srgb,var(--error) 12%,transparent);border:1px solid color-mix(in srgb,var(--error) 40%,transparent);border-radius:var(--radius-control);color:var(--error);font-size:13px;display:flex;align-items:center;gap:8px">${icon('warn')} watchdog 监视器降级：部分区域事件可能未同步，请检查 WebUI 日志</div>` : ''}

<div class="stat-grid">
  <div class="stat-card"><div class="label">${icon('movie')} A 区 STRM</div><div class="value">${d.a_count}</div></div>
  <div class="stat-card"><div class="label">${icon('tv')} B 区 STRM</div><div class="value">${d.b_count}</div></div>
  <div class="stat-card"><div class="label">${icon('area_c')} C 区幽灵</div><div class="value">${d.c_count}</div></div>
<div class="stat-card"><div class="label">B - valid</div><div class="value stat-value-primary">${d.b_valid}</div></div>
    <div class="stat-card"><div class="label">B - duplicate</div><div class="value stat-value-warning">${d.b_duplicate}</div></div>
    <div class="stat-card"><div class="label">B - quarantined</div><div class="value stat-value-error">${d.b_quarantined}</div></div>
  <div class="stat-card"><div class="label">${icon('tmdb')} TMDB</div><div class="value stat-value-large">${d.tmdb_configured ? '已配置' : '未配置'}</div></div>
  <div class="stat-card"><div class="label">WebUI 运行时间</div><div class="value stat-value-large" id="uptime-val">-</div></div>
</div>

<!-- 索引元数据（Task 2） -->
<div class="stat-grid" style="margin-top:16px">
  <div class="stat-card"><div class="label">${icon('sync')} 索引代次</div><div class="value stat-value-primary" id="index-generation">#${d.index_metadata?.index_generation || 0}</div></div>
  <div class="stat-card"><div class="label">${icon('speed')} 最近索引</div><div class="value" title="${_formatExact(d.index_metadata?.last_full_index_at)}">${d.index_metadata?.last_full_index_at ? formatTimestamp(d.index_metadata.last_full_index_at) : '暂无记录'}</div></div>
  <div class="stat-card"><div class="label">${icon('swap_horiz')} 映射版本</div><div class="value" title="${esc(d.index_metadata?.mapping_version || '')}">${d.index_metadata?.mapping_version ? d.index_metadata.mapping_version.substring(0, 8) + '...' : '-'}</div></div>
  <div class="stat-card"><div class="label">映射版本生成</div><div class="value" title="${_formatExact(d.index_metadata?.mapping_version_generated_at)}">${d.index_metadata?.mapping_version_generated_at ? formatTimestamp(d.index_metadata.mapping_version_generated_at) : '暂无记录'}</div></div>
</div>

<!-- A'.3: 立即全量审计按钮 -->
<div style="margin-top:12px;display:flex;gap:8px;align-items:center">
  <button class="toolbar-btn secondary" id="btn-run-full-audit" style="font-size:calc(var(--font-base) - 1px)">${icon('refresh')} 立即全量审计</button>
  <span id="audit-status-text" style="font-size:calc(var(--font-base) - 1px);color:var(--text-muted)"></span>
</div>

<!-- Mapping 列表（Task 2） -->
${d.mappings && d.mappings.length > 0 ? `
<div style="margin-top:16px">
  <div style="font-size:14px;font-weight:500;margin-bottom:8px;color:var(--text-primary)">映射配置</div>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px">
    ${d.mappings.map(m => `
      <div style="background:var(--bg-surface-variant);padding:12px;border-radius:8px;border:1px solid var(--border-subtle)">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <span style="font-weight:500;color:var(--text-primary)">${esc(m.label || m.mapping_id)}</span>
          <span style="font-size:11px;color:var(--text-muted)">#${m.index_generation || 0}</span>
        </div>
        <div style="font-size:12px;color:var(--text-secondary);margin-bottom:4px">
          <div>A: ${esc(_shortenPath(m.a_root))}</div>
          <div>B: ${esc(_shortenPath(m.b_root))}</div>
        </div>
        <div style="font-size:11px;color:var(--text-muted)">
          索引时间: <span title="${_formatExact(m.index_generation_at)}">${m.index_generation_at ? formatTimestamp(m.index_generation_at) : '未索引'}</span>
        </div>
      </div>
    `).join('')}
  </div>
</div>
` : ''}
  
    <!-- 密码提示 -->
    <div style="text-align:center;font-size:12px;color:var(--text-muted);margin-top:8px">
      管理密码仅在首次启动时打印到控制台（不写入日志） · 忘记密码可运行 <code style="background:var(--bg-control);padding:1px 4px;border-radius:3px">python reset_admin.py</code> 重置
    </div>`;

/** 将 Unix 时间戳转为精确的 YYYY-MM-DD HH:mm:ss 格式（用于 title tooltip） */
function _formatExact(timestamp) {
  if (!timestamp || timestamp === 0) return '暂无记录';
  try {
    const d = new Date(timestamp * 1000);
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  } catch (e) {
    return '暂无记录';
  }
}

function _shortenPath(path) {
  if (!path) return '/';
  const parts = path.split('/').filter(Boolean);
  if (parts.length <= 2) return '/' + parts.join('/');
  return '/' + parts.slice(0, 2).join('/').replace(/\/$/, '') + '/...';
}

  // Bind start/stop buttons (replaces inline onclick)
  document.getElementById('main-start-btn')?.addEventListener('click', startMainProgram);
  document.getElementById('main-stop-btn')?.addEventListener('click', stopMainProgram);

  // Bind onboarding quick button
  const onboardingBtn = document.getElementById('onboarding-quick-btn');
  if (onboardingBtn) {
    onboardingBtn.addEventListener('click', async () => {
      try {
        await api('/api/webui/config/ui', {
          method: 'POST',
          body: JSON.stringify({ onboarding_completed: '0' })
        });
      } catch ( e) {
        console.error('Failed to reset onboarding:', e);
      }
      await _loadOnboarding();
    });
  }

  // A'.3: 立即全量审计按钮 + 轮询
  const auditBtn = document.getElementById('btn-run-full-audit');
  const auditStatusText = document.getElementById('audit-status-text');
  if (auditBtn) {
    auditBtn.addEventListener('click', async () => {
      // N0: 审计轮询独立捕获代际，仅对该 handler 生效
      const isStale = captureRenderGuard();
      if (!confirm('确定要执行全量审计吗？\n\n这是一个重操作，耗时取决于 A 区库大小，会扫描全部 A 区根目录（含机械硬盘）。不会删除任何文件。')) return;
      auditBtn.disabled = true;
      auditBtn.innerHTML = '审计中...';
      if (auditStatusText) auditStatusText.textContent = '正在启动审计...';
      try {
        const resp = await api('/api/index/audit', { method: 'POST' });
        if (resp.status === 'already_running') {
          if (auditStatusText) auditStatusText.textContent = '审计已在进行中';
          auditBtn.disabled = false;
          auditBtn.innerHTML = `${icon('refresh')} 立即全量审计`;
          return;
        }
        // 轮询状态
        const maxPolls = 300;
        for (let i = 0; i < maxPolls; i++) {
          await new Promise(r => setTimeout(r, 2000));
          // [已修复] L6: 轮询循环含 isStale 检查
          if (isStale()) return;
          try {
            const st = await api('/api/index/audit/status');
            // T11c: already_running 是"被其他任务占用"，不是完成的假成功
            if (st.result && st.result.status === 'already_running') {
              if (auditStatusText) auditStatusText.textContent = '审计被其他任务占用（已在进行中）';
              auditBtn.disabled = false;
              auditBtn.innerHTML = `${icon('refresh')} 立即全量审计`;
              return;
            }
            if (!st.running && st.result) {
              // [已修复] N-P2-10: 显式判断 status === 'completed' 再读 generation，
              // 避免用 `!error` 推断成功、`|| 0` 掩盖缺 generation 的脆弱性
              if (st.result.status === 'completed') {
                if (auditStatusText) auditStatusText.textContent = '审计完成，索引代次 #' + (st.result.index_generation || 0);
              } else if (st.result.error) {
                if (auditStatusText) auditStatusText.textContent = '审计失败: ' + st.result.error;
              } else {
                if (auditStatusText) auditStatusText.textContent = '审计未完成';
              }
              auditBtn.disabled = false;
              auditBtn.innerHTML = `${icon('refresh')} 立即全量审计`;
              // 局部刷新索引卡片
              try {
                const dashResp = await api('/api/dashboard');
                if (dashResp && dashResp.index_metadata) {
                  const genEl = document.getElementById('index-generation');
                  if (genEl) genEl.textContent = '#' + (dashResp.index_metadata.index_generation || 0);
                }
              } catch (e) { /* 忽略刷新失败 */ }
              return;
            }
            if (auditStatusText) auditStatusText.textContent = '审计进行中... (' + (i * 2) + 's)';
          } catch (e) { /* 轮询失败继续 */ }
        }
        if (auditStatusText) auditStatusText.textContent = '审计超时，请稍后重试';
        auditBtn.disabled = false;
        auditBtn.innerHTML = `${icon('refresh')} 立即全量审计`;
      } catch (e) {
        if (auditStatusText) auditStatusText.textContent = '审计请求失败: ' + e.message;
        auditBtn.disabled = false;
        auditBtn.innerHTML = `${icon('refresh')} 立即全量审计`;
      }
    });
  }

  // 加载首次配置引导
  _loadOnboarding();

  // 初始化主程序状态轮询与 uptime 计时器
  updateMainStatus();
  startUptimeTimer();
  if (_mainStatusTimer) clearInterval(_mainStatusTimer);
  setMainStatusTimer(setInterval(updateMainStatus, CONFIG.MAIN_STATUS_POLL_INTERVAL));
}
