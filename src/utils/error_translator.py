"""
错误消息翻译工具 — 将技术性错误转换为易懂的用户描述

将 requests/urllib3/socket 等底层异常转换为普通用户能理解的中文描述。
"""

from __future__ import annotations

import re
from typing import Any


def translate_network_error(error: Exception, context: str = "") -> str:
    """将网络相关异常转换为易懂的中文描述。

    Args:
        error: 捕获的异常对象
        context: 错误发生的上下文（如"登录"、"获取存储列表"等）

    Returns:
        易懂的错误描述字符串

    Examples:
        >>> translate_network_error(ConnectionRefusedError(10061, "连接被拒绝"))
        "无法连接到服务器 — 请检查 OpenList 是否已启动，地址和端口是否正确"
    """
    err_name = type(error).__name__
    err_str = str(error)
    err_args = getattr(error, 'args', ())

    # 提取 errno（Windows: WSAError, Unix: errno）
    errno = None
    if len(err_args) >= 1 and isinstance(err_args[0], int):
        errno = err_args[0]

    # ConnectionRefusedError / ConnectionResetError / ConnectionAbortedError
    if err_name in ("ConnectionRefusedError", "ConnectionResetError", "ConnectionAbortedError"):
        if errno == 10061 or "refused" in err_str.lower():
            return f"{context}失败 — 无法连接到服务器，请检查：\n" \
                   f"  1. OpenList 是否已启动\n" \
                   f"  2. 地址和端口是否正确\n" \
                   f"  3. 防火墙是否阻止了连接"
        elif errno == 10054 or "reset" in err_str.lower():
            return f"{context}失败 — 连接被服务器重置，可能是 OpenList 重启或网络中断"
        else:
            return f"{context}失败 — 连接异常中断（{err_name}）"

    # OSError / socket.error（在 TimeoutError 之前检查，因为 Windows 的 WSAETIMEDOUT
    # 会同时匹配 errno 10060 和 "timed out" 字符串，需要优先按 errno 精确匹配）
    if err_name in ("OSError", "error") or (hasattr(error, 'errno') and isinstance(getattr(error, 'errno', None), int)):
        if errno == 10049 or "Cannot assign requested address" in err_str:
            return f"{context}失败 — 本地地址绑定失败，请检查网络配置"
        elif errno == 10051 or "Network is unreachable" in err_str:
            return f"{context}失败 — 网络不可达，请检查网络连接"
        elif errno == 10053 or "Software caused connection abort" in err_str:
            return f"{context}失败 — 连接被本地软件中断"
        elif errno == 10057 or "Socket is not connected" in err_str:
            return f"{context}失败 — 套接字未连接"
        elif errno == 10060 or "Connection attempt failed" in err_str:
            return f"{context}失败 — 连接尝试失败，服务器无响应"
        elif errno == 10065 or "No route to host" in err_str:
            return f"{context}失败 — 无法路由到目标主机"
        elif errno is not None:
            return f"{context}失败 — 系统网络错误（错误码 {errno}）"
        # 无 errno 的 OSError 继续往下走

    # TimeoutError / socket.timeout
    if err_name in ("TimeoutError", "timeout") or "timed out" in err_str.lower():
        return f"{context}失败 — 连接超时，服务器响应太慢或网络不稳定"
    if err_name == "ConnectionError":
        if "Failed to establish a new connection" in err_str:
            if "refused" in err_str:
                return f"{context}失败 — 无法连接到服务器，请检查 OpenList 是否已启动"
            elif "Name or service not known" in err_str or "getaddrinfo failed" in err_str:
                return f"{context}失败 — 无法解析服务器地址，请检查域名或 IP 是否正确"
            else:
                return f"{context}失败 — 无法建立网络连接"
        elif "RemoteDisconnected" in err_str:
            return f"{context}失败 — 服务器意外关闭了连接"
        else:
            return f"{context}失败 — 网络连接异常"

    if err_name == "ConnectTimeout":
        return f"{context}失败 — 连接超时，服务器无响应"

    if err_name == "ReadTimeout":
        return f"{context}失败 — 读取超时，服务器处理太慢"

    if err_name == "Timeout":
        return f"{context}失败 — 请求超时"

    if err_name == "TooManyRedirects":
        return f"{context}失败 — 重定向次数过多，请检查 URL 是否正确"

    if err_name == "URLRequired":
        return f"{context}失败 — 未提供有效的 URL"

    if err_name == "MissingSchema":
        return f"{context}失败 — URL 格式错误，应以 http:// 或 https:// 开头"

    if err_name == "InvalidSchema":
        return f"{context}失败 — URL 协议不支持，仅支持 http:// 或 https://"

    if err_name == "InvalidURL":
        return f"{context}失败 — URL 格式无效"

    # HTTPError（requests 的 HTTP 错误）
    if err_name == "HTTPError":
        # 尝试提取状态码（匹配 "401 Client Error" 或 "401 Error" 格式）
        match = re.search(r'(\d{3})\s+(?:Client|Server)\s+Error', err_str)
        if not match:
            match = re.search(r'(\d{3})\s+Error', err_str)
        if match:
            status_code = match.group(1)
            status_messages = {
                '400': '请求格式错误',
                '401': '认证失败，用户名或密码错误',
                '403': '权限不足，被服务器拒绝',
                '404': '请求的资源不存在',
                '405': '请求方法不被允许',
                '408': '请求超时',
                '429': '请求太频繁，被服务器限流',
                '500': '服务器内部错误',
                '502': '网关错误',
                '503': '服务暂时不可用',
                '504': '网关超时',
            }
            msg = status_messages.get(status_code, f'HTTP {status_code} 错误')
            return f"{context}失败 — {msg}"
        return f"{context}失败 — HTTP 错误"

    # SSLError
    if err_name == "SSLError":
        if "certificate verify failed" in err_str:
            return f"{context}失败 — SSL 证书验证失败，可能是证书过期或不受信任"
        elif "wrong version number" in err_str:
            return f"{context}失败 — SSL/TLS 版本不匹配"
        else:
            return f"{context}失败 — SSL 加密连接失败"

    # 未知错误，返回原始信息但加上上下文
    if context:
        return f"{context}失败 — {err_str}"
    return err_str


def format_error_for_log(error: Exception, context: str = "", include_technical: bool = False) -> str:
    """格式化错误信息用于日志记录。

    Args:
        error: 捕获的异常对象
        context: 错误发生的上下文
        include_technical: 是否在末尾附加技术性错误详情（供开发者调试）

    Returns:
        格式化的错误字符串
    """
    user_msg = translate_network_error(error, context)

    if include_technical:
        err_name = type(error).__name__
        technical = f"[技术详情: {err_name}: {error}]"
        return f"{user_msg} {technical}"

    return user_msg
