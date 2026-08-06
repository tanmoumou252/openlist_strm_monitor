import { api } from '../core/api.js';
import { icon } from '../core/icons.js';
import { esc, createField } from '../core/utils.js';
import { showToast } from '../components/toast.js';
import { showConfirmDialog } from '../components/dialog.js';
import { navigate, isRenderStale } from '../core/router.js';  // L6: 导入 isRenderStale 用于轮询竞态防护
import { _tmdbCache, _getUiConfig, _setUiConfig } from '../core/state.js';
import { _renderOpenListConfig } from './openlist.js';

export async function renderConfig(el, params) {
  const cfg = await api('/api/config');
  // L6: await 期间若发生新导航，放弃渲染，避免旧页覆盖
  if (isRenderStale()) return;
  // /api/config 是白名单端点，代理 URL 已脱敏为 tmdb_proxy_configured（布尔值）。
  // 配置页需要回显代理 URL，从已认证的 /api/webui/config/tmdb 获取。
  if (cfg.tmdb_proxy_configured) {
    try {
      const tmdbCfg = await api('/api/webui/config/tmdb');
      if (tmdbCfg && tmdbCfg.config && typeof tmdbCfg.config.proxy_http === 'string') {
        cfg.tmdb_proxy_http = tmdbCfg.config.proxy_http;
      } else {
        cfg.tmdb_proxy_http = '';
      }
    } catch {
      cfg.tmdb_proxy_http = '';
    }
  } else {
    cfg.tmdb_proxy_http = '';
  }
  const sub = params.sub || '';
  const isSubpage = ['config', 'openlist', 'about', 'help'].includes(sub);
  const sections = {
    config: _renderConfigContent(cfg),
    openlist: '<div class="loading"><div class="spinner"></div> 加载中...</div>',
    about: _renderConfigAbout(),
    help: _renderConfigHelp(),
  };
  const buttons = [
    { id: 'config', label: 'WebUI/TMDB' },
    { id: 'openlist', label: 'OpenList' },
    { id: 'about', label: '关于' },
    { id: 'help', label: '帮助' },
  ];
  const arrow = '<svg class="upload-arrow" viewBox="0 -960 960 960" width="28" height="28" fill="currentColor"><path d="M440-320v-326L336-542l-56-58 200-200 200 200-56 58-104-104v326h-80ZM240-160q-33 0-56.5-23.5T160-240v-120h80v120h480v-120h80v120q0 33-23.5 56.5T720-160H240Z"/></svg>';
  let html = '';
  if (!isSubpage) {
    html += `<div class="config-entrance" id="config-entrance">`;
    buttons.forEach(btn => {
      html += `<button class="config-entrance-btn" data-sub="${btn.id}">${esc(btn.label)}${arrow}</button>`;
    });
    html += `</div>`;
  }
  html += `<div class="config-subpage${isSubpage ? ' active' : ''}" id="config-subpage">`;
  if (isSubpage) {
    html += `<button class="config-back-btn" id="config-back-btn">${icon('back')} 返回</button>`;
    html += sections[sub];
  }
  html += `</div>`;
  el.innerHTML = html;
  const entrance = document.getElementById('config-entrance');
  if (entrance && sessionStorage.getItem('_config_back')) {
    entrance.classList.add('fade-in');
    sessionStorage.removeItem('_config_back');
  }
  document.querySelectorAll('.config-entrance-btn').forEach(btn => btn.addEventListener('click', () => {
    if (!entrance) return navigate(`#config?sub=${btn.dataset.sub}`);
    entrance.classList.add('fade-out');
    let navigated = false;
    const go = () => { if (navigated) return; navigated = true; navigate(`#config?sub=${btn.dataset.sub}`); };
    entrance.addEventListener('animationend', go, { once: true });
    setTimeout(go, 300);
  }));
  const backBtn = document.getElementById('config-back-btn');
  if (backBtn) backBtn.addEventListener('click', () => {
    const subpage = document.getElementById('config-subpage');
    if (!subpage) return navigate('#config');
    subpage.classList.add('exiting');
    let navigated = false;
    const go = () => { if (navigated) return; navigated = true; sessionStorage.setItem('_config_back', '1'); navigate('#config'); };
    subpage.addEventListener('animationend', go, { once: true });
    setTimeout(go, 350);
  });
  if (sub === 'config') _bindConfigFormEvents(cfg);
  if (sub === 'openlist') _renderOpenListConfig(cfg);
}

function _renderConfigContent(cfg) {
  function section(titleIcon, title, rowsHtml) {
    return `<div class="config-section"><h3>${icon(titleIcon)} ${esc(title)}</h3>${rowsHtml}</div>`;
  }
  function field(id, label, value, placeholder, type = 'text', persistLabel = false, readOnly = false, htmlLabel = '', helperText = '') {
    return createField(id, label, value, { placeholder, type, persistLabel, readOnly, htmlLabel, helperText });
  }

  const presetLangs = ['zh-CN', 'en-US', 'ja-JP'];
  const savedLang = String(cfg.tmdb_language || 'zh-CN');
  const isCustomLang = !presetLangs.includes(savedLang);
  const langSelectValue = isCustomLang ? 'custom' : savedLang;
  const languageOptions = [
    ['zh-CN', '中文'], ['en-US', '英语'], ['ja-JP', '日语'], ['custom', '自定义'],
  ].map(([val, text]) => `<option value="${val}" ${langSelectValue === val ? 'selected' : ''}>${esc(text)}</option>`).join('');

  let html = `<div class="config-blocks-wrapper">`;
  html += `<div class="config-blocks-row">`;
  html += section('database', 'Bridge数据库', `
    <div class="config-row"><span class="key">路径</span><span class="val">${esc(cfg.db_file.split(/[\\/]/).pop())}</span></div>
    <div class="config-row"><span class="key">存在</span><span class="val">${cfg.db_exists ? icon('check') : icon('error')}</span></div>
  `);
  const webuiRows = [];
  webuiRows.push(`<div class="config-row"><span class="key">端口</span><span class="val">${esc(cfg.webui_port)}</span></div>`);
  webuiRows.push(`<div class="config-row"><span class="key">绑定</span><span class="val">${esc(cfg.webui_bind)}</span></div>`);
  html += section('globe', 'WebUI', webuiRows.join(''));
  html += `</div>`;

  const tokenIsConfigured = cfg.tmdb_token_configured === true;
const tokenValue = '';  // 不预填截断预览，避免误保存覆盖真实 token
  const apiKeyValue = '';  // 不预填明文 API key（B-3：后端仅返回 bool），避免误保存覆盖
  const hostValue = cfg.tmdb_host || '';
  const proxyValue = cfg.tmdb_proxy_http || '';

  const langLabelCls = 'floating-label is-shown is-floating is-filled';
  const langCustomValue = isCustomLang ? savedLang : '';
  const langField = `
    <div class="floating-field" data-field="cfg-tmdb-lang">
      <div class="field-control">
        <label class="${langLabelCls}" data-role="label" for="cfg-tmdb-lang-select">语言</label>
        <div class="field-select-wrap${isCustomLang ? ' has-custom' : ''}">
          <select id="cfg-tmdb-lang-select" class="has-value">
            ${languageOptions}
          </select>
          <input type="text" id="cfg-tmdb-lang" placeholder="输入语言代码" value="${esc(langCustomValue)}" style="${isCustomLang ? '' : 'display:none'}">
        </div>
      </div>
    </div>`;

  const tmdbWatchlistEnabled = cfg.tmdb_watchlist_enabled !== false && cfg.tmdb_watchlist_enabled !== 'false';
  html += `<div class="config-section tmdb-section"><h3>${icon('tmdb')} TMDB <span style="font-size:calc(var(--font-base) - 1px);color:var(--text-muted);font-weight:400">(保存后即时生效，不需重启)</span></h3>`;
  html += `<div class="field-grid">`;
  html += `<div class="floating-field" data-field="cfg-tmdb-status">
    <div class="field-control">
      <label class="floating-label is-shown is-floating" data-role="label">状态</label>
      <div class="toggle-row" style="padding:4px 0">
        <div class="segmented-switch" id="cfg-tmdb-watchlist-enabled-switch" data-key="watchlist_enabled">
          <button type="button" data-value="on"${tmdbWatchlistEnabled ? ' class="active"' : ''}>启用</button>
          <button type="button" data-value="off"${!tmdbWatchlistEnabled ? ' class="active"' : ''}>禁用</button>
        </div>
      </div>
    </div>
  </div>`;
  html += field('cfg-tmdb-account', 'account_id', cfg.tmdb_account_id || '未获取', '', 'text', true, true);
  const tokenBadge = tokenIsConfigured ? '<span class="badge configured-badge" style="color:var(--success);margin-left:6px;font-size:calc(var(--font-base) - 1px)">✓ 已配置</span>' : '';
html += field('cfg-tmdb-token', 'Access Token', tokenValue, '输入 TMDB Access Token', 'password', false, false, tokenBadge);
  const apiKeyIsConfigured = cfg.tmdb_api_key_configured === true;
  const apiKeyBadge = apiKeyIsConfigured ? '<span class="badge configured-badge" style="color:var(--success);margin-left:6px;font-size:calc(var(--font-base) - 1px)">✓ 已配置</span>' : '';
  html += field('cfg-tmdb-apikey', 'API Key', apiKeyValue, '输入 TMDB API Key', 'password', false, false, apiKeyBadge);
  html += langField;
  html += field('cfg-tmdb-host', '反代 Host', hostValue, '留空则使用官方 API');
  html += field('cfg-tmdb-proxy', 'HTTP 代理', proxyValue, '例: http://127.0.0.1:7890', 'text', true);
  html += field('cfg-tmdb-fuzzy', '模糊匹配阈值', cfg.tmdb_fuzzy_threshold || '0.60', '0.0~1.0', 'text', false, false, '', '模糊匹配相似度阈值（0.0~1.0）。越高要求越严格，建议 0.60。');
    html += field('cfg-tmdb-ep-ratio', '番剧最少集数比例', cfg.tmdb_anime_min_ep_ratio || '0.30', '0.0~1.0', 'text', false, false, '', '番剧最少集数占总集数比例（0.0~1.0）。超过此比例才视为已收录，建议 0.30。');
    html += field('cfg-tmdb-season-diff', '番剧最大季数差', cfg.tmdb_anime_max_season_diff || '1', '', 'text', false, false, '', '番剧最大允许季数差。超过此差值视为未收录，建议 1。');
    html += field('cfg-tmdb-min-season-ratio', '番剧最少季数比例', cfg.tmdb_anime_min_season_ratio || '0.30', '0.0~1.0', 'text', false, false, '', '番剧最少季数占总季数比例（0.0~1.0）。超过此比例才视为已收录，建议 0.30。');
    html += field('cfg-tmdb-cache-ttl', '缓存 TTL（秒）', cfg.tmdb_cache_ttl || '604800', '', 'text', false, false, '', 'TMDB 缓存有效期（秒）。过期后重新从 TMDB 拉取数据，建议 604800（7 天）。');
    html += `</div>`;
  html += `<div class="config-form-actions"><button class="toolbar-btn primary" id="cfg-tmdb-save">${icon('save')} 保存 TMDB 配置</button><button class="toolbar-btn" id="cfg-tmdb-refresh"> 刷新待看列表</button><button class="toolbar-btn" id="cfg-tmdb-match-refresh" style="background:color-mix(in srgb,var(--primary) 15%,var(--bg-card));border-color:color-mix(in srgb,var(--primary) 30%,var(--border-color))"> 刷新收录状态</button><button class="toolbar-btn secondary" id="cfg-tmdb-restart" style="color:#e37400;border-color:#e37400">${icon('refresh')} 重启 WebUI</button></div>`;
  html += `<div class="toggle-row"><span>关闭全屏 TMDB 缓存过期提醒</span><div class="segmented-switch" data-key="tmdb_cache_never_remind"><button type="button" data-value="off" class="active">否</button><button type="button" data-value="on">是</button></div></div>`;
  html += `<div class="toggle-row"><span>关闭右上角"建议刷新列表"提醒</span><div class="segmented-switch" data-key="tmdb_match_toast_disabled"><button type="button" data-value="off" class="active">否</button><button type="button" data-value="on">是</button></div></div>`;
  html += '</div>';
  html += '</div>';
  return html;
}

function _renderConfigAbout() {
  const appName = 'STRM Bridge';
  const creditPill = (text, title = '') => `<button disabled class="credit-pill"${title ? ` title="${esc(title)}"` : ''}>${esc(text)}</button>`;
  const creditPillLink = (text, url) => `<a class="credit-pill" href="${esc(url)}" target="_blank" rel="noopener">${esc(text)}</a>`;
  return `
<div class="config-section"><h3>${icon('info')} 关于 ${esc(appName)}</h3>
<div style="line-height:1.8;font-size:var(--font-base);color:var(--text-muted);padding:8px 0">
  <p><strong>${esc(appName)}</strong> — 云盘 STRM 文件管理与 TMDB 待看列表同步工具。</p>
  <p style="margin:12px 0">感谢以下所有技术、工具和社区资源，使本项目得以实现。没有这些开源力量，就不会有这个 WebUI。</p>
  <p>开源许可证：MIT License</p>
  <p>GitHub 仓库：${creditPillLink('openlist_strm_monitor', 'https://github.com/tanmoumou252/openlist_strm_monitor')}</p>
</div>
<div class="credit-group-title">渲染与设计</div>
<p class="credit-group-desc">整个 WebUI 采用纯 HTML/CSS/JS ES Module 模块化架构（Vite 构建），两套主题分别致敬 Microsoft Fluent 2 和 Google Material Design 3 设计体系，图标来自 Google Material Symbols Outlined，字体使用思源黑体（Source Han Sans / Noto Sans SC）。</p>
<div class="credit-group">
  ${creditPill('Python')}${creditPill('SQLite')}${creditPill('纯 HTML/CSS/JS')}${creditPillLink('Material Design 3', 'https://m3.material.io/')}${creditPillLink('Fluent 2 Design', 'https://fluent2.microsoft.design/')}${creditPillLink('Material Symbols', 'https://fonts.google.com/icons')}${creditPillLink('思源黑体', 'https://github.com/adobe-fonts/source-han-sans')}
</div>
<div class="credit-group-title">API 与服务</div>
<p class="credit-group-desc">数据源和加速服务，支撑元数据获取与内容分发。</p>
<div class="credit-group">
  ${creditPillLink('TMDB API', 'https://www.themoviedb.org')}${creditPillLink('OpenList API', 'https://github.com/OpenListTeam/OpenList')}${creditPillLink('EdgeOne CDN', 'https://edgeone.ai/zh/login?s_url=https://console.tencentcloud.com/edgeone')}
</div>
<div class="credit-group-title">使用工具</div>
<p class="credit-group-desc">编写和调试本项目的编辑器与编程助手。</p>
<div class="credit-group">
  ${creditPillLink('VS Code', 'https://code.visualstudio.com')}${creditPillLink('Kilo Code', 'https://app.kilo.ai')}${creditPillLink('Aider', 'https://aider.chat')}${creditPillLink('Cline', 'https://cline.bot')}${creditPillLink('ZCode', 'https://zcode.z.ai')}${creditPillLink('AxonHub 开源 AI 网关', 'https://github.com/looplj/axonhub')}${creditPillLink('CapsWriter-Offline stt工具', 'https://github.com/HauyuChen/CapsWriter-Offline')}
</div>
<div class="credit-group-title">AI 模型</div>
<p class="credit-group-desc">参与代码生成和方案设计的大语言模型。</p>
<div class="credit-group">
  ${creditPill('Claude Opus 4.6')}${creditPill('Qwen 3.7 Plus')}${creditPill('DeepSeek-V4-Flash')}${creditPill('Glm-5.2')}${creditPill('Minimax-M3')}${creditPill('Gpt-5.4-Mini')}${creditPill('MiMo-V2.5 Pro')}${creditPill('Gpt-5.5 Via 365 Copilot')}${creditPill('Qwen3-Asr-1.7B')}
</div>
<div class="credit-group-title">中转站提供商</div>
<p class="credit-group-desc">免费或低成本的 API 代理中转站，降低开发期间的 API 调用门槛。</p>
<div class="credit-group">
  ${creditPillLink('agentrouter', 'https://agentrouter.org/login')}${creditPillLink('vsllm', 'https://vsllm.com')}${creditPillLink('商汤', 'https://www.sensenova.cn/')}${creditPillLink('freetheai', 'https://freetheai.xyz/models/')}${creditPillLink('openmodel', 'https://www.openmodel.ai/')}${creditPillLink('nvidia nim', 'https://build.nvidia.com/')}
</div>
<div class="credit-group-title">社区与灵感</div>
<p class="credit-group-desc">感谢论坛里慷慨分享免费 key 与中转站的朋友们，以及带来整个项目结构灵感的开源项目。</p>
<div class="credit-group">
  ${creditPillLink('Nodeloc 论坛分享免费 key 和上述AI中转站的朋友', 'https://www.nodeloc.com/')}${creditPillLink('embytolocalplayer', 'https://github.com/kjtsune/embytolocalplayer')}
</div>
<div class="credit-group-title">壁纸现实对照物</div>
<p class="credit-group-desc">壁纸灵感来自挪威哈当厄尔大桥——一座悬于峡湾之上的优雅斜拉桥。</p>
<div class="credit-group">${creditPillLink('Hardanger Bridge in Norway', 'https://en.wikipedia.org/wiki/Hardanger_Bridge')}</div>
<div class="credit-group-title">音乐 / 灵感</div>
<p class="credit-group-desc">开发过程中循环播放的音乐，为代码注入了灵感。</p>
<div class="credit-group">
  ${creditPill('相対性理論')}${creditPill('薬師丸悦子')}
</div>
</div>`;
}

function _renderConfigHelp() {
  const groups = [
    {
      label: '部署与配置',
      cards: [
        { icon: 'swap_horiz', title: '反代 Host 是什么？', body: `CF Workers 版 TMDB 反代 API 的上游项目在 <code>https://github.com/qqcomeup/CF-TMDB-Proxy-</code>，但并不直接适配本项目。本仓库根目录提供了 <code>edgeone_tmdb_api.js</code>，专为腾讯云 EdgeOne 边缘函数改造。以下是部署步骤：<p><strong>先配置域名：</strong>进入 EdgeOne 控制台 → <strong>域名服务 → 域名管理</strong>，添加你的自定义域名并配置源站（源站随便填，如 <code>bing.com</code>）。EdgeOne 的 SSL 证书自动申请续期大约 10 分钟才生效，不需要等，可以先部署函数。</p><ol><li><strong>创建边缘函数：</strong>进入 <strong>边缘函数 → 函数管理 → 新建函数</strong>，创建一个 <strong>Hello World</strong> 函数，创建完点"下一步"进代码编辑页。</li><li><strong>部署代码：</strong>把仓库根目录的 <code>edgeone_tmdb_api.js</code> 全部代码粘贴进去，保存并部署。</li></ol><p><strong>添加触发规则：</strong>在函数中找到 <strong>触发规则</strong>，新增一条规则（host 等于你的托管域名），保存生效。这一步相当于 Cloudflare Worker 的路由绑定。</p><p><strong>验证是否成功：</strong>部署完成后访问 <code>/health</code> 或 <code>/ping</code>，如返回 <code>{"status":"ok","timestamp":"...","uptime":"active"}</code> 说明成功。</p><p><strong>支持的接口：</strong></p><ul><li><code>/3/*</code> — TMDB API 代理（需提供 <code>api_key</code>）</li><li><code>/t/p/*</code> — TMDB 图片代理（海报/背景/头像，支持缓存）</li><li><code>/assets/*</code> — TMDB 官方资源代理（如 Logo SVG）</li><li><code>/avatar/{hash}</code> — Gravatar 头像代理</li><li><code>/admin/status</code> — 管理状态接口</li><li><code>/health</code>、<code>/ping</code> — 健康检查</li></ul><p>函数默认返回 404 页面作为伪装。API Key 通过请求头 <code>X-API-Key</code>、查询参数 <code>api_key</code> 或 <code>key</code> 传递。</p><p>完成后域名填入反代 Host 即可加速 TMDB API 调用。</p>` },
        { icon: 'speed', title: 'API 速率限制说明', body: 'TMDB 对免费账户有速率限制（约 50 次/秒）。首次启动时会批量请求剧集信息，如遇限速返回 429，程序会自动等待重试。' },
        { icon: 'save', title: 'WebUI 配置保存后不重启？', body: 'TMDB 配置（Token、API Key、语言等）保存后即时生效，无需重启 WebUI。仅更改 <code>config.toml</code> 中的服务端配置才需要重启。' }
      ]
    },
    {
      label: '功能说明',
      cards: [
        { icon: 'sync', title: '首次启动 / 手动刷新待看列表', body: '首次启动需要较长时间同步元数据，请耐心等待。在 TMDB 页面点击「刷新待看列表」按钮即可手动触发同步。' },
        { icon: 'view_list', title: 'TMDB 卡片左上角竖条的含义', body: '多季番剧的卡片左上角会显示 1~3 根彩色竖条，表示该剧的季数范围：<span style="color:#a5d6a7">1 根</span> = 2 季，<span style="color:#90caf9">2 根</span> = 3~4 季，<span style="color:#ce93d8">3 根</span> = 5 季以上（仅用于视觉区分，颜色无特殊含义）。' },
        { icon: 'label', title: '卡片上的状态标签含义', body: '每张卡片右上角显示收录状态：<span style="color:var(--primary)">已收录</span> = 已入库，<span style="color:#e37400">有疑问</span> = 待确认，灰色 = 未收录。' }
      ]
    },
    {
      label: 'OpenList 配置',
      cards: [
        { icon: 'globe', title: 'OpenList 连接配置', body: 'WebDAV 地址通常格式为 <code>http://127.0.0.1:5244/dav</code>。如果使用主动刷新功能，必须使用 <strong>admin 权限账户</strong>以调用 API 刷新路径。配置已从 <code>config.toml</code> 迁移到数据库。<p><strong>即时生效</strong>：连接信息（地址/用户名/密码/2FA）、刷新配置（开关/间隔/深度/审计周期）、行为配置（删除动作/回收站/ghost 保护/恢复延迟）、日志配置（级别/大小/路径）保存后无需重启。</p><p><strong>需重启主程序</strong>：STRM 引擎映射（引擎入口/监控目录）、A↔B 目录映射、C 区根目录变更。这些在启动期由 <code>start_watchers()</code> 读取，热更新不会重挂 watcher。</p><p>2FA 密钥（TOTP）在 OpenList 开启二次验证时才需要填写。</p>' },
        { icon: 'area_a', title: 'STRM 引擎配置表格', body: '第 1 列选择 STRM 引擎入口（如 <code>/strm</code>），第 2 列自动从 API 获取该引擎下的监控目录并显示为 tag。<p><strong>交互方式：</strong></p><ol><li>点击「测试连接」验证 API 可用性</li><li>选择引擎入口 → 自动填充监控目录 tag</li><li>可删除不需要的 tag（不监控某些目录）</li><li>点击「添加行」添加更多引擎</li></ol><p>A 区文件夹从 API 的 <code>SaveStrmLocalPath</code> 自动获取，配置页底部只读展示。</p>' },
        { icon: 'refresh', title: '主动刷新机制', body: 'OpenList 的 STRM 引擎有时需要有人去"点"一下目录，才会触发文件的生成。启用主动刷新后，程序会每隔几分钟去"模拟点击"（调用 API 获取列表），从而唤醒 OpenList 强制生成或更新最新的 STRM 文件到 A 区。<p><strong>刷新路径填写注意：</strong>不要填网盘的"真实路径"，要填 STRM 引擎映射出来的"虚拟路径"。例如真实路径 <code>/天翼云/家庭/电影</code>，引擎挂载在 <code>/strm</code>，则应填 <code>/strm/电影</code>。</p><p><strong>深度清理：</strong>如果填写的路径属于引擎管辖范围，程序刷新完后会对比本地，把云端已不存在的废弃 STRM 文件删掉。如果填错路径（不在引擎白名单），程序只会"只读刷新"，绝不清理本地文件。</p>' },
        { icon: 'settings', title: '行为配置说明', body: '<p><strong>删除动作：</strong>B 区 STRM 被删除后对 WebDAV 源文件的动作。<code>MOVE</code> = 移动到回收站目录（推荐），<code>DELETE</code> = 直接删除（危险）。</p><p><strong>Ghost 保护：</strong>B 区删除 STRM 后，A 区可能因同步延迟又短暂生成同一 STRM。Ghost 保护阻止刚删除的内容被立刻重新同步回 B 区。建议至少 300 秒。</p><p><strong>A→B 恢复延迟：</strong>等待 OpenList / STRM 引擎 / 文件系统事件落地的时间。</p>' }
      ]
    }
  ];
  return groups.map(g => `<div class="help-group"><h3 class="help-group-title" style="font-size:var(--font-base);color:var(--text-muted);margin:18px 0 8px 2px;font-weight:500;letter-spacing:.5px">${g.label}</h3>${g.cards.map(c => `<div class="config-section help-card"><h3>${icon(c.icon)} ${esc(c.title)}</h3><div style="line-height:1.7;font-size:var(--font-base);color:var(--text-muted);padding:4px 0">${c.body}</div></div>`).join('')}</div>`).join('');
}

async function _bindConfigFormEvents(cfg) {
  const tokenInput = document.getElementById('cfg-tmdb-token');
  const apiKeyInput = document.getElementById('cfg-tmdb-apikey');
  if (tokenInput) {
    tokenInput.addEventListener('focus', function () { this.type = 'text'; });
    tokenInput.addEventListener('blur', function () { this.type = 'password'; });
  }
  if (apiKeyInput) {
    apiKeyInput.addEventListener('focus', function () { this.type = 'text'; });
    apiKeyInput.addEventListener('blur', function () { this.type = 'password'; });
  }
  const bindSegmentedSwitch = (el, key) => {
    if (!el) return;
    const buttons = el.querySelectorAll('button');
    const sync = () => {
      const on = _getUiConfig(key);
      buttons.forEach(btn => {
        btn.classList.toggle('active', btn.dataset.value === (on ? 'on' : 'off'));
      });
    };
    buttons.forEach(btn => {
      btn.addEventListener('click', () => {
        _setUiConfig(key, btn.dataset.value === 'on' ? '1' : '');
        buttons.forEach(b => b.classList.toggle('active', b === btn));
      });
    });
    sync();
  };
  bindSegmentedSwitch(document.querySelector('.segmented-switch[data-key="tmdb_cache_never_remind"]'), 'tmdb_cache_never_remind');
  bindSegmentedSwitch(document.querySelector('.segmented-switch[data-key="tmdb_match_toast_disabled"]'), 'tmdb_match_toast_disabled');

  const watchlistSwitch = document.getElementById('cfg-tmdb-watchlist-enabled-switch');
  if (watchlistSwitch) {
    const watchlistButtons = watchlistSwitch.querySelectorAll('button');
    watchlistButtons.forEach(btn => {
      btn.addEventListener('click', () => {
        watchlistButtons.forEach(b => b.classList.toggle('active', b === btn));
      });
    });
  }

  const langSelect = document.getElementById('cfg-tmdb-lang-select');
  const langInput = document.getElementById('cfg-tmdb-lang');
  const langWrap = document.querySelector('[data-field="cfg-tmdb-lang"] .field-select-wrap');
  if (langSelect && langInput && langWrap) {
    const syncLang = () => {
      const isCustom = langSelect.value === 'custom';
      langWrap.classList.toggle('has-custom', isCustom);
      if (isCustom) {
        langInput.style.display = '';
        langInput.disabled = false;
        if (!langInput.value) langInput.value = '';
        langInput.focus();
      } else {
        langInput.style.display = 'none';
        langInput.disabled = true;
      }
    };
    langSelect.addEventListener('change', syncLang);
  }

  document.querySelectorAll('.floating-field input, .floating-field select').forEach(elm => {
    if (elm.id === 'cfg-tmdb-lang-select' || elm.id === 'cfg-tmdb-lang') return;
    const wrapper = elm.closest('.floating-field');
    const label = wrapper && wrapper.querySelector('.floating-label');
    if (!label) return;
    const sync = () => {
      const hv = String(elm.value || '').trim() !== '';
      const focused = document.activeElement === elm;
      const persist = elm.dataset.persistLabel === '1';
      label.classList.toggle('is-shown', hv || focused || persist);
      label.classList.toggle('is-floating', hv || focused || persist);
      label.classList.toggle('is-filled', (hv || persist) && !focused);
      elm.classList.toggle('has-value', hv);
    };
    elm.addEventListener('focus', sync);
    elm.addEventListener('blur', sync);
    elm.addEventListener('input', sync);
    sync();
  });

  document.getElementById('cfg-tmdb-save').addEventListener('click', async () => {
    const btn = document.getElementById('cfg-tmdb-save');
    btn.disabled = true;
    btn.innerHTML = '保存中...';
    try {
      const langChoice = document.getElementById('cfg-tmdb-lang-select').value;
      const langValue = langChoice === 'custom' ? (document.getElementById('cfg-tmdb-lang').value || 'zh-CN') : langChoice;
      const _wmActiveBtn = document.querySelector('#cfg-tmdb-watchlist-enabled-switch button.active');
      const watchlistEnabled = !(_wmActiveBtn && _wmActiveBtn.dataset.value === 'off');
const body = {
          language: langValue,
          host: document.getElementById('cfg-tmdb-host').value,
          proxy_http: document.getElementById('cfg-tmdb-proxy').value,
        proxy_enabled: document.getElementById('cfg-tmdb-proxy').value.trim() !== '',
        watchlist_enabled: watchlistEnabled ? 'true' : 'false',
          fuzzy_threshold: document.getElementById('cfg-tmdb-fuzzy').value || '0.60',
          anime_min_ep_ratio: document.getElementById('cfg-tmdb-ep-ratio').value || '0.30',
          anime_max_season_diff: document.getElementById('cfg-tmdb-season-diff').value || '1',
          anime_min_season_ratio: document.getElementById('cfg-tmdb-min-season-ratio').value || '0.30',
          watchlist_cache_ttl: document.getElementById('cfg-tmdb-cache-ttl').value || '604800',
        };
      // token 已配置时若输入框为空则不上传，避免截断预览覆盖真实 token
      if (tokenInput.value.trim()) body.access_token = tokenInput.value;
      if (document.getElementById('cfg-tmdb-apikey').value.trim()) body.api_key = document.getElementById('cfg-tmdb-apikey').value;
const data = await api('/api/tmdb/configure', {
          method: 'POST',
          body,
        });
        if (data.success === false) throw new Error(data.error || data.message || '保存失败');
        showToast(data.message || '已保存', 'success');
    } catch (e) {
      showToast('保存失败: ' + e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.innerHTML = `${icon('save')} 保存 TMDB 配置`;
    }
  });

  document.getElementById('cfg-tmdb-refresh').addEventListener('click', async () => {
    const btn = document.getElementById('cfg-tmdb-refresh');
    btn.disabled = true;
    btn.innerHTML = '启动同步中...';
    try {
const data = await api('/api/tmdb/watchlist/sync', { method: 'POST' });
        if (data.success) showToast('待看列表同步已启动', 'success');
        else showToast('同步失败: ' + (data.error || '未知错误'), 'error');
    } catch (e) {
      showToast('同步失败: ' + e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.innerHTML = ' 刷新待看列表';
    }
  });

  document.getElementById('cfg-tmdb-restart').addEventListener('click', async () => {
    showConfirmDialog('重启 WebUI', '确定要重启 WebUI 吗？\n\n重启期间页面将短暂不可用（约 3-5 秒）。', async () => {
      const btn = document.getElementById('cfg-tmdb-restart');
      btn.disabled = true;
      btn.innerHTML = '重启中...';
try {
          await api('/api/restart-webui', { method: 'POST' });
        showToast('WebUI 正在重启...', 'info');
      } catch (e) {
        showToast('重启失败: ' + e.message, 'error');
      } finally {
        btn.disabled = false;
        btn.innerHTML = `${icon('refresh')} 重启 WebUI`;
      }
    });
  });

  document.getElementById('cfg-tmdb-match-refresh').addEventListener('click', async () => {
    const btn = document.getElementById('cfg-tmdb-match-refresh');
    btn.disabled = true;
    btn.innerHTML = '启动刷新中...';
    try {
const data = await api('/api/tmdb/watchlist/match/refresh', { method: 'POST' });
      if (data.success) {
        showToast(data.message || '后台收录状态刷新已启动', 'info');
        btn.innerHTML = '刷新中...';
        const maxPolls = 120;
        for (let i = 0; i < maxPolls; i++) {
          await new Promise(r => setTimeout(r, 1000));
          // [已修复] L6: 轮询循环含 isRenderStale 检查
          if (isRenderStale()) return;
          try {
            const st = await api('/api/tmdb/watchlist/match/status');
            if (!st.running && st.result) {
              const r = st.result;
              if (r.error) {
                showToast('收录状态刷新失败: ' + r.error, 'error');
                // [已修复] N-P1-5: 匹配刷新失败时 break，避免错误toast风暴
                break;
              } else {
                const manualInfo = r.skipped_manual > 0 ? ` · 跳过 ${r.skipped_manual} 个人工覆盖` : '';
                const uncomputedInfo = r.uncomputed > 0 ? ` · ${r.uncomputed} 项未计算` : '';
                showToast(`收录状态已刷新: ${r.matched} 已收录 / ${r.fuzzy} 待确认 / ${r.unmatched} 未收录${uncomputedInfo} (共 ${r.total} 项)${manualInfo}`, 'success');
                _tmdbCache.movies = null;
                _tmdbCache.tv = null;
              }
              break;
            }
          } catch (e) {
            console.warn('[Match Refresh] 轮询状态失败:', e);
            if (i === maxPolls - 1) {
              showToast('收录状态刷新超时，请稍后重试', 'error');
            }
          }
        }
      } else {
        showToast(data.message || '启动刷新失败', 'error');
      }
    } catch (e) {
      showToast('刷新失败: ' + e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.innerHTML = '刷新收录状态';
    }
  });
}
