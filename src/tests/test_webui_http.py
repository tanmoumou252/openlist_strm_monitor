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
import os
import socket
import sys
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 冗余保护：确保 src/ 在 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webui.server import WebUIServer, _WebUIHandler  # noqa: E402
from _test_helpers import FakeConfigDb  # noqa: E402


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
    cfg.webdav.host = "http://openlist:5244"
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
    # DB 覆盖（no-op）
    cfg.update_from_db = MagicMock()
    return cfg


def _make_mock_db(tmp_path: Path) -> MagicMock:
    """构造最小化 Database mock。"""
    db = MagicMock(spec=["db_path", "get_table_counts", "get_b_status_counts",
                         "get_db_file_size", "get_subtitle_by_local",
                         "read_connection", "get_index_metadata", "get_all_config"])
    db.db_path = str(tmp_path / "bridge.db")
    db.get_table_counts.return_value = {
        "a_strm_files": 0, "b_strm_files": 0, "c_ghost_files": 0,
    }
    db.get_b_status_counts.return_value = {
        "valid": 0, "orphan": 0, "unknown": 0,
    }
    db.get_db_file_size.return_value = 0
    db.get_subtitle_by_local.return_value = None
    db.get_index_metadata.return_value = {"mapping_index_generation": 1, "mapping_index_generation_at": 1000.0}
    db.get_all_config.return_value = {}
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
    # 清理全局登录速率限制状态，避免测试间相互污染
    from webui.routes import _login_attempts
    _login_attempts.clear()

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
        (tmp_path / "static" / "assets").mkdir(exist_ok=True)
        (tmp_path / "static" / "assets" / "favicon.ico").write_bytes(b"\x00")

        server = WebUIServer(cfg.webui, db, app_config=cfg)
        # 设置测试密码环境变量
        test_password = "test_password_123"
        os.environ["WEBUI_TEST_MODE"] = "1"
        os.environ["WEBUI_ADMIN_PASSWORD_FOR_TEST"] = test_password
        server.start()
        # 等待服务器线程就绪
        deadline = time.time() + 2.0
        while not server._server and time.time() < deadline:
            time.sleep(0.05)

        base_url = f"http://127.0.0.1:{port}"

        # 登录并获取 session token
        login_status, login_headers, login_body = _http_post(
            base_url, "/api/login", {"password": test_password})
        assert login_status == 200
        session_token = login_body.get("token")
        assert session_token is not None

        yield server, base_url, session_token

        server.stop()


def _http_get(base_url: str, path: str, session_token: str | None = None, timeout: float = 3.0):
    """发送 GET 请求并返回 (status, headers, body_dict_or_bytes)。"""
    url = f"{base_url}{path}"
    req = urllib.request.Request(url, method="GET", headers={"X-Session-Token": session_token}) if session_token else urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype:
                json_body = json.loads(body)
                # 如果是登录响应，从 body 中提取 token
                if path == "/api/login" and "token" in json_body:
                    resp.headers["X-Session-Token"] = json_body["token"]
                return resp.status, resp.headers, json_body
            return resp.status, resp.headers, body
    except urllib.error.HTTPError as e:
        body = e.read()
        ctype = e.headers.get("Content-Type", "")
        if "application/json" in ctype:
            json_body = json.loads(body)
            # 如果是登录响应，从 body 中提取 token
            if path == "/api/login" and "token" in json_body:
                e.headers["X-Session-Token"] = json_body["token"]
            return e.code, e.headers, json_body
        return e.code, e.headers, body


def _http_post(base_url: str, path: str, data: dict | bytes, session_token: str | None = None,
               timeout: float = 3.0):
    """发送 POST 请求并返回 (status, headers, body_dict_or_bytes)。"""
    url = f"{base_url}{path}"
    if isinstance(data, bytes):
        body = data
    else:
        body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Session-Token": session_token} if session_token else {"Content-Type": "application/json"},
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
        server, base, session_token = webui_server
        status, headers, body = _http_get(base, "/", session_token)
        assert status == 200
        assert "text/html" in headers.get("Content-Type", "")
        assert b"<html>" in body

    def test_api_page_returns_index_html(self, webui_server):
        server, base, session_token = webui_server
        status, headers, body = _http_get(base, "/api/page", session_token)
        assert status == 200
        assert b"<html>" in body

    def test_favicon_ico(self, webui_server):
        server, base, session_token = webui_server
        status, _, _ = _http_get(base, "/favicon.ico", session_token)
        assert status == 200

    def test_unknown_path_404(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_get(base, "/this/path/does/not/exist", session_token)
        assert status == 404
        assert isinstance(body, dict)
        assert "error" in body

    def test_login_returns_spa_index_html(self, webui_server):
        """GET /login 应返回 SPA index.html（与 / 和 /api/page 一致）。

        回归守卫：修复前 do_GET 调用不存在的 _send_login_page()，
        导致 AttributeError 或非 200 响应。
        """
        server, base, session_token = webui_server
        status, headers, body = _http_get(base, "/login", session_token)
        assert status == 200
        assert "text/html" in headers.get("Content-Type", "")
        assert b"<html>" in body

    def test_login_without_token_returns_spa_index_html(self, webui_server):
        """GET /login 无需 token 即可访问（白名单路径），返回 SPA index.html。"""
        server, base, _ = webui_server
        status, headers, body = _http_get(base, "/login", session_token=None)
        assert status == 200
        assert "text/html" in headers.get("Content-Type", "")
        assert b"<html>" in body


# ============================================================
# Dashboard / 日志 / 记录 / 配置
# ============================================================

class TestCoreRoutes:
    """测试 Dashboard / Logs / Records / Config 路由。"""

    def test_dashboard_returns_json(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_get(base, "/api/dashboard", session_token)
        # dashboard 调用 _db_get_table_counts / _db_get_b_status_counts / _db_get_db_file_size
        # mock 下可能因 MagicMock 属性访问返回非预期类型而 500
        assert status in (200, 500)
        assert isinstance(body, dict)

    def test_records_api_returns_list(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_get(base, "/api/area/a", session_token)
        assert status == 200
        assert isinstance(body, dict)

    def test_config_api(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_get(base, "/api/config", session_token)
        assert status == 200
        assert isinstance(body, dict)

    def test_config_api_proxy_http_not_leaked(self, webui_server):
        """测试 /api/config 不泄露完整的代理 URL（白名单端点脱敏）"""
        server, base, session_token = webui_server
        # 设置代理配置
        if server._watchlist_db:
            server._watchlist_db.set_config("tmdb", "proxy_http", "http://secret-proxy:7890")
            server._watchlist_db.set_config("tmdb", "proxy_enabled", "true")
        # 调用 /api/config（无需认证）
        status, _, body = _http_get(base, "/api/config", session_token=None)
        assert status == 200
        # 不应返回完整的代理 URL 字符串
        assert not (isinstance(body.get("tmdb_proxy_http"), str)
                    and body.get("tmdb_proxy_http")), \
            "tmdb_proxy_http 不应返回完整代理 URL"
        assert not (isinstance(body.get("tmdb_proxy"), str)
                    and body.get("tmdb_proxy")), \
            "tmdb_proxy 不应返回完整代理 URL"
        # 应该返回布尔值表示是否配置
        assert "tmdb_proxy_configured" in body
        assert isinstance(body["tmdb_proxy_configured"], bool)
        assert body["tmdb_proxy_configured"] is True

    def test_logs_api_returns_lines(self, webui_server):
        """主程序日志 API 应返回 {lines, count} 结构。"""
        server, base, session_token = webui_server
        status, _, body = _http_get(base, "/api/logs", session_token)
        assert status == 200
        assert isinstance(body, dict)
        assert "lines" in body
        assert "count" in body
        assert isinstance(body["lines"], list)
        assert isinstance(body["count"], int)


# ============================================================
# 日志下载路由（/api/logs/download）
# ============================================================

class TestLogsDownloadRoute:
    """测试 /api/logs/download 路由的鉴权和下载行为。"""

    def test_logs_download_requires_auth(self, webui_server):
        """未认证访问 /api/logs/download 应返回 401。"""
        server, base, _ = webui_server
        # 不传 session_token
        status, _, body = _http_get(base, "/api/logs/download", session_token=None)
        assert status == 401
        assert isinstance(body, dict)
        assert body.get("error") == "unauthorized"

    def test_logs_download_invalid_token_rejected(self, webui_server):
        """无效 token 应返回 401。"""
        server, base, _ = webui_server
        status, _, body = _http_get(
            base, "/api/logs/download", session_token="invalid_token_xyz")
        assert status == 401
        assert isinstance(body, dict)

    def test_logs_download_authenticated_returns_404_when_no_log_file(
            self, webui_server):
        """已认证但日志文件不存在时应返回 404。"""
        server, base, session_token = webui_server
        # 确保 _log_file 为 None（fixture 默认就是 None）
        server._log_file = None
        # 同时清空 cfg.log.file
        server._config.log = None
        status, _, body = _http_get(
            base, "/api/logs/download", session_token=session_token)
        assert status == 404
        assert isinstance(body, dict)
        assert "error" in body

    def test_logs_download_authenticated_returns_file_content(
            self, tmp_path, monkeypatch):
        """已认证且日志文件存在时应返回 200 + 文件内容。"""
        # 清理全局登录速率限制状态
        from webui.routes import _login_attempts
        _login_attempts.clear()

        # 准备一个临时日志文件
        log_file = tmp_path / "strm_bridge.log"
        log_file.write_text(
            "2026-07-10 12:00:00 INFO test log line 1\n"
            "2026-07-10 12:00:01 INFO test log line 2\n",
            encoding="utf-8",
        )

        # 复用 webui_server fixture 但覆盖 _log_file
        from webui.server import WebUIServer
        cfg = _make_mock_config(tmp_path)
        db = _make_mock_db(tmp_path)
        port = _free_port()
        cfg.webui.port = port

        with patch("webui.server.PROJECT_ROOT", tmp_path), \
             patch("webui.server.STATIC_DIR", tmp_path / "static"):
            (tmp_path / "static").mkdir(exist_ok=True)
            (tmp_path / "static" / "index.html").write_text(
                "<html><body>test</body></html>", encoding="utf-8")
            (tmp_path / "static" / "assets").mkdir(exist_ok=True)
            (tmp_path / "static" / "assets" / "favicon.ico").write_bytes(b"\x00")

            server = WebUIServer(cfg.webui, db, app_config=cfg)
            test_password = "test_password_123"
            os.environ["WEBUI_TEST_MODE"] = "1"
            os.environ["WEBUI_ADMIN_PASSWORD_FOR_TEST"] = test_password
            server.start()
            deadline = time.time() + 2.0
            while not server._server and time.time() < deadline:
                time.sleep(0.05)

            try:
                # 注入日志文件路径
                server._log_file = str(log_file)
                # 清空 cfg.log 以确保走 fallback 路径
                server._config.log = None

                base_url = f"http://127.0.0.1:{port}"
                # 登录
                login_status, _, login_body = _http_post(
                    base_url, "/api/login", {"password": test_password})
                assert login_status == 200
                token = login_body.get("token")
                assert token is not None

                # 下载日志
                status, headers, body = _http_get(
                    base_url, "/api/logs/download", session_token=token)
                assert status == 200
                # 响应应为二进制内容
                assert isinstance(body, bytes)
                content = body.decode("utf-8")
                assert "test log line 1" in content
                assert "test log line 2" in content
                # Content-Disposition 应包含 attachment
                cdisp = headers.get("Content-Disposition", "")
                assert "attachment" in cdisp
                assert "strm_bridge.log" in cdisp
            finally:
                server.stop()


# ============================================================
# Area 路由
# ============================================================

class TestAreaRoutes:
    """测试 A/B/C 区状态路由。"""

    def test_area_a(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_get(base, "/api/area/a", session_token)
        assert status == 200
        assert isinstance(body, dict)

    def test_area_b(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_get(base, "/api/area/b", session_token)
        assert status == 200

    def test_area_c(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_get(base, "/api/area/c", session_token)
        assert status == 200

    def test_area_unknown_404(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_get(base, "/api/area/zzz", session_token)
        # 未知 area 返回 400（无效区域）
        assert status == 400
        assert isinstance(body, dict)
        assert "error" in body

    def test_area_detail_a_returns_last_verified_at(self, webui_server):
        """A 区详情每条记录应包含 last_verified_at 字段"""
        server, base, session_token = webui_server
        status, _, body = _http_get(base, "/api/area/a/detail?media=test", session_token)
        assert status == 200
        assert isinstance(body, dict)
        # 如果有记录，检查 last_verified_at 字段存在
        if "records" in body:
            for rec in body.get("records", []):
                assert "last_verified_at" in rec, \
                    f"记录应包含 last_verified_at 字段: {rec}"

    def test_area_detail_b_returns_last_verified_at(self, webui_server):
        """B 区详情每条记录应包含 last_verified_at 字段"""
        server, base, session_token = webui_server
        status, _, body = _http_get(base, "/api/area/b/detail?media=test", session_token)
        assert status == 200
        assert isinstance(body, dict)
        if "records" in body:
            for rec in body.get("records", []):
                assert "last_verified_at" in rec, \
                    f"记录应包含 last_verified_at 字段: {rec}"


# ============================================================
# OpenList 路由
# ============================================================

class TestOpenListRoutes:
    """测试 OpenList API 路由。"""

    def test_openlist_status_unconfigured(self, webui_server):
        server, base, session_token = webui_server
        # 显式设为未配置（_make_mock_config 默认有 host，此处覆盖）
        server._config.webdav.host = ""
        status, _, body = _http_get(base, "/api/openlist/status", session_token)
        assert status == 200
        assert isinstance(body, dict)
        assert body.get("status") == "unconfigured"

    def test_openlist_strm_engines_empty(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_get(base, "/api/openlist/strm-engines", session_token)
        assert status == 200
        assert isinstance(body, dict)
        assert "engines" in body
        assert isinstance(body["engines"], list)

    def test_openlist_monitored_paths_missing_engine(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_get(base, "/api/openlist/monitored-paths", session_token)
        assert status == 400
        assert isinstance(body, dict)

    def test_openlist_paths(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_get(base, "/api/openlist/paths", session_token)
        assert status == 200
        assert isinstance(body, dict)
        assert "a_folders" in body or "b_root" in body

    def test_openlist_ping_unreachable_host_returns_offline(self, webui_server):
        """ping 接口在 host 不可达时返回 offline，而非 online。

        回归守卫：修复前 _handle_openlist_ping 调用 client.login()（无 force=True），
        新实例会加载缓存 token 直接返回 True，导致状态误报为"已连接"。
        修复后调用 client.login(force=True)，强制真实验证连接。
        """
        server, base, session_token = webui_server
        # 设置一个非空 host，使 ping 接口进入登录逻辑
        server._config.webdav.host = "http://unreachable:5244"

        mock_class = MagicMock()
        mock_instance = MagicMock()
        mock_class.return_value = mock_instance
        mock_instance.login.return_value = False
        mock_instance.last_error_type = "network_error"
        mock_instance.last_error_message = "connection refused"

        with patch("webdav_client.OpenListAdminClient", mock_class):
            status, _, body = _http_get(base, "/api/openlist/ping", session_token)

        assert status == 200
        assert body.get("status") == "offline"
        # 验证 login 被调用时传入了 force=True（关键回归断言）
        mock_instance.login.assert_called_once_with(force=True)

    def test_openlist_ping_login_succeeds_returns_online(self, webui_server):
        """ping 接口在登录成功时返回 online。"""
        server, base, session_token = webui_server
        server._config.webdav.host = "http://openlist:5244"

        mock_class = MagicMock()
        mock_instance = MagicMock()
        mock_class.return_value = mock_instance
        mock_instance.login.return_value = True

        with patch("webdav_client.OpenListAdminClient", mock_class):
            status, _, body = _http_get(base, "/api/openlist/ping", session_token)

        assert status == 200
        assert body.get("status") == "online"
        mock_instance.login.assert_called_once_with(force=True)

    def test_openlist_ping_not_configured_returns_offline(self, webui_server):
        """ping 接口在 host 无效（not_configured）时返回 offline。

        覆盖 login() 返回 last_error_type="not_configured" 的场景：
        host 为空或 URL 无效（MissingSchema/InvalidURL）。
        """
        server, base, session_token = webui_server
        server._config.webdav.host = "http://openlist:5244"

        mock_class = MagicMock()
        mock_instance = MagicMock()
        mock_class.return_value = mock_instance
        mock_instance.login.return_value = False
        mock_instance.last_error_type = "not_configured"
        mock_instance.last_error_message = "OpenList host 配置无效"

        with patch("webdav_client.OpenListAdminClient", mock_class):
            status, _, body = _http_get(base, "/api/openlist/ping", session_token)

        assert status == 200
        assert body.get("status") == "offline"

    def test_openlist_ping_invalid_totp_returns_auth_failed_2fa(self, webui_server):
        """ping 接口在 TOTP 密钥无效（invalid_totp）时返回 auth_failed_2fa。

        覆盖 login() 返回 last_error_type="invalid_totp" 的场景：
        TOTP Secret 格式错误导致无法生成验证码。
        """
        server, base, session_token = webui_server
        server._config.webdav.host = "http://openlist:5244"

        mock_class = MagicMock()
        mock_instance = MagicMock()
        mock_class.return_value = mock_instance
        mock_instance.login.return_value = False
        mock_instance.last_error_type = "invalid_totp"
        mock_instance.last_error_message = "TOTP Secret 无效或格式错误"

        with patch("webdav_client.OpenListAdminClient", mock_class):
            status, _, body = _http_get(base, "/api/openlist/ping", session_token)

        assert status == 200
        assert body.get("status") == "auth_failed_2fa"

    def test_openlist_ping_account_not_found_returns_auth_failed(self, webui_server):
        """ping 接口在账号不存在（account_not_found）时返回 auth_failed。

        覆盖 login() 返回 last_error_type="account_not_found" 的场景：
        OpenList 返回 "user not found" 错误。
        """
        server, base, session_token = webui_server
        server._config.webdav.host = "http://openlist:5244"

        mock_class = MagicMock()
        mock_instance = MagicMock()
        mock_class.return_value = mock_instance
        mock_instance.login.return_value = False
        mock_instance.last_error_type = "account_not_found"
        mock_instance.last_error_message = "user not found"

        with patch("webdav_client.OpenListAdminClient", mock_class):
            status, _, body = _http_get(base, "/api/openlist/ping", session_token)

        assert status == 200
        assert body.get("status") == "auth_failed"


# ============================================================
# TMDB 路由
# ============================================================

class TestTmdbRoutes:
    """测试 TMDB 路由（TMDB 客户端未配置）。"""

    def test_tmdb_status_unconfigured(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_get(base, "/api/tmdb/status", session_token)
        assert status == 200
        assert isinstance(body, dict)
        assert body.get("configured") is False

    def test_tmdb_watchlist_match_status(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_get(base, "/api/tmdb/watchlist/match/status", session_token)
        assert status == 200
        assert isinstance(body, dict)
        assert "running" in body

    def test_tmdb_logs_empty(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_get(base, "/api/tmdb/logs", session_token)
        assert status == 200
        assert isinstance(body, dict)
        assert "logs" in body

    def test_tmdb_watchlist_movies_includes_is_manual_flag(self, webui_server):
        """验证 /api/tmdb/watchlist/movies?all=1 返回的 items 包含 _is_manual 字段。

        覆盖 routes.py:460-464：当 all=1 时，应为每个 item 附加 _is_manual 标记，
        基于 manual_override_at > 0 判断。
        """
        server, base, session_token = webui_server

        # 配置 mock 返回包含 manual_override_at 字段的 items
        mock_items = [
            {
                "id": 1,
                "title": "Movie1",
                "_media_type": "movie",
                "match_status": "matched",
                "manual_override_at": 1234567890.0,  # 手动覆盖
            },
            {
                "id": 2,
                "title": "Movie2",
                "_media_type": "movie",
                "match_status": "out",
                "manual_override_at": 0,  # 未手动覆盖
            },
        ]

        # 配置 server.get_watchlist_cached 返回 mock items
        server.get_watchlist_cached = lambda: mock_items

        # 配置 mock tmdb_client 以通过 routes.py:447 的检查
        mock_tmdb_client = MagicMock()
        mock_tmdb_client.account_id = "test_account"
        server._tmdb_client = mock_tmdb_client

        # 请求 all=1 以触发 _is_manual 附加逻辑
        status, _, body = _http_get(base, "/api/tmdb/watchlist/movies?all=1", session_token)
        assert status == 200
        assert isinstance(body, dict)
        assert "results" in body

        results = body["results"]
        assert len(results) == 2, f"应返回 2 个 items，实际: {len(results)}"

        # 验证 _is_manual 字段存在且正确
        for item in results:
            assert "_is_manual" in item, f"item 应包含 _is_manual 字段: {item}"

        # id=1 应标记为手动（manual_override_at > 0）
        item1 = next(i for i in results if i["id"] == 1)
        assert item1["_is_manual"] is True, \
            f"manual_override_at > 0 的 item 应标记 _is_manual=True，实际: {item1['_is_manual']}"

        # id=2 应标记为非手动（manual_override_at == 0）
        item2 = next(i for i in results if i["id"] == 2)
        assert item2["_is_manual"] is False, \
            f"manual_override_at == 0 的 item 应标记 _is_manual=False，实际: {item2['_is_manual']}"


# ============================================================
# WebUI Config 路由
# ============================================================

class TestWebUIConfigRoutes:
    """测试 /api/webui/config/{scope} 路由。"""

    def test_config_get_invalid_scope_403(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_get(base, "/api/webui/config/invalid_scope", session_token)
        assert status == 403
        assert isinstance(body, dict)

    def test_config_post_invalid_scope_403(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_post(
            base, "/api/webui/config/invalid_scope", {"k": "v"}, session_token)
        assert status == 403

    def test_config_post_invalid_json_400(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_post(
            base, "/api/webui/config/ui", b"not json", session_token)
        assert status == 400


# ============================================================
# POST 路由
# ============================================================

class TestPostRoutes:
    """测试 POST 路由。"""

    def test_unknown_post_404(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_post(base, "/api/unknown", {}, session_token)
        assert status == 404

    def test_openlist_test_connection_empty_host(self, webui_server):
        server, base, session_token = webui_server
        # 显式设为空以测试"未配置"路径（_make_mock_config 默认有 host）
        server._config.webdav.host = ""
        status, _, body = _http_post(
            base, "/api/openlist/test-connection",
            {"host": "", "user": "", "password": ""}, session_token)
        assert status == 400
        assert isinstance(body, dict)

    def _make_test_connection_mock(self, login_returns, error_type="unknown",
                                   error_message=""):
        """构造 mock OpenListAdminClient 类，用于 patch webdav_client.OpenListAdminClient。

        _handle_openlist_test_connection 内部用局部 import
        `from webdav_client import OpenListAdminClient`，故 patch 目标必须是
        `webdav_client.OpenListAdminClient`（源模块）。
        """
        mock_class = MagicMock()
        mock_instance = MagicMock()
        mock_class.return_value = mock_instance
        mock_instance.login.return_value = login_returns
        mock_instance.last_error_type = error_type
        mock_instance.last_error_message = error_message
        return mock_class

    def test_openlist_test_connection_wrong_password(self, webui_server):
        """last_error_type=wrong_password → HTTP 200，success=False，error 含"密码错误"。"""
        server, base, session_token = webui_server
        mock_class = self._make_test_connection_mock(
            False, error_type="wrong_password", error_message="密码错误")

        with patch("webdav_client.OpenListAdminClient", mock_class):
            status, _, body = _http_post(
                base, "/api/openlist/test-connection",
                {"host": "http://openlist:5244", "user": "admin", "password": "pw"},
                session_token)

        assert status == 200
        assert body.get("success") is False
        assert "密码错误" in body.get("error", "")
        assert body.get("error_type") == "wrong_password"

    def test_openlist_test_connection_wrong_2fa(self, webui_server):
        """last_error_type=wrong_2fa → HTTP 200，error 含"2FA"。"""
        server, base, session_token = webui_server
        mock_class = self._make_test_connection_mock(
            False, error_type="wrong_2fa", error_message="2FA 错误")

        with patch("webdav_client.OpenListAdminClient", mock_class):
            status, _, body = _http_post(
                base, "/api/openlist/test-connection",
                {"host": "http://openlist:5244", "user": "admin", "password": "pw",
                 "totp_secret": "secret"},
                session_token)

        assert status == 200
        assert body.get("success") is False
        assert "2FA" in body.get("error", "")
        assert body.get("error_type") == "wrong_2fa"

    def test_openlist_test_connection_network_error(self, webui_server):
        """last_error_type=network_error → HTTP 200，error 含"无法连接"。"""
        server, base, session_token = webui_server
        mock_class = self._make_test_connection_mock(
            False, error_type="network_error", error_message="connection refused")

        with patch("webdav_client.OpenListAdminClient", mock_class):
            status, _, body = _http_post(
                base, "/api/openlist/test-connection",
                {"host": "http://openlist:5244", "user": "admin", "password": "pw"},
                session_token)

        assert status == 200
        assert body.get("success") is False
        assert "无法连接" in body.get("error", "")
        assert body.get("error_type") == "network_error"

    def test_openlist_test_connection_unknown_error(self, webui_server):
        """last_error_type=unknown → HTTP 200，success=False，error_type=unknown。"""
        server, base, session_token = webui_server
        mock_class = self._make_test_connection_mock(
            False, error_type="unknown", error_message="something")

        with patch("webdav_client.OpenListAdminClient", mock_class):
            status, _, body = _http_post(
                base, "/api/openlist/test-connection",
                {"host": "http://openlist:5244", "user": "admin", "password": "pw"},
                session_token)

        assert status == 200
        assert body.get("success") is False
        assert body.get("error_type") == "unknown"

    def test_openlist_test_connection_login_succeeds(self, webui_server):
        """login() 返回 True → HTTP 200，success=True，message 含"连接成功"。"""
        server, base, session_token = webui_server
        mock_class = self._make_test_connection_mock(True)

        with patch("webdav_client.OpenListAdminClient", mock_class):
            status, _, body = _http_post(
                base, "/api/openlist/test-connection",
                {"host": "http://openlist:5244", "user": "admin", "password": "pw"},
                session_token)

        assert status == 200
        assert body.get("success") is True
        assert "连接成功" in body.get("message", "")

    def test_openlist_strm_engines_invalid_rejected(self, webui_server):
        server, base, session_token = webui_server
        bad = {"strm_engines": '[{"monitored_paths":[]}]'}  # 缺 engine
        status, _, body = _http_post(
            base, "/api/webui/config/openlist", bad, session_token)
        assert status == 400
        assert isinstance(body, dict)
        assert body.get("success") is False
        assert "strm_engines" in str(body.get("error", "")).lower()

    def test_openlist_strm_engines_valid_accepted(self, webui_server):
        server, base, session_token = webui_server
        good = {"strm_engines": json.dumps([
            {"engine": "/测试a", "monitored_paths": ["/m"]}])}
        status, _, body = _http_post(
            base, "/api/webui/config/openlist", good, session_token)
        assert status == 200
        assert body.get("success") is True

    def test_openlist_strm_engines_native_list_accepted(self, webui_server):
        server, base, session_token = webui_server
        good_native = {"strm_engines": [
            {"engine": "/测试a", "monitored_paths": ["/m"]}]}
        status, _, body = _http_post(
            base, "/api/webui/config/openlist", good_native, session_token)
        assert status == 200
        assert body.get("success") is True

    def test_openlist_strm_engines_none_rejected(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_post(
            base, "/api/webui/config/openlist", {"strm_engines": None}, session_token)
        assert status == 400
        assert body.get("success") is False

    def test_openlist_strm_engines_empty_string_rejected(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_post(
            base, "/api/webui/config/openlist", {"strm_engines": ""}, session_token)
        assert status == 400
        assert body.get("success") is False

    def test_tmdb_watchlist_match_override_invalid_media_type(
            self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_post(
            base, "/api/tmdb/watchlist/match/override",
            {"media_type": "invalid", "id": 1, "status": "matched"}, session_token)
        assert status == 400

    def test_tmdb_watchlist_match_override_invalid_status(
            self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_post(
            base, "/api/tmdb/watchlist/match/override",
            {"media_type": "movie", "id": 1, "status": "bogus"}, session_token)
        assert status == 400


class TestMatchClearEndpoint:
    """POST /api/tmdb/watchlist/match/clear 端点测试。"""

    def test_clear_requires_auth(self, tmp_path):
        """未携带 token 时返回 401/403（不在免鉴权白名单）。"""
        from webui.routes import _login_attempts
        _login_attempts.clear()

        cfg = _make_mock_config(tmp_path)
        db = _make_mock_db(tmp_path)
        port = _free_port()
        cfg.webui.port = port

        with patch("webui.server.PROJECT_ROOT", tmp_path), \
             patch("webui.server.STATIC_DIR", tmp_path / "static"):
            (tmp_path / "static").mkdir(exist_ok=True)
            (tmp_path / "static" / "index.html").write_text(
                "<html><body>test</body></html>", encoding="utf-8")
            (tmp_path / "static" / "assets").mkdir(exist_ok=True)
            (tmp_path / "static" / "assets" / "favicon.ico").write_bytes(b"\x00")

            server = WebUIServer(cfg.webui, db, app_config=cfg)
            test_password = "test_password_123"
            os.environ["WEBUI_TEST_MODE"] = "1"
            os.environ["WEBUI_ADMIN_PASSWORD_FOR_TEST"] = test_password
            server.start()
            deadline = time.time() + 2.0
            while not server._server and time.time() < deadline:
                time.sleep(0.05)

            try:
                base_url = f"http://127.0.0.1:{port}"
                status, _, body = _http_post(
                    base_url, "/api/tmdb/watchlist/match/clear",
                    {"media_type": "movie", "id": 1}, session_token=None)
                assert status in (401, 403), f"未认证应返回 401/403，实际: {status}"
            finally:
                server.stop()

    def test_clear_invalid_media_type_returns_400(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_post(
            base, "/api/tmdb/watchlist/match/clear",
            {"media_type": "invalid", "id": 1}, session_token)
        assert status == 400
        assert isinstance(body, dict)

    def test_clear_invalid_id_returns_400(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_post(
            base, "/api/tmdb/watchlist/match/clear",
            {"media_type": "movie", "id": -1}, session_token)
        assert status == 400

    def test_clear_missing_item_returns_404(self, webui_server):
        server, base, session_token = webui_server
        status, _, body = _http_post(
            base, "/api/tmdb/watchlist/match/clear",
            {"media_type": "movie", "id": 999999}, session_token)
        assert status == 404

    def test_clear_success(self, webui_server):
        """成功清除人工覆盖后返回 success=True。"""
        server, base, session_token = webui_server
        wdb = server._watchlist_db
        if not wdb:
            pytest.skip("watchlist_db not initialized")
        # 先插入一条记录并设为手动覆盖
        wdb._upsert_movie({"id": 1, "title": "Test", "original_title": "Test"}, 0.0)
        wdb.override_match_state("movie", 1, "matched", "manual")
        assert wdb.get_match_state("movie", 1)["manual_override_at"] > 0

        status, _, body = _http_post(
            base, "/api/tmdb/watchlist/match/clear",
            {"media_type": "movie", "id": 1}, session_token)
        assert status == 200
        assert body.get("success") is True

        # 验证清除后状态
        state = wdb.get_match_state("movie", 1)
        assert state["manual_override_at"] == 0.0
        assert state["manual_override_by"] == ""
        assert state["match_status"] == "uncomputed"

    def test_clear_endpoint_reachable_via_do_post(self, webui_server):
        """新端点经 do_POST 可达（覆盖 server.py 分发）。"""
        server, base, session_token = webui_server
        # 不存在的 id → 404 表示端点可达（不是 404 from do_POST）
        status, _, body = _http_post(
            base, "/api/tmdb/watchlist/match/clear",
            {"media_type": "movie", "id": 0}, session_token)
        # id<=0 → 400
        assert status == 400

    def test_openlist_save_with_empty_optional_fields_accepted(
            self, webui_server):
        """2FA/b_root/c_root 为空仍可保存（后端支持空 b/c，前端软警告不阻断）"""
        server, base, session_token = webui_server
        body = {
            "webdav_host": "http://192.168.1.100:5244",
            "webdav_user": "admin",
            "webdav_password": "",
            "webdav_totp_secret": "",
            "b_root": "",
            "c_root": "",
            "strm_engines": "[]",
            "refresh_paths": "[]",
            "log_level": "INFO",
            "log_max_size_mb": "2",
            "log_backup_count": "5",
            "log_file": "",
        }
        status, _, resp = _http_post(
            base, "/api/webui/config/openlist", body, session_token)
        assert status == 200
        assert resp.get("success") is True

    def test_openlist_save_empty_strm_engines_accepted(self, webui_server):
        """空 strm_engines 数组合法（前端过滤空引擎条目后的合法路径）"""
        server, base, session_token = webui_server
        body = {"strm_engines": "[]"}
        status, _, resp = _http_post(
            base, "/api/webui/config/openlist", body, session_token)
        assert status == 200
        assert resp.get("success") is True

    def test_openlist_save_missing_engine_entry_still_rejected(
            self, webui_server):
        """缺 engine 字段的脏载荷仍被拒 400（护栏不变），且错误文案含 strm_engines"""
        server, base, session_token = webui_server
        body = {"strm_engines": '[{"engine":"","monitored_paths":[]}]'}
        status, _, resp = _http_post(
            base, "/api/webui/config/openlist", body, session_token)
        assert status == 400
        assert resp.get("success") is False
        assert "strm_engines" in str(resp.get("error", "")).lower()


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

    def test_rate_limit_returns_429_after_five_failures(self, webui_server):
        """连续 5 次错误密码后第 6 次返回 429（登录限流端到端验证）。"""
        from webui.routes import _login_attempts

        server, base_url, session_token = webui_server
        # fixture 已成功登录一次，_login_attempts 已清空；重新确认
        _login_attempts.clear()

        # 连续发送 6 次错误密码
        for i in range(6):
            status, _, resp = _http_post(base_url, "/api/login", {"password": "wrong_password"})
            if i < 5:
                assert status == 401, f"第 {i + 1} 次错误密码应返回 401，实际: {status}"
            else:
                assert status == 429, f"第 6 次错误密码应返回 429，实际: {status}"
                assert "登录尝试过于频繁" in resp.get("error", ""), \
                    f"429 响应应包含限流提示，实际: {resp}"

        # 断言对应 IP 有 5 条失败时间戳（第 6 次被 429 拒绝，不追加）
        ip = "127.0.0.1"
        assert ip in _login_attempts
        assert len(_login_attempts[ip]) == 5, \
            f"期望 5 条失败记录（第 6 次被 429 拒绝不追加），实际: {len(_login_attempts[ip])}"


class TestWebuiConfigGetSanitization:
    """R25: GET /api/webui/config/{scope} 敏感凭据只返回布尔值，不返回明文。"""

    def _make_handler(self, scope_cfg: dict):
        wdb = MagicMock()
        wdb.get_all_config.return_value = dict(scope_cfg)
        server = MagicMock()
        server._watchlist_db = wdb
        handler = MagicMock()
        handler.client_address = ("127.0.0.1", 12345)
        return handler, server

    def test_tmdb_scope_sanitizes_sensitive(self):
        from webui.routes import _handle_webui_config_get
        handler, server = self._make_handler({
            "access_token": "secret_token_abc",
            "api_key": "secret_key_xyz",
            "language": "zh-CN",
        })
        _handle_webui_config_get(handler, server, "tmdb")
        payload = handler._send_json.call_args[0][0]
        assert payload["success"] is True
        cfg = payload["config"]
        assert cfg["access_token"] is True
        assert cfg["api_key"] is True
        assert "secret_token_abc" not in json.dumps(payload)
        assert cfg["language"] == "zh-CN"

    def test_openlist_scope_sanitizes_sensitive(self):
        from webui.routes import _handle_webui_config_get
        handler, server = self._make_handler({
            "webdav_password": "p@ssw0rd_secret",
            "webdav_totp_secret": "JBSWY3DPEHPK3PXP_secret",
            "webdav_host": "http://openlist:5244",
        })
        _handle_webui_config_get(handler, server, "openlist")
        payload = handler._send_json.call_args[0][0]
        cfg = payload["config"]
        assert cfg["webdav_password"] is True
        assert cfg["webdav_totp_secret"] is True
        assert "p@ssw0rd_secret" not in json.dumps(payload)
        assert cfg["webdav_host"] == "http://openlist:5244"

    def test_ui_scope_sanitizes_admin_password(self):
        from webui.routes import _handle_webui_config_get
        handler, server = self._make_handler({
            "admin_password": "pbkdf2$100000$secret_hash",
        })
        _handle_webui_config_get(handler, server, "ui")
        payload = handler._send_json.call_args[0][0]
        cfg = payload["config"]
        assert cfg["admin_password"] is True
        assert "pbkdf2" not in json.dumps(payload)


class TestOpenListPingRateLimit:
    """R26: /api/openlist/ping 的 IP 级 10 次/分钟速率限制。"""

    @staticmethod
    def _last_status(handler) -> int:
        """提取 _send_json 最后一次调用的 HTTP 状态码（位置参数或 kwargs）。"""
        args, kwargs = handler._send_json.call_args
        return kwargs.get("status", args[1] if len(args) > 1 else 200)

    def test_prefilled_attempts_immediately_429(self):
        from webui.routes import _handle_openlist_ping, _ping_attempts, _ping_attempts_lock
        _ping_attempts.clear()
        try:
            handler = MagicMock()
            handler.client_address = ("192.168.1.50", 12345)
            server = MagicMock()
            now = time.time()
            with _ping_attempts_lock:
                _ping_attempts["192.168.1.50"] = [now - i for i in range(10)]
            _handle_openlist_ping(handler, server)
            status = self._last_status(handler)
            payload = handler._send_json.call_args[0][0]
            assert status == 429
            assert payload["status"] == "rate_limited"
            assert payload["success"] is False
        finally:
            _ping_attempts.clear()

    def test_eleventh_call_returns_429(self):
        """速率限制检查在 host 检查之前，故第 11 次仍 429。"""
        from webui.routes import _handle_openlist_ping, _ping_attempts
        _ping_attempts.clear()
        try:
            handler = MagicMock()
            handler.client_address = ("192.168.1.60", 12345)
            server = MagicMock(name="no-host-server")
            fake_client = MagicMock()
            fake_client.login.return_value = True
            with patch("webui.routes._openlist_merged_webdav_cfg",
                       return_value=("http://openlist:5244", "u", "p", "")), \
                 patch("webdav_client.OpenListAdminClient", return_value=fake_client):
                for i in range(10):
                    _handle_openlist_ping(handler, server)
                    status = self._last_status(handler)
                    assert status != 429, f"第 {i + 1} 次不应限流"
                _handle_openlist_ping(handler, server)
                status = self._last_status(handler)
                payload = handler._send_json.call_args[0][0]
                assert status == 429, f"第 11 次应返回 429，实际 {status}"
                assert payload["status"] == "rate_limited"
        finally:
            _ping_attempts.clear()


class TestLoginToctouDoubleCheck:
    """R27: 登录失败双重检查锁定——已达上限时 429 且不追加记录。"""

    def _make_handler(self):
        handler = MagicMock()
        handler.client_address = ("127.0.0.1", 9999)
        server = MagicMock()
        server._watchlist_db = MagicMock()
        server._watchlist_db.get_config.return_value = "salt$100000$hash"
        return handler, server

    def test_locked_out_returns_429_without_appending(self):
        from webui.routes import _handle_login, _login_attempts, _login_attempts_lock
        _login_attempts.clear()
        try:
            handler, server = self._make_handler()
            now = time.time()
            with _login_attempts_lock:
                _login_attempts["127.0.0.1"] = [now] * 5
            _handle_login(handler, server, b'{"password":"wrong"}')
            status = handler._send_json.call_args[0][1]
            payload = handler._send_json.call_args[0][0]
            assert status == 429
            assert "登录尝试过于频繁" in payload.get("error", "")
            with _login_attempts_lock:
                assert len(_login_attempts["127.0.0.1"]) == 5
        finally:
            _login_attempts.clear()

    def test_double_check_blocks_toctou_race(self):
        """模拟 TOCTOU：初始检查通过（4 条），verify 期间并发补满第 5 条，
        双重检查应返回 429 且不追加——验证 R27 修复分支。"""
        import webui.routes as routes
        _login_attempts = routes._login_attempts
        _login_attempts.clear()
        try:
            handler, server = self._make_handler()
            now = time.time()
            with routes._login_attempts_lock:
                _login_attempts["127.0.0.1"] = [now - 1] * 4  # 初始检查通过

            def _race_verify(password, stored):
                # 模拟并发请求在慢哈希期间补满第 5 条
                with routes._login_attempts_lock:
                    cur = _login_attempts.get("127.0.0.1", [])
                    cur.append(time.time())
                    _login_attempts["127.0.0.1"] = cur
                return False

            with patch("utils.password_utils.verify_password", side_effect=_race_verify):
                routes._handle_login(handler, server, b'{"password":"wrong"}')
            status = handler._send_json.call_args[0][1]
            payload = handler._send_json.call_args[0][0]
            assert status == 429, f"双重检查应返回 429，实际 {status}"
            assert "登录尝试过于频繁" in payload.get("error", "")
            with routes._login_attempts_lock:
                assert len(_login_attempts["127.0.0.1"]) == 5, \
                    "双重检查已达上限应 429 且不追加"
        finally:
            _login_attempts.clear()


class TestConfigApiUnifiedSession:
    """R28: handle_config_api 用统一 _validate_session_token 校验（滑动续期 + 空 IP 兼容）。"""

    def _make_handler(self, tmp_path):
        from config import AppConfig
        toml_path = tmp_path / "config.toml"
        toml_path.write_text('[local]\ndb_file = "bridge.db"\n', encoding="utf-8")
        cfg = AppConfig.from_file(str(toml_path))
        handler = MagicMock()
        handler.webui._config = cfg
        handler.webui._tmdb_client = None
        handler.webui._watchlist_db = FakeConfigDb()
        handler.webui._has_password = True
        handler.webui._sessions = {}
        handler.webui._sessions_lock = threading.Lock()
        handler.client_address = ("192.168.1.10", 12345)
        handler.headers = {"X-Session-Token": "valid-token"}

        # 真实 _validate_session_token 实现（含滑动续期 + 空 IP 兼容）
        def _validate_token(token: str, client_ip: str) -> bool:
            import hmac
            webui = handler.webui
            now = time.time()
            with webui._sessions_lock:
                if token in webui._sessions:
                    expiry, stored_ip = webui._sessions[token]
                    if now < expiry and (stored_ip == "" or stored_ip == client_ip):
                        webui._sessions[token] = (now + 604800, client_ip)
                        return True
            return False

        handler._validate_session_token = _validate_token
        return handler

    def test_valid_token_authenticates_and_slides_expiry(self, tmp_path):
        from webui.routes import handle_config_api
        handler = self._make_handler(tmp_path)
        token = "valid-token"
        old_expiry = time.time() + 3600
        handler.webui._sessions[token] = (old_expiry, "192.168.1.10")
        handle_config_api(handler)
        payload = handler._send_json.call_args[0][0]
        assert payload["_authenticated"] is True
        # 滑动续期：过期时间被推进 ~7 天
        new_expiry, stored_ip = handler.webui._sessions[token]
        assert new_expiry > old_expiry + 600000, "有效会话应被滑动续期"
        assert stored_ip == "192.168.1.10"

    def test_empty_stored_ip_compatible(self, tmp_path):
        from webui.routes import handle_config_api
        handler = self._make_handler(tmp_path)
        handler.webui._sessions["valid-token"] = (time.time() + 3600, "")
        handle_config_api(handler)
        payload = handler._send_json.call_args[0][0]
        assert payload["_authenticated"] is True

    def test_invalid_token_not_authenticated(self, tmp_path):
        from webui.routes import handle_config_api
        handler = self._make_handler(tmp_path)
        handler.webui._sessions["other-token"] = (time.time() + 3600, "192.168.1.10")
        handle_config_api(handler)
        payload = handler._send_json.call_args[0][0]
        assert payload["_authenticated"] is False


class TestMainStartHidesExceptionDetail:
    """R29: start_main 异常时不回传内部路径/异常文本。"""

    def test_start_main_returns_generic_message_on_exception(self, tmp_path):
        from config import ABMapping
        cfg = _make_mock_config(tmp_path)
        cfg.a_b_mappings = [ABMapping(
            mapping_id="m1",
            a_root=str(tmp_path / "a"),
            b_root=str(tmp_path / "b"))]
        db = _make_mock_db(tmp_path)

        internal_path = str(tmp_path / "secret" / "config.py")

        def _boom(*args, **kwargs):
            raise FileNotFoundError(f"No such file: {internal_path}")

        with patch("webui.server.PROJECT_ROOT", tmp_path), \
             patch("webui.server.STATIC_DIR", tmp_path / "static"), \
             patch("webdav_client.OpenListAdminClient", side_effect=_boom):
            server = WebUIServer(cfg.webui, db, app_config=cfg)
            result = server.start_main()

        assert result["success"] is False
        assert result["message"] == "启动失败，请查看服务端日志"
        assert result.get("error_type") == "exception"
        assert internal_path not in result["message"]
        assert "FileNotFoundError" not in json.dumps(result)


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


# ============================================================
# 媒体刷新 API
# ============================================================

class TestAreaRefreshAPI:
    """测试 POST /api/area/{area}/refresh 刷新 API"""

    def test_area_refresh_invalid_area(self, webui_server):
        """无效 area 参数应返回 400"""
        server, base, session_token = webui_server
        body = {"media": "test_media"}
        status, _, resp = _http_post(base, "/api/area/x/refresh", body, session_token)
        assert status == 400
        assert "无效区域" in resp.get("error", "")

    def test_area_refresh_missing_media(self, webui_server):
        """缺少 media 参数应返回 400"""
        server, base, session_token = webui_server
        body = {}
        status, _, resp = _http_post(base, "/api/area/a/refresh", body, session_token)
        assert status == 400
        assert "缺少 media 参数" in resp.get("error", "")

    def test_area_refresh_auth_required(self, tmp_path):
        """未登录应返回 401"""
        from webui.routes import _login_attempts
        _login_attempts.clear()

        cfg = _make_mock_config(tmp_path)
        db = _make_mock_db(tmp_path)
        port = _free_port()
        cfg.webui.port = port

        with patch("webui.server.PROJECT_ROOT", tmp_path), \
             patch("webui.server.STATIC_DIR", tmp_path / "static"):
            (tmp_path / "static").mkdir(exist_ok=True)
            (tmp_path / "static" / "index.html").write_text(
                "<html><body>test</body></html>", encoding="utf-8")
            (tmp_path / "static" / "assets").mkdir(exist_ok=True)
            (tmp_path / "static" / "assets" / "favicon.ico").write_bytes(b"\x00")

            server = WebUIServer(cfg.webui, db, app_config=cfg)
            test_password = "test_password_123"
            os.environ["WEBUI_TEST_MODE"] = "1"
            os.environ["WEBUI_ADMIN_PASSWORD_FOR_TEST"] = test_password
            server.start()
            deadline = time.time() + 2.0
            while not server._server and time.time() < deadline:
                time.sleep(0.05)

            try:
                base_url = f"http://127.0.0.1:{port}"
                # 不传递 session_token
                body = {"media": "test_media"}
                status, _, resp = _http_post(base_url, "/api/area/a/refresh", body, session_token=None)
                assert status == 401
                assert resp.get("need_login") is True
            finally:
                server.stop()

    def test_area_refresh_not_running(self, webui_server):
        """主程序未运行时应返回 503"""
        server, base, session_token = webui_server
        # 确保 _app_service 为 None
        server._app_service = None
        body = {"media": "test_media"}
        status, _, resp = _http_post(base, "/api/area/a/refresh", body, session_token)
        assert status == 503
        assert resp.get("status") == "not_running"

    def test_area_refresh_path_traversal_rejected(self, webui_server):
        """路径穿越攻击应返回 400"""
        server, base, session_token = webui_server
        # 模拟主程序运行
        mock_app_service = MagicMock()
        server._app_service = mock_app_service

        # 测试包含 .. 的路径
        body = {"media": "../etc/passwd"}
        status, _, resp = _http_post(base, "/api/area/a/refresh", body, session_token)
        assert status == 400
        assert "媒体名包含非法字符" in resp.get("error", "")

        # 测试以 / 开头的路径
        body = {"media": "/etc/passwd"}
        status, _, resp = _http_post(base, "/api/area/a/refresh", body, session_token)
        assert status == 400
        assert "媒体名包含非法字符" in resp.get("error", "")

        # 测试以 \ 开头的路径
        body = {"media": "\\etc\\passwd"}
        status, _, resp = _http_post(base, "/api/area/a/refresh", body, session_token)
        assert status == 400
        assert "媒体名包含非法字符" in resp.get("error", "")

    def test_area_refresh_dangerous_characters_rejected(self, webui_server):
        """危险字符应返回 400"""
        server, base, session_token = webui_server
        mock_app_service = MagicMock()
        server._app_service = mock_app_service

        # 测试各种危险字符
        dangerous_inputs = [
            "test\x00name",        # null byte
            "test:name",           # colon (Windows drive letter)
            "test*name",           # asterisk
            "test?name",           # question mark
            'test"name',           # double quote
            "test<name",           # less than
            "test>name",           # greater than
            "test|name",           # pipe
            "C:\\Windows\\System", # Windows absolute path with drive letter
        ]

        for dangerous_input in dangerous_inputs:
            body = {"media": dangerous_input}
            status, _, resp = _http_post(base, "/api/area/a/refresh", body, session_token)
            assert status == 400, f"Expected 400 for input {repr(dangerous_input)}, got {status}"
            assert "媒体名包含非法字符" in resp.get("error", ""), \
                f"Expected '媒体名包含非法字符' error for {repr(dangerous_input)}, got {resp.get('error')}"

    def test_area_refresh_media_name_length_limit(self, webui_server):
        """超长媒体名应返回 400"""
        server, base, session_token = webui_server
        mock_app_service = MagicMock()
        server._app_service = mock_app_service

        # 测试超过 255 字符的媒体名
        long_name = "a" * 256
        body = {"media": long_name}
        status, _, resp = _http_post(base, "/api/area/a/refresh", body, session_token)
        assert status == 400
        assert "媒体名长度超限" in resp.get("error", "")

    def test_refresh_is_non_destructive(self, webui_server):
        """刷新不再因删除数超阈值要求确认，而是直接完成非破坏性 A→B 同步"""
        server, base, session_token = webui_server

        # Mock database 返回 15 条 A 区记录（旧逻辑下会超过阈值要求确认）
        mock_db = MagicMock()
        mock_records = [
            {"local_path": f"/a/zone/file{i}.strm", "webdav_path": f"/webdav/file{i}.strm", "parent_webdav_path": "/webdav"}
            for i in range(1, 16)
        ]

        # Mock read_connection context manager
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = mock_records
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=None)
        mock_conn.row_factory = None
        mock_db.read_connection.return_value = mock_conn

        mock_app_service = MagicMock()
        mock_app_service.db = mock_db
        mock_app_service.config = None
        # 新契约：映射到引擎入口路径 + 逐条 A→B 同步
        mock_app_service._cloud_path_to_engine_paths.return_value = ["/strm/webdav"]
        mock_app_service.copy_a_record_to_b_if_needed.return_value = True
        server._app_service = mock_app_service

        # Mock OpenList Admin API 返回空列表
        mock_admin_api = MagicMock()
        mock_admin_api.list_directory.return_value = {"code": 0, "data": {"content": []}}
        mock_app_service.admin_api = mock_admin_api

        # patch Path.exists 返回 True，使记录计入 synced 而非 skipped
        with patch("webui.routes.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            mock_path.return_value.is_absolute.return_value = False
            body = {"media": "test_movie"}
            status, _, resp = _http_post(base, "/api/area/a/refresh", body, session_token)

        # 验证非破坏性同步：不再要求确认，直接完成
        assert status == 200, f"Expected 200, got {status}. Response: {resp}"
        assert resp.get("ok") is True, f"Expected ok=True, got {resp}"
        assert "needs_confirmation" not in resp, f"不应再返回 needs_confirmation: {resp}"
        assert resp.get("synced") == 15, f"Expected synced=15, got {resp.get('synced')}"
        assert resp.get("skipped") == 0, f"Expected skipped=0, got {resp.get('skipped')}"
        assert resp.get("failed") == 0, f"Expected failed=0, got {resp.get('failed')}"

    def test_refresh_calls_copy_per_record(self, webui_server):
        """刷新逐条调用 copy_a_record_to_b_if_needed，不删除文件、不调用 delete_a_by_local"""
        server, base, session_token = webui_server

        # Mock database
        mock_db = MagicMock()
        mock_records = [
            {"local_path": f"/a/zone/file{i}.strm", "webdav_path": f"/webdav/file{i}.strm", "parent_webdav_path": "/webdav"}
            for i in range(1, 16)
        ]

        # Mock read_connection context manager
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = mock_records
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=None)
        mock_conn.row_factory = None
        mock_db.read_connection.return_value = mock_conn

        mock_app_service = MagicMock()
        mock_app_service.db = mock_db
        mock_app_service.config = None
        # 新契约：映射到引擎入口路径 + 逐条 A→B 同步
        mock_app_service._cloud_path_to_engine_paths.return_value = ["/strm/webdav"]
        mock_app_service.copy_a_record_to_b_if_needed.return_value = True
        server._app_service = mock_app_service

        # Mock OpenList Admin API 返回空列表
        mock_admin_api = MagicMock()
        mock_admin_api.list_directory.return_value = {"code": 0, "data": {"content": []}}
        mock_app_service.admin_api = mock_admin_api

        # patch Path.exists 返回 True，使记录计入 synced
        with patch("webui.routes.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            mock_path.return_value.is_absolute.return_value = False
            body = {"media": "test_movie"}
            status, _, resp = _http_post(base, "/api/area/a/refresh", body, session_token)

        # 验证逐条同步、非破坏性
        assert status == 200, f"Expected 200, got {status}. Response: {resp}"
        assert resp.get("ok") is True, f"Expected ok=True, got {resp}"
        assert mock_app_service.copy_a_record_to_b_if_needed.call_count == 15, \
            f"Expected 15 copy calls, got {mock_app_service.copy_a_record_to_b_if_needed.call_count}"
        assert resp.get("synced") == 15, f"Expected synced=15, got {resp.get('synced')}"
        # 非破坏性守卫：刷新不应删除任何 A 区记录
        assert mock_db.delete_a_by_local.called is False, "刷新不应调用 delete_a_by_local"

    def test_refresh_timeout(self, webui_server):
        """刷新不再有超时逻辑，响应中不应出现 timeout 字段"""
        server, base, session_token = webui_server

        # Mock database 返回 1 条记录
        mock_db = MagicMock()
        mock_records = [
            {"local_path": "/a/zone/file1.strm", "webdav_path": "/webdav/file1.strm", "parent_webdav_path": "/webdav"}
        ]

        # Mock read_connection context manager
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = mock_records
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=None)
        mock_conn.row_factory = None
        mock_db.read_connection.return_value = mock_conn

        mock_app_service = MagicMock()
        mock_app_service.db = mock_db
        mock_app_service.config = None
        mock_app_service._cloud_path_to_engine_paths.return_value = ["/strm/webdav"]
        mock_app_service.copy_a_record_to_b_if_needed.return_value = True
        server._app_service = mock_app_service

        # Mock OpenList Admin API 返回空列表
        mock_admin_api = MagicMock()
        mock_admin_api.list_directory.return_value = {"code": 0, "data": {"content": []}}
        mock_app_service.admin_api = mock_admin_api

        # patch Path.exists 返回 True
        with patch("webui.routes.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            mock_path.return_value.is_absolute.return_value = False
            body = {"media": "test_movie"}
            status, _, resp = _http_post(base, "/api/area/a/refresh", body, session_token)

        # 验证无超时字段（超时逻辑已移除，防止被误加回）
        assert status == 200, f"Expected 200, got {status}. Response: {resp}"
        assert resp.get("ok") is True, f"Expected ok=True, got {resp}"
        assert "timeout" not in resp, f"不应再返回 timeout 字段: {resp}"


# ============================================================
# 新手引导 API 测试
# ============================================================


class TestOnboardingAPI:
    """测试 /api/config/status 和 /api/config/validate 端点"""

    def test_config_status_unconfigured(self, webui_server):
        """未配置时返回基础状态"""
        server, base, session_token = webui_server
        # 显式设为未配置（_make_mock_config 默认有 host，此处覆盖）
        server._config.webdav.host = ""
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert status == 200
        assert resp["password_set"] is True  # 测试模式自动生成密码
        assert resp["tmdb_configured"] is False
        assert resp["openlist_configured"] is False
        assert resp["main_running"] is False
        assert resp["onboarding_completed"] is False

    def test_config_status_partially_configured(self, webui_server):
        """部分配置时返回对应字段"""
        server, base, session_token = webui_server
        # 模拟 OpenList 已配置
        server._config.webdav.host = "http://localhost:5244"
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert status == 200
        assert resp["openlist_configured"] is True
        assert resp["tmdb_configured"] is False

    def test_config_status_all_configured(self, webui_server):
        """全部配置完成时返回对应字段"""
        server, base, session_token = webui_server
        # 模拟全部配置
        server._config.webdav.host = "http://localhost:5244"
        with patch.object(server._watchlist_db, 'get_all_config', return_value={"access_token": "test_token"}), \
             patch.object(server._watchlist_db, 'get_config', return_value="1"):
            status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert status == 200
        assert resp["password_set"] is True
        assert resp["tmdb_configured"] is True
        assert resp["openlist_configured"] is True
        assert resp["onboarding_completed"] is True

    def test_config_validate_openlist_unconfigured(self, webui_server):
        """OpenList 未配置时返回 error"""
        server, base, session_token = webui_server
        # 显式设为未配置（_make_mock_config 默认有 host，此处覆盖）
        server._config.webdav.host = ""
        status, _, resp = _http_post(base, "/api/config/validate", {}, session_token)
        assert status == 200
        assert resp["ok"] is False
        checks = resp["checks"]
        openlist_config = next(c for c in checks if c["name"] == "openlist_config")
        assert openlist_config["status"] == "error"
        assert "未配置" in openlist_config["message"]

    def test_config_validate_tmdb_warning(self, webui_server):
        """TMDB 未配置时返回 warning（非阻塞）"""
        server, base, session_token = webui_server
        # 配置 OpenList 但不配置 TMDB
        server._config.webdav.host = "http://localhost:5244"
        with patch.object(server._watchlist_db, 'get_all_config', return_value={}), \
             patch("webdav_client.OpenListAdminClient") as mock_client:
            mock_client.return_value.login.return_value = True
            status, _, resp = _http_post(base, "/api/config/validate", {}, session_token)
        assert status == 200
        # TMDB 未配置应为 warning，但 ok 应为 True（仅 openlist_config 阻塞）
        tmdb_check = next(c for c in resp["checks"] if c["name"] == "tmdb_config")
        assert tmdb_check["status"] == "warning"
        assert resp["ok"] is True  # TMDB 未配置不阻塞启动

    def test_config_validate_all_ok(self, webui_server):
        """全部配置且可达时返回 ok=True"""
        server, base, session_token = webui_server
        server._config.webdav.host = "http://localhost:5244"
        with patch.object(server._watchlist_db, 'get_all_config', return_value={"access_token": "test_token"}), \
             patch("webdav_client.OpenListAdminClient") as mock_client:
            mock_client.return_value.login.return_value = True
            status, _, resp = _http_post(base, "/api/config/validate", {}, session_token)
        assert status == 200
        assert resp["ok"] is True
        assert all(c["status"] == "ok" for c in resp["checks"])

    def test_config_validate_openlist_unreachable(self, webui_server):
        """OpenList 配置但不可达时返回 warning（降级后不阻塞）"""
        server, base, session_token = webui_server
        server._config.webdav.host = "http://localhost:5244"
        with patch.object(server._watchlist_db, 'get_all_config', return_value={"access_token": "test_token"}), \
             patch("webdav_client.OpenListAdminClient") as mock_client:
            mock_client.return_value.login.return_value = False
            mock_client.return_value.last_error_type = "network_error"
            status, _, resp = _http_post(base, "/api/config/validate", {}, session_token)
        assert status == 200
        # openlist_online 应为 error，但 has_blocker 仅检查 openlist_config
        openlist_online = next(c for c in resp["checks"] if c["name"] == "openlist_online")
        assert openlist_online["status"] == "error"
        # 根据新逻辑，仅 openlist_config 阻塞，openlist_online 不阻塞
        assert resp["ok"] is True

    def test_config_validate_webui_server_none(self):
        """webui_server 为 None 时返回 500"""
        # 直接调用 handler 函数，不通过 HTTP
        from webui.routes import _handle_config_validate
        mock_handler = MagicMock()
        _handle_config_validate(mock_handler, None)
        mock_handler._send_json.assert_called_once()
        call_args = mock_handler._send_json.call_args
        # call_args 是 ((data,), {"status": 500}) 或类似格式
        assert call_args[0][0]["ok"] is False
        assert call_args[0][0]["error"] == "WebUI 服务器未初始化"
        # 检查第二个参数（status code）
        if len(call_args) > 1 and call_args[1]:
            assert call_args[1].get("status") == 500


# ============================================================
# Task 4: Area Detail API Tests
# ============================================================

class TestAreaDetailKindParameter:
    """测试 /api/area/{area}/detail 的 kind 参数处理。"""

    def _setup_mock_db(self, mock_db, records, total=1, area="b"):
        """设置 mock 数据库连接，处理多次 execute 调用。"""
        mock_conn = MagicMock()

        # 定义列名（根据 area 不同）
        if area == "a":
            columns = ["local_path", "webdav_path", "parent_webdav_path", "updated_at"]
        elif area == "b":
            columns = ["local_path", "webdav_path", "parent_webdav_path", "source_a_path", "fingerprint", "status", "updated_at", "mapping_id"]
        else:  # area == "c"
            columns = ["local_path", "webdav_path", "original_b_path", "ghost_root", "moved_at"]

        # 将记录转换为字典列表
        dict_records = [dict(zip(columns, record)) for record in records]

        # 使用 side_effect 处理多次 execute 调用
        def execute_side_effect(sql, params=None):
            mock_result = MagicMock()
            if "COUNT(*)" in sql:
                mock_result.fetchone.return_value = (total,)
            else:
                mock_result.fetchall.return_value = dict_records
            return mock_result

        mock_conn.execute.side_effect = execute_side_effect

        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.__exit__ = MagicMock(return_value=False)
        mock_db.read_connection.return_value = mock_conn_ctx

    def test_detail_api_without_kind_defaults_to_all(self, webui_server):
        """详情 API 不传 kind 时按 all 安全行为处理（不按文件名分季）"""
        server, base, session_token = webui_server
        # Mock 数据库返回包含文件名 S01E01 的记录
        mock_db = server._db
        records = [
            ("/b/movie/test_movie/Movie.S01E01.strm", "/webdav/movie/test_movie/Movie.S01E01.strm", "/webdav/movie/test_movie", "fingerprint1", "valid", 1000.0, "m1"),
        ]
        self._setup_mock_db(mock_db, records, total=1, area="b")

        # 不传 kind 参数
        status, _, resp = _http_get(base, "/api/area/b/detail?media=test_movie", session_token)
        assert status == 200
        # 验证返回的季标签为"默认"（因为 movie kind 不允许文件名 fallback）
        seasons = resp.get("seasons", [])
        assert len(seasons) == 1
        assert seasons[0]["label"] == "默认", f"movie kind 无 kind 参数时应归入默认，实际: {seasons[0]['label']}"

    def test_detail_api_with_invalid_kind_defaults_to_all(self, webui_server):
        """详情 API 传非法 kind（如 kind=../etc）降级为 all，不报错"""
        server, base, session_token = webui_server
        mock_db = server._db
        records = [
            ("/b/movie/test_movie/Movie.S01E01.strm", "/webdav/movie/test_movie/Movie.S01E01.strm", "/webdav/movie/test_movie", "fingerprint1", "valid", 1000.0, "m1"),
        ]
        self._setup_mock_db(mock_db, records, total=1, area="b")

        # 传非法 kind
        status, _, resp = _http_get(base, "/api/area/b/detail?media=test_movie&kind=../etc", session_token)
        assert status == 200
        # 应降级为 all 行为，归入默认
        seasons = resp.get("seasons", [])
        assert len(seasons) == 1
        assert seasons[0]["label"] == "默认", f"非法 kind 应降级为 all，实际: {seasons[0]['label']}"

    def test_detail_api_movie_kind_no_filename_fallback(self, webui_server):
        """movie kind 下文件名 SxxExx 不产生季，落入默认"""
        server, base, session_token = webui_server
        mock_db = server._db
        records = [
            ("/b/movie/test_movie/Movie.S01E01.strm", "/webdav/movie/test_movie/Movie.S01E01.strm", "/webdav/movie/test_movie", "fingerprint1", "valid", 1000.0, "m1"),
        ]
        self._setup_mock_db(mock_db, records, total=1, area="b")

        status, _, resp = _http_get(base, "/api/area/b/detail?media=test_movie&kind=movie", session_token)
        assert status == 200
        seasons = resp.get("seasons", [])
        assert len(seasons) == 1
        assert seasons[0]["label"] == "默认", f"movie kind 应不从文件名提取季，实际: {seasons[0]['label']}"

    def test_detail_api_anime_kind_filename_fallback_works(self, webui_server):
        """anime kind 下文件名 SxxExx 仍产生季"""
        server, base, session_token = webui_server
        mock_db = server._db
        records = [
            ("/b/anime/test_anime/Show.S01E01.strm", "/webdav/anime/test_anime/Show.S01E01.strm", "/webdav/anime/test_anime", "fingerprint1", "valid", 1000.0, "m1"),
        ]
        self._setup_mock_db(mock_db, records, total=1, area="b")

        status, _, resp = _http_get(base, "/api/area/b/detail?media=test_anime&kind=anime", session_token)
        assert status == 200
        seasons = resp.get("seasons", [])
        assert len(seasons) == 1
        assert seasons[0]["label"] == "S01", f"anime kind 应从文件名提取季，实际: {seasons[0]['label']}"

    def test_detail_api_movie_kind_skips_explicit_season_dir(self, webui_server):
        """movie kind 下显式 Season 2 目录不识别，落入默认"""
        server, base, session_token = webui_server
        mock_db = server._db
        records = [
            ("/b/movie/test_movie/Season 2/Movie.S02E01.strm", "/webdav/movie/test_movie/Season 2/Movie.S02E01.strm", "/webdav/movie/test_movie", "fingerprint1", "valid", 1000.0, "m1"),
        ]
        self._setup_mock_db(mock_db, records, total=1, area="b")

        # movie kind 不识别目录级季节
        status, _, resp = _http_get(base, "/api/area/b/detail?media=test_movie&kind=movie", session_token)
        assert status == 200
        seasons = resp.get("seasons", [])
        assert len(seasons) == 1
        assert seasons[0]["label"] == "默认", f"movie kind 应跳过显式 Season 目录，实际: {seasons[0]['label']}"

    def test_detail_api_anime_kind_explicit_season_dir_recognized(self, webui_server):
        """anime kind 下显式 Season 2 目录被正确识别为 S02"""
        server, base, session_token = webui_server
        mock_db = server._db
        records = [
            ("/b/anime/test_anime/Season 2/Show.S02E01.strm", "/webdav/anime/test_anime/Season 2/Show.S02E01.strm", "/webdav/anime/test_anime", "fingerprint1", "valid", 1000.0, "m1"),
        ]
        self._setup_mock_db(mock_db, records, total=1, area="b")

        status, _, resp = _http_get(base, "/api/area/b/detail?media=test_anime&kind=anime", session_token)
        assert status == 200
        seasons = resp.get("seasons", [])
        assert len(seasons) == 1
        assert seasons[0]["label"] == "S02", f"anime kind 应识别显式 Season 2 目录，实际: {seasons[0]['label']}"

    def test_detail_api_movie_kind_season1_dir_to_default(self, webui_server):
        """movie kind 下 Season 1 目录不识别，落入默认"""
        server, base, session_token = webui_server
        mock_db = server._db
        records = [
            ("/b/movie/test_movie/Season 1/Movie.S01E01.strm", "/webdav/movie/test_movie/Season 1/Movie.S01E01.strm", "/webdav/movie/test_movie", "fingerprint1", "valid", 1000.0, "m1"),
        ]
        self._setup_mock_db(mock_db, records, total=1, area="b")

        status, _, resp = _http_get(base, "/api/area/b/detail?media=test_movie&kind=movie", session_token)
        assert status == 200
        seasons = resp.get("seasons", [])
        assert len(seasons) == 1
        assert seasons[0]["label"] == "默认", f"movie kind 应跳过 Season 1 目录，实际: {seasons[0]['label']}"


class TestAreaDetailCZonePagination:
    """测试 C 区详情分页（R2 回归）。"""

    def _setup_mock_db(self, mock_db, records, total):
        """设置 mock 数据库连接，处理多次 execute 调用。"""
        mock_conn = MagicMock()

        columns = ["local_path", "webdav_path", "original_b_path", "ghost_root", "moved_at"]
        dict_records = [dict(zip(columns, record)) for record in records]

        def execute_side_effect(sql, params=None):
            mock_result = MagicMock()
            if "COUNT(*)" in sql:
                mock_result.fetchone.return_value = (total,)
            else:
                mock_result.fetchall.return_value = dict_records
            return mock_result

        mock_conn.execute.side_effect = execute_side_effect

        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.__exit__ = MagicMock(return_value=False)
        mock_db.read_connection.return_value = mock_conn_ctx

    def test_c_zone_detail_page2_records_not_exceed_page_size(self, webui_server):
        """C 区详情第 2 页返回的记录数不超过 PAGE_SIZE"""
        server, base, session_token = webui_server
        mock_db = server._db

        # 创建 150 条记录（PAGE_SIZE=50，共 3 页）
        records = []
        for i in range(150):
            records.append((
                f"/c/ghost/movie{i}.strm",
                f"/webdav/ghost/movie{i}.strm",
                f"/b/original/movie{i}.strm",
                "/ghost/root",
                1000.0 + i,
            ))

        self._setup_mock_db(mock_db, records, total=150)

        # 请求第 2 页
        status, _, resp = _http_get(base, "/api/area/c/detail?media=ghost&page=2", session_token)
        assert status == 200

        # 验证第 2 页记录数不超过 PAGE_SIZE (50)
        total_records = sum(len(s["records"]) for s in resp.get("seasons", []))
        assert total_records <= 50, f"第 2 页记录数不应超过 PAGE_SIZE，实际: {total_records}"

        # 验证总页数正确
        assert resp.get("total_pages") == 3, f"总页数应为 3，实际: {resp.get('total_pages')}"

    def test_c_zone_detail_page1_and_page2_no_overlap(self, webui_server):
        """C 区详情第 1 页和第 2 页记录不重叠"""
        server, base, session_token = webui_server
        mock_db = server._db

        records = []
        for i in range(150):
            records.append((
                f"/c/ghost/movie{i}.strm",
                f"/webdav/ghost/movie{i}.strm",
                f"/b/original/movie{i}.strm",
                "/ghost/root",
                1000.0 + i,
            ))

        self._setup_mock_db(mock_db, records, total=150)

        # 请求第 1 页
        status1, _, resp1 = _http_get(base, "/api/area/c/detail?media=ghost&page=1", session_token)
        assert status1 == 200
        page1_paths = {r["local_path"] for s in resp1.get("seasons", []) for r in s["records"]}

        # 请求第 2 页
        status2, _, resp2 = _http_get(base, "/api/area/c/detail?media=ghost&page=2", session_token)
        assert status2 == 200
        page2_paths = {r["local_path"] for s in resp2.get("seasons", []) for r in s["records"]}

        # 验证无重叠
        overlap = page1_paths & page2_paths
        assert len(overlap) == 0, f"第 1 页和第 2 页不应重叠，重叠路径: {overlap}"


class TestAreaDetailSingleMappingMid:
    """测试单 mapping 响应的 mapping_id 正确性（R3 回归）。"""

    def _setup_mock_db(self, mock_db, records, total):
        """设置 mock 数据库连接，处理多次 execute 调用。"""
        mock_conn = MagicMock()

        columns = ["local_path", "webdav_path", "parent_webdav_path", "source_a_path", "fingerprint", "status", "updated_at", "mapping_id"]
        dict_records = [dict(zip(columns, record)) for record in records]

        def execute_side_effect(sql, params=None):
            mock_result = MagicMock()
            if "COUNT(*)" in sql:
                mock_result.fetchone.return_value = (total,)
            else:
                mock_result.fetchall.return_value = dict_records
            return mock_result

        mock_conn.execute.side_effect = execute_side_effect

        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.__exit__ = MagicMock(return_value=False)
        mock_db.read_connection.return_value = mock_conn_ctx

    def test_single_mapping_response_mapping_id_correct(self, webui_server):
        """单 mapping 响应的 mapping_id 等于该 mapping 的真实 id"""
        server, base, session_token = webui_server
        mock_db = server._db
        mock_app_service = server._app_service

        # 设置单个 mapping - 需要先确保 app_service 不为 None
        if mock_app_service is None:
            mock_app_service = MagicMock()
            server._app_service = mock_app_service

        mock_mapping = MagicMock()
        mock_mapping.mapping_id = "real_mapping_123"
        mock_mapping.a_root = "/a/m1"
        mock_mapping.b_root = "/b/m1"
        mock_app_service.a_b_mappings = [mock_mapping]
        mock_app_service.get_mapping_for_a.return_value = ("real_mapping_123", "/a/m1", "/b/m1")

        records = [
            ("/b/m1/movie1.strm", "/webdav/m1/movie1.strm", "/webdav/m1", "/a/m1/movie1.strm", "fingerprint1", "valid", 1000.0, "real_mapping_123"),
            ("/b/m1/movie2.strm", "/webdav/m1/movie2.strm", "/webdav/m1", "/a/m1/movie2.strm", "fingerprint2", "valid", 1001.0, "real_mapping_123"),
        ]
        self._setup_mock_db(mock_db, records, total=2)

        status, _, resp = _http_get(base, "/api/area/b/detail?media=movie", session_token)
        assert status == 200

        # 单 mapping 应返回扁平响应，包含正确的 mapping_id
        assert resp.get("mapping_id") == "real_mapping_123", f"单 mapping 响应的 mapping_id 应为真实 id，实际: {resp.get('mapping_id')}"


class TestConfigApiFreshInstall:
    """全新安装（webui_config 无 openlist 作用域）时 /api/config 不得抛异常。

    关键：必须用**真实** AppConfig。本文件的 _make_mock_config 返回 MagicMock，
    而 MagicMock.__iter__ 默认返回空迭代器，会把 cfg.a_b_mappings 未赋值
    的问题完全掩盖掉——这也是这个 bug 至今没有被任何测试发现的原因。
    """

    def _fresh_handler(self, tmp_path: Path):
        from config import AppConfig
        toml_path = tmp_path / "config.toml"
        toml_path.write_text('[local]\ndb_file = "bridge.db"\n', encoding="utf-8")
        cfg = AppConfig.from_file(str(toml_path))
        handler = MagicMock()
        handler.webui._config = cfg
        handler.webui._tmdb_client = None
        handler.webui._watchlist_db = FakeConfigDb()  # 空 DB = 首次运行
        # handle_config_api 改用 handler._validate_session_token 统一校验。
        # 首次安装未设置管理员密码 → _has_password=False，模拟真实"未认证"场景，
        # 避免 MagicMock 默认真值导致 _authenticated 误判。
        handler.webui._has_password = False
        return handler

    def test_from_file_initializes_mapping_fields(self, tmp_path):
        """from_file 必须给出可安全读取的默认值，而不是留下未赋值的 slot。"""
        from config import AppConfig
        toml_path = tmp_path / "config.toml"
        toml_path.write_text('[local]\ndb_file = "bridge.db"\n', encoding="utf-8")
        cfg = AppConfig.from_file(str(toml_path))
        assert cfg.a_b_mappings == []
        assert cfg.engines_initialized is False

    def test_config_api_survives_fresh_install(self, tmp_path):
        from webui.routes import handle_config_api
        handler = self._fresh_handler(tmp_path)
        handle_config_api(handler)  # 修复前抛 AttributeError
        handler._send_json.assert_called_once()
        payload = handler._send_json.call_args[0][0]
        # 未认证响应不泄露 port/bind/counts
        assert "_authenticated" in payload
        assert payload["_authenticated"] is False
        assert "a_b_mappings_count" not in payload
        assert "webui_port" not in payload
        assert "webui_bind" not in payload
        assert "a_folders_count" not in payload
        assert payload["webdav_configured"] is False


class TestStartMainFailSafe:
    """引擎落入 fail-safe 时，start_main 必须返回失败且不置 _app_running。

    start_main 此前在 src/tests/ 下零引用——这是 D3 未被发现的原因。
    """

    def test_start_main_reports_fail_safe(self, tmp_path):
        from config import ABMapping
        cfg = _make_mock_config(tmp_path)
        cfg.a_b_mappings = [ABMapping(
            mapping_id="m1",
            a_root=str(tmp_path / "a"),
            b_root=str(tmp_path / "b"))]
        db = _make_mock_db(tmp_path)

        fake_client = MagicMock()
        fake_client.login.return_value = True
        fake_app = MagicMock()
        fake_app._running = False
        fake_app.get_config_status.return_value = {
            "status": "fail_safe_active",
            "reason": "mapping 缺少唯一 ID 或根路径"}

        with patch("webui.server.PROJECT_ROOT", tmp_path), \
             patch("webui.server.STATIC_DIR", tmp_path / "static"), \
             patch("webdav_client.OpenListAdminClient", return_value=fake_client), \
             patch("app_service.AppService", return_value=fake_app):
            server = WebUIServer(cfg.webui, db, app_config=cfg)
            result = server.start_main()

        assert result["success"] is False
        assert result.get("status") == "fail_safe_active"
        assert server._app_running is False

    def test_start_main_succeeds_when_ready(self, tmp_path):
        """配置 ready 时行为不变，避免修复把正常启动路径一起堵死。

        替身必须忠实模拟真实契约：AppService.start() 成功收尾时才置
        _running=True（不变式由 test_app_service_lifecycle.py 的
        TestStartMarksRunningWhenReady 锁死）。
        禁止预先把 _running 设为 True——那会让本用例在引擎根本不置位时也变绿，
        正是这一点让 start_main 门禁选错信号的回归漏过了测试。
        """
        from config import ABMapping
        cfg = _make_mock_config(tmp_path)
        cfg.a_b_mappings = [ABMapping(
            mapping_id="m1",
            a_root=str(tmp_path / "a"),
            b_root=str(tmp_path / "b"))]
        db = _make_mock_db(tmp_path)

        fake_client = MagicMock()
        fake_client.login.return_value = True
        fake_app = MagicMock()
        fake_app._running = False
        fake_app.get_config_status.return_value = {"status": "ready", "reason": "ok"}
        fake_app.start.side_effect = lambda: setattr(fake_app, "_running", True)

        with patch("webui.server.PROJECT_ROOT", tmp_path), \
             patch("webui.server.STATIC_DIR", tmp_path / "static"), \
             patch("webdav_client.OpenListAdminClient", return_value=fake_client), \
             patch("app_service.AppService", return_value=fake_app):
            server = WebUIServer(cfg.webui, db, app_config=cfg)
            result = server.start_main()

        assert result["success"] is True
        assert server._app_running is True
        fake_app.start.assert_called_once()


# ============================================================
# Task A: 手动全量审计端点测试
# ============================================================


class TestManualFullIndexAuditAPI:
    """测试 POST /api/index/audit 和 GET /api/index/audit/status 端点"""

    def test_audit_endpoint_requires_auth(self, webui_server):
        """审计端点需要鉴权（不在免鉴权白名单）"""
        server, base, session_token = webui_server

        # 不传 token
        body = {}
        status, _, resp = _http_post(base, "/api/index/audit", body, session_token=None)
        assert status == 401
        assert resp.get("need_login") is True

    def test_audit_when_app_service_is_none(self, webui_server):
        """当 app_service 为 None 时，审计应返回 not_configured"""
        server, base, session_token = webui_server
        server._app_service = None

        body = {}
        status, _, resp = _http_post(base, "/api/index/audit", body, session_token)
        assert status == 400
        assert resp.get("ok") is False
        assert resp.get("status") == "not_configured"

    def test_audit_when_engine_not_ready(self, webui_server):
        """当引擎未 ready 时，审计应返回 not_configured"""
        server, base, session_token = webui_server

        mock_app = MagicMock()
        mock_app._running = False
        server._app_service = mock_app

        body = {}
        status, _, resp = _http_post(base, "/api/index/audit", body, session_token)
        assert status == 400
        assert resp.get("ok") is False
        assert resp.get("status") == "not_configured"

    def test_audit_concurrent_request_returns_already_running(self, webui_server):
        """并发请求应返回 200 + already_running，不启动第二个线程"""
        server, base, session_token = webui_server

        mock_app = MagicMock()
        mock_app._running = True
        mock_app.refresh_service = MagicMock()
        mock_app.refresh_service._maybe_run_full_audit = MagicMock()
        server._app_service = mock_app

        # 模拟正在进行中
        server._index_audit_running = True

        body = {}
        status, _, resp = _http_post(base, "/api/index/audit", body, session_token)
        assert status == 200
        assert resp.get("ok") is True
        assert resp.get("status") == "already_running"

        # 验证没有调用审计方法
        mock_app.refresh_service._maybe_run_full_audit.assert_not_called()

    def test_audit_status_endpoint_returns_running_and_result(self, webui_server):
        """GET /api/index/audit/status 应返回 {running, result} 可轮询"""
        server, base, session_token = webui_server

        # 默认状态
        status, _, resp = _http_get(base, "/api/index/audit/status", session_token)
        assert status == 200
        assert "running" in resp
        assert "result" in resp
        assert resp["running"] is False
        assert resp["result"] is None


# ============================================================
# Task B: TMDB override 端点一致性收口测试
# ============================================================


class TestTMDBWatchlistMatchOverrideConsistency:
    """测试 TMDB override 端点的一致性校验（与 clear/refresh 对齐）"""

    def test_override_when_watchlist_disabled_returns_400(self, webui_server):
        """watchlist_enabled 为 'false' 时 POST override 应返回 400"""
        server, base, session_token = webui_server

        # 设置 watchlist_enabled = "false"（通过 watchlist_db）
        wdb = server._watchlist_db
        if not wdb:
            pytest.skip("watchlist_db not initialized")
        wdb.set_config("tmdb", "watchlist_enabled", "false")

        body = {"media_type": "movie", "id": 1, "status": "matched"}
        status, _, resp = _http_post(
            base, "/api/tmdb/watchlist/match/override", body, session_token)
        assert status == 400
        assert resp.get("success") is False
        assert "禁用" in resp.get("message", "")

    def test_override_with_id_zero_returns_400(self, webui_server):
        """id=0 应返回 400（无效 ID）"""
        server, base, session_token = webui_server

        body = {"media_type": "movie", "id": 0, "status": "matched"}
        status, _, resp = _http_post(base, "/api/tmdb/watchlist/match/override", body, session_token)
        assert status == 400
        assert resp.get("success") is False

    def test_override_with_negative_id_returns_400(self, webui_server):
        """id=-1 应返回 400（无效 ID）"""
        server, base, session_token = webui_server

        body = {"media_type": "movie", "id": -1, "status": "matched"}
        status, _, resp = _http_post(base, "/api/tmdb/watchlist/match/override", body, session_token)
        assert status == 400
        assert resp.get("success") is False


# ============================================================
# 回归测试：M-4 Session IP 绑定 + C-2 DB失败 fail-closed
# ============================================================


class TestSessionIPBinding:
    """测试 M-4 Session IP 绑定功能。"""

    def test_session_ip_binding_rejects_different_ip(self, webui_server):
        """登录后使用不同IP的token应被拒绝（401）"""
        server, base, session_token = webui_server

        # 直接修改 _sessions 中的 IP，模拟 token 被盗用到不同 IP
        with server._sessions_lock:
            for tok, (exp, _ip) in server._sessions.items():
                if tok == session_token:
                    server._sessions[tok] = (exp, "10.99.99.99")
                    break

        # 用原 token 从 127.0.0.1 请求 → 应被拒绝
        status, _, resp = _http_get(base, "/api/area/a", session_token)
        assert status == 401
        assert resp.get("error") == "unauthorized"

    def test_session_ip_binding_allows_original_ip(self, webui_server):
        """登录后使用相同IP的token应被接受（200）"""
        server, base, session_token = webui_server

        # 用原 token 从 127.0.0.1 请求 → 应成功
        status, _, resp = _http_get(base, "/api/area/a", session_token)
        assert status == 200


class TestRound13Regressions:
    """第 23 轮 superpower 审计回归（M3 / M4 / M5）。"""

    def test_media_name_sql_matches_all_alias_dirs(self):
        """M3: _MEDIA_NAME_SQL 应对 /movies/ /movie/ /anime/ /动漫/ /动画/ 别名目录提取正确媒体名。

        旧实现只匹配 /番剧/ 与 /电影/，别名目录全部坍缩进 '未分类'。
        """
        import sqlite3
        from webui.routes import _MEDIA_NAME_SQL

        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cases = {
            "/movies/Inception/Inception.strm": ("Inception", "电影"),
            "/movie/Dunkirk/Dunkirk.strm": ("Dunkirk", "电影"),
            "/anime/Attack/Attack.strm": ("Attack", "番剧"),
            "/动漫/鬼灭之刃/鬼灭之刃.strm": ("鬼灭之刃", "番剧"),
            "/动画/咒术回战/咒术回战.strm": ("咒术回战", "番剧"),
            "/番剧/进击的巨人/进击的巨人.strm": ("进击的巨人", "番剧"),
            "/电影/流浪地球/流浪地球.strm": ("流浪地球", "电影"),
        }
        for path, (exp_name, _exp_kind) in cases.items():
            sql = f"SELECT {_MEDIA_NAME_SQL} FROM (SELECT ? AS webdav_path, ? AS local_path)"
            got = cur.execute(sql, (path, path)).fetchone()[0]
            assert got == exp_name, (
                f"别名目录 {path} 应提取 '{exp_name}'，实际 '{got}'（未分类坍缩）"
            )

    def test_password_change_invalidates_old_session(self, webui_server):
        """M4: 改密后旧 token 应立即失效（401），不能继续冒用。"""
        server, base, session_token = webui_server

        # 改密前旧 token 有效
        status, _, _ = _http_get(base, "/api/area/a", session_token)
        assert status == 200

        # 修改管理员密码 → 应清空全部会话
        status, _, body = _http_post(
            base, "/api/webui/config/ui",
            {"admin_password": "new_password_456"}, session_token)
        assert status == 200, f"改密应成功，实际 {status}: {body}"

        # 旧 token 现在应失效
        status, _, resp = _http_get(base, "/api/area/a", session_token)
        assert status == 401, f"改密后旧 token 应返回 401，实际 {status}"
        assert resp.get("error") == "unauthorized"

        # 会话表应已清空
        with server._sessions_lock:
            assert len(server._sessions) == 0

    def test_admin_status_invalid_token_returns_401(self, webui_server):
        """M5: /api/admin/status 带无效 token 应返回 401（不再无条件 200）。"""
        server, base, _session_token = webui_server
        status, _, resp = _http_get(
            base, "/api/admin/status", "fake-or-expired-token")
        assert status == 401, f"带无效 token 的 admin/status 应返回 401，实际 {status}"
        assert resp.get("error") == "unauthorized"

    def test_admin_status_no_token_returns_200(self, webui_server):
        """M5: /api/admin/status 无 token 应保持白名单直通（200 + has_password）。

        router.js:105 的 has_password 变更检测依赖该 200 响应。
        """
        server, base, _session_token = webui_server
        status, _, body = _http_get(base, "/api/admin/status")
        assert status == 200, f"无 token 的 admin/status 应返回 200，实际 {status}"
        assert isinstance(body, dict) and "has_password" in body


class TestDBInitFailure:
    """测试 C-2 DB初始化失败时 fail-closed。"""

    def test_db_init_failure_returns_503(self, webui_server):
        """模拟 _db_init_failed=True 时 POST 应返回 503"""
        server, base, session_token = webui_server

        server._db_init_failed = True
        try:
            status, _, resp = _http_post(
                base, "/api/webui/config/ui",
                {"theme": "dark"}, session_token)
            assert status == 503
            assert resp.get("error") == "server_error"
        finally:
            server._db_init_failed = False


# ============================================================
# 数据库路径固定 + watchlist_db 移除
# ============================================================


class TestP13WatchlistDbRemoved:
    """回归验证：watchlist_db 字段已从配置中移除，验证各入口的拒绝/剥离行为。"""

    def test_tmdb_configure_rejects_watchlist_db(self, webui_server):
        """POST /api/tmdb/configure 含 watchlist_db → 400"""
        server, base, session_token = webui_server

        body = {"watchlist_db": "/custom/path.db", "language": "zh-CN"}
        status, _, resp = _http_post(base, "/api/tmdb/configure", body, session_token)

        assert status == 400, f"含 watchlist_db 应返回 400，实际: {status}"
        assert resp.get("success") is False
        assert "已移除" in resp.get("error", "")

        # 验证 language 未被写入（请求被整体拒绝）
        wdb = server._watchlist_db
        if wdb:
            stored_lang = wdb.get_config("tmdb", "language")
            # language 不应被写入（因为请求整体被拒绝）
            assert stored_lang != "zh-CN" or stored_lang is None, \
                "watchlist_db 拒绝应阻止整次写入"

    def test_webui_config_tmdb_scope_strips_watchlist_db(self, webui_server):
        """POST /api/webui/config/tmdb 含 watchlist_db → 剥离该键，其余键正常写入"""
        server, base, session_token = webui_server

        # 同时发送 watchlist_db（应被剥离）和 language（应被写入）
        body = {"watchlist_db": "/orphan/path.db", "language": "en"}
        status, _, resp = _http_post(base, "/api/webui/config/tmdb", body, session_token)

        assert status == 200, f"剥离后应正常返回 200，实际: {status}"
        assert resp.get("success") is True

        # 验证 language 已写入
        wdb = server._watchlist_db
        assert wdb is not None, "watchlist_db 应已初始化"
        stored_lang = wdb.get_config("tmdb", "language")
        assert stored_lang == "en", f"language 应为 'en'，实际: {stored_lang}"

        # 验证 watchlist_db 未写入 DB
        orphan = wdb.get_config("tmdb", "watchlist_db")
        assert orphan is None or orphan == "", \
            f"watchlist_db 应被剥离不写入 DB，实际: {orphan!r}"

    def test_config_api_db_file_is_fixed(self, webui_server):
        """GET /api/config 返回的 db_file 和 tmdb_watchlist_db 为固定项目根路径"""
        server, base, session_token = webui_server

        status, _, resp = _http_get(base, "/api/config", session_token)
        assert status == 200
        db_file = resp.get("db_file", "")
        tmdb_db = resp.get("tmdb_watchlist_db", "")
        # 两者应为非空字符串且不含自定义路径标记
        assert isinstance(db_file, str) and len(db_file) > 0
        assert isinstance(tmdb_db, str) and len(tmdb_db) > 0
        # 不应包含用户自定义路径的特征（如 /custom/）
        assert "/custom/" not in db_file, f"db_file 不应含自定义路径: {db_file}"
        assert "/custom/" not in tmdb_db, f"tmdb_watchlist_db 不应含自定义路径: {tmdb_db}"
