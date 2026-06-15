"""Utils package - common utility functions."""

from .strm_utils import (
    parse_strm_content,
    make_strm_fingerprint,
    read_strm_webdav_path,
    _canonicalize_webdav_path,
)
from .file_utils import (
    ensure_parent,
    copy_file,
    move_file,
    safe_remove_file,
    remove_empty_dirs,
    local_relative,
    local_join,
    quarantine_file,
    remove_file_strict,
)
from .webdav_utils import (
    webdav_parent,
    webdav_root_name,
    build_webdav_trash_path,
    _canonicalize_webdav_path_for_cloud,
)

__all__ = [
    "parse_strm_content",
    "make_strm_fingerprint",
    "read_strm_webdav_path",
    "_canonicalize_webdav_path",
    "ensure_parent",
    "copy_file",
    "move_file",
    "safe_remove_file",
    "remove_empty_dirs",
    "local_relative",
    "local_join",
    "quarantine_file",
    "remove_file_strict",
    "webdav_parent",
    "webdav_root_name",
    "build_webdav_trash_path",
    "_canonicalize_webdav_path_for_cloud",
]
