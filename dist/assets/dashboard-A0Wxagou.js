import{A as e,F as t,M as n,P as r,S as i,k as a,l as o,m as s,n as c,p as l,r as u}from"./core-iN5uWBSS.js";async function d(){try{let e=await l(`/api/main/status`),t=document.getElementById(`main-status-dot`),n=document.getElementById(`main-status-text`),r=document.getElementById(`main-uptime-text`),i=document.getElementById(`main-start-btn`),a=document.getElementById(`main-stop-btn`);if(!t||!n)return;e.running?(t.style.background=`#4caf50`,t.style.boxShadow=`0 0 12px rgba(76,175,80,0.6)`,n.textContent=`主程序运行中`,n.style.color=`var(--text-main)`,e.uptime&&(r.textContent=`已运行 ${Math.floor(e.uptime/3600)}小时 ${Math.floor(e.uptime%3600/60)}分 ${e.uptime%60}秒`),i&&(i.style.display=`none`),a&&(a.style.display=`inline-flex`)):(t.style.background=`#f44336`,t.style.boxShadow=`0 0 12px rgba(244,67,54,0.6)`,n.textContent=`主程序已停止`,n.style.color=`var(--text-main)`,r.textContent=`点击启动按钮开始同步服务`,i&&(i.style.display=`inline-flex`),a&&(a.style.display=`none`))}catch{}}async function f(){c(`启动主程序`,`确定要启动主程序吗？这将开始 STRM 同步服务。`,async()=>{let e=document.getElementById(`main-start-btn`);e&&(e.disabled=!0,e.innerHTML=`<span class="spinner-small"></span> 启动中...`);try{let n=await l(`/api/main/start`,{method:`POST`});n.success?(u(`主程序已启动`,`success`),d()):(u(`启动失败: `+(n.message||`未知错误`),`error`),e&&(e.disabled=!1,e.innerHTML=`${t(`refresh`)} 启动主程序`))}catch(n){u(`启动请求失败: `+n.message,`error`),e&&(e.disabled=!1,e.innerHTML=`${t(`refresh`)} 启动主程序`)}})}async function p(){c(`停止主程序`,`确定要停止主程序吗？这将停止所有 STRM 同步服务。`,async()=>{let e=document.getElementById(`main-stop-btn`);e&&(e.disabled=!0,e.innerHTML=`<span class="spinner-small"></span> 停止中...`);try{let n=await l(`/api/main/stop`,{method:`POST`});n.success?(u(`主程序已停止`,`success`),d()):(u(`停止失败: `+(n.message||`未知错误`),`error`),e&&(e.disabled=!1,e.innerHTML=`${t(`check`)} 停止主程序`))}catch(n){u(`停止请求失败: `+n.message,`error`),e&&(e.disabled=!1,e.innerHTML=`${t(`check`)} 停止主程序`)}})}async function m(r){let c=await l(`/api/dashboard`);o()||(c.uptime!=null&&e(Date.now()-c.uptime*1e3),r.innerHTML=`
<h2 class="page-header">${t(`dashboard`,`ui-icon-lg`)} 仪表盘</h2>

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
    <button class="md3-btn filled" id="main-start-btn" style="display:none">${t(`refresh`)} 启动主程序</button>
    <button class="md3-btn tonal" id="main-stop-btn" style="display:none">${t(`check`)} 停止主程序</button>
  </div>
</div>

<div class="stat-grid">
  <div class="stat-card"><div class="label">${t(`movie`)} A 区 STRM</div><div class="value">${c.a_count}</div></div>
  <div class="stat-card"><div class="label">${t(`tv`)} B 区 STRM</div><div class="value">${c.b_count}</div></div>
  <div class="stat-card"><div class="label">${t(`area_c`)} C 区幽灵</div><div class="value">${c.c_count}</div></div>
<div class="stat-card"><div class="label">B - valid</div><div class="value stat-value-primary">${c.b_valid}</div></div>
	  <div class="stat-card"><div class="label">B - duplicate</div><div class="value stat-value-warning">${c.b_duplicate}</div></div>
	  <div class="stat-card"><div class="label">B - quarantined</div><div class="value stat-value-error">${c.b_quarantined}</div></div>
  <div class="stat-card"><div class="label">${t(`tmdb`)} TMDB</div><div class="value stat-value-large">${c.tmdb_configured?`已配置`:`未配置`}</div></div>
  <div class="stat-card"><div class="label">WebUI 运行时间</div><div class="value stat-value-large" id="uptime-val">-</div></div>
</div>
	
	  <!-- 密码提示 -->
	  <div style="text-align:center;font-size:12px;color:var(--text-muted);margin-top:8px">
	    管理密码保存在 WebUI 控制台日志中 · 忘记密码可运行 <code style="background:var(--bg-control);padding:1px 4px;border-radius:3px">python reset_admin.py</code> 重置
	  </div>`,document.getElementById(`main-start-btn`)?.addEventListener(`click`,f),document.getElementById(`main-stop-btn`)?.addEventListener(`click`,p),d(),n(),i&&clearInterval(i),a(setInterval(d,s.MAIN_STATUS_POLL_INTERVAL)))}export{m as renderDashboard,d as updateMainStatus,r as updateUptime};