"""白名单免 token 契约的 HTTP 级行为测试（回归锚定）。

锚定根 AGENTS.md 认证白名单描述：/api/config 与 /api/webui/config/ui 仅 GET
免 token，POST 必须认证；/api/admin/status 为双语义（无 token 200，带 token
走标准校验，无效 token 401）；静态资产免 token。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from webui_fixtures import http_get, http_post  # noqa: E402


@pytest.fixture
def shared_server(tmp_path):
    from webui_fixtures import start_webui_server
    server, base_url, token = start_webui_server(tmp_path)
    try:
        yield server, base_url, token
    finally:
        server.stop()


class TestWhitelistContract:
    """GET 免 token 白名单与 POST 认证强制。"""

    def test_api_config_get_without_token_allowed(self, shared_server):
        _, base, _ = shared_server
        status, _, _ = http_get(base, "/api/config", session_token=None)
        assert status == 200

    def test_api_webui_config_ui_get_without_token_allowed(self, shared_server):
        _, base, _ = shared_server
        status, _, _ = http_get(base, "/api/webui/config/ui", session_token=None)
        assert status == 200

    def test_api_config_post_without_token_rejected(self, shared_server):
        _, base, _ = shared_server
        status, _, body = http_post(base, "/api/config", {}, session_token=None)
        assert status in (401, 403), f"POST /api/config 无 token 必须被拒，实际 {status}: {body}"

    def test_api_webui_config_ui_post_without_token_rejected(self, shared_server):
        _, base, _ = shared_server
        status, _, body = http_post(
            base, "/api/webui/config/ui", {"onboarding_completed": "1"}, session_token=None)
        assert status in (401, 403), f"POST /api/webui/config/ui 无 token 必须被拒，实际 {status}: {body}"

    def test_admin_status_dual_semantics_no_token_200(self, shared_server):
        """M5 双语义：无 token 免 Token 校验，返回 200。"""
        _, base, _ = shared_server
        status, _, _ = http_get(base, "/api/admin/status", session_token=None)
        assert status == 200

    def test_admin_status_dual_semantics_with_token_standard_check(self, shared_server):
        """M5 双语义：带 token 走标准校验，无效 token 返回 401。"""
        _, base, _ = shared_server
        status, _, _ = http_get(base, "/api/admin/status", session_token="invalid_token_xyz")
        assert status == 401

    def test_static_assets_without_token_allowed(self, shared_server):
        _, base, _ = shared_server
        status, _, _ = http_get(base, "/favicon.ico", session_token=None)
        assert status == 200

    def test_protected_route_without_token_rejected(self, shared_server):
        """白名单之外的路由无 token 必须 401。"""
        _, base, _ = shared_server
        status, _, body = http_get(base, "/api/logs", session_token=None)
        assert status == 401
        assert isinstance(body, dict)
        assert body.get("error") == "unauthorized"
