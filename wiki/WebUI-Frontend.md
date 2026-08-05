# 九、WebUI 前端架构

## 架构概述

WebUI 是一个 **Vanilla JavaScript 单页应用（SPA）**，使用 **Vite 8.x** 构建。无框架（React/Vue/Angular），采用模块化架构，基于 hash 的路由器、共享状态模块、内联 SVG 图标系统和直接 DOM 操作。

```
src/webui/
├── index.html           # SPA 入口（zh-CN）
├── main.js              # 引导
├── vite.config.js       # Vite 构建配置
├── package.json         # 依赖（vite ^8.1.3）
├── styles/main.css      # 双主题 CSS 变量
├── modules/
│   ├── core/            # 核心基础设施
│   │   ├── api.js       # API 封装（含鉴权）
│   │   ├── router.js    # 基于 hash 的 SPA 路由器
│   │   ├── state.js     # 单例状态模块
│   │   ├── theme.js     # MD3/Fluent2 双主题引擎
│   │   ├── utils.js     # 工具函数
│   │   ├── icons.js     # 内联 SVG 图标
│   │   └── wallpaper.js # 水墨晕染遮罩效果
│   ├── pages/           # 页面渲染器
│   │   ├── dashboard.js # 仪表盘
│   │   ├── area.js      # A/B/C 区浏览
│   │   ├── config.js    # 配置页
│   │   ├── login.js     # 登录页
│   │   ├── logs.js      # 日志页（TMDB 操作日志 + 主程序日志双来源）
│   │   ├── openlist.js  # OpenList 配置
│   │   └── tmdb.js      # TMDB 待看列表
│   └── components/      # 可复用组件
│       ├── dialog.js    # 模态对话框
│       └── toast.js     # 提示通知
└── public/              # 静态资源
```

## 构建系统

### Vite 配置

- 根目录：`src/webui/`，基础路径：`./`（相对路径）
- 输出目录：`../../dist`（项目根目录的 `dist/`）
- Rollup `manualChunks`：`core` 块（modules/core/* + modules/components/*），各页面独立块

### 构建命令

```bash
cd src/webui
npx vite build    # 生产构建 → ../../dist/
npx vite          # 开发服务器（HMR）
```

**注意**：修改 `modules/` 下文件后必须重新构建，浏览器加载的是 `dist/` 的编译文件，不是源文件。

## 引导入口（`main.js`）

`main.js` 是前端引导入口，在 `DOMContentLoaded` 中完成初始化。其中鉴权与路由的启动顺序为：**先** `fetch('/api/admin/status')` 获取管理员密码是否已设置（`setHasPassword(d.has_password)`），**再**在该请求的 `.finally` 回调中绑定 `hashchange` 事件并首次调用 `router()`。这样可确保密码状态就绪后再激活路由的 auth guard，避免竞争条件。

此外，`main.js` **静态导入** `openlist.js`（`import { _checkApiStatus } from './modules/pages/openlist.js'`），因此 `openlist.js` 不属于路由懒加载的页面模块，而是随入口一同加载，用于页面渲染后的 API 状态检测。

## 核心模块

### `api.js` — API 客户端
- 支持超时（默认 10s，`AbortController`）
- 自动附加 `X-Session-Token` 头
- 401 时清除 token、跳转到 `#login`、抛出 `ApiAuthError`

### `router.js` — SPA 路由器
- 基于 hash 的路由，含鉴权守卫
- 渲染过时检测（`_renderGen` 计数器）
- 动态导入页面模块：`await import('../pages/xxx.js')`

路由表：
| Hash | 页面模块 | 渲染函数 |
|------|----------|----------|
| `#login` | `login.js` | `renderLogin` |
| `#dashboard` | `dashboard.js` | `renderDashboard` |
| `#area_*` | `area.js` | `renderArea(el, area, params)` |
| `#tmdb` | `tmdb.js` | `renderTmdb(el, params)` |
| `#logs` | `logs.js` | `renderLogs`（TMDB 操作日志 `/api/tmdb/logs` + 主程序日志 `/api/logs` 双来源，Tab 切换） |
| `#config` | `config.js` | `renderConfig(el, params)` |

### `state.js` — 状态管理
- `CONFIG` 常量（轮询间隔、分页大小、缓存限制）
- `OpenListState` — 引擎状态、API 状态
- 鉴权状态 — `_hasPassword`（null = 未初始化）
- TMDB 状态 — 待看列表缓存（30 分钟 TTL）、类型缓存（1000 LRU）
- UI 配置 — 带 `AbortController` 取消进行中的保存

### 表单字段帮助文本系统（`helperText` / `helpKey` / `_openlistHelpTexts`）

OpenList 配置页（`openlist.js`）实现了结构化的帮助文本系统，用于在表单控件下方显示上下文相关的帮助说明：

- **`_openlistHelpTexts`** — 常量对象，定义所有帮助文本的键值对。键名对应控件的 `helpKey`，值为帮助文本字符串。
- **`helpKey`** — `olField()` / `olSelect()` / `olToggle()` 的形参（非 `createField` 配置项）。当渲染 OpenList 表单时，`helpKey` 用于从 `_openlistHelpTexts` 查找帮助文本，查找结果作为 `helperText` 传递给底层 `createField`。
- **`helperText`** — `olField()` / `olSelect()` / `olToggle()` 的形参。与 `helpKey` 互补：`helpKey` 通过字典查找，`helperText` 直接指定文本。两者非竞争关系，`helpKey` 的查找结果优先。
- **帮助图标（tooltip）**：`_olHelpIcon(key)` 根据 `helpKey` 查 `_openlistHelpTexts` 生成 `<span class="ol-help-icon">` tooltip 图标，与下方 `.field-helper-text` div 并存，前者为 hover 提示，后者为常驻文本。
- **渲染位置**：帮助文本渲染为 `<div class="field-helper-text">` 元素，显示在浮动标签输入框的外层容器内，紧接输入框之后。

`utils.js` 的 `createField()` 函数接收 `helperText` 参数并生成对应的 `.field-helper-text` div DOM 结构。`olField()` / `olSelect()` / `olToggle()` 是 OpenList 页面的封装函数，负责 `helpKey`→`_openlistHelpTexts` 查找后再调用 `createField`。

### `theme.js` — 双主题系统
- `syncTheme()` — 应用 `data-system`、`data-color`、`data-font` 到 `<html>`，持久化到 localStorage
- 两个主题：**Material Design 3**（`data-system="material"`）和 **Fluent 2**（`data-system="fluent"`）
- 四种颜色：`blue`、`purple`、`green`、`orange`
- 三种字号：`lg`（15px）、`sm`（13px）、`xs`（11px）

### `wallpaper.js` — 水墨晕染效果
Canvas 水墨鼠标擦除效果（`destination-out` 合成模式）。5 种笔刷变体，1800ms 生命周期，最多 160 个印记，320px 笔刷尺寸。触屏设备跳过效果。

## 鉴权系统

> 注意：以下两项均为后端函数，不属于前端代码。

- **PBKDF2-HMAC-SHA256** 密码哈希（600,000 次迭代）— `server.py` 中 `WebUIServer._hash_password()` 和 `routes.py` 中的配置写入路径均统一调用 `utils.password_utils.hash_password()`，前端不直接调用。

- IP 白名单（仅局域网）— `_is_lan_ip()` 为后端 `routes.py` 中定义的工具函数，`server.py` 导入并复用，前端不涉及。

免 Token 路径：`/api/config`、`/api/webui/config/ui`、`/api/tmdb/avatar`、`/api/tmdb/poster`、`/api/openlist/status`、`/api/openlist/ping`、`/api/admin/status`、`/api/login`、静态资源

## 仪表盘

仪表盘由 `modules/pages/dashboard.js`（`renderDashboard(el)`）负责，对应后端 `GET /api/dashboard`。展示两组统计卡片：

- **A/B/C 区记录数卡**：A 区 STRM 数、B 区 STRM 数、C 区幽灵数、B 区 valid/duplicate/quarantined 分布、TMDB 配置状态、WebUI 运行时间。
- **索引元数据卡（四卡）**：索引代次（`index_generation`）、最近索引时间（`last_full_index_at`，hover 显示精确时间）、映射版本（`mapping_version`，hover 显示完整哈希）、映射版本生成时间（`mapping_version_generated_at`，hover 显示精确时间）。

此外提供 **『立即全量审计』** 按钮：点击后异步触发 `POST /api/index/audit`，按钮进入「审计中...」状态并通过 `GET /api/index/audit/status` 轮询进度，完成后显示「审计完成，索引代次 #N」，失败时显示错误信息。

## 新手引导（Onboarding）

首次使用以分步卡片形式引导，一张卡片含 7 步行，覆盖以下引导步骤：

1. `password` — 确认管理员密码
2. `tmdb` — 配置 TMDB
3. `openlist` — 配置 OpenList
4. `main` — 启动主程序
5. `view_ab` — 浏览 A/B 区
6. `tmdb_refresh` — 刷新 TMDB 待看列表
7. `tmdb_match` — 检测 TMDB 收录状态

**状态读取**：引导状态**不通过独立 status 接口**，而是由前端通过 `GET /api/config/status` 读取（其响应包含 `onboarding_completed` 等键，驱动引导卡片展示）；`GET /api/webui/config/ui`（免 Token）用于读取/写入 UI 配置。关键键位包括 `onboarding_completed`、`view_ab_completed`、`tmdb_refresh_completed`、`tmdb_match_completed`（后三者由后端聚合为 `*_completed` 字段响应；底层存储键为 `onboarding_view_ab_completed` 等，前缀 `onboarding_`）。前端据此决定显示哪些卡片及是否弹出引导。

**步骤完成调用**：单步完成时调用 `POST /api/onboarding/complete-step`，body 为 `{"step": "view_ab"}` 等形式（`step` 仅允许 `view_ab` / `tmdb_refresh` / `tmdb_match`，其它值返回 400）。整体完成或跳过共用 `onboarding_completed` 配置键，通过 `POST /api/webui/config/ui` 写入（如 `{"onboarding_completed": "1"}`）。

> 注意：`onboarding_skipped` 虽仍保留在后端 `_UI_CONFIG_ALLOWED_KEYS` 白名单中，但当前代码无任何位置写入或读取该键，属死键。跳过引导同样写入的是 `onboarding_completed`，并非独立的 `onboarding_skipped`。

> 注意：不存在 `GET /api/onboarding/status` 端点，文档不做虚构。

## 区域浏览与分类搜索

区域浏览由 `modules/pages/area.js`（`renderArea(el, area, params)`）负责，对应后端 `GET /api/area/{area}`。核心特性：

- **分类 Tab**：前端提供「番剧 / 电影 / 全部」等分类 Tab，通过 `kind` 参数切换（`anime` / `movie` / `other` / `all`）。`kind=all` 即「全部分类」Tab，不做类型筛选但后端仍返回各分类真实计数 `kind_counts`。后端分类由 `webdav_path` 路径推断（番剧 / 电影 / 其他）。
- **搜索**：区域搜索框传入 `q` 参数，后端走 FTS5 全文搜索（经 `_escape_fts5_query` 转义，支持中文），FTS5 失败时回退 `LIKE` 子串匹配。
- **空状态提示**：当 `q` 为空或搜索无结果时展示空搜索状态提示，引导用户输入关键词。