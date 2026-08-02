# 十、WebUI API 接口参考

所有 API 端点由 `WebUIServer`（`src/webui/server.py`）提供服务，路由处理器在 `src/webui/routes.py` 中。

## 鉴权

两层安全：

1. **IP 白名单** — `_is_lan_ip()` 函数阻止非局域网 IP（10.x、172.16-31.x、192.168.x、169.254.x、localhost）
2. **会话 Token** — PBKDF2-HMAC-SHA256 认证。通过 `X-Session-Token` 头发送。7 天滑动过期，存储在服务器内存中。

免 Token 路径：`/api/login`、`/api/admin/status`、`/api/config`、`/api/webui/config/ui`、`/api/tmdb/avatar`、`/api/tmdb/poster`、`/api/openlist/status`、`/api/openlist/ping`、`/api/page`（SPA 入口）、静态资源（`/`、`/assets/*`、`/favicon.ico`、`/logo.png`、`/openlist_strm_bridge.png`、`/login`、`/fonts/*`、`.woff2`/`.woff`/`.ttf` 字体）。

> 注：`/api/config/status` **不在**免 Token 白名单内，需要会话 Token（白名单中的 `/api/config` 是完整配置端点，非 status 端点）。若未设置管理员密码，`_check_auth` 直接放行全部路径。

## 仪表盘

### `GET /api/main/status`
返回主程序运行状态。A/B/C 区记录数、数据库大小等汇总数据请见 `GET /api/dashboard`，本端点不返回这些字段。

**响应**（主程序运行时）：
```json
{
  "running": true,
  "uptime": 3600,
  "refresh_healthy": true,
  "refresh_consecutive_failures": 0,
  "refresh_last_error": ""
}
```

**响应**（主程序未运行时，仅含基础字段）：
```json
{
  "running": false,
  "uptime": null
}
```

字段说明：
- `running`（bool）— 主程序是否在运行。
- `uptime`（int | null）— 主程序已运行秒数；未运行时为 `null`。
- `refresh_healthy`（bool）— 刷新服务是否健康。**仅当主程序运行时存在**。
- `refresh_consecutive_failures`（int）— 刷新连续失败次数。**仅当主程序运行时存在**。
- `refresh_last_error`（str）— 最近一次刷新错误描述。**仅当主程序运行时存在**。

### `POST /api/main/start` / `POST /api/main/stop`
启动/停止主程序。需要会话 Token。

响应字段：

- `success`（bool）— 是否真正启动/停止。
- `message`（str）— 面向用户的说明。
- `status`（str，可选）— 仅失败时出现，取值如 `not_configured`（未配置 A/B mapping）、`fail_safe_active`（配置未通过 `AppService.get_config_status` 门禁）。

启动成功要求引擎完整走完 `AppService.start()`；配置未就绪时引擎进入 fail-safe 且不启动 watcher，此时接口返回 `success: false` 并带上 `status`，`_app_running` 保持 false。

**状态码语义**：业务失败（未配置 A/B mapping、fail-safe 门禁未过、OpenList 登录失败、重复启动、主程序未在运行）均返回 **200 + `success: false`**，与 `POST /api/openlist/test-connection` 的约定一致。仅服务层未预期异常（`start_main` / `stop_main` 的 `except Exception` 兜底分支）返回 **500 + `error_type: "exception"`**。

### `GET /api/dashboard`
返回仪表盘汇总数据：A/B/C 区记录数、B 区状态分布（valid/duplicate/quarantined）、数据库文件大小、TMDB 配置状态、服务运行时长。

## A/B/C 区

### `GET /api/area/{area}`
列出区域文件。`{area}` 为 `a`、`b` 或 `c`。

**参数**：`page`、`page_size`、`sort`、`order`、`kind`、`q`

- `kind` — 分类筛选，取值：`anime`（番剧，默认）、`movie`（电影）、`other`（其他）、`all`（全部）。`all` 对应前端「全部分类」Tab，不做类型筛选，但仍会返回各分类的真实计数（`kind_counts`）。分类由 `webdav_path` 路径推断（番剧 / 电影 / 其他）。
- `q` — 区域搜索关键词，走 **FTS5 全文搜索**（经 `_escape_fts5_query` 转义特殊运算符，支持中文 simple 分词器）。为空时不筛选。FTS5 查询失败时自动回退到 `LIKE` 子串匹配，避免返回空结果。
- `page_size` — 每页条数，默认 `50`，上限 `500`。
- `sort` — 排序字段，默认 `name`。
- `order` — 排序方向，默认 `asc`。

> 注：本端点不读取 `status` 与 `search` 参数（`search` 仅用于 `/api/records`）。

**响应**：
```json
{
  "area": "b",
  "kind_label": "番剧",
  "kind_counts": { "anime": 80, "movie": 65, "other": 0 },
  "media_items": [
    {
      "name": "示例番剧",
      "kind": "番剧",
      "count": 12,
      "season": "S01",
      "latest_ts": 1700000000
    }
  ],
  "total": 145,
  "page": 1,
  "total_pages": 3,
  "page_size": 50
}
```

### `GET /api/area/{area}/detail`
获取文件详情。参数：`media`（文件路径）。

### `POST /api/area/{area}/refresh`
触发指定区域的 WebDAV 路径刷新。需要会话 Token。

- `area` 仅允许 `a` 或 `b`，`c` 返回 400「无效区域，仅支持 'a' 或 'b'」。
- 必填 Body：`{ "media": "<媒体名>" }`。缺失返回 400「缺少 media 参数」；长度超过 255、含危险字符（`..`、`/`、`\`、`\x00`、`:`、`*`、`?`、`"`、`<`、`>`、`|`）或为绝对路径均返回 400。
- 主程序未运行返回 503 `{"error": "主程序未运行", "status": "not_running"}`；已有刷新进行中返回 409「刷新进行中，请稍后再试」。

成功返回 `{"ok": true, "message": "...", "refresh_dir": "...", "synced": N, "skipped": N, "failed": N}`（注意此处信封键为 `ok`，非 `success`）。

## 配置

### `GET /api/config`
获取应用配置（非敏感字段）。响应为扁平对象，包含约 30 个字段，主要分组如下：

- **数据库**：`db_file`、`db_exists`
- **WebUI**：`webui_port`、`webui_bind`
- **TMDB**：`tmdb_configured`、`tmdb_token_configured`、`tmdb_language`、`tmdb_host`、`tmdb_api_key`（**布尔值**，已脱敏）、`tmdb_api_key_configured`、`tmdb_proxy_configured`、`tmdb_proxy_enabled`、`tmdb_account_id`、`tmdb_watchlist_db`、`tmdb_watchlist_enabled`、`tmdb_fuzzy_threshold`、`tmdb_anime_min_ep_ratio`、`tmdb_anime_max_season_diff`、`tmdb_anime_min_season_ratio`、`tmdb_cache_ttl`
- **A/B/C 区**：`a_b_mappings`（A↔B 映射列表）、`b_root`、`c_root`、`a_folders`、`strm_engine_paths`、`refresh_paths`
- **OpenList/WebDAV**：`webdav_host`、`webdav_user`、`webdav_password`（**布尔值**，已脱敏）、`webdav_totp_secret`（**布尔值**，已脱敏）
- **刷新/行为**：`refresh_enabled`、`refresh_interval`、`behavior_action`、`ghost_protect_seconds`

> 注：`webdav_password`、`webdav_totp_secret`、`tmdb_api_key` 三个敏感字段均返回布尔值（表示是否已配置），不返回明文。`access_token` 不在响应中返回。

### `GET /api/webui/config/ui`
获取 UI 配置（主题偏好等）。**免 Token**。

### `POST /api/webui/config/ui`
保存 UI 配置。Body：`{ "key": "value", ... }`。

### `GET /api/webui/config/{scope}`
获取指定 scope 的配置。`{scope}` 为 `ui`、`tmdb`、`openlist` 或 `migration`。`ui` scope 的 GET 响应会剥离 `admin_password` 字段（安全相关，不对外暴露哈希）。

### `POST /api/webui/config/{scope}`
保存指定 scope 的配置。Body 为键值对 JSON。

- POST 允许的 scope：`tmdb`、`openlist`、`ui`。`migration` scope 仅支持 GET，POST 返回 403「不允许的 scope: migration」。
- `ui` scope 有严格键白名单，仅允许写入：`tmdb_cache_never_remind`、`tmdb_match_toast_disabled`、`admin_password`、`onboarding_completed`、`onboarding_skipped`。其他键返回 403「不允许的配置项: ...」。
- `admin_password` 以明文写入时会自动哈希后存储。
- `openlist` scope 的 `strm_engines` 会经 `_validate_strm_engines` 校验；`a_b_mappings` 会经 `_validate_a_b_mappings` 校验——每个映射必须是非空字符串 `a_root` 与 `b_root` 组成的 dict，否则返回 400「A↔B 映射配置(a_b_mappings)格式不正确：每个映射必须包含非空的 a_root 和 b_root 字段。」

成功返回 `{"success": true, "scope": "<scope>", "saved": <写入键数>}`。

### `GET /api/config/status`
返回配置完成状态（用于新手引导）。响应字段：`password_set`、`tmdb_configured`、`openlist_configured`、`main_running`、`onboarding_completed`、`view_ab_completed`、`tmdb_refresh_completed`、`tmdb_match_completed`（后三者读取 DB 中 `onboarding_view_ab_completed` / `onboarding_tmdb_refresh_completed` / `onboarding_tmdb_match_completed` 键）。

### `POST /api/config/validate`
验证当前配置完整性。

## 登录

### `GET /api/admin/status`
检查是否已配置管理员密码。**免 Token**。

**响应**：`{ "has_password": true }`

### `POST /api/login`
密码认证登录。

**请求**：`{ "password": "..." }`。

- 成功：`{"success": true, "token": "<session_token_hex>"}`，7 天滑动过期。
- 密码为空：400 `{"error": "密码不能为空"}`。
- 未设置管理员密码：400 `{"error": "未设置管理员密码"}`。
- 密码错误：401 `{"error": "密码错误"}`。
- 无效 JSON：400 `{"error": "无效的 JSON"}`。
- **429 限流**：同一 IP 在 300 秒内失败 5 次后触发，返回 `{"error": "登录尝试过于频繁，请在 <retry_after> 秒后重试"}`。限流按 IP 维度统计。

## OpenList

### `GET /api/openlist/status`
返回 OpenList 配置状态（`configured` / `unconfigured`），仅判断是否已配置 host，**不解耦在线性**。连通性探测请用 `/api/openlist/ping`。**免 Token**。

响应：已配置 `{"success": true, "status": "configured", "host": "..."}`；未配置 `{"success": true, "status": "unconfigured"}`。

### `GET /api/openlist/ping`
Ping OpenList API。**免 Token**。

### `POST /api/openlist/test-connection`
测试提供的凭据的 WebDAV 连接。

### `GET /api/openlist/strm-engines`
获取可用的 STRM 引擎列表。

### `GET /api/openlist/monitored-paths`
获取监控路径配置。必填 query 参数 `engine`（缺失返回 400「engine 参数必填」）。成功返回 `{"success": true, "engine": "<engine>", "paths": [...]}`。

### `GET /api/openlist/paths`
获取 OpenList 路径配置。响应字段：

- `a_folders` — A 区根目录列表（由 STRM 引擎自动发现）
- `a_b_mappings` — A↔B 映射列表，每个元素含 `a_root`（A 区根路径）与 `b_root`（对应的 B 区根路径）
- `b_root` — 全局 B 区根目录（兼容旧配置）
- `c_root` — C 区根目录

> 注：当前前端 `openlist.js` 已将「B 区根目录」输入替换为「A↔B 目录映射」逐行填写模式。写入时通过 `POST /api/webui/config/openlist` 提交 `a_b_mappings` JSON 字符串。

## TMDB

### `GET /api/tmdb/status`
返回 TMDB 配置和缓存状态。响应含 `configured`、`host`、`account_id`、`username`、`avatar_path`、`proxy_enabled`、`proxy_url`、`auth_mode`（`api_key` 或 `access_token`）、缓存状态（`cache_stale`、`cache_last_sync`、`cache_item_count`）、匹配统计（`match_uncomputed`、`match_total`）。未配置时返回 `{"configured": false}`。

### `GET /api/tmdb/watchlist/{type}`
获取待看列表。`{type}` 为 `movies` 或 `tv`（实际为 `/api/tmdb/watchlist/movies` 与 `/api/tmdb/watchlist/tv` 两个独立路由）。支持 `page`、`all`、`q` 参数。

- `all=1` — 一次性取回全量，响应注入每条目的 `_status`（由 `match_status` 经 `_STATUS_MAP` 映射）与 `_is_manual`（`manual_override_at > 0`）字段；响应为 `{"account_id": ..., "media_type": "movie"|"tv", "count": N, "results": [...]}`。
- `q` — FTS5 全表过滤（见下节）。
- `page` — 分页（仅 `all != 1` 时生效），响应为 `{"account_id": ..., "media_type": ..., "page": N, "has_next_page": bool, "count": N, "results": [...]}`。

> 注：本端点不读取 `per_page` 与 `match_status` 参数。

### `GET /api/tmdb/watchlist/movies` 与 `GET /api/tmdb/watchlist/tv` 的 `?q=` 搜索

除分页列表外，这两个端点额外支持 `q` 参数做 **FTS5 全表过滤**：

- `GET /api/tmdb/watchlist/movies?q=关键词`
- `GET /api/tmdb/watchlist/tv?q=关键词`

`q` 经 `_escape_fts5_query` 转义后匹配 `tmdb_watchlist_fts` 虚拟表（按 TMDB ID 关联）。`q` 为空时返回该类型的全部条目（不做过滤）。FTS5 异常时回退到内存子串过滤（标题 / 原标题 / 简介，大小写不敏感）作为软降级。可配合 `all=1` 一次性取回全量并过滤。

### `GET /api/tmdb/search/movie`、`GET /api/tmdb/search/tv`、`GET /api/tmdb/search`

TMDB 云端搜索（非本地数据库）：

- `GET /api/tmdb/search/movie?query=...&page=...` — 搜索电影，返回 `{ "query", "page", "results" }`。
- `GET /api/tmdb/search/tv?query=...&page=...` — 搜索电视剧，返回 `{ "query", "page", "results" }`。
- `GET /api/tmdb/search?query=...` — 综合搜索，同时搜索电影与电视剧；`query` 为空返回 400，成功返回 `{ "query", "movies"（前 10 条）, "tv_shows"（前 10 条） }`。

> 关于 FTS5 软降级：区域列表搜索（`/api/area/{area}` 的 `q`）与待看列表过滤（`/api/tmdb/watchlist/{type}?q=`）均使用 FTS5（unicode61 / simple 分词器）。当 FTS5 MATCH 查询抛出异常时，系统会回退到 `LIKE` 子串匹配或内存子串过滤，保证不会因为分词器问题静默返回 0 行。

### `POST /api/tmdb/watchlist/sync`
触发全量待看列表同步。

### `GET /api/tmdb/watchlist/refresh`
后台触发待看列表全量同步（GET 方法，向后兼容）。已在同步中时返回 `{"success": true, "message": "已在同步中"}`。

### `GET /api/tmdb/watchlist/export.csv`
导出待看列表为 CSV 文件下载（UTF-8 BOM 编码）。列：状态、TMDB ID、类型、标题、原标题、发布日期、评分。

### `POST /api/tmdb/watchlist/match/refresh`
刷新待看列表与 B 区收录的匹配状态。

### `GET /api/tmdb/watchlist/match/status`
轮询匹配刷新进度。响应：`{ "running": true/false, "result": {...} }`。

### `POST /api/tmdb/watchlist/match/override`
手动覆盖匹配状态。

**请求 Body**：
```json
{
  "id": 550,
  "media_type": "movie",
  "status": "matched",
  "reason": "manual_override"
}
```

- `id`（int，必填）— TMDB 条目 ID，非整数返回 400「无效的 id」。
- `media_type`（str，必填）— 取值 `movie` 或 `tv`，其他值返回 400「无效的 media_type」。
- `status`（str，必填）— 取值 `matched`、`fuzzy`、`unmatched`、`uncomputed` 之一。
- `reason`（str，可选）— 覆盖原因，默认 `"manual_override"`，截断至 256 字符。

> ⚠️ **契约修正**：早期文档将 body 键写作 `type`，实际代码读取的是 `media_type`。按 `type` 提交会得到 400「无效的 media_type」。这是当前文档与代码不一致的修复点。

成功返回 `{"success": true, "message": "收录状态已手动覆盖"}`。

### `POST /api/tmdb/configure`
更新 TMDB 配置。实际接受 14 个字段：

- 通用循环字段（11 个）：`access_token`、`api_key`、`language`、`host`、`watchlist_db`、`csv_watchlist_file`、`fuzzy_threshold`、`anime_min_ep_ratio`、`anime_max_season_diff`、`watchlist_cache_ttl`、`anime_min_season_ratio`
- 特殊处理字段（3 个）：`proxy_http`、`proxy_enabled`、`watchlist_enabled`

> 注：`access_token` 为空且已配置时会跳过覆盖（避免前端截断覆盖）；`watchlist_db` 相对路径会转为绝对路径。成功返回 `{"success": true, "message": "TMDB 配置已更新", "tmdb_configured": <bool>}`，无变更返回 `{"success": true, "message": "无变更"}`。

### `GET /api/tmdb/season-count/{type}/{id}`
获取电视剧季数。`{type}` 为 `tv`，`{id}` 为 TMDB ID。仅查 DB 缓存，不调用 TMDB API。非 `tv` 类型（如 `movie`）静默返回 `{"id": <id>, "season_count": 0}`，不报错。

### `GET /api/tmdb/detail/tv/{id}`
获取电视剧详情（TMDB API）。响应含 `id`、`name`、`original_name`、`first_air_date`、`last_air_date`、`number_of_seasons`、`number_of_episodes`、`status`、`vote_average`、`last_episode_to_air`。

### `GET /api/tmdb/alias/{type}/{id}`
获取影片别名列表。`{type}` 为 `movie` 或 `tv`，`{id}` 为 TMDB ID。返回 `aliases` 数组（最多 20 条）。

### `GET /api/tmdb/credits/{type}/{id}`
获取演员列表（卡片懒加载用）。`{type}` 为 `movie` 或 `tv`，返回 `cast` 数组（前 4 名演员的 `name` 和 `character`）。

### `GET /api/tmdb/genres/{type}/{id}`
获取分类名称（零 API 调用，从 DB 缓存的 `genre_ids` + 静态映射表反查）。`{type}` 为 `movie` 或 `tv`。

### `GET /api/tmdb/logs`
获取 TMDB 操作日志。参数：`limit`（默认 100，最大 500）。

### `GET /api/tmdb/logs/download`
下载 TMDB 操作日志（最多 100,000 条，`text/plain` 格式）。

### `GET /api/tmdb/avatar` / `GET /api/tmdb/poster`
TMDB 头像/海报图片代理。**免 Token**。

## 记录与日志

### `GET /api/records?area={type}`
获取同步记录。参数：`area`（`a`、`b` 或 `c`）、`page`、`page_size`、`search`。

### `GET /api/logs`
获取系统日志。唯一参数 `lines`（默认 `200`），返回最近 N 行日志。

**响应**：
```json
{
  "lines": ["行1", "行2"],
  "count": 2
}
```

> 注：本端点不读取 `level`/`limit`/`search`/`type` 参数。日志文件不存在时返回 `{"lines": [], "count": 0}`。

### `GET /api/logs/download`
下载系统日志文件。参数：`type`（`main` 或 `tmdb`）。

## 新手引导（Onboarding）

新手引导用于首次使用的分步引导。注意：**本项目不存在 `GET /api/onboarding/status` 端点**，引导状态不通过独立接口读取，而是随 UI 配置一并返回。

### `POST /api/onboarding/complete-step`

单步标记某个引导步骤完成。需要会话 Token。

**请求**：`{ "step": "..." }`

`step` 取值受限，仅支持以下之一，否则返回 400：
- `view_ab` — 浏览 A/B 区
- `tmdb_refresh` — 刷新 TMDB 待看列表匹配
- `tmdb_match` — 完成 TMDB 匹配

成功返回 `{"ok": true}`。每个步骤的完成标记写入 DB：`onboarding_{step}_completed`。

### 引导状态读取（无独立 status 端点）

引导状态由前端通过 `GET /api/config/status` 读取（其响应包含 `onboarding_completed` 等键，驱动引导卡片的「已完成 / 进行中」展示）；`GET /api/webui/config/ui`（免 Token）用于读取/写入 UI 配置，整体完成或跳过通过 `POST /api/webui/config/ui` 写入。相关键如下：

- `onboarding_completed` — 整体是否完成
- `view_ab_completed` / `tmdb_refresh_completed` / `tmdb_match_completed` — 各步骤完成标记

> 注：`onboarding_skipped` 虽然在 `ui` scope 的键白名单中，但代码中从未被写入或读取，属死键，不建议使用。

### 整体完成 / 跳过

通过 `POST /api/webui/config/ui` 写入配置键完成整体引导或跳过：

```json
{ "onboarding_completed": "1" }
```

（`onboarding_completed` 在 `ui` 作用域，属允许写入键。）

## 重启

### `POST /api/restart-webui`
重启主程序（AppService）与 WebUI HTTP 服务。需要会话 Token。立即返回 `{"success": true, "message": "正在重启主程序..."}`，实际重启在后台线程执行（先 `stop_main()` 再 `start_main()`，随后重启 HTTP 服务）。

## 响应信封与错误处理

本项目 API **无统一响应信封**。不同端点使用不同信封格式：

- **`{"success": ...}`** — POST 变更类端点（如登录、配置、TMDB 操作）的主要格式。
- **`{"ok": ...}`** — 部分端点（`POST /api/config/validate`、`POST /api/onboarding/complete-step`、`POST /api/area/{area}/refresh`）。
- **扁平对象** — GET 数据类端点直接返回键值对，无信封包裹（如 `GET /api/main/status`、`GET /api/config`、`GET /api/dashboard`、`GET /api/area/{area}`、`GET /api/logs`）。
- **`{"error": ...}`** — 错误响应，部分端点混合使用 `success: false` + `error` 字段。

常见错误状态码：200（成功）、400（参数错误）、401（未认证）、403（IP 不在白名单或 scope 键不允许）、404（不存在）、429（登录限流）、500（服务器内部错误）、503（主程序未运行）、409（刷新进行中）。

## 路由分发

服务器在 `_WebUIHandler.do_GET` 和 `do_POST` 中使用单一分发器（`server.py`）。每个请求经过：
1. `_guard_request()` — IP 白名单检查
2. `_check_auth()` — Token 验证（白名单路径跳过）
3. 路由处理器分发

路由处理器按域组织在 `routes.py` 中：
- `_tmdb_routes()` — 所有 TMDB 端点
- `_handle_openlist_*()` — OpenList 端点
- `handle_dashboard()`、`handle_area()` — 仪表盘和区域端点
- `handle_config_api()` — 配置端点
- `handle_logs_api()` — 日志端点