import { api } from '../core/api.js';
import { icon } from '../core/icons.js';
import { esc, createField } from '../core/utils.js';
import { showToast } from '../components/toast.js';
import { showConfirmDialog } from '../components/dialog.js';
import { OpenListState } from '../core/state.js';
import { captureRenderGuard } from '../core/router.js';

const _openlistHelpTexts = {
  webdav_host: 'OpenList 的 WebDAV 服务地址。\n格式：http://IP:端口/dav\n例如：http://127.0.0.1:5244/dav',
  webdav_user: '必须使用具有管理员权限的账户，以调用 API 刷新路径。',
  webdav_password: 'WebDAV 登录密码。\n保存时会加密存储。',
  webdav_totp_secret: '两步验证密钥（可选）。\n如果 OpenList 开启了两步验证，需要填写此字段。',
  b_root: '媒体库实际扫描的目录，程序会把 A 区 STRM 同步到这里。',
  c_root: '用于幽灵迁移、异常隔离等场景。',
  monitored_paths: '该引擎下挂载的真实云盘目录（如 /天翼云/番剧）。\n选择引擎入口后自动从 API 获取，不可手动输入。\n引擎配置变更时，刷新路径区域会自动补全缺失的子路径（仅添加，不覆盖用户手动配置的路径）。',
  refresh_paths: '主动刷新路径列表，格式为"引擎入口/文件夹名"（如 /strm/电影）。\n程序会定期请求这些路径，让 OpenList 重新扫描目录并生成 STRM。\n\n引擎配置变更时，系统会自动补全缺失的子路径（仅添加不覆盖），但你可随时手动增删。\n已保存的刷新路径在引擎配置再变动时不会丢失。',
  strm_engines: 'STRM 引擎配置。\n选择引擎入口后，自动填充该引擎挂载的真实云盘目录（仅作引擎配置 / A 区派生用）。\n\n刷新路径区域（下方"刷新配置"）会在此变更时自动补全缺失的引擎子路径，不会覆盖你已有的手动配置。\n只有已配置的引擎才会显示在 A 区文件夹中。',
  refresh_enabled: '是否启用主动刷新。\nfalse：只依赖文件系统事件和删除后的延迟清理。\ntrue：程序会周期性扫描 refresh_paths 中的 WebDAV 路径。\n保存后即时生效，无需重启。',
  refresh_interval_minutes: '主动刷新的时间间隔（分钟）。\n建议：5-30 分钟，根据媒体库大小调整。\n保存后即时生效，无需重启。',
  refresh_depth: 'WebDAV 主动刷新递归深度。数值越大，请求越多。\n建议：测试 2~3，正式 3~5。\n保存后即时生效，无需重启。',
  behavior_action: 'B 区 STRM 被删除后，对 WebDAV 源文件执行的动作。\nMOVE：移动到回收站目录（推荐）。\nDELETE：直接删除（危险，不建议）。',
  behavior_trash_dir_name: 'WebDAV 回收站目录名。\naction = MOVE 时生效。移动的路径由 STRM 文件内容反向拼凑。',
  behavior_ghost_protect_seconds: 'ghost 保护时间，单位：秒。\nB 区删除 STRM 后，A 区可能因为同步延迟又短暂生成同一 STRM。\nghost 保护用于阻止刚删除的内容被立刻重新同步回 B 区。\n建议：至少 300 秒。',
  behavior_a_to_b_restore_delay_seconds: 'A→B 恢复延迟时间（秒）。\n当 A 区重新生成 STRM 后，延迟多久再同步到 B 区。\n建议：30-60 秒。',
  behavior_sync_on_startup: '启动时是否执行全量同步。\ntrue：启动时同步所有 STRM 文件。\nfalse：只同步增量变更。',
  behavior_sync_on_startup_wait: '启动等待时间（秒）。\n启动后等待多久再开始同步，给系统预留初始化时间。\n建议：0-10 秒。',
  log_level: '日志记录级别。\nDEBUG：最详细，包含所有调试信息。\nINFO：常规信息（推荐）。\nWARNING：只记录警告和错误。\nERROR：只记录错误。',
  log_max_size_mb: '单个日志文件最大大小（MB）。\n超过此大小会自动轮转。\n建议：2-10 MB。',
  log_backup_count: '保留的历史日志文件数量。\n超过此数量的旧日志会被删除。\n建议：5-10 个。',
  refresh_full_audit_interval_days: '每隔多少天执行一次 A→B 全量审计。\n设为 0 可关闭周期审计。\n保存后即时生效。',
  };

function _olHelpIcon(key, tooltipBelow = false) {
  const snakeKey = key.replace(/-/g, '_');
  const text = _openlistHelpTexts[snakeKey];
  if (!text) return '';
  const safeText = esc(text);
  const belowClass = tooltipBelow ? ' tooltip-below' : '';
  return `<span class="ol-help-icon${belowClass}" data-tooltip="${safeText}" aria-label="帮助">${icon('info')}</span>`;
}

export async function _renderOpenListConfig(cfg) {
  // 代际快照工厂——在首次 await 前捕获
  const isStale = captureRenderGuard();
  // 在 await 前捕获容器引用，避免异步竞态将旧 HTML 插入当前子页
  const subpage = document.getElementById('config-subpage');
  if (!subpage || !subpage.isConnected) return;

  let openlistCfg = {};
  try {
    const resp = await api('/api/webui/config/openlist');
    if (isStale()) return; // await 返回后若已离开子页，放弃后续渲染
    if (resp.success && resp.config) openlistCfg = resp.config;
  } catch (e) { /* ignore */ }

  try {
    const engResp = await api('/api/openlist/strm-engines');
    if (isStale()) return; // await 返回后若已离开子页，放弃后续同步处理
    if (engResp.success) OpenListState.availableEngines = engResp.engines || [];
  } catch (e) { OpenListState.availableEngines = []; }

  OpenListState.strmEngines = [];
  try {
    if (openlistCfg.strm_engines) OpenListState.strmEngines = JSON.parse(openlistCfg.strm_engines);
  } catch (e) { OpenListState.strmEngines = []; }
  if (!OpenListState.strmEngines.length) OpenListState.strmEngines = [{ engine: '', monitored_paths: [] }];

  OpenListState.configured = !!(openlistCfg.webdav_host && openlistCfg.webdav_host.trim());
  _updateApiStatusDot();

  let refreshPaths = [];
  try {
    if (openlistCfg.refresh_paths) refreshPaths = JSON.parse(openlistCfg.refresh_paths);
  } catch (e) { refreshPaths = []; }

  OpenListState.refreshPaths = refreshPaths.slice();

  let abMappings = [];
  try {
    if (openlistCfg.a_b_mappings) abMappings = JSON.parse(openlistCfg.a_b_mappings);
  } catch (e) { abMappings = []; }
  OpenListState.abMappings = abMappings;

  function olField(id, label, value, placeholder, type = 'text', persistLabel = false, readOnly = false, helperText = '', helpKey = '', htmlLabel = '') {
    const key = helpKey || id.replace(/^ol-/, '');
    return createField(id, label, value, {
      placeholder, type, persistLabel, readOnly,
      helpIcon: _olHelpIcon(key),
      helperText,
      htmlLabel
    });
  }

  function olSelect(id, label, value, options, persistLabel = false, helpKey = '', helperText = '') {
    const key = helpKey || id.replace(/^ol-/, '');
    const hasValue = value !== null && value !== undefined && String(value).trim() !== '';
    const floated = hasValue || persistLabel;
    const labelCls = floated
      ? `floating-label is-shown is-floating${hasValue ? ' is-filled' : ''}`
      : 'floating-label';
    const persistAttr = persistLabel ? ' data-persist-label="1"' : '';
    let optsHtml = options.map(o => {
      const selected = o.value === value ? ' selected' : '';
      return `<option value="${esc(o.value)}"${selected}>${esc(o.label)}</option>`;
    }).join('');
    return `
      <div class="floating-field" data-field="${id}">
        <div class="field-control">
          <label class="${labelCls}" data-role="label" for="${id}">${esc(label)}${_olHelpIcon(key)}</label>
          <select id="${id}" class="ol-select"${persistAttr}>${optsHtml}</select>
        </div>${helperText ? `<div class="field-helper-text">${esc(helperText)}</div>` : ''}
      </div>`;
  }

  function olToggle(id, label, checked, helpKey = '', helperText = '') {
    const key = helpKey || id.replace(/^ol-/, '');
    return `<div class="toggle-row">
      <span>${esc(label)}${_olHelpIcon(key)}</span>
      <div class="segmented-switch" data-key="${id}" id="${id}">
        <button type="button" data-value="on"${checked ? ' class="active"' : ''}>开</button>
        <button type="button" data-value="off"${!checked ? ' class="active"' : ''}>关</button>
      </div>
    </div>${helperText ? `<div class="field-helper-text">${esc(helperText)}</div>` : ''}`;
  }

  function buildEngineTable() {
    let html = '<div class="strm-engine-wrap"><span class="monitored-paths-help">' + _olHelpIcon('monitored_paths') + '</span>';
    html += '<table class="strm-engine-table"><thead><tr><th style="width:35%">STRM 引擎入口</th><th>监控目录</th><th style="width:60px">操作</th></tr></thead><tbody>';
    OpenListState.strmEngines.forEach((row, idx) => {
      html += buildEngineRow(idx, row, OpenListState.strmEngines.length);
    });
    html += '</tbody></table>';
    const addDisabled = !OpenListState.configured ? ' disabled' : '';
    html += `<div class="table-actions">
      <button class="table-btn primary" id="add-engine-row"${addDisabled}>+ 添加行</button>
    </div></div>`;
    return html;
  }

  function buildEngineRow(idx, row, totalRows) {
    let engineOptions = '<option value="">选择引擎...</option>';
    const selectedEngines = new Set(OpenListState.strmEngines.map((r, ri) => ri === idx ? '' : r.engine).filter(Boolean));
    OpenListState.availableEngines.forEach(eng => {
      const selected = row.engine === eng.mount_path ? ' selected' : '';
      const disabled = !selected && selectedEngines.has(eng.mount_path) ? ' disabled' : '';
      engineOptions += `<option value="${esc(eng.mount_path)}"${selected}${disabled}>${esc(eng.mount_path)}</option>`;
    });
    const disabledAttr = !OpenListState.configured ? ' disabled' : '';

    let tagsHtml = '';
    if (row.monitored_paths && row.monitored_paths.length) {
      row.monitored_paths.forEach((p, pi) => {
        tagsHtml += `<span class="tag">${esc(p)}<button class="tag-remove" data-row="${idx}" data-pi="${pi}" title="删除">×</button></span>`;
      });
    }

    const rowDeleteDisabled = OpenListState.strmEngines.length <= 1 ? ' disabled' : '';
    return `<tr data-row-idx="${idx}">
      <td>
        <select class="engine-select" data-row="${idx}"${disabledAttr}>
          ${engineOptions}
        </select>
      </td>
      <td>
        <div class="tag-container" data-row="${idx}" id="tag-container-${idx}">
          ${tagsHtml || '<span style="color:var(--text-muted);font-size:calc(var(--font-base) - 1px)">选择引擎后自动填充</span>'}
        </div>
      </td>
      <td style="text-align:center">
        <button class="table-btn danger" data-delete-row="${idx}"${rowDeleteDisabled} title="删除此行">删除</button>
      </td>
    </tr>`;
  }

  let html = `<div class="openlist-form">`;
  
  html += `<div class="config-section"><h3>OpenList 连接</h3>
    <div class="ol-conn-header" style="margin-bottom:10px">
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="18" height="18" class="ol-conn-icon" style="color:#38bdf8"><path fill="currentColor" d="M244.57,776.75c-10.1,0-20.31-2.78-29.46-8.59-25.63-16.3-33.2-50.29-16.9-75.92l201.93-317.6c16.3-25.63,50.29-33.2,75.92-16.9,25.63,16.3,33.2,50.29,16.9,75.92l-201.93,317.6c-10.48,16.48-28.28,25.5-46.46,25.5Z"/><path fill="#99f6e4" d="M509.93,907.83c-35.01,0-67.29-4.84-91.84-13.86-15.63-5.74-27.82-18.25-33.15-34.03s-3.23-33.12,5.72-47.16l174.43-273.77c16.32-25.62,50.32-33.15,75.94-16.83,25.62,16.32,33.15,50.32,16.83,75.94l-126.68,198.82c25.39-1.89,54.61-7.42,84.56-19.13,71.29-27.87,127.46-82.26,158.15-153.15,30.53-70.52,31.94-147.78,3.98-217.56-28.43-70.95-82.76-126.78-152.98-157.2-70.23-30.42-147.71-31.59-218.17-3.27-73.46,29.52-126.75,82.48-158.4,157.4-11.82,27.98-44.08,41.08-72.07,29.27-27.98-11.82-41.08-44.08-29.27-72.07,42.86-101.46,118.49-176.38,218.71-216.66,49.54-19.91,101.59-29.4,154.67-28.2,51.11,1.15,100.99,12.12,148.25,32.6,47.14,20.42,89.3,49.27,125.31,85.75,37.27,37.75,66.22,81.98,86.05,131.46,19.64,49.01,28.94,100.74,27.64,153.75-1.26,51.15-12.28,101.08-32.77,148.42-20.54,47.45-49.51,89.78-86.11,125.81-38.15,37.56-82.88,66.53-132.94,86.09-42.5,16.61-89.11,26.08-134.81,27.39-3.71.11-7.4.16-11.05.16Z"/></svg>
      <span class="api-status-dot ${OpenListState.apiStatus}" id="ol-status-dot"></span>
      <span class="ol-conn-status-text" id="ol-status-text">${(() => {
        const cfgOk = !!OpenListState.configured;
        const st = OpenListState.apiStatus;
        if (!cfgOk) return 'OpenList 未配置';
        if (st === 'online') return 'OpenList 已连接';
        if (st === 'offline') return 'OpenList 已配置（离线）';
        if (st === 'auth_failed_password') return 'OpenList 密码错误';
        if (st === 'auth_failed_2fa') return 'OpenList 2FA 错误';
        if (st === 'auth_failed') return 'OpenList 认证失败';
        return 'OpenList 已配置';
      })()}</span>
    </div>
    <div class="field-grid">
      ${olField('ol-webdav-host', 'WebDAV 地址', openlistCfg.webdav_host || '', 'http://127.0.0.1:5244/dav')}
      ${olField('ol-webdav-user', '用户名', openlistCfg.webdav_user || '', 'admin')}
      ${olField('ol-webdav-password', '密码', '', '输入密码', 'password', false, false, '', '', openlistCfg.webdav_password ? '<span class="configured-badge">✓ 已配置</span>' : '')}
      ${olField('ol-webdav-totp-secret', '2FA 密钥', '', '留空则不使用 2FA', 'password', false, false, '', '', openlistCfg.webdav_totp_secret ? '<span class="configured-badge">✓ 已配置</span>' : '')}
      ${olField('ol-c-root', 'C 区根目录', openlistCfg.c_root || cfg.c_root || '', '请填写 C 区根目录的绝对路径')}
    </div>
    <div class="openlist-top-actions">
      <button class="toolbar-btn primary" id="ol-save">保存</button>
      <button class="toolbar-btn secondary" id="ol-test-connection">测试连接</button>
      <button class="toolbar-btn secondary" id="ol-restart-webui">${icon('refresh')} 重启Bridge主程序</button>
      </div>
  </div>`;

  html += `<div class="config-section"><h3>${icon('area_a')} STRM 引擎配置</h3>
    <div id="strm-engine-table-wrap">${buildEngineTable()}</div>
  </div>`;

  html += `<div class="openlist-2col-grid">`;

  const refreshEnabled = (openlistCfg.refresh_enabled || 'true').toLowerCase() === 'true';
  const refreshInterval = openlistCfg.refresh_interval_minutes || '10';
  const refreshDepth = openlistCfg.refresh_depth || '5';
  const fullAuditDays = openlistCfg.refresh_full_audit_interval_days || '7';
  html += `<div class="config-section"><h3>${icon('refresh')} 刷新配置</h3>
    ${olToggle('ol-refresh-enabled', '启用主动刷新', refreshEnabled)}
    <div class="field-grid">
      ${olField('ol-refresh-interval', '刷新间隔 (分钟)', refreshInterval, '10', 'number', false, false, '', 'refresh_interval_minutes')}
      ${olField('ol-refresh-depth', '刷新深度', refreshDepth, '5', 'number')}
      ${olField('ol-refresh-audit-days', '全量审计周期 (天，0=关闭)', fullAuditDays, '7', 'number', false, false, '', 'refresh_full_audit_interval_days')}
    </div>
    <div style="margin-top:12px">
      <div style="margin-bottom:6px">
        <span>刷新路径${_olHelpIcon('refresh_paths')}</span>
      </div>
      <div class="refresh-paths-tags" id="refresh-paths-tags"></div>
      <div style="display:flex;gap:6px;margin-top:6px">
        <input type="text" id="refresh-path-input" placeholder="输入 WebDAV 路径" style="flex:1;padding:6px 10px;border:1px solid color-mix(in srgb,var(--border-color) 30%,transparent);border-radius:var(--radius-control);background:var(--bg-control);font:inherit;color:var(--text-main);font-size:calc(var(--font-base) - 1px)">
        <button class="table-btn" id="add-refresh-path-btn">添加</button>
      </div>
    </div>
  </div>`;

  const syncOnStartup = (openlistCfg.behavior_sync_on_startup || 'false').toLowerCase() === 'true';
  const currentAction = openlistCfg.behavior_action || 'MOVE';
  html += `<div class="config-section"><h3>${icon('settings')} 行为配置</h3>
    <div class="field-grid" style="grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:6px 12px">
      ${olSelect('ol-action', '删除动作', currentAction, [
        { value: 'MOVE', label: 'MOVE — 移动到回收站（推荐）' },
        { value: 'DELETE', label: 'DELETE — 直接删除（危险）' }
      ], false, 'behavior_action')}
      ${olField('ol-trash-dir', '回收站目录名', openlistCfg.behavior_trash_dir_name || 'trash', 'strm_回收站', 'text', false, false, '', 'behavior_trash_dir_name')}
      ${olField('ol-ghost-protect', 'Ghost 保护时间 (秒)', openlistCfg.behavior_ghost_protect_seconds || '300', '300', 'number', false, false, '', 'behavior_ghost_protect_seconds')}
      ${olField('ol-restore-delay', 'A→B 恢复延迟 (秒)', openlistCfg.behavior_a_to_b_restore_delay_seconds || '30', '30', 'number', false, false, '', 'behavior_a_to_b_restore_delay_seconds')}
      ${olField('ol-startup-wait', '启动等待时间 (秒)', openlistCfg.behavior_sync_on_startup_wait || '0', '0', 'number', false, false, '', 'behavior_sync_on_startup_wait')}
    </div>
    ${olToggle('ol-sync-startup', '启动时全量同步', syncOnStartup, 'behavior_sync_on_startup')}
  </div>`;

  const currentLogLevel = openlistCfg.log_level || 'INFO';
  html += `<div class="config-section"><h3>${icon('log')} 日志配置</h3>
    <div class="field-grid">
      ${olSelect('ol-log-level', '日志级别', currentLogLevel, [
        { value: 'DEBUG', label: 'DEBUG — 调试' },
        { value: 'INFO', label: 'INFO — 信息（推荐）' },
        { value: 'WARNING', label: 'WARNING — 警告' },
        { value: 'ERROR', label: 'ERROR — 错误' }
      ], false, 'log_level')}
      ${olField('ol-log-max-size', '日志最大大小 (MB)', openlistCfg.log_max_size_mb || '2', '2', 'number', false, false, '', 'log_max_size_mb')}
      ${olField('ol-log-backup-count', '历史日志数量', openlistCfg.log_backup_count || '5', '5', 'number')}
      </div>
  </div>`;

  const configuredEngines = OpenListState.strmEngines.filter(e => e.engine);
  const aFolders = [];
  configuredEngines.forEach(eng => {
    const engData = OpenListState.availableEngines.find(e => e.mount_path === eng.engine);
    if (engData && engData.local_path && !aFolders.includes(engData.local_path)) {
      aFolders.push(engData.local_path);
    }
  });
  html += `<div class="config-section"><h3>${icon('folder')} A↔B 目录映射</h3>
    <div class="ab-mappings-display" id="ab-mappings-display">
      ${aFolders.length ? aFolders.map((f, i) => {
        // 从已保存的 a_b_mappings 回填 B 根
        let savedBRoot = '';
        if (openlistCfg.a_b_mappings) {
          try {
            const mappings = JSON.parse(openlistCfg.a_b_mappings);
            const match = mappings.find(m => m.a_root === f);
            if (match) savedBRoot = match.b_root || '';
          } catch (e) {}
        }
        return `
        <div class="ab-mapping-row">
          <span class="a-folder-chip">${esc(f)}</span>
          <div class="floating-field" data-field="b-root-${i}">
            <div class="field-control">
              <label class="floating-label${savedBRoot ? ' is-shown is-floating is-filled' : ''}" data-role="label" for="b-root-${i}">B 区根目录${_olHelpIcon('b_root')}</label>
              <input type="text" id="b-root-${i}" class="b-root-input${savedBRoot ? ' has-value' : ''}"
                     data-a-root="${esc(f)}"
                     value="${esc(savedBRoot)}"
                     placeholder="B 区根目录（如 D:\\emby\\strm）">
            </div>
          </div>
        </div>`;
      }).join('') : '<span style="color:var(--text-muted)">暂无数据（请先配置 STRM 引擎）</span>'}
    </div>
  </div>`;

  html += `</div>`;
  html += `</div>`;

  // openlist.js 异步竞态，await 后检查容器连接状态和渲染代际
  if (subpage && subpage.isConnected && !isStale()) {
    const backBtn = subpage.querySelector('.config-back-btn');
    if (backBtn) {
      backBtn.insertAdjacentHTML('afterend', html);
    } else {
      subpage.innerHTML = html;
    }
    const loadingSpinner = subpage.querySelector('.loading');
    if (loadingSpinner) loadingSpinner.remove();
  }

  _bindOpenListFormEvents(cfg, openlistCfg);
  _renderRefreshPathsTags();
  _bindEngineSelectEvents();
}

function _syncRefreshPathsFromPreviewEngines() {
    // 从引擎配置推导缺失的刷新路径（仅添加新路径，不覆盖已有路径）
    const derived = new Set();
    OpenListState.strmEngines.forEach(row => {
      if (!row || !row.engine || !row.monitored_paths) return;
      const engineEntry = row.engine;
      row.monitored_paths.forEach(p => {
        // 设计局限：仅取最后一段目录推导 refresh path，仅支持顶层挂载。
        // 嵌套挂载（如 /天翼云/电影/动作 → /strm/动作）会推导错；
        // 支持嵌套需改为完整相对路径。登记于 docs/否决方案.md。
        const lastDir = String(p).replace(/\/$/, '').split('/').pop();
        if (lastDir) {
          const refreshPath = `${engineEntry.replace(/\/$/, '')}/${lastDir}`;
          derived.add(refreshPath);
        }
      });
    });
    // 合并：保留用户已有的路径 + 补充引擎配置中新增的推导路径
    const existing = OpenListState.refreshPaths || [];
    const existingSet = new Set(existing);
    for (const d of derived) {
      if (!existingSet.has(d)) {
        existing.push(d);
      }
    }
    OpenListState.refreshPaths = existing;
    _renderRefreshPathsTags();
  }

function _renderRefreshPathsTags() {
  const container = document.getElementById('refresh-paths-tags');
  if (!container) return;
  const paths = OpenListState.refreshPaths || [];
  if (!paths.length) {
    container.innerHTML = '<span class="refresh-paths-empty">暂无刷新路径，可手动添加</span>';
    return;
  }
  container.innerHTML = paths.map((p, i) => 
    `<span class="tag">${esc(p)}<button class="tag-remove" data-path-idx="${i}" title="删除">×</button></span>`
  ).join('');
  if (!container._delegatedListener) {
    container._delegatedListener = (e) => {
      const btn = e.target.closest('.tag-remove');
      if (!btn || !container.contains(btn)) return;
      const idx = parseInt(btn.dataset.pathIdx);
      const path = OpenListState.refreshPaths && OpenListState.refreshPaths[idx];
      if (path === undefined) return;
      OpenListState.refreshPaths.splice(idx, 1);
      _renderRefreshPathsTags();
    };
    container.addEventListener('click', container._delegatedListener);
  }
}

function _bindOpenListFormEvents(cfg, openlistCfg) {
  document.querySelectorAll('.openlist-form .floating-field input, .openlist-form .floating-field select').forEach(elm => {
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

  const passwordFields = ['ol-webdav-password', 'ol-webdav-totp-secret'];
  passwordFields.forEach(id => {
    const input = document.getElementById(id);
    if (input) {
      input.addEventListener('focus', function() { this.type = 'text'; });
      input.addEventListener('blur', function() { this.type = 'password'; });
    }
  });

  const bindSegmentedSwitch = (id) => {
    const el = document.getElementById(id);
    if (!el) return;
    const buttons = el.querySelectorAll('button');
    buttons.forEach(btn => {
      btn.addEventListener('click', () => {
        buttons.forEach(b => b.classList.toggle('active', b === btn));
      });
    });
  };
  bindSegmentedSwitch('ol-refresh-enabled');
  bindSegmentedSwitch('ol-sync-startup');

  document.getElementById('ol-test-connection')?.addEventListener('click', async () => {
    const btn = document.getElementById('ol-test-connection');
    btn.disabled = true;
    btn.innerHTML = '测试中...';
    try {
const body = {
            host: document.getElementById('ol-webdav-host')?.value || '',
            user: document.getElementById('ol-webdav-user')?.value || '',
          };
          // 密码框/2FA 框为空时省略字段，使后端回退到已保存的凭据
          const pwValue = document.getElementById('ol-webdav-password')?.value || '';
          const totpValue = document.getElementById('ol-webdav-totp-secret')?.value || '';
          if (pwValue) body.password = pwValue;
          if (totpValue) body.totp_secret = totpValue;
const data = await api('/api/openlist/test-connection', {
          method: 'POST',
          body,
        });
      if (data.success) {
        showToast('连接成功！', 'success');
        OpenListState.apiStatus = 'online';
        try {
          const engResp = await api('/api/openlist/strm-engines');
          if (engResp.success) {
            OpenListState.availableEngines = engResp.engines || [];
            _refreshEngineTable();
          }
        } catch (e) { }
      } else {
        const errType = data.error_type || 'unknown';
        const statusMap = {
          'wrong_password': 'auth_failed_password',
          'wrong_2fa': 'auth_failed_2fa',
          'account_not_found': 'auth_failed',
          'network_error': 'offline',
          'exception': 'offline',
        };
        OpenListState.apiStatus = statusMap[errType] || 'auth_failed';
        showToast('连接失败: ' + (data.error || '未知错误'), 'error');
        document.querySelectorAll('.engine-select').forEach(s => s.disabled = true);
        const addBtn = document.getElementById('add-engine-row');
        if (addBtn) addBtn.disabled = true;
      }
    } catch (e) {
      showToast('测试失败: ' + e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.innerHTML = `测试连接`;
    }
  });

  document.getElementById('ol-restart-webui')?.addEventListener('click', async () => {
    showConfirmDialog(
      '重启 Bridge 主程序',
      '确定要重启 Bridge 主程序吗？\n\n重启将重新加载 STRM 存储映射，期间 WebUI 将短暂不可用（约 3-5 秒）。',
      async () => {
        const btn = document.getElementById('ol-restart-webui');
        btn.disabled = true;
        btn.innerHTML = '重启中...';
        showToast('正在重启主程序，请稍候...', 'info');
        try {
const data = await api('/api/restart-webui', { method: 'POST' });
          if (data.success) {
            showToast('主程序正在重启，请稍候刷新页面...', 'success');
            setTimeout(() => {
              window.location.reload();
            }, 4000);
          } else {
            showToast('重启失败: ' + (data.message || '未知错误'), 'error');
            btn.disabled = false;
            btn.innerHTML = `${icon('refresh')} 重启Bridge主程序`;
          }
        } catch (e) {
          showToast('重启请求失败: ' + e.message, 'error');
          btn.disabled = false;
          btn.innerHTML = `${icon('refresh')} 重启Bridge主程序`;
        }
      }
    );
  });



  const engineTableWrap = document.getElementById('strm-engine-table-wrap');
  if (engineTableWrap && !engineTableWrap._delegatedTagRemoveListener) {
    engineTableWrap._delegatedTagRemoveListener = (e) => {
      const btn = e.target.closest('.tag-remove');
      if (!btn || !engineTableWrap.contains(btn)) return;
      const ri = parseInt(btn.dataset.row);
      const pi = parseInt(btn.dataset.pi);
      if (OpenListState.strmEngines[ri] && OpenListState.strmEngines[ri].monitored_paths) {
        OpenListState.strmEngines[ri].monitored_paths.splice(pi, 1);
        _refreshEngineRow(ri);
        _syncRefreshPathsFromPreviewEngines();
      }
    };
    engineTableWrap.addEventListener('click', engineTableWrap._delegatedTagRemoveListener);
  }

  document.querySelectorAll('[data-delete-row]').forEach(btn => {
    btn.addEventListener('click', () => {
      const idx = parseInt(btn.dataset.deleteRow);
      if (OpenListState.strmEngines.length < 1) return;
      OpenListState.strmEngines.splice(idx, 1);
      _refreshEngineTable();
      _syncRefreshPathsFromPreviewEngines();
    });
  });

  document.getElementById('add-engine-row')?.addEventListener('click', () => {
    OpenListState.strmEngines.push({ engine: '', monitored_paths: [] });
    _refreshEngineTable();
    _syncRefreshPathsFromPreviewEngines();
  });

  document.getElementById('add-refresh-path-btn')?.addEventListener('click', () => {
    const input = document.getElementById('refresh-path-input');
    if (!input) return;
    const path = input.value.trim();
    if (!path) return;
    if (!OpenListState.refreshPaths) OpenListState.refreshPaths = [];
    if (OpenListState.refreshPaths.includes(path)) {
      showToast('该路径已存在', 'info');
      return;
    }
    OpenListState.refreshPaths.push(path);
    input.value = '';
    _renderRefreshPathsTags();
  });
  document.getElementById('refresh-path-input')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') document.getElementById('add-refresh-path-btn')?.click();
  });

  document.getElementById('ol-save')?.addEventListener('click', async () => {
    const btn = document.getElementById('ol-save');
    // 分层必填校验：host 硬必填（return 前不得 disable 按钮，避免卡死）
    const host = document.getElementById('ol-webdav-host')?.value.trim();
    if (!host) {
      showToast('请填写必填项：OpenList 地址', 'error');
      return;
    }
    const cRoot = document.getElementById('ol-c-root')?.value.trim();
    if (!cRoot) {
      showToast('提示：C 区根目录 未填写，可先保存连接信息，稍后再配置路径', 'info');
      // 不 return，继续保存（后端支持空 c_root）
    }
    // 收集 A↔B 映射
    const configuredEngines = (OpenListState.strmEngines || [])
      .filter(e => e && typeof e.engine === 'string' && e.engine.trim())
      .map(e => ({ engine: e.engine, monitored_paths: Array.isArray(e.monitored_paths) ? e.monitored_paths : [] }));

    const aFolders = [];
    configuredEngines.forEach(eng => {
      const engData = OpenListState.availableEngines.find(e => e.mount_path === eng.engine);
      if (engData && engData.local_path && !aFolders.includes(engData.local_path)) {
        aFolders.push(engData.local_path);
      }
    });

    // 校验：每个 A 文件夹必须有对应 B 根（空则报错阻止保存）
    const abMappings = [];
    const missingB = [];
    document.querySelectorAll('.ab-mapping-row').forEach(row => {
      const aRoot = row.querySelector('.a-folder-chip')?.textContent?.trim() || '';
      const bRoot = row.querySelector('.b-root-input')?.value?.trim() || '';
      if (aRoot && bRoot) {
        abMappings.push({ a_root: aRoot, b_root: bRoot, label: '' });
      } else if (aRoot && !bRoot) {
        missingB.push(aRoot);
      }
    });
    if (missingB.length) {
      showToast(`以下 A 区缺少 B 根映射: ${missingB.join('、')}`, 'error');
      return;  // 阻止保存
    }

    btn.disabled = true;
    btn.innerHTML = '保存中...';
    try {
      const body = {
        webdav_host: document.getElementById('ol-webdav-host')?.value || '',
        webdav_user: document.getElementById('ol-webdav-user')?.value || '',
        c_root: document.getElementById('ol-c-root')?.value || '',
        refresh_paths: JSON.stringify(OpenListState.refreshPaths || []),
        strm_engines: JSON.stringify(configuredEngines),
        a_b_mappings: JSON.stringify(abMappings),  // ← 新增，替代 b_root
        refresh_enabled: document.querySelector('#ol-refresh-enabled button[data-value="on"].active') ? 'true' : 'false',
    refresh_interval_minutes: document.getElementById('ol-refresh-interval')?.value || '10',
    refresh_depth: document.getElementById('ol-refresh-depth')?.value || '5',
    refresh_full_audit_interval_days: document.getElementById('ol-refresh-audit-days')?.value || '7',
        // refresh_log_level 已合并至全局 log_level，不再单独上传
        behavior_action: document.getElementById('ol-action')?.value || 'MOVE',
        behavior_trash_dir_name: document.getElementById('ol-trash-dir')?.value || 'trash',
        behavior_ghost_protect_seconds: document.getElementById('ol-ghost-protect')?.value || '300',
        behavior_a_to_b_restore_delay_seconds: document.getElementById('ol-restore-delay')?.value || '30',
        behavior_sync_on_startup: document.querySelector('#ol-sync-startup button[data-value="on"].active') ? 'true' : 'false',
        behavior_sync_on_startup_wait: document.getElementById('ol-startup-wait')?.value || '0',
        log_level: document.getElementById('ol-log-level')?.value || 'INFO',
        log_max_size_mb: document.getElementById('ol-log-max-size')?.value || '2',
        log_backup_count: document.getElementById('ol-log-backup-count')?.value || '5',
        };
      // 仅当用户实际输入时才上传密码/2FA，避免空值覆盖已有凭据
      const pwValue = document.getElementById('ol-webdav-password')?.value || '';
      const totpValue = document.getElementById('ol-webdav-totp-secret')?.value || '';
      if (pwValue) body.webdav_password = pwValue;
      if (totpValue) body.webdav_totp_secret = totpValue;
const data = await api('/api/webui/config/openlist', {
          method: 'POST',
          body,
        });
if (data.success) {
        showToast('OpenList 配置已保存并热更新', 'success');
        const savedHost = document.getElementById('ol-webdav-host')?.value || '';
        OpenListState.configured = !!savedHost.trim();
        OpenListState.abMappings = abMappings;
        _updateApiStatusDot();
        await _checkApiStatus();
        _refreshABMappings();
      } else {
        showToast('保存失败: ' + (data.error || '未知错误'), 'error');
      }
    } catch (e) {
      showToast('保存失败: ' + e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.innerHTML = `保存`;
    }
  });
}

function _refreshABMappings() {
    const container = document.getElementById('ab-mappings-display');
    if (!container) return;
    const configuredEngines = OpenListState.strmEngines.filter(e => e.engine);
    const aFolders = [];
    configuredEngines.forEach(eng => {
      const engData = OpenListState.availableEngines.find(e => e.mount_path === eng.engine);
      if (engData && engData.local_path && !aFolders.includes(engData.local_path)) {
        aFolders.push(engData.local_path);
      }
    });
    // 从已保存的 a_b_mappings 回填 B 根
    const savedMap = {};
    (OpenListState.abMappings || []).forEach(m => { savedMap[m.a_root] = m.b_root; });
    container.innerHTML = aFolders.length
      ? aFolders.map((f, i) => {
        const bVal = savedMap[f] || '';
        return `
        <div class="ab-mapping-row">
          <span class="a-folder-chip">${esc(f)}</span>
          <div class="floating-field" data-field="b-root-${i}">
            <div class="field-control">
              <label class="floating-label${bVal ? ' is-shown is-floating is-filled' : ''}" data-role="label" for="b-root-${i}">B 区根目录${_olHelpIcon('b_root')}</label>
              <input type="text" id="b-root-${i}" class="b-root-input${bVal ? ' has-value' : ''}"
                     data-a-root="${esc(f)}"
                     value="${esc(bVal)}"
                     placeholder="B 区根目录（如 D:\\emby\\strm）">
            </div>
          </div>
        </div>`;
      }).join('')
      : '<span style="color:var(--text-muted)">暂无数据（请先配置 STRM 引擎）</span>';
  }

  function _refreshEngineRow(idx) {
  const container = document.getElementById(`tag-container-${idx}`);
  if (!container) return;
  const row = OpenListState.strmEngines[idx];
  if (!row || !row.monitored_paths || !row.monitored_paths.length) {
    container.innerHTML = '<span style="color:var(--text-muted);font-size:calc(var(--font-base) - 1px)">选择引擎后自动填充</span>';
    return;
  }
  container.innerHTML = row.monitored_paths.map((p, pi) => 
    `<span class="tag">${esc(p)}<button class="tag-remove" data-row="${idx}" data-pi="${pi}" title="删除">×</button></span>`
  ).join('');
}

function _bindEngineSelectEvents() {
  document.querySelectorAll('.engine-select').forEach(sel => {
    // 避免重复绑定
    if (sel.dataset.bound) return;
    sel.dataset.bound = '1';
    sel.addEventListener('change', async () => {
      const idx = parseInt(sel.dataset.row);
      const mountPath = sel.value;
      if (!mountPath) return;
      const eng = OpenListState.availableEngines.find(e => e.mount_path === mountPath);
      if (eng) {
        OpenListState.strmEngines[idx] = { engine: mountPath, monitored_paths: eng.paths || [] };
        _refreshEngineRow(idx);
        _syncRefreshPathsFromPreviewEngines();
      } else {
        try {
          const resp = await api(`/api/openlist/monitored-paths?engine=${encodeURIComponent(mountPath)}`);
          if (resp.success) {
            OpenListState.strmEngines[idx] = { engine: mountPath, monitored_paths: resp.paths || [] };
            _refreshEngineRow(idx);
            _syncRefreshPathsFromPreviewEngines();
          }
        } catch (e) { }
      }
    });
  });
}

function _refreshEngineTable() {
  const wrap = document.getElementById('strm-engine-table-wrap');
  if (!wrap) return;
  let html = '<table class="strm-engine-table"><thead><tr><th style="width:35%">STRM 引擎入口</th><th>监控目录</th><th style="width:60px">操作</th></tr></thead><tbody>';
  OpenListState.strmEngines.forEach((row, idx) => {
    let engineOptions = '<option value="">选择引擎...</option>';
    const selectedEngines = new Set(OpenListState.strmEngines.map((r, ri) => ri === idx ? '' : r.engine).filter(Boolean));
    OpenListState.availableEngines.forEach(eng => {
      const selected = row.engine === eng.mount_path ? ' selected' : '';
      const disabled = !selected && selectedEngines.has(eng.mount_path) ? ' disabled' : '';
      engineOptions += `<option value="${esc(eng.mount_path)}"${selected}${disabled}>${esc(eng.mount_path)}</option>`;
    });
    const disabledAttr = !OpenListState.configured ? ' disabled' : '';
    let tagsHtml = '';
    if (row.monitored_paths && row.monitored_paths.length) {
      row.monitored_paths.forEach((p, pi) => {
        tagsHtml += `<span class="tag">${esc(p)}<button class="tag-remove" data-row="${idx}" data-pi="${pi}" title="删除">×</button></span>`;
      });
    }
    // 删除死变量 deleteDisabled，直接在模板中渲染
    html += `<tr data-row-idx="${idx}">
      <td><select class="engine-select" data-row="${idx}"${disabledAttr}>${engineOptions}</select></td>
      <td><div class="tag-container" data-row="${idx}" id="tag-container-${idx}">${tagsHtml || '<span style="color:var(--text-muted);font-size:calc(var(--font-base) - 1px)">选择引擎后自动填充</span>'}</div></td>
      <td style="text-align:center"><button class="table-btn danger" data-delete-row="${idx}" title="删除此行">删除</button></td>
    </tr>`;
  });
  html += '</tbody></table>';
  const addDisabled = !OpenListState.configured ? ' disabled' : '';
  html += `<div class="table-actions"><button class="table-btn primary" id="add-engine-row"${addDisabled}>+ 添加行</button></div>`;
  wrap.innerHTML = '<div class="strm-engine-wrap"><span class="monitored-paths-help">' + _olHelpIcon('monitored_paths') + '</span>' + html + '</div>';

  _bindEngineSelectEvents();
  if (!wrap._delegatedTagRemoveListener) {
    wrap._delegatedTagRemoveListener = (e) => {
      const btn = e.target.closest('.tag-remove');
      if (!btn || !wrap.contains(btn)) return;
      const ri = parseInt(btn.dataset.row);
      const pi = parseInt(btn.dataset.pi);
      if (OpenListState.strmEngines[ri] && OpenListState.strmEngines[ri].monitored_paths) {
        OpenListState.strmEngines[ri].monitored_paths.splice(pi, 1);
        _refreshEngineRow(ri);
        _syncRefreshPathsFromPreviewEngines();
      }
    };
    wrap.addEventListener('click', wrap._delegatedTagRemoveListener);
  }
  wrap.querySelectorAll('[data-delete-row]').forEach(btn => {
    btn.addEventListener('click', () => {
      const idx = parseInt(btn.dataset.deleteRow);
      if (OpenListState.strmEngines.length < 1) return;
      OpenListState.strmEngines.splice(idx, 1);
      _refreshEngineTable();
      _syncRefreshPathsFromPreviewEngines();
    });
  });
  document.getElementById('add-engine-row')?.addEventListener('click', () => {
    OpenListState.strmEngines.push({ engine: '', monitored_paths: [] });
    _refreshEngineTable();
    _syncRefreshPathsFromPreviewEngines();
  });
}

export async function _checkApiStatus() {
  const dot = document.getElementById('api-status-dot');
  const text = document.getElementById('api-status-text');
  if (!dot || !text) return;
  dot.className = 'api-status-dot checking';
  text.textContent = 'OpenList 检查中';

  try {
    const st = await api('/api/openlist/status');
    OpenListState.configured = st.status === 'configured';
  } catch (e) { /* 保留上次配置状态 */ }
  _updateApiStatusDot();

  try {
    const resp = await api('/api/openlist/ping');
    OpenListState.apiStatus = resp.status || 'offline';
    if (resp.status === 'unconfigured') {
      OpenListState.apiStatus = 'offline';
    }
    _updateApiStatusDot();
  } catch (e) {
    OpenListState.apiStatus = 'offline';
    _updateApiStatusDot();
  }
}

function _olStatusText(configured, status) {
  const cfgOk = !!configured;
  if (status === 'online') return cfgOk ? 'OpenList 已连接' : 'OpenList 已连接（未保存配置）';
  if (status === 'offline') return cfgOk ? 'OpenList 已配置（离线）' : 'OpenList 未配置';
  if (status === 'rate_limited') return cfgOk ? 'OpenList 请求受限 (429)' : 'OpenList 请求受限 (429)';
  if (status === 'auth_failed_password') return cfgOk ? 'OpenList 密码错误' : 'OpenList 密码错误（未保存）';
  if (status === 'auth_failed_2fa') return cfgOk ? 'OpenList 2FA 错误' : 'OpenList 2FA 错误（未保存）';
  if (status === 'auth_failed') return cfgOk ? 'OpenList 认证失败' : 'OpenList 认证失败（未保存）';
  return cfgOk ? 'OpenList 已配置' : 'OpenList 未配置';
}

function _olStatusClass(configured, status) {
  // 将子状态映射到已知 CSS 类
  const cssStatus = ({ auth_failed_password: 'auth_failed', auth_failed_2fa: 'auth_failed', rate_limited: 'auth_failed' })[status] || status;
  return configured ? `api-status-dot ${cssStatus}` : 'api-status-dot unconfigured';
}

export function _updateApiStatusDot() {
  const configured = OpenListState.configured;
  const status = OpenListState.apiStatus;
  const dot = document.getElementById('ol-status-dot');
  const text = document.getElementById('ol-status-text');
  if (dot) dot.className = _olStatusClass(configured, status === 'checking' ? 'offline' : status);
  if (text) text.textContent = _olStatusText(configured, status === 'checking' ? 'offline' : status);
  const globalDot = document.getElementById('api-status-dot');
  const globalText = document.getElementById('api-status-text');
  if (globalDot) globalDot.className = _olStatusClass(configured, status === 'checking' ? 'offline' : status);
  if (globalText) globalText.textContent = _olStatusText(configured, status === 'checking' ? 'offline' : status);
}
