import{P as e,c as t,o as n,p as r,s as i}from"./core-B6fuJhLL.js";async function a(a){let o=await t(`/api/tmdb/logs`),s=o.logs||[],c={info:`var(--primary)`,success:`#188038`,warn:`#e37400`,error:`#d93025`},l={info:`info`,success:`check`,warn:`warn`,error:`error`},u={sync_start:`同步启动`,sync:`同步`,match_refresh_start:`匹配刷新启动`,match_refresh:`匹配刷新`,config_update:`配置更新`,match_override:`状态覆盖`,restart:`重启`},d=s.map(t=>{let r=t.level||`info`,a=c[r]||`var(--text-muted)`,o=l[r]||`info`,s=u[t.op]||t.op;return`<tr>
      <td style="white-space:nowrap">${n(t.ts?i(t.ts):`-`)}</td>
      <td><span style="color:var(--primary)">${n(s)}</span></td>
      <td><span style="color:${a};display:inline-flex;align-items:center;gap:4px">${e(o)} ${n(r)}</span></td>
      <td>${n(t.msg)}</td>
    </tr>`}).join(``);a.innerHTML=`
<h2 style="font-size:20px;margin-bottom:16px;color:var(--text-main)">${e(`log`,`ui-icon-lg`)} webui日志</h2>
<p style="color:var(--text-muted);font-size:calc(var(--font-base) - 2px);margin:-8px 0 16px">此处是 WebUI TMDB 相关操作的日志，并非主程序的日志</p>
<div class="toolbar">
  <button class="toolbar-btn" id="logs-refresh">${e(`refresh`)} 刷新</button>
  <span style="color:var(--text-muted);font-size:calc(var(--font-base) - 1px)">共 ${o.count} 条</span>
</div>
<table>
<thead><tr><th>时间</th><th>操作类型</th><th>级别</th><th>消息</th></tr></thead>
<tbody>${d||`<tr><td colspan="4" style="text-align:center;color:var(--text-muted)">暂无日志</td></tr>`}</tbody>
</table>`,document.getElementById(`logs-refresh`).addEventListener(`click`,()=>{r(()=>import(`./core-B6fuJhLL.js`).then(e=>e.f).then(e=>e.router()),[],import.meta.url)})}export{a as renderLogs};