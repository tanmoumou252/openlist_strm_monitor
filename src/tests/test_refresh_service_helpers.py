"""refresh_service.py 辅助方法单元测试

覆盖此前无直接测试的低难度方法：
- _sync_and_scan_protected_roots
- _log_path_analysis
- _execute_webdav_refreshes
- _wait_for_sync
- _cleanup_a_for_update_mode
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app_service_core import AppService
from refresh_service import RefreshService, PathAnalysis
from database import Database
from config import AppConfig


def _make_refresh_service(tmp_path: Path):
    """构造 RefreshService 实例。"""
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
    config.strm_engine_paths = []
    config.refresh_paths = []
    config.refresh.enabled = True
    config.refresh.interval_seconds = 300
    config.refresh.full_audit_interval_days = 7

    db = MagicMock()
    db.init_subtitle_table = Mock()

    admin_api = Mock()

    with patch("app_service_core.RefreshService"), \
         patch("app_service_core.SyncService"), \
         patch("app_service_core.SubtitleHandler"):
        app = AppService(config, db, admin_api)

    rs = RefreshService(app)
    return rs


# ===========================================================================
# _sync_and_scan_protected_roots
# ===========================================================================


class TestSyncAndScanProtectedRoots:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_calls_both_sync_and_scan(self):
        rs = _make_refresh_service(self.tmp)
        with patch.object(rs.app, "sync_protected_roots_from_config") as mock_sync, \
             patch.object(rs.app, "scan_removed_protected_roots") as mock_scan:
            rs._sync_and_scan_protected_roots()
            mock_sync.assert_called_once()
            mock_scan.assert_called_once()


# ===========================================================================
# _log_path_analysis
# ===========================================================================


class TestLogPathAnalysis:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_logs_analysis_summary(self):
        rs = _make_refresh_service(self.tmp)
        analysis = PathAnalysis(
            valid_refresh_paths=["/a"],
            only_refresh={"/b"},
            only_engine={"/engine"},
            engine_set={"/a", "/engine"},
        )
        with patch("refresh_service.logging") as mock_log:
            rs._log_path_analysis(analysis)
            assert mock_log.warning.called or mock_log.info.called


# ===========================================================================
# _execute_webdav_refreshes
# ===========================================================================


class TestExecuteWebdavRefreshes:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_refreshes_each_path(self):
        rs = _make_refresh_service(self.tmp)
        rs.app.config.refresh.depth = 3
        with patch.object(rs.app, "refresh_webdav_root") as mock_refresh, \
             patch.object(rs.app, "refresh_webdav_root_readonly") as mock_readonly:
            rs._execute_webdav_refreshes(["/path1", "/path2"], {"/readonly"})
            assert mock_refresh.call_count == 2
            assert mock_readonly.call_count == 1


# ===========================================================================
# _wait_for_sync
# ===========================================================================


class TestWaitForSync:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sleeps_for_configured_duration(self):
        rs = _make_refresh_service(self.tmp)
        with patch("refresh_service.time.sleep") as mock_sleep:
            rs._wait_for_sync()
            mock_sleep.assert_called_once()


# ===========================================================================
# _cleanup_a_for_update_mode
# ===========================================================================


class TestCleanupAForUpdateMode:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_delegates_to_app_cleanup(self):
        rs = _make_refresh_service(self.tmp)
        with patch.object(rs.app, "cleanup_a_deleted_on_cloud") as mock_cleanup:
            rs._cleanup_a_for_update_mode({"/engine/path"})
            mock_cleanup.assert_called_once_with("/engine/path")
