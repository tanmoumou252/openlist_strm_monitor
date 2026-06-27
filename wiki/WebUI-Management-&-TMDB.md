# 🖥️ WebUI 管理面板与 TMDB

本程序内置了一个轻量级 WebUI 管理面板，提供可视化的运维管理功能，包括：
- ABC 三区文件浏览
- 仪表盘概览（文件统计、运行状态）
- TMDB 待看列表管理与对比
- 日志实时查看
- 配置修改（保存至独立文件，不回写 config.toml）

---

## 📍 访问地址

默认地址：`http://localhost:8579`

| 配置项 | 默认值 | 说明 |
|:---|:---|:---|
| `[webui] enabled` | `true` | 是否启用管理面板 |
| `[webui] port` | `8579` | 监听端口 |
| `[webui] bind` | `0.0.0.0` | 监听地址（0.0.0.0 = 本地 + 局域网） |

面板仅监听本地和局域网接口，**不会开放公网**。

---

## 🎛️ 功能一览

### 仪表盘

顶部展示：
- A/B/C 三区文件总数
- 程序运行时间
- 各服务模块状态（WebDAV、OpenList API、TMDB 等）
- 本地磁盘占用百分比

每日自动获取 Bing 壁纸作为背景。

### A/B/C 三区浏览

| 区 | 功能 |
|:---|:---|
| **A 区** | 查看 STRM 引擎生成的原始目录结构，两级展开：子类列表（番剧/电影名）→ 文件详情 |
| **B 区** | 查看媒体库消费区目录，支持删除联动操作 |
| **C 区** | 查看幽灵/隔离区内容 |

每个区支持：
- 翻页浏览（每页 50 条）
- 点击子类进入详情页查看具体 STRM 文件
- 详情页展示文件路径、大小、修改时间

### 日志查看

- 实时展示程序运行日志
- 支持按日志级别筛选（INFO / WARNING / ERROR / DEBUG）
- 自动滚动到最新

### 壁纸

- 每日自动从 Bing 获取当日壁纸
- 作为面板背景显示
- 配置禁用或启用

---

## 🎬 TMDB 待看列表

TMDB 待看列表是本面板的核心增强功能之一。

### 功能流程

```
TMDB API (watchlist)
        ↓
  TmdbWatchlistDb.sync()
        ↓
  本地缓存 (tmdb_watchlist_cache.json)
        ↓
  与本地 STRM 已收录内容对比
        ↓
  展示：已收录 ✅ / 待下载 ⬜
```

### 待看列表对比

每个待看条目会与本地数据库中的 STRM 文件做匹配，判断依据：
- **精确匹配**：归一化后标题完全相等
- **子串匹配**：任一方包含另一方（长度 >= 3）
- **模糊匹配**：SequenceMatcher 相似度 >= fuzzy_threshold
- **番剧结构验证**：
  - 季数范围验证：本地季数 <= TMDB 总季数 + 1
  - last_episode_to_air 交叉验证：TMDB 最新播出季数不得低于本地最大季数
  - 集数比例检查：已下载集数 / TMDB 总集数 >= anime_min_ep_ratio

匹配结果以不同颜色卡片展示。

### 季节数填充机制

> **背景**：TMDB API 的 watchlist 端点默认不返回季节数 (season_count)，导致番剧卡片缺少多季信息。

程序通过以下机制自动填充 season_count：

1. **批量获取**：在 `TmdbWatchlistDb.sync()` 中，从 TMDB API 获取完整待看列表后，对每条 TV 类型条目调用 `/tv/{id}` 详情接口获取季节数
2. **缓存持久化**：season_count 存储在缓存 JSON 文件中，下次启动不再重复请求
3. **显示效果**：多季番剧以竖杠 (`|`) 标识，并在卡片中显示具体季节数

### 配置覆盖文件

通过 WebUI 面板 → TMDB 设置修改的配置项（如 API Token、语言、缓存过期时间等）会保存至 `.tmdb_webui_config.json`，**不会回写 `config.toml`**。

```json
{
  "access_token": "...",
  "api_key": "...",
  "host": "...",
  "language": "zh-CN",
  "watchlist_cache_ttl": 43200,
  "tmdb_proxy_enabled": false,
  "tmdb_proxy_http": "",
  "tmdb_proxy_https": ""
}
```

启动时程序优先读取 `.tmdb_webui_config.json`，覆盖 config.toml 中的对应值。

### TMDB 相关配置项

| 配置项 | 说明 |
|:---|:---|
| `access_token` | TMDB v3 Read Access Token（获取待看列表必需） |
| `api_key` | TMDB API Key（备用认证） |
| `host` | TMDB 反代地址（可选） |
| `language` | 待看列表语言（zh-CN / en-US / ja-JP） |
| `watchlist_cache_ttl` | 缓存过期时间，单位秒（默认 86400） |
| `fuzzy_threshold` | 模糊匹配阈值（默认 0.60） |
| `anime_min_ep_ratio` | 番剧集数比例阈值（默认 0.3） |

---

## 🖼️ 自定义 Favicon 与 Logo

将以下文件放入 `src/webui_static/` 目录即可生效：

| 文件 | 说明 | 推荐尺寸 |
|:---|:---|:---|
| `favicon.ico` | 浏览器标签页图标 | 32×32 或 48×48 |
| `logo.png` | 仪表盘左上角 Logo | 400×100 或等比例 |

如果文件不存在，面板会返回 500 错误（不会崩溃），默认使用 index.html 中的 fallback 标识。

---

## 🔒 安全说明

- 面板仅监听本地和局域网接口
- 即使设置 `bind = "0.0.0.0"`，也不会自动开放公网
- 如需公网访问，请自行配置反向代理（如 Nginx）并添加 HTTPS 和认证
- WebUI 支持可选密码认证（配置 `[webui] password` 项）
