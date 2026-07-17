// ============================================================
// Login Page — WebUI 管理员登录
// 严格遵循 MD3 / Fluent 2 双主题设计系统
// ============================================================

import { api } from '../core/api.js';
import { icon } from '../core/icons.js';
import { esc } from '../core/utils.js';
import { navigate } from '../core/router.js';

export async function renderLogin(el) {
  // 始终从服务器获取密码状态，避免与 main.js 的异步初始化产生时序竞争
  let hasPassword = false;
  let fetchSucceeded = false;
  try {
    const status = await api('/api/admin/status');
    hasPassword = status.has_password;
    fetchSucceeded = true;
  } catch (e) {
    // 服务器不可达，显示连接错误
  }

  // 如果已登录，直接跳转
  const token = localStorage.getItem('session_token');
  if (token && hasPassword) {
    navigate('#dashboard');
    return;
  }
  // 仅当明确获知无密码时才删除 token（网络错误时不删除 P3-7）
  if (token && !hasPassword && fetchSucceeded) {
    localStorage.removeItem('session_token');
  }

  // 检查是否已配置密码
  if (!hasPassword) {
    el.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:center;min-height:60vh">
        <div class="page-card" style="max-width:420px;width:100%;text-align:center;padding:40px 32px">
          <div style="font-size:48px;margin-bottom:16px">${icon('lock')}</div>
          <h2 style="margin:0 0 12px;font-size:20px;color:var(--text-main)">未设置管理员密码</h2>
          <p style="color:var(--text-muted);font-size:var(--font-base);line-height:1.6">
            WebUI 当前使用 IP 白名单保护，未设置密码。<br>
            首次启动时密码已打印到控制台（仅显示一次，不写入日志），<br>
            或运行 <code style="background:var(--bg-control);padding:2px 6px;border-radius:4px">python reset_admin.py</code> 生成一个新密码。
          </p>
          <button class="toolbar-btn primary" style="margin-top:12px" id="login-go-dashboard-btn">
            ${icon('arrow_back')} 进入管理面板
          </button>
        </div>
      </div>`;
    document.getElementById('login-go-dashboard-btn')?.addEventListener('click', () => navigate('#dashboard'));
    return;
  }

  el.innerHTML = `
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
            ${icon('login')} 登录
          </button>
        </div>
        <div style="margin-top:20px;text-align:center;font-size:12px;color:var(--text-muted);line-height:1.6">
          管理密码仅在首次启动时打印到控制台（仅显示一次，不写入日志）<br>
          忘记密码可运行 <code style="background:var(--bg-control);padding:1px 4px;border-radius:3px">python reset_admin.py</code> 重置
        </div>
      </div>
    </div>`;

  // 绑定事件
  const input = document.getElementById('login-password-input');
  const btn = document.getElementById('login-btn');
  const errorEl = document.getElementById('login-error');

  function showError(msg) {
    errorEl.textContent = msg;
    errorEl.style.display = 'block';
  }

  async function doLogin() {
    const password = input.value.trim();
    if (!password) {
      showError('请输入管理员密码');
      return;
    }
    btn.disabled = true;
    btn.textContent = '登录中...';
    errorEl.style.display = 'none';

    try {
      const resp = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password })
      });
      const data = await resp.json();
      if (resp.ok && data.token) {
        localStorage.setItem('session_token', data.token);
        navigate('#dashboard');
      } else {
        showError(data.error || '密码错误');
        btn.disabled = false;
        btn.innerHTML = `${icon('login')} 登录`;
      }
    } catch (e) {
      showError('网络错误，请检查服务器是否运行');
      btn.disabled = false;
      btn.innerHTML = `${icon('login')} 登录`;
    }
  }

  btn.addEventListener('click', doLogin);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') doLogin();
  });

  // 聚焦输入框
  setTimeout(() => input.focus(), 100);
}