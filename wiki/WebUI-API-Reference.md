# 十、WebUI API 接口参考

所有 API 端点由 `WebUIServer`（`src/webui/server.py`）提供服务，路由处理器在 `src/webui/routes.py`（~2335 行）中。

## 鉴权

两层安全：

1. **IP 白名单** — `_is_lan_ip()`（`routes.py:53`）阻止非局域网 IP（10.x、172.16-31.x、192.168.x、169.254.x、localhost）
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

**参数**：`page`、`per_page`、`sort`、`order`、`kind`（anime/movie）、`status`、`search`

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