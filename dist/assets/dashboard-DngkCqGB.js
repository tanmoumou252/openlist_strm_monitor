import{N as e,O as t,P as n,S as r,c as i,j as a,k as o,m as s,n as c,r as l}from"./core-B6fuJhLL.js";async function u(){try{let e=await i(`/api/main/status`),t=document.getElementById(`main-status-dot`),n=document.getElementById(`main-status-text`),r=document.getElementById(`main-uptime-text`),a=document.getElementById(`main-start-btn`),o=document.getElementById(`main-stop-btn`);if(!t||!n)return;e.running?(t.style.background=`#4caf50`,t.style.boxShadow=`0 0 12px rgba(76,175,80,0.6)`,n.textContent=`主程序运行中`,n.style.color=`var(--text-main)`,e.uptime&&(r.textContent=`已运行 ${Math.floor(e.uptime/3600)}小时 ${Math.floor(e.uptime%3600/60)}分 ${e.uptime%60}秒`),a&&(a.style.display=`none`),o&&(o.style.display=`inline-flex`)):(t.style.background=`#f44336`,t.style.boxShadow=`0 0 12px rgba(244,67,54,0.6)`,n.textContent=`主程序已停止`,n.style.color=`var(--text-main)`,r.textContent=`点击启动按钮开始同步服务`,a&&(a.style.display=`inline-flex`),o&&(o.style.display=`none`))}catch{}}async function d(){c(`启动主程序`,`确定要启动主程序吗？这将开始 STRM 同步服务。`,async()=>{let e=document.getElementById(`main-start-btn`);e&&(e.disabled=!0,e.innerHTML=`<span class="spinner-small"></span> 启动中...`);try{let t=await i(`/api/main/start`,{method:`POST`});t.success?(l(`主程序已启动`,`success`),u()):(l(`启动失败: `+(t.message||`未知错误`),`error`),e&&(e.disabled=!1,e.innerHTML=`${n(`refresh`)} 启动主程序`))}catch(t){l(`启动请求失败: `+t.message,`error`),e&&(e.disabled=!1,e.innerHTML=`${n(`refresh`)} 启动主程序`)}})}async function f(){c(`停止主程序`,`确定要停止主程序吗？这将停止所有 STRM 同步服务。`,async()=>{let e=document.getElementById(`main-stop-btn`);e&&(e.disabled=!0,e.innerHTML=`<span class="spinner-small"></span> 停止中...`);try{let t=await i(`/api/main/stop`,{method:`POST`});t.success?(l(`主程序已停止`,`success`),u()):(l(`停止失败: `+(t.message||`未知错误`),`error`),e&&(e.disabled=!1,e.innerHTML=`${n(`check`)} 停止主程序`))}catch(t){l(`停止请求失败: `+t.message,`error`),e&&(e.disabled=!1,e.innerHTML=`${n(`check`)} 停止主程序`)}})}async function p(e){let c=await i(`/api/dashboard`);c.uptime!=null&&o(Date.now()-c.uptime*1e3),e.innerHTML=`
<h2 class="page-header">${n(`dashboard`,`ui-icon-lg`)} 仪表盘</h2>

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
    <button class="md3-btn filled" id="main-start-btn" style="display:none">${n(`refresh`)} 启动主程序</button>
    <button class="md3-btn tonal" id="main-stop-btn" style="display:none">${n(`check`)} 停止主程序</button>
  </div>
</div>

<div class="stat-grid">
  <div class="stat-card"><div class="label">${n(`movie`)} A 区 STRM</div><div class="value">${c.a_count}</div></div>
  <div class="stat-card"><div class="label">${n(`tv`)} B 区 STRM</div><div class="value">${c.b_count}</div></div>
  <div class="stat-card"><div class="label">${n(`area_c`)} C 区幽灵</div><div class="value">${c.c_count}</div></div>
  <div class="stat-card"><div class="label">B - valid</div><div class="value stat-value-primary">${c.b_valid}</div></div>
  <div class="stat-card"><div class="label">B - orphan</div><div class="value stat-value-warning">${c.b_orphan}</div></div>
  <div class="stat-card"><div class="label">B - unknown</div><div class="value stat-value-error">${c.b_unknown}</div></div>
  <div class="stat-card"><div class="label">${n(`tmdb`)} TMDB</div><div class="value stat-value-large">${c.tmdb_configured?`已配置`:`未配置`}</div></div>
  <div class="stat-card"><div class="label">WebUI 运行时间</div><div class="value stat-value-large" id="uptime-val">-</div></div>
</div>`,document.getElementById(`main-start-btn`)?.addEventListener(`click`,d),document.getElementById(`main-stop-btn`)?.addEventListener(`click`,f),u(),a(),r&&clearInterval(r),t(setInterval(u,s.MAIN_STATUS_POLL_INTERVAL))}export{p as renderDashboard,u as updateMainStatus,e as updateUptime};