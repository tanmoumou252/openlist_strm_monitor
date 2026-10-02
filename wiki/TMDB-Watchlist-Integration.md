# 八、TMDB 待看列表集成

## 架构

TMDB 集成由三个组件组成：

1. **`TmdbClient`**（`src/tmdb_client.py`）— TMDB API v3 客户端
2. **`TmdbWatchlistDb`**（`src/tmdb_watchlist_db.py`）— SQLite 缓存 + 配置存储
3. **`watchlist_match`**（`src/watchlist_match.py`）— 待看列表与 B 区本地收录的匹配逻辑

## TmdbClient

### 认证

支持两种认证方式：
- **Access Token**（v4 Bearer）— 首选，用于账户特定端点
- **API Key**（v3 查询参数）— 基本数据访问的备用方式

### 代理支持

两种代理机制：
1. **反向代理 host**：设置 `tmdb.host` 为自定义 TMDB 反向代理
2. **HTTP/HTTPS 代理**：设置 `tmdb.proxy` 段

### API 方法

| 方法 | 端点 | 用途 |
|------|------|------|
| `get_account_details()` | `/3/account/{account_id}` | 获取账户信息 |
| `get_watchlist_movies()` | `/3/account/{account_id}/watchlist/movies` | 电影待看列表 |
| `get_watchlist_tv()` | `/3/account/{account_id}/watchlist/tv` | 剧集待看列表 |
| `get_movie_details(id)` | `/3/movie/{id}` | 电影详情（含 `append_to_response`） |
| `get_tv_details(id)` | `/3/tv/{id}` | 剧集详情 |
| `search_movie(query)` | `/3/search/movie` | 搜索电影 |
| `search_tv(query)` | `/3/search/tv` | 搜索剧集 |
| `get_movie_alternative_titles(id)` | `/3/movie/{id}/alternative_titles` | 别名 |
| `get_tv_alternative_titles(id)` | `/3/tv/{id}/alternative_titles` | 别名 |

## TmdbWatchlistDb

### 数据库

四张表：`movies`、`tv`、`meta`、`webui_config`。`TmdbWatchlistDb` 使用 `ThreadPoolExecutor` 并行同步。

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

#### 阶段 1：基于标题的搜索

对每个待看条目按以下名称搜索 B 区：
- 标题（原文和翻译）
- 别名（来自 TMDB 别名端点）
- 中文译名

使用 `SequenceMatcher` 模糊匹配，阈值可通过 WebUI 配置页 → TMDB 设置中的"模糊匹配阈值"字段调整（`fuzzy_threshold`，默认 0.60）。

#### 阶段 2：结构验证

标题匹配后通过结构分析验证：

**电视剧**：
- `_compute_media_root()` — 提取媒体根文件夹
- `_extract_season_from_local_path()` — 统计季数和集数
- 对比 TMDB 的 `_season_count` 和 `_last_ep_episode`
- `anime_min_ep_ratio`（默认 0.30，可通过 WebUI 配置页 → TMDB 设置中的"番剧最少集数比例"字段调整）— 本地集数 >= TMDB 集数的 30%
- `anime_max_season_diff`（默认 1，可通过 WebUI 配置页 → TMDB 设置中的"番剧最大季数差"字段调整）— 季数最多差 1

**电影**：
- 按文件夹名相似度匹配
- 无结构验证

### 匹配状态

| 状态 | 说明 |
|------|------|
| `uncomputed` | 尚未尝试匹配 |
| `matched` | 在 B 区找到，置信度足够 |
| `unmatched` | 未在 B 区找到 |
| `ambiguous` | 多个可能匹配，无法区分 |

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
- **导出**：CSV 和 JSON 格式

### 缓存过期告警

加载时检查缓存是否过期，过期则显示弹窗（可勾选"不再提醒"）。

### 收录状态

每个卡片显示收录状态、匹配原因。收录状态通过 `match_status` 字段跟踪，`match_reason` 记录匹配依据。