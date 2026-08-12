import{D as e,R as t,a as n,o as r}from"./core-RM6lzTyI.js";async function i(i){let a=n(),o=!1,s=!1;try{let e=new AbortController,t=setTimeout(()=>e.abort(),1e4),n;try{n=await fetch(`/api/admin/status`,{signal:e.signal})}finally{clearTimeout(t)}n.ok?(o=!!(await n.json()).has_password,s=!0):s=!1}catch{}let c=localStorage.getItem(`session_token`);if(c&&o&&s){r(`#dashboard`);return}if(c&&!o&&s&&localStorage.removeItem(`session_token`),!s){if(a())return;let e=localStorage.getItem(`session_token_expired`)===`1`;localStorage.removeItem(`session_token_expired`),i.innerHTML=`
      <div style="display:flex;align-items:center;justify-content:center;min-height:60vh">
        <div class="page-card" style="max-width:420px;width:100%;text-align:center;padding:40px 32px">
          <div style="font-size:48px;margin-bottom:16px;color:var(--text-error)">${t(`warn`)}</div>
          <h2 style="margin:0 0 12px;font-size:20px;color:var(--text-main)">${e?`登录已过期`:`无法连接服务器`}</h2>
          <p style="color:var(--text-muted);font-size:var(--font-base);line-height:1.6">
            ${e?`你的登录会话已过期，请重新连接服务器并登录。`:`无法连接到 STRM Bridge 后端服务，请检查服务是否已启动。`}<br>
            默认端口为 <code style="background:var(--bg-control);padding:2px 6px;border-radius:4px">8579</code>。
          </p>
          <button class="toolbar-btn primary" style="margin-top:12px" id="login-retry-btn">
            ${t(`refresh`)} ${e?`重新连接`:`重试连接`}
          </button>
        </div>
      </div>`,document.getElementById(`login-retry-btn`)?.addEventListener(`click`,()=>{let e=window.location.hash;window.location.hash=`#login`,e===`#login`?window.dispatchEvent(new HashChangeEvent(`hashchange`)):window.location.hash=e});return}if(!o){if(a())return;i.innerHTML=`
      <div style="display:flex;align-items:center;justify-content:center;min-height:60vh">
        <div class="page-card" style="max-width:420px;width:100%;text-align:center;padding:40px 32px">
          <div style="font-size:48px;margin-bottom:16px">${t(`lock`)}</div>
          <h2 style="margin:0 0 12px;font-size:20px;color:var(--text-main)">未设置管理员密码</h2>
          <p style="color:var(--text-muted);font-size:var(--font-base);line-height:1.6">
            WebUI 当前使用 IP 白名单保护，未设置密码。<br>
            首次启动时密码已打印到控制台（仅显示一次，不写入日志），<br>
            或运行 <code style="background:var(--bg-control);padding:2px 6px;border-radius:4px">python reset_admin.py</code> 生成一个新密码。
          </p>
          <button class="toolbar-btn primary" style="margin-top:12px" id="login-go-dashboard-btn">
            ${t(`arrow_back`)} 进入管理面板
          </button>
        </div>
      </div>`,document.getElementById(`login-go-dashboard-btn`)?.addEventListener(`click`,()=>r(`#dashboard`));return}if(a())return;i.innerHTML=`
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
            ${t(`login`)} 登录
          </button>
        </div>
        <div style="margin-top:20px;text-align:center;font-size:12px;color:var(--text-muted);line-height:1.6">
          管理密码仅在首次启动时打印到控制台（仅显示一次，不写入日志）<br>
          忘记密码可运行 <code style="background:var(--bg-control);padding:1px 4px;border-radius:3px">python reset_admin.py</code> 重置
        </div>
      </div>
    </div>`;let l=document.getElementById(`login-password-input`),u=document.getElementById(`login-btn`),d=document.getElementById(`login-error`);function f(e){d.textContent=e,d.style.display=`block`}async function p(){let n=l.value;if(!n||!n.trim()){f(`请输入管理员密码`);return}u.disabled=!0,u.textContent=`登录中...`,d.style.display=`none`;try{let i=new AbortController,a=setTimeout(()=>i.abort(),1e4),o;try{o=await fetch(`/api/login`,{method:`POST`,headers:{"Content-Type":`application/json`},body:JSON.stringify({password:n}),signal:i.signal})}finally{clearTimeout(a)}let s=await o.json();o.ok&&s.token?(e(s.token),r(`#dashboard`)):(f(s.error||`密码错误`),u.disabled=!1,u.innerHTML=`${t(`login`)} 登录`)}catch{f(`网络错误，请检查服务器是否运行`),u.disabled=!1,u.innerHTML=`${t(`login`)} 登录`}}u.addEventListener(`click`,p),l.addEventListener(`keydown`,e=>{e.key===`Enter`&&p()}),setTimeout(()=>l.focus(),100)}export{i as renderLogin};