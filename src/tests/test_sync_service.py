"""
Unit tests for domain/sync/sync_service.py
Covers: initial_scan_a, scan_a_to_b_full_sync, copy_a_record_to_b_if_needed,
        copy_a_record_to_b
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# Add src/ to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from domain.sync.sync_service import SyncService
from database import ARecord
from _test_helpers import build_mock_app


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_app(tmp_path: Path, *, a_dirs: list[Path] | None = None) -> Mock:
    """Build a minimal mock AppService with the required attributes.

    委托给 _test_helpers.build_mock_app，消除重复实现。
    """
    return build_mock_app(tmp_path, a_dirs=a_dirs, use_mock=True)


def _make_a_record(
    local_path: str = "/a/folder/file.strm",
    webdav_path: str = "/mount/folder/file.mp4",
    parent: str = "/mount/folder",
) -> ARecord:
    return ARecord(
        local_path=local_path,
        webdav_path=webdav_path,
        parent_webdav_path=parent,
        updated_at=0,
    )


# ===========================================================================
# TestSyncServiceInitialScanA
# ===========================================================================


class TestSyncServiceInitialScanA:
    def test_scan_a_finds_strm_files(self, tmp_path):
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        # Create some STRM files
        (a_root / "movie.strm").write_text("/mount/movie.mp4", encoding="utf-8")
        subdir = a_root / "show" / "Season 01"
        subdir.mkdir(parents=True)
        (subdir / "ep01.strm").write_text("/mount/show/S01E01.mp4", encoding="utf-8")

        svc = SyncService(app)
        svc.initial_scan_a()

        assert app.handle_a_created_or_modified.call_count == 2

    def test_scan_a_empty_directory(self, tmp_path):
        app = _make_app(tmp_path)
        # a_root exists but is empty
        svc = SyncService(app)
        svc.initial_scan_a()
        app.handle_a_created_or_modified.assert_not_called()

    def test_scan_a_missing_root_is_skipped(self, tmp_path):
        app = _make_app(tmp_path)
        missing_root = tmp_path / "nonexistent"
        app.a_roots = [missing_root]  # override to a non-existing path
        svc = SyncService(app)
        svc.initial_scan_a()
        app.handle_a_created_or_modified.assert_not_called()

    def test_scan_a_ignores_non_strm_files(self, tmp_path):
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        (a_root / "video.mp4").write_text("binary", encoding="utf-8")
        (a_root / "info.nfo").write_text("nfo", encoding="utf-8")
        (a_root / "real.strm").write_text("/mount/file.mp4", encoding="utf-8")

        svc = SyncService(app)
        svc.initial_scan_a()

        # Only the .strm file triggers handle_a_created_or_modified
        assert app.handle_a_created_or_modified.call_count == 1


# ===========================================================================
# TestSyncServiceScanAToBFullSync
# ===========================================================================


class TestSyncServiceScanAToBFullSync:
    def _setup_records(self, app: Mock, records: list[ARecord], tmp_path: Path):
        # Make sure each local_path actually exists on disk
        for rec in records:
            p = Path(rec.local_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(rec.webdav_path, encoding="utf-8")
        app.db.get_all_a_records.return_value = records
        app.db.is_ghost_protected.return_value = False
        app.db.b_fingerprint_exists.return_value = False

    def test_full_sync_all_records(self, tmp_path):
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        records = [
            _make_a_record(str(a_root / "file1.strm"), "/m/f1.mp4", "/m"),
            _make_a_record(str(a_root / "file2.strm"), "/m/f2.mp4", "/m"),
        ]
        self._setup_records(app, records, tmp_path)
        b_root = tmp_path / "b"
        b_root.mkdir()
        app.build_b_path_from_a.return_value = b_root / "file1.strm"

        svc = SyncService(app)
        with patch.object(svc, "copy_a_record_to_b", return_value=True) as mock_copy:
            svc.scan_a_to_b_full_sync()

        assert mock_copy.call_count == 2

    def test_full_sync_with_engine_path_filter(self, tmp_path):
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        records = [
            _make_a_record(str(a_root / "f1.strm"), "/engine/show/ep1.mp4", "/engine/show"),
            _make_a_record(str(a_root / "f2.strm"), "/other/show/ep1.mp4", "/other/show"),
        ]
        self._setup_records(app, records, tmp_path)

        svc = SyncService(app)
        with patch.object(svc, "copy_a_record_to_b", return_value=True) as mock_copy:
            svc.scan_a_to_b_full_sync(valid_engine_paths=["/engine"])

        # only /engine path should be synced
        assert mock_copy.call_count == 1
        call_args = mock_copy.call_args[0]
        assert call_args[1].startswith("/engine")

    def test_full_sync_skip_ghost_protected(self, tmp_path):
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        records = [_make_a_record(str(a_root / "f1.strm"), "/m/f1.mp4", "/m")]
        self._setup_records(app, records, tmp_path)
        app.db.is_ghost_protected.return_value = True

        svc = SyncService(app)
        with patch.object(svc, "copy_a_record_to_b") as mock_copy:
            svc.scan_a_to_b_full_sync()

        mock_copy.assert_not_called()

    def test_full_sync_skip_missing_source(self, tmp_path):
        app = _make_app(tmp_path)
        records = [_make_a_record("/nonexistent/path/file.strm", "/m/f.mp4", "/m")]
        app.db.get_all_a_records.return_value = records
        app.db.is_ghost_protected.return_value = False

        svc = SyncService(app)
        with patch.object(svc, "copy_a_record_to_b") as mock_copy:
            svc.scan_a_to_b_full_sync()

        mock_copy.assert_not_called()


# ===========================================================================
# TestSyncServiceCopyARecordToB
# ===========================================================================


class TestCopyARecordToBIfNeeded:
    def test_skip_when_ghost_protected(self, tmp_path):
        app = _make_app(tmp_path)
        app.db.is_ghost_protected.return_value = True
        svc = SyncService(app)
        result = svc.copy_a_record_to_b_if_needed("/a/f.strm", "/m/f.mp4", "/m")
        assert result is None

    def test_skip_when_fingerprint_exists_in_b(self, tmp_path):
        app = _make_app(tmp_path)
        app.db.is_ghost_protected.return_value = False
        app.db.b_fingerprint_exists.return_value = True
        svc = SyncService(app)
        result = svc.copy_a_record_to_b_if_needed("/a/f.strm", "/m/f.mp4", "/m")
        assert result is None

    def test_delegates_to_copy_a_record_to_b(self, tmp_path):
        app = _make_app(tmp_path)
        app.db.is_ghost_protected.return_value = False
        app.db.b_fingerprint_exists.return_value = False
        svc = SyncService(app)
        with patch.object(svc, "copy_a_record_to_b", return_value=True) as mock_copy:
            result = svc.copy_a_record_to_b_if_needed("/a/f.strm", "/m/f.mp4", "/m")
        mock_copy.assert_called_once_with("/a/f.strm", "/m/f.mp4", "/m")
        assert result is True


class TestCopyARecordToB:
    def test_copy_success(self, tmp_path):
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        a_file = a_root / "file.strm"
        a_file.write_text("/mount/file.mp4", encoding="utf-8")

        b_file = tmp_path / "b" / "file.strm"
        b_file.parent.mkdir(parents=True, exist_ok=True)
        app.build_b_path_from_a.return_value = b_file
        app._verify_b_path_lineage.return_value = True
        app.admin_api.check_exists.return_value = True
        app.db.upsert_b = Mock()
        app.db.upsert_identity = Mock()

        svc = SyncService(app)
        result = svc.copy_a_record_to_b(str(a_file), "/mount/file.mp4", "/mount")

        assert result is True
        assert b_file.exists()

    def test_copy_lineage_fail_returns_false(self, tmp_path):
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        a_file = a_root / "file.strm"
        a_file.write_text("/mount/file.mp4", encoding="utf-8")
        b_file = tmp_path / "b" / "file.strm"
        app.build_b_path_from_a.return_value = b_file
        app._verify_b_path_lineage.return_value = False

        svc = SyncService(app)
        result = svc.copy_a_record_to_b(str(a_file), "/mount/file.mp4", "/mount")

        assert result is False

    def test_copy_b_already_exists_same_content(self, tmp_path):
        """B file already exists with same WebDAV path — db is updated, returns None."""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        webdav_path = "/mount/file.mp4"
        a_file = a_root / "file.strm"
        a_file.write_text(webdav_path, encoding="utf-8")

        b_file = tmp_path / "b" / "file.strm"
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text(webdav_path, encoding="utf-8")  # same content

        app.build_b_path_from_a.return_value = b_file
        app._verify_b_path_lineage.return_value = True
        app.db.upsert_b = Mock()
        app.db.upsert_identity = Mock()

        svc = SyncService(app)
        result = svc.copy_a_record_to_b(str(a_file), webdav_path, "/mount")

        assert result is None
        app.db.upsert_b.assert_called_once()

    def test_copy_webdav_not_exists_cleanup(self, tmp_path):
        """If WebDAV source doesn't exist, A record is cleaned up."""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        webdav_path = "/mount/gone.mp4"
        a_file = a_root / "gone.strm"
        a_file.write_text(webdav_path, encoding="utf-8")

        b_file = tmp_path / "b" / "gone.strm"
        app.build_b_path_from_a.return_value = b_file
        app._verify_b_path_lineage.return_value = True
        app.admin_api.check_exists.return_value = False
        app.db.delete_a_by_local = Mock()
        app.db.set_ghost_protection = Mock()

        svc = SyncService(app)
        result = svc.copy_a_record_to_b(str(a_file), webdav_path, "/mount")

        assert result is False
        app.db.delete_a_by_local.assert_called_once()
        app.db.set_ghost_protection.assert_called_once()

    def test_copy_io_error_returns_false(self, tmp_path):
        """IO error during shutil.copyfile returns False."""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        webdav_path = "/mount/file.mp4"
        a_file = a_root / "file.strm"
        a_file.write_text(webdav_path, encoding="utf-8")

        b_file = tmp_path / "b" / "file.strm"
        b_file.parent.mkdir(parents=True, exist_ok=True)
        app.build_b_path_from_a.return_value = b_file
        app._verify_b_path_lineage.return_value = True
        app.admin_api.check_exists.return_value = True

        svc = SyncService(app)
        with patch("shutil.copyfile", side_effect=OSError("disk full")):
            result = svc.copy_a_record_to_b(str(a_file), webdav_path, "/mount")

        assert result is False

    def test_copy_db_error_rolls_back(self, tmp_path):
        """DB error after copy — file gets removed and returns False."""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        webdav_path = "/mount/file.mp4"
        a_file = a_root / "file.strm"
        a_file.write_text(webdav_path, encoding="utf-8")

        b_file = tmp_path / "b" / "file.strm"
        b_file.parent.mkdir(parents=True, exist_ok=True)
        app.build_b_path_from_a.return_value = b_file
        app._verify_b_path_lineage.return_value = True
        app.admin_api.check_exists.return_value = True
        app.db.upsert_b.side_effect = Exception("db failure")

        svc = SyncService(app)
        result = svc.copy_a_record_to_b(str(a_file), webdav_path, "/mount")

        assert result is False
        # b_file should have been cleaned up by safe_remove_file
        assert not b_file.exists()

    def test_copy_source_file_missing_after_check(self, tmp_path):
        """Source file disappears between the webdav check and copyfile."""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        webdav_path = "/mount/file.mp4"
        a_file = a_root / "file.strm"
        a_file.write_text(webdav_path, encoding="utf-8")
        # Then delete it so it's gone during the copy step
        a_file.unlink()

        b_file = tmp_path / "b" / "file.strm"
        b_file.parent.mkdir(parents=True, exist_ok=True)
        app.build_b_path_from_a.return_value = b_file
        app._verify_b_path_lineage.return_value = True
        app.admin_api.check_exists.return_value = True

        svc = SyncService(app)
        result = svc.copy_a_record_to_b(str(a_file), webdav_path, "/mount")

        assert result is False

    def test_copy_build_b_path_raises_value_error(self, tmp_path):
        """ValueError from build_b_path_from_a returns False."""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        a_file = a_root / "file.strm"
        a_file.write_text("/mount/file.mp4", encoding="utf-8")
        app.build_b_path_from_a.side_effect = ValueError("not under any root")

        svc = SyncService(app)
        result = svc.copy_a_record_to_b(str(a_file), "/mount/file.mp4", "/mount")

        assert result is False
