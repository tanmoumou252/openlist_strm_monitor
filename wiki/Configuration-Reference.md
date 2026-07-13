# 十一、配置项完整参考

## 配置优先级

配置按以下优先级解析：

1. **数据库**（`tmdb_watchlist.db` → `webui_config` 表）— 最高优先级
2. **OpenList API** — 动态 STRM 存储映射
3. **config.toml** — 静态文件配置
4. **默认值** — dataclass 定义中的硬编码默认值

## config.toml 参考

所有配置类定义在 `src/config.py` 中，为类型化 dataclass。

### `[local]` — `LocalConfig`

```toml
[local]
db_file = "./bridge.db"
```

| 键 | 默认值 | 说明 |
|-----|------|------|
| `db_file` | `"./bridge.db"` | 核心数据库路径 |

### `[paths]` — `PathsConfig`

```toml
[paths]
b_root = "./测试b"
c_root = "./测试c"
```

| 键 | 默认值 | 说明 |
|-----|------|------|
| `b_root` | `"./b"` | B 区根目录（媒体库消费） |
| `c_root` | `"./c"` | C 区根目录（幽灵收容） |

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
| `interval_minutes` | `20` | 刷新间隔（分钟），内部转秒：`interval_seconds = interval_minutes * 60` |
| `depth` | `5` | WebDAV PROPFIND 扫描深度 |

### `[behavior]` — `BehaviorConfig`

| 键 | 默认值 | 说明 |
|-----|------|------|
| `sync_on_startup` | `false` | 启动时是否执行全量同步 |
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
| `max_size_mb` | `10` | 单文件最大 MB，超限轮转 |
| `backup_count` | `5` | 保留的轮转备份数 |

### `[webui]` — `WebUIConfig`

| 键 | 默认值 | 说明 |
|-----|------|------|
| `enabled` | `true` | 启用 WebUI |
| `port` | `8579` | HTTP 监听端口 |
| `bind` | `"0.0.0.0"` | 监听地址。`"127.0.0.1"` 仅本地访问 |

### `[tmdb]` — `TmdbConfig`

| 键 | 默认值 | 说明 |
|-----|------|------|
| `access_token` | `""` | TMDB API v4 访问令牌（Bearer 认证） |
| `api_key` | `""` | TMDB API v3 密钥（查询参数认证） |
| `language` | `"zh-CN"` | TMDB API 语言参数 |
| `host` | `""` | 自定义 TMDB 反向代理 |
| `watchlist_cache_ttl` | `604800` | 待看列表缓存 TTL（秒，默认 7 天） |
| `fuzzy_threshold` | `0.60` | 标题模糊匹配最低相似度（0.0-1.0） |
| `anime_min_ep_ratio` | `0.30` | 番剧匹配最少集数比例 |
| `anime_max_season_diff` | `1` | 番剧匹配允许的最大季数差 |

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
| `openlist` | `webdav_host` | OpenList 主机 |
| `openlist` | `webdav_user` | OpenList 用户 |
| `openlist` | `webdav_password` | OpenList 密码 |
| `openlist` | `totp_secret` | TOTP 密钥 |
| `openlist` | `strm_engine_paths` | JSON 数组的引擎挂载路径 |
| `openlist` | `behaviour_action` | 删除行为覆盖 |
| `ui` | `admin_password` | PBKDF2 密码哈希 |
| `migration` | `config_migrated` | 迁移跟踪 |

## STRM 存储映射

从 OpenList Admin API 动态加载（`config.py:load_strm_storage_from_api()`）：

```python
@dataclass(slots=True, frozen=True)
class StrmStorageInfo:
    id: int
    mount_path: str       # 引擎挂载点，如 /测试a
    status: str           # work 或其他
    paths: list[str]      # 监控的云端路径
    save_local_mode: str  # 保存模式，须为 update
```

### 三路映射关系

```
OpenList 后台配置                    程序内部映射
┌─────────────────────┐            ┌─────────────────────┐
│ 存储名称: 天翼云盘    │            │ engine_entry: /测试a │
│ 驱动: strm           │     ──▶    │ cloud_path: /天翼云盘/│
│ 挂载路径: /测试a      │            │ 番剧                │
│ SaveStrmLocalPath:   │            │ local_path: C:/测试a/│
│   C:/测试a           │            │   测试a             │
│ 工作模式: update     │            │ status: work        │
│ 状态: work           │            │ save_local_mode:    │
└─────────────────────┘            │   update            │
                                   └─────────────────────┘
```

## JWT Token 缓存

`OpenListAdminClient` 将 JWT Token 缓存到 `~/.openlist_admin_token.json`，避免重复登录。Token 过期前 60 秒自动刷新。401 时清除缓存并重新登录。

## 多存储分组

系统支持多个 STRM 引擎并行工作，每个引擎有独立的 A 区目录、血统校验范围、主动刷新任务和幽灵保护空间。