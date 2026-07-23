# OpenList STRM Bridge Wiki 首页

`openlist_strm_bridge` 是一个专为 **OpenList STRM 引擎"更新模式"** 打造的本地与云端智能防灾协调层。作为 OpenList 与媒体库（Emby / Jellyfin）之间的协调中枢，打通 STRM 的"生成→刮削消费→重命名整理→删除→云端联动→冗余回收"整条闭环链路。

## 💡 项目核心定位

OpenList STRM 引擎能够高效地生成 `.strm` 文件供本地媒体库刮削使用。但在真实的生产环境中，用户通常面临以下严峻的自愈与防灾挑战：

1. **删除意图不一致**：在媒体库中删除 STRM 文件后，云盘上的真实视频文件不会同步被删。
2. **网盘掉线误删灾难**：当云盘由于网络、到期或掉签而失效时，同步程序可能因读取到"空目录"而误将本地媒体库一并清空。
3. **整理重命名冲突**：刮削器（如 TMM）整理媒体库后会导致文件名变更，普通同步程序会重新同步一份"烂原名"文件，导致媒体库混乱和重复刮削。
4. **单兵逃逸与污染**：用户手滑跨库移动、或者将文件提取至根目录，会导致云端结构与数据库映射彻底失效。
5. **字幕资产丢失**：刮削产生的海报、NFO、外挂字幕在清理空目录时被误删。

本中间件的目的是在 A 区（引擎生成区）、B 区（媒体库消费区）、C 区（幽灵收容区）与云端真实文件之间建立一条由 SQLite 持久化、OpenList Admin API 强绑定的自愈型桥梁，实现双向同步、血统校验、智能去重、字幕同步、熔断保护的完整闭环。

## 🌟 核心特性

1. **API 动态映射**：启动时主动调用 OpenList Admin API 抓取所有 `driver=strm` 的存储节点，自动梳理本地路径与云端真实监控路径的分组映射。（`config.py`、`app_service_core.py`）

2. **智能媒体类型识别与字幕同步**：自动识别电影/番剧类型，电影字幕保持同目录结构，番剧字幕按 `Season XX/S01E01.forced.zho.简体.ass` 标准格式归档。（`media_renamer.py`、`subtitle_handler.py`）

3. **血统鉴权**：任何试图进入媒体库的文件必须接受溯源校验。30 秒观察期捕获越界操作，单兵脱群改名直接物理击杀。（`app_service_core.py` 血统校验管线）

4. **单实例去重**：同一视频源只允许一个可见实例。命名打分机制确保最优命名存活，劣质命名被重命名为 `.duplicate`。（`app_service_core.py` 去重逻辑）

5. **B 区逆向自同步**：启动时对 B 区进行全量盘点——物理磁盘 vs 数据库记录双向比对，自动注册新文件、清理失效记录、追踪改名文件。（`app_service_core.py` 的 `initial_scan_b`，内部经 `_scan_b_disk` 扫描磁盘、`_reconcile_b_historical_records` 对比历史记录）

> 注：`sync_service.py:initial_scan_a` 是**A 区**启动扫描（批量索引 A 区 STRM 文件，多线程 4 线程并发读取，启动时使用 bulk_connection 长连接模式，仅 DB 索引，不做 WebDAV 检查或 A→B 复制。每 100 条或每 2 秒输出进度日志 + records/s 性能基准），与上述 B 区逆向盘点不同，勿混淆。`cleanup_a_redundant_using_api()` 使用 OpenList API 批量清理 A 区冗余文件（本地有但云端没有）。并发分页 + 客户端过滤，性能提升 750 倍。

6. **云端回收站智能重建**：触发删除联动时，在云端配置的回收站内一比一重建原文件夹树再执行移动。（`utils/webdav_utils.py:build_webdav_trash_path`）

7. **被破坏文件自动恢复**：B 区 STRM 文件内容被清空或损坏时，逆向查库并从 A 区自动恢复。（`app_service_core.py` 恢复逻辑）

8. **中文智能搜索**：基于 SQLite FTS5 + `simple` 中文分词器（cppjieba 封装），对 A/B 区 STRM 文件与 TMDB 待看列表实现中文标题/路径全文检索；分词器缺失时软降级但中文检索会失效，故 `simple` 为硬依赖。（`database.py`、`tmdb_watchlist_db.py`）

## 🚨 终极警示：OpenList 令牌与播放签名的强依赖关系

由于 OpenList STRM 引擎下发的直链包含了签名（`?sign=`）：

- **不可恢复的重置灾难**：该签名是 OpenList 服务端算出来的。**绝对不要在 OpenList 后台轻易重置令牌**。一旦重置，即使本地所有 STRM 路径保存完好、云端文件没有任何变动，已生成的 STRM 里的直链全部会报"签名失效"导致彻底无法播放。
- **系统绑定约束**：本项目对于特定的 OpenList 服务端是**强绑定**的。如果迁移到新的服务器，必须清理本地 `bridge.db` 数据库及 B 区文件，让新服务器重新建立全量生成。

## 📂 A/B/C 三区模型

| 区域 | 角色 | 目录来源 | 监控处理器 |
|------|------|----------|-----------|
| **A 区** | 引擎原始 STRM 输出 | OpenList `SaveStrmLocalPath` | `AAreaEventHandler` — 创建/修改/删除 |
| **B 区** | 媒体库消费 | 用户配置的 B 根目录 | `BAreaEventHandler` — 创建/修改/删除/移动 |
| **C 区** | 幽灵收容 | 用户配置的 C 根目录 | `CAreaEventHandler` — 仅记录日志 |

## 🔍 智能搜索

项目内置中文友好的全文搜索：底层使用 SQLite **FTS5** 虚拟表，并加载 `simple` 中文分词器（cppjieba 封装，资源位于 `src/tokenizers/simple/`）对 A 区/B 区 STRM 文件路径及 TMDB 待看列表的标题、简介建立索引。当分词器扩展缺失时会软降级到 `unicode61`，但 `unicode61` 不对中文切词，导致中文搜索实际失效——因此部署时需确保 `simple.dll` 存在。索引的孤儿行由 `_rebuild_fts_if_stale` 等机制在基表变更后自动清理，保证检索结果与真实数据一致。

## 🧭 首次启动引导

新用户首次打开 WebUI 会看到 **7 步新手引导**（确认管理员密码 → 配置 TMDB → 配置 OpenList → 启动主程序 → 查看 A/B 区 → 刷新 TMDB 待看列表 → 检测 TMDB 收录状态）。单步完成通过 `POST /api/onboarding/complete-step` 上报，整体完成或跳过则通过 `POST /api/webui/config/ui` 写入 `onboarding_completed` 标记；引导状态保存在 `tmdb_watchlist.db` 的 `webui_config` 表中，下次打开自动恢复进度。

## 🚀 快速开始

```bash
pip install -r requirements.txt
# 编辑 config.toml 填写 OpenList 服务器信息
python src/webui/server.py
# 打开 http://localhost:8579
```

## 项目目录结构

```
openlist_strm_bridge/
├── src/
│   ├── main.py                  # 入口 — 配置加载、DB 初始化、AppService 启动
│   ├── app_service_core.py      # 核心同步引擎
│   ├── app_service.py           # 兼容性 re-export 层
│   ├── config.py                # 类型化 dataclass 配置
│   ├── database.py              # SQLite bridge.db 管理器
│   ├── webdav_client.py         # OpenList Admin API + WebDAV 客户端
│   ├── area_watchers.py         # Watchdog 文件系统事件处理器
│   ├── media_renamer.py         # 重命名、季集提取
│   ├── refresh_service.py       # 周期 WebDAV 刷新
│   ├── tmdb_client.py           # TMDB API v3 客户端
│   ├── tmdb_watchlist_db.py     # TMDB 待看列表 SQLite 数据库
│   ├── tmdb_watchlist.py        # TMDB 待看列表数据类
│   ├── watchlist_match.py       # 待看列表 vs B 区收录匹配
│   ├── secret_manager.py        # 敏感信息加密
│   ├── logger_setup.py          # 日志初始化配置
│   ├── openlist_login_shared.py # OpenList 共享登录逻辑
│   ├── domain/media/subtitle_handler.py  # 字幕同步
│   ├── domain/sync/sync_service.py      # A→B 同步编排
│   ├── utils/strm_utils.py      # 指纹、WebDAV 路径解析
│   ├── utils/file_utils.py      # 文件 I/O 操作
│   ├── utils/webdav_utils.py    # WebDAV 路径工具
│   ├── utils/error_translator.py # 错误信息翻译
│   ├── utils/bootstrap.py       # 启动引导辅助
│   ├── webui/                   # WebUI 服务端 + 前端源码
│   ├── tests/                   # 测试（37 个测试文件）
│   └── tokenizers/              # simple 中文分词器（simple.dll 等资源）
├── dist/                        # 构建后的前端产物（Vite 输出）
├── docs/                        # API 文档、设计文档
├── wiki/                        # 项目文档
├── config.toml                  # 主配置文件
├── bridge.db                    # 核心 SQLite 数据库 (WAL 模式)
├── tmdb_watchlist.db            # TMDB 待看列表数据库 (WAL 模式)
├── reset_admin.py               # 管理员密码重置工具
└── requirements.txt             # Python 依赖
```

## License

MIT License — 参见 [LICENSE](../LICENSE)。