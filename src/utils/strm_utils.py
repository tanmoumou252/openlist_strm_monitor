"""STRM fingerprint and path utilities."""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlparse

FINGERPRINT_VERSION = "strmfp:v1"


def parse_strm_content(content: str) -> str | None:
    """
    从 STRM 内容中解析真实 WebDAV 路径。

    支持：
    - http://host/d/xxx/yyy.mp4?sign=...
    - https://host/d/xxx/yyy.mp4?sign=...
    - /xxx/yyy.mp4

    注意：
    - 不使用 query 参数参与身份判断
    - 会 URL decode path
    - 如果路径以 /d/ 开头，会去掉 /d 前缀
    """
    if not isinstance(content, str):
        return None

    content = content.strip()
    if not content:
        return None

    if content.startswith("http://") or content.startswith("https://"):
        parsed = urlparse(content)

        # 只取 path，不取 query。
        path = unquote(parsed.path)

        # OpenList 直链常见格式 /d/挂载名/路径
        if path.startswith("/d/"):
            path = "/" + path[3:]

        return canonicalize_webdav_path(path, case_sensitive=True)

    if content.startswith("/"):
        return canonicalize_webdav_path(content, case_sensitive=True)

    return None


def canonicalize_webdav_path(webdav_path: str, *,
                             case_sensitive: bool = True) -> str:
    """
    规范化 WebDAV 路径，用于稳定比较和生成身份指纹。

    处理内容：
    - 类型校验
    - 去首尾空白
    - Unicode NFC 规范化
    - 反斜杠统一为正斜杠
    - 确保前导 /
    - 合并连续 /
    - 去除末尾 /，根路径除外
    - 可选 Unicode casefold 大小写折叠

    默认保持大小写敏感，因为 OpenList/WebDAV 服务端路径理论上可能大小写敏感。
    """
    if not isinstance(webdav_path, str):
        raise TypeError(
            f"webdav_path must be str, got {type(webdav_path).__name__}")

    canonical = webdav_path.strip()
    if not canonical:
        raise ValueError("webdav_path cannot be empty or whitespace-only")

    canonical = unicodedata.normalize("NFC", canonical)
    canonical = canonical.replace("\\", "/")

    if not canonical.startswith("/"):
        canonical = "/" + canonical

    canonical = re.sub(r"/+", "/", canonical)

    if canonical != "/" and canonical.endswith("/"):
        canonical = canonical.rstrip("/")

    if not case_sensitive:
        canonical = canonical.casefold()

    if not canonical:
        raise ValueError("webdav_path cannot be empty after canonicalization")

    return canonical


def make_strm_fingerprint(webdav_path: str, *,
                          case_sensitive: bool = True) -> str:
    """
    根据 WebDAV 路径生成稳定 STRM 身份指纹。

    不直接 hash 原始 STRM 内容，因为 STRM URL 可能带有 sign、token、
    openlist_ts 等临时参数。这里基于解析后的真实 webdav_path 生成指纹。

    指纹输入格式带版本前缀，方便未来升级算法：
    - strmfp:v1:/挂载名/目录/文件.mp4
    """
    canonical = canonicalize_webdav_path(
        webdav_path,
        case_sensitive=case_sensitive,
    )
    payload = f"{FINGERPRINT_VERSION}:{canonical}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def read_strm_webdav_path(file_path: str | Path) -> str | None:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return parse_strm_content(f.read())
    except (FileNotFoundError, OSError, PermissionError, UnicodeDecodeError):
        # 如果文件不存在、无法读取或包含非法字节，返回 None 而不是崩溃
        return None


def escape_like(value: str) -> str:
    """转义 SQL LIKE 通配符（% _ \\），配合 ``ESCAPE '\\'`` 子句使用。

    用于构建 LIKE 模式时，确保路径/媒体名中的下划线、百分号不被当作通配符。
    顺序很重要：必须先转义反斜杠本身，否则后面对 %/_ 加的反斜杠会被这一步再次转义。
    转义只作用于传入的 LIKE 模式值，不影响被匹配列的内容（如 Windows 路径中的反斜杠）。
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
