# 十二、开发指南

## 技术栈

| 层 | 技术 |
|----|------|
| **语言** | Python 3.11+（后端）、JavaScript（前端） |
| **前端构建** | Vite 8.x，Vanilla JS（无 React/Vue） |
| **后端 HTTP** | Python 标准库 `http.server`（单线程） |
| **数据库** | SQLite（WAL 模式，两个文件） |
| **文件监控** | `watchdog` 库 |
| **HTTP 客户端** | `requests` 库 |
| **WebDAV XML** | `lxml` 库 |
| **二次验证** | `pyotp` 库 |
| **测试** | pytest（20 个测试文件，`src/tests/`） |

## 源码目录

```
src/
├── main.py                  # 入口
├── app_service_core.py      # 核心同步引擎
├── app_service.py           # re-export 桶
├── config.py                # 配置类
├── database.py              # SQLite bridge.db 管理器
├── webdav_client.py         # OpenList API + WebDAV 客户端
├── area_watchers.py         # Watchdog 事件处理器
├── refresh_service.py       # 周期刷新服务
├── media_renamer.py         # 媒体重命名 + 字幕检测
├── sync_service.py          # 同步服务（domain/sync/）
├── subtitle_handler.py      # 字幕处理（domain/media/）
├── tmdb_client.py           # TMDB API v3 客户端
├── tmdb_watchlist_db.py     # TMDB 待看列表 DB
├── watchlist_match.py       # 待看列表匹配
├── utils/                   # 工具函数
├── webui/                   # SPA 前端 + HTTP 服务器
└── tests/                   # 20 个测试文件
```

## 前端构建

```bash
cd src/webui
npx vite build    # 生产构建 → ../../dist/
npx vite          # 开发服务器（HMR）
```

**重要**：修改 `src/webui/modules/` 下的文件后必须重新构建。生产服务器从 `dist/assets/` 加载编译文件。

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

```bash
pytest src/tests/ -v
```

20 个测试文件覆盖：配置加载、数据库 CRUD、指纹计算、WebDAV 路径解析、字幕语言检测、媒体重命名、待看列表匹配。

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
2. **服务器单线程**：Python 的 `http.server` 一次处理一个请求，长时间运行的 TMDB 同步会阻塞服务器。
3. **SQLite WAL 文件**：不要删除 `-shm` 或 `-wal` 伴生文件，它们是 WAL 模式必需的。
4. **配置分层**：DB 配置覆盖 config.toml。如果修改了 config.toml 但未生效，请检查 DB 的 `webui_config` 表。
5. **密码重置**：管理员密码哈希存储在 `tmdb_watchlist.db` → `webui_config`，scope='ui'、key='admin_password'。使用 `reset_admin.py` 重置。

## 部署

### Windows

两个启动脚本：
- `嵌入式启动.bat` — 使用 `src/python_embed/` 中的 Python 3.14
- `环境变量启动.bat` — 使用系统 Python

均提供启动模式选择菜单（WebUI 仅模式 vs 完整模式）。

### 网络绑定

默认 `0.0.0.0:8579` 使 WebUI 在局域网可访问。仅本地访问时改为 `127.0.0.1`。