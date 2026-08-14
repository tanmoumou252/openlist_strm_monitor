# 三、安装与配置
> 最后更新：2026-08-06

## 系统要求

- **操作系统**：Windows 10/11 或 Server 2016+
- **Python**：3.11+
- **磁盘**：500MB+ 剩余空间
- **网络**：可访问 OpenList 服务器（WebDAV + Admin API），可选 TMDB API 访问

## 安装

### 依赖安装

```bash
pip install -r requirements.txt
```

核心依赖包：

| 包名 | 版本 | 用途 |
|------|------|------|
| `watchdog` | ≥4.0.0 | 文件系统监控 |
| `requests` | ≥2.31.0 | HTTP 客户端 |
| `lxml` | ≥5.0.0 | WebDAV XML 解析（PROPFIND 响应） |
| `tomli` | ≥2.0.1 | TOML 解析（Python <3.11 兼容） |

### 启动方式

1. **嵌入式 Python** — 双击 `嵌入式启动.bat`（推荐，自带 Python 环境，自动检测 pip 与依赖后直接启动 WebUI）
2. **系统 Python** — 双击 `环境变量启动.bat`，或直接运行 `python src/webui/server.py`

两种批处理脚本均**直接启动 WebUI**（`src/webui/server.py`），启动后通过 WebUI 内的交互菜单选择是否同时拉起同步引擎（AppService）；批处理本身不提供选项菜单。

## 配置文件

### `config.toml`

生产同步依赖显式 A/B mapping。空 mapping_id、重复 mapping 根、零命中或多命中均进入 fail-safe；C 区按 mapping_id 隔离保存，格式为 `C/<mapping_id>/<relative>`。

主配置文件位于项目根目录，包含以下段：

```toml
[paths]
b_root = ""                      # 运行时仍消费，作为单 mapping 兼容路径（b_root→local.b_dir）
c_root = "./测试c"               # 全局 C 区幽灵目录（c_root→local.c_dir）

# 生产映射在 WebUI/DB 中显式配置：
# [{a_root="./测试a1", b_root="./测试b1"}]   # mapping_id 自动生成，无需手写


[webdav]
host = "http://192.168.x.x:5243" # OpenList WebDAV 地址
user = "admin"                    # 管理员用户名
password = ""                     # 管理员密码
totp_secret = ""                  # TOTP 二步验证密钥

[refresh]
enabled = true                    # 是否启用主动刷新
interval_minutes = 20             # 刷新间隔（分钟）
depth = 5                         # WebDAV 刷新递归深度
timeout_seconds = 300             # 刷新操作超时时间（秒）
log_level = "INFO"                # 刷新日志级别
full_audit_interval_days = 7      # A 区全量审计周期；0 关闭（可能访问所有 A 根）

[behavior]
sync_on_startup = true            # 启动时是否全量同步（默认 true，示例可改为 false 跳过）
sync_on_startup_wait = 0          # 同步前等待秒数
trash_dir_name = "trash"          # 云端回收站目录名
action = "MOVE"                   # 删除动作：MOVE 或 DELETE
ghost_protect_seconds = 300       # 幽灵保护冷却（秒）
a_to_b_restore_delay_seconds = 30 # 损坏文件恢复前等待秒数

[log]
level = "DEBUG"                   # 日志级别
file = "./strm_bridge.log"        # 日志文件路径
max_size_mb = 2                   # 日志文件最大 MB
backup_count = 5                  # 轮转备份数

[webui]
port = 8579  # 默认端口,实际值可自定义                       # HTTP 端口
bind = "0.0.0.0"                  # 监听地址

[tmdb]
# 注意：[tmdb] TOML 段已废弃，勿恢复 enabled 等开关；TMDB 配置仅从数据库加载
# 以下仅供参考，用于首次迁移
access_token = ""                 # TMDB API 访问令牌
api_key = ""                      # TMDB API 密钥
language = "zh-CN"                # 语言偏好
watchlist_cache_ttl = 604800      # 缓存 TTL（秒，默认 7 天）
fuzzy_threshold = 0.60            # 标题匹配阈值
```

### 配置从 TOML 到 DB 的迁移

首次启动时（`main.py` 启动流程），程序会：
1. 创建 `TmdbWatchlistDb` 实例指向 `tmdb_watchlist.db`
2. 调用 `migrate_config_to_db(config, wdb)` — 一次性迁移 config.toml 内容
3. 调用 `config.update_from_db(wdb)` — 加载 DB 覆盖（DB > TOML）

> **注意**：迁移仅在 `main.py` 入口执行。如果通过 `python src/webui/server.py` 直接启动 WebUI，config.toml 中的 WebDAV 凭据不会自动迁移到 DB。建议首次运行 `python src/main.py` 完成迁移，之后通过 WebUI 管理配置。

迁移后，许多配置项可通过 WebUI 管理，无需直接编辑 config.toml：
- OpenList 连接（host、user、password、TOTP）
- STRM 引擎路径和监控路径
- 刷新路径和行为设置
- TMDB 配置

### 数据库存储的配置

`tmdb_watchlist.db` 中的 `webui_config` 表按 scope 存储运行时配置：

| Scope | 示例 |
|-------|------|
| `tmdb` | access_token、api_key、language、host、fuzzy_threshold |
| `openlist` | webdav_host、webdav_user、webdav_password、webdav_totp_secret、strm_engines |
| `ui` | admin_password（PBKDF2 哈希）、主题偏好、UI 状态 |
| `migration` | 迁移跟踪键 |

## 首次设置流程

1. 启动服务器（`python src/webui/server.py`）
2. 打开 `http://localhost:8579`
3. 使用首次启动时控制台打印的密码登录
4. 进入 **配置 → OpenList** 设置：
   - WebDAV 地址、管理员用户名/密码、TOTP 密钥
   - 点击"测试链接"验证连接
5. 配置 STRM 引擎：
   - API 验证通过后从下拉列表选择引擎
   - 监控目录从 API 数据自动填充
6. 配置 A↔B 映射（`a_b_mappings`）：每个 A 区根目录必须绑定唯一的 B 区根目录，否则启动失败
7. 设置 C 区根目录
7. 配置刷新路径和行为设置：`refresh_paths` 为空时不执行周期主动扫描，仅依赖 watchdog 和 B 区删除联动；非空时只扫描命中这些 WebDAV 引擎路径的 A↔B mapping。`full_audit_interval_days` 默认每 7 天执行一次全量 A 区审计，可能访问未订阅的 A 根，设为 0 可关闭。
8. 进入 **配置 → WebUI/TMDB** 设置：
   - TMDB access_token 或 api_key
   - 语言偏好和缓存 TTL

## 运行模式

### 仅 WebUI（推荐首次配置）
```bash
python src/webui/server.py
# 在启动菜单中选择选项 2
```
WebUI 立即可用于配置。

### 完整模式（WebUI + AppService）
```bash
python src/webui/server.py
# 在启动菜单中选择选项 1
```
WebUI 和核心同步引擎同时启动。主程序状态显示在 WebUI 仪表盘上。

## 数据库文件

| 文件 | 用途 | 表数量 | 位置 |
|------|------|--------|------|
| `bridge.db` | 核心同步状态 | 13 常规表 + 3 FTS5 | 项目根目录（固定） |
| `tmdb_watchlist.db` | TMDB 缓存 + WebUI 配置 | 7 张表 | 项目根目录（固定） |

两个数据库均使用 **WAL 模式** 以获得并发读取性能。