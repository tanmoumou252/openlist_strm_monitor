# 八、TMDB 待看列表集成
> 最后更新：2026-08-06

## 架构

TMDB 集成由三个组件组成：

1. **`TmdbClient`**（`src/tmdb_client.py`）— TMDB API v3 客户端
2. **`TmdbWatchlistDb`**（`src/tmdb_watchlist_db.py`）— SQLite 缓存 + 配置存储
3. **`watchlist_match`**（`src/watchlist_match.py`）— 待看列表与 B 区本地收录的匹配逻辑

## TmdbClient

### 认证

支持两种认证方式：
- **Access Token**（v3 Bearer，`Authorization: Bearer {access_token}`）— 首选，用于账户特定端点
- **API Key**（v3 查询参数 `?api_key=xxx`）— 基本数据访问的备用方式

优先级：(1) `access_token` → Bearer header，(2) `api_key` → query param，(3) 均为空 → 返回 None。

### 代理支持

两种代理机制：
1. **反向代理 host**：设置 `tmdb.host` 为自定义 TMDB 反向代理
2. **HTTP/HTTPS 代理**：设置 `tmdb.proxy` 段

### API 方法

| 方法 | 端点 | 用途 |
|------|------|------|
| `fetch_account_id()` | `/3/account` | 获取账户 ID（结果缓存到 `.tmdb_account.json`，TTL 7 天） |
| `get_watchlist_movies()` | `/3/account/{account_id}/watchlist/movies` | 电影待看列表 |
| `get_watchlist_tv()` | `/3/account/{account_id}/watchlist/tv` | 剧集待看列表 |
| `get_movie_details(id)` | `/3/movie/{id}` | 电影详情（含 `append_to_response`） |
| `get_tv_details(id)` | `/3/tv/{id}` | 剧集详情 |
| `search_movie(query)` | `/3/search/movie` | 搜索电影 |
| `search_tv(query)` | `/3/search/tv` | 搜索剧集 |
| `get_movie_aliases(id)` | `/3/movie/{id}/alternative_titles` | 电影别名（注：存在但不在匹配流程中使用） |
| `get_tv_aliases(id)` | `/3/tv/{id}/alternative_titles` | 剧集别名（注：存在但不在匹配流程中使用） |

### 请求重试策略

`request()` 方法内建重试：默认 3 次重试，指数退避（`backoff=1.0`）。遇到 429 状态码时解析 `Retry-After` header 等待；网络错误时按指数退避重试。

## TmdbWatchlistDb

### 数据库

七张表：`movies`、`tv`、`meta`、`webui_config`、`tmdb_operation_log`（操作日志，level 含 `success`），以及 2 张 FTS5 虚拟表 `movies_fts`（索引 `movies.title`/`original_title`/`overview`）与 `tv_fts`（索引 `tv.name`/`tv.original_name`/`overview`），分别用于电影/电视剧标题搜索。`TmdbWatchlistDb` 使用 `ThreadPoolExecutor` 仅用于 `_populate_tv_details` 批量补齐 TV 详情（非全量并行同步）。

### 缓存 TTL

默认 604800 秒（7 天）。可通过 WebUI 配置页 → TMDB 设置中的"缓存 TTL（秒）"字段调整。

## 待看列表匹配

### 问题

将 TMDB 待看列表条目与本地 B 区收录匹配是一个**非标准媒体集合对齐问题**：
- B 区文件夹名可能含噪音（如"番剧 1-3 季全集"）
- 多个 TMDB 条目可能共享相同名称（如不同版本的"GTO"）
- 中文名 vs 原名不匹配
- 需要季集结构分析

### 匹配算法

#### 阶段 1：三级标题匹配

对每个待看条目，使用条目自身的字段（`title`、`name`、`original_title`、`original_name`、`name_cn`、`original_name_cn`）搜索 B 区，**不调用 TMDB 别名 API**：

1. **精确匹配**（`exact`）— 标准化后完全相等
2. **松散匹配**（`loose`）— 子串匹配（≥3 字符）
3. **模糊匹配**（`fuzzy`）— `SequenceMatcher` ≥ 阈值（默认 0.60，可通过 WebUI 配置页 → TMDB 设置中的"模糊匹配阈值"字段调整）

匹配结果生成 `match_reason` 格式：`{type}:{detail}`，如 `movie_exact:{name}`、`tv_loose:{name}|S{num}`、`tv_few_episodes:{ratio}%<{min}%`。

#### 阶段 2：结构验证

标题匹配后通过结构分析验证：

**电视剧**：
- `_compute_media_root()` — 提取媒体根文件夹
- `_extract_season_from_local_path()` — 统计季数和集数
- 对比 TMDB 的 `_season_count` 和 `_last_ep_episode`
- `anime_min_ep_ratio`（默认 0.30，可通过 WebUI 配置页 → TMDB 设置中的"番剧最少集数比例"字段调整）— 本地集数 >= TMDB 集数的 30%
- 季数检查：硬编码 `season_num > total_seasons + 1`（注：`anime_max_season_diff` 配置字段存在但 `watchlist_match.py` 未读取，运行时无效）

**电影**：
- 按文件夹名相似度匹配
- 无结构验证

### 匹配状态

| 状态 | 说明 |
|------|------|
| `uncomputed` | 尚未尝试匹配 |
| `matched` | 在 B 区找到，置信度足够 |
| `unmatched` | 未在 B 区找到 |
| `fuzzy` | 多个可能匹配，无法确定唯一匹配 |

### 手动覆盖

用户可通过 WebUI 手动覆盖匹配状态。

## WebUI 集成

### TMDB 页面

TMDB 待看列表页面提供：
- **海报网格**，延迟加载图片
- **季数条**（电视剧可视化季数指示）
- **筛选控制**：全部/已收录/未收录
- **卡片翻转**：点击显示详情和匹配原因
- **同步按钮**：从 TMDB API 刷新待看列表
- **导出**：CSV 格式（`export_watchlist_csv()`，UTF-8 BOM 编码）

### 缓存过期告警

加载时检查缓存是否过期，过期则显示弹窗（可勾选"不再提醒"）。

### 收录状态

每个卡片显示收录状态、匹配原因。收录状态通过 `match_status` 字段跟踪，`match_reason` 记录匹配依据。

## 未文档化机制

### `account_id` 文件缓存

`fetch_account_id()` 获取的 `account_id` 缓存到 `src/.tmdb_account.json`，TTL 7 天。避免每次启动都重新调用 `/3/account` 端点。

### 敏感配置加解密

`TmdbWatchlistDb` 的 `webui_config` 表中，敏感键（`access_token`、`api_key`、`proxy_http`、`webdav_password`、`webdav_totp_secret`）通过 `secret_manager.encrypt()`/`decrypt()` 加密存储。`get_config()`/`set_config()` 透明处理加解密。`migrate_plaintext_to_encrypted()` 用于从旧版明文迁移。

### 数据模型（`tmdb_watchlist.py`）

独立文件定义三个 dataclass：`TmdbItem`（待看条目）、`LastEpisode`（最近一集信息）、`MatchResult`（匹配结果）。`export_watchlist_csv()` 也在此文件中。

### 手动覆盖机制

`POST /api/tmdb/watchlist/match/override` 允许手动设置匹配状态。写入 `match_status` + `manual_override_at`（时间戳）+ `match_reason`（默认 `"manual_override"`）。后续自动匹配不会覆盖 `manual_override_at > 0` 的条目（除非用户再次手动触发）。

### 清除覆盖

`POST /api/tmdb/watchlist/match/clear` 允许清除手动覆盖，将条目恢复为 `uncomputed` 状态。写入 `match_status='uncomputed'`，清除 `manual_override_at` 和 `manual_override_by`。请求体：`{media_type: str, id: int}`。前端 TMDB 卡片翻转视图中的"恢复自动匹配"按钮调用此端点。调用前需检查 `watchlist_enabled` 开关（与 `/match/override` 行为一致）。