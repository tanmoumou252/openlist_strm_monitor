# 十、WebUI API 接口参考

所有 API 端点由 `WebUIServer`（`src/webui/server.py`）提供服务，路由处理器在 `src/webui/routes.py` 中。

## 鉴权

两层安全：

1. **IP 白名单** — `_is_lan_ip()` 函数阻止非局域网 IP（10.x、172.16-31.x、192.168.x、169.254.x、localhost）
2. **会话 Token** — PBKDF2-HMAC-SHA256 认证。通过 `X-Session-Token` 头发送。7 天滑动过期，存储在服务器内存中。

免 Token 路径：`/api/config`、`/api/webui/config/ui`、`/api/tmdb/avatar`、`/api/tmdb/poster`、`/api/openlist/status`、`/api/openlist/ping`、`/api/admin/status`、`/api/login`、静态资源

## 仪表盘

### `GET /api/main/status`
返回主程序运行状态。

**响应**：
```json
{
  "running": true,
  "uptime": 3600,
  "a_count": 150,
  "b_count": 145,
  "c_count": 3,
  "app_uptime": 3600,
  "db_size": 1048576,
  "tmdb_configured": true,
  "openlist_configured": true,
  "user_configured": true
}
```

### `POST /api/main/start` / `POST /api/main/stop`
启动/停止主程序。需要会话 Token。

## A/B/C 区

### `GET /api/area/{area}`
列出区域文件。`{area}` 为 `a`、`b` 或 `c`。

**参数**：`page`、`per_page`、`sort`、`order`、`kind`、`q`、`status`、`search`

- `kind` — 分类筛选，取值：`anime`（番剧，默认）、`movie`（电影）、`other`（其他）、`all`（全部）。`all` 对应前端「全部分类」Tab，不做类型筛选，但仍会返回各分类的真实计数（`kind_counts`）。分类由 `webdav_path` 路径推断（番剧 / 电影 / 其他）。
- `q` — 区域搜索关键词，走 **FTS5 全文搜索**（经 `_escape_fts5_query` 转义特殊运算符，支持中文 simple 分词器）。为空时不筛选。FTS5 查询失败时自动回退到 `LIKE` 子串匹配，避免返回空结果。

**响应**：
```json
{
  "items": [
    {
      "local_path": "C:\\strm\\b\\movie\\example.strm",
      "webdav_path": "/cloud/movies/example.mp4",
      "fingerprint": "abc123...",
      "status": "valid",
      "updated_at": 1700000000
    }
  ],
  "total": 145,
  "page": 1,
  "per_page": 50,
  "categories": { "anime": 80, "movie": 65 }
}
```

### `GET /api/area/{area}/detail`
获取文件详情。参数：`media`（文件路径）。

### `DELETE /api/area/{area}`
删除文件。需要会话 Token。Body：`{ "path": "..." }`。

## 配置

### `GET /api/config`
获取应用配置（非敏感字段）。

### `GET /api/webui/config/ui`
获取 UI 配置（主题偏好等）。**免 Token**。

### `POST /api/webui/config/ui`
保存 UI 配置。Body：`{ "key": "value", ... }`。

## 登录

### `GET /api/admin/status`
检查是否已配置管理员密码。**免 Token**。

**响应**：`{ "has_password": true, "authenticated": false }`

### `POST /api/login`
密码认证登录。

**请求**：`{ "password": "..." }`。成功返回 `{ "token": "session_token_hex" }`，失败返回 401。

## OpenList

### `GET /api/openlist/status`
检查 OpenList API 连通性。**免 Token**。

### `GET /api/openlist/ping`
Ping OpenList API。**免 Token**。

### `POST /api/openlist/test-connection`
测试提供的凭据的 WebDAV 连接。

### `GET /api/openlist/strm-engines`
获取可用的 STRM 引擎列表。

### `GET /api/openlist/monitored-paths`
获取监控路径配置。

## TMDB

### `GET /api/tmdb/watchlist/{type}`
获取待看列表。`{type}` 为 `movies` 或 `tv`。支持 `page`、`per_page`、`match_status` 参数。

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

### `POST /api/tmdb/watchlist/match/refresh`
刷新待看列表与 B 区收录的匹配状态。

### `POST /api/tmdb/watchlist/match/override`
手动覆盖匹配状态。Body：`{ "id": 550, "type": "movie", "status": "matched" }`。

### `POST /api/tmdb/configure`
更新 TMDB 配置。Body：`{ "access_token": "...", "api_key": "...", "language": "zh-CN" }`。

### `GET /api/tmdb/season-count/{type}/{id}`
获取电视剧季数。`{type}` 为 `tv`，`{id}` 为 TMDB ID。

### `GET /api/tmdb/logs`
获取 TMDB 操作日志。

### `GET /api/tmdb/avatar` / `GET /api/tmdb/poster`
TMDB 头像/海报图片代理。**免 Token**。

## 记录与日志

### `GET /api/records/{type}`
获取同步记录。`{type}` 为 `a`、`b`、`c` 或 `identity`。

### `GET /api/logs`
获取系统日志。参数：`level`、`limit`、`search`。

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
- `onboarding_skipped` — 是否跳过
- `view_ab_completed` / `tmdb_refresh_completed` / `tmdb_match_completed` — 各步骤完成标记

### 整体完成 / 跳过

通过 `POST /api/webui/config/ui` 写入配置键完成整体引导或跳过：

```json
{ "onboarding_completed": "1" }
```

（跳过同理写入 `onboarding_skipped: "1"`；`onboarding_completed` 与 `onboarding_skipped` 均在 `ui` 作用域，属允许写入键。）

## 重启

### `POST /api/restart/webui`
重启 WebUI 服务器。需要会话 Token。

## 错误处理

所有端点返回 JSON。错误响应格式：
```json
{
  "error": "错误码",
  "message": "可读的错误描述"
}
```

常见状态码：200（成功）、400（参数错误）、401（未认证）、403（IP 不在白名单）、404（不存在）、500（服务器内部错误）。

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