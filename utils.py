from __future__ import annotations

import hashlib
import os
import re
import shutil
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

        return _canonicalize_webdav_path(path, case_sensitive=True)

    if content.startswith("/"):
        return _canonicalize_webdav_path(content, case_sensitive=True)

    return None


def _canonicalize_webdav_path(webdav_path: str, *, case_sensitive: bool = True) -> str:
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
        raise TypeError(f"webdav_path must be str, got {type(webdav_path).__name__}")

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


def make_strm_fingerprint(webdav_path: str, *, case_sensitive: bool = True) -> str:
    """
    根据 WebDAV 路径生成稳定 STRM 身份指纹。

    不直接 hash 原始 STRM 内容，因为 STRM URL 可能带有 sign、token、
    openlist_ts 等临时参数。这里基于解析后的真实 webdav_path 生成指纹。

    指纹输入格式带版本前缀，方便未来升级算法：
    - strmfp:v1:/挂载名/目录/文件.mp4
    """
    canonical = _canonicalize_webdav_path(
        webdav_path,
        case_sensitive=case_sensitive,
    )
    payload = f"{FINGERPRINT_VERSION}:{canonical}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def read_strm_webdav_path(file_path: str | Path) -> str | None:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return parse_strm_content(f.read())
    except (FileNotFoundError, OSError, PermissionError):
        # 如果文件不存在或无法读取，返回 None 而不是崩溃
        return None


def ensure_parent(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def copy_file(src: str | Path, dst: str | Path) -> None:
    ensure_parent(dst)
    shutil.copy2(src, dst)


def move_file(src: str | Path, dst: str | Path) -> None:
    ensure_parent(dst)
    shutil.move(str(src), str(dst))


def safe_remove_file(path: str | Path) -> None:
    try:
        if Path(path).exists():
            Path(path).unlink()
    except OSError:
        pass


def remove_empty_dirs(root_folder: str | Path) -> None:
    root_folder = Path(root_folder)
    if not root_folder.exists():
        return

    for current_root, dirs, files in os.walk(root_folder, topdown=False):
        current = Path(current_root)
        if current == root_folder:
            continue
        try:
            if not any(current.iterdir()):
                current.rmdir()
        except OSError:
            pass


def local_relative(root: str | Path, target: str | Path) -> Path:
    return Path(target).resolve().relative_to(Path(root).resolve())


def local_join(root: str | Path, relative_path: Path) -> Path:
    return Path(root).resolve() / relative_path


def webdav_parent(path: str) -> str:
    path = _canonicalize_webdav_path(path, case_sensitive=True)
    parts = path.strip("/").split("/")
    if len(parts) <= 1:
        return "/"
    return "/" + "/".join(parts[:-1])


def webdav_root_name(path: str) -> str:
    path = _canonicalize_webdav_path(path, case_sensitive=True)
    parts = [p for p in path.strip("/").split("/") if p]
    return parts[0] if parts else ""


def build_webdav_trash_path(webdav_path: str, trash_dir_name: str) -> str:
    webdav_path = _canonicalize_webdav_path(webdav_path, case_sensitive=True)

    parts = [p for p in webdav_path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"非法 webdav_path: {webdav_path}")

    root = parts[0]
    filename = parts[-1]
    middle = parts[1:-1]

    trash_dir = f"/{root}/{trash_dir_name}"
    if middle:
        trash_dir += "/" + "/".join(middle)

    return trash_dir + "/" + filename


def remove_file_strict(path: str | Path) -> bool:
    """
    严格删除文件。
    返回 True 表示文件不存在或删除成功。
    返回 False 表示删除失败。
    """
    p = Path(path)
    try:
        if p.exists():
            p.unlink()
        return True
    except OSError:
        return False


def quarantine_file(path: str | Path, suffix: str = ".invalid") -> Path | None:
    """
    将异常文件隔离，避免媒体库继续扫描到 .strm。
    例如：
      xxx.strm -> xxx.strm.invalid
    如果目标已存在，则自动追加时间戳。
    """
    p = Path(path)
    if not p.exists():
        return None

    target = p.with_name(p.name + suffix)
    if target.exists():
        import time

        target = p.with_name(f"{p.name}{suffix}.{int(time.time())}")

    try:
        p.rename(target)
        return target
    except OSError:
        return None
