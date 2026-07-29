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
│  SubtitleHandler（domain/media/subtitle_handler.py）           │
│  SyncService（domain/sync/sync_service.py）                    │
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
│  routes.py — 全部 API 处理器                              │
│  modules/ — Vanilla JS SPA 前端                               │
└──────────────────────────────────────────────────────────────┘
```

## 关键类

### `AppService`（`app_service_core.py`）
中央编排器。管理完整的同步生命周期：
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

### `Database`（`database.py`）
SQLite 数据库管理器，WAL 模式。通过自定义 `ReadWriteLock` 类保证线程安全（非 `RLock`）。
管理 bridge.db 中 16 张表（13 张普通表 + 3 张 FTS5 虚拟表），提供读写连接的上下文管理器。

### `OpenListAdminClient`（`webdav_client.py`）
JWT 认证的 OpenList Admin API 客户端：
- TOTP 2FA 支持（base32/base64 自适应）
- Token 缓存（24h TTL）
- 401 自动重试重新登录
- 大列表分页获取

### `StrmStorageManager`（`app_service_core.py`）
管理 STRM 存储的发现与本地配置的验证。

### `AppConfig`（`config.py`）
类型化 dataclass 配置，嵌套配置段。从 `config.toml` 加载，数据库 `webui_config` 表可覆盖。

## 锁获取顺序

引擎使用严格的 6 级锁层次防止死锁（定义在 `AppService.__init__` 方法中）：

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
- `get_webdav_lock(webdav_path)` — WebDAV 路径锁，使用 `webdav:` 前缀命名空间隔离

## 配置优先级

配置按以下优先级解析（`main.py` 启动流程、`config.py`）：

1. **数据库**（`tmdb_watchlist.db` → `webui_config` 表）— 最高优先级
2. **OpenList API** — 动态 STRM 存储映射
3. **config.toml** — 静态文件配置
4. **默认值** — dataclass 定义中的硬编码默认值

首次启动时，`config.toml` 内容会被一次迁移到数据库（`config.py:migrate_config_to_db`），之后 DB 成为运行时配置的权威来源。

## 关键设计模式

| 模式 | 用途 | 位置 |
|------|------|------|
| Dataclass 配置 | 所有配置段使用 `@dataclass(slots=True)`；`StrmStorageInfo` 等不可变快照使用 `frozen=True` | `config.py`、`app_service_core.py` |
| 上下文管理器 DB 连接 | `with self.lock, self.connection() as conn:` | `database.py` |
| `bulk_connection()` 长连接模式 | 启动时批量同步使用单一连接+单一事务，绕过 `rw_lock`（跨进程安全，同进程多线程不安全）。消费函数：`initial_scan_a(use_bulk=True)`、`scan_a_to_b_full_sync(use_bulk=True)` | `database.py` |
| 并发分页模式 | 使用 `ThreadPoolExecutor` 并发请求多个 API 页面（5 并发 + 重试机制） | `app_service_core.py` |
| 指纹去重 | `make_strm_fingerprint()` 对 WebDAV 路径做 SHA256 哈希 | `utils/strm_utils.py` |
| 事件驱动文件监控 | watchdog `Observer` + 3 个事件处理器 | `area_watchers.py` |
| 子服务委托 | AppService 创建 SyncService、SubtitleHandler、RefreshService | `app_service_core.py` |
| 渲染过时检测 | 前端 router 根据计数器判定渲染结果是否过时 | `router.js` |
| 三层防御模式 | 批量同步使用内存缓存 + 文件系统检查 + 去重清理，不使用指纹锁（避免性能灾难） | `sync_service.py` |

## 搜索架构（FTS5 / simple 分词器 / unicode61 降级）

项目的中文智能搜索建立在 **SQLite FTS5 全文搜索虚拟表** 之上，核心由基础设施层的 `Database`（`src/database.py`）与 `TmdbWatchlistDb`（`src/tmdb_watchlist_db.py`）负责。

- **分词器**：中文使用 `simple` 分词器（cppjieba 封装，源于 wangfenjin/simple，内置版本见 `src/tokenizers/simple/VERSION`，约 v0.7.1）。`database.py` 的 `_load_simple_tokenizer` 与 `tmdb_watchlist_db.py` 的 `_load_simple_into` 在连接建立时通过 `conn.load_extension` 加载 `src/tokenizers/simple/simple.dll`；加载成功后切换为 `simple` 并记录版本，失败则**软降级**到内建 `unicode61`（仅 warning，不阻断启动）。
- **降级风险**：`unicode61` 不会对中文切分出有效 token，因此 `simple.dll` 缺失时中文搜索实际完全失效。本项目的 FTS 查询转义会移除 `*` 等通配符，前缀 `黑*` 之类的侥幸命中也不成立——**`simple` 是中文搜索的硬依赖**。
- **FTS 虚拟表**：bridge.db 的 `a_strm_files_fts` / `b_strm_files_fts` / `c_ghost_files_fts` 索引 STRM/幽灵文件的 `local_path`、`webdav_path`；tmdb_watchlist.db 的 `tmdb_watchlist_fts` 索引待看列表的 `title`、`original_title`、`overview`。
- **一致性**：`Database._rebuild_fts_if_stale` / `_backfill_fts_if_empty` 以及 `tmdb_watchlist_db.py` 中的孤儿清理，负责在基表变更后清理 FTS 中悬空的孤儿行，保证索引与基表一致。

## 首次启动引导（Onboarding）

为降低首次使用门槛，WebUI 提供 7 步新手引导，引导状态持久化在 tmdb_watchlist.db 的 `webui_config` 表中（`scope='ui'`，键如 `onboarding_completed`）。

- **7 个步骤**（定义于前端 `src/webui/modules/pages/dashboard.js` 的 steps）：`password`（确认管理员密码）、`tmdb`（配置 TMDB）、`openlist`（配置 OpenList）、`main`（启动主程序）、`view_ab`（查看 A/B 区）、`tmdb_refresh`（刷新 TMDB 待看列表）、`tmdb_match`（检测 TMDB 收录状态）。
- **单步完成**：前端调用 `POST /api/onboarding/complete-step` 手动标记某一步已完成。
- **整体完成 / 跳过**：通过 `POST /api/webui/config/ui` 写入 `{ onboarding_completed: '1' }` 标记引导结束；白名单键见 `routes.py` 的 `_UI_CONFIG_ALLOWED_KEYS`。
- **状态读取**：`GET /api/config/status` 等接口回传 `onboarding_completed` 等字段，驱动前端步骤卡片的「已完成 / 进行中」展示。