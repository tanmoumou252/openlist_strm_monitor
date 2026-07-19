"""测试 OpenList 登录错误解析"""
import pytest
from openlist_login_shared import parse_login_error


class TestParseLoginError:
    """测试 parse_login_error 函数"""

    def test_empty_message(self):
        """空消息返回 unknown"""
        assert parse_login_error("") == "unknown"
        assert parse_login_error(None) == "unknown"

    def test_wrong_password(self):
        """密码错误消息"""
        assert parse_login_error("username or password is wrong") == "wrong_password"
        assert parse_login_error("Password incorrect") == "wrong_password"

    def test_wrong_2fa(self):
        """2FA/OTP 错误消息"""
        assert parse_login_error("otp code is wrong") == "wrong_2fa"
        assert parse_login_error("2FA verification failed") == "wrong_2fa"
        assert parse_login_error("Two-factor authentication error") == "wrong_2fa"

    def test_account_not_found(self):
        """账号不存在消息"""
        assert parse_login_error("user not found") == "account_not_found"
        assert parse_login_error("Account not found") == "account_not_found"

    def test_unknown_error(self):
        """其他未知错误"""
        assert parse_login_error("server internal error") == "unknown"
        assert parse_login_error("rate limit exceeded") == "unknown"

    def test_2fa_priority_over_password(self):
        """当消息同时含 otp 和 password 时，2FA 优先（源码注释明确设计如此）"""
        assert parse_login_error("otp code is wrong for password") == "wrong_2fa"

    def test_case_insensitive(self):
        """匹配不区分大小写"""
        assert parse_login_error("OTP CODE IS WRONG") == "wrong_2fa"
        assert parse_login_error("USERNAME OR PASSWORD IS WRONG") == "wrong_password"
        assert parse_login_error("USER NOT FOUND") == "account_not_found"
