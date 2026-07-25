"""Utils package - common utility functions."""

from .bootstrap import (
    ensure_base_dir_first,
    load_local_module,
    BASE_DIR as BOOTSTRAP_BASE_DIR,
)
from .strm_utils import (
    parse_strm_content,
    make_strm_fingerprint,
    read_strm_webdav_path,
    canonicalize_webdav_path,
    escape_like,
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
from .encoding_utils import (
    copy_subtitle_utf8,
)

__all__ = [
    # Bootstrap
    "ensure_base_dir_first",
    "load_local_module",
    "BOOTSTRAP_BASE_DIR",
    # STRM
    "parse_strm_content",
    "make_strm_fingerprint",
    "read_strm_webdav_path",
    "canonicalize_webdav_path",
    "escape_like",
    # File operations
    "ensure_parent",
    "copy_file",
    "move_file",
    "safe_remove_file",
    "remove_empty_dirs",
    "local_relative",
    "local_join",
    "quarantine_file",
    "remove_file_strict",
    # WebDAV
    "webdav_parent",
    "webdav_root_name",
    "build_webdav_trash_path",
    "_canonicalize_webdav_path_for_cloud",
    # Encoding
    "copy_subtitle_utf8",
]
