import{A as e,C as t,F as n,I as r,N as i,h as a,j as o,m as s,n as c,o as l,r as u,u as d}from"./core-B71bPzFl.js";var f=null;async function p(){try{return await s(`/api/config/status`)}catch{return null}}async function m(){try{await s(`/api/webui/config/ui`,{method:`POST`,body:JSON.stringify({onboarding_completed:`1`})})}catch{}}async function h(){try{await s(`/api/webui/config/ui`,{method:`POST`,body:JSON.stringify({onboarding_completed:`0`})})}catch{}}function g(e){if(!e||e.onboarding_completed)return``;let t=[{key:`password`,label:`确认管理员密码`,done:e.password_set,link:`#config`,linkText:`前往配置`,message:`首次启动时系统已自动生成随机密码并打印到控制台（仅显示一次，不写入日志）。遗忘或需自定义密码，请运行 reset_admin.py。`},{key:`tmdb`,label:`配置 TMDB`,done:e.tmdb_configured,link:`#config?sub=config`,linkText:`前往配置`,message:`配置 TMDB API Token 以启用待看列表和影视信息获取功能（可选）。`},{key:`openlist`,label:`配置 OpenList`,done:e.openlist_configured,link:`#config?sub=openlist`,linkText:`前往配置`,message:`填写 OpenList WebDAV 地址、用户名和密码，以连接 STRM 引擎。`},{key:`main`,label:`启动主程序`,done:e.main_running,link:null,linkText:`点击下方启动按钮`,message:`完成以上配置后，点击「启动主程序」按钮开始同步服务。`},{key:`view_ab`,label:`查看 A/B 分区`,done:e.view_ab_completed||!1,link:`#area_a`,linkText:`前往查看`,message:`浏览 A 区和 B 区的文件列表，了解同步状态。`},{key:`tmdb_refresh`,label:`刷新 TMDB 待看列表`,done:e.tmdb_refresh_completed||!1,link:`#config?sub=config`,linkText:`前往刷新`,message:`点击「刷新待看列表」按钮，从 TMDB 获取最新数据。`},{key:`tmdb_match`,label:`检测 TMDB 收录状态`,done:e.tmdb_match_completed||!1,link:`#config?sub=config`,linkText:`前往检测`,message:`点击「刷新收录状态」按钮，检测本地文件是否已收录到 TMDB。`}],n=t.filter(e=>!e.done).length,i=n===0,a=t.map((e,t)=>`
    <div class="onboarding-step ${e.done?`done`:``}">
      <div class="onboarding-step-indicator">
        ${e.done?r(`check`):`<span>${t+1}</span>`}
      </div>
      <div class="onboarding-step-content">
        <div class="onboarding-step-label">${l(e.label)}</div>
        <div class="onboarding-step-message">${l(e.message)}</div>
        ${!e.done&&e.link?`<a href="${e.link}" class="onboarding-step-link">${l(e.linkText)} →</a>`:``}
        ${!e.done&&!e.link?`<span class="onboarding-step-hint">${l(e.linkText)}</span>`:``}
        ${!e.done&&e.key!==`password`&&e.key!==`tmdb`&&e.key!==`openlist`&&e.key!==`main`?`<button class="onboarding-step-complete-btn" data-step="${e.key}">标记完成</button>`:``}
      </div>
    </div>
  `).join(``);return`
    <div class="onboarding-card" id="onboarding-card">
      <div class="onboarding-header">
        <div class="onboarding-title">
          ${r(`menu_book`,`ui-icon-lg`)} 初次使用
        </div>
        <div class="onboarding-progress">
          ${t.length-n} / ${t.length} 已完成
        </div>
      </div>
      <div class="onboarding-steps">
        ${a}
      </div>
      <div class="onboarding-footer">
        ${i?`<button class="md3-btn filled" id="onboarding-complete-btn">${r(`check`)} 完成引导</button>`:`<button class="md3-btn tonal" id="onboarding-skip-btn">跳过引导</button>`}
      </div>
    </div>
  `}function _(){let e=document.getElementById(`onboarding-skip-btn`),t=document.getElementById(`onboarding-complete-btn`),n=document.getElementById(`onboarding-restart-btn`);e&&e.addEventListener(`click`,async()=>{await m(),f&&(f.onboarding_completed=!0);let e=document.getElementById(`onboarding-card`);e&&e.remove();let t=document.getElementById(`onboarding-quick-btn`);t&&(t.style.display=`inline-flex`),u(`已跳过引导，可随时在仪表盘重新显示`,`info`)}),t&&t.addEventListener(`click`,async()=>{await m(),f&&(f.onboarding_completed=!0);let e=document.getElementById(`onboarding-card`);e&&e.remove();let t=document.getElementById(`onboarding-quick-btn`);t&&(t.style.display=`inline-flex`),u(`引导已完成`,`success`)}),n&&n.addEventListener(`click`,async()=>{await h(),v(),u(`引导已重新开始`,`success`)}),document.querySelectorAll(`.onboarding-step-complete-btn`).forEach(e=>{e.addEventListener(`click`,async()=>{let t=e.dataset.step;try{await s(`/api/onboarding/complete-step`,{method:`POST`,body:JSON.stringify({step:t})}),await v(),u(`步骤已标记完成`,`success`)}catch(e){u(`标记失败: `+e.message,`error`)}})})}async function v(){let e=await p();f=e;let t=document.getElementById(`onboarding-container`);t&&(t.innerHTML=g(e),_());let n=document.getElementById(`onboarding-quick-btn`);n&&(e&&e.onboarding_completed?n.style.display=`inline-flex`:n.style.display=`none`)}async function y(){try{return await s(`/api/config/validate`,{method:`POST`})}catch(e){return{ok:!1,error:e.message}}}function b(e){if(e.ok)return null;let t=(e.checks||[]).map(e=>{let t=e.status===`ok`?r(`check`):e.status===`warning`?r(`warn`):e.status===`skipped`?r(`info`):r(`error`);return`
      <div class="preflight-check ${`preflight-${e.status}`}">
        <div class="preflight-check-icon">${t}</div>
        <div class="preflight-check-content">
          <div class="preflight-check-label">${l(e.label)}</div>
          <div class="preflight-check-message">${l(e.message)}</div>
          ${e.suggestion?`<div class="preflight-check-suggestion">${l(e.suggestion)}</div>`:``}
        </div>
      </div>
    `}).join(``);return`
    <div class="preflight-dialog">
      <div class="preflight-header">
        ${r(`warn`)} 启动前检查未通过
      </div>
      <div class="preflight-checks">
        ${t}
      </div>
      <div class="preflight-footer">
        请修复以上问题后再启动主程序。
      </div>
    </div>
  `}async function x(){try{let e=await s(`/api/main/status`),t=document.getElementById(`main-status-dot`),n=document.getElementById(`main-status-text`),r=document.getElementById(`main-uptime-text`),i=document.getElementById(`main-start-btn`),a=document.getElementById(`main-stop-btn`);if(!t||!n)return;e.running?(t.style.background=`#4caf50`,t.style.boxShadow=`0 0 12px rgba(76,175,80,0.6)`,n.textContent=`主程序运行中`,n.style.color=`var(--text-main)`,e.uptime&&(r.textContent=`已运行 ${Math.floor(e.uptime/3600)}小时 ${Math.floor(e.uptime%3600/60)}分 ${e.uptime%60}秒`),i&&(i.style.display=`none`),a&&(a.style.display=`inline-flex`)):(t.style.background=`#f44336`,t.style.boxShadow=`0 0 12px rgba(244,67,54,0.6)`,n.textContent=`主程序已停止`,n.style.color=`var(--text-main)`,r.textContent=`点击启动按钮开始同步服务`,i&&(i.style.display=`inline-flex`),a&&(a.style.display=`none`))}catch{}}async function S(){let e=await y();if(!e.ok){let t=b(e);t&&c(`启动前检查未通过`,t,`知道了`,`取消`,{htmlContent:!0});return}c(`启动主程序`,`确定要启动主程序吗？这将开始 STRM 同步服务。`,async()=>{let e=document.getElementById(`main-start-btn`);e&&(e.disabled=!0,e.innerHTML=`<span class="spinner-small"></span> 启动中...`);try{let t=await s(`/api/main/start`,{method:`POST`});t.success?(u(`主程序已启动`,`success`),x(),v()):(u(`启动失败: `+(t.message||`未知错误`),`error`),e&&(e.disabled=!1,e.innerHTML=`${r(`refresh`)} 启动主程序`))}catch(t){u(`启动请求失败: `+t.message,`error`),e&&(e.disabled=!1,e.innerHTML=`${r(`refresh`)} 启动主程序`)}})}async function C(){c(`停止主程序`,`确定要停止主程序吗？这将停止所有 STRM 同步服务。`,async()=>{let e=document.getElementById(`main-stop-btn`);e&&(e.disabled=!0,e.innerHTML=`<span class="spinner-small"></span> 停止中...`);try{let t=await s(`/api/main/stop`,{method:`POST`});t.success?(u(`主程序已停止`,`success`),x()):(u(`停止失败: `+(t.message||`未知错误`),`error`),e&&(e.disabled=!1,e.innerHTML=`${r(`check`)} 停止主程序`))}catch(t){u(`停止请求失败: `+t.message,`error`),e&&(e.disabled=!1,e.innerHTML=`${r(`check`)} 停止主程序`)}})}async function w(n){let c=await s(`/api/dashboard`);if(d())return;c.uptime!=null&&o(Date.now()-c.uptime*1e3),n.innerHTML=`
<div class="dashboard-header-row" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
  <h2 class="page-header" style="margin:0">${r(`dashboard`,`ui-icon-lg`)} 仪表盘</h2>
  <button class="onboarding-quick-btn" id="onboarding-quick-btn" title="初次使用" style="display:none">
    ${r(`menu_book`)} <span>初次使用</span>
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
    <button class="md3-btn filled" id="main-start-btn" style="display:none">${r(`refresh`)} 启动主程序</button>
    <button class="md3-btn tonal" id="main-stop-btn" style="display:none">${r(`check`)} 停止主程序</button>
  </div>
</div>

<div class="stat-grid">
  <div class="stat-card"><div class="label">${r(`movie`)} A 区 STRM</div><div class="value">${c.a_count}</div></div>
  <div class="stat-card"><div class="label">${r(`tv`)} B 区 STRM</div><div class="value">${c.b_count}</div></div>
  <div class="stat-card"><div class="label">${r(`area_c`)} C 区幽灵</div><div class="value">${c.c_count}</div></div>
<div class="stat-card"><div class="label">B - valid</div><div class="value stat-value-primary">${c.b_valid}</div></div>
	  <div class="stat-card"><div class="label">B - duplicate</div><div class="value stat-value-warning">${c.b_duplicate}</div></div>
	  <div class="stat-card"><div class="label">B - quarantined</div><div class="value stat-value-error">${c.b_quarantined}</div></div>
  <div class="stat-card"><div class="label">${r(`tmdb`)} TMDB</div><div class="value stat-value-large">${c.tmdb_configured?`已配置`:`未配置`}</div></div>
  <div class="stat-card"><div class="label">WebUI 运行时间</div><div class="value stat-value-large" id="uptime-val">-</div></div>
</div>

<!-- 索引元数据（Task 2） -->
<div class="stat-grid" style="margin-top:16px">
  <div class="stat-card"><div class="label">${r(`update`)} 索引代次</div><div class="value stat-value-primary">#${c.index_metadata?.index_generation||0}</div></div>
  <div class="stat-card"><div class="label">${r(`schedule`)} 最近索引</div><div class="value" title="${f(c.index_metadata?.last_full_index_at)}">${c.index_metadata?.last_full_index_at?u(c.index_metadata.last_full_index_at):`暂无记录`}</div></div>
  <div class="stat-card"><div class="label">${r(`link`)} 映射版本</div><div class="value" title="${l(c.index_metadata?.mapping_version||``)}">${c.index_metadata?.mapping_version?c.index_metadata.mapping_version.substring(0,8)+`...`:`-`}</div></div>
  <div class="stat-card"><div class="label">映射版本生成</div><div class="value" title="${f(c.index_metadata?.mapping_version_generated_at)}">${c.index_metadata?.mapping_version_generated_at?u(c.index_metadata.mapping_version_generated_at):`暂无记录`}</div></div>
</div>

<!-- A'.3: 立即全量审计按钮 -->
<div style="margin-top:12px;display:flex;gap:8px;align-items:center">
  <button class="toolbar-btn secondary" id="btn-run-full-audit" style="font-size:calc(var(--font-base) - 1px)">${r(`play_arrow`)} 立即全量审计</button>
  <span id="audit-status-text" style="font-size:calc(var(--font-base) - 1px);color:var(--text-muted)"></span>
</div>

<!-- Mapping 列表（Task 2） -->
${c.mappings&&c.mappings.length>0?`
<div style="margin-top:16px">
  <div style="font-size:14px;font-weight:500;margin-bottom:8px;color:var(--text-primary)">映射配置</div>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px">
    ${c.mappings.map(e=>`
      <div style="background:var(--bg-surface-variant);padding:12px;border-radius:8px;border:1px solid var(--border-subtle)">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <span style="font-weight:500;color:var(--text-primary)">${l(e.label||e.mapping_id)}</span>
          <span style="font-size:11px;color:var(--text-muted)">#${e.index_generation||0}</span>
        </div>
        <div style="font-size:12px;color:var(--text-secondary);margin-bottom:4px">
          <div>A: ${l(p(e.a_root))}</div>
          <div>B: ${l(p(e.b_root))}</div>
        </div>
        <div style="font-size:11px;color:var(--text-muted)">
          索引时间: <span title="${f(e.index_generation_at)}">${e.index_generation_at?u(e.index_generation_at):`未索引`}</span>
        </div>
      </div>
    `).join(``)}
  </div>
</div>
`:``}
	
	  <!-- 密码提示 -->
	  <div style="text-align:center;font-size:12px;color:var(--text-muted);margin-top:8px">
      管理密码仅在首次启动时打印到控制台（不写入日志） · 忘记密码可运行 <code style="background:var(--bg-control);padding:1px 4px;border-radius:3px">python reset_admin.py</code> 重置
	  </div>`;function u(e){if(!e||e===0)return`未知`;try{let t=new Date(e*1e3),n=new Date-t,r=Math.floor(n/6e4),i=Math.floor(n/36e5),a=Math.floor(n/864e5);return r<1?`刚刚`:r<60?`${r}分钟前`:i<24?`${i}小时前`:a<7?`${a}天前`:t.toLocaleDateString(`zh-CN`,{year:`numeric`,month:`2-digit`,day:`2-digit`,hour:`2-digit`,minute:`2-digit`})}catch{return`未知`}}function f(e){if(!e||e===0)return`暂无记录`;try{let t=new Date(e*1e3),n=e=>String(e).padStart(2,`0`);return`${t.getFullYear()}-${n(t.getMonth()+1)}-${n(t.getDate())} ${n(t.getHours())}:${n(t.getMinutes())}:${n(t.getSeconds())}`}catch{return`暂无记录`}}function p(e){if(!e)return`/`;let t=e.split(`/`).filter(Boolean);return t.length<=2?`/`+t.join(`/`):`/`+t.slice(0,2).join(`/`).replace(/\/$/,``)+`/...`}document.getElementById(`main-start-btn`)?.addEventListener(`click`,S),document.getElementById(`main-stop-btn`)?.addEventListener(`click`,C);let m=document.getElementById(`onboarding-quick-btn`);m&&m.addEventListener(`click`,async()=>{try{await s(`/api/webui/config/ui`,{method:`POST`,body:JSON.stringify({onboarding_completed:`0`})})}catch(e){console.error(`Failed to reset onboarding:`,e)}await v()});let h=document.getElementById(`btn-run-full-audit`),g=document.getElementById(`audit-status-text`);h&&h.addEventListener(`click`,async()=>{if(confirm(`确定要执行全量审计吗？

这是一个重操作，耗时取决于 A 区库大小，会扫描全部 A 区根目录（含机械硬盘）。不会删除任何文件。`)){h.disabled=!0,h.innerHTML=`审计中...`,g&&(g.textContent=`正在启动审计...`);try{if((await s(`/api/index/audit`,{method:`POST`})).status===`already_running`){g&&(g.textContent=`审计已在进行中`),h.disabled=!1,h.innerHTML=`${r(`play_arrow`)} 立即全量审计`;return}for(let e=0;e<300;e++){await new Promise(e=>setTimeout(e,2e3));try{let t=await s(`/api/index/audit/status`);if(!t.running&&t.result){t.result.error?g&&(g.textContent=`审计失败: `+t.result.error):g&&(g.textContent=`审计完成，索引代次 #`+(t.result.index_generation||0)),h.disabled=!1,h.innerHTML=`${r(`play_arrow`)} 立即全量审计`;try{let e=await s(`/api/dashboard`);if(e&&e.index_metadata){let t=document.querySelector(`.stat-value-primary`);t&&(t.textContent=`#`+(e.index_metadata.index_generation||0))}}catch{}return}g&&(g.textContent=`审计进行中... (`+e*2+`s)`)}catch{}}g&&(g.textContent=`审计超时，请稍后重试`),h.disabled=!1,h.innerHTML=`${r(`play_arrow`)} 立即全量审计`}catch(e){g&&(g.textContent=`审计请求失败: `+e.message),h.disabled=!1,h.innerHTML=`${r(`play_arrow`)} 立即全量审计`}}}),v(),x(),i(),t&&clearInterval(t),e(setInterval(x,a.MAIN_STATUS_POLL_INTERVAL))}export{w as renderDashboard,x as updateMainStatus,n as updateUptime};