import { api } from '../core/api.js';
import { icon } from '../core/icons.js';
import { esc, fmtTime } from '../core/utils.js';
import { showToast } from '../components/toast.js';
import { captureRenderGuard } from '../core/router.js';

// 当前日志类型：'tmdb' = TMDB 操作日志（主日志，默认），'main' = 主程序日志
let currentLogType = 'tmdb';

// 标签切换与刷新不经 router、不推进 _renderGen，代际护栏无法拦截
// "切标签后旧响应回填"。模块级 _logsGeneration 在切换/刷新时自增，响应写入前比对。
let _logsGeneration = 0;

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
  // 代际快照工厂——在首次 await 前捕获
  const isStale = captureRenderGuard();
  // 快照起始时刻的类型与请求代际，全程使用；await 后若已变化则丢弃响应
  const logType = currentLogType;
  const url = logType === 'tmdb' ? '/api/tmdb/logs' : '/api/logs';
  const gen = _logsGeneration;
  const data = await api(url);

  // 导航期间在途请求返回后，若页面代际已变则丢弃，避免覆盖新页面
  if (isStale()) return;
  // 标签切换/刷新期间又发起新请求 -> 旧响应作废，避免回填错位
  if (currentLogType !== logType || _logsGeneration !== gen) return;

  let logs, totalCount;
  if (logType === 'tmdb') {
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
    if (logType === 'main') {
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

  const mainActive = logType === 'main' ? 'active' : '';
  const tmdbActive = logType === 'tmdb' ? 'active' : '';

  // 下载按钮文案根据当前日志类型动态变化
  const downloadLabel = logType === 'tmdb'
    ? '下载当前 TMDB 日志'
    : '下载当前主程序日志';

  // tab 按钮上直接显示条数
  const tmdbTabLabel = logType === 'tmdb'
    ? `TMDB 操作日志 (${totalCount})`
    : 'TMDB 操作日志';
  const mainTabLabel = logType === 'main'
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
<tbody>${rows || '<tr><td colspan="1" style="text-align:center;color:var(--text-muted)">暂无日志</td></tr>'}</tbody>
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
        // 切换标签时自增请求代际，使在途旧响应作废
        _logsGeneration++;
        currentLogType = newType;
        // 标签切换未 catch
        _fetchAndRenderLogs(el).catch(e => showToast('加载失败: ' + e.message, 'error'));
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
        // 手动刷新时自增请求代际，使在途旧响应作废
        _logsGeneration++;
        await _fetchAndRenderLogs(el);
      } catch (e) {
        showToast('刷新失败: ' + e.message, 'error');
      } finally {
        // 恢复按钮状态（_fetchAndRenderLogs 可能重建 DOM，需重新获取引用）
        const btn = document.getElementById('logs-refresh');
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = originalHtml;
        }
      }
    });
  }

  // 下载按钮：改用服务端流式下载端点（/api/logs/download、/api/tmdb/logs/download），
  // 自动附加 X-Session-Token，避免 401，且能下载完整日志（不受分页上限截断）。
  const downloadBtn = document.getElementById('logs-download');
  if (downloadBtn) {
    downloadBtn.addEventListener('click', async () => {
      const originalHtml = downloadBtn.innerHTML;
      downloadBtn.disabled = true;
      downloadBtn.innerHTML = '准备下载...';

      try {
        const endpoint = currentLogType === 'tmdb'
          ? '/api/tmdb/logs/download'
          : '/api/logs/download';
        const filename = currentLogType === 'tmdb'
          ? 'tmdb_operations.log'
          : 'strm_bridge.log';

        const headers = {};
        const token = localStorage.getItem('session_token');
        if (token) headers['X-Session-Token'] = token;

        const resp = await fetch(endpoint, { headers });
        if (resp.status === 401) {
          localStorage.removeItem('session_token');
          showToast('登录已过期，请重新登录', 'error');
          return;
        }
        if (!resp.ok) {
          showToast('下载失败: HTTP ' + resp.status, 'error');
          return;
        }

        const blob = await resp.blob();
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
        showToast('下载失败: ' + err.message, 'error');
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
