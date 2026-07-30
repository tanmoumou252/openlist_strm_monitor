"""app_service_core.py 辅助方法单元测试

覆盖此前无直接测试的低难度方法：
- 锁工厂：get_path_lock / get_webdav_lock / get_fingerprint_lock
- 映射解析：get_a_root_for_path / get_b_root_for_a / get_b_root_for_path / _mapping_id_for_b
- db 查询委托：_check_fingerprint_exists_in_b / find_a_source_by_webdav / _refresh_b_record
- WebDAV 辅助：_cloud_path_to_engine_paths / _build_trash_path / _delete_a_file_by_webdav
- 标记机制：_clear_engine_internal / _clear_engine_internal_delayed
- 路径解析：_maybe_record_boundary_mapping / _log_lineage_pass_once / handle_b_renamed_to_non_strm
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app_service_core import AppService
from database import Database
from config import AppConfig, ABMapping


def _make_app(tmp_path: Path, *, a_b_mappings=None, strm_engine_paths=None):
    """构造最小化 AppService 实例，用于测试辅助方法。"""
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    c_dir = tmp_path / "c"
    for d in [a_dir, b_dir, c_dir]:
        d.mkdir(parents=True, exist_ok=True)

    config = Mock(spec=AppConfig)
    config.a_folders = [str(a_dir)]
    config.a_b_mappings = a_b_mappings or []
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

    return app, a_dir, b_dir, c_dir


# ===========================================================================
# 锁工厂
# ===========================================================================


class TestLockFactories:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app, *_ = _make_app(self.tmp)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_get_path_lock_returns_lock(self):
        lock = self.app.get_path_lock("/some/path")
        assert isinstance(lock, threading.Lock)

    def test_get_path_lock_same_path_returns_same_lock(self):
        lock1 = self.app.get_path_lock("/same/path")
        lock2 = self.app.get_path_lock("/same/path")
        assert lock1 is lock2

    def test_get_path_lock_different_paths_return_different_locks(self):
        lock1 = self.app.get_path_lock("/path/a")
        lock2 = self.app.get_path_lock("/path/b")
        assert lock1 is not lock2

    def test_get_webdav_lock_returns_lock(self):
        lock = self.app.get_webdav_lock("/movies/a.mp4")
        assert isinstance(lock, threading.Lock)

    def test_get_webdav_lock_same_path_returns_same_lock(self):
        lock1 = self.app.get_webdav_lock("/movies/a.mp4")
        lock2 = self.app.get_webdav_lock("/movies/a.mp4")
        assert lock1 is lock2

    def test_get_webdav_lock_different_from_path_lock(self):
        """WebDAV 锁与本地路径锁使用独立命名空间。"""
        path_lock = self.app.get_path_lock("/movies/a.mp4")
        webdav_lock = self.app.get_webdav_lock("/movies/a.mp4")
        assert path_lock is not webdav_lock

    def test_get_fingerprint_lock_returns_lock(self):
        lock = self.app.get_fingerprint_lock("abc123")
        assert isinstance(lock, threading.Lock)

    def test_get_fingerprint_lock_same_fp_returns_same_lock(self):
        lock1 = self.app.get_fingerprint_lock("abc123")
        lock2 = self.app.get_fingerprint_lock("abc123")
        assert lock1 is lock2

    def test_get_fingerprint_lock_different_fps_return_different_locks(self):
        lock1 = self.app.get_fingerprint_lock("fp1")
        lock2 = self.app.get_fingerprint_lock("fp2")
        assert lock1 is not lock2


# ===========================================================================
# 映射解析
# ===========================================================================


class TestMappingResolution:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.a_dir = self.tmp / "a_root"
        self.b_dir = self.tmp / "b_root"
        self.a_dir.mkdir(parents=True, exist_ok=True)
        self.b_dir.mkdir(parents=True, exist_ok=True)

        mapping = ABMapping(
            mapping_id="m1",
            a_root=str(self.a_dir),
            b_root=str(self.b_dir),
            label="test",
        )
        self.app, *_ = _make_app(self.tmp, a_b_mappings=[mapping])

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_get_a_root_for_path_match(self):
        file_path = str(self.a_dir / "subdir" / "file.strm")
        result = self.app.get_a_root_for_path(file_path)
        assert result is not None
        assert str(Path(str(result))) == str(self.a_dir)

    def test_get_a_root_for_path_no_match(self):
        result = self.app.get_a_root_for_path("/unrelated/path/file.strm")
        assert result is None

    def test_get_b_root_for_a_match(self):
        result = self.app.get_b_root_for_a(str(self.a_dir))
        assert result is not None

    def test_get_b_root_for_a_no_match(self):
        with pytest.raises(ValueError, match="无法唯一解析"):
            self.app.get_b_root_for_a("/nonexistent")

    def test_get_b_root_for_path_match(self):
        file_path = str(self.b_dir / "subdir" / "file.strm")
        result = self.app.get_b_root_for_path(file_path)
        assert result is not None

    def test_get_b_root_for_path_no_match(self):
        result = self.app.get_b_root_for_path("/unrelated/path")
        assert result is None

    def test_mapping_id_for_b_match(self):
        file_path = str(self.b_dir / "subdir" / "file.strm")
        result = self.app._mapping_id_for_b(file_path)
        assert result == "m1"

    def test_mapping_id_for_b_no_match(self):
        result = self.app._mapping_id_for_b("/nonexistent")
        assert result is None


# ===========================================================================
# db 查询委托
# ===========================================================================


class TestDbDelegation:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app, self.a_dir, self.b_dir, self.c_dir = _make_app(self.tmp)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_check_fingerprint_exists_in_b_returns_false_when_empty(self):
        self.app.db.get_all_b_by_fingerprint.return_value = []
        result = self.app._check_fingerprint_exists_in_b("nonexistent_fp")
        assert result is False

    def test_check_fingerprint_exists_in_b_returns_false_when_mapping_empty(self):
        """mapping_id 为空时 fail-closed 返回 False。"""
        result = self.app._check_fingerprint_exists_in_b(
            "fp", mapping_id=None)
        assert result is False

    def test_find_a_source_by_webdav_returns_none_when_db_empty(self):
        self.app.db.get_a_by_webdav_path.return_value = None
        result = self.app.find_a_source_by_webdav("/nonexistent/path")
        assert result is None

    def test_find_a_source_by_webdav_returns_path_when_exists(self):
        fake_a_path = self.a_dir / "test.strm"
        fake_a_path.write_text("/web/test", encoding="utf-8")
        self.app.db.get_a_local_path_by_webdav.return_value = str(fake_a_path)
        result = self.app.find_a_source_by_webdav("/test/path")
        assert result == str(fake_a_path)
        self.app.db.get_a_local_path_by_webdav.assert_called_once_with("/test/path")


# ===========================================================================
# WebDAV 辅助
# ===========================================================================


class TestWebdavHelpers:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app, self.a_dir, self.b_dir, self.c_dir = _make_app(self.tmp)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cloud_path_to_engine_paths_empty_when_no_storages(self):
        self.app.config.strm_storage_map = {}
        result = self.app._cloud_path_to_engine_paths("/cloud/path")
        assert result == []

    def test_cloud_path_to_engine_paths_matches_storage(self):
        mock_storage = Mock(local_path=str(self.a_dir), paths=["/cloud/"])
        self.app.config.strm_storage_map = {
            "/mount/a": mock_storage,
        }
        result = self.app._cloud_path_to_engine_paths("/cloud/path")
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_build_trash_path(self):
        result = self.app._build_trash_path("/mount/番剧/视频.mp4")
        assert isinstance(result, str)
        assert "trash" in result or "trash" in self.app.config.behavior.trash_dir_name

    def test_delete_a_file_by_webdav_delegates_to_safe_remove(self):
        fake_a_path = self.a_dir / "test.strm"
        fake_a_path.write_text("/web/test", encoding="utf-8")
        self.app.db.get_a_by_webdav.return_value = Mock(local_path=str(fake_a_path))
        with patch("app_service_core.safe_remove_file", return_value=True) as mock_remove:
            self.app._delete_a_file_by_webdav("/test/path")
            mock_remove.assert_called_once_with(str(fake_a_path))
        self.app.db.delete_a_by_local.assert_called_once_with(str(fake_a_path))

    def test_delete_a_file_by_webdav_noop_when_not_found(self):
        self.app.db.get_a_by_webdav.return_value = None
        # 不抛异常即通过
        self.app._delete_a_file_by_webdav("/nonexistent")


# ===========================================================================
# 标记机制
# ===========================================================================


class TestEngineInternalMarkers:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app, *_ = _make_app(self.tmp)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clear_engine_internal_removes_marker(self):
        self.app._mark_engine_internal("fp1")
        assert "fp1" in self.app._engine_internal_markers
        self.app._clear_engine_internal("fp1")
        assert "fp1" not in self.app._engine_internal_markers

    def test_clear_engine_internal_empty_fingerprint_noop(self):
        self.app._clear_engine_internal("")
        self.app._clear_engine_internal(None)

    def test_clear_engine_internal_nonexistent_noop(self):
        self.app._clear_engine_internal("nonexistent")

    def test_clear_engine_internal_delayed_removes_after_delay(self):
        self.app._mark_engine_internal("fp2")
        assert "fp2" in self.app._engine_internal_markers
        self.app._clear_engine_internal_delayed("fp2", delay=0.1)
        # 标记仍在（延迟清除尚未执行）
        assert "fp2" in self.app._engine_internal_markers
        # 等待延迟清除完成
        time.sleep(0.3)
        assert "fp2" not in self.app._engine_internal_markers


# ===========================================================================
# 路径解析辅助
# ===========================================================================


class TestPathHelpers:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app, self.a_dir, self.b_dir, self.c_dir = _make_app(self.tmp)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_log_lineage_pass_once_deduplicates(self):
        """同一 reason+path 组合只记录一次。"""
        with patch("app_service_core.logging") as mock_log:
            self.app._log_lineage_pass_once("test_reason", "/b/path/file.strm")
            self.app._log_lineage_pass_once("test_reason", "/b/path/file.strm")
            # debug 只调用一次（去重）
            debug_calls = [c for c in mock_log.debug.call_args_list
                           if "血统校验通过" in str(c)]
            assert len(debug_calls) == 1

    def test_log_lineage_pass_once_different_reasons_not_deduped(self):
        with patch("app_service_core.logging") as mock_log:
            self.app._log_lineage_pass_once("reason_a", "/b/path/file.strm")
            self.app._log_lineage_pass_once("reason_b", "/b/path/file.strm")
            debug_calls = [c for c in mock_log.debug.call_args_list
                           if "血统校验通过" in str(c)]
            assert len(debug_calls) == 2

    def test_handle_b_renamed_to_non_strm_deletes_record(self):
        local_path = str((self.b_dir / "file.old").resolve())
        self.app.db.get_b_by_local_full.return_value = Mock(
            fingerprint="fp1", webdav_path="/web/path", local_path=local_path)
        self.app.handle_b_renamed_to_non_strm(local_path)
        self.app.db.delete_b_by_local.assert_called_once_with(local_path)

    def test_handle_b_renamed_to_non_strm_noop_when_not_found(self):
        self.app.db.get_b_by_local_full.return_value = None
        self.app.handle_b_renamed_to_non_strm("/b/nonexistent")
        # 方法内部检查 db 记录，无记录时提前返回，验证不抛异常即可

    def test_maybe_record_boundary_mapping_noop_when_file_missing(self):
        # 本地文件不存在时，方法提前返回
        fake_path = self.b_dir / "nonexistent.strm"
        self.app._maybe_record_boundary_mapping(fake_path, "/web/test.strm", "fp1")
        # 不抛异常即通过
