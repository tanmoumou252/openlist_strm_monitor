import{I as e,m as t,o as n,s as r}from"./core-BTjoc0Zv.js";var i=`tmdb`,a={sync:`同步`,sync_start:`同步启动`,sync_done:`同步完成`,sync_error:`同步失败`,sync_cache_expired:`缓存过期`,sync_movies_done:`电影同步完成`,sync_movies_error:`电影同步失败`,sync_tv_done:`剧集同步完成`,sync_tv_error:`剧集同步失败`,sync_tv_details_start:`剧集详情获取启动`,sync_tv_details_done:`剧集详情获取完成`,sync_tv_details_error:`剧集详情获取失败`,sync_summary:`同步汇总`,match_refresh_start:`收录刷新启动`,match_refresh:`收录刷新`,match_refresh_done:`收录刷新完成`,match_refresh_error:`收录刷新失败`,match_override:`收录覆盖`,match:`收录匹配`,match_done:`收录匹配完成`,match_error:`收录匹配失败`,configure:`配置保存`,config_save:`配置保存`,config_update:`配置更新`,openlist_config_save:`OpenList 配置保存`,restart:`重启`,webui_restart:`WebUI 重启`,login:`登录`,logout:`登出`,add:`新增`,update:`更新`,delete:`删除`,fetch:`拉取`,search:`搜索`,cache_clear:`清理缓存`,cache_hit:`缓存命中`,cache_miss:`缓存未命中`,api_call:`API 调用`,api_error:`API 错误`,rate_limit:`速率限制`,auth:`认证`,token_refresh:`令牌刷新`,watchlist_sync:`待看列表同步`,watchlist_refresh:`待看列表刷新`,info:`信息`,warn:`警告`,error:`错误`,success:`成功`};async function o(e){await s(e)}async function s(o){let l=await t(i===`tmdb`?`/api/tmdb/logs`:`/api/logs`),u,d;i===`tmdb`?(u=l.logs||[],d=l.count||u.length):(u=(l.lines||[]).map(e=>({msg:e})),d=l.count||u.length);let f={info:`var(--primary)`,success:`#188038`,warn:`#e37400`,error:`#d93025`},p={info:`info`,success:`check`,warn:`warn`,error:`error`},m={info:`信息`,success:`成功`,warn:`警告`,error:`错误`},h=u.map(t=>{if(i===`main`)return`<tr><td>${n(t.msg)}</td></tr>`;{let i=t.level||`info`,o=f[i]||`var(--text-muted)`,s=p[i]||`info`,c=m[i]||i,l=a[t.op]||t.op||`-`;return`<tr>
        <td style="white-space:nowrap">${n(t.ts?r(t.ts):`-`)}</td>
        <td><span style="color:var(--primary)">${n(l)}</span></td>
        <td><span style="color:${o};display:inline-flex;align-items:center;gap:4px">${e(s)} ${n(c)}</span></td>
        <td>${n(t.msg)}</td>
      </tr>`}}).join(``),g=i===`main`?`active`:``,_=i===`tmdb`?`active`:``,v=i===`tmdb`?`下载当前 TMDB 日志`:`下载当前主程序日志`,y=i===`tmdb`?`TMDB 操作日志 (${d})`:`TMDB 操作日志`,b=i===`main`?`主程序日志 (${d})`:`主程序日志`;o.innerHTML=`
<h2 style="font-size:20px;margin-bottom:16px;color:var(--text-main)">${e(`log`,`ui-icon-lg`)} 日志查看</h2>
<div class="log-type-toggle" id="log-type-toggle">
  <button data-log-type="tmdb" class="${_}">${n(y)}</button>
  <button data-log-type="main" class="${g}">${n(b)}</button>
</div>
<div class="toolbar">
  <button class="toolbar-btn" id="logs-refresh">${e(`refresh`)} 刷新当前日志</button>
  <button class="toolbar-btn secondary" id="logs-download">${e(`download`)} ${n(v)}</button>
  <span style="color:var(--text-muted);font-size:calc(var(--font-base) - 1px)" id="logs-count">共 ${d} 条</span>
</div>
<table>
<thead id="log-table-header"></thead>
<tbody>${h||`<tr><td colspan="4" style="text-align:center;color:var(--text-muted)">暂无日志</td></tr>`}</tbody>
</table>`,c();let x=document.getElementById(`log-type-toggle`);x&&x.addEventListener(`click`,e=>{let t=e.target.closest(`button`);if(!t)return;let n=t.dataset.logType;n&&n!==i&&(i=n,s(o))});let S=document.getElementById(`logs-refresh`);S&&S.addEventListener(`click`,async()=>{S.innerHTML,S.disabled=!0,S.innerHTML=`刷新中...`;try{await s(o)}finally{}});let C=document.getElementById(`logs-download`);C&&C.addEventListener(`click`,async()=>{let e=C.innerHTML;C.disabled=!0,C.innerHTML=`准备下载...`;try{let e,n;i===`tmdb`?(e=((await t(`/api/tmdb/logs?limit=500`)).logs||[]).map(e=>`[${e.ts?new Date(e.ts*1e3).toLocaleString(`zh-CN`):`-`}] [${(e.level||`info`).toUpperCase()}] [${a[e.op]||e.op||`-`}] ${e.msg||``}`).join(`
`),n=`tmdb_operations.log`):(e=((await t(`/api/logs?lines=1000`)).lines||[]).join(`
`),n=`strm_bridge.log`);let r=new Blob([e],{type:`text/plain;charset=utf-8`}),o=URL.createObjectURL(r),s=document.createElement(`a`);s.href=o,s.download=n,document.body.appendChild(s),s.click(),document.body.removeChild(s),URL.revokeObjectURL(o)}catch(e){console.error(`下载日志失败:`,e),alert(`下载失败: `+e.message)}finally{C.disabled=!1,C.innerHTML=e}})}function c(){let e=document.getElementById(`log-table-header`);e&&(i===`main`?e.innerHTML=`<tr><th>消息</th></tr>`:e.innerHTML=`<tr><th>时间</th><th>操作类型</th><th>级别</th><th>消息</th></tr>`)}export{o as renderLogs};