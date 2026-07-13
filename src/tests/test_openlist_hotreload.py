"""HotReload 保留旧客户端专项测试。

测试目标：webui/routes.py 中的 `_reinit_admin_client` 与
`_hot_reload_openlist_config` 函数。

核心回归守卫：用户原始报错
    ERROR:webdav_client:登录请求失败: 'NoneType' object has no attribute 'get'
    WARNING:root:[HotReload] 新的 OpenListAdminClient 登录失败: ... — 保留旧客户端继续运行

关键实现细节：
- `_reinit_admin_client`（routes.py:1061）与 `_handle_openlist_test_connection`
  （routes.py:1101）都用**局部 import** `from webdav_client import OpenListAdminClient`，
  每次函数调用都重新执行此 import，从 `webdav_client` 模块读取名称。
  因此 patch 目标必须是 **`webdav_client.OpenListAdminClient`**（源模块），
  而非 `routes.OpenListAdminClient`。错误的 patch 目标会静默不生效。
- 登录失败时必须保留旧 client 引用，避免用无效客户端冲掉正常工作实例。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add src/ to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webui.routes import _reinit_admin_client, _hot_reload_openlist_config
from webdav_client import OpenListAdminClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(data: dict | None = None, status: int = 200) -> MagicMock:
    """Build a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status
    resp.text = json.dumps(data) if data is not None else ""
    resp.content = resp.text.encode("utf-8")
    if data is not None:
        resp.json.return_value = data
    else:
        resp.json.side_effect = ValueError("not json")
    resp.raise_for_status = MagicMock()
    return resp


def _make_mock_server() -> MagicMock:
    """构造 _reinit_admin_client / _hot_reload_openlist_config 所需的 mock server。

    结构参考 routes.py:988-1080 对 webui_server 属性的访问：
      - server._config.webdav.{host,user,password,totp_secret}
      - server._admin_client（旧客户端）
      - server._app_service（含 admin_api 属性）
      - server._watchlist_db（_hot_reload_openlist_config 用）
    """
    server = MagicMock()
    server._config.webdav.host = "http://openlist:5244"
    server._config.webdav.user = "admin"
    server._config.webdav.password = "pw"
    server._config.webdav.totp_secret = ""
    # 日志配置（_hot_reload_openlist_config 读取 cfg.log.*）
    server._config.log.level = "INFO"
    server._config.log.max_size_mb = 2
    server._config.log.backup_count = 5
    server._config.log.file = ""
    # 旧客户端引用（用具名 MagicMock 便于身份比较）
    old_client = MagicMock(name="old_client")
    server._admin_client = old_client
    # AppService mock，含 admin_api 属性
    app_service = MagicMock(name="app_service")
    old_admin_api = MagicMock(name="old_admin_api")
    app_service.admin_api = old_admin_api
    server._app_service = app_service
    # watchlist db（_hot_reload_openlist_config 调 cfg.update_from_db(_wdb)）
    server._watchlist_db = MagicMock(name="watchlist_db")
    return server


# ===========================================================================
# _reinit_admin_client
# ===========================================================================


class TestReinitAdminClient:
    """_reinit_admin_client 保留旧客户端行为测试。"""

    def test_reinit_keeps_old_client_when_login_fails(self):
        """登录失败 → 旧 client 保留，AppService.admin_api 不被替换。"""
        server = _make_mock_server()
        old_client = server._admin_client
        old_admin_api = server._app_service.admin_api

        mock_class = MagicMock()
        mock_instance = MagicMock()
        mock_class.return_value = mock_instance
        mock_instance.login.return_value = False
        mock_instance.last_error_type = "wrong_password"
        mock_instance.last_error_message = "密码错误"

        with patch("webdav_client.OpenListAdminClient", mock_class):
            _reinit_admin_client(server)

        # 旧 client 仍是同一引用
        assert server._admin_client is old_client
        # AppService.admin_api 未被替换
        assert server._app_service.admin_api is old_admin_api

    def test_reinit_replaces_when_login_succeeds(self):
        """登录成功 → server._admin_client 替换为新实例，AppService.admin_api 同步更新。"""
        server = _make_mock_server()
        old_client = server._admin_client

        mock_class = MagicMock()
        mock_instance = MagicMock()
        mock_class.return_value = mock_instance
        mock_instance.login.return_value = True

        with patch("webdav_client.OpenListAdminClient", mock_class):
            _reinit_admin_client(server)

        # 新 client 已替换
        assert server._admin_client is mock_instance
        assert server._admin_client is not old_client
        # AppService.admin_api 同步更新为新 client
        assert server._app_service.admin_api is mock_instance

    def test_reinit_forces_login_not_cached(self):
        """回归守卫：_reinit_admin_client 必须调用 login(force=True)，而非 login()。

        修复前调用 login()（无 force=True），新实例会加载缓存 token 直接返回 True，
        导致修改配置后不会真实验证连接。修复后调用 login(force=True)，强制真实验证。
        """
        server = _make_mock_server()

        mock_class = MagicMock()
        mock_instance = MagicMock()
        mock_class.return_value = mock_instance
        mock_instance.login.return_value = True

        with patch("webdav_client.OpenListAdminClient", mock_class):
            _reinit_admin_client(server)

        # 关键回归断言：login 必须被调用时传入了 force=True
        mock_instance.login.assert_called_once_with(force=True)

    def test_reinit_login_data_null_does_not_raise(self):
        """回归守卫：真实 OpenListAdminClient + data:null 响应不抛 AttributeError。

        模拟用户原始报错场景：OpenList API 返回 {"data": null, "message": "..."}，
        旧代码 data.get("data", {}).get("token") 会抛
        'NoneType' object has no attribute 'get'。修复后 login() 安全返回 False，
        _reinit_admin_client 保留旧 client，无异常逃逸。
        """
        server = _make_mock_server()
        old_client = server._admin_client

        # 用真实 OpenListAdminClient，但 mock session.post 返回 data:null
        real_instance = OpenListAdminClient.__new__(OpenListAdminClient)
        real_instance.host = "http://openlist:5244"
        real_instance.user = "admin"
        real_instance.password = "pw"
        real_instance.totp_secret = ""
        real_instance.token = None
        real_instance.session = MagicMock()
        real_instance.session.post.return_value = _make_response(
            {"data": None, "message": "authentication failed"}
        )
        real_instance._fs_list_logged = set()
        real_instance._fs_list_logged_time = 0.0
        real_instance._check_exists_cache = {}
        real_instance._check_exists_cache_ttl = 60
        real_instance.last_error_message = None
        real_instance.last_error_type = None
        real_instance.token_cache_path = ""

        mock_class = MagicMock(return_value=real_instance)

        # 不应抛出 AttributeError
        with patch("webdav_client.OpenListAdminClient", mock_class):
            _reinit_admin_client(server)

        # login 失败 → 旧 client 保留
        assert server._admin_client is old_client
        # login() 应安全返回 False 且错误类型为 unknown（data:null 走新防御分支）
        assert real_instance.last_error_type == "unknown"

    def test_reinit_constructor_exception_keeps_old_client(self):
        """构造 OpenListAdminClient 抛异常 → 旧 client 保留，无异常逃逸。

        覆盖 routes.py:1079-1080 的外层 except Exception。
        """
        server = _make_mock_server()
        old_client = server._admin_client

        mock_class = MagicMock(side_effect=RuntimeError("ctor boom"))

        with patch("webdav_client.OpenListAdminClient", mock_class):
            # 不应抛出
            _reinit_admin_client(server)

        assert server._admin_client is old_client


# ===========================================================================
# _hot_reload_openlist_config
# ===========================================================================


class TestHotReloadOpenlistConfig:
    """_hot_reload_openlist_config 异常吞咽测试。"""

    def test_hot_reload_strm_reload_failure_is_swallowed(self):
        """load_strm_storage_from_api 抛异常 → 被 except 吞咽，无异常逃逸。

        覆盖 routes.py:1049-1053：STRM 存储映射重载失败仅记 warning。
        """
        server = _make_mock_server()
        # update_from_db 为 no-op（MagicMock 默认）
        server._config.update_from_db = MagicMock()
        # webdav 配置未变 → 不触发 _reinit_admin_client，直接走 load_strm_storage_from_api
        server._config.load_strm_storage_from_api = MagicMock(
            side_effect=RuntimeError("boom")
        )

        # 不应抛出
        _hot_reload_openlist_config(server)

        server._config.load_strm_storage_from_api.assert_called_once()

    def test_hot_reload_no_watchlist_db_returns_early(self):
        """无 _watchlist_db → 提前返回，不执行 update_from_db。"""
        server = _make_mock_server()
        server._watchlist_db = None

        _hot_reload_openlist_config(server)

        server._config.update_from_db.assert_not_called()

    def test_hot_reload_webdav_changed_triggers_reinit(self):
        """webdav 连接信息变更 → 触发 _reinit_admin_client。

        通过 patch _reinit_admin_client 验证调用，避免真实登录。
        """
        server = _make_mock_server()
        server._config.update_from_db = MagicMock()

        # update_from_db 后改变 host，使新旧 host 不同 → 触发 reinit
        def _change_host(_wdb):
            server._config.webdav.host = "http://newhost:5244"

        server._config.update_from_db.side_effect = _change_host
        server._config.load_strm_storage_from_api = MagicMock()

        with patch("webui.routes._reinit_admin_client") as mock_reinit:
            _hot_reload_openlist_config(server)
            mock_reinit.assert_called_once_with(server)

    def test_hot_reload_outer_exception_is_swallowed(self):
        """cfg 访问抛异常 → 外层 except 吞咽，无异常逃逸。

        覆盖 routes.py:1054-1055 的最外层 except Exception。
        """
        server = _make_mock_server()
        # 让 cfg.webdav.host 属性访问抛异常，触发外层 except
        type(server._config.webdav).host = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("cfg broken"))
        )

        # 不应抛出
        _hot_reload_openlist_config(server)
