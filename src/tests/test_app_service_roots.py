"""app_service_core.py 保护根目录与快照方法单元测试

覆盖此前无直接测试的低难度方法：
- sync_protected_roots_from_config
- scan_removed_protected_roots
- persist_current_roots_snapshot
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app_service_core import AppService
from database import Database
from config import AppConfig


def _make_app(tmp_path: Path, *, strm_engine_paths=None):
    """构造最小化 AppService 实例。"""
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    c_dir = tmp_path / "c"
    for d in [a_dir, b_dir, c_dir]:
        d.mkdir(parents=True, exist_ok=True)

    config = Mock(spec=AppConfig)
    config.a_folders = [str(a_dir)]
    config.a_b_mappings = []
    config.paths = Mock()
    config.paths.b_root = str(b_dir)
    config.paths.c_root = str(c_dir)
    config.behavior = Mock()
    config.behavior.ghost_protect_seconds = 300
    config.behavior.trash_dir_name = "trash"
    config.strm_engine_paths = strm_engine_paths or []

    db = MagicMock()
    db.init_subtitle_table = Mock()

    admin_api = Mock()

    with patch("app_service_core.RefreshService"), \
         patch("app_service_core.SyncService"), \
         patch("app_service_core.SubtitleHandler"):
        app = AppService(config, db, admin_api)

    return app


# ===========================================================================
# sync_protected_roots_from_config
# ===========================================================================


class TestSyncProtectedRootsFromConfig:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_calls_db_replace_with_engine_paths(self):
        app = _make_app(self.tmp, strm_engine_paths=["/mount/strm"])
        app.sync_protected_roots_from_config()
        app.db.replace_protected_roots.assert_called_once()
        call_args = app.db.replace_protected_roots.call_args[0][0]
        assert len(call_args) == 1
        assert call_args[0][0] == "/mount/strm"

    def test_empty_engine_paths_calls_db_with_empty(self):
        app = _make_app(self.tmp, strm_engine_paths=[])
        app.sync_protected_roots_from_config()
        app.db.replace_protected_roots.assert_called_once_with([])

    def test_multiple_engine_paths(self):
        app = _make_app(self.tmp, strm_engine_paths=["/m1", "/m2"])
        app.sync_protected_roots_from_config()
        call_args = app.db.replace_protected_roots.call_args[0][0]
        assert len(call_args) == 2


# ===========================================================================
# scan_removed_protected_roots
# ===========================================================================


class TestScanRemovedProtectedRoots:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_removed_roots(self):
        app = _make_app(self.tmp)
        app.db.get_protected_root_paths.return_value = {"/root1"}
        app.db.get_protected_roots_snapshot_paths.return_value = {"/root1"}
        with patch.object(app, "migrate_b_under_root_to_c") as mock_migrate:
            app.scan_removed_protected_roots()
            mock_migrate.assert_not_called()

    def test_removed_root_triggers_migration(self):
        app = _make_app(self.tmp)
        app.db.get_protected_root_paths.return_value = set()
        app.db.get_protected_roots_snapshot_paths.return_value = {"/removed_root"}
        with patch.object(app, "migrate_b_under_root_to_c") as mock_migrate:
            app.scan_removed_protected_roots()
            mock_migrate.assert_called_once_with("/removed_root")

    def test_multiple_removed_roots(self):
        app = _make_app(self.tmp)
        app.db.get_protected_root_paths.return_value = set()
        app.db.get_protected_roots_snapshot_paths.return_value = {"/r1", "/r2"}
        with patch.object(app, "migrate_b_under_root_to_c") as mock_migrate:
            app.scan_removed_protected_roots()
            assert mock_migrate.call_count == 2


# ===========================================================================
# persist_current_roots_snapshot
# ===========================================================================


class TestPersistCurrentRootsSnapshot:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_persists_active_roots(self):
        app = _make_app(self.tmp)
        mock_record = Mock(root_path="/root1", trash_path="/trash1", active=True)
        app.db.get_protected_roots.return_value = [mock_record]
        app.persist_current_roots_snapshot()
        app.db.save_protected_roots_snapshot.assert_called_once()
        saved_roots = app.db.save_protected_roots_snapshot.call_args[0][0]
        assert len(saved_roots) == 1
        assert saved_roots[0] == ("/root1", "/trash1")

    def test_skips_inactive_roots(self):
        app = _make_app(self.tmp)
        active = Mock(root_path="/active", trash_path="/t1", active=True)
        inactive = Mock(root_path="/inactive", trash_path="/t2", active=False)
        app.db.get_protected_roots.return_value = [active, inactive]
        app.persist_current_roots_snapshot()
        saved_roots = app.db.save_protected_roots_snapshot.call_args[0][0]
        assert len(saved_roots) == 1
        assert saved_roots[0][0] == "/active"

    def test_filters_by_valid_engine_paths(self):
        app = _make_app(self.tmp)
        r1 = Mock(root_path="/r1", trash_path="/t1", active=True)
        r2 = Mock(root_path="/r2", trash_path="/t2", active=True)
        app.db.get_protected_roots.return_value = [r1, r2]
        app.persist_current_roots_snapshot(valid_engine_paths=["/r1"])
        saved_roots = app.db.save_protected_roots_snapshot.call_args[0][0]
        assert len(saved_roots) == 1
        assert saved_roots[0][0] == "/r1"

    def test_empty_roots(self):
        app = _make_app(self.tmp)
        app.db.get_protected_roots.return_value = []
        app.persist_current_roots_snapshot()
        app.db.save_protected_roots_snapshot.assert_called_once_with([])
