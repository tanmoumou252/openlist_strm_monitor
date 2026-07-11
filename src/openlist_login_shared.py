"""OpenList 登录逻辑共享模块

提供两个 OpenListAdminClient 类共享的登录错误解析逻辑。
"""


def parse_login_error(message: str) -> str:
    """解析 OpenList 登录错误消息，返回错误类型。

    常见错误消息:
    - "username or password is wrong" → wrong_password
    - "otp code is wrong" → wrong_2fa
    - "user not found" → account_not_found
    """
    if not message:
        return "unknown"

    msg_lower = message.lower()

    # 2FA 错误优先检查（因为可能同时包含 password 关键词）
    if "otp" in msg_lower or "2fa" in msg_lower or "two-factor" in msg_lower:
        return "wrong_2fa"

    # 密码错误
    if "password" in msg_lower or "username or password" in msg_lower:
        return "wrong_password"

    # 账号不存在
    if "user not found" in msg_lower or "account not found" in msg_lower:
        return "account_not_found"

    # 其他错误
    return "unknown"
