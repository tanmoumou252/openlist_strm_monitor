"""
WebUI 服务器模块（合并自 standalone_webui.py + webui.py + webui_font_proxy.py）。

职责：
1. 提供 FontProxyMixin（字体代理，EdgeOne CDN / 本地 Google Fonts 代理）
2. 提供 _WebUIHandler HTTP 请求处理器（SPA 架构，所有路由分发）
3. 提供 WebUIServer 服务器（启动/停止 HTTP 服务）
4. 提供 main() 独立入口

合并说明：
- webui.py 的 WebUIServer（使用 AppConfig，生产环境）作为主服务器
- webui_font_proxy.py 的 FontProxyMixin 内联为模块级类
- 路由与处理器统一从 webui.routes 引入
- 独立运行模式使用 AppConfig.from_file 加载配置
"""
# autopep8: off
# isort: off
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import secrets
import sys
import threading
import time

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse
import urllib.request

# ============================================================
# 路径设置（必须在项目模块导入之前）
# ============================================================
# webui/ 是 src/webui/，所以 parent 是 src/，parent.parent 是项目根
SRC_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from tmdb_watchlist_db import TmdbWatchlistDb
from watchlist_match import (
    refresh_watchlist_match_state as _refresh_watchlist_match_state,
)

# ============================================================
# 日志
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("webui")

# ============================================================
# 导入项目模块（sys.path 已设置，可以正常导入）
# ============================================================
from tmdb_client import create_tmdb_client  # noqa: E402
from database import Database  # noqa: E402
from app_service_core import AppService  # noqa: E402
# 路由与处理器统一从 webui.routes 引入
from webui.routes import (  # noqa: E402
    _tmdb_routes, _is_lan_ip, _try_bind_port,
    _handle_login, _handle_tmdb_configure, _handle_tmdb_watchlist_match_refresh,
    _handle_tmdb_watchlist_match_override, _handle_tmdb_watchlist_match_clear,
    _handle_tmdb_watchlist_bg_sync,
    _handle_restart_webui, _handle_webui_config_get, _handle_webui_config_post,
    _handle_openlist_test_connection, _handle_openlist_strm_engines,
    _handle_openlist_monitored_paths, _handle_openlist_status,
    _handle_openlist_ping, _handle_openlist_paths,
    _handle_main_status, _handle_main_start, _handle_main_stop,
    _handle_config_status, _handle_config_validate,
    handle_dashboard, handle_area, handle_area_detail, handle_area_refresh,
    handle_records_api, handle_logs_api, handle_download_log_api,
    handle_config_api,
)

if TYPE_CHECKING:
    from config import WebUIConfig

# autopep8: on
# isort: on


# ============================================================
# 静态文件目录（PROJECT_ROOT/dist/）
# ============================================================

STATIC_DIR = PROJECT_ROOT / "dist"

# POST 请求体大小上限（10 MB），防止 Content-Length 攻击导致 OOM（B-5）
_MAX_CONTENT_LENGTH = 10 * 1024 * 1024

# 检查 dist/ 是否存在
if not STATIC_DIR.exists():
    logger.warning(
        "静态文件目录不存在: %s\n"
        "请运行 'cd src/webui && npm run build' 构建前端资源",
        STATIC_DIR
    )


# ============================================================
# 字体代理 Mixin（合并自 webui_font_proxy.py）
# ============================================================

class FontProxyMixin:
    """字体代理 Mixin，供 _WebUIHandler 继承。

    职责：
    1. 读取 [tmdb].host 作为 EdgeOne CDN Host（可选）。
    2. 当配置了 tmdb.host 时，将 /fonts/css/* 和 /fonts/gstatic/* 请求 302 到该 Host。
    3. 当未配置 tmdb.host 时，保持原有本地字体代理逻辑
       （fonts.googleapis.com / fonts.gstatic.com）。
    4. 修复原有本地代理 CSS 请求丢失 query string 的问题。

    要求宿主类提供：
    - self.webui（或 self._config）用于读取配置
    - self.send_response / self.send_header / self.end_headers / self.wfile
    - self.send_error
    - self.headers（HTTP 请求头）
    """

    # ----------------------------------------------------------
    # 配置读取
    # ----------------------------------------------------------
    def _configured_cdn_host(self) -> str:
        """读取 config.toml 中配置的 EdgeOne CDN / Function Host。

        当前项目中复用 [tmdb].host 作为 EdgeOne 反代 Host。
        当该值为空时，表示未启用 EdgeOne 反代。
        """
        # 优先从 self.webui._config 读取
        webui_server = getattr(self, "webui", None)
        cfg = getattr(webui_server, "_config", None) if webui_server else None

        # 如果 webui 没有 _config，尝试直接从 self 读取
        if cfg is None:
            cfg = getattr(self, "_config", None)

        tmdb_cfg = getattr(cfg, "tmdb", None) if cfg else None
        host = str(getattr(tmdb_cfg, "host", "") or "").strip().rstrip("/")

        if host and not host.startswith(("http://", "https://")):
            host = "https://" + host

        return host

    # ----------------------------------------------------------
    # 302 跳转到配置的 CDN Host
    # ----------------------------------------------------------
    def _redirect_to_configured_cdn(self, path: str, query: str = "") -> None:
        """将本地 /fonts/* 请求路由到 config.toml 中配置的 EdgeOne CDN Host。

        用途：
        - HTML 继续使用相对路径 /fonts/css/...
        - 本地 WebUI 根据 [tmdb].host 判断是否启用 EdgeOne
        - 启用后，本地不再直接代理字体上游，而是 302 到 EdgeOne
        """
        cdn_host = self._configured_cdn_host()

        if not cdn_host:
            # type: ignore[attr-defined]
            self.send_error(502, "cdn host not configured")
            return

        location = f"{cdn_host}{path}"
        if query:
            location += f"?{query}"

        self.send_response(302)  # type: ignore[attr-defined]
        self.send_header("Location", location)  # type: ignore[attr-defined]
        # type: ignore[attr-defined]
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Access-Control-Allow-Origin",
            "*")  # type: ignore[attr-defined]
        self.end_headers()  # type: ignore[attr-defined]

    # ----------------------------------------------------------
    # 本地字体代理（未配置 tmdb.host 时使用）
    # ----------------------------------------------------------
    def _proxy_google_font_css(self, path: str, query: str = "") -> None:
        """代理 CSS：/fonts/css/<rest>?<query> → fonts.googleapis.com/<rest>?<query>
        重写 CSS 中的 fonts.gstatic.com URL 为 /fonts/gstatic/ 相对路径。
        失败时返回 502，触发客户端 onerror 回退。
        """
        rest = path[len("/fonts/css/"):]
        url = f"https://fonts.googleapis.com/{rest}"
        if query:
            url += f"?{query}"

        try:
            req = urllib.request.Request(url, headers={
                # type: ignore[attr-defined]
                "User-Agent": self.headers.get("User-Agent", ""),
                "Accept": "text/css,*/*;q=0.1",
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read()
                # 将 CSS 中的 fonts.gstatic.com URL 替换为本地代理路径
                body_text = body.decode("utf-8", errors="replace")
                body_text = body_text.replace(
                    "https://fonts.gstatic.com/", "/fonts/gstatic/")
                body = body_text.encode("utf-8")

            self.send_response(200)  # type: ignore[attr-defined]
            # type: ignore[attr-defined]
            self.send_header("Content-Type", "text/css; charset=utf-8")
            # type: ignore[attr-defined]
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header(
                "Access-Control-Allow-Origin",
                "*")  # type: ignore[attr-defined]
            self.send_header("Content-Length", str(len(body))
                             )  # type: ignore[attr-defined]
            self.end_headers()  # type: ignore[attr-defined]
            try:
                self.wfile.write(body)  # type: ignore[attr-defined]
            except (ConnectionAbortedError, BrokenPipeError):
                pass

        except Exception as e:
            logging.debug("[WebUI] 字体 CSS 代理失败 (%s): %s", url, e)
            # type: ignore[attr-defined]
            self.send_error(502, "font proxy failed")

    def _proxy_google_font_file(self, path: str) -> None:
        """代理字体文件：/fonts/gstatic/<rest> → fonts.gstatic.com/<rest>
        失败时返回 502。
        """
        rest = path[len("/fonts/gstatic/"):]
        url = f"https://fonts.gstatic.com/{rest}"
        try:
            req = urllib.request.Request(url, headers={
                # type: ignore[attr-defined]
                "User-Agent": self.headers.get("User-Agent", ""),
                "Origin": "https://fonts.googleapis.com",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read()
                content_type = resp.headers.get("Content-Type", "font/woff2")
            self.send_response(200)  # type: ignore[attr-defined]
            # type: ignore[attr-defined]
            self.send_header("Content-Type", content_type)
            # type: ignore[attr-defined]
            self.send_header("Cache-Control", "public, max-age=31536000")
            self.send_header(
                "Access-Control-Allow-Origin",
                "*")  # type: ignore[attr-defined]
            self.send_header("Content-Length", str(len(body))
                             )  # type: ignore[attr-defined]
            self.end_headers()  # type: ignore[attr-defined]
            try:
                self.wfile.write(body)  # type: ignore[attr-defined]
            except (ConnectionAbortedError, BrokenPipeError):
                pass

        except Exception as e:
            logging.debug("[WebUI] 字体文件代理失败 (%s): %s", url, e)
            # type: ignore[attr-defined]
            self.send_error(502, "font proxy failed")


# ============================================================
# Handler 类（合并自 webui.py 的 _WebUIHandler 和
#                standalone_webui.py 的 _TestWebUIHandler）
# ============================================================

class _WebUIHandler(FontProxyMixin, BaseHTTPRequestHandler):
    """WebUI HTTP 请求处理器（SPA 架构）。

    合并自 webui.py 和 standalone_webui.py 的处理器，
    两者路由逻辑完全一致，统一在此维护。
    """

    # 由 WebUIServer.start() 动态设置
    webui: "WebUIServer"

    def log_message(self, format, *args):
        pass  # 静默默认日志

    # ----------------------------------------------------------
    # 安全
    # ----------------------------------------------------------
    def _is_client_allowed(self) -> bool:
        ip = self.client_address[0] if self.client_address else ""
        return _is_lan_ip(ip)

    def _guard_request(self) -> bool:
        if not self._is_client_allowed():
            self._send_json({"error": "forbidden"}, 403)
            return False
        return True

    def _check_auth(self) -> bool:
        """检查请求是否已通过密码认证。

        如果未设置密码 → 放行（向后兼容）
        如果已设置密码 → 检查 X-Session-Token 头
        """
        webui = self.webui
        if not webui._has_password:
            return True
        # 标准化路径，匹配路由分发逻辑
        from urllib.parse import urlparse
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        # 登录接口放行
        if path == "/api/login":
            return True
        # 密码状态查询放行
        if path == "/api/admin/status":
            return True
        # 静态资源放行（SPA 需要加载 — 登录前必须可用）
        if path == "/" or path.startswith("/assets/") or path == "/favicon.ico" \
                or path == "/logo.png" or path == "/openlist_strm_bridge.png" \
                or path == "/api/page" or path == "/login" \
                or path.startswith("/fonts/") \
                or path.endswith(".woff2") or path.endswith(".woff") or path.endswith(".ttf"):
            return True
        # API 白名单：登录前初始化和图片代理（非敏感数据）
        if path in ("/api/config", "/api/webui/config/ui",
                    "/api/tmdb/avatar", "/api/tmdb/poster",
                    "/api/openlist/status", "/api/openlist/ping"):
            return True
        # 验证 session token
        token = self.headers.get("X-Session-Token", "")
        now = time.time()
        with webui._sessions_lock:
            # 使用常量时间比较防止时序攻击
            import hmac
            matched_token = None
            for stored_token in webui._sessions:
                if hmac.compare_digest(token.encode('utf-8'), stored_token.encode('utf-8')):
                    matched_token = stored_token
                    break

            if matched_token and now < webui._sessions[matched_token]:
                # 滑动过期：刷新 7 天
                webui._sessions[matched_token] = now + 604800
                return True
        self._send_json({"error": "unauthorized", "need_login": True}, 401)
        return False

    # ----------------------------------------------------------
    # 响应工具
    # ----------------------------------------------------------
    def _send_json(self, data, status=200):
        body = json.dumps(
            data,
            ensure_ascii=False,
            default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError):
            pass  # 客户端提前断开，可忽略

    def _send_html(self, body: str, status=200):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (ConnectionAbortedError, BrokenPipeError) as e:
            # 客户端提前断开连接（如刷新页面、快速切换 tab），静默忽略
            logging.debug("_send_html: 客户端已断开连接: %s", e)

    def _send_static_file(self, filename: str = "index.html", status=200):
        file_path = STATIC_DIR / filename
        try:
            body = file_path.read_bytes()
        except OSError as e:
            logging.error("_send_static_file: 无法读取静态文件 %s: %s", file_path, e)
            self.send_error(500, "static file not found")
            return
        # 根据文件扩展名设置 Content-Type
        ext = Path(filename).suffix.lower()
        ctype_map = {
            ".ico": "image/x-icon",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".svg": "image/svg+xml",
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".woff2": "font/woff2",
            ".woff": "font/woff",
            ".ttf": "font/ttf",
        }
        content_type = ctype_map.get(ext, "application/octet-stream")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        # CORS header for crossorigin attribute in HTML
        self.send_header("Access-Control-Allow-Origin", "*")

        # Cache-Control 策略：
        # - index.html: no-store（始终重新验证）
        # - assets/ 目录下的哈希文件: 长缓存（1年）
        # - 字体文件: 7天缓存
        # - 其他: no-store
        if filename == "index.html":
            self.send_header("Cache-Control", "no-store")
        elif filename.startswith("assets/"):
            # Vite 构建的哈希文件，文件名包含内容哈希
            self.send_header(
                "Cache-Control",
                "public, max-age=31536000, immutable")
        elif ext in (".woff2", ".woff", ".ttf"):
            self.send_header("Cache-Control", "public, max-age=604800")
        else:
            self.send_header("Cache-Control", "no-store")

        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError):
            pass

    def _try_serve_static(self, path: str) -> bool:
        """尝试从 static/ 目录提供静态文件。返回 True 表示已处理，False 表示未找到。"""
        # 安全检查：禁止路径穿越
        if ".." in path or path.startswith("//"):
            return False

        # 移除开头的 /
        fname = path.lstrip("/")
        if not fname:
            return False

        # 检查文件是否存在
        file_path = STATIC_DIR / fname
        if not file_path.is_file():
            return False

        # 确保文件在 STATIC_DIR 内（防止符号链接攻击）
        try:
            file_path.resolve().relative_to(STATIC_DIR.resolve())
        except ValueError:
            return False

        self._send_static_file(fname)
        return True

    # ----------------------------------------------------------
    # 路由分发
    # ----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)

        if not self._guard_request():
            return

        if not self._check_auth():
            return

        # TMDB 路由（复用 webui.routes 的增强版）
        if path.startswith("/api/tmdb/"):
            tmdb_client = getattr(self.webui, '_tmdb_client', None)
            if _tmdb_routes(self, tmdb_client, path, params,
                            webui_server=self.webui):
                return

        # SPA 初始页面（从 dist/index.html 提供）
        if path == "/" or path == "/api/page":
            self._send_static_file()
        elif path == "/login":
            self._send_static_file()
        elif path == "/favicon.ico":
            # publicDir 提供稳定无哈希路径 assets/favicon.ico
            self._send_static_file("assets/favicon.ico")
        elif path == "/logo.png":
            logos = sorted(STATIC_DIR.glob("assets/logo.*.png"))
            if logos:
                self._send_static_file(
                    str(random.choice(logos).relative_to(STATIC_DIR)))
            else:
                logger.error(
                    "_send_static_file: Logos not found in assets, returning 404.")
                self.send_error(404, "Logo not found")
        elif path == "/api/dashboard":
            handle_dashboard(self)
        elif path.startswith("/api/area/"):
            area = path.split("/api/area/")[1].split("/")[0].split("?")[0]
            rest = path.split("/api/area/")[1]
            sub = rest[len(area):] if len(rest) > len(area) else ""
            if sub.startswith("/detail"):
                handle_area_detail(self, area, params)
            elif area:
                handle_area(self, area, params)
            else:
                self._send_json({"error": "not found"}, 404)
        elif path == "/openlist_strm_bridge.png":
            self._send_static_file("assets/openlist_strm_bridge.png")
        elif path.startswith("/fonts/css/"):
            # 配置了 tmdb.host：路由到 EdgeOne CDN
            # 未配置 tmdb.host：保留原来的本地 Google Fonts CSS 代理
            if self._configured_cdn_host():
                self._redirect_to_configured_cdn(path, parsed.query)
            else:
                self._proxy_google_font_css(path, parsed.query)
        elif path.startswith("/fonts/gstatic/"):
            # 配置了 tmdb.host：路由到 EdgeOne CDN
            # 未配置 tmdb.host：保留原来的本地 Google Fonts 字体文件代理
            if self._configured_cdn_host():
                self._redirect_to_configured_cdn(path, parsed.query)
            else:
                self._proxy_google_font_file(path)
        elif path.endswith(".woff2") or path.endswith(".woff") or path.endswith(".ttf"):
            # Font files served from static/
            fname = path.lstrip("/")
            if ".." in fname or "/" in fname or "\\" in fname:
                self._send_json({"error": "invalid path"}, 400)
            else:
                self._send_static_file(fname)
        elif path == "/api/logs":
            handle_logs_api(self, params)
        elif path == "/api/logs/download":
            handle_download_log_api(self, params)
        elif path == "/api/records":
            handle_records_api(self, params)
        elif path == "/api/config":
            handle_config_api(self)
        elif path == "/api/config/status":
            _handle_config_status(self, self.webui)
        elif path.startswith("/api/webui/config/"):
            scope = path.split(
                "/api/webui/config/")[1].split("/")[0].split("?")[0]
            if scope:
                _handle_webui_config_get(self, self.webui, scope)
            else:
                self._send_json({"error": "scope required"}, 400)
        # OpenList API 路由
        elif path == "/api/openlist/status":
            _handle_openlist_status(self, self.webui)
        elif path == "/api/openlist/ping":
            _handle_openlist_ping(self, self.webui)
        elif path == "/api/openlist/strm-engines":
            _handle_openlist_strm_engines(self, self.webui)
        elif path == "/api/openlist/monitored-paths":
            _handle_openlist_monitored_paths(self, self.webui, params)
        elif path == "/api/openlist/paths":
            _handle_openlist_paths(self, self.webui)
        # 主程序控制路由
        elif path == "/api/main/status":
            _handle_main_status(self, self.webui)
        elif path == "/api/admin/status":
            self._send_json({"has_password": self.webui._has_password})
        # 通用静态文件处理（.js / .css / .svg / .png / .jpg / .ico / .woff2 等）
        elif self._try_serve_static(path):
            pass
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if not self._guard_request():
            return
        if not self._check_auth():
            return
        content_length = int(self.headers.get("Content-Length", 0))
        # 防止恶意超大请求体耗尽内存（DoS）— 配置类 JSON 载荷不会超过此值
        if content_length > _MAX_CONTENT_LENGTH:
            self._send_json(
                {"error": "payload too large", "max_bytes": _MAX_CONTENT_LENGTH},
                413,
            )
            return
        body = self.rfile.read(content_length) if content_length else b"{}"
        if path == "/api/login":
            _handle_login(self, self.webui, body)
        elif path == "/api/tmdb/configure":
            _handle_tmdb_configure(self, self.webui, body)
        elif path == "/api/tmdb/watchlist/match/refresh":
            _handle_tmdb_watchlist_match_refresh(self, self.webui)
        elif path == "/api/tmdb/watchlist/match/override":
            _handle_tmdb_watchlist_match_override(self, self.webui, body)
        elif path == "/api/tmdb/watchlist/match/clear":
            _handle_tmdb_watchlist_match_clear(self, self.webui, body)
        elif path == "/api/tmdb/watchlist/sync":
            _handle_tmdb_watchlist_bg_sync(self, self.webui)
        elif path == "/api/restart-webui":
            _handle_restart_webui(self, self.webui)
        elif path == "/api/openlist/test-connection":
            _handle_openlist_test_connection(self, self.webui, body)
        elif path == "/api/config/validate":
            _handle_config_validate(self, self.webui)
        elif path == "/api/onboarding/complete-step":
            from webui.routes import _handle_onboarding_complete_step
            _handle_onboarding_complete_step(self, self.webui, body)
        elif path == "/api/main/start":
            _handle_main_start(self, self.webui, body)
        elif path == "/api/main/stop":
            _handle_main_stop(self, self.webui)
        elif path.startswith("/api/area/") and path.endswith("/refresh"):
            # POST /api/area/{area}/refresh
            parts = path.split("/api/area/")
            if len(parts) == 2:
                rest = parts[1]
                area_and_refresh = rest.split("/")
                if len(area_and_refresh) == 2 and area_and_refresh[1] == "refresh":
                    area = area_and_refresh[0]
                    handle_area_refresh(self, area, body)
                else:
                    self._send_json({"error": "not found"}, 404)
            else:
                self._send_json({"error": "not found"}, 404)
        elif path.startswith("/api/webui/config/"):
            scope = path.split(
                "/api/webui/config/")[1].split("/")[0].split("?")[0]
            if scope:
                _handle_webui_config_post(self, self.webui, scope, body)
            else:
                self._send_json({"error": "scope required"}, 400)
        else:
            self._send_json({"error": "not found"}, 404)


# ============================================================
# 服务器（合并自 webui.py 的 WebUIServer 和
#              standalone_webui.py 的 TestWebUIServer）
# ============================================================

class WebUIServer:
    """WebUI 管理面板服务器。

    使用 AppConfig 加载配置，支持生产环境和独立运行模式。

    通过 app_config 参数区分：
    - 生产环境（main.py）：传入 AppConfig 实例，自动从 DB 加载配置覆盖
    - 独立运行（main()）：传入 AppConfig 实例，从 DB 加载 OpenList 配置
    """

    def __init__(self, config: WebUIConfig, db: Database,
                 app_config=None) -> None:
        self._config = app_config
        self._db = db
        self._port = config.port
        self._bind = config.bind
        self._enabled = config.enabled
        self._start_time = time.time()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._project_root = PROJECT_ROOT

        # AppService 管理（主程序）
        self._app_service: AppService | None = None
        self._app_running = False
        self._app_start_lock = threading.Lock()
        # 主程序启动时间戳（None 表示未运行）；与 WebUIServer._start_time 区分
        self._app_start_time: float | None = None

        # 顺序很重要：
        # 1) 无条件创建 watchlist DB（用于存储 webui_config）
        # 2) 从 DB 读取 TMDB 配置覆盖
        # 3) 从 DB 读取 OpenList 配置覆盖（仅 AppConfig 支持）
        # 4) 用最终配置初始化 TMDB 客户端
        self._tmdb_client: object | None = None
        self._watchlist_db: TmdbWatchlistDb | None = None
        self._sync_lock = threading.Lock()
        self._sync_running = False
        self._match_refresh_lock = threading.Lock()
        self._match_refresh_running = False
        self._match_refresh_result: dict | None = None

        # 尝试查找日志文件（独立运行模式使用）
        self._log_file: str | None = None
        log_candidates = [
            PROJECT_ROOT / "strm_bridge.log",
            SRC_DIR / "strm_bridge.log",
        ]
        for p in log_candidates:
            if p.exists():
                self._log_file = str(p)
                break

        # 认证 & Session
        self._sessions: dict[str, float] = {}
        self._sessions_lock = threading.Lock()
        self._has_password = False

        # 1) 无条件创建 DB（存储配置 + 待看列表数据）
        self._reinit_watchlist_db()

        # 2) 从 DB 加载 TMDB 配置覆盖
        self._raise_on_start_failure = False  # 生产模式：失败时记录日志并返回
        if app_config is not None:
            self._load_db_config()
            # 3) 从 DB 加载 OpenList 配置覆盖
            self._load_openlist_db_config()
        else:
            # 独立运行模式：失败时抛出异常，让调用方感知
            self._raise_on_start_failure = True

        # 4) 用最终配置初始化 TMDB 客户端
        self._init_tmdb_client()

    def _load_db_config(self) -> None:
        """从 DB 的 webui_config 表加载 TMDB 配置覆盖。

        替代原 _load_webui_overrides()（读取 .tmdb_webui_config.json）。
        DB 为唯一配置来源。
        """
        if not self._watchlist_db:
            return
        try:
            db_cfg = self._watchlist_db.get_all_config("tmdb")
            if not db_cfg or not hasattr(self._config, "tmdb"):
                return
            cfg_tmdb = self._config.tmdb
            proxy_changed = False
            for key, val in db_cfg.items():
                if val is None:
                    continue
                # proxy 扁平字段单独处理
                if key == "proxy_http":
                    cfg_tmdb.proxy_http = val or ""
                    cfg_tmdb.proxy.http = val or ""
                    proxy_changed = True
                    continue
                if key == "proxy_enabled":
                    cfg_tmdb.proxy_enabled = str(
                        val).lower() in ("true", "1", "yes")
                    cfg_tmdb.proxy.enabled = cfg_tmdb.proxy_enabled
                    proxy_changed = True
                    continue
                # 数值类型字段
                if key in ("watchlist_cache_ttl", "fuzzy_threshold",
                           "anime_min_ep_ratio", "anime_max_season_diff",
                           "anime_min_season_ratio"):
                    try:
                        setattr(cfg_tmdb, key, float(val))
                    except (ValueError, TypeError):
                        pass
                    continue
                # 其余字段需是 TmdbConfig 已声明槽位，避免 AttributeError
                if val != "" and hasattr(cfg_tmdb, key):
                    setattr(cfg_tmdb, key, val)
            logging.info(
                "[WebUI] 已从 DB 加载 TMDB 配置 (%d 项)"
                + (" (含代理设置)" if proxy_changed else ""),
                len(db_cfg),
            )
        except Exception as e:
            logging.debug("[WebUI] 从 DB 加载 TMDB 配置失败: %s", e)

    def _load_openlist_db_config(self) -> None:
        """从 DB 加载 OpenList 配置覆盖到 AppConfig。"""
        if not self._watchlist_db:
            return
        try:
            self._config.update_from_db(self._watchlist_db)
        except Exception as e:
            logging.debug("[WebUI] 从 DB 加载 OpenList 配置失败: %s", e)

    def _init_tmdb_client(self) -> None:
        """初始化 TMDB 客户端（仅当配置了 API key/token 时）"""
        self._tmdb_client = None  # type: ignore[assignment]
        # 若 watchlist 已禁用，跳过 TMDB 客户端初始化（节省 API 配额）
        if self._watchlist_db:
            enabled_raw = self._watchlist_db.get_config(
                "tmdb", "watchlist_enabled")
            if str(enabled_raw).lower() == "false":
                logging.info("[WebUI] TMDB 待看列表已禁用，跳过客户端初始化")
                return
        try:
            from tmdb_client import create_tmdb_client
            tmdb_cfg = getattr(self._config, "tmdb", None)
            if tmdb_cfg:
                has_token = bool(getattr(tmdb_cfg, "access_token", None))
                has_key = bool(getattr(tmdb_cfg, "api_key", None))
                if has_token or has_key:
                    from webui.routes import _resolve_tmdb_proxy
                    proxy = _resolve_tmdb_proxy(self._config)
                    self._tmdb_client = create_tmdb_client(
                        access_token=getattr(
                            tmdb_cfg, "access_token", "") or "",
                        language=getattr(tmdb_cfg, "language", "zh-CN"),
                        proxy=proxy,
                        host=getattr(tmdb_cfg, "host", ""),
                        api_key=getattr(tmdb_cfg, "api_key", "") or "",
                    )
        except Exception as e:
            logging.debug("[WebUI] TMDB 客户端初始化失败: %s", e)

    def _reinit_watchlist_db(self) -> None:
        """据当前配置重建 TMDB 待看列表 SQLite 数据库。

        DB 无条件创建（用于存储 webui_config 配置），
        固定路径：{project_root}/tmdb_watchlist.db。
        """
        tmdb_cfg = getattr(self._config, "tmdb", None)
        # 固定使用 project_root 下的 tmdb_watchlist.db
        db_path = str(self._project_root / "tmdb_watchlist.db")
        ttl = float(
            getattr(
                tmdb_cfg,
                "watchlist_cache_ttl",
                604800)) if tmdb_cfg else 604800
        try:
            self._watchlist_db = TmdbWatchlistDb(db_path, ttl)
            # 兜底加密：旧版本 DB 可能残留明文凭据
            try:
                self._watchlist_db.migrate_plaintext_to_encrypted()
            except Exception as e:
                logging.warning("[WebUI] 凭据加密迁移失败: %s", e)
        except Exception as e:
            logging.warning("[WebUI] 待看列表数据库初始化失败: %s", e)
            self._watchlist_db = None

    def get_watchlist_cached(self) -> list[dict]:
        """获取待看列表。缓存过期时直接返回旧数据，不自动同步。"""
        if not self._tmdb_client or not self._watchlist_db:
            return []
        # 检查 watchlist_enabled 开关（只有明确设为 "false" 才禁用，未设置/空字符串默认启用）
        enabled_raw = self._watchlist_db.get_config(
            "tmdb", "watchlist_enabled")
        if str(enabled_raw).lower() == "false":
            return []
        return self._watchlist_db.get_all()

    def refresh_watchlist_match_state(self) -> dict[str, int]:
        """刷新收录状态（独立运行模式使用）。"""
        if not self._watchlist_db or not self._db:
            return {"matched": 0, "fuzzy": 0, "unmatched": 0,
                    "uncomputed": 0, "skipped_manual": 0, "total": 0}
        tmdb_cfg = getattr(self._config, "tmdb", None)
        fuzzy = float(
            getattr(
                tmdb_cfg,
                "fuzzy_threshold",
                0.60)) if tmdb_cfg else 0.60
        min_ep = float(
            getattr(
                tmdb_cfg,
                "anime_min_ep_ratio",
                0.3)) if tmdb_cfg else 0.3
        return _refresh_watchlist_match_state(self, fuzzy, min_ep)

    def start(self):
        """启动 WebUI 服务器

        失败时：生产模式记录日志后返回（不中断主程序），
        独立运行模式抛出 RuntimeError（让调用方感知）。
        """
        if not self._enabled:
            logging.info("[WebUI] 已禁用，跳过启动")
            return

        port = self._port
        bind = self._bind

        if bind not in ("127.0.0.1", "0.0.0.0") and not _is_lan_ip(bind):
            logging.warning("[WebUI] 绑定地址 %s 可能不是局域网地址", bind)

        # 端口预检
        if not _try_bind_port(bind, port):
            msg = f"端口 {port} 已被占用，请关闭占用程序或修改 config.toml 中的端口配置"
            if self._raise_on_start_failure:
                raise RuntimeError(msg)
            logging.error("[WebUI] %s", msg)
            return

        # 动态绑定 handler 类
        handler_cls = type("_BoundHandler", (_WebUIHandler,), {})
        handler_cls.webui = self
        handler_cls.allow_reuse_address = True

        try:
            self._server = ThreadingHTTPServer((bind, port), handler_cls)
        except OSError as e:
            err = getattr(
                e,
                'winerror',
                None) or getattr(
                e,
                'errno',
                None) or 0
            if err in (10048, 98):
                msg = f"端口 {port} 已被占用，请关闭占用程序或修改端口配置"
            else:
                msg = f"启动 HTTP 服务器失败: {e}"
            if self._raise_on_start_failure:
                raise RuntimeError(msg) from e
            logging.error("[WebUI] %s", msg)
            return

        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="WebUI")
        self._thread.start()

        # 初始化管理员密码
        self._init_admin_password()

        # 启动 session 过期自动清理（每小时执行一次）
        self._session_cleanup_event = threading.Event()

        def _cleanup_sessions():
            while not self._session_cleanup_event.is_set():
                now = time.time()
                with self._sessions_lock:
                    self._sessions = {
                        k: v for k, v in self._sessions.items() if v > now}
                self._session_cleanup_event.wait(timeout=3600)

        threading.Thread(target=_cleanup_sessions, daemon=True).start()

        tmdb_info = "已配置" if self._tmdb_client else "未配置"
        login_url = f"http://{bind}:{port}"
        logging.info(
            "[WebUI] ╔══════════════════════════════════════════════╗")
        logging.info(
            "[WebUI] ║  管理面板已启动                              ║")
        logging.info(
            "[WebUI] ║  访问地址: %s               ║",
            login_url)
        if self._has_password:
            logging.info(
                "[WebUI] ║  首次访问会自动跳转至登录页面                ║")
        logging.info(
            "[WebUI] ╚══════════════════════════════════════════════╝")
        logging.info("[WebUI] TMDB: %s", tmdb_info)

    @staticmethod
    def _hash_password(password: str) -> str:
        """对密码加盐 PBKDF2-HMAC-SHA256 哈希，返回 salt$iterations$hash 格式。"""
        salt = secrets.token_hex(16)
        iterations = 600000
        h = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            salt.encode(),
            iterations)
        return f"{salt}${iterations}${h.hex()}"

    @staticmethod
    def _check_password(password: str, stored: str) -> bool:
        """验证密码是否与存储的 salt$iterations$hash 匹配。"""
        try:
            parts = stored.split("$", 2)
            if len(parts) != 3:
                return False
            salt, iterations_str, stored_hash = parts
            iterations = int(iterations_str)
            h = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode(),
                salt.encode(),
                iterations)
            return h.hex() == stored_hash
        except (ValueError, AttributeError):
            return False

    def _init_admin_password(self) -> None:
        """检查或生成管理员密码。"""
        if not self._watchlist_db:
            self._has_password = False
            return
        stored = self._watchlist_db.get_config("ui", "admin_password", "")
        if stored:
            self._has_password = True
            logging.info(
                "[WebUI] ============================================")
            logging.info(
                "[WebUI] WebUI 管理面板密码已保存到数据库中")
            logging.info(
                "[WebUI] 忘记密码请运行: python reset_admin.py")
            logging.info(
                "[WebUI] ============================================")
        else:
            # 首次启动，生成随机密码
            # 仅当显式启用测试模式（WEBUI_TEST_MODE=1）时才允许通过环境变量设置密码，
            # 防止生产环境因误设环境变量而使用弱密码。
            test_password = None
            if os.getenv("WEBUI_TEST_MODE") == "1":
                test_password = os.getenv("WEBUI_ADMIN_PASSWORD_FOR_TEST")
                if test_password:
                    logging.warning(
                        "[WebUI] 测试模式：使用环境变量 WEBUI_ADMIN_PASSWORD_FOR_TEST 设置管理员密码")
            if test_password:
                new_password = test_password
            else:
                new_password = secrets.token_urlsafe(12)

            hashed = self._hash_password(new_password)
            self._watchlist_db.set_config("ui", "admin_password", hashed)
            self._has_password = True
            login_url = f"http://{getattr(self,
                                          '_bind',
                                          '0.0.0.0')}:{self._port}"
            logging.info(
                "[WebUI] ╔══════════════════════════════════════════════╗")
            logging.info(
                "[WebUI] ║  WebUI 管理密码已生成                        ║")
            logging.info(
                "[WebUI] ║                                              ║")
            logging.info(
                "[WebUI] ║  访问地址: %s               ║", login_url)
            logging.info(
                "[WebUI] ║  请在浏览器中打开访问地址                    ║")
            logging.info(
                "[WebUI] ║  手动设置密码: python reset_admin.py 1111    ║")
            logging.info(
                "[WebUI] ║  忘记密码请运行: python reset_admin.py       ║")
            logging.info(
                "[WebUI] ╚══════════════════════════════════════════════╝")
            # 密码仅输出到控制台（print），不写入日志文件
            print(f"[WebUI] 管理密码: {new_password}")

    def stop(self):
        """停止 WebUI 服务器"""
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            logging.info("[WebUI] 已停止")
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    # ============================================================
    # AppService 管理（主程序）
    # ============================================================

    def start_main(self) -> dict:
        """启动主程序（AppService）

        Returns:
            {"success": bool, "message": str}
        """
        with self._app_start_lock:
            if self._app_running:
                return {"success": False, "message": "主程序已在运行中"}

            if not self._config:
                return {"success": False, "message": "配置未加载"}
            configured_mappings = getattr(self._config, "a_b_mappings", [])
            if not configured_mappings:
                return {"success": False, "status": "not_configured", "message": "未配置 A/B mapping"}

            try:
                from app_service import AppService
                from webdav_client import OpenListAdminClient

                # 创建 OpenListAdminClient（主程序生命周期内复用）
                admin_client = OpenListAdminClient(
                    self._config.webdav.host,
                    self._config.webdav.user,
                    self._config.webdav.password,
                    totp_secret=self._config.webdav.totp_secret,
                )

                # 登录验证（强制重新登录，不使用缓存 token，确保真实验证连接）
                if not admin_client.login(force=True):
                    error_msg = admin_client.last_error_message or "未知错误"
                    return {"success": False,
                            "message": f"OpenList 登录失败: {error_msg}"}

                # 从 OpenList API 加载 STRM 存储映射（复用 admin_client，避免重复登录）
                try:
                    self._config.load_strm_storage_from_api(
                        admin_client=admin_client)
                except Exception as exc:
                    logging.warning("[Main] 加载 STRM 存储映射失败: %s", exc)

                # 缓存到 WebUIServer 供热更新/状态查询复用
                self._admin_client = admin_client

                # 启动主程序前按配置页的日志级别/路径重新初始化日志系统，
                # 确保 WebUI 启动的主程序不仅输出到控制台，也写入日志文件。
                # log_file 留空时回退 strm_bridge.log（始终写文件）。
                try:
                    from logger_setup import setup_logging
                    setup_logging(
                        level=self._config.log.level,
                        log_file=self._config.log.file or "strm_bridge.log",
                        max_size_mb=self._config.log.max_size_mb,
                        backup_count=self._config.log.backup_count,
                    )
                    logging.info(
                        "[Main] 日志已按配置初始化: level=%s, file=%s",
                        self._config.log.level,
                        self._config.log.file or "strm_bridge.log",
                    )
                except Exception as log_exc:
                    logging.warning("[Main] 日志初始化失败（沿用原配置）: %s", log_exc)

                # 创建 AppService
                self._app_service = AppService(
                    self._config, self._db, admin_client)
                self._app_service.start()

                # 引擎可能在配置未就绪时进入 fail-safe（只 return，不抛异常）。
                # 此时 watcher / 同步全部未启动，不能对外报"已启动"。
                cfg_status = self._app_service.get_config_status()
                if not getattr(self._app_service, "_running", False):
                    reason = cfg_status.get("reason", "配置未就绪")
                    logging.error("[Main] 启动被 fail-safe 拦截: %s", cfg_status)
                    self._app_service = None
                    return {
                        "success": False,
                        "status": str(cfg_status.get("status", "fail_safe_active")),
                        "message": f"主程序未启动：{reason}",
                    }

                self._app_running = True
                self._app_start_time = time.time()

                logging.info("[Main] 主程序已启动")
                return {"success": True, "message": "主程序已启动"}

            except Exception as e:
                logging.error("[Main] 启动失败: %s", e)
                return {"success": False, "message": f"启动失败: {e}",
                        "error_type": "exception"}

    def stop_main(self) -> dict:
        """停止主程序（AppService）

        Returns:
            {"success": bool, "message": str}
        """
        with self._app_start_lock:
            if not self._app_running:
                return {"success": False, "message": "主程序未在运行"}

            try:
                if self._app_service:
                    self._app_service.stop()
                    self._app_service = None
                self._app_running = False
                self._app_start_time = None

                logging.info("[Main] 主程序已停止")
                return {"success": True, "message": "主程序已停止"}

            except Exception as e:
                logging.error("[Main] 停止失败: %s", e)
                return {"success": False, "message": f"停止失败: {e}",
                        "error_type": "exception"}

    def get_main_status(self) -> dict:
        """获取主程序状态

        Returns:
            {"running": bool, "uptime": int | None,
             "refresh_healthy": bool, "refresh_consecutive_failures": int,
             "refresh_last_error": str}
        """
        result: dict = {
            "running": self._app_running,
            "uptime": int(time.time() - self._app_start_time) if self._app_running and self._app_start_time else None,
        }
        # 刷新服务健康状态（主程序运行时才有意义）
        if self._app_running and self._app_service:
            rs = getattr(self._app_service, 'refresh_service', None)
            if rs:
                result["refresh_healthy"] = rs._consecutive_failures == 0
                result["refresh_consecutive_failures"] = rs._consecutive_failures
                result["refresh_last_error"] = rs._last_error_summary
        return result


# ============================================================
# 独立运行入口
# ============================================================

def main():
    """独立运行入口函数（替代原 standalone_webui.py 的 main）"""
    config_path = PROJECT_ROOT / "config.toml"

    if not config_path.exists():
        logger.error("未找到配置文件: %s", config_path)
        sys.exit(1)

    # 加载配置（使用 AppConfig 统一配置系统）
    logger.info("加载配置: %s", config_path)
    from config import AppConfig
    cfg = AppConfig.from_file(str(config_path))

    # 从 DB 加载 TMDB 配置覆盖（DB 为唯一来源，替代 .tmdb_webui_config.json）
    db_path = str(PROJECT_ROOT / "tmdb_watchlist.db")
    try:
        _tmp_db = TmdbWatchlistDb(db_path)
        db_cfg = _tmp_db.get_all_config("tmdb")
        if db_cfg:
            tmdb_cfg = cfg.tmdb
            for key, val in db_cfg.items():
                if val is None or val == "":
                    continue
                if key == "proxy_enabled":
                    tmdb_cfg.proxy_enabled = str(
                        val).lower() in ("true", "1", "yes")
                    continue
                if key in ("watchlist_cache_ttl", "fuzzy_threshold",
                           "anime_min_ep_ratio"):
                    try:
                        setattr(tmdb_cfg, key, float(val))
                    except (ValueError, TypeError):
                        pass
                    continue
                if hasattr(tmdb_cfg, key):
                    setattr(tmdb_cfg, key, val)
            logger.info(
                "[WebUI] 已从 DB 加载 TMDB 配置 (%d 项)", len(db_cfg))
    except Exception as e:
        logger.debug("[WebUI] 从 DB 加载 TMDB 配置失败: %s", e)

    # 初始化数据库（Database 会自动创建不存在的数据库文件）
    logger.info("打开数据库: %s", cfg.local.db_file)
    db = Database(cfg.local.db_file)

    # 从 WebUI 配置 DB 加载 OpenList 配置覆盖（WebUI 配置 > config.toml）
    # 包含 strm_engines、refresh_paths、行为配置等
    try:
        cfg.update_from_db(TmdbWatchlistDb(db_path))
    except Exception as e:
        logger.warning("[WebUI] 从 DB 加载 OpenList 配置失败: %s", e)

    # 初始化 TMDB 客户端
    tmdb_client = None
    if cfg.tmdb.access_token:
        # 配置了自定义 host 时不再需要本地代理
        proxy = None if cfg.tmdb.host else (
            cfg.tmdb.proxy_http if cfg.tmdb.proxy_enabled else None)
        try:
            tmdb_client = create_tmdb_client(
                access_token=cfg.tmdb.access_token,
                language=cfg.tmdb.language,
                proxy=proxy,
                host=cfg.tmdb.host,
                api_key=cfg.tmdb.api_key,
                auto_validate=False,
            )
            logger.info(
                "TMDB 客户端已初始化 (account_id: %s)",
                tmdb_client.account_id)
        except Exception as e:
            logger.warning("TMDB 客户端初始化失败: %s", e)
    else:
        logger.info("未配置 TMDB access_token，跳过初始化")

    # 启动 WebUI
    server = WebUIServer(cfg.webui, db, app_config=cfg)
    try:
        server.start()
    except RuntimeError as e:
        logger.error("[WebUI] %s", e)
        sys.exit(1)

    port = cfg.webui.port
    logger.info("=" * 50)
    logger.info("  管理面板已就绪: http://127.0.0.1:%d", port)
    logger.info("=" * 50)

    # 询问是否自动启动主程序
    auto_start_main = False
    print("\n请选择启动模式:")
    print("  1. 自动启动主程序 (AppService)")
    print("  2. 仅启动 WebUI")
    try:
        choice = input("请输入选项 [1/2] (默认 2): ").strip().lower()
        if choice == "1":
            auto_start_main = True
    except (EOFError, KeyboardInterrupt):
        pass

    # 如果选择自动启动主程序
    if auto_start_main:
        logger.info("正在启动主程序...")
        result = server.start_main()
        if result.get("success"):
            logger.info("主程序已启动")
        else:
            logger.error("主程序启动失败: %s", result.get("message"))

    logger.info("按 Ctrl+C 或输入 q 退出")

    try:
        while True:
            cmd = input().strip().lower()
            if cmd in ("q", "quit", "exit"):
                break
    except (KeyboardInterrupt, EOFError):
        pass

    # 退出时停止主程序（如果在运行）
    if server._app_running:
        logger.info("正在停止主程序...")
        server.stop_main()

    server.stop()
    logger.info("已退出")


if __name__ == "__main__":
    main()
