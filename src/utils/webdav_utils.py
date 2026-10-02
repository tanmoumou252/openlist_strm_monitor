# autopep8: off
# isort: off

"""WebDAV path utilities."""

from __future__ import annotations

import posixpath
import urllib.parse
from utils.strm_utils import canonicalize_webdav_path

# autopep8: on
# isort: on

def webdav_parent(path: str) -> str:
    path = canonicalize_webdav_path(path, case_sensitive=True)
    parts = path.strip("/").split("/")
    if len(parts) <= 1:
        return "/"
    return "/" + "/".join(parts[:-1])

def webdav_root_name(path: str) -> str:
    path = canonicalize_webdav_path(path, case_sensitive=True)
    parts = [p for p in path.strip("/").split("/") if p]
    return parts[0] if parts else ""

def build_webdav_trash_path(webdav_path: str, trash_dir_name: str) -> str:
    webdav_path = canonicalize_webdav_path(webdav_path, case_sensitive=True)

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

def _canonicalize_webdav_path_for_cloud(webdav_path: str) -> str:
    """规范化 WebDAV 路径用于云端操作
    # 两套路径语义是设计分工，勿合并

    后者用于指纹/身份（NFC，不解码不 normpath）；本函数用于云 API 路径比较
    （unquote+normpath，无 NFC）。两者从不交叉比较，仅 _b_file_score 去重打分用。勿合并。
    """
    if not webdav_path:
        return ""
    path = webdav_path.replace("\\", "/")
    path = urllib.parse.unquote(path)
    return posixpath.normpath(path)
