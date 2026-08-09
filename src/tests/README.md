# 测试脚本说明

本目录包含 `openlist_strm_bridge` 项目的全部测试文件。使用 `python -m pytest src/tests --collect-only -q` 获取当前实时收集的测试数量。`conftest.py` 的 `collect_ignore_glob` 排除部分独立手动脚本（需要外部服务运行）。+ 若干辅助工具脚本。

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
| `test_area_watchers.py` | A/B/C 三区文件系统监视器事件处理测试 |
| `test_refresh_media.py` | 媒体刷新逻辑（差异检测、逐条同步、LIKE 转义、计数回传）测试（含 `TestLastVerifiedAtWiring`） |
| `test_refresh_service.py` | 周期性 WebDAV 刷新服务测试（含 `TestFullAuditTouchVerified`、`TestRunFullAuditNow`） |
| `test_refresh_service_helpers.py` | RefreshService 辅助委托、路径分析日志、WebDAV 刷新与 update 模式清理测试。 |
| `test_bootstrap.py` | 启动路径工具（`ensure_base_dir_first`、`load_local_module`）测试 |
| `test_log_issues_simulation.py` | 八类真实日志问题的沙盒实验与修复回归（SQLite 锁竞争、padding 路径碰撞、B 区血统清理健康度、B 区事件洪泛、重复实例隔离、字幕路由、WebDAV 假阴性、Unicode 路径） |
| `test_lineage_snapshot_production.py` | 真实 `AppService` 的 mapping-scoped lineage snapshot 验收：覆盖未变更复用、内容修改、删除、同 mapping 重命名、跨 mapping/非法目录移动、无指纹、同/跨 mapping 重复指纹、A 源缺失 boundary 放行、同名不同根、mapping/lineage 版本变化、snapshot 缺失或损坏、stat/DB 写异常及扫描期间文件修改。 |
| `test_app_service_helpers.py` | AppService 辅助方法：锁工厂、mapping 路径解析、WebDAV 辅助、引擎内部标记与日志去重。 |
| `test_app_service_roots.py` | 保护根目录同步、移除根目录扫描与当前根目录快照持久化。 |
| `test_app_service_lifecycle.py` | `AppService` 生命周期编排：配置未就绪 fail-safe、启动阶段顺序、`start_watchers()` 的 A/B/C schedule（mock Observer，不启动真实 watchdog）、`stop()` 的定时器取消与 observer 停止、重复 stop 以及当前未提供的生命周期保证记录。**D1 回归**：WebUI 真实保存体经 DB 往返后引擎门禁 ready（`TestWebUiSavedMappingReachesReady`）。**start() 不变式**：成功启动后 `_running` 置真（`TestStartMarksRunningWhenReady`）；启动期日志可格式化（`TestStartupLogFormatting`）。 |
| `test_multi_mapping_production_acceptance.py` | 多 mapping 生产验收与跨根隔离测试。 |

### 数据库 / FTS

| 文件 | 说明 |
|------|------|
| `test_integration.py` | 数据库重构与核心流程集成测试（含 A/B/C 区 FTS 完整性回归） |
| `test_database_bulk.py` | bulk_connection 批量写入、只读 getter 读锁与并发数据库行为测试（含 `TestLastVerifiedAtColumn`） |
| `test_fts5_search.py` | FTS5 全文检索查询与匹配测试（含 simple 分词器加载、版本可读、`黑暗`/`暗黑` 按词分词语义断言） |
| `test_fts5_escape_and_tmdb_search.py` | FTS5 查询转义函数（`_escape_fts5_query`）与 TMDB 搜索路由测试（含 `进击的巨人[限制级]`、`电影：测试*`、`Spy×Family` 真实媒体名转义） |
| `test_fts_orphan_cleanup.py` | FTS 孤儿行清理与一致性测试（含 **H1 回归**：`sync()` 同时含电影+剧集时两类 FTS 行均保留、电影搜索不失效） |
| `test_tmdb_watchlist_db.py` | TMDB 待看列表 DB 单元测试：匹配状态 CRUD、季数缓存、全量同步 upsert/FTS/独立事务、TV detail 填充、操作日志、webui_config CRUD、加密迁移 |

### 配置 / 安全

| 文件 | 说明 |
|------|------|
| `test_config.py` | 配置模块单元测试：ABMapping、mapping_version、AppConfig.from_file、update_from_db、load_strm_storage_from_api、migrate_config_to_db、配置 fail-closed。**D1 回归**：`update_from_db` 补齐缺失 `mapping_id`（`test_a_b_mappings_backfills_missing_mapping_id` 等）；**D2 回归**：`from_file` 初始化 `a_b_mappings` / `engines_initialized`（`test_from_file_initializes_mapping_fields`）。 |
| `test_password_security.py` | 管理员密码 PBKDF2 哈希与校验安全测试 |
| `test_auth_security.py` | 认证安全测试：登录限流字典逻辑（5 次失败后限流）、密码哈希格式（salt$iterations$hash 三段式，iterations=600000）、首启密码生成与哈希验证往返、损坏密码格式提示（含 `reset_admin.py` 重置指令） |
| `test_secret_manager.py` | 密钥/凭据安全管理测试 |
| `test_migrate_encryption.py` | 加密方案迁移测试 |
| `test_integration_security.py` | 跨模块安全边界与鉴权测试 |
| `test_strm_engines_validation.py` | STRM 引擎配置校验测试 |

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
| `test_subset_font.py` | 字体子集化脚本单元测试：参数解析、Unicode 集合运算、网页字符扫描、缺字来源区分、CSS 一致性校验、icon-preview 与 icons.js 一致性（**icon-preview 副标题图标总数与 `ICONS` 键数一致**（`TestIconPreviewParity`））。 |

### API 客户端

| 文件 | 说明 |
|------|------|
| `test_openlist_hotreload.py` | OpenList 热重载/配置刷新测试 |
| `test_webdav_client.py` | WebDAV 协议客户端测试（含 `_check_exists_cache` 容量淘汰验证、**M8** TOTP 无 padding base64 密钥解码回归） |
| `test_tmdb_client.py` | TMDB API v3 客户端测试 |
| `test_openlist_login_shared.py` | OpenList 登录错误消息解析（`parse_login_error`）测试 |
| `test_main_entry.py` | `main.py` 入口参数拒绝路径测试（禁止使用 `--webui-only` / `--webui`）。 |

### WebUI

| 文件 | 说明 |
|------|------|
| `test_webui_http.py` | WebUI HTTP 服务器与路由分发测试（含 `TestAreaDetailKindParameter`、`TestAreaDetailCZonePagination`、`TestAreaDetailSingleMappingMid`、`TestTMDBWatchlistMatchOverrideConsistency`、`TestManualFullIndexAuditAPI`）。**D2 回归**：全新安装 `/api/config` 不抛异常（`TestConfigApiFreshInstall`）；**D3 回归**：fail-safe 时 `start_main` 返回失败（`TestStartMainFailSafe`）。**第 23 轮回归**（`TestRound13Regressions`）：**M3** `_MEDIA_NAME_SQL` 别名目录（`/movies/` 等）不再坍缩「未分类」、**M4** 改密后旧 token 立即失效、**M5** `/api/admin/status` 带无效 token 返回 401 / 无 token 保持 200。 |
| `test_webui_help_texts.py` | WebUI 帮助文案系统测试：`createField` 输出 `.field-helper-text`、`_openlistHelpTexts` 键完整性、`log_file` 不含「重启」、`refresh_*` 含「即时生效」、TMDB 阈值字段 helperText、孤儿键标注、死字段「未接入匹配逻辑」标注 |
| `test_webui_source_contracts.py` | 前端源码契约回归测试：未定义变量（`mappingIdParam`/`deleteDisabled`）、死参数（`mapping_id`）、CSV 公式注入安全、`_do_bg_sync` 预检查、dialog 断言正则、配置「未接入」标注、畸形请求不计数、交付文档无行号、**M9** `_pageRenderGen = -1` 渲染护栏、**L5** `parseHash` 畸形编码容错 |
| `test_call_coverage.py` | 路由调用覆盖率测试 |
| `test_logging_system.py` | TMDB 操作日志表、日志读取接口与轮转产物测试 |
| `test_logger_setup.py` | logger_setup 模块单元测试：handler 装配、重复初始化（热更新）、回退路径、级别过滤、启动分隔标记、临时目录清理（**窄编码控制台下无法编码字符不丢日志、且不改写流的全局 errors 策略**（`TestConsoleEncodingFallback`））。 |
| `test_concurrency.py` | 并发请求与锁竞争测试 |

### 匹配 / 监视

| 文件 | 说明 |
|------|------|
| `test_watchlist_match.py` | TMDB 想看列表与本地收藏匹配逻辑测试（含 `TestExtractSeasonFromLocalPath`） |
| `test_watchlist_match_state.py` | 匹配状态持久化与状态机测试 |

### 端到端

| 文件 | 说明 |
|------|------|
| `test_index_metadata_api.py` | 索引元数据 API 测试（含 `TestManualFullIndexAudit`，验证 `last_verified_at` 推进） |
| `test_multi_mapping_partition.py` | 多 mapping 分区测试 |
| `test_e2e_full_flow.py` | 完整业务流程端到端测试（登录→配置→A/B 区→状态校验）。**##26 七步链路**：`test_complete_seven_step_onboarding` 覆盖七步正向 HTTP 全链路（登录→TMDb→OpenList→启动→分区→待看同步→收录检测）；`TestSevenStepFailureReasons` 覆盖每步的失败原因与成功条件（未授权、mapping 校验、`not_configured`、`fail_safe_active`、OpenList 登录失败、非法分区、待看开关关闭）。**HTTP→handler 接线回归**：`TestTmdbConfigPersistence::test_tmdb_config_reinitializes_client` 验证 TMDB 配置保存后 `_handler_reinit_tmdb` 确实重建客户端；`TestConfigurationLinkage::test_mapping_id_autogenerated_on_config_save` 验证 HTTP POST → 真实 SQLite → `update_from_db` → mapping_id 自动生成 → 引擎门禁 ready 的全链路（逻辑主覆盖见 `test_config` / `test_app_service_lifecycle`）；`TestFailureScenarios::test_openlist_config_triggers_storage_reload` 验证 OpenList 保存后 `_hot_reload_openlist_config` 被触发（逻辑覆盖见 `test_openlist_hotreload`）。引擎侧 `start()` 置 `_running` 的不变式由 `test_app_service_lifecycle.py` 守卫，本文件用替身只验证 WebUI 启动契约。 |
| `test_onboarding_e2e.py` | 新手引导流程端到端测试。覆盖引导步骤单步跟踪（`view_ab` / `tmdb_refresh` / `tmdb_match` 等）、`config/validate` 预检（OpenList 未配置 / 已配置但离线 / TMDB 未配置警告）、完整引导旅程与整体完成/复位、以及配置联动（OpenList/TMDB/主程序状态变更实时反映在 config/status）。**注意**：七步全链路不在本文件，见 `test_e2e_full_flow.py`。 |

### 性能基准门禁

| 文件 | 说明 |
|------|------|
| `perf/test_benchmark_lineage.py` | 基准正确性门禁：compute_digest 稳定性、build_fixture 结构、baseline/optimized 等价性（不包含性能阈值断言） |

### 独立手动脚本（非 pytest 测试）

以下脚本需外部服务运行，不纳入 pytest 收集（已在 `conftest.py` 的 `collect_ignore_glob` 中排除）：

| 文件 | 说明 | 依赖 |
|------|------|------|
| `test_openlist_admin_api.py` | OpenList Admin API 手动烟雾测试 | 运行中的 OpenList 服务器 |
| `test_tmdb_api.py` | TMDB API CLI/Flask 端点测试 | 有效的 TMDB access_token |
| `test_real_server.py` | 真实服务器安全验证探测 | 运行中的 WebUI (8579) |
| `test_webui_standalone.py` | WebUI 在线集成测试 | 运行中的 WebUI (8579) |

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
python -m pytest src/tests/test_refresh_media.py -v
```

### 运行特定测试类

```bash
python -m pytest src/tests/test_refresh_media.py::TestSyncToBZone -v
```

### 运行特定测试方法

```bash
python -m pytest src/tests/test_refresh_media.py::TestSyncToBZone::test_sync_counts_mixed_results -v
```

### 运行测试并生成覆盖率报告

```bash
python -m pytest src/tests/ --cov=src --cov-report=html
```

## 测试依赖

```bash
pip install -r src/tests/requirements-dev.txt
```

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
- 固定测试密码（`test_password_123`）

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
| `perf/benchmark_lineage.py` | B 区血统核对性能基准 CLI（增量校验方案），非 pytest 测试，用法见文件头注释 |
| `perf/instrumentation.py` | 性能埋点/计时工具，用于基准测试的代码插桩 |

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
