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
│   │   ├── logs.js      # TMDB 操作日志
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
| `#logs` | `logs.js` | `renderLogs` |
| `#config` | `config.js` | `renderConfig(el, params)` |

### `state.js` — 状态管理
- `CONFIG` 常量（轮询间隔、分页大小、缓存限制）
- `OpenListState` — 引擎状态、API 状态
- 鉴权状态 — `_hasPassword`（null = 未初始化）
- TMDB 状态 — 待看列表缓存（30 分钟 TTL）、类型缓存（1000 LRU）
- UI 配置 — 带 `AbortController` 取消进行中的保存

### `theme.js` — 双主题系统
- `syncTheme()` — 应用 `data-system`、`data-color`、`data-font` 到 `<html>`，持久化到 localStorage
- 两个主题：**Material Design 3**（`data-system="material"`）和 **Fluent 2**（`data-system="fluent"`）
- 四种颜色：`blue`、`purple`、`green`、`orange`
- 三种字号：`lg`（15px）、`sm`（13px）、`xs`（11px）

### `wallpaper.js` — 水墨晕染效果
Canvas 水墨鼠标擦除效果（`destination-out` 合成模式）。5 种笔刷变体，1800ms 生命周期，最多 160 个印记，320px 笔刷尺寸。触屏设备跳过效果。

## 鉴权系统

- **PBKDF2-HMAC-SHA256** 密码哈希（600,000 次迭代）— `_hash_password()` 方法

- IP 白名单（仅局域网）— `_is_lan_ip()` 函数

免 Token 路径：`/api/config`、`/api/webui/config/ui`、`/api/tmdb/avatar`、`/api/tmdb/poster`、`/api/openlist/status`、`/api/openlist/ping`、`/api/admin/status`、`/api/login`、静态资源