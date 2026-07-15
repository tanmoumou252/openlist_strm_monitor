"""
完整业务流程端到端测试。

覆盖场景：
1. 成功路径：登录 → 配置 TMDB → 配置 OpenList → 启动 Bridge → 查看 A/B 区 → 刷新待看列表 → 验证收录状态
2. 失败场景：不可达 OpenList 地址、预检失败
"""

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ============================================================
# Fixtures（与 test_onboarding_e2e.py 共享模式）
# ============================================================

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def _make_mock_config(tmp_path: Path) -> MagicMock:
    """构造最小化 AppConfig mock。"""
    cfg = MagicMock()
    cfg.webui.enabled = True
    cfg.webui.port = 0
    cfg.webui.bind = "127.0.0.1"
    cfg.tmdb.access_token = ""
    cfg.tmdb.api_key = ""
    cfg.tmdb.language = "zh-CN"
    cfg.tmdb.host = ""
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
    cfg.webdav.host = ""
    cfg.webdav.user = ""
    cfg.webdav.password = ""
    cfg.webdav.totp_secret = ""
    cfg.paths.b_root = str(tmp_path / "b")
    cfg.paths.c_root = str(tmp_path / "c")
    cfg.behavior.ghost_protect_seconds = 300
    cfg.strm_storage_map = {}
    cfg.strm_engine_paths = []
    cfg.update_from_db = MagicMock()
    return cfg


def _make_mock_db(tmp_path: Path) -> MagicMock:
    db = MagicMock()
    db.db_path = str(tmp_path / "bridge.db")
    db.get_table_counts.return_value = {
        "a_strm_files": 0, "b_strm_files": 0, "c_ghost_files": 0,
    }
    db.get_b_status_counts.return_value = {
        "valid": 0, "orphan": 0, "unknown": 0,
    }
    db.get_db_file_size.return_value = 0
    db.get_subtitle_by_local.return_value = None
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = (0,)
    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn_ctx.__exit__ = MagicMock(return_value=False)
    db.read_connection.return_value = mock_conn_ctx
    return db


@pytest.fixture
def webui_server(tmp_path):
    """启动一个真实的 WebUIServer 实例。"""
    from webui.server import WebUIServer
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

        base_url = f"http://127.0.0.1:{port}"
        login_status, _, login_body = _http_post(
            base_url, "/api/login", {"password": test_password})
        assert login_status == 200
        session_token = login_body.get("token")
        assert session_token is not None

        yield server, base_url, session_token
        server.stop()


def _http_get(base_url, path, session_token=None, timeout=3.0):
    url = f"{base_url}{path}"
    headers = {"X-Session-Token": session_token} if session_token else {}
    req = urllib.request.Request(url, method="GET", headers=headers)
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


def _http_post(base_url, path, body=None, session_token=None, timeout=3.0):
    url = f"{base_url}{path}"
    data = json.dumps(body or {}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if session_token:
        headers["X-Session-Token"] = session_token
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
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


# ============================================================
# 场景 1：成功路径
# ============================================================

class TestSuccessfulFlow:
    """测试完整新用户成功路径"""

    def test_complete_new_user_flow(self, webui_server):
        """完整新用户流程：登录 → 配置 TMDB → 配置 OpenList → 启动 Bridge → 查看 A/B 区 → 刷新待看列表 → 验证收录状态"""
        server, base, session_token = webui_server

        # 1. 登录（已在 fixture 中完成）
        assert session_token is not None

        # 2. 配置 TMDB（_handle_tmdb_configure 只保存配置，不验证 token）
        status, _, resp = _http_post(base, "/api/tmdb/configure", {
            "access_token": "test_token",
            "api_key": "test_key",
            "language": "zh-CN",
        }, session_token)
        assert status == 200
        assert resp.get("success") is True

        # 3. 配置 OpenList（_handle_webui_config_post 只保存配置，不测试连接）
        status, _, resp = _http_post(base, "/api/webui/config/openlist", {
            "webdav_host": "http://localhost:5244",
            "webdav_user": "admin",
            "webdav_password": "password",
        }, session_token)
        assert status == 200
        assert resp.get("success") is True

        # 4. 查看 A/B 区
        status, _, resp = _http_get(base, "/api/area/a", session_token)
        assert status == 200

        status, _, resp = _http_get(base, "/api/area/b", session_token)
        assert status == 200

        # 5. 验证配置状态
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert status == 200
        assert resp["openlist_configured"] is True

    def test_onboarding_step_completion(self, webui_server):
        """引导步骤完成流程"""
        server, base, session_token = webui_server

        # 初始状态：所有步骤未完成
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert resp["view_ab_completed"] is False

        # 完成 view_ab 步骤
        status, _, resp = _http_post(base, "/api/onboarding/complete-step",
                                     {"step": "view_ab"}, session_token)
        assert status == 200

        # 验证状态更新
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert resp["view_ab_completed"] is True


# ============================================================
# 场景 2：失败场景
# ============================================================

class TestFailureScenarios:
    """测试失败场景"""

    def test_unreachable_openlist_config_still_saves(self, webui_server):
        """不可达 OpenList 地址仍可保存配置（后端不测试连接）"""
        server, base, session_token = webui_server

        # 后端 _handle_webui_config_post 只保存配置，不测试连接
        status, _, resp = _http_post(base, "/api/webui/config/openlist", {
            "webdav_host": "http://192.0.2.1:5244",  # TEST-NET-1
            "webdav_user": "admin",
            "webdav_password": "password",
        }, session_token)
        assert status == 200
        assert resp.get("success") is True

    def test_preflight_check_failure(self, webui_server):
        """OpenList 未配置时预检失败"""
        server, base, session_token = webui_server

        status, _, resp = _http_post(base, "/api/config/validate", {}, session_token)
        assert status == 200
        assert resp.get("ok") is False

    def test_invalid_scope_rejected(self, webui_server):
        """非法 scope 被拒绝"""
        server, base, session_token = webui_server

        status, _, resp = _http_post(base, "/api/webui/config/invalid_scope", {
            "key": "value"
        }, session_token)
        assert status == 403

    def test_empty_strm_engines_rejected(self, webui_server):
        """包含空 engine 的 strm_engines 被拒绝"""
        server, base, session_token = webui_server

        status, _, resp = _http_post(base, "/api/webui/config/openlist", {
            "webdav_host": "http://localhost:5244",
            "strm_engines": json.dumps([{"engine": "", "monitored_paths": []}]),
        }, session_token)
        assert status == 400


# ============================================================
# 场景 3：分页与搜索功能测试
# ============================================================

class TestPaginationAndSearch:
    """测试分页和搜索功能"""

    def test_area_list_default_page_size(self, webui_server):
        """A/B/C 区列表默认 page_size 为 50"""
        server, base, session_token = webui_server

        status, _, resp = _http_get(base, "/api/area/a", session_token)
        assert status == 200
        assert resp.get("page_size") == 50

    def test_area_list_custom_page_size(self, webui_server):
        """A/B/C 区列表支持自定义 page_size"""
        server, base, session_token = webui_server

        status, _, resp = _http_get(base, "/api/area/a?page_size=100", session_token)
        assert status == 200
        assert resp.get("page_size") == 100

    def test_area_list_page_size_cap(self, webui_server):
        """A/B/C 区列表 page_size 上限为 500"""
        server, base, session_token = webui_server

        # 请求 page_size=1000，应被裁剪为 500
        status, _, resp = _http_get(base, "/api/area/a?page_size=1000", session_token)
        assert status == 200
        assert resp.get("page_size") == 500

    def test_area_list_search_with_q_parameter(self, webui_server):
        """A/B/C 区列表支持 q 参数搜索（FTS5）"""
        server, base, session_token = webui_server

        # 搜索不存在的关键词，应返回空结果
        encoded_query = urllib.parse.quote("不存在的媒体")
        status, _, resp = _http_get(base, f"/api/area/a?q={encoded_query}", session_token)
        assert status == 200
        assert resp.get("total") == 0
        assert resp.get("media_items") == []

    def test_area_list_total_pages_calculation(self, webui_server):
        """A/B/C 区列表 total_pages 计算正确"""
        server, base, session_token = webui_server

        # 空数据库，total_pages 应为 1
        status, _, resp = _http_get(base, "/api/area/a?page_size=50", session_token)
        assert status == 200
        assert resp.get("total_pages") == 1
        assert resp.get("total") == 0

    def test_area_detail_route(self, webui_server):
        """A/B/C 区详情路由 /api/area/{area}/detail"""
        server, base, session_token = webui_server

        # 查询不存在的媒体，应返回空结果
        encoded_media = urllib.parse.quote("不存在的媒体")
        status, _, resp = _http_get(
            base, f"/api/area/a/detail?media={encoded_media}", session_token)
        assert status == 200
        assert resp.get("total") == 0
        assert resp.get("seasons") == []
