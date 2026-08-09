# 十一、配置项完整参考
> 最后更新：2026-08-06

## 配置优先级

配置按以下优先级解析：

1. **数据库**（`tmdb_watchlist.db` → `webui_config` 表）— 最高优先级
2. **OpenList API** — 动态 STRM 存储映射（API 层本身由 DB 门控：仅当 DB 中配置了 host/credentials 后才会调用 API）
3. **config.toml** — 静态文件配置（首次迁移后，大部分被 DB 覆盖）
4. **默认值** — dataclass 定义中的硬编码默认值

## config.toml 参考

所有配置类定义在 `src/config.py` 中，为类型化 dataclass。

> **数据库路径固定**：`bridge.db` 与 `tmdb_watchlist.db` 固定在项目根目录，`[local].db_file` 配置项已移除，**不可通过 config.toml 或 WebUI 更改数据库路径**。

### `[paths]` — `PathsConfig`

```toml
[paths]
# 旧 b_root 仅为兼容显示，不自动生成生产 mapping
b_root = ""
c_root = "./测试c"

# 生产 B 归属通过 WebUI/DB 的 a_b_mappings 显式配置：
# [{a_root="./测试a1", b_root="./测试b1"}]   # mapping_id 自动生成，无需手写
```


| 键 | 默认值 | 说明 |
|-----|------|------|
| `b_root` | `""` | B 区根目录（媒体库消费） |
| `c_root` | `""` | C 区根目录（幽灵收容） |

### `[webdav]` — `WebDAVConfig`

```toml
[webdav]
host = "http://192.168.1.100:5243"
user = "admin"
password = ""
totp_secret = ""
```

| 键 | 默认值 | 说明 |
|-----|------|------|
| `host` | `""` | OpenList WebDAV 地址，末尾斜杠和 `/dav` 后缀自动移除 |
| `user` | `""` | 管理员用户名（需管理员权限） |
| `password` | `""` | 管理员密码 |
| `totp_secret` | `""` | TOTP 二步验证密钥，支持 base32 和 base64 编码 |

### `[refresh]` — `RefreshConfig`

| 键 | 默认值 | 说明 |
|-----|------|------|
| `enabled` | `true` | 启用周期 WebDAV 刷新 |
| `interval_minutes` | `10` | 刷新间隔（分钟），内部转秒：`interval_seconds = interval_minutes * 60` |
| `depth` | `5` | WebDAV PROPFIND 扫描深度 |
| `timeout_seconds` | `300` | 刷新操作超时时间（秒） |
| `log_level` | `"INFO"` | 刷新日志级别：DEBUG/INFO/WARNING |
| `full_audit_interval_days` | `7` | A 区全量审计周期；0 关闭。`refresh_paths` 为空时周期局部扫描停止，但到期全量审计仍可能访问所有 A 根。DB 键名为 `refresh_full_audit_interval_days`（带 `refresh_` 前缀） |

### `[behavior]` — `BehaviorConfig`

| 键 | 默认值 | 说明 |
|-----|------|------|
| `sync_on_startup` | `true` | 启动时是否执行全量同步 |
| `sync_on_startup_wait` | `0` | 同步前等待秒数 |
| `trash_dir_name` | `"trash"` | 云端回收站目录名 |
| `action` | `"MOVE"` | 删除动作：MOVE（移到回收站，安全）或 DELETE（永久删除） |
| `ghost_protect_seconds` | `300` | 幽灵保护时长（秒） |
| `a_to_b_restore_delay_seconds` | `30` | 损坏文件恢复前等待秒数 |

### `[log]` — `LogConfig`

| 键 | 默认值 | 说明 |
|-----|------|------|
| `level` | `"INFO"` | 日志级别：DEBUG/INFO/WARNING/ERROR |
| `file` | `"strm_bridge.log"` | 日志文件路径（默认位于项目根目录） |
| `max_size_mb` | `2` | 单文件最大 MB，超限轮转 |
| `backup_count` | `5` | 保留的轮转备份数 |

### `[webui]` — `WebUIConfig`

| 键 | 默认值 | 说明 |
|-----|------|------|
| `enabled` | `true` | 启用 WebUI |
| `port` | `8579` | HTTP 监听端口 |
| `bind` | `"0.0.0.0"` | 监听地址。`"127.0.0.1"` 仅本地访问 |

### `[tmdb]` — `TmdbConfig`

> **注意**：`[tmdb]` TOML 段已废弃。`from_file()` 显式忽略此段的 TOML 值，TMDB 配置仅从数据库加载（通过 WebUI 配置页管理）。以下仅供参考，用于初始迁移。

| 键 | 默认值 | 说明 |
|-----|------|------|
| `access_token` | `""` | TMDB API v3 访问令牌（Bearer 认证，用于 `/3/account` 等账户端点） |
| `api_key` | `""` | TMDB API v3 密钥（查询参数认证） |
| `language` | `"zh-CN"` | TMDB API 语言参数 |
| `host` | `""` | 自定义 TMDB 反向代理 |
| `watchlist_cache_ttl` | `604800` | 待看列表缓存 TTL（秒，默认 7 天） |
| `fuzzy_threshold` | `0.60` | 标题模糊匹配最低相似度（0.0-1.0） |
| `anime_min_ep_ratio` | `0.30` | 番剧匹配最少集数比例 |
| `anime_max_season_diff` | `0.3` | 番剧匹配允许的最大季数差（运行时未读取，当前无效） |
| `anime_min_season_ratio` | `0.3` | 番剧匹配最少季数比例（**运行时未读取**，仅配置兼容保留） |
| `csv_watchlist_file` | `""` | CSV 待看列表文件路径（可选） |
| `proxy_enabled` | `false` | 启用 TMDB API 代理 |
| `proxy_http` | `""` | HTTP 代理地址 |
| `proxy` | `{}` | 嵌套代理配置 `TmdbProxyConfig`（含 `enabled`、`http`、`https`） |

## 数据库存储的配置

`webui_config` 表存储运行时配置，可覆盖 `config.toml` 值。通过 WebUI 配置页管理。

| Scope | 键 | 说明 |
|-------|-----|------|
| `tmdb` | `access_token` | TMDB 访问令牌 |
| `tmdb` | `api_key` | TMDB API 密钥 |
| `tmdb` | `language` | 语言偏好 |
| `tmdb` | `host` | 自定义 TMDB 地址 |
| `tmdb` | `watchlist_cache_ttl` | 缓存 TTL 覆盖 |
| `tmdb` | `fuzzy_threshold` | 匹配阈值 |
| `tmdb` | `proxy_enabled` | TMDB 代理开关 |
| `tmdb` | `proxy_http` | TMDB HTTP 代理地址 |
| `tmdb` | `watchlist_enabled` | 待看列表功能开关 |
| `tmdb` | `anime_min_ep_ratio` | 番剧最少集数比例 |
| `tmdb` | `anime_max_season_diff` | 番剧最大季数差（运行时未读取，当前无效） |
| `tmdb` | `anime_min_season_ratio` | 番剧最少季数比例 |
| `tmdb` | `csv_watchlist_file` | CSV 待看列表文件路径 |
| `openlist` | `webdav_host` | OpenList 主机 |
| `openlist` | `webdav_user` | OpenList 用户 |
| `openlist` | `webdav_password` | OpenList 密码（加密存储） |
| `openlist` | `webdav_totp_secret` | TOTP 密钥（加密存储） |
| `openlist` | `b_root` | B 区根目录 |
| `openlist` | `c_root` | C 区根目录 |
| `openlist` | `a_b_mappings` | A↔B 映射列表（JSON 数组，元素含 `a_root`、`b_root`、`label`；由 `openlist.js` 提交，替代旧 `b_root` 单值输入）。`mapping_id` **不由前端提交**，读取侧 `AppConfig.update_from_db` 按 A 根规范化路径调用 `ABMapping.generate_mapping_id` 自动补齐；显式写入的 `mapping_id` 不会被覆盖 |
| `openlist` | `strm_engines` | 引擎配置（从 WebUI 写入，派生 `a_folders` 和 `strm_engine_paths`） |
| `openlist` | `engines_initialized` | 引擎初始化标志（迁移时设为 `true`） |
| `openlist` | `refresh_paths` | 刷新路径 |
| `openlist` | `refresh_enabled` | 刷新开关 |
| `openlist` | `refresh_interval_minutes` | 刷新间隔（分钟，内部转秒） |
| `openlist` | `refresh_depth` | WebDAV PROPFIND 扫描深度 |
| `openlist` | `refresh_log_level` | 刷新日志级别 |
| `openlist` | `refresh_full_audit_interval_days` | 全量审计周期（天）；0 关闭。**注意：TOML 中对应键名为 `full_audit_interval_days`（无 `refresh_` 前缀），两者不同** |
| `openlist` | `behavior_action` | 删除行为（MOVE/DELETE） |
| `openlist` | `behavior_trash_dir_name` | 云端回收站目录名 |
| `openlist` | `behavior_ghost_protect_seconds` | 幽灵保护时长（秒） |
| `openlist` | `behavior_a_to_b_restore_delay_seconds` | 损坏文件恢复前等待秒数 |
| `openlist` | `behavior_sync_on_startup` | 启动时全量同步 |
| `openlist` | `behavior_sync_on_startup_wait` | 同步前等待秒数 |
| `openlist` | `log_level` | 日志级别 |
| `openlist` | `log_max_size_mb` | 日志文件最大 MB |
| `openlist` | `log_backup_count` | 日志轮转备份数 |
| `openlist` | `log_file` | 日志文件路径 |
| `ui` | `admin_password` | PBKDF2 密码哈希 |
| `ui` | `onboarding_completed` | 引导完成标记 |
| `ui` | `tmdb_cache_never_remind` | 缓存过期不再提醒 |
| `ui` | `tmdb_match_toast_disabled` | 匹配 Toast 禁用 |
| `migration` | `config_toml_migrated` | 迁移跟踪 |

> 注：`a_folders` 不是 DB 键，而是从 `strm_engines` 动态派生的 A 区本地路径列表。

## STRM 存储映射

从 OpenList Admin API 动态加载（`config.py:load_strm_storage_from_api()`）：

```python
# app_service_core.py — 存储节点信息快照（5 字段）
@dataclass(slots=True, frozen=True)
class StrmStorageInfo:
    id: int
    mount_path: str       # 引擎挂载点，如 /测试a
    status: str           # work 或其他
    paths: list[str]      # 监控的云端路径
    save_local_mode: str  # 保存模式，须为 update

# config.py — 存储映射关系（3 字段，无 frozen/slots 修饰）
@dataclass
class StrmStorageMapping:
    mount_path: str       # 引擎挂载点
    paths: list[str]      # 监控的云端路径
    local_path: str       # A 区本地路径
```

> 注：`status`/`save_local_mode` 属于 `StrmStorageInfo`，不属于 `StrmStorageMapping`。`config.py` 中的主要配置段使用 `slots=True`；`StrmStorageMapping` 等简单映射类仅用 `@dataclass`；`StrmStorageInfo` 使用 `slots=True, frozen=True`。

### 三路映射关系

```
OpenList 后台配置                    程序内部映射
┌─────────────────────┐            ┌─────────────────────┐
│ 存储名称: 天翼云盘    │            │ engine_entry: /测试a │
│ 驱动: strm           │     ──▶    │ cloud_path: /天翼云盘/│
│ 挂载路径: /测试a      │            │ 番剧                │
│ SaveStrmLocalPath:   │            │ local_path: C:/测试a/│
│   C:/测试a           │            │   测试a             │
│ 工作模式: update     │            └─────────────────────┘
│ 状态: work           │              ↑ StrmStorageMapping
└─────────────────────┘              ↑ StrmStorageInfo（含 status/save_local_mode）
```

## JWT Token 缓存

`OpenListAdminClient` 将 JWT Token 缓存到 `src/.admin_token.json`（`OpenListAdminClient` 所在目录），避免重复登录。Token 过期前 60 秒自动刷新。401 时清除缓存并重新登录。

> **注意：** 这里的"60 秒"指 JWT Token 过期前的自动刷新提前量，与 `refresh.interval_minutes`（默认 10 分钟）控制的周期性 WebDAV/主动刷新间隔是两套独立机制，勿混淆。

## 多存储分组

系统支持多个 STRM 引擎并行工作，每个引擎有独立的 A 区目录、血统校验范围、主动刷新任务和幽灵保护空间。