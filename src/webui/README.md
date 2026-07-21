# STRM Bridge WebUI

基于 Vite 构建的现代化 WebUI，采用 ES 模块懒加载架构。

## 目录结构

```
src/webui/
├── index.html                    # 入口（header 骨架 + <script type="module" src="/main.js">）
├── main.js                       # 应用入口（初始化主题、路由、全局事件）
├── styles/
│   └── main.css                  # 从 static/styles.css 迁移
├── modules/
│   ├── core/
│   │   ├── state.js              # OpenListState、_tmdbCache、定时器句柄等共享状态
│   │   ├── icons.js              # ICONS、FILLED_ICONS、BRAND_ICONS、icon()
│   │   ├── theme.js              # syncTheme、initDropdowns
│   │   ├── utils.js              # esc、fmtTime、_formatTimeAgo、createSortLink、createField、copyPathBlock
│   │   ├── api.js                # api() 请求封装
│   │   ├── router.js             # parseHash、navigate、router（懒加载各页面）
│   │   └── wallpaper.js          # 从 wallpaper-reveal.js 迁移为 ES module
│   ├── components/
│   │   ├── toast.js              # showToast
│   │   └── dialog.js             # showConfirmDialog、showCacheStaleModal
│   └── pages/                    # 懒加载页面
│       ├── login.js
│       ├── dashboard.js
│       ├── area.js
│       ├── tmdb.js
│       ├── config.js
│       ├── openlist.js
│       └── logs.js
├── assets/                       # favicon、logo.*.png、openlist_strm_bridge.png
├── vite.config.js
├── package.json
└── README.md

# 构建产物位于项目根目录：
openlist_strm_bridge/
└── dist/                         # 构建产物（提交 git，终端用户使用）
    ├── index.html
    └── assets/
```

## 开发

### 环境要求

- Node.js >= 18

### 安装依赖

```bash
cd src/webui
npm install
```

### 启动开发服务器

```bash
npm run dev
```

开发服务器支持热更新，修改源码后自动刷新。

### 构建生产版本

```bash
npm run build
```

构建产物输出到项目根 `dist/` 目录，包含：
- `index.html` - 精简入口（~7KB）
- `assets/` - 哈希命名的 JS/CSS/图片资源
  - `core-*.js` - 核心模块（~28KB）
  - `index-*.js` - 主入口（~29KB）
  - `dashboard-*.js` - 仪表盘页面（~4KB）
  - `area-*.js` - A/B/C 区页面（~8KB）
  - `tmdb-*.js` - TMDB 页面（~12KB）
  - `config-*.js` - 配置页面（~22KB）
  - `openlist-*.js` - OpenList 配置（~22KB）
  - `logs-*.js` - 日志页面（~2KB）
  - `index-*.css` - 样式（~61KB）

### 预览构建产物

```bash
npm run preview
```

## 部署

终端用户无需 Node.js。项目根 `dist/` 目录已提交到 git，Python 服务器直接读取。

### 缓存策略

- `index.html`: `Cache-Control: no-store`（始终重新验证）
- `assets/*`: `Cache-Control: public, max-age=31536000, immutable`（1年长缓存，文件名含哈希）
- 字体文件: 7天缓存
- 其他: `no-store`

### 懒加载

页面模块按需加载：
- 访问 `#dashboard` 时加载 `dashboard-*.js`
- 访问 `#area_*` 时加载 `area-*.js`
- 访问 `#tmdb` 时加载 `tmdb-*.js`
- 访问 `#config` 时加载 `config-*.js` 和 `openlist-*.js`
- 访问 `#logs` 时加载 `logs-*.js`

首次加载从 ~154KB 降至 ~50KB（index.html + core chunk + index chunk）。

## 修改前端后重新构建

开发者修改源码后，必须重新构建并提交 `dist/`：

```bash
cd src/webui
npm run build
git add ../../dist/
git commit -m "build: rebuild webui"
```

## 架构说明

### 模块依赖

```
main.js
├── theme.js (syncTheme, initDropdowns)
├── router.js (parseHash, navigate, router)
│   └── 动态 import() 各页面模块
├── wallpaper.js (initWallpaperReveal)
└── state.js (共享状态)

页面模块 (dashboard.js, area.js, tmdb.js, config.js, openlist.js, logs.js)
├── api.js
├── icons.js
├── utils.js
├── toast.js
├── dialog.js
└── state.js
```

### 共享状态

`state.js` 导出单例对象：
- `OpenListState` - OpenList 配置状态
- `_tmdbCache` - TMDB 待看列表缓存
- `_serverStartTime`, `_mainStatusTimer`, `_uptimeTimer` - 定时器句柄
- `CONFIG` - 配置常量

页面模块通过 `import` 引用，避免全局变量污染。

### 内联事件处理器

原 `index.html` 中的 4 处 inline `onclick` 已改为 `addEventListener`：
- `navigate('#config?sub=openlist')` → `gear-quick-btn` 在 `main.js` 中绑定
- `startMainProgram()` → `main-start-btn` 在 `renderDashboard` 中绑定
- `stopMainProgram()` → `main-stop-btn` 在 `renderDashboard` 中绑定
- `event.stopPropagation()` → 已移除（TMDB 卡片跳转按钮）

## 故障排查

### 构建失败

```bash
# 清理缓存（从项目根目录运行）
rm -rf src/webui/node_modules dist
cd src/webui && npm install && npm run build
```

### 页面白屏

1. 检查浏览器控制台是否有 JS 错误
2. 确认项目根 `dist/index.html` 存在
3. 确认项目根 `dist/assets/` 下有对应的哈希文件

### 懒加载失败

检查 Network 面板，确认访问对应 hash 时加载了对应的 chunk 文件。

## 范围外

本计划不包含：
- 引入前端框架（Vue/React）
- 启用 gzip/brotli 传输压缩
- PWA / Service Worker
- CI/CD 自动构建
- 重写 CSS 或调整视觉样式
