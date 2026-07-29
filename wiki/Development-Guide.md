# 十二、开发指南

## 技术栈

| 层 | 技术 |
|----|------|
| **语言** | Python 3.11+（后端）、JavaScript（前端） |
| **前端构建** | Vite 8.x，Vanilla JS（无 React/Vue） |
| **后端 HTTP** | Python 标准库 `http.server` - `ThreadingHTTPServer`（多线程） |
| **数据库** | SQLite（WAL 模式，两个文件） |
| **文件监控** | `watchdog` 库 |
| **HTTP 客户端** | `requests` 库 |
| **WebDAV XML** | `lxml` 库 |
| **TOML 解析** | `tomli` 库（Python < 3.11 回退） |
| **测试** | pytest（见 `src/tests/README.md` 获取测试文件清单，`python -m pytest src/tests --collect-only -q` 获取实时收集数量；`src/tests/`） |

## 源码目录

```
src/
├── main.py                  # 入口
├── app_service_core.py       # 核心同步引擎
├── app_service.py           # re-export 桶
├── config.py                # 配置类
├── database.py              # SQLite bridge.db 管理器
├── webdav_client.py         # OpenList API + WebDAV 客户端
├── area_watchers.py         # Watchdog 事件处理器
├── refresh_service.py       # 周期刷新服务
├── media_renamer.py         # 媒体重命名 + 字幕检测
├── tmdb_client.py           # TMDB API v3 客户端
├── tmdb_watchlist_db.py     # TMDB 待看列表 DB
├── tmdb_watchlist.py        # TMDB 待看列表服务
├── watchlist_match.py       # 待看列表匹配
├── secret_manager.py        # 密钥/凭据管理
├── openlist_login_shared.py # OpenList 登录共享逻辑
├── logger_setup.py          # 日志初始化
├── domain/                  # 领域模块
│   ├── sync/
│   │   └── sync_service.py  # 同步服务
│   └── media/
│       └── subtitle_handler.py  # 字幕处理
├── utils/                   # 工具函数
│   ├── strm_utils.py        # STRM 路径/指纹工具
│   ├── file_utils.py       # 文件系统工具
│   ├── webdav_utils.py      # WebDAV 路径工具
│   ├── error_translator.py  # 错误信息翻译
│   ├── encoding_utils.py   # 编码规范化（NFC/NFD、URL、全角/连续空格）
│   └── bootstrap.py        # 启动引导工具
├── webui/                   # SPA 前端 + HTTP 服务器
└── tests/                   # 测试文件（见 src/tests/README.md）
```

## 前端构建

```bash
cd src/webui
npx vite build    # 生产构建 → ../../dist/
npx vite          # 开发服务器（HMR）
```

**重要**：修改 `src/webui/modules/` 下的文件后必须重新构建。生产服务器从 `dist/assets/` 加载编译文件。

字体子集化（`src/webui/scripts/subset_font.py`）需要 `fonttools`，见 `src/webui/scripts/requirements.txt`。

### 构建产物说明

构建完成后，`dist/` 目录包含：

```
dist/
├── index.html              # SPA 入口页面
├── icon-preview.html       # 图标预览页面（开发用）
└── assets/                 # 编译后的静态资源
    ├── index-XXXX.js       # 入口 JS
    ├── core-XXXX.js        # 核心模块（router/state/api/icons 等）
    ├── dashboard-XXXX.js   # 仪表盘页面
    ├── area-XXXX.js        # A/B/C 区浏览页面
    ├── config-XXXX.js      # 配置页面
    ├── tmdb-XXXX.js        # TMDB 待看列表页面
    ├── login-XXXX.js       # 登录页面
    ├── logs-XXXX.js        # 日志页面
    ├── index-XXXX.css      # 主样式文件
    ├── favicon.ico         # 站点图标
    ├── *.png               # Logo 与品牌图标（如 logo.01-04.png、openlist_strm_bridge.png）
    └── *.woff2             # 字体文件
```

### 开发流程

1. **修改源码**: 编辑 `src/webui/modules/` 下的 JS 文件
2. **构建**: 运行 `npx vite build`
3. **验证**: 启动服务器 `python src/webui/server.py`，访问 WebUI 确认更改生效

### 常见问题

**Q: 修改了 JS 文件但浏览器看不到变化？**
A: 必须重新运行 `npx vite build`。生产服务器从 `dist/assets/` 加载编译文件，不是源文件。

**Q: 如何调试前端代码？**
A: 使用开发服务器 `npx vite`，支持 HMR（热模块替换），修改后自动刷新浏览器。

**Q: 图标预览页面如何使用？**
A: 直接在浏览器打开 `dist/icon-preview.html`，查看所有 SVG 图标的渲染效果。用于验证 `icons.js` 中的 SVG path 是否正确。

## 测试

测试依赖需单独安装：

```bash
pip install -r src/tests/requirements-dev.txt
```

> 包含 `pytest`（测试框架）、`flask`（Mock 服务器，仅 `test_tmdb_api.py` 使用）、`pytest-cov`（覆盖率报告，可选）。

`src/tests/run_tests.bat` 封装了 pytest 调用：

```bash
src/tests/run_tests.bat            # 运行全部测试
src/tests/run_tests.bat --cov      # 额外生成覆盖率报告
```

也可直接使用 pytest：

```bash
python -m pytest src/tests/ -v
```

测试文件覆盖（见 `src/tests/README.md` 获取完整清单；`python -m pytest src/tests --collect-only -q` 获取实时收集数量）：配置加载、数据库 CRUD、指纹计算、WebDAV 路径解析、字幕语言检测、媒体重命名、待看列表匹配，以及：

- **FTS5 中文搜索**：`test_fts5_search.py`（黑暗/暗黑分词语义 `test_search_dark_vs_reverse`、`test_simple_version_readable`）、`test_fts5_escape_and_tmdb_search.py`（真实媒体名转义，如 `进击的巨人[限制级]`、`电影：测试*`、`Spy×Family`）。
- **simple 分词器加载与版本**：`src/tokenizers/simple/` 下的 `simple.dll` + `VERSION` + `README.md`，由 `database.py` 的 `_load_simple_tokenizer` 与 `tmdb_watchlist_db.py` 的 `_load_simple_into` 加载，版本可读性由 `test_simple_version_readable` 校验。
- **孤儿行清理**：`test_fts_orphan_cleanup.py` 验证 FTS 索引与业务表的孤儿记录清理逻辑。
- **新手引导端到端**：`test_onboarding_e2e.py` 覆盖新手引导全流程的端到端测试。
- **噪音标签剥离**：`media_renamer.py` 的 `suggest_rename` 在提取季集号前，会先剥离文件名中的噪音标签（分辨率/编码/音频等），避免 `1920x1080` 被误解析为 `S20E1080`。剥离逻辑由 `_strip_noise_tags` 函数实现，使用 `NOISE_TAG_PATTERNS` 常量定义的正则模式。
- **人工处理清单**：当 `scan_a_to_b_full_sync` 检测到目标路径冲突时，会在 B 区根目录生成 `_MANUAL_REVIEW_YYYYMMDD_HHMMSS.md` 清单文件，列出被跳过的 A 源路径、WebDAV 路径、目标路径和原因，供用户人工处理。

## 代码规范

### 锁顺序

引擎使用严格的 6 级锁层次（定义在 `AppService.__init__` 方法中）：

```
获取顺序：1._path_locks_lock → 2._path_locks[path] → 3._dav_write_lock
         → 4._cleanup_lock → 5._restoring_lock → 6._lineage_log_lock
```

新代码必须遵守此顺序，违反会导致死锁。

### 数据库连接

始终使用上下文管理器：
```python
# 只读（WAL 并发）
with db.read_connection() as conn:
    cur = conn.execute("SELECT ...")

# 写（串行化）
with db.lock, db.connection() as conn:
    conn.execute("INSERT INTO ...")
```

### 前端 API 调用

始终使用 `api()` 封装：
```javascript
import { api } from '../core/api.js';
const data = await api('/api/endpoint');
```

自动附加鉴权 Token，自动处理 401。

### 渲染过时检测

长时间运行的渲染器应检查过时：
```javascript
import { isRenderStale } from '../core/router.js';
const gen = _renderGen;
const data = await fetchData();
if (isRenderStale(gen)) return; // 用户已导航离开
```

## 常见陷阱

1. **未重新构建 dist**：修改 `src/webui/modules/*.js` 后必须运行 `npx vite build`，否则浏览器看不到更改。
2. **服务器多线程但 DB 锁定**：Python 的 `ThreadingHTTPServer`（多线程）可并发处理请求，但长时间运行的操作（如 TMDB 同步）会持有数据库锁，可能阻塞其他请求。
3. **SQLite WAL 文件**：不要删除 `-shm` 或 `-wal` 伴生文件，它们是 WAL 模式必需的。
4. **配置分层**：DB 配置覆盖 config.toml。如果修改了 config.toml 但未生效，请检查 DB 的 `webui_config` 表。
5. **密码重置**：管理员密码哈希存储在 `tmdb_watchlist.db` → `webui_config`，scope='ui'、key='admin_password'。使用 `reset_admin.py` 重置。

## 部署

### Windows

两个启动脚本：
- `嵌入式启动.bat` — 使用嵌入式 Python 环境
- `环境变量启动.bat` — 使用系统 Python

均提供启动模式选择菜单（WebUI 仅模式 vs 完整模式）。

### 网络绑定

默认 `0.0.0.0:8579` 使 WebUI 在局域网可访问。仅本地访问时改为 `127.0.0.1`。