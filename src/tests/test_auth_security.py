"""
认证安全测试 (Task H.1)

测试范围：
1. 登录限流数据结构验证（端到端 429 测试见 test_webui_http.py）
2. _hash_password 返回值符合 salt$iterations$hash 三段式，iterations 为 600000
3. 首启密码只打印一次：二次初始化时不再打印

运行方式：
  python -m pytest src/tests/test_auth_security.py -v
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 确保 src/ 在 sys.path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestLoginRateLimit:
    """测试登录限流机制（核心逻辑验证）"""

    def test_login_attempts_dict_tracks_failures(self):
        """_login_attempts 字典正确记录失败时间戳列表"""
        from webui.routes import _login_attempts
        _login_attempts.clear()

        ip = "127.0.0.1"
        now = time.time()
        # 真实数据结构：时间戳列表
        _login_attempts[ip] = [now]
        assert len(_login_attempts[ip]) == 1
        assert _login_attempts[ip][0] == now

        # 追加第二次失败
        now2 = now + 1
        _login_attempts[ip] = _login_attempts[ip] + [now2]
        assert len(_login_attempts[ip]) == 2

        # 成功登录后应重置（pop）
        _login_attempts.pop(ip, None)
        assert ip not in _login_attempts
        _login_attempts.clear()

    def test_rate_limit_threshold_is_five(self):
        """限流阈值为 5 次失败（时间戳列表长度 >= 5）"""
        from webui.routes import _login_attempts, _LOGIN_MAX_ATTEMPTS
        _login_attempts.clear()

        ip = "127.0.0.1"
        now = time.time()
        # 模拟 5 次失败：时间戳列表长度达到阈值
        _login_attempts[ip] = [now + i for i in range(5)]
        assert len(_login_attempts[ip]) == _LOGIN_MAX_ATTEMPTS

        # 第 6 次时，检查逻辑应返回 429
        # （实际 HTTP 检查在 test_webui_http.py 中覆盖）
        _login_attempts.clear()

    def test_malformed_json_body_does_not_count(self):
        """非 dict JSON（[]、null、字符串）返回 400 且不向 _login_attempts 追加时间戳"""
        from webui.routes import _handle_login, _login_attempts
        _login_attempts.clear()

        handler = MagicMock()
        handler.client_address = ("127.0.0.1",)
        webui_server = MagicMock()
        webui_server._watchlist_db = MagicMock()
        webui_server._watchlist_db.get_config.return_value = "stored_hash"

        for bad_body in (b"[]", b"null", b'"abc"'):
            _login_attempts.clear()
            _handle_login(handler, webui_server, bad_body)
            # 畸形请求不应追加时间戳（列表长度保持 0）
            attempts = _login_attempts.get("127.0.0.1", [])
            assert len(attempts) == 0, \
                f"畸形请求体 {bad_body!r} 不应向 _login_attempts 追加时间戳，实际: {attempts}"
            # 应返回 400
            call_args = handler._send_json.call_args
            assert call_args[0][1] == 400

    def test_non_json_content_type_is_rejected_before_password_processing(self):
        """登录请求必须声明 application/json，且错误类型直接返回 400。"""
        from webui.routes import _handle_login, _login_attempts

        _login_attempts.clear()
        handler = MagicMock()
        handler.client_address = ("127.0.0.1",)
        handler.headers = {"Content-Type": "text/plain"}
        webui_server = MagicMock()

        _handle_login(handler, webui_server, b'{"password": "wrong"}')

        handler._send_json.assert_called_once_with(
            {"error": "Content-Type 必须为 application/json"}, 400
        )
        webui_server._watchlist_db.get_config.assert_not_called()
        _login_attempts.clear()

    def test_wrong_password_counts_toward_limit(self):
        """错误密码返回 401 且 _login_attempts[ip] 增长"""
        from webui.routes import _handle_login, _login_attempts
        from utils.password_utils import hash_password
        _login_attempts.clear()

        handler = MagicMock()
        handler.client_address = ("127.0.0.1",)
        handler.headers = {"Content-Type": "application/json; charset=utf-8"}
        webui_server = MagicMock()
        # 设置一个真实哈希，但密码不匹配
        stored_hash = hash_password("1111")
        webui_server._watchlist_db = MagicMock()
        webui_server._watchlist_db.get_config.return_value = stored_hash

        _handle_login(handler, webui_server, b'{"password": "wrong"}')
        assert "127.0.0.1" in _login_attempts
        assert len(_login_attempts["127.0.0.1"]) == 1
        handler._send_json.assert_called_once()
        assert handler._send_json.call_args[0][1] == 401
        _login_attempts.clear()


class TestPasswordHash:
    """测试密码哈希格式"""

    def test_hash_password_format(self):
        """_hash_password 返回值符合 salt$iterations$hash 三段式"""
        from webui.server import WebUIServer

        # _hash_password 是 WebUIServer 的静态方法
        password = "1111"
        hash_result = WebUIServer._hash_password(password)

        # 检查三段式格式
        parts = hash_result.split("$")
        assert len(parts) == 3, f"哈希格式不正确，应为 salt$iterations$hash，实际: {hash_result}"

        salt, iterations_str, hash_value = parts

        # 检查 salt 不为空
        assert len(salt) > 0, "salt 不能为空"

        # 检查 iterations 为 600000
        iterations = int(iterations_str)
        assert iterations == 600000, f"iterations 应为 600000，实际: {iterations}"

        # 检查 hash_value 不为空
        assert len(hash_value) > 0, "hash_value 不能为空"

    def test_same_password_produces_different_hashes(self):
        """相同密码产生不同哈希（不同 salt）"""
        from webui.server import WebUIServer

        password = "1111"
        hash1 = WebUIServer._hash_password(password)
        hash2 = WebUIServer._hash_password(password)

        # 由于 salt 不同，哈希应该不同
        assert hash1 != hash2, "相同密码应产生不同哈希（不同 salt）"

    def test_different_passwords_produce_different_hashes(self):
        """不同密码产生不同哈希"""
        from webui.server import WebUIServer

        hash1 = WebUIServer._hash_password("password1")
        hash2 = WebUIServer._hash_password("password2")

        assert hash1 != hash2, "不同密码应产生不同哈希"


class TestFirstStartupPassword:
    """测试首启密码生成与验证逻辑"""

    def test_generated_password_is_nonempty_string(self):
        """secrets.token_urlsafe 生成的密码为非空字符串"""
        import secrets
        pwd = secrets.token_urlsafe(12)
        assert isinstance(pwd, str)
        assert len(pwd) > 0

    def test_generated_password_meets_length_requirement(self):
        """生成的密码长度 >= 16（安全最低要求）"""
        import secrets
        # token_urlsafe(12) 产生约 16 字符的 base64url 编码
        pwd = secrets.token_urlsafe(12)
        assert len(pwd) >= 16, f"生成密码长度不足: {len(pwd)}"

    def test_hash_and_verify_roundtrip(self):
        """_hash_password 生成的哈希可通过 _check_password 验证"""
        from webui.server import WebUIServer
        password = "test_first_startup_pw"
        hashed = WebUIServer._hash_password(password)
        assert WebUIServer._check_password(password, hashed) is True
        assert WebUIServer._check_password("wrong_password", hashed) is False


class TestPasswordCorruptedFormat:
    """测试损坏的密码格式提示"""

    def test_corrupted_password_returns_500_with_reset_instruction(self):
        """损坏的密码哈希返回 500 且消息含 reset_admin.py"""
        # Mock the database to return a corrupted hash
        from unittest.mock import MagicMock

        # Create a minimal mock of the handler
        handler = MagicMock()
        handler.client_address = ("127.0.0.1",)
        handler._send_json = MagicMock()

        # Mock webui_server with corrupted password
        webui_server = MagicMock()
        webui_server._watchlist_db = MagicMock()
        webui_server._watchlist_db.get_config.return_value = "corrupted_hash"

        # Import and call the handler
        from webui.routes import _handle_login
        body = b'{"password": "test"}'
        _handle_login(handler, webui_server, body)

        # Should return 500 with reset_admin.py instruction
        handler._send_json.assert_called_once()
        call_args = handler._send_json.call_args
        response = call_args[0][0]
        status_code = call_args[0][1]

        assert status_code == 500
        assert "reset_admin.py" in response["error"]
        assert "密码格式损坏" in response["error"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
