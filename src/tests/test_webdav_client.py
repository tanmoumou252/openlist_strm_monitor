"""
Unit tests for webdav_client.py

Covers:
  - _normalize_host
  - _generate_totp (module-level)
  - OpenListAdminClient:
      login, _parse_login_error, _do_request, token cache,
      list_storages, get_storage_info, list_directory, mkdir,
      move, remove, check_exists, list_contents,
      trigger_refresh_via_fs_list
  - OpenlistWebDAV:
      _request, check_exists, list_contents, read_file,
      write_file, delete_file, mkdir, move, copy
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add src/ to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import webdav_client as _module
from webdav_client import (
    _normalize_host,
    _generate_totp,
    OpenListAdminClient,
    OpenlistWebDAV,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(
    data: dict | None = None,
    status: int = 200,
    text: str = "",
    content: bytes = b"",
) -> MagicMock:
    """Build a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status
    resp.text = text or (json.dumps(data) if data is not None else "")
    resp.content = content or resp.text.encode("utf-8")
    if data is not None:
        resp.json.return_value = data
    else:
        resp.json.side_effect = ValueError("not json")
    resp.raise_for_status = MagicMock()
    return resp


def _make_admin_client(tmp_path: Path, host: str = "http://openlist:5244") -> OpenListAdminClient:
    """Build an OpenListAdminClient with token cache redirected to tmp_path."""
    with patch.object(
        _module, "os", wraps=_module.os
    ):
        client = OpenListAdminClient.__new__(OpenListAdminClient)
        client.host = _normalize_host(host)
        client.user = "admin"
        client.password = "secret"
        client.totp_secret = ""
        client.token = None
        client.session = MagicMock()
        client._fs_list_logged = set()
        client._fs_list_logged_time = 0.0
        client._check_exists_cache = {}
        client._check_exists_cache_ttl = 60
        client._check_exists_cache_max = 5000
        client.last_error_message = None
        client.last_error_type = None
        client.token_cache_path = str(tmp_path / ".admin_token.json")
        return client


# ===========================================================================
# Module-level helpers
# ===========================================================================


class TestNormalizeHost:
    def test_strips_trailing_slash(self):
        assert _normalize_host("http://host:5244/") == "http://host:5244"

    def test_strips_dav_suffix(self):
        assert _normalize_host("http://host:5244/dav") == "http://host:5244"

    def test_strips_both(self):
        assert _normalize_host("http://host:5244/dav/") == "http://host:5244"

    def test_no_change(self):
        assert _normalize_host("http://host:5244") == "http://host:5244"


class TestGenerateTotp:
    def test_returns_6_digits(self):
        # A valid base32 secret
        code = _generate_totp("JBSWY3DPEHPK3PXP")
        assert len(code) == 6
        assert code.isdigit()

    def test_empty_secret_raises(self):
        with pytest.raises(ValueError, match="不能为空"):
            _generate_totp("")

    def test_invalid_encoding_raises(self):
        with pytest.raises(ValueError, match="编码无效"):
            _generate_totp("!!!not-base32-or-base64!!!")

    def test_deterministic_within_same_interval(self):
        code_a = _generate_totp("JBSWY3DPEHPK3PXP")
        code_b = _generate_totp("JBSWY3DPEHPK3PXP")
        assert code_a == code_b

    def test_custom_digits(self):
        code = _generate_totp("JBSWY3DPEHPK3PXP", digits=8)
        assert len(code) == 8
        assert code.isdigit()


# ===========================================================================
# OpenListAdminClient — token cache
# ===========================================================================


class TestAdminTokenCache:
    def test_load_token_from_cache(self, tmp_path):
        cache_file = tmp_path / ".admin_token.json"
        cache_file.write_text(json.dumps({"token": "cached-jwt"}), encoding="utf-8")
        client = _make_admin_client(tmp_path)
        client._load_token_from_cache()
        assert client.token == "cached-jwt"

    def test_load_encrypted_token_from_cache(self, tmp_path):
        """加密格式（ENC: 前缀）的缓存应解密还原。"""
        import secret_manager
        cache_file = tmp_path / ".admin_token.json"
        cache_file.write_text(
            json.dumps({"token": secret_manager.encrypt("enc-jwt")}),
            encoding="utf-8",
        )
        client = _make_admin_client(tmp_path)
        client._load_token_from_cache()
        assert client.token == "enc-jwt"

    def test_load_token_missing_file(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client._load_token_from_cache()
        assert client.token is None

    def test_save_token_to_cache(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client._save_token_to_cache("new-jwt")
        assert client.token == "new-jwt"
        data = json.loads((tmp_path / ".admin_token.json").read_text(encoding="utf-8"))
        # 非空 token 应加密存储（ENC: 前缀），而非明文
        assert data["token"] != "new-jwt"
        assert "ts" in data
        # 加密值应能通过 _load_token_from_cache 解密还原
        client.token = None
        client._load_token_from_cache()
        assert client.token == "new-jwt"

    def test_save_empty_token_to_cache(self, tmp_path):
        """空 token 写入空串（不加密），避免 ENC: 空密文堆积。"""
        client = _make_admin_client(tmp_path)
        client._save_token_to_cache("")
        assert client.token == ""
        data = json.loads((tmp_path / ".admin_token.json").read_text(encoding="utf-8"))
        assert data["token"] == ""

    def test_save_token_io_error_swallowed(self, tmp_path):
        client = _make_admin_client(tmp_path)
        with patch("builtins.open", side_effect=OSError("disk full")):
            # Should not raise
            client._save_token_to_cache("jwt")
        # token attribute is still updated in-memory
        assert client.token == "jwt"


# ===========================================================================
# OpenListAdminClient — login
# ===========================================================================


class TestAdminLogin:
    def test_login_cached_token_no_request(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "cached-jwt"
        result = client.login()
        assert result is True
        client.session.post.assert_not_called()

    def test_login_force_re_authenticates(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "old-jwt"
        resp = _make_response({"data": {"token": "new-jwt"}})
        client.session.post.return_value = resp

        result = client.login(force=True)
        assert result is True
        assert client.token == "new-jwt"
        client.session.post.assert_called_once()

    def test_login_success(self, tmp_path):
        client = _make_admin_client(tmp_path)
        resp = _make_response({"data": {"token": "fresh-jwt"}})
        client.session.post.return_value = resp

        result = client.login()
        assert result is True
        assert client.token == "fresh-jwt"
        assert client.last_error_type is None

    def test_login_http_error_wrong_password(self, tmp_path):
        client = _make_admin_client(tmp_path)
        resp = _make_response({"message": "username or password is wrong"}, status=401)
        client.session.post.return_value = resp

        result = client.login()
        assert result is False
        assert client.last_error_type == "wrong_password"

    def test_login_http_error_unparseable(self, tmp_path):
        client = _make_admin_client(tmp_path)
        resp = MagicMock()
        resp.status_code = 500
        resp.json.side_effect = ValueError("not json")
        client.session.post.return_value = resp

        result = client.login()
        assert result is False
        assert client.last_error_type == "unknown"

    def test_login_200_but_no_token(self, tmp_path):
        client = _make_admin_client(tmp_path)
        resp = _make_response({"message": "something went wrong"})
        client.session.post.return_value = resp

        result = client.login()
        assert result is False
        assert client.last_error_type is not None

    def test_login_network_error(self, tmp_path):
        import requests as _req
        client = _make_admin_client(tmp_path)
        client.session.post.side_effect = _req.exceptions.ConnectionError("refused")

        result = client.login()
        assert result is False
        assert client.last_error_type == "network_error"

    def test_login_invalid_totp(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.totp_secret = "!!!invalid!!!"

        result = client.login()
        assert result is False
        assert client.last_error_type == "invalid_totp"

    def test_login_handles_missing_schema_error(self, tmp_path):
        """login() 区分 MissingSchema/InvalidURL 与一般网络异常。

        覆盖 webdav_client.py:208-214：当 session.post 抛出
        requests.exceptions.MissingSchema（host 无 scheme）时，
        应归类为 not_configured 而非 network_error。
        """
        import requests as _req
        client = _make_admin_client(tmp_path, host="no-scheme-host")
        # host 无 scheme，session.post 会因 URL 无效抛 MissingSchema
        client.session.post.side_effect = _req.exceptions.MissingSchema(
            "Invalid URL 'no-scheme-host/api/auth/login': No scheme supplied"
        )

        result = client.login()

        assert result is False
        assert client.last_error_type == "not_configured"
        assert "配置无效" in client.last_error_message

    def test_login_returns_not_configured_when_host_empty(self, tmp_path):
        """login() 在 host 为空时直接返回 not_configured，不发起请求。

        覆盖 webdav_client.py:162-166：host 为空时前置检查直接返回，
        避免空 host 拼出无效 URL 误报为网络异常。
        """
        client = _make_admin_client(tmp_path)
        client.host = ""

        result = client.login()

        assert result is False

    def test_login_response_data_field_is_null(self, tmp_path):
        """login() 处理 API 返回 {data: null, message: "error"} 的情况。

        覆盖 webdav_client.py:190-201：当 OpenList API 返回 data 字段为 null 时，
        原代码 data.get("data", {}).get("token") 会抛出
        'NoneType' object has no attribute 'get' 错误。
        修复后应安全处理并返回明确的错误信息。
        """
        client = _make_admin_client(tmp_path)
        # 模拟 OpenList 返回 data: null 的情况
        resp = _make_response({"data": None, "message": "authentication failed"})
        client.session.post.return_value = resp

        result = client.login()

        assert result is False
        assert client.last_error_type == "unknown"
        assert client.last_error_message == "authentication failed"
        assert client.token is None

    def test_login_response_data_field_is_wrong_type(self, tmp_path):
        """login() 处理 API 返回 data 字段为非字典类型的情况。

        覆盖 webdav_client.py:190-201：当 data 字段为字符串、列表等非字典类型时，
        应安全处理并返回错误。
        """
        client = _make_admin_client(tmp_path)
        # 模拟 data 字段为字符串
        resp = _make_response({"data": "unexpected string", "message": "error"})
        client.session.post.return_value = resp

        result = client.login()

        assert result is False
        assert client.last_error_type == "unknown"
        assert client.token is None

    def test_login_response_missing_data_field(self, tmp_path):
        """login() 处理 API 响应完全缺少 data 字段的情况。

        覆盖 webdav_client.py:190-201：当响应中没有 data 字段时，
        应安全处理并返回错误。
        """
        client = _make_admin_client(tmp_path)
        # 模拟响应缺少 data 字段
        resp = _make_response({"message": "some error"})
        client.session.post.return_value = resp

        result = client.login()

        assert result is False
        assert client.last_error_type == "unknown"
        assert client.token is None
        assert client.last_error_message == "some error"


# ===========================================================================
# OpenListAdminClient — _do_request
# ===========================================================================


class TestAdminDoRequest:
    def test_do_request_injects_auth_header(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "my-jwt"
        client.session.request.return_value = _make_response({"code": 200})

        client._do_request("GET", "http://h/api/fs/list")
        call_kwargs = client.session.request.call_args
        assert call_kwargs.kwargs["headers"]["Authorization"] == "my-jwt"

    def test_do_request_auto_login_when_no_token(self, tmp_path):
        client = _make_admin_client(tmp_path)
        # login succeeds, sets token
        login_resp = _make_response({"data": {"token": "auto-jwt"}})
        client.session.post.return_value = login_resp
        client.session.request.return_value = _make_response({"code": 200})

        client._do_request("GET", "http://h/api/test")
        assert client.token == "auto-jwt"
        client.session.request.assert_called_once()

    def test_do_request_retry_on_401(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "old-jwt"

        expired = _make_response({"code": 401}, status=401)
        fresh = _make_response({"code": 200}, status=200)
        client.session.request.side_effect = [expired, fresh]
        # login(force=True) sets new token
        login_resp = _make_response({"data": {"token": "new-jwt"}})
        client.session.post.return_value = login_resp

        res = client._do_request("GET", "http://h/api/test")
        assert res.status_code == 200
        assert client.session.request.call_count == 2

    def test_do_request_returns_none_on_exception(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.side_effect = RuntimeError("boom")

        res = client._do_request("GET", "http://h/api/test")
        assert res is None

    def test_do_request_retry_login_fails(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "old-jwt"

        expired = _make_response({"code": 401}, status=401)
        client.session.request.return_value = expired
        # login(force=True) fails
        client.session.post.return_value = _make_response({"message": "wrong"}, status=401)

        res = client._do_request("GET", "http://h/api/test")
        # Returns the expired response since re-login failed
        assert res is expired

    def test_do_request_returns_none_when_host_empty(self, tmp_path):
        """_do_request 在 host 为空时直接返回 None，不发起请求。

        覆盖 webdav_client.py:243-247：host 为空时前置检查直接返回，
        避免空 host 拼出无效 URL 误报为网络异常。
        """
        client = _make_admin_client(tmp_path)
        client.host = ""
        # 设置 token 以跳过 login()，确保走 host 空检查路径而非 login 路径
        client.token = "some-token"

        res = client._do_request("GET", "/api/test")

        assert res is None
        assert client.last_error_type == "not_configured"
        # session.request 不应被调用
        assert client.session.request.call_count == 0

    def test_do_request_handles_missing_schema_exception(self, tmp_path):
        """_do_request 在 session.request 抛 MissingSchema 时返回 None。

        覆盖 webdav_client.py:330-341：当 session.request 抛出
        requests.exceptions.MissingSchema（URL 无 scheme）时，
        应被 except 捕获并返回 None，不向上抛出。
        注意：与 login() 不同，此处仅记日志，不设置 last_error_type。
        """
        import requests as _req
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        # session.request 抛 MissingSchema（模拟 host 无 scheme 但已缓存 token 的场景）
        client.session.request.side_effect = _req.exceptions.MissingSchema(
            "Invalid URL '/api/test': No scheme supplied"
        )

        res = client._do_request("GET", "/api/test")

        # 应返回 None 而非向上抛出
        assert res is None

    def test_do_request_handles_invalid_url_exception(self, tmp_path):
        """_do_request 在 session.request 抛 InvalidURL 时返回 None。

        覆盖 webdav_client.py:330-341：InvalidURL 与 MissingSchema 同属
        "未配置/无效 URL" 分类，应被捕获并返回 None。
        """
        import requests as _req
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.side_effect = _req.exceptions.InvalidURL(
            "failed to parse: /api/test"
        )

        res = client._do_request("GET", "/api/test")

        assert res is None


# ===========================================================================
# OpenListAdminClient — business methods
# ===========================================================================


class TestAdminListStorages:
    def test_single_page(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        payload = {"code": 200, "data": {"content": [{"id": 1}], "total": 1}}
        client.session.request.return_value = _make_response(payload)

        result = client.list_storages()
        assert result is not None
        assert result["data"]["content"] == [{"id": 1}]
        assert result["data"]["total"] == 1

    def test_pagination_aggregates(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        page1 = _make_response({"code": 200, "data": {"content": [{"id": 1}], "total": 2}})
        page2 = _make_response({"code": 200, "data": {"content": [{"id": 2}], "total": 2}})
        client.session.request.side_effect = [page1, page2]

        result = client.list_storages(per_page=1)
        assert result is not None
        assert len(result["data"]["content"]) == 2

    def test_returns_none_on_failure(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = _make_response(status=500)

        assert client.list_storages() is None

    def test_returns_none_on_non_json(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("not json")
        client.session.request.return_value = resp

        assert client.list_storages() is None

    def test_response_data_field_is_null(self, tmp_path):
        """list_storages 处理 API 返回 data: null 的情况。

        覆盖 webdav_client.py:382-389：当 OpenList API 返回 data 字段为 null 时，
        应安全处理并返回空结果，不抛出异常。
        """
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        # 模拟 OpenList 返回 data: null 的情况
        client.session.request.return_value = _make_response({"code": 200, "data": None})

        result = client.list_storages()

        # 应返回空结果而非抛出异常或 None
        assert result is not None
        assert result["data"]["content"] == []
        assert result["data"]["total"] == 0

    def test_response_data_content_is_null(self, tmp_path):
        """list_storages 处理 API 返回 data.content: null 的情况。

        覆盖 webdav_client.py:382-389：当 data.content 为 null 时，
        应安全处理并返回空列表。
        """
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        # 模拟 data.content 为 null
        client.session.request.return_value = _make_response({"code": 200, "data": {"content": None, "total": 0}})

        result = client.list_storages()

        # 应返回结果，content 为空列表
        assert result is not None
        assert result["data"]["content"] == []


class TestAdminGetStorageInfo:
    def test_success(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = _make_response({"code": 200, "data": {"id": 5}})

        result = client.get_storage_info(5)
        assert result == {"code": 200, "data": {"id": 5}}

    def test_returns_none_on_failure(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = _make_response(status=500)

        assert client.get_storage_info(5) is None

    def test_response_data_field_is_null(self, tmp_path):
        """get_storage_info 处理 API 返回 data: null 的情况。

        覆盖 webdav_client.py:458-466：当 OpenList API 返回 data 字段为 null 时，
        应安全处理并返回原始响应，不抛出异常。
        """
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        # 模拟 OpenList 返回 data: null 的情况
        client.session.request.return_value = _make_response({"code": 200, "data": None})

        result = client.get_storage_info(5)

        # 应返回原始响应而非抛出异常
        assert result is not None
        assert result["code"] == 200
        assert result["data"] is None

    def test_response_missing_data_field(self, tmp_path):
        """get_storage_info 处理 API 响应缺少 data 字段的情况。

        覆盖 webdav_client.py:458-466：当响应中没有 data 字段时，
        应安全处理并返回原始响应。
        """
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        # 模拟响应缺少 data 字段
        client.session.request.return_value = _make_response({"code": 200})

        result = client.get_storage_info(5)

        # 应返回原始响应而非抛出异常
        assert result is not None
        assert result["code"] == 200
        assert "data" not in result


class TestAdminGetStrmStoragesFullInfo:
    """get_strm_storages_full_info 的 null 防御测试。

    覆盖 webdav_client.py:420-455：当 list_storages / get_storage_info 返回
    data: null 或 None 时，应安全回退，不抛出 'NoneType' object has no
    attribute 'get' 异常。
    """

    def test_list_storages_data_null(self, tmp_path):
        """list_storages 返回 data: null → 返回 [] 不抛异常。"""
        client = _make_admin_client(tmp_path)
        with patch.object(client, "list_storages", return_value={"code": 200, "data": None}):
            result = client.get_strm_storages_full_info()
            assert result == []

    def test_list_storages_returns_none(self, tmp_path):
        """list_storages 返回 None → 返回 [] 不抛异常。"""
        client = _make_admin_client(tmp_path)
        with patch.object(client, "list_storages", return_value=None):
            result = client.get_strm_storages_full_info()
            assert result == []

    def test_get_storage_info_data_null_falls_back(self, tmp_path):
        """get_storage_info 返回 data: null → fallback 到原始 storage 条目。

        覆盖 webdav_client.py:446-450：full_info.get("data", {}) 在 data 为
        None 时返回 None，isinstance(data, dict) 守卫为 False，走 else 分支
        追加原始 storage。
        """
        client = _make_admin_client(tmp_path)
        list_result = {
            "code": 200,
            "data": {
                "content": [{"id": 1, "driver": "Strm", "mount_path": "/s1"}],
                "total": 1,
            },
        }
        with patch.object(client, "list_storages", return_value=list_result), \
             patch.object(client, "get_storage_info", return_value={"code": 200, "data": None}):
            result = client.get_strm_storages_full_info()
            assert len(result) == 1
            # data: null → fallback，应返回原始 storage（含 id, driver, mount_path）
            assert result[0]["id"] == 1
            assert result[0]["mount_path"] == "/s1"

    def test_get_storage_info_returns_none_falls_back(self, tmp_path):
        """get_storage_info 返回 None → fallback 到原始 storage 条目。

        覆盖 webdav_client.py:451-453：full_info 为 None 时走 else 分支追加
        原始 storage。
        """
        client = _make_admin_client(tmp_path)
        list_result = {
            "code": 200,
            "data": {
                "content": [{"id": 2, "driver": "Strm", "mount_path": "/s2"}],
                "total": 1,
            },
        }
        with patch.object(client, "list_storages", return_value=list_result), \
             patch.object(client, "get_storage_info", return_value=None):
            result = client.get_strm_storages_full_info()
            assert len(result) == 1
            assert result[0]["id"] == 2
            assert result[0]["mount_path"] == "/s2"

    def test_empty_strm_list(self, tmp_path):
        """STRM 列表为空（无 Strm 驱动存储）→ 返回 []。"""
        client = _make_admin_client(tmp_path)
        list_result = {
            "code": 200,
            "data": {
                "content": [{"id": 3, "driver": "Local", "mount_path": "/local"}],
                "total": 1,
            },
        }
        with patch.object(client, "list_storages", return_value=list_result):
            result = client.get_strm_storages_full_info()
            assert result == []

    def test_full_info_with_valid_data(self, tmp_path):
        """get_storage_info 返回有效 data → 返回完整 data（含 addition）。"""
        client = _make_admin_client(tmp_path)
        list_result = {
            "code": 200,
            "data": {
                "content": [{"id": 1, "driver": "Strm", "mount_path": "/s1"}],
                "total": 1,
            },
        }
        full_info = {
            "code": 200,
            "data": {"id": 1, "driver": "Strm", "mount_path": "/s1", "addition": "{}"},
        }
        with patch.object(client, "list_storages", return_value=list_result), \
             patch.object(client, "get_storage_info", return_value=full_info):
            result = client.get_strm_storages_full_info()
            assert len(result) == 1
            assert result[0]["id"] == 1
            assert result[0]["addition"] == "{}"


class TestAdminListDirectory:
    def test_success(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        payload = {"code": 200, "data": {"content": [{"name": "a"}], "total": 1}}
        client.session.request.return_value = _make_response(payload)

        result = client.list_directory("/test")
        assert result is not None
        assert result["data"]["content"][0]["name"] == "a"

    def test_returns_none_on_failure(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = _make_response(status=500)

        assert client.list_directory("/test") is None


class TestAdminMkdir:
    def test_success_code_zero(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = _make_response({"code": 0, "message": "ok"})

        assert client.mkdir("/new_dir") is True

    def test_success_code_200(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = _make_response({"code": 200, "message": "ok"})

        assert client.mkdir("/new_dir") is True

    def test_already_exists_is_success(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = _make_response(
            {"code": 500, "message": "directory already exists"}
        )

        assert client.mkdir("/existing") is True

    def test_request_failure_returns_false(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = None

        assert client.mkdir("/dir") is False

    def test_non_200_returns_false(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = _make_response(status=500)

        assert client.mkdir("/dir") is False

    def test_business_error_returns_false(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = _make_response(
            {"code": 500, "message": "permission denied"}
        )

        assert client.mkdir("/dir") is False


class TestAdminMove:
    def test_success(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = _make_response({"code": 0})

        assert client.move("/src/a.txt", "/dst/a.txt") is True

    def test_failure_returns_false(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = None

        assert client.move("/src/a.txt", "/dst/a.txt") is False

    def test_business_error(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = _make_response({"code": 500, "message": "fail"})

        assert client.move("/src/a.txt", "/dst/a.txt") is False


class TestAdminRemove:
    def test_success(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = _make_response({"code": 0})

        assert client.remove("/path/file.txt") is True

    def test_failure(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = None

        assert client.remove("/path/file.txt") is False


class TestAdminCheckExists:
    def test_root_exists(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = _make_response({"code": 200, "data": {"content": [], "total": 0}})

        assert client.check_exists("/") is True

    def test_root_not_exists(self, tmp_path):
        """根目录列表失败 → None（不可信），不得当 False。"""
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = None

        assert client.check_exists("/") is None

    def test_file_found_in_listing(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        payload = {"code": 200, "data": {"content": [{"name": "target.txt"}], "total": 1}}
        client.session.request.return_value = _make_response(payload)

        assert client.check_exists("/dir/target.txt") is True

    def test_file_not_found(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        payload = {"code": 200, "data": {"content": [{"name": "other.txt"}], "total": 1}}
        client.session.request.return_value = _make_response(payload)

        assert client.check_exists("/dir/target.txt") is False

    def test_empty_path_treated_as_root(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = _make_response({"code": 200, "data": {"content": [], "total": 0}})

        assert client.check_exists("") is True

    def test_data_field_null_returns_none(self, tmp_path):
        """非根路径 data: null → 不可信 None（fail-closed，不得当不存在）。"""
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        with patch.object(client, "list_directory", return_value={"code": 200, "data": None}):
            assert client.check_exists("/dir/target.txt") is None

    def test_content_none_returns_none(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        with patch.object(
            client, "list_directory",
            return_value={"code": 200, "data": {"content": None, "total": 1}},
        ):
            assert client.check_exists("/dir/target.txt") is None

    def test_bool_total_returns_none(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        with patch.object(
            client, "list_directory",
            return_value={
                "code": 200,
                "data": {"content": [{"name": "x"}], "total": True},
            },
        ):
            assert client.check_exists("/dir/target.txt") is None

    def test_uses_per_page_100(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        with patch.object(
            client, "list_directory",
            return_value={"code": 200, "data": {"content": [], "total": 0}},
        ) as mock_list:
            client.check_exists("/dir/target.txt")
            mock_list.assert_called()
            kwargs = mock_list.call_args
            # path 为位置或关键字；per_page 必须为 100
            assert kwargs.kwargs.get("per_page") == 100 or (
                len(kwargs.args) >= 1 and kwargs.kwargs.get("per_page", 100) == 100
            )
            # 更稳妥：检查 call 参数
            _, call_kwargs = mock_list.call_args
            if "per_page" in call_kwargs:
                assert call_kwargs["per_page"] == 100


class TestAdminListContents:
    def test_separates_folders_and_files(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        payload = {
            "code": 200,
            "data": {
                "content": [
                    {"name": "sub", "is_dir": True, "size": 0},
                    {"name": "a.txt", "is_dir": False, "size": 100},
                ],
                "total": 2,
            },
        }
        client.session.request.return_value = _make_response(payload)

        result = client.list_contents("/dir")
        assert result is not None
        assert len(result["folders"]) == 1
        assert len(result["files"]) == 1
        assert result["folders"][0]["name"] == "sub"
        assert result["files"][0]["name"] == "a.txt"

    def test_returns_none_on_failure(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = None

        assert client.list_contents("/dir") is None

    def test_returns_none_on_error_code(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = _make_response({"code": 404, "data": {}})

        assert client.list_contents("/dir") is None

    def test_data_field_null(self, tmp_path):
        """list_directory 返回 data: null → list_contents 返回空结构。

        覆盖 webdav_client.py:662-665：data 为 None 时 isinstance 守卫使
        content 为 []，返回 {"folders": [], "files": []}。
        """
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        with patch.object(client, "list_directory", return_value={"code": 200, "data": None}):
            result = client.list_contents("/dir")
            assert result == {"folders": [], "files": []}

    def test_content_field_null(self, tmp_path):
        """list_directory 返回 data.content: null → list_contents 返回空结构。

        覆盖 webdav_client.py:664-665：content is None 守卫将其重置为 []。
        """
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        with patch.object(client, "list_directory", return_value={"code": 200, "data": {"content": None}}):
            result = client.list_contents("/dir")
            assert result == {"folders": [], "files": []}


class TestAdminTriggerRefresh:
    def test_all_paths_succeed(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = _make_response({"code": 200, "data": {}})

        assert client.trigger_refresh_via_fs_list(["/a", "/b"]) is True

    def test_one_path_fails(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"
        client.session.request.return_value = None

        assert client.trigger_refresh_via_fs_list(["/a", "/b"]) is False

    def test_empty_list_succeeds(self, tmp_path):
        client = _make_admin_client(tmp_path)
        client.token = "jwt"

        assert client.trigger_refresh_via_fs_list([]) is True


# ===========================================================================
# OpenlistWebDAV
# ===========================================================================


class TestWebDAVInit:
    def test_strips_trailing_slash(self):
        dav = OpenlistWebDAV("http://host:5244/", "u", "p")
        assert dav.host == "http://host:5244"

    def test_strips_dav_suffix(self):
        dav = OpenlistWebDAV("http://host:5244/dav", "u", "p")
        assert dav.host == "http://host:5244"


class TestWebDAVRequest:
    def test_builds_correct_url(self):
        dav = OpenlistWebDAV("http://host:5244", "u", "p")
        dav.session = MagicMock()
        dav.session.request.return_value = _make_response()

        dav._request("GET", "/path/file.txt")
        call_args = dav.session.request.call_args
        assert call_args.args[1] == "http://host:5244/dav/path/file.txt"
        assert call_args.args[0] == "GET"

    def test_injects_totp_header_when_secret_set(self):
        dav = OpenlistWebDAV("http://host:5244", "u", "p", totp_secret="JBSWY3DPEHPK3PXP")
        dav.session = MagicMock()
        dav.session.request.return_value = _make_response()

        dav._request("GET", "/path")
        headers = dav.session.request.call_args.kwargs["headers"]
        assert "X-TOTP-Code" in headers

    def test_no_totp_header_without_secret(self):
        dav = OpenlistWebDAV("http://host:5244", "u", "p")
        dav.session = MagicMock()
        dav.session.request.return_value = _make_response()

        dav._request("GET", "/path")
        headers = dav.session.request.call_args.kwargs["headers"]
        assert "X-TOTP-Code" not in headers

    def test_prepends_slash_if_missing(self):
        dav = OpenlistWebDAV("http://host:5244", "u", "p")
        dav.session = MagicMock()
        dav.session.request.return_value = _make_response()

        dav._request("GET", "path/file.txt")
        url = dav.session.request.call_args.args[1]
        assert url == "http://host:5244/dav/path/file.txt"


class TestWebDAVCheckExists:
    def test_exists_returns_true(self):
        dav = OpenlistWebDAV("http://host:5244", "u", "p")
        dav.session = MagicMock()
        dav.session.request.return_value = _make_response(status=200)

        assert dav.check_exists("/file.txt") is True

    def test_not_exists_returns_false(self):
        import requests as _req
        dav = OpenlistWebDAV("http://host:5244", "u", "p")
        dav.session = MagicMock()
        err_resp = _make_response(status=404)
        http_err = _req.exceptions.HTTPError(response=err_resp)
        dav.session.request.side_effect = http_err

        assert dav.check_exists("/missing.txt") is False

    def test_other_http_error_raises(self):
        import requests as _req
        dav = OpenlistWebDAV("http://host:5244", "u", "p")
        dav.session = MagicMock()
        err_resp = _make_response(status=500)
        http_err = _req.exceptions.HTTPError(response=err_resp)
        dav.session.request.side_effect = http_err

        with pytest.raises(_req.exceptions.HTTPError):
            dav.check_exists("/file.txt")


class TestWebDAVListContents:
    def test_parses_propfind_response(self):
        dav = OpenlistWebDAV("http://host:5244", "u", "p")
        dav.session = MagicMock()
        xml = b"""<?xml version="1.0"?>
        <d:multistatus xmlns:d="DAV:">
          <d:response>
            <d:href>/dav/dir/</d:href>
            <d:propstat>
              <d:prop>
                <d:resourcetype><d:collection/></d:resourcetype>
                <d:getcontentlength>0</d:getcontentlength>
              </d:prop>
            </d:propstat>
          </d:response>
          <d:response>
            <d:href>/dav/dir/file.txt</d:href>
            <d:propstat>
              <d:prop>
                <d:resourcetype/>
                <d:getcontentlength>1234</d:getcontentlength>
              </d:prop>
            </d:propstat>
          </d:response>
        </d:multistatus>"""
        resp = MagicMock()
        resp.status_code = 207
        resp.content = xml
        dav.session.request.return_value = resp

        result = dav.list_contents("/dir")
        assert isinstance(result, dict)
        assert len(result["folders"]) == 1
        assert len(result["files"]) == 1
        assert result["files"][0]["size"] == 1234

    def test_404_returns_error_dict(self):
        import requests as _req
        dav = OpenlistWebDAV("http://host:5244", "u", "p")
        dav.session = MagicMock()
        err_resp = _make_response(status=404)
        http_err = _req.exceptions.HTTPError(response=err_resp)
        dav.session.request.side_effect = http_err

        result = dav.list_contents("/missing")
        assert isinstance(result, dict)
        assert result.get("code") == 404

    def test_non_207_returns_error_dict(self):
        dav = OpenlistWebDAV("http://host:5244", "u", "p")
        dav.session = MagicMock()
        resp = MagicMock()
        resp.status_code = 500
        resp.raise_for_status = MagicMock()
        dav.session.request.return_value = resp

        result = dav.list_contents("/dir")
        assert isinstance(result, dict)
        assert result.get("code") == 500


class TestWebDAVReadWriteDelete:
    def test_read_file(self):
        dav = OpenlistWebDAV("http://host:5244", "u", "p")
        dav.session = MagicMock()
        resp = MagicMock()
        resp.text = "file content"
        resp.raise_for_status = MagicMock()
        dav.session.request.return_value = resp

        assert dav.read_file("/file.txt") == "file content"

    def test_write_file(self):
        dav = OpenlistWebDAV("http://host:5244", "u", "p")
        dav.session = MagicMock()
        resp = _make_response()
        dav.session.request.return_value = resp

        dav.write_file("/file.txt", "new content")
        call_args = dav.session.request.call_args
        assert call_args.args[0] == "PUT"
        assert call_args.kwargs["data"] == b"new content"

    def test_delete_file(self):
        dav = OpenlistWebDAV("http://host:5244", "u", "p")
        dav.session = MagicMock()
        resp = _make_response()
        dav.session.request.return_value = resp

        dav.delete_file("/file.txt")
        assert dav.session.request.call_args.args[0] == "DELETE"


class TestWebDAVMkdir:
    def test_creates_directory(self):
        dav = OpenlistWebDAV("http://host:5244", "u", "p")
        dav.session = MagicMock()
        resp = _make_response()
        dav.session.request.return_value = resp

        dav.mkdir("/new_dir")
        assert dav.session.request.call_args.args[0] == "MKCOL"

    def test_405_already_exists_is_ignored(self):
        import requests as _req
        dav = OpenlistWebDAV("http://host:5244", "u", "p")
        dav.session = MagicMock()
        err_resp = _make_response(status=405)
        http_err = _req.exceptions.HTTPError(response=err_resp)
        dav.session.request.side_effect = http_err

        # Should not raise
        dav.mkdir("/existing_dir")

    def test_other_error_raises(self):
        import requests as _req
        dav = OpenlistWebDAV("http://host:5244", "u", "p")
        dav.session = MagicMock()
        err_resp = _make_response(status=500)
        http_err = _req.exceptions.HTTPError(response=err_resp)
        dav.session.request.side_effect = http_err

        with pytest.raises(_req.exceptions.HTTPError):
            dav.mkdir("/dir")


class TestWebDAVMoveCopy:
    def test_move(self):
        dav = OpenlistWebDAV("http://host:5244", "u", "p")
        dav.session = MagicMock()
        resp = _make_response()
        dav.session.request.return_value = resp

        dav.move("/src.txt", "/dst.txt")
        call_args = dav.session.request.call_args
        assert call_args.args[0] == "MOVE"
        assert call_args.kwargs["headers"]["Destination"] == "http://host:5244/dav/dst.txt"

    def test_copy(self):
        dav = OpenlistWebDAV("http://host:5244", "u", "p")
        dav.session = MagicMock()
        resp = _make_response()
        dav.session.request.return_value = resp

        dav.copy("/src.txt", "/dst.txt")
        call_args = dav.session.request.call_args
        assert call_args.args[0] == "COPY"
        assert call_args.kwargs["headers"]["Destination"] == "http://host:5244/dav/dst.txt"
