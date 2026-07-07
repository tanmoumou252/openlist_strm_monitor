import { api } from '../core/api.js';
import { icon } from '../core/icons.js';
import { esc, fmtTime } from '../core/utils.js';

export async function renderLogs(el) {
  const data = await api('/api/tmdb/logs');
  const logs = data.logs || [];

  const levelColor = { 'info': 'var(--primary)', 'success': '#188038', 'warn': '#e37400', 'error': '#d93025' };
  const levelIcon = { 'info': 'info', 'success': 'check', 'warn': 'warn', 'error': 'error' };
  const opLabel = {
    'sync_start': '同步启动', 'sync': '同步', 'match_refresh_start': '匹配刷新启动',
    'match_refresh': '匹配刷新', 'config_update': '配置更新', 'match_override': '状态覆盖', 'restart': '重启'
  };

  const rows = logs.map(log => {
    const lv = log.level || 'info';
    const color = levelColor[lv] || 'var(--text-muted)';
    const ic = levelIcon[lv] || 'info';
    const opText = opLabel[log.op] || log.op;
    const ts = log.ts ? fmtTime(log.ts) : '-';
    return `<tr>
      <td style="white-space:nowrap">${esc(ts)}</td>
      <td><span style="color:var(--primary)">${esc(opText)}</span></td>
      <td><span style="color:${color};display:inline-flex;align-items:center;gap:4px">${icon(ic)} ${esc(lv)}</span></td>
      <td>${esc(log.msg)}</td>
    </tr>`;
  }).join('');

  el.innerHTML = `
<h2 style="font-size:20px;margin-bottom:16px;color:var(--text-main)">${icon('log','ui-icon-lg')} webui日志</h2>
<p style="color:var(--text-muted);font-size:calc(var(--font-base) - 2px);margin:-8px 0 16px">此处是 WebUI TMDB 相关操作的日志，并非主程序的日志</p>
<div class="toolbar">
  <button class="toolbar-btn" id="logs-refresh">${icon('refresh')} 刷新</button>
  <span style="color:var(--text-muted);font-size:calc(var(--font-base) - 1px)">共 ${data.count} 条</span>
</div>
<table>
<thead><tr><th>时间</th><th>操作类型</th><th>级别</th><th>消息</th></tr></thead>
<tbody>${rows || '<tr><td colspan="4" style="text-align:center;color:var(--text-muted)">暂无日志</td></tr>'}</tbody>
</table>`;

  document.getElementById('logs-refresh').addEventListener('click', () => {
    import('../core/router.js').then(m => m.router());
  });
}
