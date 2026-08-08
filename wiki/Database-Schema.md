# 五、数据库结构参考
> 最后更新：2026-08-06

项目使用两个 SQLite 数据库，均采用 **WAL 模式** 以获得并发读取性能。

## bridge.db — 核心同步状态

由 `Database` 类管理（`src/database.py`）。通过自定义 `ReadWriteLock` 类（`database.py` 顶部定义，支持读写分离：多个读者并发、写者独占、写者优先防饥饿）保证线程安全。所有表在 `_create_schema()` 方法中创建。bridge.db 共 **16 张表**：13 张常规表（`a_strm_files`、`b_strm_files`、`strm_identity`、`c_ghost_files`、`ghost_protection`、`known_folders`、`protected_roots`、`protected_roots_snapshot`、`sync_control`、`strm_media_boundary`、`b_identity_projection`、`b_lineage_snapshot`，以及独立创建的 `subtitles` 表（由 `init_subtitle_table()` 单独创建，`Database.__init__()` 时调用））+ 3 张 FTS5 虚拟表（`a_strm_files_fts`、`b_strm_files_fts`、`c_ghost_files_fts`）。

### 性能 PRAGMA 设置

每个连接应用以下优化：

```sql
PRAGMA journal_mode=WAL;      -- 写前日志，支持并发读取
PRAGMA busy_timeout=10000;     -- 10 秒忙等待
PRAGMA synchronous=NORMAL;     -- WAL 模式下安全与速度的平衡
PRAGMA cache_size=-64000;      -- 64MB 页缓存
PRAGMA temp_store=MEMORY;      -- 临时表在内存中
PRAGMA mmap_size=268435456;    -- 256MB 内存映射 I/O
```

只读连接额外设置：`PRAGMA query_only=ON;`

### 连接管理策略

- 只做 `SELECT` 的 getter 使用 `Database.read_connection()`，避免在 WAL 写事务期间执行 `BEGIN IMMEDIATE`。
- `Database.connection()` 是写连接，会执行写能力探测，仅用于 INSERT、UPDATE、DELETE 或需要维护 FTS 的操作。
- `Database.bulk_connection()` 绕过进程内读写锁，仅用于启动阶段的单线程批量写入；主动刷新使用分批提交，以缩短 RESERVED 锁持有时间。
- 该区分尤其保护 B 区 watcher 的只读查询，避免 A→B bulk 同步期间出现 `database is locked`。

### 只读 getter 读锁一致性（Task 7 修复）

所有只读 getter（含 `get_all_b_records`、`get_table_counts`、`get_b_status_counts`）除使用 `read_connection()` 外，还必须包裹 `rw_lock.read_locked()`，与 `b_fingerprint_exists` 等同模块只读 getter 惯用法一致。这避免并发写事务持写锁时段读到部分提交快照（详见 `src/tests/test_database_bulk.py::TestReadonlyGettersReadLock` 结构性回归）。

### 表结构

#### `a_strm_files` — A 区 STRM 文件记录

| 字段 | 类型 | 说明 |
|------|------|------|
| `local_path` | TEXT PRIMARY KEY | A 区本地绝对路径 |
| `webdav_path` | TEXT NOT NULL | 规范化后的 WebDAV 路径 |
| `parent_webdav_path` | TEXT NOT NULL | 父级 WebDAV 目录 |
| `updated_at` | REAL NOT NULL | 最后更新时间戳 |
| `last_verified_at` | REAL NOT NULL DEFAULT 0 | 最后核对时间戳（单剧目刷新/全量审计后更新，与 `updated_at` 语义不同：`updated_at`=最后变更，`last_verified_at`=最后核对） |

**注意**：`last_verified_at` 不在 upsert 热路径写入（保护 ##29 启动性能），仅在单剧目刷新和全量审计后由 `touch_verified_a`/`touch_verified_by_mapping` 有界批量更新。

**索引**：`idx_a_strm_webdav_path`（webdav_path）、`idx_a_strm_updated_at`（updated_at）

#### `b_strm_files` — B 区 STRM 文件记录

| 字段 | 类型 | 说明 |
|------|------|------|
| `local_path` | TEXT PRIMARY KEY | B 区本地绝对路径 |
| `mapping_id` | TEXT NOT NULL | 所属 A/B mapping，不能为空 |
| `webdav_path` | TEXT NOT NULL | 规范化后的 WebDAV 路径 |
| `parent_webdav_path` | TEXT NOT NULL | 父级 WebDAV 目录 |
| `source_a_path` | TEXT | 对应的 A 区源路径 |
| `fingerprint` | TEXT | SHA-256 指纹 |
| `status` | TEXT DEFAULT 'valid' | 状态：valid/duplicate/quarantined/invalid/ghost |
| `updated_at` | REAL NOT NULL | 更新时间戳 |
| `last_verified_at` | REAL NOT NULL DEFAULT 0 | 最后核对时间戳（语义同 A 区；通过 `source_a_path` 或 `mapping_id` 关联更新） |

**索引**：`idx_b_strm_webdav_path`、`idx_b_strm_fingerprint`、`idx_b_strm_status`、`idx_b_strm_mapping_id`、`idx_b_strm_mapping_fp`、`idx_b_strm_updated_at`。指纹去重和 projection 查询均限定 mapping。

#### `strm_identity` — 身份指纹全局表

| 字段 | 类型 | 说明 |
|------|------|------|
| `fingerprint` | TEXT PRIMARY KEY | SHA-256 指纹 |
| `webdav_path` | TEXT NOT NULL | 规范化的 WebDAV 路径 |
| `source_a_path` | TEXT | 原始 A 区源路径 |
| `current_b_path` | TEXT | 当前 B 区路径（可能因改名而不同于 A 区） |
| `updated_at` | REAL NOT NULL | 更新时间戳 |

**索引**：`idx_identity_webdav_path`、`idx_identity_current_b_path`

#### `c_ghost_files` — C 区幽灵文件记录

| 字段 | 类型 | 说明 |
|------|------|------|
| `local_path` | TEXT PRIMARY KEY | C 区当前路径 |
| `webdav_path` | TEXT NOT NULL | 原始 WebDAV 路径 |
| `original_b_path` | TEXT NOT NULL | 迁移前的 B 区路径 |
| `ghost_root` | TEXT NOT NULL | 所在的 C 区根目录 |
| `moved_at` | REAL NOT NULL | 迁移时间戳 |

**索引**：`idx_c_ghost_moved_at`（moved_at）

#### `ghost_protection` — 幽灵保护表

| 字段 | 类型 | 说明 |
|------|------|------|
| `webdav_path` | TEXT PRIMARY KEY | 受保护的 WebDAV 路径 |
| `expire_time` | REAL NOT NULL | 保护过期时间戳 |
| `reason` | TEXT | 保护原因（如 user_delete） |

#### `known_folders` — 已知文件夹表

| 字段 | 类型 | 说明 |
|------|------|------|
| `folder_path` | TEXT PRIMARY KEY | WebDAV 文件夹路径 |
| `source` | TEXT | 发现来源 |
| `updated_at` | REAL NOT NULL | 最后发现时间 |

#### `protected_roots` — 受保护根目录表

| 字段 | 类型 | 说明 |
|------|------|------|
| `root_path` | TEXT PRIMARY KEY | 引擎根 WebDAV 路径 |
| `trash_path` | TEXT NOT NULL | 对应的回收站路径 |
| `active` | INTEGER NOT NULL | 1=活跃，0=不活跃 |
| `updated_at` | REAL NOT NULL | 更新时间戳 |

#### `protected_roots_snapshot` — 根目录快照表

| 字段 | 类型 | 说明 |
|------|------|------|
| `root_path` | TEXT PRIMARY KEY | 引擎根 WebDAV 路径 |
| `trash_path` | TEXT NOT NULL | 对应的回收站路径 |
| `updated_at` | REAL NOT NULL | 快照时间戳 |

#### `sync_control` — 同步控制表

| 字段 | 类型 | 说明 |
|------|------|------|
| `control_key` | TEXT PRIMARY KEY | 控制键名 |
| `control_value` | TEXT NOT NULL | 控制值（JSON 编码） |
| `updated_at` | REAL NOT NULL | 更新时间戳 |

已知键值（`control_key`）：

| 键名 | 说明 |
|------|------|
| `index_generation` | 全局索引代数（int），每次 `complete_index_generation` 调用递增 |
| `index_generation_at` | 全局索引重建时间戳 |
| `index_generation:{mapping_id}` | 每个 mapping 的索引代数（int） |
| `index_generation_at:{mapping_id}` | 每个 mapping 的索引重建时间戳 |
| `mapping_version` | mapping 配置 + C 根的稳定摘要字符串，用于 `b_lineage_snapshot` 失效判断 |
| `mapping_version_generated_at` | mapping_version 生成时间戳 |
| `last_full_audit_at` | 最后一次全量审计时间戳（float）。由 `RefreshService` 周期审计与 `run_full_audit_now` 手动审计写入，供下一轮周期判断是否需要再次审计。 |

`get_index_metadata()` 方法返回上述键值的聚合快照，供 Dashboard 展示索引健康状态。

#### `strm_media_boundary` — 媒体边界映射表

| 字段 | 类型 | 说明 |
|------|------|------|
| `mapping_id` | TEXT NOT NULL | 所属 mapping |
| `fingerprint` | TEXT NOT NULL | SHA-256 指纹 |
| `source_media_name` | TEXT NOT NULL | 原始云端媒体名 |
| `current_media_name` | TEXT NOT NULL | 当前本地文件夹名 |
| `engine_entry_path` | TEXT NOT NULL | 对应的引擎入口路径 |
| `updated_at` | REAL NOT NULL | 时间戳 |

**主键**：`(mapping_id, fingerprint)`。**索引**：`idx_boundary_source_name`、`idx_boundary_current_name`。不同 mapping 的同名边界互不覆盖。

旧版只有 fingerprint 主键或缺少 `mapping_id` 的 boundary 表无法可靠归属到任意 mapping。数据库初始化时会在同一写事务中重建为上述结构，并删除无法归属的旧记录；迁移失败会回滚并保留原表，不猜测 mapping。

#### `b_identity_projection` — mapping 级当前可见身份

主键为 `(fingerprint, mapping_id)`，记录每个 mapping 当前 visible B 路径；不能使用全局 fingerprint 删除其他 mapping 的 projection。

#### `b_lineage_snapshot` — B 区 lineage 增量快照

主键为 `(mapping_id, local_path)`，保存 `file_size`、`mtime_ns`、`fingerprint`、`mapping_version`、`lineage_version`、`validation_state` 和 `verified_at`。只有 state=`valid` 且版本、元数据和指纹全部匹配时才能复用；损坏或异常必须回退完整核对。

#### FTS5 全文搜索虚拟表

bridge.db 中包含三张 FTS5 虚拟表，使用 `simple` 或 `unicode61` 分词器（取决于 `simple.dll` 是否加载成功）：

| 虚拟表 | 索引基表 | 索引字段 |
|--------|----------|----------|
| `a_strm_files_fts` | `a_strm_files` | `local_path`、`webdav_path` |
| `b_strm_files_fts` | `b_strm_files` | `local_path`、`webdav_path` |
| `c_ghost_files_fts` | `c_ghost_files` | `local_path`、`webdav_path` |

维护：`_backfill_fts_if_empty`（首次回填）和 `_rebuild_fts_if_stale`（孤儿清理，rowid 不一致时全量重建）。

#### `subtitles` — 字幕处理记录表

由 `init_subtitle_table()` 单独创建，`Database.__init__()` 时调用。

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | 自增 ID |
| `local_path` | TEXT NOT NULL UNIQUE | 原始字幕文件路径 |
| `target_path` | TEXT NOT NULL | B 区同步路径 |
| `fingerprint` | TEXT NOT NULL | 关联 STRM 的指纹 |
| `season` | INTEGER | 提取的季号（番剧） |
| `episode` | INTEGER | 提取的集号（番剧） |
| `lang_code` | TEXT | 检测的语言代码 |
| `status` | TEXT DEFAULT 'valid' | 处理状态 |
| `created_at` | TIMESTAMP DEFAULT CURRENT_TIMESTAMP | 创建时间 |
| `updated_at` | TIMESTAMP DEFAULT CURRENT_TIMESTAMP | 更新时间 |

**索引**：`idx_subtitle_fingerprint`（fingerprint）、`idx_subtitle_target`（target_path）

## tmdb_watchlist.db — TMDB 缓存 + WebUI 配置

由 `TmdbWatchlistDb` 类管理（`src/tmdb_watchlist_db.py`）。tmdb_watchlist.db 共 **7 张表**：5 张常规表（`movies`、`tv`、`meta`、`webui_config`、`tmdb_operation_log`）以及 2 张 FTS5 虚拟表 `movies_fts` 与 `tv_fts`。

### 全文搜索与中文分词（FTS5 / simple / unicode61 降级）

本项目的全文搜索依赖 **SQLite FTS5** 虚拟表，并结合中文分词器 `simple`（cppjieba 封装，源于 wangfenjin/simple，当前内置版本见 `src/tokenizers/simple/VERSION`，约 v0.7.1）实现中文智能检索。

- **分词器加载**：`database.py` 的 `_load_simple_tokenizer`（在每次建立连接时通过 `conn.load_extension` 加载 `src/tokenizers/simple/simple.dll`）与 `tmdb_watchlist_db.py` 的 `_load_simple_into` 采用相同逻辑。加载成功后记录实际分词器名（`_fts_tokenizer = 'simple'`）并读取 `VERSION` 文件缓存版本到 `_simple_version`；失败（dll 缺失或加载异常）时仅记录 `logging.warning` **软降级**到 SQLite 内建的 `unicode61`，不阻断启动。
- **降级风险**：`unicode61` 对中文不产生有效的 token，因此当 `simple.dll` 缺失而降级时，**中文搜索实际上会完全失效**。即便存在前缀查询（如 `黑*`），本项目的 FTS 查询转义逻辑（`_escape_fts5_query`）会移除 `*` 等通配符，所以前缀侥幸命中也不成立。换言之，**`simple` 分词器是中文搜索的硬依赖**，部署时必须保证 `src/tokenizers/simple/simple.dll` 存在且可被 `load_extension` 加载。
- **FTS 虚拟表**：bridge.db 中包含 `a_strm_files_fts` / `b_strm_files_fts` / `c_ghost_files_fts`，tmdb_watchlist.db 中包含 `movies_fts` 与 `tv_fts`。这些虚拟表使用上述选中的分词器（simple 优先，否则 unicode61），索引对应基表的 `local_path`/`webdav_path`（或 tmdb 的 `title`/`original_title`/`overview`）。
- **孤儿行清理**：当基表发生增删改时，通过 `_rebuild_fts_if_stale`（及 `_backfill_fts_if_empty`）比对 rowid 集合，删除 FTS 中已无对应基表行的孤儿记录，保证索引与数据一致。

### 表结构

#### `movies` — TMDB 待看列表电影

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `id` | INTEGER PRIMARY KEY | | TMDB 电影 ID |
| `title` | TEXT | '' | 电影标题 |
| `original_title` | TEXT | '' | 原始标题 |
| `overview` | TEXT | '' | 剧情简介 |
| `poster_path` | TEXT | '' | 海报路径 |
| `backdrop_path` | TEXT | '' | 背景图路径 |
| `release_date` | TEXT | '' | 上映日期 |
| `vote_average` | REAL | 0.0 | TMDB 评分 |
| `vote_count` | INTEGER | 0 | 评分人数 |
| `genre_ids` | TEXT | '[]' | 类型 ID 数组（JSON） |
| `popularity` | REAL | 0.0 | 人气值 |
| `original_language` | TEXT | '' | 原始语言 |
| `video` | INTEGER | 0 | 是否为视频 |
| `adult` | INTEGER | 0 | 是否为成人内容 |
| `_media_type` | TEXT | 'movie' | 媒体类型 |
| `_synced_at` | REAL | 0 | 同步时间戳（NOT NULL） |
| `match_status` | TEXT | 'uncomputed' | 匹配状态 |
| `match_reason` | TEXT | '' | 匹配原因说明 |
| `match_updated_at` | REAL | 0 | 匹配状态最后更新时间 |
| `manual_override_at` | REAL | 0 | 手动覆盖时间 |
| `manual_override_by` | TEXT | '' | 手动覆盖操作者 |

#### `tv` — TMDB 待看列表电视剧

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `id` | INTEGER PRIMARY KEY | | TMDB 剧集 ID |
| `name` | TEXT | '' | 剧集名称 |
| `original_name` | TEXT | '' | 原始名称 |
| `overview` | TEXT | '' | 剧情简介 |
| `poster_path` | TEXT | '' | 海报路径 |
| `backdrop_path` | TEXT | '' | 背景图路径 |
| `first_air_date` | TEXT | '' | 首播日期 |
| `vote_average` | REAL | 0.0 | 评分 |
| `vote_count` | INTEGER | 0 | 评分人数 |
| `genre_ids` | TEXT | '[]' | 类型 ID 数组（JSON） |
| `popularity` | REAL | 0.0 | 人气值 |
| `origin_country` | TEXT | '[]' | 产地国家（JSON 数组） |
| `original_language` | TEXT | '' | 原始语言 |
| `_season_count` | INTEGER | 0 | 季数 |
| `_episode_count` | INTEGER | 0 | 集数 |
| `_last_ep_season` | INTEGER | 0 | 最后一季号 |
| `_last_ep_episode` | INTEGER | 0 | 最后一集号 |
| `_media_type` | TEXT | 'tv' | 媒体类型 |
| `_synced_at` | REAL | 0 | 同步时间戳（NOT NULL） |
| `match_status` | TEXT | 'uncomputed' | 匹配状态 |
| `match_reason` | TEXT | '' | 匹配原因说明 |
| `match_updated_at` | REAL | 0 | 匹配状态最后更新时间 |
| `manual_override_at` | REAL | 0 | 手动覆盖时间 |
| `manual_override_by` | TEXT | '' | 手动覆盖操作者 |

#### `meta` — 元数据存储

| 字段 | 类型 | 说明 |
|------|------|------|
| `key` | TEXT PRIMARY KEY | 元数据键 |
| `value` | TEXT NOT NULL | 值（JSON 编码） |

#### `webui_config` — WebUI 配置存储

| 字段 | 类型 | 说明 |
|------|------|------|
| `scope` | TEXT NOT NULL | 配置作用域（tmdb、openlist、ui、migration） |
| `key` | TEXT NOT NULL | 配置键名 |
| `value` | TEXT NOT NULL DEFAULT '' | 配置值（JSON 编码） |
| `updated_at` | REAL NOT NULL DEFAULT 0 | 更新时间戳 |

**主键**：`(scope, key)`

示例：`('tmdb', 'access_token', 'eyJ...', 1700000000)`、`('ui', 'admin_password', 'salt$600000$hash', 1700000000)`

#### `tmdb_operation_log` — TMDB 操作日志

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | 自增 ID |
| `ts` | REAL NOT NULL | 时间戳 |
| `op` | TEXT NOT NULL | 操作类型（如 `sync`） |
| `level` | TEXT NOT NULL DEFAULT 'info' | 日志级别（info/success/warn/error） |
| `msg` | TEXT NOT NULL | 日志消息 |
| `detail` | TEXT | 详细信息 |

**索引**：`idx_tmdb_log_ts`（ts DESC）

#### `movies_fts` — FTS5 全文搜索虚拟表（电影）

为 `movies` 表提供全文搜索能力，建表语句见 `tmdb_watchlist_db.py` 的 `_init_schema` 方法。使用与 bridge.db 相同的分词器选择逻辑（`simple` 优先，失败降级 `unicode61`，见上文「全文搜索与中文分词」）。

- **索引字段**：`title`（对应 `movies.title`）、`original_title`、`overview`，覆盖标题与简介的中英文检索。
- **维护**：在电影写入、更新、删除时通过 `_upsert_movie` 同步增删 FTS 行；通过 `DELETE ... WHERE rowid NOT IN (SELECT id FROM movies)` 清除孤儿行。

#### `tv_fts` — FTS5 全文搜索虚拟表（电视剧）

为 `tv` 表提供全文搜索能力，建表与分词器逻辑同 `movies_fts`。

- **索引字段**：`title`（对应 `tv.name`）、`original_title`（对应 `tv.original_name`）、`overview`。
- **维护**：在电视剧写入、更新、删除时通过 `_upsert_tv` 同步增删 FTS 行；通过 `DELETE ... WHERE rowid NOT IN (SELECT id FROM tv)` 清除孤儿行。