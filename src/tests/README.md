# 测试脚本说明

本目录包含 `openlist_strm_bridge` 项目的全部测试文件。使用 `python -m pytest src/tests --collect-only -q` 获取当前实时收集的测试数量（测试数量以 collect-only 实时输出为准）。`conftest.py` 的 `collect_ignore_glob` 排除部分独立手动脚本（需要外部服务运行）。若干辅助工具脚本。

所有测试均从**项目根目录**运行（`src/tests/conftest.py` 负责 `src/` 路径注入），推荐命令：

```bash
python -m pytest src/tests/ -v
```

## 测试文件清单（按功能分组）

### 核心引擎

| 文件 | 说明 |
|------|------|
| `test_app_service_core.py` | 核心同步引擎 `AppService` 主流程与状态机测试（含 **M6** `_extract_save_local_mode` 对 `SaveLocalMode: null` 返回 `""` 的守卫测试、**R23 回归** `TestRestoreBFromAAfterViolation` 验证 B 区指纹违规后 A 重构恢复） |
| `test_sync_service.py` | A→B 同步服务（`initial_scan_a` 批量索引、`scan_a_to_b_full_sync` 双模式同步、`_bulk_upsert_b` FTS 孤儿行处理）测试。**R24 回归**：`test_full_sync_concurrent_no_typeerror` 验证批量同步并发安全性。 |
| `test_a_snapshot_skip.py` | A 区 size+mtime 内容读跳检契约测试：以计数器替换 `read_strm_webdav_path` 断言"正文 open 未发生"（测行为而非结果凑对），覆盖快照命中复用、size/mtime 变化重读、`parse_version` 漂移使快照行整体失效、快照缺失或损坏 fail-open、bulk 模式快照写入位于 bulk 事务提交之后等契约；孤儿快照行仅经全量审计 prune 删除路径清理；夹具用契约 DDL 自建 `a_strm_snapshot` 表，红灯落在行为断言而非脚手架错误 |
| `test_area_watchers.py` | A/B/C 三区文件系统监视器事件处理测试 |
| `test_area_watchers_debounce.py` | 监视器去抖与告警聚合（C1/C3）：小时间窗（0.2s）+ `Event.wait` 必达断言锁定同路径去抖；`debounce_seconds=0` 保持原即时派发语义；F-6 逐字节断言锁定基类迁移后的 A/B 区告警文案 |
| `test_webui_refresh_media.py` | 媒体刷新逻辑（差异检测、逐条同步、LIKE 转义、计数回传）测试（含 `TestLastVerifiedAtWiring`）。**全局日志级别回归**：`test_refresh_logger_uses_global_log_level` 断言 `_do_media_refresh` 读取 `AppConfig.log.level` 并传给 `_make_refresh_logger`，而非已废弃的刷新专用级别 |
| `test_refresh_service.py` | 周期性 WebDAV 刷新服务测试（含 `TestFullAuditTouchVerified`、`TestRunFullAuditNow`） |
| `test_refresh_service_helpers.py` | RefreshService 辅助委托、路径分析日志、WebDAV 刷新与 update 模式清理测试。 |
| `test_bootstrap.py` | 启动路径工具（`ensure_base_dir_first`、`load_local_module`）测试 |
| `test_log_issues_simulation.py` | 八类真实日志问题的沙盒实验与修复回归（SQLite 锁竞争、padding 路径碰撞、B 区血统清理健康度、B 区事件洪泛、重复实例隔离、字幕路由、WebDAV 假阴性、Unicode 路径） |
| `test_lineage_snapshot_production.py` | 真实 `AppService` 的 mapping-scoped lineage snapshot 验收：覆盖未变更复用、内容修改、删除、同 mapping 重命名、跨 mapping/非法目录移动、无指纹、同/跨 mapping 重复指纹、A 源缺失 boundary 放行、同名不同根、mapping/lineage 版本变化、snapshot 缺失或损坏、stat/DB 写异常及扫描期间文件修改。**B 区启动核对缓存与批量写入**：本文件同时覆盖 B 区历史核对的内存预载缓存（A 记录重复键首行优先、boundary 三种索引 updated_at 最新优先、按 mapping_id 隔离）与批量 snapshot 写入（单行失败不阻断其余行、新增记录路径不缓冲直写）。 |
| `test_app_service_helpers.py` | AppService 辅助方法：锁工厂、mapping 路径解析、WebDAV 辅助、引擎内部标记与日志去重。 |
| `test_app_service_roots.py` | 保护根目录同步、移除根目录扫描与当前根目录快照持久化。 |
| `test_app_service_lifecycle.py` | `AppService` 生命周期编排：配置未就绪 fail-safe、启动阶段顺序、`start_watchers()` 的 A/B/C schedule（mock Observer，不启动真实 watchdog）、`stop()` 的定时器取消与 observer 停止、重复 stop 以及当前未提供的生命周期保证记录。**D1 回归**：WebUI 真实保存体经 DB 往返后引擎门禁 ready（`TestWebUiSavedMappingReachesReady`）。**start() 不变式**：成功启动后 `_running` 置真（`TestStartMarksRunningWhenReady`）；启动期日志可格式化（`TestStartupLogFormatting`）。 |
| `test_multi_mapping_production_acceptance.py` | 多 mapping 生产验收与跨根隔离测试。 |
| `test_snapshot_reuse_parallel.py` | boundary 快照复用并行等价（红线先行）：`_snapshot_reuse_check_parallel` 与逐行串行判定等价（cache 未构建 / `force_full` 时 helper 零调用）、`snapshots_loaded` 整 mapping 预载成功后 dict miss 直接 False 免 DB 回退（假 miss 只触发全量校验重写，绝不假命中）、`_snapshot_reuses_valid_lineage` 异常分支降 DEBUG 防并行 WARNING 刷屏 |
| `test_boundary_lazy_diff.py` | boundary 二次盘扫惰性差集化：`_load_b_db_records` 返回 None 时零磁盘读直接 return、差集为空时零内容读（`_scan_b_disk` 不被调用）、差集 delta 与全读 delta 逐项一致；枚举失败经 `_enumerate_b_strm_paths` 返回 False 回退全读路径 |

### 数据库 / FTS

| 文件 | 说明 |
|------|------|
| `test_integration.py` | 数据库重构与核心流程集成测试（含 A/B/C 区 FTS 完整性回归） |
| `test_database_bulk.py` | bulk_connection 批量写入、只读 getter 读锁与并发数据库行为测试（含 `TestLastVerifiedAtColumn`）。**bulk 生命周期边界**：`TestBulkConnection`（`test_yields_connection_and_commits` 事务提交、`test_rollback_on_exception` 异常回滚、`test_connection_closed_after_exit` 连接关闭、`test_bypasses_rw_lock` 绕过读写锁）、`TestBatchOver900Records`（>900/1500 记录跨 SQLite 变量数边界批次）、`TestReadonlyGettersReadLock`（只读 getter 读锁） |
| `test_fts5_search.py` | FTS5 全文检索查询与匹配测试（含 simple 分词器加载、版本可读、`黑暗`/`暗黑` 按词分词语义断言） |
| `test_webui_fts5_escape_and_tmdb_search.py` | FTS5 查询转义函数（`_escape_fts5_query`）与 TMDB 搜索路由测试（含 `进击的巨人[限制级]`、`电影：测试*`、`Spy×Family` 真实媒体名转义） |
| `test_fts_orphan_cleanup.py` | FTS 孤儿行清理与一致性测试（含 **H1 回归**：`sync()` 同时含电影+剧集时两类 FTS 行均保留、电影搜索不失效） |
| `test_tmdb_watchlist_db.py` | TMDB 待看列表 DB 单元测试：匹配状态 CRUD、季数缓存、全量同步 upsert/FTS/独立事务、TV detail 填充、操作日志、webui_config CRUD、加密迁移 |

### 配置 / 安全

| 文件 | 说明 |
|------|------|
| `test_config.py` | 配置模块单元测试：ABMapping、mapping_version、AppConfig.from_file、update_from_db、load_strm_storage_from_api、migrate_config_to_db、配置 fail-closed。**D1 回归**：`update_from_db` 补齐缺失 `mapping_id`（`test_a_b_mappings_backfills_missing_mapping_id` 等）；**D2 回归**：`from_file` 初始化 `a_b_mappings` / `engines_initialized`（`test_from_file_initializes_mapping_fields`）。**日志级别映射**：`test_log_level_db_key_maps_to_log_config_level` 断言 DB 键 `log_level` 映射到 `LogConfig.level`（全局日志），已废除的 `RefreshConfig.log_level` 不被误写 |
| `test_webui_password_security.py` | 管理员密码 PBKDF2 哈希与校验安全测试 |
| `test_webui_auth_security.py` | 认证安全测试：登录限流字典逻辑（5 次失败后限流）、密码哈希格式（salt$iterations$hash 三段式，iterations=600000）、首启密码生成与哈希验证往返、损坏密码格式提示（含 `reset_admin.py` 重置指令） |
| `test_secret_manager.py` | 密钥/凭据安全管理测试 |
| `test_migrate_encryption.py` | 加密方案迁移测试 |
| `test_webui_integration_security.py` | 跨模块安全边界与鉴权测试 |
| `test_webui_strm_engines_validation.py` | STRM 引擎配置校验测试 |

### 工具 / 媒体

| 文件 | 说明 |
|------|------|
| `test_utils.py` | 通用工具函数测试（含 `TestMoveFile` EXDEV 跨设备回退测试、`quarantine_file` 时间戳重名、WebDAV 路径规范化、**M7** `parse_strm_content` 对 `http://host?sign=xxx` 空 path 返回 `None` 的守卫测试） |
| `test_encoding_utils.py` | 字幕编码转 UTF-8 工具测试（空字节、UTF-8 带/不带 BOM、GB18030→UTF-8、Big5→UTF-8、UTF-16 LE/BE 带 BOM、UTF-16 LE 无 BOM、不可识别编码 fail-safe、真实字幕样本往返） |
| `test_media_renamer.py` | 媒体重命名与季/集号提取测试 |
| `test_subtitle_handler.py` | 字幕同步与规范化测试（含 `TestSubtitleEncodingConversion`） |
| `test_subtitle_multi_bug_repro.py` | 番剧多字幕场景 NameError 回归测试 |
| `test_boundary_conditions.py` | 边界条件与异常输入健壮性测试 |
| `test_error_translator.py` | 错误码到用户可读信息的翻译测试 |
| `test_subset_font.py` | 字体子集化脚本单元测试：参数解析、Unicode 集合运算、网页字符扫描、缺字来源区分、CSS 一致性校验、icon-preview 与 icons.js 一致性（**icon-preview 副标题图标总数与 `ICONS` 键数一致**（`TestIconPreviewParity`））。**字体覆盖分工（三个不同边界，勿合并描述）**：`TestDistFontCoverage::test_dist_font_covers_scanned_codepoints` 验证发布字体（`dist/assets/NotoSansSC-Subset-*.woff2`）cmap 覆盖当前网页全部扫描字符，且区分「源字体缺字（WARNING → skip）」与「子集字体丢字（ERROR → fail）」；`TestVerifyCssDeclaration::test_real_css_matches_shipped_font` 验证源码 `main.css` 的 `unicode-range` 声明不超过源码子集字体实际 cmap；`TestUnicodeRangeCoverage::test_scanned_codepoints_within_css_range` 验证后端 Python 扫描到的 CJK 码点落在 CSS 声明的 `unicode-range` 内。三者不是「CSS 与 dist 字体 cmap 完全相等」的单一测试。**依赖**：需要 `fonttools[woff2]`（见 `requirements-dev.txt`），无法读取字体 cmap 时相关用例跳过。**已知 skip**：`TestDistFontCoverage` 当前唯一跳过项为 U+25BE `▾`（十进制 9662），该字符在源字体 `NotoSansSC-VF.ttf` 中缺失，`classify_missing()` 归入 `from_source`，属于源字体 fallback 风险，不是子集化失败。`python -m pytest src/tests/test_subset_font.py -q -rs` 预期结果为 `144 passed, 1 skipped`（仅 U+25BE 源字体缺失）。 |

### API 客户端

| 文件 | 说明 |
|------|------|
| `test_webui_openlist_hotreload.py` | OpenList 热重载/配置刷新测试：`_reinit_admin_client` 保留旧客户端（登录失败/构造异常时不替换）、`_hot_reload_openlist_config` 异常吞咽与 WebDAV 变更触发重连。**日志热更新行为**：`test_hot_reload_log_changed_reinitializes_logging` 断言 `update_from_db` 改变 `cfg.log.level` 后 `setup_logging` 以新 `level`/`log_file`/`max_size_mb`/`backup_count` 被调用；`test_hot_reload_log_unchanged_skips_setup_logging` 断言日志配置未变时不调用。函数内为局部 `from logger_setup import setup_logging`，patch 目标为源模块 `logger_setup.setup_logging`（与 patch `webdav_client.OpenListAdminClient` 同理） |
| `test_webdav_client.py` | WebDAV 协议客户端测试（含 `_check_exists_cache` 容量淘汰验证、**M8** TOTP 无 padding base64 密钥解码回归） |
| `test_tmdb_client.py` | TMDB API v3 客户端测试 |
| `test_openlist_login_shared.py` | OpenList 登录错误消息解析（`parse_login_error`）测试 |
| `test_main_entry.py` | `main.py` 入口参数拒绝路径测试（禁止使用 `--webui-only` / `--webui`）。 |

### WebUI

| 文件 | 说明 |
|------|------|
| `test_webui_http.py` | WebUI HTTP 服务器与路由分发测试（含 `TestAreaDetailKindParameter`、`TestAreaDetailCZonePagination`、`TestAreaDetailSingleMappingMid`、`TestTMDBWatchlistMatchOverrideConsistency`、`TestManualFullIndexAuditAPI`）。**D2 回归**：全新安装 `/api/config` 不抛异常（`TestConfigApiFreshInstall`）；**D3 回归**：fail-safe 时 `start_main` 返回失败且不置存活态（`TestStartMainFailSafe`）。**安全边界**：`TestSecurity`（公网 IP 拒绝、5 次失败限流 429）、`TestSessionIPBinding`（会话 IP 绑定拒绝异 IP / 放行原 IP）、`TestConfigApiUnifiedSession`（token 滑动过期、空 stored_ip 兼容、无效 token 拒绝）、`TestMainStartHidesExceptionDetail`（后台异常返回通用消息）。**第 23 轮回归**（`TestRound13Regressions`）：**M3** `_MEDIA_NAME_SQL` 别名目录（`/movies/` 等）不再坍缩「未分类」、**M4** 改密后旧 token 立即失效、**M5** `/api/admin/status` 带无效 token 返回 401 / 无 token 保持 200。**日志顺序与下载**：`TestLogsOrdering` 锁定 `/api/tmdb/logs` 最近 N 条截取 + 旧到新展示（`count == len(logs)`、无分页字段、同时间戳不断言相对顺序）与 `/api/logs` 主程序日志文件原序返回；`TestLogsDownloadFilename` 锁定 `webui_operations.log` 下载名、下载内容不受页面 limit 截断、主程序日志仍为 `strm_bridge.log`。**OpenList 数字校验**：`TestOpenListNumericValidation` 覆盖 8 个数字字段 ×（合法整数/负数/中文/英文/小数/科学计数法/空字符串/bool/None），断言非法值 HTTP 400 且 `set_config` 完全未调用（零写入）、合法零值（`refresh_full_audit_interval_days=0`、`behavior_sync_on_startup_wait=0`）与空字段可保存。 |
| `test_webui_help_texts.py` | WebUI 帮助文案系统测试：`createField` 输出 `.field-helper-text`、`_openlistHelpTexts` 键完整性、`log_file` 已删除、`refresh_*` 含「即时生效」、TMDB 阈值字段 helpIcon、孤儿键标注、死字段「未接入匹配逻辑」标注 |
| `test_webui_source_contracts.py` | 前端源码契约回归测试：未定义变量（`mappingIdParam`/`deleteDisabled`）、死参数（`mapping_id`）、CSV 公式注入安全、`_do_bg_sync` 预检查、dialog 断言正则、配置「未接入」标注、畸形请求不计数、交付文档无行号、`captureRenderGuard()` 渲染护栏、`parseHash` 畸形编码容错。**数字零值保护**：`test_openlist_field_value_preserves_zero` / `test_openlist_no_value_or_default_for_numeric_fields` 锁定 `_olFieldValue` 严格空字符串判断（禁止 `value || default` 吞掉合法零值）。**详情返回契约**：`test_area_detail_link_preserves_list_state` / `test_area_back_link_preserves_list_state` / `test_area_page_size_whitelist_rejects_invalid` 锁定 `{kind, q, sort, order, page, page_size}` 状态保留与 `page_size` 50/100/200 白名单；`test_area_search_and_kind_switch_reset_page` 锁定搜索/分类切换主动移除旧 page（P2-1/P2-2）。**表单属性透传**：`test_create_field_passes_numeric_attributes` 锁定 `createField` 透传 min/max/step/inputMode。**TMDB 比例字段**：`test_tmdb_ratio_fields_min_0_01` 锁定 `fuzzy_threshold` / `anime_min_ep_ratio` / `anime_min_season_ratio` 的 `min:'0.01'`（与后端 `(0,1]` 契约一致）且保留 `step:'0.01'`。 |
| `test_webui_dashboard_phase_contract.py` | dashboard.js 相位契约静态断言（零 node/零 DOM）：`_lastMainPhase` 基线门终态集合含 `stopping`、`updateMainStatus` 有独立 stopping 渲染分支（文案「正在停止主程序...」）、stopping 分支位于 ready 分支与最终 else 之间不被遮蔽 |
| `test_webui_entry_behavior.py` | WebUI 入口行为测试（`server.py` `main()` 的普通交互模式与 `BRIDGE_HEADLESS=1` 无头模式）：覆盖普通交互菜单（选 1 启动 Bridge、默认仅 WebUI）、无头自动启动 Bridge 并跳过 stdin 静默等待、交互循环 `q`/`quit` 退出、EOFError 不崩溃、KeyboardInterrupt 可控退出、退出时清理子程序与服务器、配置缺失 `sys.exit(1)`、启动失败 `sys.exit(1)`。**无头失败入口**：`test_headless_start_main_failure_does_not_escape` 验证 `start_main` 返回失败结果时异常不逃逸并进入清理路径（该测试直接断言清理路径，不断言日志内容；不置存活态的 fail-safe 见 `test_webui_http.py::TestStartMainFailSafe`）。验证 `q` 退出用例真实断言未启动 Bridge（`start_main.call_count == 0`）。 |
| `test_call_coverage.py` | 启动链调用覆盖率测试 |
| `test_webui_logging_system.py` | WebUI 操作日志表、日志读取接口与轮转产物测试 |
| `test_logger_setup.py` | logger_setup 模块单元测试：handler 装配、重复初始化（热更新）、回退路径、级别过滤、启动分隔标记、临时目录清理（**窄编码控制台下无法编码字符不丢日志、且不改写流的全局 errors 策略**（`TestConsoleEncodingFallback`））。 |
| `test_webui_concurrency.py` | 并发请求与锁竞争测试 |
| `test_webui_auth_whitelist_contract.py` | 白名单免 token 契约的 HTTP 级回归锚定（自带 `shared_server` 夹具）：`/api/config` 与 `/api/webui/config/ui` **GET 免 token、POST 必须认证**（4 条）、`/api/admin/status` 双语义（无 token 200 / 带无效 token 401，2 条）、静态资产免 token放行、受保护路径无 token 拒绝 |
| `test_webui_marker_contract.py` | 回归入口接线契约：每个 import WebUI 符号的测试文件都带 `test_webui_` 前缀（否则不进 `-m webui`）、`conftest` marker 名与 `run_webui_regression.bat` 字面量一致、`.bat` 以 cmd 侧枚举 JS 用例文件（显式路径传参）并保留 `-m webui`、`.bat` 含零用例判红闸、`node:test` 套件非空、`collect_ignore_glob` 条目仍存在 |
| `test_webui_dist_asset_contracts.py` | 构建产物与二进制资产存在性契约：`dist/assets` 必须存在 vite `manualChunks` 的 `core-*.js` 分组；`publicDir` 资产（favicon 等）必须原样复制进 `dist` 且不加哈希；FTS5 `simple` 分词 DLL 必须随仓库分发（缺失时中文搜索静默退化） |
| `test_webui_dist_freshness.py` | dist 新鲜度与引用完整性护栏（`-m webui` 成员）：`index.html` 引用的每个产物（`.js`/`.css`/`.ico` 等，含嵌套子目录）必须真实存在于磁盘（捕获悬空引用）；`index-` entry chunk 必须存在；源码 mtime 不得晚于 dist chunk |
| `test_webui_launcher_contracts.py` | 启动器契约：`后台带Bridge启动webui.vbs` 保留 UTF-8 BOM、设置 `BRIDGE_HEADLESS` 且与 `server.py` 一致、回退端口与 `config.toml` 默认值一致、含 Python 版本检查；两个 `.bat` 均含版本检查且依赖探测为 `requirements.txt` 子集 |
| `test_webui_port_default_contract.py` | 默认端口单一来源契约：`AppConfig` dataclass 默认值、`config.toml.example` 解析结果、仓库内 `config.toml`（gitignore 管理，缺席时 skip）、`.vbs` 回退端口、`routes.py` 字面量回退五处必须一致 |
| `test_webui_theme_contracts.py` | 主题三联动契约：`<select>` 下拉选项与 `colormap` / `fsmap` 状态机映射一致、`system` 下拉为 material/fluent、`theme.js` 的 localStorage 键与 HTML 选项对称；JS 引用的 CSS 变量均已在 `main.css` 定义 |
| `test_webui_gapfill.py` | WebUI 路由缺口回归：配置 CDN 主机归一化与 `/api/redirect-to-configured-cdn`、Google Fonts CSS/字体代理（含超限拒绝）、`/api/records` 分页入参钳制与过滤、后台重启 WebUI、`do_match_refresh` 阈值传递与异常脱敏 |
| `test_webui_tmdb_watchlist_pure.py` | TMDB 待看纯函数测试：`all_titles()` 聚合 title/original_title/aliases/titles_list 并去重、剔除空白、导出 CSV 带 UTF-8 BOM 且表头固定、`user_movies` 混合结果按标题与 id 稳定排序 |
| `test_module_gapfill.py` | 后端模块覆盖缺口补测（无前缀属**有意**：覆盖 `reset_admin.py`、`logger_setup.EncodingSafeStreamHandler`、`secret_manager`、`watchlist_match`、`tmdb_watchlist_db` 与 DDL 漂移检测，不属 WebUI 专项，故不进 `-m webui`）。这是显式分层契约：引擎侧用例由全量 pytest 收集执行，WebUI 专项回归（`run_webui_regression.bat`）只收集 `test_webui_*` / e2e / onboarding，勿为进 `-m webui` 而改前缀或打标 |

### 匹配 / 监视

| 文件 | 说明 |
|------|------|
| `test_watchlist_match.py` | TMDB 想看列表与本地收藏匹配逻辑测试（含 `TestExtractSeasonFromLocalPath`） |
| `test_watchlist_match_state.py` | 匹配状态持久化与状态机测试 |

### 端到端

| 文件 | 说明 |
|------|------|
| `test_index_metadata_api.py` | 索引元数据 API 测试（含 `TestManualFullIndexAudit`，验证 `last_verified_at` 推进） |
| `test_b_orphan_observation.py` | B 区孤儿只读观测与零组信号日志等级：dashboard 只读字段 `b_orphan_count`（R-6：孤儿 COUNT 锁外计算 + ≥60s TTL 缓存 + 异常回退 None，不进 `_state_lock`）、NULL 三值逻辑防御（两侧 `webdav_path IS NOT NULL`，防 NOT IN 恒 UNKNOWN 致计数静默归零）、启动零重复指纹组的信号行 DEBUG → INFO（使 X-8 能区分「清扫 0 组」与「清扫未运行」；启动不清理孤儿为有意设计） |
| `test_webui_multi_mapping_partition.py` | 多 mapping 分区测试 |
| `test_e2e_full_flow.py` | 完整业务流程端到端测试（登录→配置→A/B 区→状态校验）。**##26 七步链路**：`test_complete_seven_step_onboarding` 覆盖七步正向 HTTP 全链路（登录→TMDb→OpenList→启动→分区→待看同步→收录检测）；`TestSevenStepFailureReasons` 覆盖每步的失败原因与成功条件（未授权、mapping 校验、`not_configured`、`fail_safe_active`、OpenList 登录失败、非法分区、待看开关关闭）。**HTTP→handler 接线回归**：`TestTmdbConfigPersistence::test_tmdb_config_reinitializes_client` 验证 TMDB 配置保存后 `_handler_reinit_tmdb` 确实重建客户端；`TestConfigurationLinkage::test_mapping_id_autogenerated_on_config_save` 验证 HTTP POST → 真实 SQLite → `update_from_db` → mapping_id 自动生成 → 引擎门禁 ready 的全链路（逻辑主覆盖见 `test_config` / `test_app_service_lifecycle`）；`TestFailureScenarios::test_openlist_config_triggers_storage_reload` 验证 OpenList 保存后 `_hot_reload_openlist_config` 被触发（逻辑覆盖见 `test_webui_openlist_hotreload`）。引擎侧 `start()` 置 `_running` 的不变式由 `test_app_service_lifecycle.py` 守卫。异步启动契约的验证分工：本文件 `TestSuccessfulFlow` 的启动用例用替身（`MagicMock` + 可 JSON 序列化的 `get_state_summary` 建模相位推进），而 `TestSevenStepFailureReasons` 的登录失败用例走**真实 `AppService`**（不 patch `app_service.AppService`），以真实 `set_phase` / `get_state_summary` 全链验证 `fail_safe` 终态与 `error` 透传。 |
| `test_onboarding_e2e.py` | 新手引导流程端到端测试。覆盖引导步骤单步跟踪（`view_ab` / `tmdb_refresh` / `tmdb_match` 等）、`config/validate` 预检（OpenList 未配置 / 已配置但离线 / TMDB 未配置警告）、完整引导旅程与整体完成/复位、以及配置联动（OpenList/TMDB/主程序状态变更实时反映在 config/status）。**注意**：七步全链路不在本文件，见 `test_e2e_full_flow.py`。 |

### 性能基准门禁

| 文件 | 说明 |
|------|------|
| `perf/test_benchmark_lineage.py` | 基准正确性门禁：compute_digest 稳定性、build_fixture 结构、baseline/optimized 等价性（不包含性能阈值断言） |
| `perf/test_benchmark_pipeline.py` | 真实 AppService/Database 启动流水线正确性门禁：临时目录隔离、零网络、终态 digest 稳定性与 CLI 契约 |
| `perf/test_benchmark_candidates.py` | 五组数据库候选方案正确性门禁：等价性、事务回滚隔离、mapping 边界与参数分片契约 |
| `perf/test_benchmark_fake_lifecycle.py` | Runner B 门禁：Fake OpenList 生命周期与启动协议冻结契约（替身只计白名单调用、未白名单方法直接 trap；Runner B 各启动场景的 happy/无 storage/启动登录失败/加载异常契约与 CLI） |
| `perf/test_benchmark_incremental.py` | Runner C 门禁：真实增量流水线与终态状态机（delta 计数取整规则与下限、增量流水线全状态检查、零网络与 B 区三态清理、零 Admin 替身对 `check_exists` 三态的 trap 与放行） |
| `perf/test_benchmark_real_integration.py` | Runner D 门禁：真实 OpenList 集成。**仅 `test_real_integration_runs_only_when_opted_in` 一条被无条件 `@pytest.mark.skip` 跳过**（该装饰器无 `condition`，函数体内的 `OPENLIST_REAL_INTEGRATION_TEST` 二次判断不可达，属既有实现，勿据本行推断整文件跳过）；其余 5 条默认执行：凭据脱敏（密码 / URL host / storage 摘要中的挂载路径）与 CLI 必须显式提供凭据、参数解析 |

### 独立手动脚本（非 pytest 测试）

以下脚本需外部服务运行，不纳入 pytest 收集（已在 `conftest.py` 的 `collect_ignore_glob` 中排除）：

| 文件 | 说明 | 依赖 |
|------|------|------|
| `test_openlist_admin_api.py` | OpenList Admin API 手动烟雾测试 | 运行中的 OpenList 服务器 |
| `test_tmdb_api.py` | TMDB API CLI/Flask 端点测试 | 有效的 TMDB access_token |
| `test_real_server.py` | 真实服务器安全验证探测 | 运行中的 WebUI（固定默认端口 8579，见 `config.toml` `[webui].port`，脚本硬编码默认值） |
| `test_webui_standalone.py` | WebUI 在线集成测试 | 运行中的 WebUI（固定默认端口 8579，见 `config.toml` `[webui].port`，脚本硬编码默认值） |

## 运行测试

### 运行所有测试

```bash
python -m pytest src/tests/ -v
```

也可使用封装脚本（可选 `--cov` 生成覆盖率报告）：

```bash
src/tests/run_tests.bat
src/tests/run_tests.bat --cov
```

### 运行特定测试文件

```bash
python -m pytest src/tests/test_webui_refresh_media.py -v
```

### 运行特定测试类

```bash
python -m pytest src/tests/test_webui_refresh_media.py::TestSyncToBZone -v
```

### Windows PowerShell 5.1 针对性测试命令

以下命令用于计划验证阶段的针对性运行，均在项目根目录执行：

```powershell
# 日志相关测试
python -m pytest src/tests/test_webui_logging_system.py src/tests/test_tmdb_watchlist_db.py -q

# B 区历史核对优化核心保护集（预载缓存 + 批量写入 + 安全语义回归）
python -m pytest src/tests/test_lineage_snapshot_production.py src/tests/test_log_issues_simulation.py src/tests/test_app_service_core.py -q

# WebUI / OpenList 测试（含日志排序、数字校验、源码契约）
python -m pytest src/tests/test_webui_http.py src/tests/test_webui_source_contracts.py src/tests/test_webui_openlist_hotreload.py -q

# 字体子集 / dist 资源 / icon-preview 核验
python -m pytest src/tests/test_subset_font.py -q

# 收集全部测试数量（核对本 README 文件清单是否与实际一致）
python -m pytest src/tests --collect-only -q
```

### 运行特定测试方法

```bash
python -m pytest src/tests/test_webui_refresh_media.py::TestSyncToBZone::test_sync_counts_mixed_results -v
```

### 运行测试并生成覆盖率报告

```bash
python -m pytest src/tests/ --cov=src --cov-report=html
```

## WebUI 专项回归

WebUI 专项测试通过 `webui` marker 聚合（`conftest.py` 按文件名自动打标：`test_webui_*.py`、`test_e2e_full_flow.py`、`test_onboarding_e2e.py`），两个入口：

```bash
# Python 侧（pytest marker）
python -m pytest src/tests -m webui

# 一键双入口（JS node:test + pytest webui marker），仓库根执行
run_webui_regression.bat
```

前端纯逻辑模块（不依赖 DOM/网络）另有 Node 内置 `node:test` 零依赖用例，位于 `src/webui/tests/`（要求 Node >= 20.19，说明见 `src/webui/tests/README.md`）：

```bash
node.exe --test "src/webui/tests/*.test.mjs"
```

注意：上式引号 glob 的展开依赖 Node 版本——未展开且不报错时会静默零覆盖。需要机械保证零覆盖必红时，用上方 `run_webui_regression.bat`（cmd 侧枚举 + 零用例判红）。

`-m "not webui"` 可在跑全量时排除 WebUI 专项（含端到端启动服务器的慢用例）。

## 测试依赖

```bash
pip install -r src/tests/requirements-dev.txt
```

字体相关测试（`test_subset_font.py`）需要 `fonttools[woff2]>=4.50.0`，用于字体子集化、WOFF2 cmap 读取及发布字体覆盖测试。该依赖**仅用于开发/测试**，不是生产运行依赖。无法读取字体 cmap 时，相关用例会跳过而不是失败。

## 测试环境

- Python 3.11+
- SQLite（内置，WAL 模式）
- 多数测试无需外部服务（外部依赖已 mock）；端到端与真实服务器测试会启动本地 WebUI/引擎实例

## 测试策略

### Mock 策略

- `WebUIServer` 使用真实实例，但 mock 数据库和配置
- `Database` 在集成/FTS 测试中多使用真实临时 SQLite（`tempfile.TemporaryDirectory`），单元测试中可 mock
- `AppConfig` 使用 mock，提供最小化配置
- TMDB / OpenList / WebDAV 客户端使用 mock，避免真实网络调用

### 测试隔离

每个测试使用独立的 `tmp_path` / 临时目录，确保：

- 数据库文件隔离
- 配置文件隔离
- 日志文件隔离

### 测试数据

- 空数据库（0 条记录）或最小化数据集
- 最小化配置（仅必要字段）
- 固定测试密码（`1111`）——仅指 pytest 收集的测试。`test_webui_standalone.py` 为**手动在线集成脚本**（不在 pytest 收集范围），使用独立默认密码 `admin123`（可用命令行参数覆盖），二者互不影响

## 常见问题

### Q: 测试失败提示 "ModuleNotFoundError"

**A:** 从**项目根目录**运行测试（不要 `cd src`）：

```bash
python -m pytest src/tests/
```

### Q: 测试失败提示 "Port already in use"

**A:** 测试使用随机端口，通常不会冲突。如遇到，等待几秒后重试。

### Q: 测试运行缓慢

**A:** 端到端测试需要启动真实 WebUIServer，可能需要 10-20 秒。单元测试通常在 1 秒内完成。

### Q: 如何添加新测试

**A:**

1. 在 `src/tests/` 目录下创建新文件 `test_xxx.py`
2. 使用 `pytest` 标准语法编写测试
3. 如需 WebUIServer，参考 `test_e2e_full_flow.py` 的 fixture
4. 运行测试验证

## 测试覆盖率

- 核心同步引擎与数据库/FTS：高覆盖（含孤儿行、rowid 复用等回归）
- WebUI 路由：较高覆盖
- 端到端流程：覆盖主路径与关键失败分支

目标覆盖率：80%+

### 辅助工具（非测试、非 pytest 收集）

| 文件 | 说明 |
|------|------|
| `debug_console.py` | 调试控制台交互工具（数据库/区域状态检查） |
| `verify_login_flow.py` | 登录流程手动验证脚本 |
| `_test_helpers.py` | 测试共用辅助函数（被其他测试文件 import） |
| `perf/benchmark_lineage.py` | B 区血统核对性能基准 CLI（增量校验方案），非 pytest 测试，用法见文件头注释。**注意**：使用自建简化 schema，仅用于观察算法趋势，不作为生产性能结论；生产优化度量以真实启动日志「B 区历史记录核对完成」耗时为准 |
| `perf/benchmark_startup_pipeline.py` | 真实 `AppService` 启动流水线 benchmark CLI：临时 A/B/C 根目录、`FailFastAdmin` 零网络拦截与终态摘要导出 |
| `perf/benchmark_database_candidates.py` | 五组数据库候选方案的 benchmark-only 隔离对比 CLI，不修改生产 `Database`/`AppService` |
| `perf/instrumentation.py` | 性能埋点/计时工具（可选预留模块），当前各 benchmark 均不使用，详见 `src/tests/perf/README.md` 的插桩模块说明 |
| `perf/benchmark_fake_lifecycle.py` | Runner B 基准 CLI：Fake 生命周期基准与启动协议冻结契约验证（`test_benchmark_fake_lifecycle.py` 的被测对象），非 pytest 收集 |
| `perf/benchmark_incremental.py` | Runner C 基准 CLI：真实增量流水线与终态状态机验证（`test_benchmark_incremental.py` 的被测对象），非 pytest 收集 |
| `perf/benchmark_real_integration.py` | Runner D 基准 CLI：真实 OpenList 集成基准，需显式 opt-in 且输出脱敏记录（`test_benchmark_real_integration.py` 的被测对象），非 pytest 收集 |
| `perf/OPTIMIZATION_PLAN.md` | 批量 I/O 性能优化方案（含 benchmarking 方法、当前瓶颈与优化方向），非 pytest 测试 |
| `perf/README.md` | 性能基准工具集总说明（各 Runner 职责、运行方式与插桩模块），非测试 |
| `webui_fixtures.py` | WebUI 回归共享夹具：空闲端口、最小 `AppConfig`/`Database` mock、真实服务器起停（供测试文件 import），非测试 |

## 日志问题模拟测试（`test_log_issues_simulation.py`）

该测试专门针对 `strm_bridge.log` 中出现的**八类**真实问题进行模拟与审核，运行机制与一般单元测试不同，需注意目录与日志的留存策略：

| 目录 / 文件 | 用途 | 测试后处理 |
|------|------|------|
| `src/tests/strm.test.A/` | 模拟生成的源文件（~100 个 STRM / 图片 / 字幕 / 畸形文件，幂等刷新） | **保留**，下次复用 |
| `src/tests/strm.test.B/` | 真实 `scan_a_to_b_full_sync` 复制出的目标文件，审核对象 | **删除**，保持 tests 文件夹干净 |
| `<项目根>/test_logs/log_issues_sim_<时间戳>.log` | 本轮测试日志（含同步阶段标记、冲突 WARNING） | **保留**，供排查 |

该文件是一个可重复的“沙盒找修复”实验场，而不是只验证 mock 调用的单元测试。每类问题都先用受控旧行为确认 baseline 能复现，再验证生产代码中的候选修复；生产修复完成后，测试中的 monkeypatch 只保留 baseline 控制组，真实路径继续作为回归保护。

1. **`database is locked`**：在真实 SQLite WAL 数据库中用未提交的 bulk 写事务持有 RESERVED 锁。baseline 直接使用旧的写连接 getter，必须稳定抛出 `sqlite3.OperationalError`；修复后的只读 getter 使用 `read_connection()` 并持有 `rw_lock.read_locked()` 读锁，
由 `test_database_bulk.py::TestReadonlyGettersReadLock` 结构性检查覆盖，B watcher 查询不再抢写锁。
2. **S04E01 / S4E01 路径碰撞**：baseline 使用旧 builder，两个不同 WebDAV 源会落到同一个 B 目标并生成 `_MANUAL_REVIEW_*.md`；修复后 B 区文件名保留 WebDAV basename 的原始 padding，两个源都进入 B，内容不串改。
3. **B 区历史越界清理**：按 `_resolve_a_source` 的路径 A、路径 B 分别构造孤立记录和引擎边界不匹配记录，验证非法文件被物理清理且 DB 同步删除；无引擎配置时合法基础层级仍保留，正常 A→B 产物不被误删。
4. **B 区事件洪泛与锁竞争**：用真实 `BAreaEventHandler` + 手动 watchdog 事件对象触发生产入口，通过可追踪调度器收集后台线程异常并重抛主线程，验证完整事件流不丢 B 记录、不触发 `database is locked`。
5. **同 fingerprint 多实例隔离**：构造 2-3 个同 fingerprint 的 B 实例，验证 `ensure_single_visible_instance` 最终恰好保留一个 `status='valid'` 实例；验证回滚失败时抛异常使清理中止。
6. **字幕路由与多语言**：安装真实 `SubtitleHandler`，验证番剧字幕进入 `Season XX`、中文季名规范化、电影字幕保留目录结构、同集多语言不互相覆盖。
7. **WebDAV 假阴性 fail-closed**：参数化覆盖 `_parse_fs_list_content` 的 22 个不可信响应向量和 A/B 区集成测试，验证不可信父目录整组排除。
8. **Unicode 路径身份与冲突**：验证 NFC/NFD 规范化、斜杠规范化、URL 编码解码、大小写敏感、全角/连续空格不误合并。

综合夹具还保留非 STRM、真二进制 JPEG、字幕、畸形 STRM 和边缘命名样本，用于验证输入鲁棒性与文件统计覆盖。

运行方式（仅该文件）：

```bash
python -m pytest src/tests/test_log_issues_simulation.py -v
```

测试完成后，`src/tests/strm.test.A/` 和 `test_logs/` 保留，`src/tests/strm.test.B/`、临时数据库和 C 区删除。baseline 测试必须先能复现问题，生产代码迁移完成后整文件转绿才算修复有效。

### 人工处理清单

路径碰撞 baseline 会在 B 区根目录生成 `_MANUAL_REVIEW_*.md`，用于证明旧逻辑确实跳过了冲突源。修复后的 padding 实验不应生成该清单。

## 相关文档

- [API 文档](../../docs/)
- [项目说明](../../README.md)
