"""
Unit tests for domain/sync/sync_service.py
Covers: initial_scan_a, scan_a_to_b_full_sync, copy_a_record_to_b_if_needed,
        copy_a_record_to_b
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# Add src/ to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from domain.sync.sync_service import SyncService
from database import ARecord, Database
from config import ABMapping
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
        (a_root / "movie.strm").write_text(
            "/mount/movie.mp4", encoding="utf-8")
        subdir = a_root / "show" / "Season 01"
        subdir.mkdir(parents=True)
        (subdir / "ep01.strm").write_text(
            "/mount/show/S01E01.mp4", encoding="utf-8")

        svc = SyncService(app)
        # Use patch to capture the batch before it's cleared
        with patch.object(svc.db, "upsert_a_batch") as mock_upsert:
            svc.initial_scan_a(use_bulk=False)
            assert mock_upsert.call_count == 1
            # Mock stores a reference; call_args_list preserves the reference
            # at call time. Verify via the captured list argument.
            # Note: call_args reflects current state (after clear), so
            # verify count and that save_known_folders was called.
            saved_folders = app.db.save_known_folders_batch.call_args[0][0]
            assert "/mount" in saved_folders
            assert "/mount/show" in saved_folders

    def test_scan_a_saves_parent_folders(self, tmp_path):
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        (a_root / "movie.strm").write_text(
            "/mount/movie.mp4", encoding="utf-8")
        subdir = a_root / "show"
        subdir.mkdir(parents=True)
        (subdir / "ep01.strm").write_text(
            "/mount/show/ep01.mp4", encoding="utf-8")

        svc = SyncService(app)
        svc.initial_scan_a(use_bulk=False)

        assert app.db.save_known_folders_batch.call_count == 1
        saved_folders = app.db.save_known_folders_batch.call_args[0][0]
        assert "/mount" in saved_folders
        assert "/mount/show" in saved_folders

    def test_scan_a_explicit_root_subset(self, tmp_path):
        """周期刷新可显式只扫描命中 refresh_paths 的 A root。"""
        first = tmp_path / "a1"
        second = tmp_path / "a2"
        first.mkdir()
        second.mkdir()
        (first / "one.strm").write_text("/engine-a/one.mp4", encoding="utf-8")
        (second / "two.strm").write_text("/engine-b/two.mp4", encoding="utf-8")
        app = _make_app(tmp_path, a_dirs=[first, second])
        svc = SyncService(app)

        captured: list[tuple[str, str, str]] = []

        def capture(records):
            captured.extend(list(records))
            return len(records)

        app.db.upsert_a_batch.side_effect = capture
        svc.initial_scan_a(use_bulk=False, a_roots=[first])

        assert [Path(row[0]).name for row in captured] == ["one.strm"]
        assert all(not row[0].startswith(str(second)) for row in captured)

    def test_scan_a_explicit_empty_roots_is_noop(self, tmp_path):
        """refresh_paths 无匹配时传空列表，不能回退为扫描全部 A root。"""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        (a_root / "movie.strm").write_text("/mount/movie.mp4", encoding="utf-8")
        svc = SyncService(app)

        svc.initial_scan_a(use_bulk=False, a_roots=[])

        app.db.upsert_a_batch.assert_not_called()
        app.db.save_known_folders_batch.assert_not_called()

    def test_scan_a_empty_directory(self, tmp_path):
        app = _make_app(tmp_path)
        svc = SyncService(app)
        svc.initial_scan_a(use_bulk=False)
        app.db.upsert_a_batch.assert_not_called()
        app.db.save_known_folders_batch.assert_not_called()

    def test_scan_a_missing_root_is_skipped(self, tmp_path):
        app = _make_app(tmp_path)
        missing_root = tmp_path / "nonexistent"
        app.a_roots = [missing_root]
        svc = SyncService(app)
        svc.initial_scan_a(use_bulk=False)
        app.db.upsert_a_batch.assert_not_called()

    def test_scan_a_ignores_non_strm_files(self, tmp_path):
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        (a_root / "video.mp4").write_text("binary", encoding="utf-8")
        (a_root / "info.nfo").write_text("nfo", encoding="utf-8")
        (a_root / "real.strm").write_text(
            "/mount/file.mp4", encoding="utf-8")

        svc = SyncService(app)
        svc.initial_scan_a(use_bulk=False)

        # Only the .strm file is batched
        assert app.db.upsert_a_batch.call_count == 1

    def test_scan_a_batch_flush_at_boundary(self, tmp_path):
        """Records are flushed when batch reaches BATCH_SIZE."""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        # Create 1001 files to trigger one flush at 1000 + one final flush of 1
        for i in range(1001):
            (a_root / f"file{i}.strm").write_text(
                f"/mount/f{i}.mp4", encoding="utf-8")

        svc = SyncService(app)
        svc.initial_scan_a(use_bulk=False)

        # BATCH_SIZE=1000, so 1001 files → 2 upsert calls (1000 + 1)
        assert app.db.upsert_a_batch.call_count == 2

    def test_scan_a_skips_unparseable_strm(self, tmp_path):
        """STRM files that can't be parsed are skipped."""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        (a_root / "good.strm").write_text(
            "/mount/good.mp4", encoding="utf-8")
        # Empty STRM file — read_strm_webdav_path returns None
        (a_root / "empty.strm").write_text("", encoding="utf-8")

        svc = SyncService(app)
        svc.initial_scan_a(use_bulk=False)

        assert app.db.upsert_a_batch.call_count == 1

    def test_scan_a_bulk_mode_writes_to_bulk_connection(self, tmp_path):
        """use_bulk=True 时使用 bulk_connection 而非 upsert_a_batch。"""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        (a_root / "movie.strm").write_text("/mount/movie.mp4", encoding="utf-8")

        svc = SyncService(app)
        mock_conn = Mock()
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        # Mock execute().fetchall() to return empty list (no existing records)
        mock_conn.execute.return_value.fetchall.return_value = []
        app.db.bulk_connection.return_value = mock_conn

        svc.initial_scan_a(use_bulk=True)

        # bulk_connection 被调用
        app.db.bulk_connection.assert_called_once()
        # upsert_a_batch 不被调用（bulk 模式用 _upsert_a_batch_bulk）
        app.db.upsert_a_batch.assert_not_called()
        # rebuild_fts_table 被调用
        app.db.rebuild_fts_table.assert_called_once_with("a_strm_files", "a_strm_files_fts")

    def test_scan_a_non_bulk_mode_rebuilds_no_fts(self, tmp_path):
        """use_bulk=False 时不调用 rebuild_fts_table。"""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        (a_root / "movie.strm").write_text("/mount/movie.mp4", encoding="utf-8")

        svc = SyncService(app)
        svc.initial_scan_a(use_bulk=False)

        # rebuild_fts_table 不被调用（upsert_a_batch 已逐批维护 FTS）
        app.db.rebuild_fts_table.assert_not_called()
        # upsert_a_batch 被调用
        app.db.upsert_a_batch.assert_called()


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
        app.db.get_all_ghost_protected_paths.return_value = set()
        app.db.get_all_b_fingerprints.return_value = set()
        # Set up mapping resolution (scan_a_to_b_full_sync now calls get_mapping_for_a)
        app.get_mapping_for_a.return_value = ("test_m1", Path("/a_root"), Path("/b_root"))
        app.a_b_mappings = [ABMapping(mapping_id="test_m1", a_root="/a_root", b_root="/b_root")]

    def _make_bulk_conn_mock(self, app):
        """Create a mock connection that supports context manager protocol."""
        mock_conn = Mock()
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        # [已修复] Task 1: use_bulk=False 走 self.db.connection()，需同时 mock
        app.db.bulk_connection.return_value.__enter__ = Mock(return_value=mock_conn)
        app.db.bulk_connection.return_value.__exit__ = Mock(return_value=False)
        app.db.connection.return_value.__enter__ = Mock(return_value=mock_conn)
        app.db.connection.return_value.__exit__ = Mock(return_value=False)
        return mock_conn

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
        # 两个源映射到不同 B 目标（非冲突场景）
        def _build_side_effect(local_path, webdav_path=None):
            name = Path(local_path).stem
            return b_root / f"{name}.strm"
        app.build_b_path_from_a.side_effect = _build_side_effect

        svc = SyncService(app)
        mock_conn = self._make_bulk_conn_mock(app)
        app.db.bulk_connection.return_value.__enter__ = Mock(return_value=mock_conn)
        app.db.bulk_connection.return_value.__exit__ = Mock(return_value=False)
        with patch.object(svc, "_sync_one_record", return_value="success") as mock_sync:
            svc.scan_a_to_b_full_sync(use_bulk=True)

        assert mock_sync.call_count == 2

    def test_full_sync_with_engine_path_filter(self, tmp_path):
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        records = [
            _make_a_record(str(a_root / "f1.strm"), "/engine/show/ep1.mp4", "/engine/show"),
            _make_a_record(str(a_root / "f2.strm"), "/other/show/ep1.mp4", "/other/show"),
        ]
        self._setup_records(app, records, tmp_path)

        svc = SyncService(app)
        mock_conn = self._make_bulk_conn_mock(app)
        app.db.bulk_connection.return_value.__enter__ = Mock(return_value=mock_conn)
        app.db.bulk_connection.return_value.__exit__ = Mock(return_value=False)
        with patch.object(svc, "_sync_one_record", return_value="success") as mock_sync:
            svc.scan_a_to_b_full_sync(valid_engine_paths=["/engine"])

        # 两遍结构中，过滤在索引阶段完成，只有 1 条通过引擎路径过滤
        assert mock_sync.call_count == 1

    def test_full_sync_skip_ghost_protected(self, tmp_path):
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        records = [_make_a_record(str(a_root / "f1.strm"), "/m/f1.mp4", "/m")]
        self._setup_records(app, records, tmp_path)
        app.db.get_all_ghost_protected_paths.return_value = {"/m/f1.mp4"}

        svc = SyncService(app)
        mock_conn = self._make_bulk_conn_mock(app)
        app.db.bulk_connection.return_value.__enter__ = Mock(return_value=mock_conn)
        app.db.bulk_connection.return_value.__exit__ = Mock(return_value=False)
        with patch.object(svc, "_sync_one_record", return_value="skip_ghost") as mock_sync:
            svc.scan_a_to_b_full_sync()

        # 两遍结构中，ghost 检查在索引阶段完成，_sync_one_record 不被调用
        mock_sync.assert_not_called()

    def test_full_sync_skip_missing_source(self, tmp_path):
        app = _make_app(tmp_path)
        records = [_make_a_record("/nonexistent/path/file.strm", "/m/f.mp4", "/m")]
        app.db.get_all_a_records.return_value = records
        app.db.get_all_ghost_protected_paths.return_value = set()
        app.db.get_all_b_fingerprints.return_value = set()

        svc = SyncService(app)
        mock_conn = self._make_bulk_conn_mock(app)
        app.db.bulk_connection.return_value.__enter__ = Mock(return_value=mock_conn)
        app.db.bulk_connection.return_value.__exit__ = Mock(return_value=False)
        with patch.object(svc, "_sync_one_record", return_value="skip_missing") as mock_sync:
            svc.scan_a_to_b_full_sync()

        # 两遍结构中，文件存在性检查在索引阶段完成，_sync_one_record 不被调用
        mock_sync.assert_not_called()

    def _setup_bulk_records(self, app: Mock, count: int, tmp_path: Path = None):
        """Set up db mocks for *count* A records and write files to disk.

        _sync_one_record is patched by the caller, so real STRM content
        is not needed, but the files must exist for the index pass to pass.
        """
        base_dir = tmp_path if tmp_path else Path("/a")
        records = [_make_a_record(str(base_dir / f"f{i}.strm"), f"/m/f{i}.mp4", "/m")
                   for i in range(count)]
        # Write files to disk so the index pass doesn't skip them
        for rec in records:
            p = Path(rec.local_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(rec.webdav_path, encoding="utf-8")
        app.db.get_all_a_records.return_value = records
        app.db.get_all_ghost_protected_paths.return_value = set()
        app.db.get_all_b_fingerprints.return_value = set()
        # Set up mapping resolution (scan_a_to_b_full_sync now calls get_mapping_for_a)
        app.get_mapping_for_a.return_value = ("test_m1", Path("/a_root"), Path("/b_root"))
        app.a_b_mappings = [ABMapping(mapping_id="test_m1", a_root="/a_root", b_root="/b_root")]
        # 每个记录映射到不同的 B 目标路径（避免全部冲突）
        b_root = tmp_path / "b" if tmp_path else Path("/b")
        b_root.mkdir(exist_ok=True)
        app.build_b_path_from_a.side_effect = lambda local, webdav=None: b_root / Path(local).name

    def test_full_sync_with_bulk_mode_single_commit(self, tmp_path):
        """use_bulk=True with >BATCH_COMMIT_SIZE records: exactly 1 commit.

        Bulk mode never issues interim commits.  The only commit comes from
        the final ``if batch_count > 0: conn.commit()`` after the loop.
        """
        app = _make_app(tmp_path)
        N = 1001  # > BATCH_COMMIT_SIZE (1000)
        self._setup_bulk_records(app, N, tmp_path)

        svc = SyncService(app)
        mock_conn = self._make_bulk_conn_mock(app)
        app.db.bulk_connection.return_value.__enter__ = Mock(return_value=mock_conn)
        app.db.bulk_connection.return_value.__exit__ = Mock(return_value=False)
        with patch.object(svc, "_sync_one_record", return_value="success") as mock_sync:
            svc.scan_a_to_b_full_sync(use_bulk=True)

        assert mock_sync.call_count == N
        # Bulk mode: only the trailing commit after the loop
        assert mock_conn.commit.call_count == 1

    def test_full_sync_with_batch_mode_multi_commit(self, tmp_path):
        """use_bulk=False with >BATCH_COMMIT_SIZE records: interim + final.

        Batch mode commits every BATCH_COMMIT_SIZE records and once at the
        end.  With 1001 records that means 1 interim commit (after record
        1000) + 1 trailing commit = 2 total.
        """
        app = _make_app(tmp_path)
        N = 1001  # > BATCH_COMMIT_SIZE (1000)
        self._setup_bulk_records(app, N, tmp_path)

        svc = SyncService(app)
        mock_conn = self._make_bulk_conn_mock(app)
        app.db.bulk_connection.return_value.__enter__ = Mock(return_value=mock_conn)
        app.db.bulk_connection.return_value.__exit__ = Mock(return_value=False)
        with patch.object(svc, "_sync_one_record", return_value="success") as mock_sync:
            svc.scan_a_to_b_full_sync(use_bulk=False)

        assert mock_sync.call_count == N
        # Batch mode: interim commit at 1000 + trailing commit = 2
        assert mock_conn.commit.call_count == 2

    def test_full_sync_batch_mode_exact_boundary(self, tmp_path):
        """use_bulk=False with exactly BATCH_COMMIT_SIZE records.

        1000 records: batch_count reaches 1000 at the last record and commits
        (interim), then the loop ends with batch_count reset to 0, so the
        trailing ``if batch_count > 0`` is False → exactly 1 commit.
        """
        app = _make_app(tmp_path)
        N = 1000  # exactly BATCH_COMMIT_SIZE
        self._setup_bulk_records(app, N, tmp_path)

        svc = SyncService(app)
        mock_conn = self._make_bulk_conn_mock(app)
        app.db.bulk_connection.return_value.__enter__ = Mock(return_value=mock_conn)
        app.db.bulk_connection.return_value.__exit__ = Mock(return_value=False)
        with patch.object(svc, "_sync_one_record", return_value="success") as mock_sync:
            svc.scan_a_to_b_full_sync(use_bulk=False)

        assert mock_sync.call_count == N
        # Exactly at boundary: 1 interim commit (at record 1000), trailing is skipped
        assert mock_conn.commit.call_count == 1

    def test_full_sync_bulk_mode_under_threshold(self, tmp_path):
        """use_bulk=True with <BATCH_COMMIT_SIZE records: still 1 commit."""
        app = _make_app(tmp_path)
        N = 999
        self._setup_bulk_records(app, N, tmp_path)

        svc = SyncService(app)
        mock_conn = self._make_bulk_conn_mock(app)
        app.db.bulk_connection.return_value.__enter__ = Mock(return_value=mock_conn)
        app.db.bulk_connection.return_value.__exit__ = Mock(return_value=False)
        with patch.object(svc, "_sync_one_record", return_value="success") as mock_sync:
            svc.scan_a_to_b_full_sync(use_bulk=True)

        assert mock_sync.call_count == N
        assert mock_conn.commit.call_count == 1

    def test_full_sync_batch_mode_under_threshold(self, tmp_path):
        """use_bulk=False with <BATCH_COMMIT_SIZE records: 1 trailing commit only."""
        app = _make_app(tmp_path)
        N = 999
        self._setup_bulk_records(app, N, tmp_path)

        svc = SyncService(app)
        mock_conn = self._make_bulk_conn_mock(app)
        app.db.bulk_connection.return_value.__enter__ = Mock(return_value=mock_conn)
        app.db.bulk_connection.return_value.__exit__ = Mock(return_value=False)
        with patch.object(svc, "_sync_one_record", return_value="success") as mock_sync:
            svc.scan_a_to_b_full_sync(use_bulk=False)

        assert mock_sync.call_count == N
        # Under threshold: no interim commits, just the trailing commit
        assert mock_conn.commit.call_count == 1

    def test_full_sync_caches_cleared_on_exception(self, tmp_path):
        """Caches are cleared even if an exception occurs."""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        records = [_make_a_record(str(a_root / "f.strm"), "/m/f.mp4", "/m")]
        self._setup_records(app, records, tmp_path)

        svc = SyncService(app)
        mock_conn = self._make_bulk_conn_mock(app)
        app.db.bulk_connection.return_value.__enter__ = Mock(return_value=mock_conn)
        app.db.bulk_connection.return_value.__exit__ = Mock(return_value=False)
        with patch.object(svc, "_sync_one_record", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                svc.scan_a_to_b_full_sync()

        # Caches should be cleared after exception
        assert svc._cache_ghost is None
        assert svc._cache_b_fp is None

    def test_full_sync_uses_bulk_connection(self, tmp_path):
        """scan_a_to_b_full_sync uses bulk_connection() instead of per-record upsert_b."""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        records = [
            _make_a_record(str(a_root / "file1.strm"), "/m/f1.mp4", "/m"),
            _make_a_record(str(a_root / "file2.strm"), "/m/f2.mp4", "/m"),
        ]
        self._setup_records(app, records, tmp_path)
        b_root = tmp_path / "b"
        b_root.mkdir()
        # 两个源映射到不同 B 目标
        app.build_b_path_from_a.side_effect = lambda local, webdav=None: b_root / Path(local).name

        svc = SyncService(app)
        mock_conn = self._make_bulk_conn_mock(app)
        app.db.bulk_connection.return_value.__enter__ = Mock(return_value=mock_conn)
        app.db.bulk_connection.return_value.__exit__ = Mock(return_value=False)

        # Patch _bulk_upsert_b to verify it's called
        with patch.object(svc, "_bulk_upsert_b") as mock_bulk_upsert:
            svc.scan_a_to_b_full_sync()
            # _bulk_upsert_b should be called for each record
            assert mock_bulk_upsert.call_count >= 1

    def test_full_sync_skips_lineage(self, tmp_path):
        """scan_a_to_b_full_sync does NOT call _verify_b_path_lineage (startup sync skips lineage)."""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        records = [
            _make_a_record(str(a_root / "file1.strm"), "/m/f1.mp4", "/m"),
        ]
        self._setup_records(app, records, tmp_path)
        b_root = tmp_path / "b"
        b_root.mkdir()
        app.build_b_path_from_a.return_value = b_root / "file1.strm"

        svc = SyncService(app)
        mock_conn = self._make_bulk_conn_mock(app)
        app.db.bulk_connection.return_value.__enter__ = Mock(return_value=mock_conn)
        app.db.bulk_connection.return_value.__exit__ = Mock(return_value=False)

        # _verify_b_path_lineage should NOT be called during bulk sync
        with patch.object(app, "_verify_b_path_lineage") as mock_lineage:
            svc.scan_a_to_b_full_sync()
            # Lineage verification is skipped in bulk sync
            assert mock_lineage.call_count == 0

    def test_full_sync_target_conflict_all_sources_skipped(self, tmp_path):
        """两个 A 源映射到同一 B 目标但 WebDAV 不同 → 全部跳过，无文件拷贝"""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        records = [
            _make_a_record(str(a_root / "ep10_a.strm"), "/show/ep10_ver1.mp4", "/show"),
            _make_a_record(str(a_root / "ep10_b.strm"), "/show/ep10_ver2.mp4", "/show"),
        ]
        self._setup_records(app, records, tmp_path)
        # 两个源计算出相同的 B 目标路径
        b_root = tmp_path / "b"
        b_root.mkdir()
        app.build_b_path_from_a.return_value = b_root / "Season 20" / "S20E10.strm"

        svc = SyncService(app)
        mock_conn = self._make_bulk_conn_mock(app)
        app.db.bulk_connection.return_value.__enter__ = Mock(return_value=mock_conn)
        app.db.bulk_connection.return_value.__exit__ = Mock(return_value=False)
        with patch.object(svc, "_sync_one_record", return_value="success") as mock_sync:
            svc.scan_a_to_b_full_sync()

        # 冲突目标全部在索引阶段被跳过，_sync_one_record 不被调用
        mock_sync.assert_not_called()

    def test_full_sync_target_conflict_same_webdav_not_conflict(self, tmp_path):
        """两个 A 源映射到同一 B 目标且 WebDAV 相同 → 不是冲突，正常去重"""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        records = [
            _make_a_record(str(a_root / "ep10_a.strm"), "/show/ep10.mp4", "/show"),
            _make_a_record(str(a_root / "ep10_b.strm"), "/show/ep10.mp4", "/show"),
        ]
        self._setup_records(app, records, tmp_path)
        b_root = tmp_path / "b"
        b_root.mkdir()
        app.build_b_path_from_a.return_value = b_root / "Season 20" / "S20E10.strm"

        svc = SyncService(app)
        mock_conn = self._make_bulk_conn_mock(app)
        app.db.bulk_connection.return_value.__enter__ = Mock(return_value=mock_conn)
        app.db.bulk_connection.return_value.__exit__ = Mock(return_value=False)
        with patch.object(svc, "_sync_one_record", return_value="success") as mock_sync:
            svc.scan_a_to_b_full_sync()

        # 同 WebDAV 的两个源都通过索引阶段，由 _sync_one_record 去重
        assert mock_sync.call_count == 2

    def test_full_sync_no_network_during_index_phase(self, tmp_path):
        """索引阶段不触发任何网络调用"""
        import shutil
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        records = [
            _make_a_record(str(a_root / "f1.strm"), "/m/f1.mp4", "/m"),
        ]
        self._setup_records(app, records, tmp_path)
        b_root = tmp_path / "b"
        b_root.mkdir()
        app.build_b_path_from_a.return_value = b_root / "f1.strm"

        svc = SyncService(app)
        mock_conn = self._make_bulk_conn_mock(app)
        app.db.bulk_connection.return_value.__enter__ = Mock(return_value=mock_conn)
        app.db.bulk_connection.return_value.__exit__ = Mock(return_value=False)

        # 确保 admin_api（网络客户端）在扫描阶段未被调用
        with patch.object(svc, "_sync_one_record", return_value="success"):
            svc.scan_a_to_b_full_sync()
        app.admin_api.check_exists.assert_not_called()


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
        app.get_mapping_for_a.return_value = ("test_m1", Path("/a_root"), Path("/b_root"))
        app.db.b_fingerprint_exists.return_value = True
        svc = SyncService(app)
        result = svc.copy_a_record_to_b_if_needed("/a/f.strm", "/m/f.mp4", "/m")
        assert result is None

    def test_delegates_to_copy_a_record_to_b(self, tmp_path):
        app = _make_app(tmp_path)
        app.db.is_ghost_protected.return_value = False
        app.get_mapping_for_a.return_value = ("test_m1", Path("/a_root"), Path("/b_root"))
        app.db.b_fingerprint_exists.return_value = False
        svc = SyncService(app)
        with patch.object(svc, "copy_a_record_to_b", return_value=True) as mock_copy:
            result = svc.copy_a_record_to_b_if_needed("/a/f.strm", "/m/f.mp4", "/m")
        mock_copy.assert_called_once_with("/a/f.strm", "/m/f.mp4", "/m", mapping_id="test_m1")
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
        app.get_mapping_for_a.return_value = ("test_m1", Path("/a_root"), Path("/b_root"))

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
        app.get_mapping_for_a.return_value = ("test_m1", Path("/a_root"), Path("/b_root"))

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
        app.get_mapping_for_a.return_value = ("test_m1", Path("/a_root"), Path("/b_root"))

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
        app.get_mapping_for_a.return_value = ("test_m1", Path("/a_root"), Path("/b_root"))

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
        app.get_mapping_for_a.return_value = ("test_m1", Path("/a_root"), Path("/b_root"))

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
        app.get_mapping_for_a.return_value = ("test_m1", Path("/a_root"), Path("/b_root"))

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
        app.get_mapping_for_a.return_value = ("test_m1", Path("/a_root"), Path("/b_root"))

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
        app.get_mapping_for_a.return_value = ("test_m1", Path("/a_root"), Path("/b_root"))

        svc = SyncService(app)
        result = svc.copy_a_record_to_b(str(a_file), "/mount/file.mp4", "/mount")

        assert result is False


# ===========================================================================
# TestSyncServiceSyncOneRecord
# ===========================================================================


class TestSyncServiceSyncOneRecord:
    """Tests for _sync_one_record() helper method."""

    def test_sync_one_record_skip_missing(self, tmp_path):
        """Skip when source file does not exist."""
        app = _make_app(tmp_path)
        svc = SyncService(app)
        svc._cache_ghost = set()
        svc._cache_b_fp = set()

        rec = _make_a_record("/nonexistent/file.strm", "/m/f.mp4", "/m")
        conn = Mock()
        result = svc._sync_one_record(rec, None, conn)

        assert result == "skip_missing"

    def test_sync_one_record_skip_filtered(self, tmp_path):
        """Skip when path is not in valid_engine_paths."""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        a_file = a_root / "file.strm"
        a_file.write_text("/other/file.mp4", encoding="utf-8")

        svc = SyncService(app)
        svc._cache_ghost = set()
        svc._cache_b_fp = set()

        rec = _make_a_record(str(a_file), "/other/file.mp4", "/other")
        conn = Mock()
        result = svc._sync_one_record(rec, ["/engine"], conn)

        assert result == "skip_filtered"

    def test_sync_one_record_skip_ghost(self, tmp_path):
        """Skip when path is ghost protected."""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        a_file = a_root / "file.strm"
        a_file.write_text("/m/file.mp4", encoding="utf-8")

        svc = SyncService(app)
        svc._cache_ghost = {"/m/file.mp4"}
        svc._cache_b_fp = set()

        rec = _make_a_record(str(a_file), "/m/file.mp4", "/m")
        conn = Mock()
        result = svc._sync_one_record(rec, None, conn)

        assert result == "skip_ghost"

    def test_sync_one_record_skip_fingerprint(self, tmp_path):
        """Skip when fingerprint already exists in B."""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        a_file = a_root / "file.strm"
        a_file.write_text("/m/file.mp4", encoding="utf-8")
        app.get_mapping_for_a.return_value = ("test_m1", a_root, tmp_path / "b")

        svc = SyncService(app)
        svc._cache_ghost = set()
        # Pre-populate with a known (mapping_id, fingerprint) compound key
        from utils import make_strm_fingerprint
        fp = make_strm_fingerprint("/m/file.mp4")
        svc._cache_b_fp = {("test_m1", fp)}

        rec = _make_a_record(str(a_file), "/m/file.mp4", "/m")
        conn = Mock()
        result = svc._sync_one_record(rec, None, conn, mapping_id="test_m1")

        assert result == "skip_fp"

    def _make_db_conn_mock(self):
        """Create a mock sqlite3 connection with proper cursor behavior."""
        mock_conn = Mock()
        mock_cursor = Mock()
        mock_cursor.fetchone.return_value = None
        mock_conn.execute.return_value = mock_cursor
        return mock_conn

    def test_sync_one_record_success_new_file(self, tmp_path):
        """Copy and write new file to B zone."""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        a_file = a_root / "file.strm"
        a_file.write_text("/m/file.mp4", encoding="utf-8")

        b_root = tmp_path / "b"
        b_root.mkdir()
        app.build_b_path_from_a.return_value = b_root / "file.strm"
        app.get_mapping_for_a.return_value = ("test_m1", a_root, b_root)

        svc = SyncService(app)
        svc._cache_ghost = set()
        svc._cache_b_fp = set()

        conn = self._make_db_conn_mock()
        rec = _make_a_record(str(a_file), "/m/file.mp4", "/m")
        result = svc._sync_one_record(rec, None, conn, mapping_id="test_m1")

        assert result == "success"
        assert (b_root / "file.strm").exists()
        # _cache_b_fp should contain the computed fingerprint
        from utils import make_strm_fingerprint
        fp = make_strm_fingerprint("/m/file.mp4")
        assert ("test_m1", fp) in svc._cache_b_fp
        # DB upserts should have been called on conn
        assert conn.execute.call_count > 0

    def test_sync_one_record_success_existing_b(self, tmp_path):
        """B file already exists with same content - just update DB."""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        webdav = "/m/file.mp4"
        a_file = a_root / "file.strm"
        a_file.write_text(webdav, encoding="utf-8")

        b_root = tmp_path / "b"
        b_root.mkdir()
        b_file = b_root / "file.strm"
        b_file.write_text(webdav, encoding="utf-8")
        app.build_b_path_from_a.return_value = b_file
        app.get_mapping_for_a.return_value = ("test_m1", a_root, b_root)

        svc = SyncService(app)
        svc._cache_ghost = set()
        svc._cache_b_fp = set()

        conn = self._make_db_conn_mock()

        rec = _make_a_record(str(a_file), webdav, "/m")
        result = svc._sync_one_record(rec, None, conn, mapping_id="test_m1")

        assert result == "success"
        # The computed fingerprint should be in cache
        from utils import make_strm_fingerprint
        fp = make_strm_fingerprint(webdav)
        assert ("test_m1", fp) in svc._cache_b_fp
        # No new file copy should have happened
        conn.execute.assert_called()

    def test_sync_one_record_fail_build_b_path(self, tmp_path):
        """Fail when build_b_path_from_a raises ValueError."""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        a_file = a_root / "file.strm"
        a_file.write_text("/m/file.mp4", encoding="utf-8")
        app.build_b_path_from_a.side_effect = ValueError("not under root")

        svc = SyncService(app)
        svc._cache_ghost = set()
        svc._cache_b_fp = set()

        conn = Mock()
        rec = _make_a_record(str(a_file), "/m/file.mp4", "/m")
        result = svc._sync_one_record(rec, None, conn)

        assert result == "fail"

    def test_sync_one_record_fail_copy_error(self, tmp_path):
        """Fail when file copy fails."""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        a_file = a_root / "file.strm"
        a_file.write_text("/m/file.mp4", encoding="utf-8")

        b_root = tmp_path / "b"
        app.build_b_path_from_a.return_value = b_root / "file.strm"

        svc = SyncService(app)
        svc._cache_ghost = set()
        svc._cache_b_fp = set()

        conn = Mock()
        rec = _make_a_record(str(a_file), "/m/file.mp4", "/m")

        with patch("shutil.copyfile", side_effect=OSError("disk full")):
            result = svc._sync_one_record(rec, None, conn)

        assert result == "fail"

    def test_sync_one_record_fail_db_error_rolls_back_file(self, tmp_path):
        """DB error after copy should delete copied file."""
        app = _make_app(tmp_path)
        a_root = app.a_roots[0]
        a_file = a_root / "file.strm"
        a_file.write_text("/m/file.mp4", encoding="utf-8")

        b_root = tmp_path / "b"
        b_file = b_root / "file.strm"
        app.build_b_path_from_a.return_value = b_file

        svc = SyncService(app)
        svc._cache_ghost = set()
        svc._cache_b_fp = set()

        conn = Mock()
        # First execute for get_all_ghost_protected is not called here
        # but we need to simulate _bulk_upsert_b failing
        conn.execute.side_effect = [None, None, None, Exception("db error")]

        rec = _make_a_record(str(a_file), "/m/file.mp4", "/m")
        result = svc._sync_one_record(rec, None, conn)

        assert result == "fail"
        assert not b_file.exists()


# ===========================================================================
# TestSyncServiceBulkUpsertHelpers
# ===========================================================================


class TestSyncServiceBulkUpsertHelpers:
    """Tests for _bulk_upsert_b and _bulk_upsert_identity helper methods."""

    def test_bulk_upsert_b_inserts_new_record(self, tmp_path):
        """Insert a new B record with FTS."""
        app = _make_app(tmp_path)
        svc = SyncService(app)

        conn = Mock()
        # No existing record
        conn.execute.return_value.fetchone.side_effect = [None, (1,)]

        svc._bulk_upsert_b(
            conn,
            local_path="/b/file.strm",
            webdav_path="/m/file.mp4",
            parent_webdav_path="/m",
            source_a_path="/a/file.strm",
            fingerprint="abc123",
            mapping_id="test_m1",
        )

        # Should have: 1 SELECT old row (None), 1 INSERT base,
        # 1 SELECT new rowid, 1 INSERT FTS
        calls = conn.execute.call_args_list
        assert len(calls) >= 3
        # First call should be SELECT to check for existing row
        assert "SELECT" in calls[0][0][0] and "b_strm_files" in calls[0][0][0]
        # Should insert FTS for new record
        fts_insert_calls = [c for c in calls if "INSERT INTO b_strm_files_fts" in c[0][0]]
        assert len(fts_insert_calls) >= 1, "Should insert FTS for new record"

    def test_bulk_upsert_b_replaces_existing_record(self, tmp_path):
        """Update an existing B record when fields change and update FTS."""
        app = _make_app(tmp_path)
        svc = SyncService(app)

        conn = Mock()
        # Existing record with old webdav_path (will trigger change detection)
        conn.execute.return_value.fetchone.side_effect = [
            (42, "/m/old.mp4", "/m", "/a/file.strm", "abc123", "test_m1"),  # old row
            (42,),  # rowid after UPDATE (same rowid, no change)
        ]

        svc._bulk_upsert_b(
            conn,
            local_path="/b/file.strm",
            webdav_path="/m/file.mp4",  # Changed from /m/old.mp4
            parent_webdav_path="/m",
            source_a_path="/a/file.strm",
            fingerprint="abc123",
            mapping_id="test_m1",
        )

        calls = conn.execute.call_args_list
        # Should delete old FTS row and insert new one (webdav_path changed)
        delete_calls = [c for c in calls if "DELETE FROM b_strm_files_fts" in c[0][0]]
        insert_calls = [c for c in calls if "INSERT INTO b_strm_files_fts" in c[0][0]]
        assert len(delete_calls) >= 1, "Should delete old FTS row when webdav_path changes"
        assert len(insert_calls) >= 1, "Should insert new FTS row when webdav_path changes"

    def test_bulk_upsert_identity(self, tmp_path):
        """Insert identity record via bulk helper."""
        app = _make_app(tmp_path)
        svc = SyncService(app)

        conn = Mock()

        svc._bulk_upsert_identity(
            conn,
            fingerprint="abc123",
            webdav_path="/m/file.mp4",
            source_a_path="/a/file.strm",
            current_b_path="/b/file.strm",
        )

        conn.execute.assert_called_once()
        call_args = conn.execute.call_args[0]
        assert "INSERT OR REPLACE INTO strm_identity" in call_args[0]
        # Verify parameters include fingerprint, webdav_path, etc.
        params = call_args[1]
        assert params[0] == "abc123"
        assert params[1] == "/m/file.mp4"
        assert params[2] == "/a/file.strm"
        assert params[3] == "/b/file.strm"


# ===========================================================================
# TestBulkUpsertTimestampSemantics
# 时间语义测试：_upsert_a_batch_bulk 与 _bulk_upsert_b（真实 Database）
# ===========================================================================


class TestBulkUpsertTimestampSemantics:
    """验证 bulk 路径的两个 upsert 实现只在业务字段变化时更新 updated_at，
    并保留 _bulk_upsert_b 的既有 status（不 SET status）。"""

    @staticmethod
    def _new_db() -> Database:
        tmpdir = tempfile.TemporaryDirectory()
        db = Database(str(Path(tmpdir.name) / "test.db"))
        return db, tmpdir  # 调用方持有 tmpdir 防止清理

    @staticmethod
    def _query_a_updated_at(db: Database, local_path: str):
        with db.read_connection() as conn:
            return conn.execute(
                "SELECT webdav_path, parent_webdav_path, updated_at FROM a_strm_files WHERE local_path = ?",
                (local_path,),
            ).fetchone()

    @staticmethod
    def _query_b(db: Database, local_path: str):
        with db.read_connection() as conn:
            return conn.execute(
                "SELECT webdav_path, parent_webdav_path, source_a_path, fingerprint, "
                "status, updated_at, mapping_id FROM b_strm_files WHERE local_path = ?",
                (local_path,),
            ).fetchone()

    @staticmethod
    def _sleep():
        time.sleep(0.005)

    # === _upsert_a_batch_bulk ===

    def test_upsert_a_batch_bulk_unchanged_keeps_updated_at(self):
        db, tmpdir = self._new_db()
        try:
            svc = SyncService(_make_app(Path(tmpdir.name)))
            with db.bulk_connection() as conn:
                svc._upsert_a_batch_bulk(conn, [("/a/1.strm", "/m/1.mp4", "/m")])
            first = self._query_a_updated_at(db, "/a/1.strm")
            assert first is not None
            self._sleep()
            with db.bulk_connection() as conn:
                svc._upsert_a_batch_bulk(conn, [("/a/1.strm", "/m/1.mp4", "/m")])
            second = self._query_a_updated_at(db, "/a/1.strm")
            assert second[2] == first[2], "无变化时应保留 updated_at"
        finally:
            tmpdir.cleanup()

    def test_upsert_a_batch_bulk_changed_updates_updated_at(self):
        db, tmpdir = self._new_db()
        try:
            svc = SyncService(_make_app(Path(tmpdir.name)))
            with db.bulk_connection() as conn:
                svc._upsert_a_batch_bulk(conn, [("/a/1.strm", "/m/1.mp4", "/m")])
            first = self._query_a_updated_at(db, "/a/1.strm")
            self._sleep()
            with db.bulk_connection() as conn:
                svc._upsert_a_batch_bulk(conn, [("/a/1.strm", "/m/1b.mp4", "/m")])
            second = self._query_a_updated_at(db, "/a/1.strm")
            assert second[2] > first[2], "webdav_path 变化时应更新 updated_at"
            assert second[0] == "/m/1b.mp4"
        finally:
            tmpdir.cleanup()

    def test_upsert_a_batch_bulk_returns_processed_count(self):
        db, tmpdir = self._new_db()
        try:
            svc = SyncService(_make_app(Path(tmpdir.name)))
            with db.bulk_connection() as conn:
                n = svc._upsert_a_batch_bulk(conn, [("/a/1.strm", "/m/1.mp4", "/m"),
                                                     ("/a/2.strm", "/m/2.mp4", "/m")])
            assert n == 2
            self._sleep()
            with db.bulk_connection() as conn:
                n2 = svc._upsert_a_batch_bulk(conn, [("/a/1.strm", "/m/1.mp4", "/m"),
                                                      ("/a/2.strm", "/m/2.mp4", "/m")])
            assert n2 == 2, "返回值不得改为变化条数"
        finally:
            tmpdir.cleanup()

    def test_upsert_a_batch_bulk_empty_returns_zero(self):
        db, tmpdir = self._new_db()
        try:
            svc = SyncService(_make_app(Path(tmpdir.name)))
            with db.bulk_connection() as conn:
                n = svc._upsert_a_batch_bulk(conn, [])
            assert n == 0
        finally:
            tmpdir.cleanup()

    # === _bulk_upsert_b ===

    def test_bulk_upsert_b_unchanged_keeps_updated_at(self):
        db, tmpdir = self._new_db()
        try:
            svc = SyncService(_make_app(Path(tmpdir.name)))
            with db.bulk_connection() as conn:
                svc._bulk_upsert_b(conn, "/b/1.strm", "/m/1.mp4", "/m",
                                   "/a/1.strm", "fp1", "m1")
            first = self._query_b(db, "/b/1.strm")
            assert first is not None
            assert first[4] == "valid"
            self._sleep()
            with db.bulk_connection() as conn:
                svc._bulk_upsert_b(conn, "/b/1.strm", "/m/1.mp4", "/m",
                                   "/a/1.strm", "fp1", "m1")
            second = self._query_b(db, "/b/1.strm")
            assert second[5] == first[5], "无变化时应保留 updated_at"
        finally:
            tmpdir.cleanup()

    def test_bulk_upsert_b_changed_updates_updated_at(self):
        db, tmpdir = self._new_db()
        try:
            svc = SyncService(_make_app(Path(tmpdir.name)))
            with db.bulk_connection() as conn:
                svc._bulk_upsert_b(conn, "/b/1.strm", "/m/1.mp4", "/m",
                                   "/a/1.strm", "fp1", "m1")
            first = self._query_b(db, "/b/1.strm")
            self._sleep()
            with db.bulk_connection() as conn:
                svc._bulk_upsert_b(conn, "/b/1.strm", "/m/1b.mp4", "/m",
                                   "/a/1.strm", "fp1", "m1")
            second = self._query_b(db, "/b/1.strm")
            assert second[5] > first[5], "业务字段变化时应更新 updated_at"
        finally:
            tmpdir.cleanup()

    def test_bulk_upsert_b_preserves_duplicate_status(self):
        """命中既有 duplicate 行时 status 不被改回 valid（ON CONFLICT 不 SET status）。"""
        db, tmpdir = self._new_db()
        try:
            svc = SyncService(_make_app(Path(tmpdir.name)))
            # 先用 upsert_b 写入一条 duplicate 记录
            db.upsert_b("/b/1.strm", "/m/1.mp4", "/m", "/a/1.strm", "m1", "fp1", "duplicate")
            assert self._query_b(db, "/b/1.strm")[4] == "duplicate"
            # _bulk_upsert_b 用相同业务字段再次写入（A→B 同步路径）
            with db.bulk_connection() as conn:
                svc._bulk_upsert_b(conn, "/b/1.strm", "/m/1.mp4", "/m",
                                   "/a/1.strm", "fp1", "m1")
            row = self._query_b(db, "/b/1.strm")
            # status 必须仍为 duplicate，不得被改回 valid
            assert row[4] == "duplicate", "_bulk_upsert_b 不得把既有 duplicate 改回 valid"
        finally:
            tmpdir.cleanup()

    def test_bulk_upsert_b_preserves_quarantined_status(self):
        """命中既有 quarantined 行时 status 不被改回 valid。"""
        db, tmpdir = self._new_db()
        try:
            svc = SyncService(_make_app(Path(tmpdir.name)))
            db.upsert_b("/b/2.strm", "/m/2.mp4", "/m", "/a/2.strm", "m1", "fp2", "quarantined")
            with db.bulk_connection() as conn:
                svc._bulk_upsert_b(conn, "/b/2.strm", "/m/2.mp4", "/m",
                                   "/a/2.strm", "fp2", "m1")
            row = self._query_b(db, "/b/2.strm")
            assert row[4] == "quarantined", "_bulk_upsert_b 不得把既有 quarantined 改回 valid"
        finally:
            tmpdir.cleanup()

    def test_bulk_upsert_b_mapping_id_required(self):
        db, tmpdir = self._new_db()
        try:
            svc = SyncService(_make_app(Path(tmpdir.name)))
            with db.bulk_connection() as conn:
                with pytest.raises(ValueError):
                    svc._bulk_upsert_b(conn, "/b/1.strm", "/m/1.mp4", "/m",
                                       "/a/1.strm", "fp1", "")
        finally:
            tmpdir.cleanup()

    def test_bulk_upsert_b_no_duplicate_fts(self):
        db, tmpdir = self._new_db()
        try:
            svc = SyncService(_make_app(Path(tmpdir.name)))
            with db.bulk_connection() as conn:
                svc._bulk_upsert_b(conn, "/b/1.strm", "/m/1.mp4", "/m",
                                   "/a/1.strm", "fp1", "m1")
            self._sleep()
            with db.bulk_connection() as conn:
                svc._bulk_upsert_b(conn, "/b/1.strm", "/m/1.mp4", "/m",
                                   "/a/1.strm", "fp1", "m1")
            with db.read_connection() as conn:
                main = conn.execute("SELECT COUNT(*) FROM b_strm_files").fetchone()[0]
                fts = conn.execute("SELECT COUNT(*) FROM b_strm_files_fts").fetchone()[0]
            assert main == 1 and fts == 1
        finally:
            tmpdir.cleanup()

    def test_bulk_upsert_b_webdav_change_fts_swaps(self):
        db, tmpdir = self._new_db()
        try:
            svc = SyncService(_make_app(Path(tmpdir.name)))
            with db.bulk_connection() as conn:
                svc._bulk_upsert_b(conn, "/b/1.strm", "/m/old.mp4", "/m",
                                   "/a/1.strm", "fp1", "m1")
            self._sleep()
            with db.bulk_connection() as conn:
                svc._bulk_upsert_b(conn, "/b/1.strm", "/m/new.mp4", "/m",
                                   "/a/1.strm", "fp1", "m1")
            with db.read_connection() as conn:
                row = conn.execute("SELECT rowid FROM b_strm_files WHERE local_path = ?", ("/b/1.strm",)).fetchone()
                rid = row[0]
                new_hit = conn.execute(
                    "SELECT rowid FROM b_strm_files_fts WHERE rowid = ? AND webdav_path MATCH 'new'",
                    (rid,),
                ).fetchone()
                old_hit = conn.execute(
                    "SELECT rowid FROM b_strm_files_fts WHERE rowid = ? AND webdav_path MATCH 'old'",
                    (rid,),
                ).fetchone()
            assert new_hit is not None and old_hit is None
        finally:
            tmpdir.cleanup()
