"""
WebUI 管理面板 - 轻量级 HTTP 服务器
仅监听 localhost 和局域网，不开放公网。
使用 Python 内置 http.server，无需额外依赖。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import TYPE_CHECKING
from urllib.parse import urlparse, parse_qs
import urllib.request

if TYPE_CHECKING:
    from config import WebUIConfig
    from database import Database


def _is_lan_ip(ip: str) -> bool:
    """检查 IP 是否属于局域网地址段"""
    if ip in ("127.0.0.1", "::1", "localhost"):
        return True
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    # 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16
    if a == 10:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    if a == 169 and b == 254:
        return True
    return False


class _WebUIHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""
    webui: WebUIServer

    def log_message(self, format, *args):
        """抑制默认日志输出"""
        pass

    def _is_client_allowed(self) -> bool:
        """只允许本机和局域网来源访问，避免公网暴露。"""
        client_ip = self.client_address[0] if self.client_address else ""
        return _is_lan_ip(client_ip)

    def _check_auth(self) -> bool:
        """检查密码认证"""
        pwd = self.webui._password
        if not pwd:
            return True
        cookie = self.headers.get("Cookie", "")
        return f"webui_auth={pwd}" in cookie

    def _guard_request(self, allow_login: bool = False) -> bool:
        """统一访问保护：限制来源 IP + 可选密码认证。"""
        if not self._is_client_allowed():
            self._send_json({"error": "forbidden: only localhost/LAN is allowed"}, 403)
            return False
        if allow_login:
            return True
        if not self._check_auth():
            self._redirect("/login")
            return False
        return True

    def _send_json(self, data: dict | list, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, url: str) -> None:
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)

        if path == "/login":
            if not self._guard_request(allow_login=True):
                return
            self._handle_login_page()
        elif path == "/api/login":
            if not self._guard_request(allow_login=True):
                return
            self._handle_login(params)
        elif path == "/api/logout":
            if not self._guard_request():
                return
            self._handle_logout()
        elif path == "/api/dashboard":
            if not self._guard_request():
                return
            self._handle_dashboard()
        elif path == "/api/records/a":
            if not self._guard_request():
                return
            self._handle_records("a")
        elif path == "/api/records/b":
            if not self._guard_request():
                return
            self._handle_records("b")
        elif path == "/api/records/c":
            if not self._guard_request():
                return
            self._handle_records("c")
        elif path == "/api/records/subtitles":
            if not self._guard_request():
                return
            self._handle_subtitles()
        elif path == "/api/config":
            if not self._guard_request():
                return
            self._handle_config()
        elif path == "/api/search":
            if not self._guard_request():
                return
            self._handle_search(params)
        elif path == "/api/logs":
            if not self._guard_request():
                return
            self._handle_logs(params)
        elif path == "/api/bing-wallpapers":
            if not self._guard_request():
                return
            self._handle_bing_wallpapers()
        elif path == "/" or path == "":
            if not self._guard_request():
                return
            self._handle_index()
        else:
            if not self._guard_request():
                return
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/login":
            if not self._guard_request(allow_login=True):
                return
        else:
            if not self._guard_request():
                return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}

        if path == "/api/login":
            self._handle_login(data)
        elif path == "/api/delete/b":
            self._handle_delete_b(data)
        elif path == "/api/delete/a":
            self._handle_delete_a(data)
        elif path == "/api/cleanup/ghosts":
            self._handle_cleanup_ghosts()
        else:
            self._send_json({"error": "not found"}, 404)

    # ==================== 页面 ====================

    def _handle_index(self) -> None:
        self._send_html(_DASHBOARD_HTML)

    def _handle_login_page(self) -> None:
        self._send_html(_LOGIN_HTML)

    # ==================== API ====================

    def _handle_login(self, params: dict) -> None:
        raw_password = params.get("password", "")
        if isinstance(raw_password, list):
            password = raw_password[0] if raw_password else ""
        else:
            password = str(raw_password)
        if password == self.webui._password:
            body = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", f"webui_auth={password}; Path=/; Max-Age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send_json({"ok": False, "error": "密码错误"}, 401)

    def _handle_logout(self) -> None:
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "webui_auth=; Path=/; Max-Age=0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_dashboard(self) -> None:
        db: Database = self.webui._db
        try:
            counts = db.get_table_counts()
            b_status = db.get_b_status_counts()
            db_size = db.get_db_file_size()
            self._send_json({
                "table_counts": counts,
                "b_status_counts": b_status,
                "db_file_size": db_size,
                "db_file_size_human": _human_size(db_size),
                "uptime": time.time() - self.webui._start_time,
            })
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_records(self, area: str) -> None:
        db: Database = self.webui._db
        try:
            if area == "a":
                records = db.get_all_a_records()
                items = [{"local_path": r[0], "webdav_path": r[1],
                          "parent_webdav_path": r[2], "updated_at": r[3]}
                         for r in records]
            elif area == "b":
                records = db.get_all_b_records()
                items = [{"local_path": r[0], "webdav_path": r[1],
                          "parent_webdav_path": r[2], "source_a_path": r[3],
                          "fingerprint": r[4], "status": r[5],
                          "updated_at": r[6]}
                         for r in records]
            elif area == "c":
                records = db.get_all_c()
                items = [{"local_path": r[0], "webdav_path": r[1],
                          "original_b_path": r[2], "ghost_root": r[3],
                          "moved_at": r[4]}
                         for r in records]
            else:
                items = []
            self._send_json({"count": len(items), "items": items})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_subtitles(self) -> None:
        db: Database = self.webui._db
        try:
            with db.lock, db.connection() as conn:
                cur = conn.execute("SELECT * FROM subtitles")
                cols = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchall()
                items = [dict(zip(cols, row)) for row in rows]
            self._send_json({"count": len(items), "items": items})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_config(self) -> None:
        cfg = self.webui._config
        self._send_json({
            "b_root": cfg.paths.b_root,
            "c_root": cfg.paths.c_root,
            "a_folders": cfg.a_folders,
            "strm_engine_paths": cfg.strm_engine_paths,
            "refresh_paths": cfg.refresh_paths,
            "webdav_host": cfg.webdav.host,
            "webdav_user": cfg.webdav.user,
            "refresh_enabled": cfg.refresh.enabled,
            "refresh_interval": cfg.refresh.interval_seconds,
            "behavior_action": cfg.behavior.action,
            "ghost_protect_seconds": cfg.behavior.ghost_protect_seconds,
        })

    def _handle_search(self, params: dict) -> None:
        q = params.get("q", [""])[0].strip()
        area = params.get("area", ["all"])[0]
        if not q:
            self._send_json({"error": "缺少搜索关键词 q"}, 400)
            return
        db: Database = self.webui._db
        results = []
        try:
            if area in ("a", "all"):
                with db.lock, db.connection() as conn:
                    cur = conn.execute(
                        "SELECT local_path, webdav_path FROM a_strm_files "
                        "WHERE local_path LIKE ? OR webdav_path LIKE ?",
                        (f"%{q}%", f"%{q}%"))
                    for r in cur.fetchall():
                        results.append({"area": "a", "local_path": r[0], "webdav_path": r[1]})
            if area in ("b", "all"):
                with db.lock, db.connection() as conn:
                    cur = conn.execute(
                        "SELECT local_path, webdav_path, fingerprint, status FROM b_strm_files "
                        "WHERE local_path LIKE ? OR webdav_path LIKE ? OR fingerprint LIKE ?",
                        (f"%{q}%", f"%{q}%", f"%{q}%"))
                    for r in cur.fetchall():
                        results.append({"area": "b", "local_path": r[0],
                                        "webdav_path": r[1], "fingerprint": r[2],
                                        "status": r[3]})
            self._send_json({"count": len(results), "results": results})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_logs(self, params: dict) -> None:
        lines = int(params.get("lines", ["200"])[0])
        log_file = self.webui._config.log.file
        try:
            if not os.path.exists(log_file):
                self._send_json({"lines": [], "count": 0})
                return
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            tail = all_lines[-lines:]
            self._send_json({"lines": [l.rstrip() for l in tail], "count": len(tail)})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_delete_b(self, data: dict) -> None:
        local_path = data.get("local_path", "")
        if not local_path:
            self._send_json({"error": "缺少 local_path"}, 400)
            return
        db: Database = self.webui._db
        db.delete_b_by_local(local_path)
        self._send_json({"ok": True, "deleted": local_path})

    def _handle_delete_a(self, data: dict) -> None:
        local_path = data.get("local_path", "")
        if not local_path:
            self._send_json({"error": "缺少 local_path"}, 400)
            return
        db: Database = self.webui._db
        db.delete_a_by_local(local_path)
        self._send_json({"ok": True, "deleted": local_path})

    def _handle_cleanup_ghosts(self) -> None:
        db: Database = self.webui._db
        db.cleanup_expired_ghosts()
        self._send_json({"ok": True})

    def _handle_bing_wallpapers(self) -> None:
        """获取最近 8 天的 Bing 壁纸 URL 列表"""
        try:
            url = "https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=8&mkt=zh-CN"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            images = raw.get("images", [])
            wallpapers = []
            for img in images:
                url_path = img.get("url", "")
                if url_path:
                    wallpapers.append({
                        "url": "https://www.bing.com" + url_path,
                        "copyright": img.get("copyright", ""),
                    })
            self._send_json({"wallpapers": wallpapers})
        except Exception as e:
            self._send_json({"wallpapers": [], "error": str(e)})


class WebUIServer:
    """WebUI HTTP 服务器"""

    def __init__(self, config: WebUIConfig, db: Database, app_config=None) -> None:
        self._config = app_config
        self._db = db
        self._password = config.password
        self._port = config.port
        self._bind = config.bind
        self._enabled = config.enabled
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._start_time = time.time()

    def start(self) -> None:
        if not self._enabled:
            logging.info("[WebUI] 已禁用，跳过启动")
            return
        # 检查绑定地址是否为局域网/本地
        bind_ip = self._bind
        if bind_ip != "127.0.0.1" and bind_ip != "0.0.0.0":
            if not _is_lan_ip(bind_ip):
                logging.warning("[WebUI] 绑定地址 %s 可能不是局域网地址", bind_ip)

        # 捕获 config 和 db 到 handler 类
        handler = type("_BoundHandler", (_WebUIHandler,), {
            "webui": self,
        })

        try:
            self._server = HTTPServer((self._bind, self._port), handler)
            self._server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True, name="WebUI")
            self._thread.start()
            logging.info("[WebUI] 管理面板已启动: http://%s:%s", self._bind, self._port)
        except OSError as e:
            logging.error("[WebUI] 启动失败 (端口 %s): %s", self._port, e)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            logging.info("[WebUI] 已停止")


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ==================== HTML 模板 ====================

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>STRM Bridge 管理面板</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; }
#wallpaper-bg { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; z-index: -2; background: #0f172a; background-size: cover; background-position: center; transition: opacity 2s ease-in-out; }
#wallpaper-overlay { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; z-index: -1; background: linear-gradient(180deg, rgba(15,23,42,0.55) 0%, rgba(15,23,42,0.75) 50%, rgba(15,23,42,0.9) 100%); pointer-events: none; }
#wallpaper-info { position: fixed; bottom: 12px; right: 16px; z-index: 1; font-size: 11px; color: rgba(148,163,184,0.6); max-width: 400px; text-align: right; }
.header { background: linear-gradient(135deg, rgba(30,41,59,0.85) 0%, rgba(15,23,42,0.9) 100%); border-bottom: 1px solid #334155; padding: 16px 24px; display: flex; justify-content: space-between; align-items: center; backdrop-filter: blur(12px); }
.header h1 { font-size: 20px; color: #38bdf8; }
.header .subtitle { font-size: 12px; color: #64748b; margin-top: 2px; }
.header .nav { display: flex; gap: 8px; }
.header .nav button { background: #1e293b; border: 1px solid #475569; color: #94a3b8; padding: 6px 16px; border-radius: 6px; cursor: pointer; font-size: 13px; transition: all 0.2s; }
.header .nav button:hover, .header .nav button.active { background: #334155; color: #38bdf8; border-color: #38bdf8; }
.container { max-width: 1200px; margin: 0 auto; padding: 20px; }
.stats { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; margin-bottom: 20px; }
.stat-card { background: rgba(30,41,59,0.78); border: 1px solid rgba(51,65,85,0.9); border-radius: 10px; padding: 16px; backdrop-filter: blur(12px); box-shadow: 0 10px 30px rgba(0,0,0,0.22); }
.stat-card .label { font-size: 12px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px; }
.stat-card .value { font-size: 28px; font-weight: 700; color: #38bdf8; margin-top: 4px; }
.stat-card .value.green { color: #4ade80; }
.stat-card .value.yellow { color: #fbbf24; }
.stat-card .value.red { color: #f87171; }
.section { background: rgba(30,41,59,0.80); border: 1px solid rgba(51,65,85,0.9); border-radius: 10px; padding: 20px; margin-bottom: 16px; backdrop-filter: blur(12px); box-shadow: 0 10px 30px rgba(0,0,0,0.22); }
.section h2 { font-size: 16px; color: #e2e8f0; margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
.section h2 .icon { font-size: 18px; }
.search-box { display: flex; gap: 8px; margin-bottom: 12px; }
.search-box input { flex: 1; background: #0f172a; border: 1px solid #475569; color: #e2e8f0; padding: 8px 12px; border-radius: 6px; font-size: 13px; }
.search-box input:focus { outline: none; border-color: #38bdf8; }
.search-box select { background: #0f172a; border: 1px solid #475569; color: #e2e8f0; padding: 8px; border-radius: 6px; font-size: 13px; }
.search-box button { background: #2563eb; border: none; color: white; padding: 8px 20px; border-radius: 6px; cursor: pointer; font-size: 13px; }
.search-box button:hover { background: #1d4ed8; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { text-align: left; padding: 8px 10px; border-bottom: 2px solid #475569; color: #94a3b8; font-weight: 600; white-space: nowrap; }
td { padding: 6px 10px; border-bottom: 1px solid #1e293b; color: #cbd5e1; word-break: break-all; max-width: 300px; overflow: hidden; text-overflow: ellipsis; }
tr:hover td { background: rgba(56, 189, 248, 0.05); }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.badge.valid { background: rgba(74, 222, 128, 0.15); color: #4ade80; }
.badge.duplicate { background: rgba(251, 191, 36, 0.15); color: #fbbf24; }
.badge.ghost { background: rgba(248, 113, 113, 0.15); color: #f87171; }
.btn-sm { padding: 3px 10px; border-radius: 4px; border: 1px solid #475569; background: transparent; color: #94a3b8; cursor: pointer; font-size: 11px; }
.btn-sm:hover { background: #334155; color: #f87171; border-color: #f87171; }
.loading { text-align: center; padding: 40px; color: #64748b; }
.log-area { background: #0f172a; border: 1px solid #334155; border-radius: 6px; padding: 12px; font-family: "Cascadia Code", "Fira Code", monospace; font-size: 11px; line-height: 1.6; max-height: 500px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }
.log-line { padding: 1px 0; }
.log-line.info { color: #38bdf8; }
.log-line.warning { color: #fbbf24; }
.log-line.error { color: #f87171; }
.log-line.debug { color: #64748b; }
.pagination { display: flex; justify-content: center; gap: 8px; margin-top: 12px; }
.pagination button { background: #1e293b; border: 1px solid #475569; color: #94a3b8; padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; }
.pagination button:hover { background: #334155; }
.pagination button:disabled { opacity: 0.4; cursor: default; }
.hidden { display: none !important; }
</style>
</head>
<body>
<div id="wallpaper-bg"></div>
<div id="wallpaper-overlay"></div>
<div id="wallpaper-info"></div>
<div class="header">
  <div>
    <h1>🎬 STRM Bridge 管理面板</h1>
    <div class="subtitle">openlist_strm_bridge · 实时监控与管理</div>
  </div>
  <div class="nav">
    <button class="active" onclick="showTab('dashboard')">📊 仪表盘</button>
    <button onclick="showTab('area_b')">📁 B区</button>
    <button onclick="showTab('area_a')">📂 A区</button>
    <button onclick="showTab('area_c')">👻 C区</button>
    <button onclick="showTab('search')">🔍 搜索</button>
    <button onclick="showTab('logs')">📜 日志</button>
    <button onclick="showTab('config')">⚙️ 配置</button>
  </div>
</div>
<div class="container">
  <!-- Dashboard -->
  <div id="tab-dashboard">
    <div class="stats" id="stats-grid"></div>
    <div class="section">
      <h2><span class="icon">📋</span> B区状态分布</h2>
      <div id="b-status"></div>
    </div>
  </div>
  <!-- Area B -->
  <div id="tab-area_b" class="hidden">
    <div class="section">
      <h2><span class="icon">📁</span> B区文件记录 <span id="b-count" style="font-size:12px;color:#64748b"></span></h2>
      <div id="b-table-area"></div>
    </div>
  </div>
  <!-- Area A -->
  <div id="tab-area_a" class="hidden">
    <div class="section">
      <h2><span class="icon">📂</span> A区文件记录 <span id="a-count" style="font-size:12px;color:#64748b"></span></h2>
      <div id="a-table-area"></div>
    </div>
  </div>
  <!-- Area C -->
  <div id="tab-area_c" class="hidden">
    <div class="section">
      <h2><span class="icon">👻</span> C区幽灵文件 <span id="c-count" style="font-size:12px;color:#64748b"></span></h2>
      <div id="c-table-area"></div>
    </div>
  </div>
  <!-- Search -->
  <div id="tab-search" class="hidden">
    <div class="section">
      <h2><span class="icon">🔍</span> 全局搜索</h2>
      <div class="search-box">
        <input id="search-input" placeholder="输入关键词搜索文件路径、指纹..." onkeydown="if(event.key==='Enter')doSearch()">
        <select id="search-area">
          <option value="all">全部区域</option>
          <option value="a">A区</option>
          <option value="b">B区</option>
        </select>
        <button onclick="doSearch()">搜索</button>
      </div>
      <div id="search-results"></div>
    </div>
  </div>
  <!-- Logs -->
  <div id="tab-logs" class="hidden">
    <div class="section">
      <h2><span class="icon">📜</span> 运行日志 <button class="btn-sm" onclick="loadLogs()" style="margin-left:auto">🔄 刷新</button></h2>
      <div class="log-area" id="log-content">加载中...</div>
    </div>
  </div>
  <!-- Config -->
  <div id="tab-config" class="hidden">
    <div class="section">
      <h2><span class="icon">⚙️</span> 运行配置</h2>
      <div id="config-content" style="font-size:13px;line-height:1.8;"></div>
    </div>
  </div>
</div>

<script>
const API = '';
let currentTab = 'dashboard';
let allRecords = {a:[], b:[], c:[]};
const PAGE_SIZE = 100;
let pageState = {a:0, b:0, c:0};

function showTab(tab) {
  document.querySelectorAll('[id^="tab-"]').forEach(el => el.classList.add('hidden'));
  document.getElementById('tab-' + tab).classList.remove('hidden');
  document.querySelectorAll('.nav button').forEach(btn => btn.classList.remove('active'));
  document.querySelectorAll('.nav button').forEach(btn => { if(btn.textContent.includes(tabLabel(tab))) btn.classList.add('active'); });
  currentTab = tab;
  if (tab === 'dashboard') loadDashboard();
  if (tab === 'area_b') loadRecords('b');
  if (tab === 'area_a') loadRecords('a');
  if (tab === 'area_c') loadRecords('c');
  if (tab === 'logs') loadLogs();
  if (tab === 'config') loadConfig();
}

function tabLabel(t) {
  const m = {dashboard:'仪表盘',area_b:'B区',area_a:'A区',area_c:'C区',search:'搜索',logs:'日志',config:'配置'};
  return m[t]||t;
}

async function loadDashboard() {
  try {
    const r = await fetch(API + '/api/dashboard');
    const d = await r.json();
    const counts = d.table_counts || {};
    const grid = document.getElementById('stats-grid');
    grid.innerHTML = `
      <div class="stat-card"><div class="label">A区 STRM</div><div class="value">${counts.a_strm_files||0}</div></div>
      <div class="stat-card"><div class="label">B区 STRM</div><div class="value green">${counts.b_strm_files||0}</div></div>
      <div class="stat-card"><div class="label">C区 幽灵文件</div><div class="value red">${counts.c_ghost_files||0}</div></div>
      <div class="stat-card"><div class="label">身份映射</div><div class="value">${counts.strm_identity||0}</div></div>
      <div class="stat-card"><div class="label">Ghost 保护</div><div class="value yellow">${counts.ghost_protection||0}</div></div>
      <div class="stat-card"><div class="label">字幕记录</div><div class="value">${counts.subtitles||0}</div></div>
      <div class="stat-card"><div class="label">数据库大小</div><div class="value">${d.db_file_size_human}</div></div>
      <div class="stat-card"><div class="label">运行时间</div><div class="value">${fmtDuration(d.uptime)}</div></div>
    `;
    const bs = d.b_status_counts || {};
    const bsArea = document.getElementById('b-status');
    let bsHtml = '<div style="display:flex;gap:12px;flex-wrap:wrap;">';
    for (const [k,v] of Object.entries(bs)) {
      const cls = k === 'valid' ? 'valid' : (k === 'duplicate' ? 'duplicate' : 'ghost');
      bsHtml += `<span class="badge ${cls}">${k}: ${v}</span>`;
    }
    bsHtml += '</div>';
    bsArea.innerHTML = bsHtml;
  } catch(e) { document.getElementById('stats-grid').innerHTML = '<div class="loading">加载失败</div>'; }
}

async function loadRecords(area) {
  const container = document.getElementById(area + '-table-area');
  container.innerHTML = '<div class="loading">加载中...</div>';
  try {
    const r = await fetch(API + '/api/records/' + area);
    const d = await r.json();
    allRecords[area] = d.items || [];
    document.getElementById(area + '-count').textContent = `(${d.count} 条)`;
    pageState[area] = 0;
    renderTable(area);
  } catch(e) { container.innerHTML = '<div class="loading">加载失败</div>'; }
}

function renderTable(area) {
  const container = document.getElementById(area + '-table-area');
  const items = allRecords[area];
  const page = pageState[area];
  const start = page * PAGE_SIZE;
  const end = Math.min(start + PAGE_SIZE, items.length);
  const pageItems = items.slice(start, end);
  const totalPages = Math.ceil(items.length / PAGE_SIZE);

  let html = '<table><thead><tr>';
  if (area === 'a') html += '<th>#</th><th>本地路径</th><th>WebDAV 路径</th><th>更新时间</th><th>操作</th>';
  if (area === 'b') html += '<th>#</th><th>本地路径</th><th>WebDAV 路径</th><th>指纹</th><th>状态</th><th>操作</th>';
  if (area === 'c') html += '<th>#</th><th>本地路径</th><th>原始B路径</th><th>幽灵根</th><th>移动时间</th>';
  html += '</tr></thead><tbody>';
  pageItems.forEach((item, i) => {
    const idx = start + i + 1;
    html += '<tr>';
    html += '<td>' + idx + '</td>';
    if (area === 'a') {
      html += '<td title="' + esc(item.local_path) + '">' + trunc(item.local_path) + '</td>';
      html += '<td title="' + esc(item.webdav_path) + '">' + trunc(item.webdav_path) + '</td>';
      html += '<td>' + fmtTime(item.updated_at) + '</td>';
      html += '<td><button class="btn-sm" onclick="delA(\\'' + esc(item.local_path) + '\\')">删除</button></td>';
    }
    if (area === 'b') {
      html += '<td title="' + esc(item.local_path) + '">' + trunc(item.local_path) + '</td>';
      html += '<td title="' + esc(item.webdav_path) + '">' + trunc(item.webdav_path) + '</td>';
      html += '<td title="' + esc(item.fingerprint||'') + '">' + trunc(item.fingerprint||'-', 16) + '</td>';
      html += '<td><span class="badge ' + (item.status||'valid') + '">' + (item.status||'valid') + '</span></td>';
      html += '<td><button class="btn-sm" onclick="delB(\\'' + esc(item.local_path) + '\\')">删除</button></td>';
    }
    if (area === 'c') {
      html += '<td title="' + esc(item.local_path) + '">' + trunc(item.local_path) + '</td>';
      html += '<td title="' + esc(item.original_b_path) + '">' + trunc(item.original_b_path) + '</td>';
      html += '<td title="' + esc(item.ghost_root) + '">' + trunc(item.ghost_root) + '</td>';
      html += '<td>' + fmtTime(item.moved_at) + '</td>';
    }
    html += '</tr>';
  });
  html += '</tbody></table>';

  if (totalPages > 1) {
    html += '<div class="pagination">';
    html += '<button ' + (page===0?'disabled':'') + ' onclick="goPage(\\'' + area + '\\',' + (page-1) + ')">上一页</button>';
    html += '<span style="color:#64748b;padding:4px 12px;font-size:12px;">第 ' + (page+1) + '/' + totalPages + ' 页 (共 ' + items.length + ' 条)</span>';
    html += '<button ' + (page>=totalPages-1?'disabled':'') + ' onclick="goPage(\\'' + area + '\\',' + (page+1) + ')">下一页</button>';
    html += '</div>';
  }
  container.innerHTML = html;
}

function goPage(area, page) {
  pageState[area] = page;
  renderTable(area);
}

async function delB(p) {
  if (!confirm('确定删除B区记录？\\n' + p)) return;
  await fetch(API + '/api/delete/b', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({local_path:p})});
  loadRecords('b');
}

async function delA(p) {
  if (!confirm('确定删除A区记录？\\n' + p)) return;
  await fetch(API + '/api/delete/a', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({local_path:p})});
  loadRecords('a');
}

async function doSearch() {
  const q = document.getElementById('search-input').value.trim();
  const area = document.getElementById('search-area').value;
  if (!q) return;
  const container = document.getElementById('search-results');
  container.innerHTML = '<div class="loading">搜索中...</div>';
  try {
    const r = await fetch(API + '/api/search?q=' + encodeURIComponent(q) + '&area=' + area);
    const d = await r.json();
    if (d.error) { container.innerHTML = '<div class="loading">' + d.error + '</div>'; return; }
    let html = '<div style="color:#64748b;font-size:12px;margin-bottom:8px;">找到 ' + d.count + ' 条结果</div>';
    html += '<table><thead><tr><th>区域</th><th>本地路径</th><th>WebDAV 路径</th><th>指纹/状态</th></tr></thead><tbody>';
    d.results.forEach(item => {
      html += '<tr>';
      html += '<td><span class="badge ' + (item.area==='b'?'valid':'ghost') + '">' + item.area + '</td>';
      html += '<td title="' + esc(item.local_path) + '">' + trunc(item.local_path) + '</td>';
      html += '<td title="' + esc(item.webdav_path) + '">' + trunc(item.webdav_path) + '</td>';
      html += '<td>' + (item.fingerprint ? trunc(item.fingerprint, 16) : (item.status||'-')) + '</td>';
      html += '</tr>';
    });
    html += '</tbody></table>';
    container.innerHTML = html;
  } catch(e) { container.innerHTML = '<div class="loading">搜索失败</div>'; }
}

async function loadLogs() {
  try {
    const r = await fetch(API + '/api/logs?lines=300');
    const d = await r.json();
    const el = document.getElementById('log-content');
    el.innerHTML = (d.lines||[]).map(l => {
      let cls = '';
      if (l.includes('[ERROR]') || l.includes('ERROR')) cls = 'error';
      else if (l.includes('[WARNING]') || l.includes('WARNING')) cls = 'warning';
      else if (l.includes('[DEBUG]') || l.includes('DEBUG')) cls = 'debug';
      else cls = 'info';
      return '<div class="log-line ' + cls + '">' + esc(l) + '</div>';
    }).join('');
    el.scrollTop = el.scrollHeight;
  } catch(e) { document.getElementById('log-content').textContent = '加载日志失败'; }
}

async function loadConfig() {
  try {
    const r = await fetch(API + '/api/config');
    const d = await r.json();
    const el = document.getElementById('config-content');
    el.innerHTML = Object.entries(d).map(([k,v]) =>
      '<div style="display:flex;border-bottom:1px solid #334155;padding:4px 0;"><span style="color:#64748b;width:220px;">' + k + '</span><span style="color:#e2e8f0;">' + esc(String(Array.isArray(v)?v.join(', '):v)) + '</span></div>'
    ).join('');
  } catch(e) { document.getElementById('config-content').textContent = '加载配置失败'; }
}

function fmtTime(ts) {
  if (!ts) return '-';
  return new Date(ts * 1000).toLocaleString('zh-CN');
}
function fmtDuration(s) {
  if (!s) return '-';
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60);
  return h > 0 ? h + '时' + m + '分' : m + '分' + Math.floor(s%60) + '秒';
}
function trunc(s, n) { n = n || 40; if (!s) return ''; return s.length > n ? s.substring(0, n) + '...' : s; }
function esc(s) { const el = document.createElement('span'); el.textContent = s; return el.innerHTML; }

let bingWallpapers = [];
let wallpaperIndex = 0;
let wallpaperTimer = null;

async function loadBingWallpapers() {
  try {
    const r = await fetch(API + '/api/bing-wallpapers');
    const d = await r.json();
    bingWallpapers = d.wallpapers || [];
    if (bingWallpapers.length > 0) {
      setWallpaper(0);
      if (wallpaperTimer) clearInterval(wallpaperTimer);
      wallpaperTimer = setInterval(nextWallpaper, 45000);
    }
  } catch(e) {
    console.warn('Bing 壁纸加载失败', e);
  }
}

function setWallpaper(index) {
  if (!bingWallpapers.length) return;
  wallpaperIndex = index % bingWallpapers.length;
  const item = bingWallpapers[wallpaperIndex];
  const bg = document.getElementById('wallpaper-bg');
  const info = document.getElementById('wallpaper-info');
  if (!bg || !item) return;
  bg.style.opacity = '0';
  setTimeout(() => {
    bg.style.backgroundImage = 'url("' + item.url + '")';
    bg.style.opacity = '1';
    if (info) info.textContent = item.copyright || '';
  }, 350);
}

function nextWallpaper() {
  if (!bingWallpapers.length) return;
  setWallpaper((wallpaperIndex + 1) % bingWallpapers.length);
}

loadBingWallpapers();
loadDashboard();
</script>
</body>
</html>"""

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>STRM Bridge - 登录</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #e2e8f0; display: flex; justify-content: center; align-items: center; min-height: 100vh; }
.login-box { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 40px; width: 360px; text-align: center; }
.login-box h1 { font-size: 22px; color: #38bdf8; margin-bottom: 8px; }
.login-box p { font-size: 13px; color: #64748b; margin-bottom: 24px; }
.login-box input { width: 100%; padding: 10px 14px; background: #0f172a; border: 1px solid #475569; color: #e2e8f0; border-radius: 6px; font-size: 14px; margin-bottom: 12px; }
.login-box input:focus { outline: none; border-color: #38bdf8; }
.login-box button { width: 100%; padding: 10px; background: #2563eb; border: none; color: white; border-radius: 6px; font-size: 14px; cursor: pointer; }
.login-box button:hover { background: #1d4ed8; }
.error { color: #f87171; font-size: 12px; margin-top: 8px; }
</style>
</head>
<body>
<div class="login-box">
  <h1>🎬 STRM Bridge</h1>
  <p>请输入管理密码</p>
  <input type="password" id="pwd" placeholder="密码" onkeydown="if(event.key==='Enter')doLogin()">
  <button onclick="doLogin()">登录</button>
  <div id="err" class="error"></div>
</div>
<script>
async function doLogin() {
  const pwd = document.getElementById('pwd').value;
  try {
    const r = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({password:pwd})});
    const d = await r.json();
    if (d.ok) location.href = '/';
    else document.getElementById('err').textContent = d.error || '登录失败';
  } catch(e) { document.getElementById('err').textContent = '网络错误'; }
}
</script>
</body>
</html>"""