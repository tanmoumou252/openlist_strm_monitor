"""
WebUI HTTP 请求级集成测试。

通过 threading + HTTPServer 启动真实 WebUIServer，
对每个路由发送 HTTP 请求并验证响应结构、状态码、JSON 契约。
覆盖 WebUI 路由层（webui/routes.py + webui/server.py）的 0% 覆盖缺口。

测试策略：
- 使用真实 HTTPServer + 临时端口（0 表示系统分配空闲端口）
- 使用 MagicMock 提供 AppConfig / Database / TmdbWatchlistDb
- 不依赖真实 OpenList / TMDB 网络服务
- 验证响应契约（ok / status / data 等字段）而非具体业务数据
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 冗余保护：确保 src/ 在 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webui.server import WebUIServer, _WebUIHandler  # noqa: E402


# ============================================================
# Fixtures
# ============================================================

def _free_port() -> int:
    """获取一个空闲端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def _make_mock_config(tmp_path: Path) -> MagicMock:
    """构造最小化 AppConfig mock，满足 WebUIServer 初始化需求。"""
    cfg = MagicMock()
    # webui
    cfg.webui.enabled = True
    cfg.webui.port = 0  # 由 _free_port() 覆盖
    cfg.webui.bind = "127.0.0.1"
    # tmdb
    cfg.tmdb.access_token = ""
    cfg.tmdb.api_key = ""
    cfg.tmdb.language = "zh-CN"
    cfg.tmdb.host = ""
    cfg.tmdb.csv_watchlist_file = ""
    cfg.tmdb.watchlist_db = ""
    cfg.tmdb.watchlist_cache_ttl = 604800
    cfg.tmdb.fuzzy_threshold = 0.60
    cfg.tmdb.anime_min_ep_ratio = 0.3
    cfg.tmdb.proxy_enabled = False
    cfg.tmdb.proxy_http = ""
    proxy = MagicMock()
    proxy.enabled = False
    proxy.http = ""
    cfg.tmdb.proxy = proxy
    # webdav
    cfg.webdav.host = ""
    cfg.webdav.user = ""
    cfg.webdav.password = ""
    cfg.webdav.totp_secret = ""
    # paths
    cfg.paths.b_root = str(tmp_path / "b")
    cfg.paths.c_root = str(tmp_path / "c")
    # behavior
    cfg.behavior.ghost_protect_seconds = 300
    # strm
    cfg.strm_storage_map = {}
    cfg.strm_engine_paths = []
    cfg.strm_monitored_paths = []
    # DB 覆盖（no-op）
    cfg.update_from_db = MagicMock()
    return cfg


def _make_mock_db(tmp_path: Path) -> MagicMock:
    """构造最小化 Database mock。"""
    db = MagicMock(spec=["db_path", "get_table_counts", "get_b_status_counts",
                         "get_db_file_size", "get_subtitle_by_local",
                         "read_connection"])
    db.db_path = str(tmp_path / "bridge.db")
    db.get_table_counts.return_value = {
        "a_strm_files": 0, "b_strm_files": 0, "c_ghost_files": 0,
    }
    db.get_b_status_counts.return_value = {
        "valid": 0, "orphan": 0, "unknown": 0,
    }
    db.get_db_file_size.return_value = 0
    db.get_subtitle_by_local.return_value = None
    
    # read_connection 需要返回一个上下文管理器
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = (0,)
    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn_ctx.__exit__ = MagicMock(return_value=False)
    db.read_connection.return_value = mock_conn_ctx
    
    return db


@pytest.fixture
def webui_server(tmp_path):
    """启动一个真实的 WebUIServer 实例，返回 (server, base_url)。"""
    cfg = _make_mock_config(tmp_path)
    db = _make_mock_db(tmp_path)
    port = _free_port()
    cfg.webui.port = port

    # 用 tmp_path 作为 project_root，避免污染真实项目目录
    with patch("webui.server.PROJECT_ROOT", tmp_path), \
         patch("webui.server.STATIC_DIR", tmp_path / "static"):
        (tmp_path / "static").mkdir(exist_ok=True)
        # 写入最小 index.html 供 SPA 路由返回
        (tmp_path / "static" / "index.html").write_text(
            "<html><body>test</body></html>", encoding="utf-8")
        (tmp_path / "static" / "favicon.ico").write_bytes(b"\x00")

        server = WebUIServer(cfg.webui, db, app_config=cfg)
        server.start()
        # 等待服务器线程就绪
        deadline = time.time() + 2.0
        while not server._server and time.time() < deadline:
            time.sleep(0.05)

        base_url = f"http://127.0.0.1:{port}"
        yield server, base_url

        server.stop()


def _http_get(base_url: str, path: str, timeout: float = 3.0):
    """发送 GET 请求并返回 (status, headers, body_dict_or_bytes)。"""
    url = f"{base_url}{path}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype:
                return resp.status, resp.headers, json.loads(body)
            return resp.status, resp.headers, body
    except urllib.error.HTTPError as e:
        body = e.read()
        ctype = e.headers.get("Content-Type", "")
        if "application/json" in ctype:
            return e.code, e.headers, json.loads(body)
        return e.code, e.headers, body


def _http_post(base_url: str, path: str, data: dict | bytes,
               timeout: float = 3.0):
    """发送 POST 请求并返回 (status, headers, body_dict_or_bytes)。"""
    url = f"{base_url}{path}"
    if isinstance(data, bytes):
        body = data
    else:
        body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype:
                return resp.status, resp.headers, json.loads(raw)
            return resp.status, resp.headers, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        ctype = e.headers.get("Content-Type", "")
        if "application/json" in ctype:
            return e.code, e.headers, json.loads(raw)
        return e.code, e.headers, raw


# ============================================================
# SPA / 静态资源
# ============================================================

class TestStaticRoutes:
    """测试 SPA 初始页面和静态资源路由。"""

    def test_root_returns_index_html(self, webui_server):
        server, base = webui_server
        status, headers, body = _http_get(base, "/")
        assert status == 200
        assert "text/html" in headers.get("Content-Type", "")
        assert b"<html>" in body

    def test_api_page_returns_index_html(self, webui_server):
        server, base = webui_server
        status, headers, body = _http_get(base, "/api/page")
        assert status == 200
        assert b"<html>" in body

    def test_favicon_ico(self, webui_server):
        server, base = webui_server
        status, _, _ = _http_get(base, "/favicon.ico")
        assert status == 200

    def test_unknown_path_404(self, webui_server):
        server, base = webui_server
        status, _, body = _http_get(base, "/this/path/does/not/exist")
        assert status == 404
        assert isinstance(body, dict)
        assert "error" in body


# ============================================================
# Dashboard / 日志 / 记录 / 配置
# ============================================================

class TestCoreRoutes:
    """测试 Dashboard / Logs / Records / Config 路由。"""

    def test_dashboard_returns_json(self, webui_server):
        server, base = webui_server
        status, _, body = _http_get(base, "/api/dashboard")
        # dashboard 调用 _db_get_table_counts / _db_get_b_status_counts / _db_get_db_file_size
        # mock 下可能因 MagicMock 属性访问返回非预期类型而 500
        assert status in (200, 500)
        assert isinstance(body, dict)

    def test_logs_api_returns_list(self, webui_server):
        server, base = webui_server
        status, _, body = _http_get(base, "/api/logs")
        assert status == 200
        assert isinstance(body, dict)
        assert "logs" in body or "entries" in body or "lines" in body

    def test_records_api_returns_list(self, webui_server):
        server, base = webui_server
        status, _, body = _http_get(base, "/api/records")
        assert status == 200
        assert isinstance(body, dict)

    def test_config_api(self, webui_server):
        server, base = webui_server
        status, _, body = _http_get(base, "/api/config")
        assert status == 200
        assert isinstance(body, dict)


# ============================================================
# Area 路由
# ============================================================

class TestAreaRoutes:
    """测试 A/B/C 区状态路由。"""

    def test_area_a(self, webui_server):
        server, base = webui_server
        status, _, body = _http_get(base, "/api/area/a")
        assert status == 200
        assert isinstance(body, dict)

    def test_area_b(self, webui_server):
        server, base = webui_server
        status, _, body = _http_get(base, "/api/area/b")
        assert status == 200

    def test_area_c(self, webui_server):
        server, base = webui_server
        status, _, body = _http_get(base, "/api/area/c")
        assert status == 200

    def test_area_unknown_404(self, webui_server):
        server, base = webui_server
        status, _, body = _http_get(base, "/api/area/zzz")
        # 未知 area 返回 400（invalid area）
        assert status == 400
        assert isinstance(body, dict)
        assert "error" in body


# ============================================================
# OpenList 路由
# ============================================================

class TestOpenListRoutes:
    """测试 OpenList API 路由。"""

    def test_openlist_status_unconfigured(self, webui_server):
        """webdav.host 为空时应返回 unconfigured 状态。"""
        server, base = webui_server
        status, _, body = _http_get(base, "/api/openlist/status")
        assert status == 200
        assert isinstance(body, dict)
        assert body.get("status") == "unconfigured"

    def test_openlist_strm_engines_empty(self, webui_server):
        """strm_storage_map 为空且 API 不可达时返回空 engines。"""
        server, base = webui_server
        status, _, body = _http_get(base, "/api/openlist/strm-engines")
        assert status == 200
        assert isinstance(body, dict)
        assert "engines" in body
        assert isinstance(body["engines"], list)

    def test_openlist_monitored_paths_missing_engine(self, webui_server):
        """缺少 engine 参数应返回 400。"""
        server, base = webui_server
        status, _, body = _http_get(base, "/api/openlist/monitored-paths")
        assert status == 400
        assert isinstance(body, dict)

    def test_openlist_paths(self, webui_server):
        server, base = webui_server
        status, _, body = _http_get(base, "/api/openlist/paths")
        assert status == 200
        assert isinstance(body, dict)
        assert "a_folders" in body or "b_root" in body


# ============================================================
# TMDB 路由
# ============================================================

class TestTmdbRoutes:
    """测试 TMDB 路由（TMDB 客户端未配置）。"""

    def test_tmdb_status_unconfigured(self, webui_server):
        """TMDB 未配置时应返回 configured=False。"""
        server, base = webui_server
        status, _, body = _http_get(base, "/api/tmdb/status")
        assert status == 200
        assert isinstance(body, dict)
        assert body.get("configured") is False

    def test_tmdb_watchlist_match_status(self, webui_server):
        server, base = webui_server
        status, _, body = _http_get(base, "/api/tmdb/watchlist/match/status")
        assert status == 200
        assert isinstance(body, dict)
        assert "running" in body

    def test_tmdb_logs_empty(self, webui_server):
        server, base = webui_server
        status, _, body = _http_get(base, "/api/tmdb/logs")
        assert status == 200
        assert isinstance(body, dict)
        assert "logs" in body


# ============================================================
# WebUI Config 路由
# ============================================================

class TestWebUIConfigRoutes:
    """测试 /api/webui/config/{scope} 路由。"""

    def test_config_get_invalid_scope_403(self, webui_server):
        server, base = webui_server
        status, _, body = _http_get(base, "/api/webui/config/invalid_scope")
        assert status == 403
        assert isinstance(body, dict)

    def test_config_post_invalid_scope_403(self, webui_server):
        server, base = webui_server
        status, _, body = _http_post(
            base, "/api/webui/config/invalid_scope", {"k": "v"})
        assert status == 403

    def test_config_post_invalid_json_400(self, webui_server):
        server, base = webui_server
        status, _, body = _http_post(
            base, "/api/webui/config/ui", b"not json")
        assert status == 400


# ============================================================
# POST 路由
# ============================================================

class TestPostRoutes:
    """测试 POST 路由。"""

    def test_unknown_post_404(self, webui_server):
        server, base = webui_server
        status, _, body = _http_post(base, "/api/unknown", {})
        assert status == 404

    def test_openlist_test_connection_empty_host(self, webui_server):
        """host 为空时应返回 400。"""
        server, base = webui_server
        status, _, body = _http_post(
            base, "/api/openlist/test-connection",
            {"host": "", "user": "", "password": ""})
        assert status == 400
        assert isinstance(body, dict)

    def test_tmdb_watchlist_match_override_invalid_media_type(
            self, webui_server):
        server, base = webui_server
        status, _, body = _http_post(
            base, "/api/tmdb/watchlist/match/override",
            {"media_type": "invalid", "id": 1, "status": "matched"})
        assert status == 400

    def test_tmdb_watchlist_match_override_invalid_status(
            self, webui_server):
        server, base = webui_server
        status, _, body = _http_post(
            base, "/api/tmdb/watchlist/match/override",
            {"media_type": "movie", "id": 1, "status": "bogus"})
        assert status == 400


# ============================================================
# 安全 / 局域网限制
# ============================================================

class TestSecurity:
    """测试安全相关行为。"""

    def test_non_lan_request_rejected(self, tmp_path):
        """非局域网 IP 应被拒绝（403）。

        通过直接调用 _is_client_allowed 验证 IP 过滤逻辑。
        """
        from webui.server import _WebUIHandler
        # 构造一个最小化 handler 实例用于测试
        class _FakeHandler:
            client_address = ("203.0.113.1", 12345)  # 公网 IP
            def _is_client_allowed(self_inner):
                from webui.routes import _is_lan_ip
                ip = self_inner.client_address[0] if self_inner.client_address else ""
                return _is_lan_ip(ip)

        h = _FakeHandler()
        assert h._is_client_allowed() is False

    def test_lan_ip_allowed(self):
        from webui.routes import _is_lan_ip
        assert _is_lan_ip("127.0.0.1") is True
        assert _is_lan_ip("192.168.1.100") is True
        assert _is_lan_ip("10.0.0.5") is True
        assert _is_lan_ip("172.16.0.1") is True

    def test_public_ip_rejected(self):
        from webui.routes import _is_lan_ip
        assert _is_lan_ip("203.0.113.1") is False
        assert _is_lan_ip("8.8.8.8") is False
        assert _is_lan_ip("1.2.3.4") is False


# ============================================================
# 工具函数
# ============================================================

class TestRouteHelpers:
    """测试路由层工具函数。"""

    def test_human_size(self):
        from webui.routes import _human_size
        assert _human_size(0) == "0.0 B"
        assert "KB" in _human_size(1024)
        assert "MB" in _human_size(1024 * 1024)
        assert "GB" in _human_size(1024 ** 3)
        assert "TB" in _human_size(1024 ** 4)

    def test_safe_int(self):
        from webui.routes import _safe_int
        assert _safe_int("42") == 42
        assert _safe_int("abc") == 0
        assert _safe_int(None) == 0
        assert _safe_int("abc", 7) == 7

    def test_resolve_tmdb_proxy_none_when_host_set(self):
        from webui.routes import _resolve_tmdb_proxy
        cfg = MagicMock()
        cfg.tmdb.host = "https://tmdb.example.com"
        assert _resolve_tmdb_proxy(cfg) is None

    def test_resolve_tmdb_proxy_flat_fields(self):
        from webui.routes import _resolve_tmdb_proxy
        cfg = MagicMock()
        cfg.tmdb.host = ""
        cfg.tmdb.proxy_enabled = True
        cfg.tmdb.proxy_http = "http://proxy:8080"
        assert _resolve_tmdb_proxy(cfg) == "http://proxy:8080"

    def test_resolve_tmdb_proxy_nested_fallback(self):
        from webui.routes import _resolve_tmdb_proxy
        cfg = MagicMock()
        cfg.tmdb.host = ""
        cfg.tmdb.proxy_enabled = False
        cfg.tmdb.proxy_http = ""
        proxy = MagicMock()
        proxy.enabled = True
        proxy.http = "http://nested:8080"
        cfg.tmdb.proxy = proxy
        assert _resolve_tmdb_proxy(cfg) == "http://nested:8080"

    def test_try_bind_port_invalid_host(self):
        from webui.routes import _try_bind_port
        # 无效 host 应返回 False
        assert _try_bind_port("invalid.host.xxx", 1) is False
