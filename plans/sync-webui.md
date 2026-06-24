# 同步 test_webui.py → webui.py + 文档更新

## 背景

- `test_webui.py` 是独立测试版，`webui.py` 是被 `main.py` 调用的版本
- test 版本具备所有需要的功能，webui.py 应向 test 版本对齐
- 唯一区别: test 版本能独立启动（含自己的 `main()`），webui.py 依赖 `main.py` 传入 config

## 任务一：webui.py 同步 test 版本的改动

### 1.1 `_send_static_file` — 根据扩展名设置 Content-Type

**现状**：webui.py 写死 `Content-Type: text/html`，`/favicon.ico` `/logo.png` 都会类型错误

**改法**：复制 test_webui.py 的 `ctype_map` 实现，用 `Path(filename).suffix.lower()` 映射

```python
ext = Path(filename).suffix.lower()
ctype_map = {
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}
content_type = ctype_map.get(ext, "application/octet-stream")
```

### 1.2 `do_GET` — 添加 `/favicon.ico` 和 `/logo.png` 路由

**现状**：webui_static/index.html 引用了 `favicon.ico` 和 `logo.png`，但 webui.py 没有对应路由

**改法**：在 `do_GET` 添加：
```python
elif path == "/favicon.ico":
    self._send_static_file("favicon.ico")
elif path == "/logo.png":
    self._send_static_file("logo.png")
```

### 1.3 `start()` — 端口复用（TIME_WAIT 修复）

**现状**：webui.py 在 HTTPServer 创建后 `setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)`，此时 bind 已执行，reuse 未生效。导致重启时 TIME_WAIT 绑定失败。

**改法**：
- 创建 handler class 时设 `handler_cls.allow_reuse_address = True`（让 HTTPServer 在 bind 之前就设置 SO_REUSEADDR）
- 删除创建后 `self._server.socket.setsockopt(...)` 的冗余调用

## 任务二：config.toml 注释更新

**现状**：config.toml 和 config.toml.example 是引擎配置，不含 webui/tmdb 说明

**改法**：
- 在两个文件中找到 `[webui]` 和 `[tmdb]` 节，添加注释解释端口/密码/favicon/logo 等功能的用法
- 不改变任何配置值，只改注释

## 任务三：README 添加 WebUI 章节

在 README.md 新增一节，覆盖：
- WebUI 管理面板功能概览（仪表盘、A/B/C 区浏览、TMDB 待看列表）
- 多季番剧竖杠标识
- favicon/logo 说明
- 访问地址与配置项

## 任务四：wiki 新增「WebUI 管理面板与 TMDB」页面

- 新建 `wiki/WebUI-Management-&-TMDB.md`
- 内容：WebUI 功能描述、A/B/C 三区可视化、TMDB 待看列表、配置覆盖文件 `.tmdb_webui_config.json`、季节数填充机制、favicon 与 logo 定制
- 更新 `_Sidebar.md` 添加链接
