import{L as e,c as t,o as n}from"./core-CWeA9Nn1.js";async function r(r){let i=!1,a=!1;try{i=(await t(`/api/admin/status`)).has_password,a=!0}catch{}let o=localStorage.getItem(`session_token`);if(o&&i){n(`#dashboard`);return}if(o&&!i&&a&&localStorage.removeItem(`session_token`),!i){r.innerHTML=`
      <div style="display:flex;align-items:center;justify-content:center;min-height:60vh">
        <div class="page-card" style="max-width:420px;width:100%;text-align:center;padding:40px 32px">
          <div style="font-size:48px;margin-bottom:16px">${e(`lock`)}</div>
          <h2 style="margin:0 0 12px;font-size:20px;color:var(--text-main)">未设置管理员密码</h2>
          <p style="color:var(--text-muted);font-size:var(--font-base);line-height:1.6">
            WebUI 当前使用 IP 白名单保护，未设置密码。<br>
            首次启动时密码已打印到控制台（仅显示一次，不写入日志），<br>
            或运行 <code style="background:var(--bg-control);padding:2px 6px;border-radius:4px">python reset_admin.py</code> 生成一个新密码。
          </p>
          <button class="toolbar-btn primary" style="margin-top:12px" id="login-go-dashboard-btn">
            ${e(`arrow_back`)} 进入管理面板
          </button>
        </div>
      </div>`,document.getElementById(`login-go-dashboard-btn`)?.addEventListener(`click`,()=>n(`#dashboard`));return}r.innerHTML=`
    <div style="display:flex;align-items:center;justify-content:center;min-height:70vh">
      <div class="page-card" id="login-card" style="max-width:400px;width:100%;padding:36px 28px 28px">
        <div style="text-align:center;margin-bottom:24px">
          <img src="/logo.png" alt="STRM Bridge" style="width:64px;height:64px;border-radius:12px;margin-bottom:8px;object-fit:cover">
          <h2 style="margin:0;font-size:20px;color:var(--text-main)">STRM Bridge</h2>
          <p style="margin:6px 0 0;font-size:var(--font-base);color:var(--text-muted)">管理面板</p>
        </div>
        <div id="login-error" style="display:none;background:color-mix(in srgb,#d93025 10%,var(--bg-card));border:1px solid color-mix(in srgb,#d93025 24%,transparent);border-radius:10px;padding:12px 16px;color:#d93025;font-size:var(--font-base);margin-bottom:16px;text-align:center"></div>
        <div class="floating-field" data-field="login-password">
          <div class="field-control">
            <label class="floating-label is-shown is-floating is-filled" data-role="label" for="login-password-input">管理员密码</label>
            <input type="password" id="login-password-input" class="has-value" placeholder="输入管理员密码" autocomplete="current-password" autofocus>
          </div>
        </div>
        <div style="margin-top:24px;display:flex;flex-direction:column;gap:8px">
          <button class="toolbar-btn primary" id="login-btn" style="width:100%;justify-content:center;padding:12px 20px;font-size:15px">
            ${e(`login`)} 登录
          </button>
        </div>
        <div style="margin-top:20px;text-align:center;font-size:12px;color:var(--text-muted);line-height:1.6">
          管理密码仅在首次启动时打印到控制台（仅显示一次，不写入日志）<br>
          忘记密码可运行 <code style="background:var(--bg-control);padding:1px 4px;border-radius:3px">python reset_admin.py</code> 重置
        </div>
      </div>
    </div>`;let s=document.getElementById(`login-password-input`),c=document.getElementById(`login-btn`),l=document.getElementById(`login-error`);function u(e){l.textContent=e,l.style.display=`block`}async function d(){let t=s.value;if(!t||!t.trim()){u(`请输入管理员密码`);return}c.disabled=!0,c.textContent=`登录中...`,l.style.display=`none`;try{let r=await fetch(`/api/login`,{method:`POST`,headers:{"Content-Type":`application/json`},body:JSON.stringify({password:t})}),i=await r.json();r.ok&&i.token?(localStorage.setItem(`session_token`,i.token),n(`#dashboard`)):(u(i.error||`密码错误`),c.disabled=!1,c.innerHTML=`${e(`login`)} 登录`)}catch{u(`网络错误，请检查服务器是否运行`),c.disabled=!1,c.innerHTML=`${e(`login`)} 登录`}}c.addEventListener(`click`,d),s.addEventListener(`keydown`,e=>{e.key===`Enter`&&d()}),setTimeout(()=>s.focus(),100)}export{r as renderLogin};