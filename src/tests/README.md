# 测试脚本说明

本目录包含 `openlist_strm_bridge` 项目的全部测试文件，当前共 **34** 个 `test_*.py`。

所有测试均从**项目根目录**运行（根目录含 `conftest.py` 与 `src/` 路径注入），推荐命令：

```bash
python -m pytest src/tests/ -v
```

## 测试文件清单（按功能分组）

### 核心引擎

| 文件 | 说明 |
|------|------|
| `test_app_service_core.py` | 核心同步引擎 `AppServiceCore` 主流程与状态机测试 |
| `test_sync_service.py` | A→B 同步服务（`copy_a_record_to_b_if_needed` 等）测试 |
| `test_area_watchers.py` | A/B/C 三区文件系统监视器事件处理测试 |
| `test_refresh_media.py` | 媒体刷新逻辑（差异检测、逐条同步、LIKE 转义、计数回传）测试 |
| `test_refresh_service.py` | 周期性 WebDAV 刷新服务测试 |

### 数据库 / FTS

| 文件 | 说明 |
|------|------|
| `test_integration.py` | 数据库重构与核心流程集成测试（含 A/B/C 区 FTS 完整性回归） |
| `test_fts5_search.py` | FTS5 全文检索查询与匹配测试 |
| `test_fts5_escape_and_tmdb_search.py` | FTS5 查询转义函数与 TMDB 搜索路由测试 |
| `test_fts_orphan_cleanup.py` | FTS 孤儿行清理与一致性测试 |

### 配置 / 安全

| 文件 | 说明 |
|------|------|
| `test_password_security.py` | 管理员密码 PBKDF2 哈希与校验安全测试 |
| `test_secret_manager.py` | 密钥/凭据安全管理测试 |
| `test_migrate_encryption.py` | 加密方案迁移测试 |
| `test_integration_security.py` | 跨模块安全边界与鉴权测试 |
| `test_strm_engines_validation.py` | STRM 引擎配置校验测试 |

### 工具 / 媒体

| 文件 | 说明 |
|------|------|
| `test_utils.py` | 通用工具函数测试 |
| `test_media_renamer.py` | 媒体重命名与季/集号提取测试 |
| `test_subtitle_handler.py` | 字幕同步与规范化测试 |
| `test_boundary_conditions.py` | 边界条件与异常输入健壮性测试 |
| `test_error_translator.py` | 错误码到用户可读信息的翻译测试 |

### API 客户端

| 文件 | 说明 |
|------|------|
| `test_openlist_admin_api.py` | OpenList Admin API 客户端测试 |
| `test_openlist_hotreload.py` | OpenList 热重载/配置刷新测试 |
| `test_webdav_client.py` | WebDAV 协议客户端测试 |
| `test_tmdb_client.py` | TMDB API v3 客户端测试 |
| `test_tmdb_api.py` | TMDB API 路由与缓存测试 |

### WebUI

| 文件 | 说明 |
|------|------|
| `test_webui_http.py` | WebUI HTTP 服务器与路由分发测试 |
| `test_webui_standalone.py` | WebUI 独立启动与静态资源服务测试 |
| `test_call_coverage.py` | 路由调用覆盖率测试 |
| `test_logging_system.py` | 日志系统测试 |
| `test_concurrency.py` | 并发请求与锁竞争测试 |

### 匹配 / 监视

| 文件 | 说明 |
|------|------|
| `test_watchlist_match.py` | TMDB 想看列表与本地收藏匹配逻辑测试 |
| `test_watchlist_match_state.py` | 匹配状态持久化与状态机测试 |

### 端到端

| 文件 | 说明 |
|------|------|
| `test_e2e_full_flow.py` | 完整业务流程端到端测试（登录→配置→A/B 区→状态校验） |
| `test_onboarding_e2e.py` | 新手引导流程端到端测试 |
| `test_real_server.py` | 真实服务器启动与存活探测测试 |

## 运行测试

### 运行所有测试

```bash
python -m pytest src/tests/ -v
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
pip install pytest pytest-cov
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

## 相关文档

- [API 文档](../docs/)
- [项目说明](../README.md)
