const __vite__mapDeps=(i,m=__vite__mapDeps,d=(m.f||(m.f=["./dashboard-DngkCqGB.js","./core-B6fuJhLL.js"])))=>i.map(i=>d[i]);
import{A as e,F as t,I as n,L as r,M as i,O as a,P as o,S as s,c,d as l,h as u,i as d,j as f,l as p,m,n as h,o as g,p as _,r as v,u as y,x as b}from"./core-B6fuJhLL.js";(function(){let e=document.createElement(`link`).relList;if(e&&e.supports&&e.supports(`modulepreload`))return;for(let e of document.querySelectorAll(`link[rel="modulepreload"]`))n(e);new MutationObserver(e=>{for(let t of e)if(t.type===`childList`)for(let e of t.addedNodes)e.tagName===`LINK`&&e.rel===`modulepreload`&&n(e)}).observe(document,{childList:!0,subtree:!0});function t(e){let t={};return e.integrity&&(t.integrity=e.integrity),e.referrerPolicy&&(t.referrerPolicy=e.referrerPolicy),e.crossOrigin===`use-credentials`?t.credentials=`include`:e.crossOrigin===`anonymous`?t.credentials=`omit`:t.credentials=`same-origin`,t}function n(e){if(e.ep)return;e.ep=!0;let n=t(e);fetch(e.href,n)}})();var x={webdav_host:`OpenList 的 WebDAV 服务地址。
格式：http://IP:端口/dav
例如：http://127.0.0.1:5244/dav`,webdav_user:`必须使用具有管理员权限的账户，以调用 API 刷新路径。`,webdav_password:`WebDAV 登录密码。
保存时会加密存储。`,webdav_totp_secret:`两步验证密钥（可选）。
如果 OpenList 开启了两步验证，需要填写此字段。`,b_root:`媒体库实际扫描的目录，程序会把 A 区 STRM 同步到这里。`,c_root:`用于幽灵迁移、异常隔离等场景。`,monitored_paths:`该引擎下挂载的真实云盘目录（如 /天翼云/番剧）。
选择引擎入口后自动从 API 获取，不可手动输入。
引擎配置变更时，刷新路径区域会自动补全缺失的子路径（仅添加，不覆盖用户手动配置的路径）。`,refresh_paths:`主动刷新路径列表，格式为"引擎入口/文件夹名"（如 /strm/电影）。
程序会定期请求这些路径，让 OpenList 重新扫描目录并生成 STRM。

引擎配置变更时，系统会自动补全缺失的子路径（仅添加不覆盖），但你可随时手动增删。
已保存的刷新路径在引擎配置再变动时不会丢失。`,strm_engines:`STRM 引擎配置。
选择引擎入口后，自动填充该引擎挂载的真实云盘目录（仅作引擎配置 / A 区派生用）。

刷新路径区域（下方"刷新配置"）会在此变更时自动补全缺失的引擎子路径，不会覆盖你已有的手动配置。
只有已配置的引擎才会显示在 A 区文件夹中。`,refresh_enabled:`是否启用主动刷新。
false：只依赖文件系统事件和删除后的延迟清理。
true：程序会周期性扫描 refresh_paths 中的 WebDAV 路径。`,refresh_interval_minutes:`主动刷新的时间间隔（分钟）。
建议：5-30 分钟，根据媒体库大小调整。`,refresh_depth:`WebDAV 主动刷新递归深度。数值越大，请求越多。
建议：测试 2~3，正式 3~5。`,behavior_action:`B 区 STRM 被删除后，对 WebDAV 源文件执行的动作。
MOVE：移动到回收站目录（推荐）。
DELETE：直接删除（危险，不建议）。`,behavior_trash_dir_name:`WebDAV 回收站目录名。
action = MOVE 时生效。移动的路径由 STRM 文件内容反向拼凑。`,behavior_ghost_protect_seconds:`ghost 保护时间，单位：秒。
B 区删除 STRM 后，A 区可能因为同步延迟又短暂生成同一 STRM。
ghost 保护用于阻止刚删除的内容被立刻重新同步回 B 区。
建议：至少 300 秒。`,behavior_a_to_b_restore_delay_seconds:`A→B 恢复延迟时间（秒）。
当 A 区重新生成 STRM 后，延迟多久再同步到 B 区。
建议：30-60 秒。`,behavior_sync_on_startup:`启动时是否执行全量同步。
true：启动时同步所有 STRM 文件。
false：只同步增量变更。`,behavior_sync_on_startup_wait:`启动等待时间（秒）。
启动后等待多久再开始同步，给系统预留初始化时间。
建议：0-10 秒。`,log_level:`日志记录级别。
DEBUG：最详细，包含所有调试信息。
INFO：常规信息（推荐）。
WARNING：只记录警告和错误。
ERROR：只记录错误。`,log_max_size_mb:`单个日志文件最大大小（MB）。
超过此大小会自动轮转。
建议：2-10 MB。`,log_backup_count:`保留的历史日志文件数量。
超过此数量的旧日志会被删除。
建议：5-10 个。`,log_file:`日志文件保存路径（相对路径相对项目根目录）。
留空使用默认 ./activity.log。
修改并保存后，重启 WebUI / 主程序即按此路径与上面设置的级别写日志。`};function S(e,t=!1){let n=x[e.replace(/-/g,`_`)];if(!n)return``;let r=g(n);return`<span class="ol-help-icon${t?` tooltip-below`:``}" data-tooltip="${r}" aria-label="帮助">${o(`info`)}</span>`}async function C(e,t){let n={};try{let e=await c(`/api/webui/config/openlist`);e.success&&e.config&&(n=e.config)}catch{}try{let e=await c(`/api/openlist/strm-engines`);e.success&&(u.availableEngines=e.engines||[])}catch{u.availableEngines=[]}u.strmEngines=[];try{n.strm_engines&&(u.strmEngines=JSON.parse(n.strm_engines))}catch{u.strmEngines=[]}u.strmEngines.length||(u.strmEngines=[{engine:``,monitored_paths:[]}]),u.configured=!!(n.webdav_host&&n.webdav_host.trim()),M();let r=[];try{n.refresh_paths&&(r=JSON.parse(n.refresh_paths))}catch{r=[]}window._openlistRefreshPaths=r.slice();function i(e,t,n,r,i=`text`,a=!1,o=!1){return d(e,t,n,{placeholder:r,type:i,persistLabel:a,readOnly:o,helpIcon:S(e.replace(/^ol-/,``))})}function a(e,t,n,r,i=!1){let a=n!=null&&String(n).trim()!==``,o=a||i?`floating-label is-shown is-floating${a?` is-filled`:``}`:`floating-label`,s=i?` data-persist-label="1"`:``,c=r.map(e=>{let t=e.value===n?` selected`:``;return`<option value="${g(e.value)}"${t}>${g(e.label)}</option>`}).join(``);return`
      <div class="floating-field" data-field="${e}">
        <div class="field-control">
          <label class="${o}" data-role="label" for="${e}">${g(t)}${S(e.replace(/^ol-/,``))}</label>
          <select id="${e}" class="ol-select"${s}>${c}</select>
        </div>
      </div>`}function s(e,t,n){return`<div class="toggle-row">
      <span>${g(t)}${S(e.replace(/^ol-/,``))}</span>
      <div class="segmented-switch" data-key="${e}" id="${e}">
        <button type="button" data-value="on"${n?` class="active"`:``}>开</button>
        <button type="button" data-value="off"${n?``:` class="active"`}>关</button>
      </div>
    </div>`}function l(){let e=`<div class="strm-engine-wrap"><span class="monitored-paths-help">`+S(`monitored_paths`)+`</span>`;e+=`<table class="strm-engine-table"><thead><tr><th style="width:35%">STRM 引擎入口</th><th>监控目录</th><th style="width:60px">操作</th></tr></thead><tbody>`,u.strmEngines.forEach((t,n)=>{e+=f(n,t,u.strmEngines.length)}),e+=`</tbody></table>`;let t=u.configured?``:` disabled`;return e+=`<div class="table-actions">
      <button class="table-btn primary" id="add-engine-row"${t}>+ 添加行</button>
    </div></div>`,e}function f(e,t,n){let r=`<option value="">选择引擎...</option>`,i=new Set(u.strmEngines.map((t,n)=>n===e?``:t.engine).filter(Boolean));u.availableEngines.forEach(e=>{let n=t.engine===e.mount_path?` selected`:``,a=!n&&i.has(e.mount_path)?` disabled`:``;r+=`<option value="${g(e.mount_path)}"${n}${a}>${g(e.mount_path)}</option>`});let a=u.configured?``:` disabled`,o=``;return t.monitored_paths&&t.monitored_paths.length&&t.monitored_paths.forEach((t,n)=>{o+=`<span class="tag">${g(t)}<button class="tag-remove" data-row="${e}" data-pi="${n}" title="删除">×</button></span>`}),`<tr data-row-idx="${e}">
      <td>
        <select class="engine-select" data-row="${e}"${a}>
          ${r}
        </select>
      </td>
      <td>
        <div class="tag-container" data-row="${e}" id="tag-container-${e}">
          ${o||`<span style="color:var(--text-muted);font-size:calc(var(--font-base) - 1px)">选择引擎后自动填充</span>`}
        </div>
      </td>
      <td style="text-align:center">
        <button class="table-btn danger" data-delete-row="${e}" title="删除此行">删除</button>
      </td>
    </tr>`}let p=`<div class="openlist-form">`;p+=`<div class="config-section"><h3>OpenList 连接</h3>
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" style="width:20px;height:20px;flex-shrink:0"><path fill="#38bdf8" d="M244.57,776.75c-10.1,0-20.31-2.78-29.46-8.59-25.63-16.3-33.2-50.29-16.9-75.92l201.93-317.6c16.3-25.63,50.29-33.2,75.92-16.9,25.63,16.3,33.2,50.29,16.9,75.92l-201.93,317.6c-10.48,16.48-28.28,25.5-46.46,25.5Z"/><path fill="#99f6e4" d="M509.93,907.83c-35.01,0-67.29-4.84-91.84-13.86-15.63-5.74-27.82-18.25-33.15-34.03s-3.23-33.12,5.72-47.16l174.43-273.77c16.32-25.62,50.32-33.15,75.94-16.83,25.62,16.32,33.15,50.32,16.83,75.94l-126.68,198.82c25.39-1.89,54.61-7.42,84.56-19.13,71.29-27.87,127.46-82.26,158.15-153.15,30.53-70.52,31.94-147.78,3.98-217.56-28.43-70.95-82.76-126.78-152.98-157.2-70.23-30.42-147.71-31.59-218.17-3.27-73.46,29.52-126.75,82.48-158.4,157.4-11.82,27.98-44.08,41.08-72.07,29.27-27.98-11.82-41.08-44.08-29.27-72.07,42.86-101.46,118.49-176.38,218.71-216.66,49.54-19.91,101.59-29.4,154.67-28.2,51.11,1.15,100.99,12.12,148.25,32.6,47.14,20.42,89.3,49.27,125.31,85.75,37.27,37.75,66.22,81.98,86.05,131.46,19.64,49.01,28.94,100.74,27.64,153.75-1.26,51.15-12.28,101.08-32.77,148.42-20.54,47.45-49.51,89.78-86.11,125.81-38.15,37.56-82.88,66.53-132.94,86.09-42.5,16.61-89.11,26.08-134.81,27.39-3.71.11-7.4.16-11.05.16Z"/></svg>
      <span class="api-status-dot ${u.apiStatus}" id="ol-status-dot"></span>
      <span style="font-size:var(--font-base);color:var(--text-muted)" id="ol-status-text">${(()=>{let e=!!u.configured,t=u.apiStatus;return e?t===`online`?`OpenList 已连接`:t===`offline`?`OpenList 已配置（离线）`:t===`auth_failed_password`?`OpenList 密码错误`:t===`auth_failed_2fa`?`OpenList 2FA 错误`:t===`auth_failed`?`OpenList 认证失败`:`OpenList 已配置`:`OpenList 未配置`})()}</span>
    </div>
    <div class="field-grid">
      ${i(`ol-webdav-host`,`WebDAV 地址`,n.webdav_host||``,`http://127.0.0.1:5244/dav`)}
      ${i(`ol-webdav-user`,`用户名`,n.webdav_user||``,`admin`)}
      ${i(`ol-webdav-password`,`密码`,n.webdav_password||``,`输入密码`,`password`)}
      ${i(`ol-webdav-totp-secret`,`2FA 密钥`,n.webdav_totp_secret||``,`留空则不使用 2FA`,`password`)}
      ${i(`ol-b-root`,`B 区根目录`,n.b_root||t.b_root||``,`C:/path/to/b`)}
      ${i(`ol-c-root`,`C 区根目录`,n.c_root||t.c_root||``,`C:/path/to/c`)}
    </div>
    <div class="openlist-top-actions">
      <button class="toolbar-btn primary" id="ol-save">保存</button>
      <button class="toolbar-btn secondary" id="ol-test-connection">测试连接</button>
      <button class="toolbar-btn secondary" id="ol-restart-webui">${o(`refresh`)} 重启Bridge主程序</button>
      <span class="test-connection-hint">openlist api 测试通过，再保存, 必须使用管理员账户登录</span>
    </div>
  </div>`,p+=`<div class="config-section"><h3>${o(`area_a`)} STRM 引擎配置</h3>
    <p style="color:var(--text-muted);font-size:calc(var(--font-base) - 1px);margin:0 0 10px">选择 STRM 引擎入口 → 自动填充该引擎挂载的真实云盘目录（不可手动输入）。引擎配置变更时，下方"刷新路径"会自动补全对应的引擎子路径。</p>
    <div id="strm-engine-table-wrap">${l()}</div>
  </div>`,p+=`<div class="openlist-2col-grid">`;let m=(n.refresh_enabled||`true`).toLowerCase()===`true`,h=n.refresh_interval_minutes||`10`,_=n.refresh_depth||`5`;p+=`<div class="config-section"><h3>${o(`refresh`)} 刷新配置</h3>
    ${s(`ol-refresh-enabled`,`启用主动刷新`,m)}
    <div class="field-grid">
      ${i(`ol-refresh-interval`,`刷新间隔 (分钟)`,h,`10`,`number`)}
      ${i(`ol-refresh-depth`,`刷新深度`,_,`5`,`number`)}
    </div>
    <div style="margin-top:12px">
      <div style="font-size:calc(var(--font-base) - 1px);color:var(--text-muted);margin-bottom:6px;display:flex;align-items:center;gap:6px">
        <span>刷新路径</span>
        ${S(`refresh_paths`)}
      </div>
      <div class="refresh-paths-tags" id="refresh-paths-tags"></div>
      <div style="display:flex;gap:6px;margin-top:6px">
        <input type="text" id="refresh-path-input" placeholder="输入 WebDAV 路径" style="flex:1;padding:6px 10px;border:1px solid color-mix(in srgb,var(--border-color) 30%,transparent);border-radius:var(--radius-control);background:var(--bg-control);font:inherit;color:var(--text-main);font-size:calc(var(--font-base) - 1px)">
        <button class="table-btn" id="add-refresh-path-btn">添加</button>
      </div>
    </div>
  </div>`;let v=(n.behavior_sync_on_startup||`false`).toLowerCase()===`true`,y=n.behavior_action||`MOVE`;p+=`<div class="config-section"><h3>${o(`settings`)} 行为配置</h3>
    <div class="field-grid" style="grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:6px 12px">
      ${a(`ol-action`,`删除动作`,y,[{value:`MOVE`,label:`MOVE — 移动到回收站（推荐）`},{value:`DELETE`,label:`DELETE — 直接删除（危险）`}])}
      ${i(`ol-trash-dir`,`回收站目录名`,n.behavior_trash_dir_name||`trash`,`strm_回收站`)}
      ${i(`ol-ghost-protect`,`Ghost 保护时间 (秒)`,n.behavior_ghost_protect_seconds||`300`,`300`,`number`)}
      ${i(`ol-restore-delay`,`A→B 恢复延迟 (秒)`,n.behavior_a_to_b_restore_delay_seconds||`30`,`30`,`number`)}
      ${i(`ol-startup-wait`,`启动等待时间 (秒)`,n.behavior_sync_on_startup_wait||`0`,`0`,`number`)}
    </div>
    ${s(`ol-sync-startup`,`启动时全量同步`,v)}
  </div>`;let b=n.log_level||`INFO`;p+=`<div class="config-section"><h3>${o(`log`)} 日志配置</h3>
    <div class="field-grid">
      ${a(`ol-log-level`,`日志级别`,b,[{value:`DEBUG`,label:`DEBUG — 调试`},{value:`INFO`,label:`INFO — 信息（推荐）`},{value:`WARNING`,label:`WARNING — 警告`},{value:`ERROR`,label:`ERROR — 错误`}])}
      ${i(`ol-log-max-size`,`日志最大大小 (MB)`,n.log_max_size_mb||`2`,`2`,`number`)}
      ${i(`ol-log-backup-count`,`历史日志数量`,n.log_backup_count||`5`,`5`,`number`)}
      ${i(`ol-log-path`,`日志保存路径`,n.log_file||``,`./activity.log`)}
    </div>
  </div>`;let x=u.strmEngines.filter(e=>e.engine),C=[];x.forEach(e=>{let t=u.availableEngines.find(t=>t.mount_path===e.engine);t&&t.local_path&&!C.includes(t.local_path)&&C.push(t.local_path)}),p+=`<div class="config-section"><h3>${o(`folder`)} A 区文件夹 (只读)</h3>
    <p style="color:var(--text-muted);font-size:calc(var(--font-base) - 1px);margin:0 0 8px">从已配置的 STRM 引擎自动获取 (SaveStrmLocalPath)，不可手动修改</p>
    <div class="a-folders-display" id="a-folders-display">
      ${C.length?C.map(e=>`<span class="a-folder-chip">${g(e)}</span>`).join(``):`<span style="color:var(--text-muted)">暂无数据（请先配置 STRM 引擎）</span>`}
    </div>
  </div>`,p+=`</div>`,p+=`</div>`;let w=document.getElementById(`config-subpage`);if(w){let e=w.querySelector(`.config-back-btn`);e?e.insertAdjacentHTML(`afterend`,p):w.innerHTML=p;let t=w.querySelector(`.loading`);t&&t.remove()}E(t,n),T()}function w(){let e=new Set;u.strmEngines.forEach(t=>{if(!t||!t.engine||!t.monitored_paths)return;let n=t.engine;t.monitored_paths.forEach(t=>{let r=String(t).replace(/\/$/,``).split(`/`).pop();if(r){let t=`${n.replace(/\/$/,``)}/${r}`;e.add(t)}})});let t=window._openlistRefreshPaths||[],n=new Set(t);for(let r of e)n.has(r)||t.push(r);window._openlistRefreshPaths=t,T()}function T(){let e=document.getElementById(`refresh-paths-tags`);if(!e)return;let t=window._openlistRefreshPaths||[];if(!t.length){e.innerHTML=`<span class="refresh-paths-empty">暂无刷新路径，可手动添加</span>`;return}e.innerHTML=t.map((e,t)=>`<span class="tag">${g(e)}<button class="tag-remove" data-path-idx="${t}" title="删除">×</button></span>`).join(``),e._delegatedListener||(e._delegatedListener=t=>{let n=t.target.closest(`.tag-remove`);if(!n||!e.contains(n))return;let r=parseInt(n.dataset.pathIdx);(window._openlistRefreshPaths&&window._openlistRefreshPaths[r])!==void 0&&(window._openlistRefreshPaths.splice(r,1),T())},e.addEventListener(`click`,e._delegatedListener))}function E(e,t){document.querySelectorAll(`.openlist-form .floating-field input, .openlist-form .floating-field select`).forEach(e=>{let t=e.closest(`.floating-field`),n=t&&t.querySelector(`.floating-label`);if(!n)return;let r=()=>{let t=String(e.value||``).trim()!==``,r=document.activeElement===e,i=e.dataset.persistLabel===`1`;n.classList.toggle(`is-shown`,t||r||i),n.classList.toggle(`is-floating`,t||r||i),n.classList.toggle(`is-filled`,(t||i)&&!r),e.classList.toggle(`has-value`,t)};e.addEventListener(`focus`,r),e.addEventListener(`blur`,r),e.addEventListener(`input`,r),r()}),[`ol-webdav-password`,`ol-webdav-totp-secret`].forEach(e=>{let t=document.getElementById(e);t&&(t.addEventListener(`focus`,function(){this.type=`text`}),t.addEventListener(`blur`,function(){this.type=`password`}))});let n=e=>{let t=document.getElementById(e);if(!t)return;let n=t.querySelectorAll(`button`);n.forEach(e=>{e.addEventListener(`click`,()=>{n.forEach(t=>t.classList.toggle(`active`,t===e))})})};n(`ol-refresh-enabled`),n(`ol-sync-startup`),document.getElementById(`ol-test-connection`)?.addEventListener(`click`,async()=>{let e=document.getElementById(`ol-test-connection`);e.disabled=!0,e.innerHTML=`测试中...`;try{let e=await(await fetch(`/api/openlist/test-connection`,{method:`POST`,headers:{"Content-Type":`application/json`},body:JSON.stringify({host:document.getElementById(`ol-webdav-host`)?.value||``,user:document.getElementById(`ol-webdav-user`)?.value||``,password:document.getElementById(`ol-webdav-password`)?.value||``,totp_secret:document.getElementById(`ol-webdav-totp-secret`)?.value||``})})).json();if(e.success){v(`连接成功！`,`success`),u.apiStatus=`online`;try{let e=await c(`/api/openlist/strm-engines`);e.success&&(u.availableEngines=e.engines||[],O())}catch{}}else{u.apiStatus={wrong_password:`auth_failed_password`,wrong_2fa:`auth_failed_2fa`,account_not_found:`auth_failed`,network_error:`offline`,exception:`offline`}[e.error_type||`unknown`]||`auth_failed`,v(`连接失败: `+(e.error||`未知错误`),`error`),document.querySelectorAll(`.engine-select`).forEach(e=>e.disabled=!0);let t=document.getElementById(`add-engine-row`);t&&(t.disabled=!0)}}catch(e){v(`测试失败: `+e.message,`error`)}finally{e.disabled=!1,e.innerHTML=`测试连接`}}),document.getElementById(`ol-restart-webui`)?.addEventListener(`click`,async()=>{h(`重启 Bridge 主程序`,`确定要重启 Bridge 主程序吗？

重启将重新加载 STRM 存储映射，期间 WebUI 将短暂不可用（约 3-5 秒）。`,async()=>{let e=document.getElementById(`ol-restart-webui`);e.disabled=!0,e.innerHTML=`重启中...`,v(`正在重启主程序，请稍候...`,`info`);try{let t=await(await fetch(`/api/restart-webui`,{method:`POST`})).json();t.success?(v(`主程序正在重启，请稍候刷新页面...`,`success`),setTimeout(()=>{window.location.reload()},4e3)):(v(`重启失败: `+(t.message||`未知错误`),`error`),e.disabled=!1,e.innerHTML=`${o(`refresh`)} 重启Bridge主程序`)}catch(t){v(`重启请求失败: `+t.message,`error`),e.disabled=!1,e.innerHTML=`${o(`refresh`)} 重启Bridge主程序`}})});let r=document.getElementById(`strm-engine-table-wrap`);r&&!r._delegatedTagRemoveListener&&(r._delegatedTagRemoveListener=e=>{let t=e.target.closest(`.tag-remove`);if(!t||!r.contains(t))return;let n=parseInt(t.dataset.row),i=parseInt(t.dataset.pi);u.strmEngines[n]&&u.strmEngines[n].monitored_paths&&(u.strmEngines[n].monitored_paths.splice(i,1),D(n),w())},r.addEventListener(`click`,r._delegatedTagRemoveListener)),document.querySelectorAll(`[data-delete-row]`).forEach(e=>{e.addEventListener(`click`,()=>{let t=parseInt(e.dataset.deleteRow);u.strmEngines.length<1||(u.strmEngines.splice(t,1),O(),w())})}),document.getElementById(`add-engine-row`)?.addEventListener(`click`,()=>{u.strmEngines.push({engine:``,monitored_paths:[]}),O(),w()}),document.getElementById(`add-refresh-path-btn`)?.addEventListener(`click`,()=>{let e=document.getElementById(`refresh-path-input`);if(!e)return;let t=e.value.trim();if(t){if(window._openlistRefreshPaths||(window._openlistRefreshPaths=[]),window._openlistRefreshPaths.includes(t)){v(`该路径已存在`,`info`);return}window._openlistRefreshPaths.push(t),e.value=``,T()}}),document.getElementById(`refresh-path-input`)?.addEventListener(`keydown`,e=>{e.key===`Enter`&&document.getElementById(`add-refresh-path-btn`)?.click()}),document.getElementById(`ol-save`)?.addEventListener(`click`,async()=>{let e=document.getElementById(`ol-save`);e.disabled=!0,e.innerHTML=`保存中...`;try{let e={webdav_host:document.getElementById(`ol-webdav-host`)?.value||``,webdav_user:document.getElementById(`ol-webdav-user`)?.value||``,webdav_password:document.getElementById(`ol-webdav-password`)?.value||``,webdav_totp_secret:document.getElementById(`ol-webdav-totp-secret`)?.value||``,b_root:document.getElementById(`ol-b-root`)?.value||``,c_root:document.getElementById(`ol-c-root`)?.value||``,refresh_paths:JSON.stringify(window._openlistRefreshPaths||[]),strm_engines:JSON.stringify(u.strmEngines),refresh_enabled:document.querySelector(`#ol-refresh-enabled button[data-value="on"].active`)?`true`:`false`,refresh_interval_minutes:document.getElementById(`ol-refresh-interval`)?.value||`10`,refresh_depth:document.getElementById(`ol-refresh-depth`)?.value||`5`,behavior_action:document.getElementById(`ol-action`)?.value||`MOVE`,behavior_trash_dir_name:document.getElementById(`ol-trash-dir`)?.value||`trash`,behavior_ghost_protect_seconds:document.getElementById(`ol-ghost-protect`)?.value||`300`,behavior_a_to_b_restore_delay_seconds:document.getElementById(`ol-restore-delay`)?.value||`30`,behavior_sync_on_startup:document.querySelector(`#ol-sync-startup button[data-value="on"].active`)?`true`:`false`,behavior_sync_on_startup_wait:document.getElementById(`ol-startup-wait`)?.value||`0`,log_level:document.getElementById(`ol-log-level`)?.value||`INFO`,log_max_size_mb:document.getElementById(`ol-log-max-size`)?.value||`2`,log_backup_count:document.getElementById(`ol-log-backup-count`)?.value||`5`,log_file:document.getElementById(`ol-log-path`)?.value||``},t=await(await fetch(`/api/webui/config/openlist`,{method:`POST`,headers:{"Content-Type":`application/json`},body:JSON.stringify(e)})).json();t.success?(v(`OpenList 配置已保存并热更新`,`success`),u.configured=!!(document.getElementById(`ol-webdav-host`)?.value||``).trim(),M(),await k()):v(`保存失败: `+(t.error||`未知错误`),`error`)}catch(e){v(`保存失败: `+e.message,`error`)}finally{e.disabled=!1,e.innerHTML=`保存`}})}function D(e){let t=document.getElementById(`tag-container-${e}`);if(!t)return;let n=u.strmEngines[e];if(!n||!n.monitored_paths||!n.monitored_paths.length){t.innerHTML=`<span style="color:var(--text-muted);font-size:calc(var(--font-base) - 1px)">选择引擎后自动填充</span>`;return}t.innerHTML=n.monitored_paths.map((t,n)=>`<span class="tag">${g(t)}<button class="tag-remove" data-row="${e}" data-pi="${n}" title="删除">×</button></span>`).join(``)}function O(){let e=document.getElementById(`strm-engine-table-wrap`);if(!e)return;let t=`<table class="strm-engine-table"><thead><tr><th style="width:35%">STRM 引擎入口</th><th>监控目录</th><th style="width:60px">操作</th></tr></thead><tbody>`;u.strmEngines.forEach((e,n)=>{let r=`<option value="">选择引擎...</option>`,i=new Set(u.strmEngines.map((e,t)=>t===n?``:e.engine).filter(Boolean));u.availableEngines.forEach(t=>{let n=e.engine===t.mount_path?` selected`:``,a=!n&&i.has(t.mount_path)?` disabled`:``;r+=`<option value="${g(t.mount_path)}"${n}${a}>${g(t.mount_path)}</option>`});let a=u.configured?``:` disabled`,o=``;e.monitored_paths&&e.monitored_paths.length&&e.monitored_paths.forEach((e,t)=>{o+=`<span class="tag">${g(e)}<button class="tag-remove" data-row="${n}" data-pi="${t}" title="删除">×</button></span>`}),t+=`<tr data-row-idx="${n}">
      <td><select class="engine-select" data-row="${n}"${a}>${r}</select></td>
      <td><div class="tag-container" data-row="${n}" id="tag-container-${n}">${o||`<span style="color:var(--text-muted);font-size:calc(var(--font-base) - 1px)">选择引擎后自动填充</span>`}</div></td>
      <td style="text-align:center"><button class="table-btn danger" data-delete-row="${n}" title="删除此行">删除</button></td>
    </tr>`}),t+=`</tbody></table>`;let n=u.configured?``:` disabled`;t+=`<div class="table-actions"><button class="table-btn primary" id="add-engine-row"${n}>+ 添加行</button></div>`,e.innerHTML=`<div class="strm-engine-wrap"><span class="monitored-paths-help">`+S(`monitored_paths`)+`</span>`+t+`</div>`,e.querySelectorAll(`.engine-select`).forEach(e=>{e.addEventListener(`change`,async()=>{let t=parseInt(e.dataset.row),n=e.value;if(!n)return;let r=u.availableEngines.find(e=>e.mount_path===n);if(r)u.strmEngines[t]={engine:n,monitored_paths:r.paths||[]},D(t),w();else try{let e=await c(`/api/openlist/monitored-paths?engine=${encodeURIComponent(n)}`);e.success&&(u.strmEngines[t]={engine:n,monitored_paths:e.paths||[]},D(t),w())}catch{}})}),e._delegatedTagRemoveListener||(e._delegatedTagRemoveListener=t=>{let n=t.target.closest(`.tag-remove`);if(!n||!e.contains(n))return;let r=parseInt(n.dataset.row),i=parseInt(n.dataset.pi);u.strmEngines[r]&&u.strmEngines[r].monitored_paths&&(u.strmEngines[r].monitored_paths.splice(i,1),D(r),w())},e.addEventListener(`click`,e._delegatedTagRemoveListener)),e.querySelectorAll(`[data-delete-row]`).forEach(e=>{e.addEventListener(`click`,()=>{let t=parseInt(e.dataset.deleteRow);u.strmEngines.length<1||(u.strmEngines.splice(t,1),O(),w())})}),document.getElementById(`add-engine-row`)?.addEventListener(`click`,()=>{u.strmEngines.push({engine:``,monitored_paths:[]}),O(),w()})}async function k(){let e=document.getElementById(`api-status-dot`),t=document.getElementById(`api-status-text`);if(!(!e||!t)){e.className=`api-status-dot checking`,t.textContent=`OpenList 检查中`;try{u.configured=(await c(`/api/openlist/status`)).status===`configured`}catch{}M();try{let e=await c(`/api/openlist/ping`);u.apiStatus=e.status||`offline`,e.status===`unconfigured`&&(u.apiStatus=`offline`,u.configured=!1),M()}catch{u.apiStatus=`offline`,M()}}}function A(e,t){let n=!!e;return t===`online`?n?`OpenList 已连接`:`OpenList 已连接（未保存配置）`:t===`offline`?n?`OpenList 已配置（离线）`:`OpenList 未配置`:t===`auth_failed_password`?n?`OpenList 密码错误`:`OpenList 密码错误（未保存）`:t===`auth_failed_2fa`?n?`OpenList 2FA 错误`:`OpenList 2FA 错误（未保存）`:t===`auth_failed`?n?`OpenList 认证失败`:`OpenList 认证失败（未保存）`:n?`OpenList 已配置`:`OpenList 未配置`}function j(e,t){return e?`api-status-dot ${t}`:`api-status-dot unconfigured`}function M(){let e=u.configured,t=u.apiStatus,n=document.getElementById(`ol-status-dot`),r=document.getElementById(`ol-status-text`);n&&(n.className=j(e,t===`checking`?`offline`:t)),r&&(r.textContent=A(e,t===`checking`?`offline`:t));let i=document.getElementById(`api-status-dot`),a=document.getElementById(`api-status-text`);i&&(i.className=j(e,t===`checking`?`offline`:t)),a&&(a.textContent=A(e,t===`checking`?`offline`:t))}n(l),window.addEventListener(`hashchange`,l),document.addEventListener(`DOMContentLoaded`,()=>{let n=document.documentElement,o=localStorage.getItem(`webui_theme_system`),c=localStorage.getItem(`webui_theme_color`),u=localStorage.getItem(`webui_theme_fontsize`);o&&(n.dataset.system=o),c&&(n.dataset.color=c),u&&[`lg`,`sm`,`xs`].includes(u)&&(n.dataset.font=u),r(),t();let d=document.getElementById(`gear-quick-btn`);d&&d.addEventListener(`click`,()=>{y(`#config?sub=openlist`)}),b(),fetch(`/api/config`).then(e=>e.json()).then(t=>{e(t.tmdb_host&&!t.tmdb_host.startsWith(`https://api.themoviedb.org`)?t.tmdb_host:`https://www.themoviedb.org`)}).catch(()=>{}),document.addEventListener(`visibilitychange`,()=>{document.hidden?(s&&(clearInterval(s),a(null)),i()):document.getElementById(`uptime-val`)&&(s||_(()=>import(`./dashboard-DngkCqGB.js`).then(e=>{a(setInterval(e.updateMainStatus,m.MAIN_STATUS_POLL_INTERVAL))}),__vite__mapDeps([0,1]),import.meta.url),f())}),p(),setTimeout(()=>{k()},0),l()});export{C as t};