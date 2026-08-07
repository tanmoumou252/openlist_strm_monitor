"""
Unit tests for utils/ submodules:
  - utils/strm_utils.py  — parse_strm_content, canonicalize_webdav_path,
                           make_strm_fingerprint, read_strm_webdav_path
  - utils/file_utils.py  — ensure_parent, copy_file, move_file,
                           safe_remove_file, remove_empty_dirs,
                           local_relative, local_join, quarantine_file
  - utils/webdav_utils.py — webdav_parent, webdav_root_name,
                            build_webdav_trash_path,
                            _canonicalize_webdav_path_for_cloud
"""
from __future__ import annotations

import errno
import hashlib
import os
import shutil
import sys
import unicodedata
from pathlib import Path
from unittest.mock import patch

import pytest

# Add src/ to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.strm_utils import (
    canonicalize_webdav_path,
    make_strm_fingerprint,
    parse_strm_content,
    read_strm_webdav_path,
    FINGERPRINT_VERSION,
)
from utils.file_utils import (
    copy_file,
    ensure_parent,
    local_join,
    local_relative,
    move_file,
    quarantine_file,
    remove_empty_dirs,
    safe_remove_file,
)
from utils.webdav_utils import (
    _canonicalize_webdav_path_for_cloud,
    build_webdav_trash_path,
    webdav_parent,
    webdav_root_name,
)


# ===========================================================================
# TestParseStrmContent
# ===========================================================================


class TestParseStrmContent:
    def test_parse_http_url(self):
        result = parse_strm_content("http://host/video/movie.mp4")
        assert result == "/video/movie.mp4"

    def test_parse_https_url(self):
        result = parse_strm_content("https://host/video/movie.mp4")
        assert result == "/video/movie.mp4"

    def test_parse_url_with_d_prefix(self):
        # /d/ prefix should be stripped
        result = parse_strm_content("https://host/d/mymount/folder/file.mp4")
        assert result == "/mymount/folder/file.mp4"

    def test_parse_url_with_query_params(self):
        # query params (sign, token) must be dropped
        result = parse_strm_content(
            "https://host/d/mount/file.mp4?sign=abc123&t=999"
        )
        assert result == "/mount/file.mp4"

    def test_parse_plain_path(self):
        result = parse_strm_content("/some/webdav/path.mp4")
        assert result == "/some/webdav/path.mp4"

    def test_parse_empty_returns_none(self):
        assert parse_strm_content("") is None

    def test_parse_whitespace_only_returns_none(self):
        assert parse_strm_content("   ") is None

    def test_parse_non_string_returns_none(self):
        assert parse_strm_content(None) is None  # type: ignore[arg-type]
        assert parse_strm_content(123) is None  # type: ignore[arg-type]

    def test_parse_invalid_format_returns_none(self):
        # No leading slash and not a URL — should return None
        assert parse_strm_content("just-a-filename.mp4") is None

    def test_parse_url_encoded_path(self):
        result = parse_strm_content("https://host/d/mount/%E5%8A%A8%E6%BC%AB/ep01.mp4")
        assert "动漫" in result

    def test_strips_whitespace_before_parse(self):
        result = parse_strm_content("  https://host/d/mount/file.mp4  ")
        assert result == "/mount/file.mp4"

    def test_parse_http_url_with_empty_path_returns_none(self):
        # M7: http://host?sign=xxx 时 parsed.path 为空，不应抛 ValueError
        assert parse_strm_content("http://host?sign=xxx") is None
        assert parse_strm_content("https://host?token=abc") is None


# ===========================================================================
# TestCanonicalizeWebdavPath
# ===========================================================================


class TestCanonicalizeWebdavPath:
    def test_normalize_backslash(self):
        assert canonicalize_webdav_path("/foo\\bar") == "/foo/bar"

    def test_add_leading_slash(self):
        assert canonicalize_webdav_path("foo/bar") == "/foo/bar"

    def test_merge_consecutive_slashes(self):
        assert canonicalize_webdav_path("//foo//bar//") == "/foo/bar"

    def test_strip_trailing_slash(self):
        assert canonicalize_webdav_path("/foo/bar/") == "/foo/bar"

    def test_root_slash_preserved(self):
        assert canonicalize_webdav_path("/") == "/"

    def test_unicode_nfc_normalize(self):
        # NFD form of 'é' (e + combining accent)
        nfd = unicodedata.normalize("NFD", "é")
        result = canonicalize_webdav_path("/" + nfd)
        assert result == "/\u00e9"  # NFC é

    def test_casefold_disabled_by_default(self):
        result = canonicalize_webdav_path("/UPPER/Case")
        assert result == "/UPPER/Case"

    def test_casefold_enabled(self):
        result = canonicalize_webdav_path("/UPPER/Case", case_sensitive=False)
        assert result == "/upper/case"

    def test_empty_raises_value_error(self):
        with pytest.raises(ValueError):
            canonicalize_webdav_path("")

    def test_whitespace_only_raises_value_error(self):
        with pytest.raises(ValueError):
            canonicalize_webdav_path("   ")

    def test_non_string_raises_type_error(self):
        with pytest.raises(TypeError):
            canonicalize_webdav_path(123)  # type: ignore[arg-type]

    def test_deep_path_preserved(self):
        result = canonicalize_webdav_path("/a/b/c/d/e.mp4")
        assert result == "/a/b/c/d/e.mp4"


# ===========================================================================
# TestMakeStrmFingerprint
# ===========================================================================


class TestMakeStrmFingerprint:
    def test_fingerprint_stable(self):
        fp1 = make_strm_fingerprint("/mount/folder/file.mp4")
        fp2 = make_strm_fingerprint("/mount/folder/file.mp4")
        assert fp1 == fp2

    def test_fingerprint_different_paths(self):
        fp1 = make_strm_fingerprint("/mount/a.mp4")
        fp2 = make_strm_fingerprint("/mount/b.mp4")
        assert fp1 != fp2

    def test_fingerprint_is_sha256_hex(self):
        fp = make_strm_fingerprint("/test/file.mp4")
        assert len(fp) == 64
        int(fp, 16)  # must be valid hex

    def test_fingerprint_case_sensitive_default(self):
        fp_lower = make_strm_fingerprint("/mount/File.mp4", case_sensitive=True)
        fp_upper = make_strm_fingerprint("/mount/FILE.MP4", case_sensitive=True)
        assert fp_lower != fp_upper

    def test_fingerprint_case_insensitive(self):
        fp_lower = make_strm_fingerprint("/mount/File.mp4", case_sensitive=False)
        fp_upper = make_strm_fingerprint("/MOUNT/FILE.MP4", case_sensitive=False)
        assert fp_lower == fp_upper

    def test_fingerprint_uses_version_prefix(self):
        path = "/mount/test.mp4"
        canonical = canonicalize_webdav_path(path)
        expected_payload = f"{FINGERPRINT_VERSION}:{canonical}"
        expected = hashlib.sha256(expected_payload.encode("utf-8")).hexdigest()
        assert make_strm_fingerprint(path) == expected

    def test_fingerprint_normalizes_path(self):
        # trailing slash should be stripped before fingerprinting
        fp1 = make_strm_fingerprint("/mount/folder/")
        fp2 = make_strm_fingerprint("/mount/folder")
        assert fp1 == fp2


# ===========================================================================
# TestReadStrmWebdavPath
# ===========================================================================


class TestReadStrmWebdavPath:
    def test_read_valid_strm(self, tmp_path):
        strm_file = tmp_path / "test.strm"
        strm_file.write_text("https://host/d/mount/folder/video.mp4", encoding="utf-8")
        result = read_strm_webdav_path(strm_file)
        assert result == "/mount/folder/video.mp4"

    def test_read_file_not_found_returns_none(self, tmp_path):
        result = read_strm_webdav_path(tmp_path / "nonexistent.strm")
        assert result is None

    def test_read_empty_file_returns_none(self, tmp_path):
        strm_file = tmp_path / "empty.strm"
        strm_file.write_text("", encoding="utf-8")
        result = read_strm_webdav_path(strm_file)
        assert result is None

    def test_read_invalid_content_returns_none(self, tmp_path):
        strm_file = tmp_path / "bad.strm"
        strm_file.write_text("not-a-valid-url-or-path", encoding="utf-8")
        result = read_strm_webdav_path(strm_file)
        assert result is None

    def test_accepts_string_path(self, tmp_path):
        strm_file = tmp_path / "test.strm"
        strm_file.write_text("/some/webdav/path.mp4", encoding="utf-8")
        result = read_strm_webdav_path(str(strm_file))
        assert result == "/some/webdav/path.mp4"

    def test_malformed_strm_returns_none(self, tmp_path):
        """畸形 STRM（二进制垃圾）应返回 None 而非抛出异常"""
        path = tmp_path / "bad.strm"
        path.write_bytes(b"\xff\xfe\x00")
        assert read_strm_webdav_path(path) is None


# ===========================================================================
# TestFileUtils
# ===========================================================================


class TestEnsureParent:
    def test_creates_missing_parent(self, tmp_path):
        deep_file = tmp_path / "a" / "b" / "c" / "file.txt"
        ensure_parent(deep_file)
        assert (tmp_path / "a" / "b" / "c").is_dir()

    def test_existing_parent_is_noop(self, tmp_path):
        f = tmp_path / "file.txt"
        ensure_parent(f)  # tmp_path already exists, should not raise


class TestCopyFile:
    def test_copy_creates_destination(self, tmp_path):
        src = tmp_path / "src.txt"
        src.write_text("hello")
        dst = tmp_path / "sub" / "dst.txt"
        copy_file(src, dst)
        assert dst.exists()
        assert dst.read_text() == "hello"

    def test_copy_preserves_content(self, tmp_path):
        src = tmp_path / "data.strm"
        content = "/mount/folder/video.mp4"
        src.write_text(content)
        dst = tmp_path / "copy.strm"
        copy_file(src, dst)
        assert dst.read_text() == content


class TestMoveFile:
    def test_move_renames_file(self, tmp_path):
        src = tmp_path / "original.txt"
        src.write_text("content")
        dst = tmp_path / "new_dir" / "moved.txt"
        move_file(src, dst)
        assert dst.exists()
        assert not src.exists()

    def test_move_content_preserved(self, tmp_path):
        src = tmp_path / "src.txt"
        src.write_text("data")
        dst = tmp_path / "dst.txt"
        move_file(src, dst)
        assert dst.read_text() == "data"

    def test_move_cross_device_fallback(self, tmp_path):
        """EXDEV error triggers copy+remove fallback."""
        src = tmp_path / "cross_device.txt"
        src.write_text("test_content")
        dst = tmp_path / "dest.txt"

        with patch("shutil.move", side_effect=OSError(errno.EXDEV, "Invalid cross-device link")):
            # Allow copy2 and os.remove to work normally
            move_file(src, dst)

            assert dst.exists()
            assert not src.exists()
            assert dst.read_text() == "test_content"

    def test_move_non_exdev_oserror_propagates(self, tmp_path):
        """Non-EXDEV OSError should be re-raised."""
        src = tmp_path / "src.txt"
        src.write_text("content")
        dst = tmp_path / "dst.txt"

        with patch("shutil.move", side_effect=OSError(errno.EACCES, "Permission denied")):
            with pytest.raises(OSError) as exc_info:
                move_file(src, dst)
            assert exc_info.value.errno == errno.EACCES


class TestSafeRemoveFile:
    def test_removes_existing_file(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("x")
        result = safe_remove_file(f)
        assert result is True
        assert not f.exists()

    def test_returns_true_when_file_not_exist(self, tmp_path):
        result = safe_remove_file(tmp_path / "nonexistent.txt")
        assert result is True

    def test_returns_false_on_permission_error(self, tmp_path):
        f = tmp_path / "locked.txt"
        f.write_text("x")
        with patch("utils.file_utils.Path.unlink", side_effect=PermissionError):
            with patch("utils.file_utils.os.chmod", side_effect=PermissionError):
                result = safe_remove_file(f)
        assert result is False


class TestRemoveEmptyDirs:
    def test_removes_empty_subdirs(self, tmp_path):
        empty_sub = tmp_path / "empty"
        empty_sub.mkdir()
        remove_empty_dirs(tmp_path)
        assert not empty_sub.exists()

    def test_preserves_non_empty_dirs(self, tmp_path):
        non_empty = tmp_path / "nonempty"
        non_empty.mkdir()
        (non_empty / "file.txt").write_text("x")
        remove_empty_dirs(tmp_path)
        assert non_empty.exists()

    def test_does_not_remove_root_itself(self, tmp_path):
        remove_empty_dirs(tmp_path)
        assert tmp_path.exists()

    def test_noop_when_root_not_exist(self, tmp_path):
        remove_empty_dirs(tmp_path / "missing")  # should not raise


class TestLocalRelative:
    def test_relative_path(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        target = root / "sub" / "file.txt"
        result = local_relative(root, target)
        assert result == Path("sub") / "file.txt"


class TestLocalJoin:
    def test_joins_root_and_relative(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        rel = Path("sub") / "file.txt"
        result = local_join(root, rel)
        assert result == root.resolve() / rel


class TestQuarantineFile:
    def test_renames_to_invalid_suffix(self, tmp_path):
        f = tmp_path / "test.strm"
        f.write_text("content")
        result = quarantine_file(f)
        assert result is not None
        assert result.exists()
        assert result.name.endswith(".invalid")
        assert not f.exists()

    def test_returns_none_when_file_not_exist(self, tmp_path):
        result = quarantine_file(tmp_path / "nonexistent.strm")
        assert result is None

    def test_appends_timestamp_when_target_exists(self, tmp_path):
        f = tmp_path / "test.strm"
        f.write_text("content")
        # Pre-create the .invalid target
        existing_invalid = tmp_path / "test.strm.invalid"
        existing_invalid.write_text("old")
        result = quarantine_file(f)
        assert result is not None
        # The new name should differ from the pre-existing invalid
        assert result != existing_invalid

    def test_custom_suffix(self, tmp_path):
        f = tmp_path / "test.strm"
        f.write_text("content")
        result = quarantine_file(f, suffix=".quarantined")
        assert result is not None
        assert result.name.endswith(".quarantined")



# ===========================================================================
# TestWebdavUtils
# ===========================================================================


class TestWebdavParent:
    def test_returns_parent_dir(self):
        assert webdav_parent("/mount/folder/file.mp4") == "/mount/folder"

    def test_single_segment_returns_root(self):
        assert webdav_parent("/mount") == "/"

    def test_root_returns_root(self):
        assert webdav_parent("/") == "/"

    def test_multi_level(self):
        assert webdav_parent("/a/b/c/d") == "/a/b/c"


class TestWebdavRootName:
    def test_extracts_first_segment(self):
        assert webdav_root_name("/mount/folder/file.mp4") == "mount"

    def test_single_segment(self):
        assert webdav_root_name("/mount") == "mount"

    def test_root_returns_empty(self):
        assert webdav_root_name("/") == ""


class TestBuildWebdavTrashPath:
    def test_basic_trash_path(self):
        result = build_webdav_trash_path("/mount/folder/file.mp4", ".trash")
        assert result == "/mount/.trash/folder/file.mp4"

    def test_file_at_root_level(self):
        result = build_webdav_trash_path("/mount/file.mp4", ".trash")
        assert result == "/mount/.trash/file.mp4"

    def test_raises_for_single_segment_path(self):
        with pytest.raises(ValueError):
            build_webdav_trash_path("/file.mp4", ".trash")

    def test_raises_for_root_path(self):
        with pytest.raises(ValueError):
            build_webdav_trash_path("/", ".trash")


class TestCanonicalizeWebdavPathForCloud:
    def test_backslash_to_slash(self):
        result = _canonicalize_webdav_path_for_cloud("/foo\\bar")
        assert "\\" not in result

    def test_url_decodes(self):
        result = _canonicalize_webdav_path_for_cloud("/mount/%E5%8A%A8%E6%BC%AB")
        assert "动漫" in result

    def test_normalizes_double_slashes(self):
        result = _canonicalize_webdav_path_for_cloud("/a//b")
        assert "//" not in result

    def test_empty_returns_empty(self):
        result = _canonicalize_webdav_path_for_cloud("")
        assert result == ""

    def test_strips_trailing_slash(self):
        result = _canonicalize_webdav_path_for_cloud("/mount/folder/")
        assert not result.endswith("/")
