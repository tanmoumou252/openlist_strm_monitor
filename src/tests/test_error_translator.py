"""
测试错误消息翻译工具
"""

import pytest
import requests
from utils.error_translator import translate_network_error, format_error_for_log


class TestTranslateNetworkError:
    """测试 translate_network_error 函数"""

    def test_connection_refused(self):
        """测试连接被拒绝错误"""
        error = ConnectionRefusedError(10061, "连接被拒绝")
        result = translate_network_error(error, "登录")
        assert "无法连接到服务器" in result
        assert "OpenList 是否已启动" in result
        assert "地址和端口是否正确" in result

    def test_connection_reset(self):
        """测试连接被重置错误"""
        error = ConnectionResetError(10054, "连接被重置")
        result = translate_network_error(error, "获取存储列表")
        assert "连接被服务器重置" in result

    def test_timeout_error(self):
        """测试超时错误"""
        error = TimeoutError("连接超时")
        result = translate_network_error(error, "登录")
        assert "连接超时" in result
        assert "服务器响应太慢" in result

    def test_requests_connection_error_refused(self):
        """测试 requests.ConnectionError（连接被拒绝）"""
        error = requests.exceptions.ConnectionError(
            "Failed to establish a new connection: Connection refused"
        )
        result = translate_network_error(error, "登录")
        assert "无法连接到服务器" in result
        assert "OpenList 是否已启动" in result

    def test_requests_connection_error_dns(self):
        """测试 requests.ConnectionError（DNS 解析失败）"""
        error = requests.exceptions.ConnectionError(
            "Failed to establish a new connection: Name or service not known"
        )
        result = translate_network_error(error, "登录")
        assert "无法解析服务器地址" in result

    def test_requests_connect_timeout(self):
        """测试 requests.ConnectTimeout"""
        error = requests.exceptions.ConnectTimeout("连接超时")
        result = translate_network_error(error, "登录")
        assert "连接超时" in result

    def test_requests_read_timeout(self):
        """测试 requests.ReadTimeout"""
        error = requests.exceptions.ReadTimeout("读取超时")
        result = translate_network_error(error, "获取文件列表")
        assert "读取超时" in result

    def test_requests_missing_schema(self):
        """测试 requests.MissingSchema"""
        error = requests.exceptions.MissingSchema("Invalid URL: no scheme")
        result = translate_network_error(error, "登录")
        assert "URL 格式错误" in result
        assert "http://" in result

    def test_requests_invalid_url(self):
        """测试 requests.InvalidURL"""
        error = requests.exceptions.InvalidURL("Invalid URL format")
        result = translate_network_error(error, "登录")
        assert "URL 格式无效" in result

    def test_http_error_401(self):
        """测试 HTTPError 401"""
        error = requests.exceptions.HTTPError("401 Client Error: Unauthorized")
        result = translate_network_error(error, "登录")
        assert "认证失败" in result
        assert "用户名或密码错误" in result

    def test_http_error_404(self):
        """测试 HTTPError 404"""
        error = requests.exceptions.HTTPError("404 Client Error: Not Found")
        result = translate_network_error(error, "获取文件")
        assert "资源不存在" in result

    def test_http_error_500(self):
        """测试 HTTPError 500"""
        error = requests.exceptions.HTTPError("500 Server Error: Internal Server Error")
        result = translate_network_error(error, "登录")
        assert "服务器内部错误" in result

    def test_ssl_error_certificate(self):
        """测试 SSLError（证书验证失败）"""
        error = requests.exceptions.SSLError("certificate verify failed")
        result = translate_network_error(error, "登录")
        assert "SSL 证书验证失败" in result

    def test_os_error_network_unreachable(self):
        """测试 OSError（网络不可达）"""
        error = OSError(10051, "Network is unreachable")
        result = translate_network_error(error, "登录")
        assert "网络不可达" in result

    def test_os_error_connection_timeout(self):
        """测试 OSError（连接超时）"""
        error = OSError(10060, "Connection attempt failed")
        result = translate_network_error(error, "登录")
        assert "连接尝试失败" in result

    def test_unknown_error_with_context(self):
        """测试未知错误（带上下文）"""
        error = ValueError("未知错误")
        result = translate_network_error(error, "登录")
        assert "登录失败" in result
        assert "未知错误" in result

    def test_unknown_error_without_context(self):
        """测试未知错误（无上下文）"""
        error = ValueError("未知错误")
        result = translate_network_error(error)
        assert "未知错误" in result


class TestFormatErrorForLog:
    """测试 format_error_for_log 函数"""

    def test_without_technical(self):
        """测试不包含技术详情"""
        error = ConnectionRefusedError(10061, "连接被拒绝")
        result = format_error_for_log(error, "登录", include_technical=False)
        assert "无法连接到服务器" in result
        assert "ConnectionRefusedError" not in result

    def test_with_technical(self):
        """测试包含技术详情"""
        error = ConnectionRefusedError(10061, "连接被拒绝")
        result = format_error_for_log(error, "登录", include_technical=True)
        assert "无法连接到服务器" in result
        assert "ConnectionRefusedError" in result
        assert "技术详情" in result


class TestEdgeCases:
    """测试边界情况"""

    def test_empty_context(self):
        """测试空上下文"""
        error = TimeoutError("超时")
        result = translate_network_error(error, "")
        assert "连接超时" in result

    def test_empty_args(self):
        """测试 args 为空的异常"""
        error = Exception()
        result = translate_network_error(error, "测试")
        assert "测试失败" in result

    def test_complex_error_message(self):
        """测试复杂错误消息"""
        error = requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='example.com', port=443): "
            "Max retries exceeded with url: /api/test "
            "(Caused by NewConnectionError('Failed to establish a new connection: "
            "Connection refused'))"
        )
        result = translate_network_error(error, "测试")
        assert "无法连接到服务器" in result
