import { api } from '../core/api.js';
import { icon } from '../core/icons.js';
import { esc, fmtTime } from '../core/utils.js';

// 当前日志类型：'tmdb' = TMDB 操作日志（主日志，默认），'main' = 主程序日志
let currentLogType = 'tmdb';

// TMDB 操作类型 → 中文标签映射（覆盖后端所有 op code）
const opLabel = {
  sync: '同步',
  sync_start: '同步启动',
  sync_done: '同步完成',
  sync_error: '同步失败',
  sync_cache_expired: '缓存过期',
  sync_movies_done: '电影同步完成',
  sync_movies_error: '电影同步失败',
  sync_tv_done: '剧集同步完成',
  sync_tv_error: '剧集同步失败',
  sync_tv_details_start: '剧集详情获取启动',
  sync_tv_details_done: '剧集详情获取完成',
  sync_tv_details_error: '剧集详情获取失败',
  sync_summary: '同步汇总',
  match_refresh_start: '收录刷新启动',
  match_refresh: '收录刷新',
  match_refresh_done: '收录刷新完成',
  match_refresh_error: '收录刷新失败',
  match_override: '收录覆盖',
  match: '收录匹配',
  match_done: '收录匹配完成',
  match_error: '收录匹配失败',
  configure: '配置保存',
  config_save: '配置保存',
  config_update: '配置更新',
  openlist_config_save: 'OpenList 配置保存',
  restart: '重启',
  webui_restart: 'WebUI 重启',
  login: '登录',
  logout: '登出',
  add: '新增',
  update: '更新',
  delete: '删除',
  fetch: '拉取',
  search: '搜索',
  cache_clear: '清理缓存',
  cache_hit: '缓存命中',
  cache_miss: '缓存未命中',
  api_call: 'API 调用',
  api_error: 'API 错误',
  rate_limit: '速率限制',
  auth: '认证',
  token_refresh: '令牌刷新',
  watchlist_sync: '待看列表同步',
  watchlist_refresh: '待看列表刷新',
  info: '信息',
  warn: '警告',
  error: '错误',
  success: '成功',
};

export async function renderLogs(el) {
  await _fetchAndRenderLogs(el);
}

async function _fetchAndRenderLogs(el) {
  const url = currentLogType === 'tmdb' ? '/api/tmdb/logs' : '/api/logs';
  const data = await api(url);

  let logs, totalCount;
  if (currentLogType === 'tmdb') {
    logs = data.logs || [];
    totalCount = data.count || logs.length;
  } else {
    logs = (data.lines || []).map(line => ({ msg: line }));
    totalCount = data.count || logs.length;
  }

  const levelColor = { 'info': 'var(--primary)', 'success': '#188038', 'warn': '#e37400', 'error': '#d93025' };
  const levelIcon = { 'info': 'info', 'success': 'check', 'warn': 'warn', 'error': 'error' };
  const levelLabel = { 'info': '信息', 'success': '成功', 'warn': '警告', 'error': '错误' };

  const rows = logs.map(log => {
    if (currentLogType === 'main') {
      return `<tr><td>${esc(log.msg)}</td></tr>`;
    } else {
      const lv = log.level || 'info';
      const color = levelColor[lv] || 'var(--text-muted)';
      const ic = levelIcon[lv] || 'info';
      const lvText = levelLabel[lv] || lv;
      const opText = opLabel[log.op] || log.op || '-';
      const ts = log.ts ? fmtTime(log.ts) : '-';
      return `<tr>
        <td style="white-space:nowrap">${esc(ts)}</td>
        <td><span style="color:var(--primary)">${esc(opText)}</span></td>
        <td><span style="color:${color};display:inline-flex;align-items:center;gap:4px">${icon(ic)} ${esc(lvText)}</span></td>
        <td>${esc(log.msg)}</td>
      </tr>`;
    }
  }).join('');

  const mainActive = currentLogType === 'main' ? 'active' : '';
  const tmdbActive = currentLogType === 'tmdb' ? 'active' : '';

  // 下载按钮文案根据当前日志类型动态变化
  const downloadLabel = currentLogType === 'tmdb'
    ? '下载当前 TMDB 日志'
    : '下载当前主程序日志';

  // tab 按钮上直接显示条数
  const tmdbTabLabel = currentLogType === 'tmdb'
    ? `TMDB 操作日志 (${totalCount})`
    : 'TMDB 操作日志';
  const mainTabLabel = currentLogType === 'main'
    ? `主程序日志 (${totalCount})`
    : '主程序日志';

  el.innerHTML = `
<h2 style="font-size:20px;margin-bottom:16px;color:var(--text-main)">${icon('log','ui-icon-lg')} 日志查看</h2>
<div class="log-type-toggle" id="log-type-toggle">
  <button data-log-type="tmdb" class="${tmdbActive}">${esc(tmdbTabLabel)}</button>
  <button data-log-type="main" class="${mainActive}">${esc(mainTabLabel)}</button>
</div>
<div class="toolbar">
  <button class="toolbar-btn" id="logs-refresh">${icon('refresh')} 刷新当前日志</button>
  <button class="toolbar-btn secondary" id="logs-download">${icon('download')} ${esc(downloadLabel)}</button>
  <span style="color:var(--text-muted);font-size:calc(var(--font-base) - 1px)" id="logs-count">共 ${totalCount} 条</span>
</div>
<table>
<thead id="log-table-header"></thead>
<tbody>${rows || '<tr><td colspan="4" style="text-align:center;color:var(--text-muted)">暂无日志</td></tr>'}</tbody>
</table>`;

  _renderLogTableHeader();

  // 切换按钮事件（每次重建 DOM 后重新绑定，确保双向切换可用）
  const toggleEl = document.getElementById('log-type-toggle');
  if (toggleEl) {
    toggleEl.addEventListener('click', (e) => {
      const btn = e.target.closest('button');
      if (!btn) return;
      const newType = btn.dataset.logType;
      if (newType && newType !== currentLogType) {
        currentLogType = newType;
        _fetchAndRenderLogs(el);
      }
    });
  }

  // 刷新按钮：重新拉取当前日志类型的最新数据
  const refreshBtn = document.getElementById('logs-refresh');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', async () => {
      const originalHtml = refreshBtn.innerHTML;
      refreshBtn.disabled = true;
      refreshBtn.innerHTML = '刷新中...';
      try {
        await _fetchAndRenderLogs(el);
      } finally {
        // _fetchAndRenderLogs 已经重建了 DOM，这里不需要恢复按钮状态
      }
    });
  }

  // 下载按钮：通过 api() 获取数据后生成 Blob 下载（避免 401）
  const downloadBtn = document.getElementById('logs-download');
  if (downloadBtn) {
    downloadBtn.addEventListener('click', async () => {
      const originalHtml = downloadBtn.innerHTML;
      downloadBtn.disabled = true;
      downloadBtn.innerHTML = '准备下载...';
      
      try {
        let content, filename;
        
        if (currentLogType === 'tmdb') {
          // TMDB 日志：请求最多 500 条
          const data = await api('/api/tmdb/logs?limit=500');
          const logs = data.logs || [];
          const lines = logs.map(log => {
            const ts = log.ts ? new Date(log.ts * 1000).toLocaleString('zh-CN') : '-';
            const level = (log.level || 'info').toUpperCase();
            const op = opLabel[log.op] || log.op || '-';
            return `[${ts}] [${level}] [${op}] ${log.msg || ''}`;
          });
          content = lines.join('\n');
          filename = 'tmdb_operations.log';
        } else {
          // 主程序日志：请求最多 1000 行
          const data = await api('/api/logs?lines=1000');
          const lines = data.lines || [];
          content = lines.join('\n');
          filename = 'strm_bridge.log';
        }
        
        // 创建 Blob 并触发下载
        const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      } catch (err) {
        console.error('下载日志失败:', err);
        alert('下载失败: ' + err.message);
      } finally {
        downloadBtn.disabled = false;
        downloadBtn.innerHTML = originalHtml;
      }
    });
  }
}

function _renderLogTableHeader() {
  const headerEl = document.getElementById('log-table-header');
  if (!headerEl) return;
  if (currentLogType === 'main') {
    headerEl.innerHTML = '<tr><th>消息</th></tr>';
  } else {
    headerEl.innerHTML = '<tr><th>时间</th><th>操作类型</th><th>级别</th><th>消息</th></tr>';
  }
}
