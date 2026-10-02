"""凭据对称加密管理（Fernet / AES-128-CBC + HMAC-SHA256）。

主密钥存储于 ``src/.secret_key``（与本项目其它本地敏感文件如
``.admin_token.json`` 同目录，已被 .gitignore 排除）。

约定：
- ``encrypt("")`` 返回 ``""``，避免在 DB 中堆积无意义的 ``ENC:`` 空密文。
- ``decrypt(value)`` 在任何失败场景（主密钥不匹配 / 文件损坏 / 非 ``ENC:`` 前缀）
  下返回 ``""``，不抛异常。这样单条凭据损坏不会导致整个配置加载崩溃，
  系统降级为"凭据未配置"，用户可通过 WebUI 重新输入。
- ``is_encrypted(value)`` 仅判断前缀，不做解密验证。

依赖降级：
- 若 ``cryptography`` 未安装，加密功能降级为明文存储（带警告日志），
  服务仍可正常启动。用户可通过 ``pip install cryptography>=42.0.0`` 启用加密。
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
import stat

log = logging.getLogger(__name__)

# 密文前缀，用于区分已加密值与历史明文值
_ENC_PREFIX = "ENC:"

# 主密钥文件路径：与本模块同目录（src/.secret_key）
_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secret_key")

# 主密钥字节长度（Fernet 要求 32 字节 URL-safe base64 编码密钥）
_KEY_BYTE_LEN = 32

# 模块级缓存，避免每次调用都读磁盘
_cached_fernet = None

# cryptography 是否可用（首次调用时检测）
_cryptography_available: bool | None = None


def _check_cryptography_available() -> bool:
    """检测 cryptography 是否已安装，结果缓存。"""
    global _cryptography_available
    if _cryptography_available is not None:
        return _cryptography_available
    try:
        from cryptography.fernet import Fernet  # noqa: F401
        _cryptography_available = True
    except ImportError:
        _cryptography_available = False
        log.warning(
            "[SecretManager] cryptography 未安装，凭据将以明文存储。"
            "如需启用加密，请执行 `pip install cryptography>=42.0.0`"
        )
    return _cryptography_available


def _load_or_create_master_key() -> bytes:
    """加载或创建主密钥。

    首次调用时生成 32 字节随机密钥并以 URL-safe base64 写入 ``.secret_key``；
    后续调用直接读取。Unix 下尝试 chmod 600；Windows 尽力而为。
    """
    if os.path.exists(_KEY_FILE):
        with open(_KEY_FILE, "rb") as f:
            key = f.read().strip()
        if not key:
            raise RuntimeError(f"主密钥文件存在但为空: {_KEY_FILE}")
        return key

    # 首次创建：生成 32 字节随机数并 base64 编码（Fernet 要求）
    raw = secrets.token_bytes(_KEY_BYTE_LEN)
    key = base64.urlsafe_b64encode(raw)

    # 写入文件
    fd = os.open(_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)

    # 尽力收紧权限（Unix）
    try:
        os.chmod(_KEY_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except (OSError, NotImplementedError):
        # Windows 或只读文件系统，忽略
        pass

    log.info("[SecretManager] 已生成新的主密钥文件: %s", _KEY_FILE)
    return key


def _get_fernet():
    """获取 Fernet 实例（带模块级缓存）。若 cryptography 不可用返回 None。"""
    global _cached_fernet
    if _cached_fernet is not None:
        return _cached_fernet
    if not _check_cryptography_available():
        return None
    from cryptography.fernet import Fernet

    key = _load_or_create_master_key()
    _cached_fernet = Fernet(key)
    return _cached_fernet


def is_encrypted(value: str) -> bool:
    """判断值是否已被本模块加密（以 ``ENC:`` 开头）。"""
    return isinstance(value, str) and value.startswith(_ENC_PREFIX)


def encrypt(plaintext: str) -> str:
    """加密明文字符串。

    - 空串直接返回 ``""``，不写入 ``ENC:`` 前缀。
    - 非空串返回 ``"ENC:" + base64(Fernet 密文)``。
    - 若 cryptography 不可用，返回原明文（降级为明文存储）。
    """
    if not isinstance(plaintext, str):
        raise TypeError(f"encrypt 仅接受 str，收到 {type(plaintext).__name__}")
    if plaintext == "":
        return ""
    fernet = _get_fernet()
    if fernet is None:
        # cryptography 不可用，降级为明文
        return plaintext
    ciphertext = fernet.encrypt(plaintext.encode("utf-8"))
    # Fernet 输出已是 URL-safe base64 bytes，直接拼接前缀
    return _ENC_PREFIX + ciphertext.decode("ascii")


def decrypt(ciphertext: str) -> str:
    """解密密文字符串。

    - 非 ``ENC:`` 前缀 → 视为历史明文，原样返回（兼容迁移期）。
    - ``ENC:`` 前缀但解密失败（主密钥不匹配 / 数据损坏）→ 返回 ``""``，
      不抛异常，使系统降级为"凭据未配置"。
    - 空串返回 ``""``。
    - 若 cryptography 不可用且值是 ``ENC:`` 前缀 → 返回 ``""``（无法解密）。
    """
    if not isinstance(ciphertext, str):
        return ""
    if ciphertext == "":
        return ""
    if not ciphertext.startswith(_ENC_PREFIX):
        # 历史明文，原样返回（迁移期兼容）
        return ciphertext
    fernet = _get_fernet()
    if fernet is None:
        # cryptography 不可用，无法解密 ENC: 前缀的值
        log.warning(
            "[SecretManager] cryptography 未安装，无法解密 ENC: 前缀的凭据，"
            "该凭据将视为未配置。"
        )
        return ""
    try:
        payload = ciphertext[len(_ENC_PREFIX):]
        plaintext = fernet.decrypt(payload.encode("ascii"))
        return plaintext.decode("utf-8")
    except Exception as e:
        # 主密钥不匹配 / 密文损坏 / cryptography 异常 → 降级为空串
        log.warning(
            "[SecretManager] 解密失败（凭据将视为未配置）: %s", e
        )
        return ""


def reset_master_key_for_testing() -> None:
    """测试用：清空模块级 Fernet 缓存，强制下次调用重新加载。

    生产代码不应调用此函数。
    """
    global _cached_fernet
    _cached_fernet = None
