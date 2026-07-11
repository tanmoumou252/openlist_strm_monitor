# 一、架构设计与关键类

## 分层架构

代码库采用分层架构，各层职责明确：

```
┌──────────────────────────────────────────────────────────────┐
│                    入口层 (main.py)                            │
│  加载配置 → 初始化 DB → 创建 Admin Client → 启动 AppService    │
├──────────────────────────────────────────────────────────────┤
│              编排层 (app_service_core.py)                     │
│  AppService — 生命周期、事件处理器、血统校验、清理               │
│  SyncService — A→B 同步管线                                   │
│  RefreshService — 周期性 WebDAV 刷新                          │
│  SubtitleHandler — 字幕检测与归档                             │
├──────────────────────────────────────────────────────────────┤
│              领域服务层 (domain/)                              │
│  SubtitleHandler（字幕处理）、SyncService（同步服务）           │
├──────────────────────────────────────────────────────────────┤
│              基础设施层                                        │
│  Database — SQLite bridge.db 管理器                           │
│  OpenListAdminClient — JWT 认证 + Admin API                   │
│  OpenlistWebDAV — WebDAV 协议客户端                           │
│  TmdbClient — TMDB API v3 客户端                              │
│  TmdbWatchlistDb — TMDB 待看列表 DB                           │
├──────────────────────────────────────────────────────────────┤
│              工具层 (utils/)                                   │
│  strm_utils（指纹/路径解析）、file_utils、webdav_utils        │
├──────────────────────────────────────────────────────────────┤
│              WebUI 层 (webui/)                                 │
│  server.py — HTTP 服务器 + 鉴权 + 路由分发                    │
│  routes.py — 全部 API 处理器 (~2335 行)                       │
│  modules/ — Vanilla JS SPA 前端                               │
└──────────────────────────────────────────────────────────────┘
```

## 关键类

### `AppService`（`app_service_core.py:185`）
中央编排器（~2332 行）。管理完整的同步生命周期：
- 环境准备与数据库初始化
- OpenList 引擎配置加载
- A/B/C 区 watchdog 事件处理器
- A→B 复制与血统校验
- B 区事件处理（创建/修改/删除/移动）
- 幽灵保护与冗余清理
- 文件损坏恢复

```python
class AppService:
    def __init__(self, config: AppConfig, db: Database,
                 admin_api: OpenListAdminClient) -> None:
```

### `Database`（`database.py:99`）
SQLite 数据库管理器，WAL 模式（~1400 行）。通过 `threading.RLock()` 保证线程安全。
管理 bridge.db 中 10 张表，提供读写连接的上下文管理器。

### `OpenListAdminClient`（`webdav_client.py:69`）
JWT 认证的 OpenList Admin API 客户端：
- TOTP 2FA 支持（base32/base64 自适应）
- Token 缓存（24h TTL）
- 401 自动重试重新登录
- 大列表分页获取

### `StrmStorageManager`（`app_service_core.py:97`）
管理 STRM 存储的发现与本地配置的验证。

### `AppConfig`（`config.py`）
类型化 dataclass 配置，嵌套配置段。从 `config.toml` 加载，数据库 `webui_config` 表可覆盖。

## 锁获取顺序

引擎使用严格的 6 级锁层次防止死锁（`app_service_core.py:186-196`）：

```
获取顺序（必须从小到大获取）：
  1. _path_locks_lock     （获取 path_lock 时）
  2. _path_locks[path]    （单路径操作）
  3. _dav_write_lock      （WebDAV 写操作）
  4. _cleanup_lock        （延迟清理定时器管理）
  5. _restoring_lock      （恢复标记 / 引擎内部删除标记）
  6. _lineage_log_lock    （日志记录）
```

**规则**：只能按编号从小到大获取，释放时反向。禁止同时持有非相邻的锁。

附加锁：
- `_fingerprint_locks` — 按指纹串行化 A→B 处理（防止 TOCTOU 竞态）
- `get_path_lock(path)` — 按文件路径锁定，使用 `Path(path).resolve()` 做 key
- `get_webdav_lock(webdav_path)` — WebDAV 路径锁，使用 `webdav:` 前缀命名空间隔离（`app_service_core.py:292`）

## 配置优先级

配置按以下优先级解析（`main.py:47-64`, `config.py`）：

1. **数据库**（`tmdb_watchlist.db` → `webui_config` 表）— 最高优先级
2. **OpenList API** — 动态 STRM 存储映射
3. **config.toml** — 静态文件配置
4. **默认值** — dataclass 定义中的硬编码默认值

首次启动时，`config.toml` 内容会被一次迁移到数据库（`config.py:migrate_config_to_db`），之后 DB 成为运行时配置的权威来源。

## 关键设计模式

| 模式 | 用途 | 位置 |
|------|------|------|
| Dataclass 配置 | 所有配置段使用 `@dataclass(slots=True, frozen=True)` | `config.py` |
| 上下文管理器 DB 连接 | `with self.lock, self.connection() as conn:` | `database.py:117` |
| 指纹去重 | `make_strm_fingerprint()` 对 WebDAV 路径做 SHA256 哈希 | `utils/strm_utils.py` |
| 事件驱动文件监控 | watchdog `Observer` + 3 个事件处理器 | `area_watchers.py` |
| 子服务委托 | AppService 创建 SyncService、SubtitleHandler、RefreshService | `app_service_core.py:238-239` |
| 渲染过时检测 | 前端 router 根据计数器判定渲染结果是否过时 | `router.js` |