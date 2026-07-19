"""
新手引导端到端测试。

覆盖场景：
1. 引导流程：7 个步骤的完成状态跟踪
2. 启动预检：OpenList/TMDB 配置检查
3. 完整旅程：从登录到完成引导
4. 配置联动：配置状态与引导步骤的关联
"""

from __future__ import annotations

import json
import os
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

from webui.server import WebUIServer  # noqa: E402


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
    """构造最小化 AppConfig mock。"""
    cfg = MagicMock()
    cfg.webui.enabled = True
    cfg.webui.port = 0
    cfg.webui.bind = "127.0.0.1"
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

        # 登录并获取 session token
        login_status, _, login_body = _http_post(
            base_url, "/api/login", {"password": test_password})
        assert login_status == 200
        session_token = login_body.get("token")
        assert session_token is not None

        yield server, base_url, session_token

        server.stop()


def _http_get(base_url: str, path: str, session_token: str | None = None, timeout: float = 3.0):
    """发送 GET 请求并返回 (status, headers, body_dict)。"""
    url = f"{base_url}{path}"
    req = urllib.request.Request(url, method="GET", headers={"X-Session-Token": session_token}) if session_token else urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype:
                json_body = json.loads(body)
                return resp.status, resp.headers, json_body
            return resp.status, resp.headers, body
    except urllib.error.HTTPError as e:
        body = e.read()
        ctype = e.headers.get("Content-Type", "")
        if "application/json" in ctype:
            return e.code, e.headers, json.loads(body)
        return e.code, e.headers, body


def _http_post(base_url: str, path: str, body: dict | None = None, session_token: str | None = None, timeout: float = 3.0):
    """发送 POST 请求并返回 (status, headers, body_dict)。"""
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
# 场景 1：引导流程
# ============================================================

class TestOnboardingFlow:
    """测试引导流程的 7 个步骤"""

    def test_initial_status_all_false(self, webui_server):
        """首次启动时所有引导步骤都未完成"""
        server, base, session_token = webui_server
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert status == 200
        assert resp["onboarding_completed"] is False
        assert resp["view_ab_completed"] is False
        assert resp["tmdb_refresh_completed"] is False
        assert resp["tmdb_match_completed"] is False

    def test_complete_step_view_ab(self, webui_server):
        """标记 view_ab 步骤完成"""
        server, base, session_token = webui_server
        
        # 标记步骤完成
        status, _, resp = _http_post(base, "/api/onboarding/complete-step", 
                                     {"step": "view_ab"}, session_token)
        assert status == 200
        assert resp.get("ok") is True
        
        # 验证状态更新
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert resp["view_ab_completed"] is True
        assert resp["tmdb_refresh_completed"] is False
        assert resp["tmdb_match_completed"] is False

    def test_complete_step_tmdb_refresh(self, webui_server):
        """标记 tmdb_refresh 步骤完成"""
        server, base, session_token = webui_server
        
        status, _, resp = _http_post(base, "/api/onboarding/complete-step", 
                                     {"step": "tmdb_refresh"}, session_token)
        assert status == 200
        assert resp.get("ok") is True
        
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert resp["tmdb_refresh_completed"] is True

    def test_complete_step_tmdb_match(self, webui_server):
        """标记 tmdb_match 步骤完成"""
        server, base, session_token = webui_server
        
        status, _, resp = _http_post(base, "/api/onboarding/complete-step", 
                                     {"step": "tmdb_match"}, session_token)
        assert status == 200
        assert resp.get("ok") is True
        
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert resp["tmdb_match_completed"] is True

    def test_complete_all_steps(self, webui_server):
        """完成所有新增步骤"""
        server, base, session_token = webui_server
        
        # 依次完成 3 个步骤
        for step in ["view_ab", "tmdb_refresh", "tmdb_match"]:
            status, _, resp = _http_post(base, "/api/onboarding/complete-step", 
                                         {"step": step}, session_token)
            assert status == 200
            assert resp.get("ok") is True
        
        # 验证所有步骤都已完成
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert resp["view_ab_completed"] is True
        assert resp["tmdb_refresh_completed"] is True
        assert resp["tmdb_match_completed"] is True

    def test_invalid_step_rejected(self, webui_server):
        """无效步骤名应返回 400"""
        server, base, session_token = webui_server
        
        status, _, resp = _http_post(base, "/api/onboarding/complete-step", 
                                     {"step": "invalid_step"}, session_token)
        assert status == 400
        assert "invalid step" in resp.get("error", "").lower()

    def test_missing_step_parameter(self, webui_server):
        """缺少 step 参数应返回 400"""
        server, base, session_token = webui_server
        
        status, _, resp = _http_post(base, "/api/onboarding/complete-step", 
                                     {}, session_token)
        assert status == 400


# ============================================================
# 场景 2：启动预检
# ============================================================

class TestStartupPreflight:
    """测试启动预检 API"""

    def test_preflight_openlist_unconfigured(self, webui_server):
        """OpenList 未配置时预检失败"""
        server, base, session_token = webui_server
        
        status, _, resp = _http_post(base, "/api/config/validate", {}, session_token)
        assert status == 200
        assert resp["ok"] is False
        
        # 检查检查结果
        checks = resp.get("checks", [])
        openlist_config_check = next((c for c in checks if c["name"] == "openlist_config"), None)
        assert openlist_config_check is not None
        assert openlist_config_check["status"] == "error"

    def test_preflight_openlist_configured_but_offline(self, webui_server):
        """OpenList 已配置但不可达时预检通过（仅警告）"""
        server, base, session_token = webui_server
        
        # 配置 OpenList 但使用不可达地址
        server._config.webdav.host = "http://192.0.2.1:5244"  # TEST-NET-1
        
        # 使用 mock 避免真实网络调用
        with patch("webdav_client.OpenListAdminClient") as mock_client:
            mock_client.return_value.login.return_value = False
            mock_client.return_value.last_error_type = "network_error"
            
            status, _, resp = _http_post(base, "/api/config/validate", {}, session_token)
        
        assert status == 200
        # 根据实现，openlist_online 失败时降级为 warning，不阻塞
        assert resp["ok"] is True
        
        checks = resp.get("checks", [])
        openlist_online_check = next((c for c in checks if c["name"] == "openlist_online"), None)
        assert openlist_online_check is not None
        # 应该是 warning 或 error，但不影响 ok=True
        assert openlist_online_check["status"] in ("warning", "error")

    def test_preflight_tmdb_unconfigured_warning(self, webui_server):
        """TMDB 未配置时预检通过（警告级别）"""
        server, base, session_token = webui_server
        
        # 配置 OpenList 使预检通过
        server._config.webdav.host = "http://localhost:5244"
        
        status, _, resp = _http_post(base, "/api/config/validate", {}, session_token)
        assert status == 200
        assert resp["ok"] is True
        
        checks = resp.get("checks", [])
        tmdb_check = next((c for c in checks if c["name"] == "tmdb_config"), None)
        assert tmdb_check is not None
        assert tmdb_check["status"] == "warning"


# ============================================================
# 场景 3：完整旅程
# ============================================================

class TestCompleteJourney:
    """测试从登录到完成引导的完整旅程"""

    def test_full_onboarding_journey(self, webui_server):
        """完整引导旅程：登录 → 查看引导 → 完成步骤 → 完成引导"""
        server, base, session_token = webui_server
        
        # 1. 登录（已在 fixture 中完成）
        assert session_token is not None
        
        # 2. 查看引导状态
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert status == 200
        assert resp["onboarding_completed"] is False
        
        # 3. 完成所有新增步骤
        for step in ["view_ab", "tmdb_refresh", "tmdb_match"]:
            status, _, resp = _http_post(base, "/api/onboarding/complete-step", 
                                         {"step": step}, session_token)
            assert status == 200
        
        # 4. 验证所有步骤完成
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert resp["view_ab_completed"] is True
        assert resp["tmdb_refresh_completed"] is True
        assert resp["tmdb_match_completed"] is True
        
        # 5. 完成引导（设置 onboarding_completed）
        status, _, resp = _http_post(base, "/api/webui/config/ui", 
                                     {"onboarding_completed": "1"}, session_token)
        assert status == 200
        
        # 6. 验证引导已完成
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert resp["onboarding_completed"] is True


class TestOnboardingCompleteViaConfig:
    """整体完成/跳过的端到端闭环（前端 skip/complete 共用 POST /api/webui/config/ui）。

    与单步 complete-step 不同，这里测试「一次性标记引导完成/复位」这一路径，
    覆盖前端「跳过引导」「完成引导」「重新开始引导」按钮。
    """

    def test_onboarding_completed_via_config(self, webui_server):
        """POST /api/webui/config/ui {onboarding_completed:'1'} → status 返回 true。"""
        server, base, session_token = webui_server

        # 初始未完成
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert status == 200
        assert resp["onboarding_completed"] is False

        # 走前端 skip/complete 共用路径
        status, _, resp = _http_post(
            base, "/api/webui/config/ui",
            {"onboarding_completed": "1"}, session_token)
        assert status == 200

        # 标记完成后 status 应为 true（前端据此隐藏引导卡片）
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert status == 200
        assert resp["onboarding_completed"] is True

    def test_onboarding_reset(self, webui_server):
        """POST /api/webui/config/ui {onboarding_completed:'0'} → 状态复位为 false。"""
        server, base, session_token = webui_server

        # 先标记完成
        status, _, resp = _http_post(
            base, "/api/webui/config/ui",
            {"onboarding_completed": "1"}, session_token)
        assert status == 200
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert resp["onboarding_completed"] is True

        # 复位（前端「重新开始引导」）
        status, _, resp = _http_post(
            base, "/api/webui/config/ui",
            {"onboarding_completed": "0"}, session_token)
        assert status == 200

        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert status == 200
        assert resp["onboarding_completed"] is False


# ============================================================
# 场景 4：配置联动
# ============================================================

class TestConfigurationLinkage:
    """测试配置状态与引导步骤的关联"""

    def test_openlist_configured_updates_status(self, webui_server):
        """配置 OpenList 后 openlist_configured 变为 True"""
        server, base, session_token = webui_server
        
        # 初始状态
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert resp["openlist_configured"] is False
        
        # 配置 OpenList
        server._config.webdav.host = "http://localhost:5244"
        
        # 验证状态更新
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert resp["openlist_configured"] is True

    def test_tmdb_configured_updates_status(self, webui_server):
        """配置 TMDB 后 tmdb_configured 变为 True"""
        server, base, session_token = webui_server
        
        # 初始状态
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert resp["tmdb_configured"] is False
        
        # 模拟 TMDB 已配置（通过 mock watchlist_db）
        with patch.object(server._watchlist_db, 'get_all_config', 
                         return_value={"access_token": "test_token"}):
            status, _, resp = _http_get(base, "/api/config/status", session_token)
            assert resp["tmdb_configured"] is True

    def test_main_running_updates_status(self, webui_server):
        """主程序运行后 main_running 变为 True"""
        server, base, session_token = webui_server
        
        # 初始状态
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert resp["main_running"] is False
        
        # 模拟主程序运行
        server._app_running = True
        
        # 验证状态更新
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert resp["main_running"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
