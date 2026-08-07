"""统一密码哈希/验证工具。

M-1+M-2: 消除 server.py / routes.py / reset_admin.py 中三处重复的
PBKDF2-HMAC-SHA256 密码哈希逻辑。

约定：
- 哈希格式：salt$iterations$hash（hex 编码）
- 盐长度：16 字节 URL-safe hex
- 迭代次数：600,000（OWASP 2023 推荐）
- 验证使用 hmac.compare_digest 防止时序攻击
"""

from __future__ import annotations

import hashlib
import hmac
import secrets


# 常量：OWASP 2023 PBKDF2-HMAC-SHA256 推荐
_DEFAULT_ITERATIONS = 600_000
_SALT_BYTE_LEN = 16  # 128 位
# 硬上限：防止植入极端迭代次数导致 DoS
_MAX_ITERATIONS = 10_000_000


def hash_password(password: str, iterations: int = _DEFAULT_ITERATIONS) -> str:
    """将密码哈希为 salt$iterations$hex 格式。
    
    Args:
        password: 明文密码
        iterations: PBKDF2 迭代次数（默认 600,000）
    
    Returns:
        格式化字符串：salt$iterations$hash_hex
    """
    salt = secrets.token_hex(_SALT_BYTE_LEN)
    h = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    )
    return f"{salt}${iterations}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """验证密码是否匹配存储的哈希。
    
    Args:
        password: 待验证的明文密码
        stored: 存储的 salt$iterations$hash 格式字符串
    
    Returns:
        True 如果密码匹配，否则 False
    """
    try:
        parts = stored.split("$", 2)
        if len(parts) != 3:
            return False
        salt, iterations_str, stored_hash = parts
        iterations = int(iterations_str)
        if iterations > _MAX_ITERATIONS:
            return False
        h = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            iterations,
        )
        # M-5: 使用 hmac.compare_digest 防止时序攻击
        return hmac.compare_digest(h.hex(), stored_hash)
    except (ValueError, AttributeError):
        return False
