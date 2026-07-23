"""
app_service_core.py 扩展单元测试

测试核心业务逻辑（含原 src/test_app_service_core.py 迁移内容）：
- StrmStorageInfo / StrmStorageManager
- AppService 路径验证、血统检查
- B 区文件处理 (handle_b_created_or_modified, handle_b_deleted)
- ghost 保护机制
- build_b_path_from_a 路径构建
- _perform_webdav_action
- migrate_b_under_root_to_c
- cleanup_b_redundant
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

# Add src/ to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app_service_core import AppService, StrmStorageManager, StrmStorageInfo
from database import Database, BRecord
from config import AppConfig


# ===========================================================================
# TestStrmStorageInfo  (migrated from src/test_app_service_core.py)
# ===========================================================================


class TestStrmStorageInfo:
    def test_is_working_true(self):
        info = StrmStorageInfo(1, "/test", "work", [], "update")
        assert info.is_working is True

    def test_is_working_false(self):
        info = StrmStorageInfo(2, "/test", "error", [], "update")
        assert info.is_working is False

    def test_is_sync_mode_update(self):
        info = StrmStorageInfo(1, "/test", "work", [], "update")
        assert info.is_sync_mode is True

    def test_is_sync_mode_case_insensitive(self):
        info = StrmStorageInfo(1, "/test", "work", [], "UPDATE")
        assert info.is_sync_mode is True

    def test_is_sync_mode_other_mode(self):
        info = StrmStorageInfo(2, "/test", "work", [], "sync")
        assert info.is_sync_mode is False


# ===========================================================================
# TestStrmStorageManager  (migrated + extended)
# ===========================================================================


class TestStrmStorageManager:
    def setup_method(self):
        self.mock_client = Mock()
        self.manager = StrmStorageManager(self.mock_client)

    def test_extract_paths_from_addition_json_list(self):
        addition = '{"paths": ["/path1", "/path2"]}'
        paths = self.manager._extract_paths_from_addition(addition)
        assert paths == ["/path1", "/path2"]

    def test_extract_paths_from_addition_json_string_newlines(self):
        addition = '{"paths": "/p1\\n/p2\\n/p3"}'
        paths = self.manager._extract_paths_from_addition(addition)
        assert len(paths) == 3
        assert paths[0] == "/p1"

    def test_extract_paths_from_addition_empty(self):
        assert self.manager._extract_paths_from_addition("") == []

    def test_extract_paths_from_addition_invalid_json(self):
        assert self.manager._extract_paths_from_addition("NOT JSON") == []

    def test_extract_save_local_mode(self):
        addition = '{"SaveLocalMode": "update"}'
        mode = self.manager._extract_save_local_mode(addition)
        assert mode == "update"

    def test_get_strm_storages_returns_full_info(self):
        # get_strm_storages_full_info 已经按 driver 过滤，返回扁平 STRM 列表（含完整 addition）
        self.mock_client.get_strm_storages_full_info.return_value = [
            {"id": 1, "mount_path": "/s1", "driver": "Strm",
             "addition": '{"SaveLocalMode":"update"}', "status": "work"},
        ]
        storages = self.manager.get_strm_storages()
        assert len(storages) == 1
        assert storages[0].mount_path == "/s1"

    def test_get_working_sync_storages(self):
        # get_strm_storages_full_info returns flat list of STRM storages with full addition
        self.mock_client.get_strm_storages_full_info.return_value = [
            {"id": 1, "mount_path": "/ok", "driver": "Strm", "status": "work",
             "addition": '{"SaveLocalMode":"update"}'},
            {"id": 2, "mount_path": "/err", "driver": "Strm", "status": "error",
             "addition": '{"SaveLocalMode":"update"}'},
            {"id": 3, "mount_path": "/nosync", "driver": "Strm", "status": "work",
             "addition": '{"SaveLocalMode":"sync"}'},
        ]
        storages = self.manager.get_working_sync_storages()
        assert len(storages) == 1
        assert storages[0].mount_path == "/ok"

    def test_get_strm_storages_empty_list(self):
        self.mock_client.get_strm_storages_full_info.return_value = []
        assert self.manager.get_strm_storages() == []

    def test_get_strm_storages_no_response(self):
        self.mock_client.get_strm_storages_full_info.return_value = None
        assert self.manager.get_strm_storages() == []


# ===========================================================================
# TestAppServicePathValidation  (migrated)
# ===========================================================================


class TestAppServicePathValidation:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = os.path.join(self.tmp, "a")
        self.b_dir = os.path.join(self.tmp, "b")
        self.c_dir = os.path.join(self.tmp, "c")
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            os.makedirs(d)

        config = Mock(spec=AppConfig)
        config.a_folders = [self.a_dir]
        config.paths = Mock()
        config.paths.b_root = self.b_dir
        config.paths.c_root = self.c_dir
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_is_path_under_any_root_match(self):
        assert self.app.is_path_under_any_root("/test/subdir", ["/test"]) is True

    def test_is_path_under_any_root_exact(self):
        assert self.app.is_path_under_any_root("/test", ["/test"]) is True

    def test_is_path_under_any_root_no_match(self):
        assert self.app.is_path_under_any_root("/other", ["/test"]) is False

    def test_is_path_under_any_root_empty_path(self):
        assert self.app.is_path_under_any_root("", ["/test"]) is False

    def test_is_path_under_any_root_slash_path(self):
        assert self.app.is_path_under_any_root("/", ["/test"]) is False

    def test_is_valid_refresh_root_in_engine_paths(self):
        self.app.config.strm_engine_paths = ["/e1"]
        assert self.app.is_valid_refresh_root("/e1/subdir") is True

    def test_is_valid_refresh_root_not_in_engine_paths(self):
        self.app.config.strm_engine_paths = ["/e1"]
        assert self.app.is_valid_refresh_root("/other") is False

    def test_is_valid_refresh_root_empty_engine_paths(self):
        self.app.config.strm_engine_paths = []
        assert self.app.is_valid_refresh_root("/any/path") is True


# ===========================================================================
# TestGhostProtection  (migrated)
# ===========================================================================


class TestGhostProtection:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        a_dir = os.path.join(self.tmp, "a")
        os.makedirs(a_dir)

        config = Mock(spec=AppConfig)
        config.a_folders = [a_dir]
        config.paths = Mock()
        config.paths.b_root = os.path.join(self.tmp, "b")
        config.paths.c_root = os.path.join(self.tmp, "c")
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, Mock())
        self.a_dir = a_dir

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ghost_protection_blocks_sync(self):
        webdav_path = "/test/file.mp4"
        self.app.db.is_ghost_protected.return_value = True
        a_file = os.path.join(self.a_dir, "file.strm")
        Path(a_file).write_text(webdav_path, encoding="utf-8")
        self.app.admin_api.check_exists.return_value = True

        # When ghost-protected, we expect the db to be checked
        self.app.db.upsert_a = Mock()
        self.app.db.save_known_folder = Mock()
        self.app.db.upsert_identity = Mock()
        self.app.db.get_identity_by_fingerprint = Mock(return_value=None)

        with patch.object(self.app, "build_b_path_from_a") as mock_build:
            self.app.handle_a_created_or_modified(a_file)
            # Ghost protection should block the A->B copy entirely
            mock_build.assert_not_called()
        self.app.db.is_ghost_protected.assert_called_with(webdav_path)

    def test_ghost_not_protected_proceeds(self):
        webdav_path = "/test/file.mp4"
        self.app.db.is_ghost_protected.return_value = False
        a_file = os.path.join(self.a_dir, "file.strm")
        Path(a_file).write_text(webdav_path, encoding="utf-8")
        self.app.admin_api.check_exists.return_value = True
        self.app.db.upsert_a = Mock()
        self.app.db.save_known_folder = Mock()
        self.app.db.upsert_identity = Mock()
        self.app.db.get_identity_by_fingerprint = Mock(return_value=None)
        self.app.db.get_valid_b_instance_by_fingerprint = Mock(return_value=None)

        self.app.handle_a_created_or_modified(a_file)
        self.app.db.is_ghost_protected.assert_called_with(webdav_path)


# ===========================================================================
# TestBuildBPathFromA
# ===========================================================================


class TestBuildBPathFromA:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_movie_direct_mapping(self):
        """Movie files map directly under b_root."""
        a_file = self.a_dir / "Movie Name" / "Movie Name.strm"
        a_file.parent.mkdir(parents=True, exist_ok=True)
        a_file.write_text("/mount/Movie Name.mp4", encoding="utf-8")

        with patch.object(self.app, "_should_treat_as_movie", return_value=True):
            result = self.app.build_b_path_from_a(str(a_file))

        # Movie: b_root / rel_path
        rel = a_file.resolve().relative_to(self.a_dir)
        assert result == self.b_dir / rel

    def test_file_not_under_a_root_raises(self):
        """File outside any a_root raises ValueError."""
        with pytest.raises(ValueError, match="不属于任何A根目录"):
            self.app.build_b_path_from_a("/completely/different/path.strm")

    def test_anime_with_season_dir_adds_season_layer(self):
        """Anime with S01E01 in name gets Season XX folder inserted."""
        season_dir = self.a_dir / "Show Name" / "Season 1"
        season_dir.mkdir(parents=True, exist_ok=True)
        a_file = season_dir / "Show.Name.S01E01.strm"
        a_file.write_text("/mount/show/S01E01.mp4", encoding="utf-8")

        with patch.object(self.app, "_should_treat_as_movie", return_value=False):
            with patch("app_service_core.suggest_rename", return_value="Show.Name.S01E01.strm"):
                with patch("app_service_core._extract_season_episode", return_value=(1, 1)):
                    with patch("app_service_core.extract_season_from_path", return_value=1):
                        result = self.app.build_b_path_from_a(
                            str(a_file), "/mount/show/S01E01.mp4")

        # Should have Season 01 in path
        assert "Season" in str(result)

    def test_no_season_episode_falls_back_to_original_rel(self):
        """No season/episode info → just remap under b_root."""
        a_file = self.a_dir / "Some Movie" / "Some Movie.strm"
        a_file.parent.mkdir(parents=True, exist_ok=True)
        a_file.write_text("/mount/movie.mp4", encoding="utf-8")

        with patch.object(self.app, "_should_treat_as_movie", return_value=False):
            with patch("app_service_core.suggest_rename", return_value=None):
                result = self.app.build_b_path_from_a(str(a_file))

        rel = a_file.resolve().relative_to(self.a_dir)
        assert result == self.b_dir / rel


# ===========================================================================
# TestPerformWebdavAction
# ===========================================================================


class TestPerformWebdavAction:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.behavior.action = "DELETE"
        config.behavior.trash_dir_name = ".trash"
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()
        self.admin_api = Mock()

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, self.admin_api)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_delete_action_success(self):
        self.app.config.behavior.action = "DELETE"
        self.admin_api.remove.return_value = True
        result = self.app._perform_webdav_action("/mount/file.mp4")
        assert result is True
        self.admin_api.remove.assert_called_once_with("/mount/file.mp4")

    def test_delete_action_failure(self):
        self.app.config.behavior.action = "DELETE"
        self.admin_api.remove.return_value = False
        result = self.app._perform_webdav_action("/mount/file.mp4")
        assert result is False

    def test_move_action_success(self):
        self.app.config.behavior.action = "MOVE"
        self.admin_api.move.return_value = True
        with patch.object(self.app, "_build_trash_path", return_value="/mount/.trash/file.mp4"), \
             patch.object(self.app, "_ensure_trash_dirs", return_value=True):
            result = self.app._perform_webdav_action("/mount/file.mp4")
        assert result is True
        self.admin_api.move.assert_called_once()

    def test_move_action_trash_path_build_fail(self):
        self.app.config.behavior.action = "MOVE"
        with patch.object(self.app, "_build_trash_path", return_value=None):
            result = self.app._perform_webdav_action("/mount/file.mp4")
        assert result is False
        self.admin_api.move.assert_not_called()

    def test_move_action_ensure_trash_dirs_fail(self):
        self.app.config.behavior.action = "MOVE"
        with patch.object(self.app, "_build_trash_path", return_value="/mount/.trash/file.mp4"), \
             patch.object(self.app, "_ensure_trash_dirs", return_value=False):
            result = self.app._perform_webdav_action("/mount/file.mp4")
        assert result is False


# ===========================================================================
# TestHandleBDeleted
# ===========================================================================


class TestHandleBDeleted:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.behavior.action = "DELETE"
        config.behavior.a_to_b_restore_delay_seconds = 30
        config.behavior.trash_dir_name = ".trash"
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()
        self.db = db
        self.admin_api = Mock()

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, self.admin_api)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_b_record(self, local_path: str, webdav_path: str = "/mount/f.mp4") -> BRecord:
        return BRecord(
            local_path=local_path,
            webdav_path=webdav_path,
            parent_webdav_path="/mount",
            source_a_path=None,
            fingerprint="fp_abc",
            status="valid",
            updated_at=0,
        )

    def test_delete_no_db_record_noop(self):
        """No DB record: handle_b_deleted silently returns."""
        self.db.get_b_by_local_full.return_value = None
        b_file = self.b_dir / "ghost.strm"
        self.app.handle_b_deleted(str(b_file))
        self.db.delete_b_by_local.assert_not_called()

    def test_delete_with_webdav_deletion(self):
        """Normal deletion — B file removed, WebDAV deletion triggered."""
        b_file = self.b_dir / "file.strm"
        row = self._make_b_record(str(b_file))
        self.db.get_b_by_local_full.return_value = row
        self.db.has_other_b_instance.return_value = False
        self.db.get_b_instances_by_fingerprint.return_value = []
        self.db.set_ghost_protection = Mock()
        self.db.get_a_by_webdav.return_value = None
        self.admin_api.remove.return_value = True
        self.db.get_identity_by_fingerprint.return_value = None
        self.db.get_all_b_by_fingerprint.return_value = []

        with patch.object(self.app, "_execute_webdav_deletion", return_value=True) as mock_del:
            self.app.handle_b_deleted(str(b_file))

        mock_del.assert_called_once_with("/mount/f.mp4", "/mount")
        self.db.delete_b_by_local.assert_called()

    def test_delete_skips_webdav_when_other_b_instance(self):
        """Other B instance exists — WebDAV deletion skipped, DB record removed."""
        b_file = self.b_dir / "dup.strm"
        row = self._make_b_record(str(b_file))
        self.db.get_b_by_local_full.return_value = row
        self.db.has_other_b_instance.return_value = True

        with patch.object(self.app, "_execute_webdav_deletion") as mock_del:
            self.app.handle_b_deleted(str(b_file))

        mock_del.assert_not_called()
        self.db.delete_b_by_local.assert_called()

    def test_delete_during_restore_skips(self):
        """If fingerprint is in restoring_markers, deletion is skipped."""
        b_file = self.b_dir / "restoring.strm"
        row = self._make_b_record(str(b_file))
        self.db.get_b_by_local_full.return_value = row
        # Inject the fingerprint into restoring markers
        self.app._restoring_markers.add("fp_abc")

        with patch.object(self.app, "_execute_webdav_deletion") as mock_del:
            self.app.handle_b_deleted(str(b_file))

        mock_del.assert_not_called()
        self.db.delete_b_by_local.assert_not_called()


# ===========================================================================
# TestHandleBCreatedOrModified
# ===========================================================================


class TestHandleBCreatedOrModified:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()
        self.db = db
        self.admin_api = Mock()

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, self.admin_api)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_nonexistent_b_file_is_noop(self):
        """If B file doesn't exist, handler returns immediately."""
        self.app.handle_b_created_or_modified(str(self.b_dir / "ghost.strm"))
        self.db.get_b_by_local_full.assert_not_called()

    def test_unparseable_strm_quarantine(self):
        """STRM that cannot be parsed → quarantined to .invalid."""
        b_file = self.b_dir / "bad.strm"
        b_file.write_text("NOT A VALID PATH", encoding="utf-8")
        self.db.get_b_by_local_full.return_value = None

        self.app.handle_b_created_or_modified(str(b_file))

        # File should have been quarantined
        assert not b_file.exists()
        assert any(p.suffix == ".invalid" for p in self.b_dir.iterdir())

    def test_new_b_file_without_a_source_deleted(self):
        """New B file with valid webdav_path but no A source → deleted."""
        webdav_path = "/mount/orphan.mp4"
        b_file = self.b_dir / "orphan.strm"
        b_file.write_text(webdav_path, encoding="utf-8")
        self.db.get_b_by_local_full.return_value = None
        # Lineage check passes (but A source check fails)
        with patch.object(self.app, "_verify_b_path_lineage", return_value=True):
            with patch.object(self.app, "_verify_a_source_exists", return_value=False):
                self.app.handle_b_created_or_modified(str(b_file))

        assert not b_file.exists()
        self.db.delete_b_by_local.assert_not_called()  # row was None so delete not called

    def test_b_file_lineage_fail_triggers_restore(self):
        """B file that fails lineage → _restore_b_from_a_after_violation called."""
        webdav_path = "/mount/bad.mp4"
        b_file = self.b_dir / "bad.strm"
        b_file.write_text(webdav_path, encoding="utf-8")
        self.db.get_b_by_local_full.return_value = None

        with patch.object(self.app, "_verify_b_path_lineage", return_value=False):
            with patch.object(self.app, "_restore_b_from_a_after_violation") as mock_restore:
                self.app.handle_b_created_or_modified(str(b_file))

        mock_restore.assert_called_once()


# ===========================================================================
# TestMigrateBUnderRootToC
# ===========================================================================


class TestMigrateBUnderRootToC:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()
        self.db = db

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_migrate_existing_files_to_c(self):
        """Existing B files get moved to C directory."""
        b_file = self.b_dir / "show" / "ep01.strm"
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text("/mount/show/ep01.mp4", encoding="utf-8")

        record = BRecord(
            local_path=str(b_file),
            webdav_path="/mount/show/ep01.mp4",
            parent_webdav_path="/mount/show",
            source_a_path=None,
            fingerprint=None,
            status="valid",
            updated_at=0,
        )
        self.db.get_b_under_root.return_value = [record]
        self.db.upsert_c = Mock()
        self.db.delete_b_by_local = Mock()
        self.db.get_identity_by_fingerprint = Mock(return_value=None)
        self.db.get_all_b_by_fingerprint = Mock(return_value=[])

        self.app.migrate_b_under_root_to_c("/mount/show")

        # File should have been moved
        assert not b_file.exists()
        self.db.upsert_c.assert_called_once()
        self.db.delete_b_by_local.assert_called_once()

    def test_migrate_nonexistent_files_skipped(self):
        """B records pointing to missing files → DB record deleted only."""
        record = BRecord(
            local_path=str(self.b_dir / "missing.strm"),
            webdav_path="/mount/missing.mp4",
            parent_webdav_path="/mount",
            source_a_path=None,
            fingerprint=None,
            status="valid",
            updated_at=0,
        )
        self.db.get_b_under_root.return_value = [record]
        self.db.delete_b_by_local = Mock()
        self.db.upsert_c = Mock()

        self.app.migrate_b_under_root_to_c("/mount")

        self.db.delete_b_by_local.assert_called_once()
        self.db.upsert_c.assert_not_called()


# ===========================================================================
# TestCleanupBRedundant
# ===========================================================================


class TestCleanupBRedundant:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()
        self.db = db
        self.admin_api = Mock()

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, self.admin_api)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cleanup_removes_keyword_suffixed_files(self):
        """Files with .duplicate/.quarantined/.invalid extensions are deleted."""
        bad1 = self.b_dir / "old.strm.duplicate"
        bad2 = self.b_dir / "old.strm.invalid"
        bad1.write_text("x")
        bad2.write_text("x")

        # No regular b records so it ends early
        self.db.get_all_b_records.return_value = []

        self.app.cleanup_b_redundant()

        assert not bad1.exists()
        assert not bad2.exists()

    def test_cleanup_skips_ghost_protected_records(self):
        """Ghost-protected records are not migrated or deleted."""
        a_file = self.a_dir / "keep.strm"
        a_file.write_text("/mount/f.mp4", encoding="utf-8")
        b_file = self.b_dir / "keep.strm"
        b_file.write_text("/mount/f.mp4", encoding="utf-8")

        record = BRecord(
            local_path=str(b_file),
            webdav_path="/mount/f.mp4",
            parent_webdav_path="/mount",
            source_a_path=str(a_file),
            fingerprint="fp_x",
            status="valid",
            updated_at=0,
        )
        self.db.get_all_b_records.return_value = [record]
        self.db.is_ghost_protected.return_value = True

        self.app.cleanup_b_redundant()

        # Ghost-protected: should not be deleted
        assert b_file.exists()

    def test_cleanup_migrates_a_source_deleted(self):
        """When A source is deleted and WebDAV also gone → B file migrated to C."""
        b_file = self.b_dir / "orphan.strm"
        b_file.write_text("/mount/gone.mp4", encoding="utf-8")

        record = BRecord(
            local_path=str(b_file),
            webdav_path="/mount/gone.mp4",
            parent_webdav_path="/mount",
            source_a_path="/a/nonexistent.strm",  # doesn't exist
            fingerprint="fp_gone",
            status="valid",
            updated_at=0,
        )
        self.db.get_all_b_records.return_value = [record]
        self.db.is_ghost_protected.return_value = False
        # No alt source either
        self.db.get_a_local_path_by_webdav.return_value = None
        # WebDAV doesn't exist
        self.admin_api.check_exists.return_value = False
        self.db.upsert_c = Mock()
        self.db.delete_b_by_local = Mock()
        self.db.get_identity_by_fingerprint = Mock(return_value=None)
        self.db.get_all_b_by_fingerprint = Mock(return_value=[])

        self.app.cleanup_b_redundant()

        # b_file should have been moved to C
        assert not b_file.exists()
        self.db.upsert_c.assert_called_once()
        self.db.delete_b_by_local.assert_called()

    def test_cleanup_removes_if_webdav_not_exists(self):
        """A source exists but WebDAV doesn't → B file removed."""
        a_file = self.a_dir / "src.strm"
        a_file.write_text("/mount/src.mp4", encoding="utf-8")
        b_file = self.b_dir / "src.strm"
        b_file.write_text("/mount/src.mp4", encoding="utf-8")

        record = BRecord(
            local_path=str(b_file),
            webdav_path="/mount/src.mp4",
            parent_webdav_path="/mount",
            source_a_path=str(a_file),
            fingerprint="fp_src",
            status="valid",
            updated_at=0,
        )
        self.db.get_all_b_records.return_value = [record]
        self.db.is_ghost_protected.return_value = False
        self.admin_api.check_exists.return_value = False  # WebDAV gone
        self.db.delete_b_by_local = Mock()
        self.db.get_identity_by_fingerprint = Mock(return_value=None)
        self.db.get_all_b_by_fingerprint = Mock(return_value=[])

        self.app.cleanup_b_redundant()

        assert not b_file.exists()
        self.db.delete_b_by_local.assert_called()


# ===========================================================================
# TestVerifyBPathLineage
# ===========================================================================


class TestVerifyBPathLineage:
    """Test the 9-step lineage verification chain."""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = ["/engine"]

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()
        self.db = db

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _setup_a_source(self, a_file: Path, webdav_path: str):
        """Create A source file and mock DB to return it."""
        a_file.parent.mkdir(parents=True, exist_ok=True)
        a_file.write_text(webdav_path, encoding="utf-8")
        from database import ARecord
        self.db.get_a_by_webdav.return_value = ARecord(
            local_path=str(a_file),
            webdav_path=webdav_path,
            parent_webdav_path="/mount",
            updated_at=0,
        )

    def test_step1_no_a_source_fails(self):
        """Step 1: No A source record → lineage fails."""
        b_file = self.b_dir / "file.strm"
        b_file.write_text("/mount/file.mp4", encoding="utf-8")
        self.db.get_a_by_webdav.return_value = None
        self.db.get_identity_by_fingerprint.return_value = None

        result = self.app._verify_b_path_lineage(str(b_file), "/mount/file.mp4")
        assert result is False

    def test_step2_basic_lineage_pass(self):
        """Step 2: A/B directories match exactly → pass."""
        a_file = self.a_dir / "folder" / "file.strm"
        b_file = self.b_dir / "folder" / "file.strm"
        self._setup_a_source(a_file, "/mount/folder/file.mp4")
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text("/mount/folder/file.mp4", encoding="utf-8")

        result = self.app._verify_b_path_lineage(str(b_file), "/mount/folder/file.mp4")
        assert result is True

    def test_step3_season_layer_addition_pass(self):
        """Step 3: B adds Season layer → pass."""
        a_file = self.a_dir / "show" / "file.strm"
        b_file = self.b_dir / "show" / "Season 01" / "file.strm"
        self._setup_a_source(a_file, "/mount/show/file.mp4")
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text("/mount/show/file.mp4", encoding="utf-8")

        result = self.app._verify_b_path_lineage(str(b_file), "/mount/show/file.mp4")
        assert result is True

    def test_step5_no_engine_config_pass(self):
        """Step 5: No engine config → default pass."""
        a_file = self.a_dir / "folder" / "file.strm"
        b_file = self.b_dir / "folder" / "different" / "file.strm"
        self._setup_a_source(a_file, "/mount/folder/file.mp4")
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text("/mount/folder/file.mp4", encoding="utf-8")
        self.app.engine_configs = []  # No engine config

        result = self.app._verify_b_path_lineage(str(b_file), "/mount/folder/file.mp4")
        assert result is True

    def test_step6_boundary_files_check(self):
        """Step 6: Boundary files check with shallow paths."""
        a_file = self.a_dir / "file.strm"
        b_file = self.b_dir / "file.strm"
        self._setup_a_source(a_file, "/mount/file.mp4")
        b_file.write_text("/mount/file.mp4", encoding="utf-8")
        self.app.engine_configs = [
            {"a_root_norm": str(self.a_dir.resolve()), "mount_path": "/mount", "source_paths": ["/engine"]}
        ]

        # Both b_parts and rel_parts are shallow (< 2 parts)
        result = self.app._verify_b_path_lineage(str(b_file), "/mount/file.mp4")
        assert result is True

    def test_step7_boundary_mapping_match(self):
        """Step 7: Boundary mapping matches → pass."""
        a_file = self.a_dir / "show" / "file.strm"
        b_file = self.b_dir / "different_show" / "Season 01" / "file.strm"
        self._setup_a_source(a_file, "/engine/show/ep.mp4")
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text("/engine/show/ep.mp4", encoding="utf-8")
        self.app.engine_configs = [
            {"a_root_norm": str(self.a_dir.resolve()), "mount_path": "/mount", "source_paths": ["/engine"]}
        ]
        from database import BoundaryRecord
        self.db.get_media_boundary_by_fingerprint.return_value = BoundaryRecord(
            fingerprint="fp",
            source_media_name="show",
            current_media_name="different_show",
            engine_entry_path=str(self.b_dir),
            updated_at=0,
        )

        result = self.app._verify_b_path_lineage(str(b_file), "/engine/show/ep.mp4")
        assert result is True

    def test_step8_sync_phase_boundary_record(self):
        """Step 8: Sync phase records boundary mapping → pass."""
        a_file = self.a_dir / "show" / "file.strm"
        b_file = self.b_dir / "different_show" / "Season 01" / "file.strm"
        self._setup_a_source(a_file, "/engine/show/ep.mp4")
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text("/engine/show/ep.mp4", encoding="utf-8")
        self.app.engine_configs = [
            {"a_root_norm": str(self.a_dir.resolve()), "mount_path": "/mount", "source_paths": ["/engine"]}
        ]
        self.db.get_media_boundary_by_fingerprint.return_value = None
        self.db.get_media_boundary_by_source_name_only.return_value = None
        self.db.get_media_boundary_by_current_name.return_value = None

        result = self.app._verify_b_path_lineage(
            str(b_file), "/engine/show/ep.mp4", is_sync_phase=True)
        assert result is True
        self.db.upsert_media_boundary.assert_called()

    def test_step9_solo_episode_check(self):
        """Step 9: Solo episode check → pass."""
        a_file = self.a_dir / "show" / "file.strm"
        b_file = self.b_dir / "different_show" / "Season 01" / "file.strm"
        self._setup_a_source(a_file, "/engine/show/ep.mp4")
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text("/engine/show/ep.mp4", encoding="utf-8")
        self.app.engine_configs = [
            {"a_root_norm": str(self.a_dir.resolve()), "mount_path": "/mount", "source_paths": ["/engine"]}
        ]
        self.db.get_media_boundary_by_fingerprint.return_value = None
        self.db.get_media_boundary_by_source_name_only.return_value = None
        self.db.get_media_boundary_by_current_name.return_value = None
        self.db.get_a_count_under_root.return_value = 1  # Solo episode

        result = self.app._verify_b_path_lineage(
            str(b_file), "/engine/show/ep.mp4", is_sync_phase=False)
        assert result is True


# ===========================================================================
# TestInitialScanB
# ===========================================================================


class TestInitialScanB:
    """Test initial_scan_b disk/DB reconciliation."""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()
        self.db = db

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_b_empty_directory(self):
        """Empty B directory → no DB operations."""
        self.db.get_all_b_records.return_value = []
        self.app.initial_scan_b()
        self.db.upsert_b.assert_not_called()

    def test_scan_b_no_db_records_inserts_new(self):
        """B has files but DB is empty → insert new records."""
        b_file = self.b_dir / "file.strm"
        b_file.write_text("/mount/file.mp4", encoding="utf-8")
        from utils import make_strm_fingerprint
        fp = make_strm_fingerprint("/mount/file.mp4")
        self.db.get_all_b_records.return_value = []
        self.db.get_identity_by_fingerprint.return_value = None
        self.db.get_a_local_path_by_webdav.return_value = None
        # 模拟 upsert_b 后 DB 已含新记录的真实联动：返回与磁盘文件匹配的 valid 实例
        new_record = BRecord(
            local_path=str(b_file),
            webdav_path="/mount/file.mp4",
            parent_webdav_path="/mount",
            source_a_path=None,
            fingerprint=fp,
            status="valid",
            updated_at=0,
        )
        self.db.get_all_b_by_fingerprint.return_value = [new_record]
        # ensure_single_visible_instance 会用 mark_other_b_instances_duplicate 的返回值迭代
        self.db.mark_other_b_instances_duplicate.return_value = []

        with patch.object(self.app, "_verify_b_path_lineage", return_value=True):
            self.app.initial_scan_b()

        self.db.upsert_b.assert_called_once()
        self.db.upsert_identity.assert_called_once()

    def test_scan_b_db_record_matches_disk(self):
        """DB record matches disk file → no changes."""
        b_file = self.b_dir / "file.strm"
        b_file.write_text("/mount/file.mp4", encoding="utf-8")
        from utils import make_strm_fingerprint
        fp = make_strm_fingerprint("/mount/file.mp4")
        record = BRecord(
            local_path=str(b_file),
            webdav_path="/mount/file.mp4",
            parent_webdav_path="/mount",
            source_a_path=None,
            fingerprint=fp,
            status="valid",
            updated_at=0,
        )
        self.db.get_all_b_records.return_value = [record]

        with patch.object(self.app, "_verify_b_path_lineage", return_value=True):
            self.app.initial_scan_b()

        self.db.upsert_b.assert_not_called()
        self.db.delete_b_by_local.assert_not_called()

    def test_scan_b_db_record_no_fingerprint_deleted(self):
        """DB record with no fingerprint → deleted."""
        record = BRecord(
            local_path=str(self.b_dir / "old.strm"),
            webdav_path="/mount/old.mp4",
            parent_webdav_path="/mount",
            source_a_path=None,
            fingerprint=None,
            status="valid",
            updated_at=0,
        )
        self.db.get_all_b_records.return_value = [record]

        self.app.initial_scan_b()

        self.db.delete_b_by_local.assert_called_once()


# ===========================================================================
# TestCleanupBZombiesUnderFolder
# ===========================================================================


class TestCleanupBZombiesUnderFolder:
    """Test cleanup_b_zombies_under_folder."""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()
        self.db = db
        self.admin_api = Mock()

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, self.admin_api)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cleanup_zombies_webdav_not_exists(self):
        """WebDAV doesn't exist → zombie file is cleaned up (batch API optimization)."""
        b_file = self.b_dir / "zombie.strm"
        b_file.write_text("/mount/zombie.mp4", encoding="utf-8")
        record = BRecord(
            local_path=str(b_file),
            webdav_path="/mount/zombie.mp4",
            parent_webdav_path="/mount",
            source_a_path=None,
            fingerprint="fp_zombie",
            status="valid",
            updated_at=0,
        )
        self.db.get_b_under_root.return_value = [record]
        # 新实现使用 list_directory 批量获取云端文件列表（而非逐条 check_exists）
        # 返回空列表表示云端没有该文件 → 判定为僵尸文件
        self.admin_api.list_directory.return_value = {"code": 0, "data": {"content": []}}
        self.db.get_b_by_local_full.return_value = record
        self.db.delete_b_by_local = Mock()
        self.db.set_ghost_protection = Mock()
        self.db.get_identity_by_fingerprint = Mock(return_value=None)
        self.db.get_all_b_by_fingerprint = Mock(return_value=[])

        self.app.cleanup_b_zombies_under_folder("/mount")

        assert not b_file.exists()
        self.db.delete_b_by_local.assert_called()
        self.db.set_ghost_protection.assert_called()

    def test_cleanup_zombies_webdav_exists_skipped(self):
        """WebDAV exists → zombie file is NOT cleaned up (batch API optimization)."""
        b_file = self.b_dir / "alive.strm"
        b_file.write_text("/mount/alive.mp4", encoding="utf-8")
        record = BRecord(
            local_path=str(b_file),
            webdav_path="/mount/alive.mp4",
            parent_webdav_path="/mount",
            source_a_path=None,
            fingerprint="fp_alive",
            status="valid",
            updated_at=0,
        )
        self.db.get_b_under_root.return_value = [record]
        # 新实现使用 list_directory 批量获取云端文件列表
        # 返回列表包含该文件 → 判定为存活文件，跳过清理
        self.admin_api.list_directory.return_value = {
            "code": 0,
            "data": {"content": [{"name": "alive.mp4", "is_dir": False}]},
        }

        self.app.cleanup_b_zombies_under_folder("/mount")

        assert b_file.exists()
        self.db.delete_b_by_local.assert_not_called()


# ===========================================================================
# TestHandleACreatedOrModified
# ===========================================================================


class TestHandleACreatedOrModified:
    """Test the full handle_a_created_or_modified path."""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()
        self.db = db
        self.admin_api = Mock()

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, self.admin_api)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_nonexistent_file_is_noop(self):
        """File doesn't exist → return immediately."""
        self.app.handle_a_created_or_modified(
            str(self.a_dir / "ghost.strm"))
        self.db.upsert_a.assert_not_called()

    def test_file_not_under_a_root_skipped(self):
        """File outside any A root → skipped."""
        outside = Path(self.tmp) / "outside.strm"
        outside.write_text("/mount/file.mp4", encoding="utf-8")
        self.app.handle_a_created_or_modified(str(outside))
        self.db.upsert_a.assert_not_called()

    def test_unparseable_strm_skipped(self):
        """STRM content cannot be parsed → upsert_a not called."""
        a_file = self.a_dir / "bad.strm"
        a_file.write_text("NOT A VALID PATH", encoding="utf-8")
        self.app.handle_a_created_or_modified(str(a_file))
        self.db.upsert_a.assert_not_called()

    def test_webdav_not_exists_cleans_up_a(self):
        """WebDAV source doesn't exist → A file deleted, ghost protection set."""
        webdav_path = "/mount/gone.mp4"
        a_file = self.a_dir / "gone.strm"
        a_file.write_text(webdav_path, encoding="utf-8")
        self.admin_api.check_exists.return_value = False
        self.db.set_ghost_protection = Mock()

        self.app.handle_a_created_or_modified(str(a_file))

        assert not a_file.exists()
        self.db.delete_a_by_local.assert_called_once()
        self.db.set_ghost_protection.assert_called_once()

    def test_ghost_protected_skips_copy(self):
        """Ghost protected → copy_a_record_to_b NOT called."""
        webdav_path = "/mount/guarded.mp4"
        a_file = self.a_dir / "guarded.strm"
        a_file.write_text(webdav_path, encoding="utf-8")
        self.admin_api.check_exists.return_value = True
        self.db.is_ghost_protected.return_value = True
        self.db.get_identity_by_fingerprint.return_value = None

        with patch.object(self.app, "copy_a_record_to_b") as mock_copy:
            self.app.handle_a_created_or_modified(str(a_file))

        mock_copy.assert_not_called()

    def test_existing_b_same_content_updates_record(self):
        """B file exists with same content → upsert_b, no copy."""
        webdav_path = "/mount/file.mp4"
        a_file = self.a_dir / "file.strm"
        a_file.write_text(webdav_path, encoding="utf-8")
        b_file = self.b_dir / "file.strm"
        b_file.write_text(webdav_path, encoding="utf-8")
        self.admin_api.check_exists.return_value = True
        self.db.is_ghost_protected.return_value = False
        self.db.get_identity_by_fingerprint.return_value = None
        self.db.get_valid_b_instance_by_fingerprint.return_value = None
        self.db.get_all_b_by_fingerprint.return_value = []

        with patch.object(self.app, "build_b_path_from_a", return_value=b_file):
            with patch.object(self.app, "copy_a_record_to_b") as mock_copy:
                self.app.handle_a_created_or_modified(str(a_file))

        mock_copy.assert_not_called()
        self.db.upsert_b.assert_called_once()

    def test_valid_b_instance_better_score_skips_copy(self):
        """Existing B instance has better score → new copy skipped."""
        webdav_path = "/mount/file.mp4"
        a_file = self.a_dir / "file.strm"
        a_file.write_text(webdav_path, encoding="utf-8")
        b_existing = self.b_dir / "better.strm"
        b_existing.write_text(webdav_path, encoding="utf-8")
        self.admin_api.check_exists.return_value = True
        self.db.is_ghost_protected.return_value = False
        self.db.get_identity_by_fingerprint.return_value = None
        existing_instance = Mock()
        existing_instance.local_path = str(b_existing)
        self.db.get_valid_b_instance_by_fingerprint.return_value = existing_instance

        from database import BRecord
        self.db.get_b_by_local_full.return_value = BRecord(
            local_path=str(b_existing), webdav_path=webdav_path,
            parent_webdav_path="/mount", source_a_path=None,
            fingerprint="fp", status="valid", updated_at=0)

        new_b = self.b_dir / "worse.strm"
        with patch.object(self.app, "build_b_path_from_a", return_value=new_b):
            with patch.object(self.app, "copy_a_record_to_b") as mock_copy:
                with patch.object(self.app, "_b_file_score") as mock_score:
                    mock_score.side_effect = [(1, 0, 10, "worse"), (0, 5, 20, "better")]
                    self.app.handle_a_created_or_modified(str(a_file))

        mock_copy.assert_not_called()

    def test_proceeds_to_copy_when_all_checks_pass(self):
        """All checks pass → copy_a_record_to_b called."""
        webdav_path = "/mount/new.mp4"
        a_file = self.a_dir / "new.strm"
        a_file.write_text(webdav_path, encoding="utf-8")
        self.admin_api.check_exists.return_value = True
        self.db.is_ghost_protected.return_value = False
        self.db.get_identity_by_fingerprint.return_value = None
        self.db.get_valid_b_instance_by_fingerprint.return_value = None
        b_target = self.b_dir / "new.strm"

        with patch.object(self.app, "build_b_path_from_a", return_value=b_target):
            with patch.object(self.app, "copy_a_record_to_b") as mock_copy:
                self.app.handle_a_created_or_modified(str(a_file))

        mock_copy.assert_called_once()


# ===========================================================================
# TestHandleADeleted
# ===========================================================================


class TestHandleADeleted:
    """Test handle_a_deleted."""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()
        self.db = db

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_file_still_exists_skips(self):
        """File still exists → skip (openlist sync delete+recreate)."""
        a_file = self.a_dir / "exists.strm"
        a_file.write_text("/mount/file.mp4", encoding="utf-8")
        self.app.handle_a_deleted(str(a_file))
        self.db.delete_a_by_local.assert_not_called()

    def test_delete_with_db_record(self):
        """File gone, DB has record → delete index, trigger cleanup."""
        from database import ARecord
        self.db.get_a_by_local.return_value = ARecord(
            local_path="/a/gone.strm",
            webdav_path="/mount/gone.mp4",
            parent_webdav_path="/mount",
            updated_at=0,
        )
        with patch.object(self.app, "trigger_delayed_cleanup") as mock_trigger:
            self.app.handle_a_deleted("/a/gone.strm")
        self.db.delete_a_by_local.assert_called_once()
        mock_trigger.assert_called_once_with("/mount")

    def test_delete_without_db_record(self):
        """File gone, no DB record → only delete index attempt."""
        self.db.get_a_by_local.return_value = None
        with patch.object(self.app, "trigger_delayed_cleanup") as mock_trigger:
            self.app.handle_a_deleted("/a/unknown.strm")
        self.db.delete_a_by_local.assert_called_once()
        mock_trigger.assert_not_called()


# ===========================================================================
# TestBFileScore
# ===========================================================================


class TestBFileScore:
    """Test _b_file_score scoring logic."""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()
        self.db = db

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_standard_name_lower_priority(self):
        """Standard name (S01E01) gets is_standard=0 → higher priority."""
        score = self.app._b_file_score(
            str(self.b_dir / "show" / "Season 01" / "S01E01.strm"))
        # is_standard=True → 0 (sorts first = higher priority)
        assert score[0] == 0

    def test_non_standard_name_higher_value(self):
        """Non-standard name gets is_standard=1 → lower priority."""
        score = self.app._b_file_score(
            str(self.b_dir / "random_file.strm"))
        assert score[0] == 1

    def test_is_standard_media_name_s01e01(self):
        assert self.app._is_standard_media_name("Show.S01E01.strm") is True

    def test_is_standard_media_name_1x1(self):
        assert self.app._is_standard_media_name("Show.1x1.strm") is True

    def test_is_standard_media_name_not_standard(self):
        assert self.app._is_standard_media_name("random_file.strm") is False


# ===========================================================================
# TestEnsureSingleVisibleInstance
# ===========================================================================


class TestEnsureSingleVisibleInstance:
    """Test ensure_single_visible_instance duplicate quarantine."""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()
        self.db = db

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_instances_is_noop(self):
        """No B instances → return immediately."""
        self.db.get_all_b_by_fingerprint.return_value = []
        self.app.ensure_single_visible_instance("fp", "/b/file.strm")
        self.db.mark_other_b_instances_duplicate.assert_not_called()

    def test_no_valid_files_skips(self):
        """All instances are non-valid or non-existent → skip."""
        record = Mock()
        record.status = "duplicate"
        record.local_path = str(self.b_dir / "dup.strm")
        self.db.get_all_b_by_fingerprint.return_value = [record]
        self.app.ensure_single_visible_instance("fp", str(self.b_dir / "file.strm"))
        self.db.mark_other_b_instances_duplicate.assert_not_called()

    def test_quarantines_duplicates(self):
        """Multiple valid files → keep best, quarantine others."""
        keep_file = self.b_dir / "show" / "Season 01" / "S01E01.strm"
        dup_file = self.b_dir / "misc" / "random.strm"
        keep_file.parent.mkdir(parents=True, exist_ok=True)
        dup_file.parent.mkdir(parents=True, exist_ok=True)
        keep_file.write_text("/mount/show/ep.mp4", encoding="utf-8")
        dup_file.write_text("/mount/show/ep.mp4", encoding="utf-8")

        keep_rec = Mock()
        keep_rec.status = "valid"
        keep_rec.local_path = str(keep_file)
        dup_rec = Mock()
        dup_rec.status = "valid"
        dup_rec.local_path = str(dup_file)
        self.db.get_all_b_by_fingerprint.return_value = [keep_rec, dup_rec]
        self.db.mark_other_b_instances_duplicate.return_value = [str(dup_file)]
        self.db.move_b_record = Mock(return_value=True)
        self.db.mark_b_instance_status = Mock()

        self.app.ensure_single_visible_instance("fp", str(keep_file))

        # Duplicate should have been quarantined (renamed to .duplicate)
        assert not dup_file.exists()
        self.db.mark_other_b_instances_duplicate.assert_called_once()


# ===========================================================================
# TestRestoreBFileFromA
# ===========================================================================


class TestRestoreBFileFromA:
    """Test restore_b_file_from_a."""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()
        self.db = db

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_restore_success(self):
        """A source exists → B file restored successfully."""
        webdav_path = "/mount/show/ep.mp4"
        a_file = self.a_dir / "show" / "ep.strm"
        a_file.parent.mkdir(parents=True, exist_ok=True)
        a_file.write_text(webdav_path, encoding="utf-8")
        b_target = self.b_dir / "show" / "ep.strm"

        with patch.object(self.app, "ensure_single_visible_instance"):
            result = self.app.restore_b_file_from_a(
                str(b_target), webdav_path, "/mount/show", str(a_file))

        assert result is True
        assert b_target.exists()

    def test_restore_no_a_source_fails(self):
        """No A source file → returns False."""
        self.db.get_a_local_path_by_webdav.return_value = None
        result = self.app.restore_b_file_from_a(
            str(self.b_dir / "target.strm"), "/mount/gone.mp4", "/mount", None)
        assert result is False

    def test_restore_source_disappears_fails(self):
        """A source path provided but file deleted → returns False."""
        # Ensure find_a_source_by_webdav also returns None
        self.db.get_a_local_path_by_webdav.return_value = None
        result = self.app.restore_b_file_from_a(
            str(self.b_dir / "target.strm"), "/mount/gone.mp4", "/mount",
            str(self.a_dir / "nonexistent.strm"))
        assert result is False

    def test_restore_copyfile_oserror_fails(self):
        """shutil.copyfile raises OSError → returns False."""
        webdav_path = "/mount/show/ep.mp4"
        a_file = self.a_dir / "show" / "ep.strm"
        a_file.parent.mkdir(parents=True, exist_ok=True)
        a_file.write_text(webdav_path, encoding="utf-8")

        with patch("shutil.copyfile", side_effect=OSError("disk full")):
            result = self.app.restore_b_file_from_a(
                str(self.b_dir / "target.strm"), webdav_path, "/mount", str(a_file))
        assert result is False

    def test_restore_db_error_fails(self):
        """DB write fails after copy → returns False."""
        import sqlite3
        webdav_path = "/mount/show/ep.mp4"
        a_file = self.a_dir / "show" / "ep.strm"
        a_file.parent.mkdir(parents=True, exist_ok=True)
        a_file.write_text(webdav_path, encoding="utf-8")
        self.db.upsert_b.side_effect = sqlite3.Error("db locked")

        with patch.object(self.app, "ensure_single_visible_instance"):
            result = self.app.restore_b_file_from_a(
                str(self.b_dir / "target.strm"), webdav_path, "/mount", str(a_file))
        assert result is False

    def test_restore_finds_alt_source_by_webdav(self):
        """source_a_path is None but alt source found by webdav_path → success."""
        webdav_path = "/mount/show/ep.mp4"
        a_file = self.a_dir / "found.strm"
        a_file.write_text(webdav_path, encoding="utf-8")
        self.db.get_a_local_path_by_webdav.return_value = str(a_file)

        with patch.object(self.app, "ensure_single_visible_instance"):
            result = self.app.restore_b_file_from_a(
                str(self.b_dir / "target.strm"), webdav_path, "/mount", None)

        assert result is True


# ===========================================================================
# TestValidateStrmStorages
# ===========================================================================


class TestValidateStrmStorages:
    """validate_strm_storages 的 null 防御测试。

    覆盖 app_service_core.py:1288-1305：当 admin_api.list_storages() 返回
    data: null 或 None 时，isinstance 守卫使 content 为 []，返回空结果，
    不抛出 'NoneType' object has no attribute 'get' 异常。
    """

    def _make_service(self):
        """构造最小化 AppService（跳过 __init__，仅设 validate 所需属性）。"""
        svc = AppService.__new__(AppService)
        svc.admin_api = MagicMock()
        return svc

    def test_data_field_null(self):
        """list_storages 返回 data: null → 返回空结果。"""
        svc = self._make_service()
        svc.admin_api.list_storages.return_value = {"code": 200, "data": None}

        result = svc.validate_strm_storages()

        assert result == {"total": 0, "working": 0, "storages": []}

    def test_list_storages_returns_none(self):
        """list_storages 返回 None → 返回空结果。"""
        svc = self._make_service()
        svc.admin_api.list_storages.return_value = None

        result = svc.validate_strm_storages()

        assert result == {"total": 0, "working": 0, "storages": []}

    def test_data_content_null(self):
        """list_storages 返回 data.content: null → 返回空结果。"""
        svc = self._make_service()
        svc.admin_api.list_storages.return_value = {
            "code": 200,
            "data": {"content": None, "total": 0},
        }

        result = svc.validate_strm_storages()

        assert result == {"total": 0, "working": 0, "storages": []}

    def test_normal_storages(self):
        """正常存储列表 → 返回正确计数。"""
        svc = self._make_service()
        svc.admin_api.list_storages.return_value = {
            "code": 200,
            "data": {
                "content": [
                    {"id": 1, "status": "work"},
                    {"id": 2, "status": "error"},
                ],
                "total": 2,
            },
        }

        result = svc.validate_strm_storages()

        assert result["total"] == 2
        assert result["working"] == 1
        assert len(result["storages"]) == 2


# ===========================================================================
# TestCleanupARedundantUsingApi
# ===========================================================================


class TestCleanupARedundantUsingApi:
    """Test cleanup_a_redundant_using_api using OpenList API."""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()
        self.db = db
        self.admin_api = Mock()

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, self.admin_api)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_a_records_skips(self):
        """No A records → skips cleanup."""
        self.db.get_all_a_records.return_value = []
        self.app.cleanup_a_redundant_using_api()
        self.admin_api.list_directory.assert_not_called()

    def test_no_redundant_files(self):
        """All local files exist in cloud → no deletion.
        
        API 的 path 字段是存储系统路径，代码从 cloud_path + name 构建
        WebDAV 路径来与 rec.webdav_path 比较。
        """
        a_file = self.a_dir / "keep.strm"
        a_file.write_text("/mount/keep.strm", encoding="utf-8")
        self.db.get_all_a_records.return_value = [
            Mock(local_path=str(a_file), webdav_path="/mount/keep.strm",
                 parent_webdav_path="/mount")
        ]
        self.admin_api.list_directory.return_value = {
            "code": 200,
            "data": {
                "total": 1,
                "content": [{"name": "keep.strm", "is_dir": False,
                             "path": "D:\\storage\\keep.strm"}],
            },
        }

        self.app.cleanup_a_redundant_using_api()

        self.db.delete_a_by_local.assert_not_called()

    def test_redundant_file_deleted(self):
        """Local file not in cloud → deleted with ghost protection."""
        a_file = self.a_dir / "gone.strm"
        a_file.write_text("/mount/gone.mp4", encoding="utf-8")
        self.db.get_all_a_records.return_value = [
            Mock(local_path=str(a_file), webdav_path="/mount/gone.mp4",
                 parent_webdav_path="/mount")
        ]
        # Cloud returns empty → all local files are redundant
        self.admin_api.list_directory.return_value = {
            "code": 200,
            "data": {"total": 0, "content": []},
        }

        self.app.cleanup_a_redundant_using_api()

        self.db.delete_a_by_local.assert_called_once_with(str(a_file))
        self.db.set_ghost_protection.assert_called_once()

    def test_only_strm_files_from_cloud(self):
        """Cloud returns non-.strm files → only .strm paths collected,
        so local .strm records are flagged as redundant."""
        a_file = self.a_dir / "good.strm"
        a_file.write_text("/mount/good.strm", encoding="utf-8")
        self.db.get_all_a_records.return_value = [
            Mock(local_path=str(a_file), webdav_path="/mount/good.strm",
                 parent_webdav_path="/mount")
        ]
        # Cloud has .nfo and .srt files but NOT the .strm file
        self.admin_api.list_directory.return_value = {
            "code": 200,
            "data": {
                "total": 2,
                "content": [
                    {"name": "good.nfo", "is_dir": False,
                     "path": "D:\\storage\\good.nfo"},
                    {"name": "good.srt", "is_dir": False,
                     "path": "D:\\storage\\good.srt"},
                ],
            },
        }

        self.app.cleanup_a_redundant_using_api()

        self.db.delete_a_by_local.assert_called_once_with(str(a_file))


# ===========================================================================
# TestCollectCloudFilesConcurrent
# ===========================================================================


class TestCollectCloudFilesConcurrent:
    """Test _collect_cloud_files_concurrent pagination logic."""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()
        self.db = db
        self.admin_api = Mock()

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, self.admin_api)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_single_page_collection(self):
        """Directory with ≤100 files → single page request.
        
        API 的 path 字段是存储系统原始路径（如 D:\\files\\xxx），
        但代码应从 cloud_path + name 重构 WebDAV 虚拟路径。
        """
        self.admin_api.list_directory.return_value = {
            "code": 200,
            "data": {
                "total": 2,
                "content": [
                    {"name": "f1.strm", "is_dir": False,
                     "path": "D:\\storage\\movies\\f1.strm"},
                    {"name": "f2.strm", "is_dir": False,
                     "path": "D:\\storage\\movies\\f2.strm"},
                ],
            },
        }
        file_set: set[str] = set()
        self.app._collect_cloud_files_concurrent("/mount", file_set)

        # 路径应从 cloud_path + name 重构，而非使用 API 的 path 字段
        assert file_set == {"/mount/f1.strm", "/mount/f2.strm"}
        self.admin_api.list_directory.assert_called_once()

    def test_multi_page_concurrent_collection(self):
        """Directory with 150 files → 2 pages, page 2 fetched concurrently."""
        page1 = {
            "code": 200,
            "data": {
                "total": 150,
                "content": [
                    {"name": f"f{i}.strm", "is_dir": False,
                     "path": f"D:\\storage\\f{i}.strm"}
                    for i in range(100)
                ],
            },
        }
        page2 = {
            "code": 200,
            "data": {
                "total": 150,
                "content": [
                    {"name": f"f{i}.strm", "is_dir": False,
                     "path": f"D:\\storage\\f{i}.strm"}
                    for i in range(100, 150)
                ],
            },
        }
        self.admin_api.list_directory.side_effect = [page1, page2]

        file_set: set[str] = set()
        self.app._collect_cloud_files_concurrent("/mount", file_set)

        assert len(file_set) == 150

    def test_empty_directory(self):
        """Empty directory → no files collected."""
        self.admin_api.list_directory.return_value = {
            "code": 200,
            "data": {"total": 0, "content": []},
        }
        file_set: set[str] = set()
        self.app._collect_cloud_files_concurrent("/mount", file_set)

        assert len(file_set) == 0

    def test_filters_non_strm_files(self):
        """Non-.strm files in cloud are filtered out."""
        self.admin_api.list_directory.return_value = {
            "code": 200,
            "data": {
                "total": 3,
                "content": [
                    {"name": "good.strm", "is_dir": False,
                     "path": "D:\\storage\\good.strm"},
                    {"name": "bad.nfo", "is_dir": False,
                     "path": "D:\\storage\\bad.nfo"},
                    {"name": "dir", "is_dir": True,
                     "path": "D:\\storage\\dir"},
                ],
            },
        }
        file_set: set[str] = set()
        self.app._collect_cloud_files_concurrent("/mount", file_set)

        assert file_set == {"/mount/good.strm"}

    def test_api_failure_returns_empty(self):
        """API failure → returns empty, no exception."""
        self.admin_api.list_directory.return_value = None
        file_set: set[str] = set()
        self.app._collect_cloud_files_concurrent("/mount", file_set)

        assert len(file_set) == 0

    def test_items_with_missing_name_are_skipped(self):
        """Items with no name field are skipped, not added to the set."""
        self.admin_api.list_directory.return_value = {
            "code": 200,
            "data": {
                "total": 3,
                "content": [
                    {"name": "good.strm", "is_dir": False,
                     "path": "D:\\storage\\good.strm"},
                    {"is_dir": False, "path": "D:\\storage\\noname.strm"},
                    {"name": None, "is_dir": False,
                     "path": "D:\\storage\\none.strm"},
                ],
            },
        }
        file_set: set[str] = set()
        self.app._collect_cloud_files_concurrent("/mount", file_set)

        assert file_set == {"/mount/good.strm"}

    def test_path_reconstructed_from_cloud_path_plus_name(self):
        """Verify path is built from cloud_path + name, not from API path field.
        
        The API's path field returns a full system storage path like 
        D:\\movies\\show\\file.strm, which is NOT a WebDAV virtual path.
        The code must reconstruct the WebDAV path from the cloud_path 
        (parent directory) and the item name.
        """
        self.admin_api.list_directory.return_value = {
            "code": 200,
            "data": {
                "total": 1,
                "content": [
                    {"name": "episode.strm", "is_dir": False,
                     "path": "E:\\cloud\\movies\\Show\\Season 01\\episode.strm"},
                ],
            },
        }
        file_set: set[str] = set()
        self.app._collect_cloud_files_concurrent("/mount/show", file_set)

        # Should be cloud_path + "/" + name, NOT item["path"]
        assert file_set == {"/mount/show/episode.strm"}


# ===========================================================================
# TestScanASubtitlesOnStartup
# ===========================================================================


class TestScanASubtitlesOnStartup:
    """Test _scan_a_subtitles_on_startup subtitle scanning."""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()
        self.db = db

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_subtitle_files(self):
        """Subtitle files in A directory are processed."""
        sub = self.a_dir / "movie.srt"
        sub.write_text("subtitle content", encoding="utf-8")
        # Also create a non-subtitle file
        (self.a_dir / "movie.strm").write_text(
            "/mount/movie.mp4", encoding="utf-8")

        with patch.object(self.app, "process_subtitle_file") as mock_proc:
            self.app._scan_a_subtitles_on_startup()

        mock_proc.assert_called_once()


# ===========================================================================
# TestStartCallsCleanupARedundant
# ===========================================================================


class TestStartCallsCleanupARedundant:
    """Verify start() does NOT call cleanup_a_redundant_using_api or cleanup_b_redundant.
    
    冗余清理已改为局部触发（WebUI 手动刷新 / B 区删除事件），不再在启动时执行全盘清理。
    """

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.paths.strm_engine_paths = []
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.behavior.sync_on_startup_wait = 0
        config.behavior.sync_on_startup = True
        config.behavior.subtitle_extensions = []
        config.behavior.subtitle_scan_roots = []

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()
        self.db = db

        with patch("app_service_core.RefreshService") as mock_refresh, \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, Mock())
            self.mock_refresh = mock_refresh.return_value

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_start_does_not_call_cleanup_a_redundant(self):
        """start() does NOT call cleanup_a_redundant_using_api (redundant cleanup is now local-only)."""
        with patch.object(self.app, "prepare_environment"), \
             patch.object(self.app, "update_engine_configs"), \
             patch.object(self.app, "initial_scan_b"), \
             patch.object(self.app, "sync_protected_roots_from_config"), \
             patch.object(self.app, "scan_removed_protected_roots"), \
             patch.object(self.app, "persist_current_roots_snapshot"), \
             patch.object(self.app, "initial_scan_a"), \
             patch.object(self.app, "cleanup_a_redundant_using_api") as mock_cleanup_a, \
             patch.object(self.app, "scan_a_to_b_full_sync"), \
             patch.object(self.app, "cleanup_b_redundant") as mock_cleanup_b, \
             patch.object(self.app, "start_watchers"), \
             patch.object(self.app, "_scan_a_subtitles_on_startup"), \
             patch.object(self.app.admin_api, "list_storages"):
            self.db.get_all_a_records.return_value = []
            self.app.start()

        # 启动时不再调用全局冗余清理
        mock_cleanup_a.assert_not_called()
        mock_cleanup_b.assert_not_called()

    def test_missing_root_skipped(self):
        """Non-existent A root is skipped."""
        self.app.a_roots = [Path(self.tmp) / "nonexistent"]

        with patch.object(self.app, "process_subtitle_file") as mock_proc:
            self.app._scan_a_subtitles_on_startup()

        mock_proc.assert_not_called()


# ===========================================================================
# TestCleanupBZombiesBatchOptimization
# ===========================================================================


class TestCleanupBZombiesBatchOptimization:
    """Verify cleanup_b_zombies_under_folder uses batch API calls instead of per-record check_exists."""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()

        db = Mock(spec=Database)
        self.db = db

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cleanup_b_zombies_uses_batch_api(self):
        """cleanup_b_zombies_under_folder groups records by parent and uses list_directory instead of check_exists."""
        # 准备测试数据：3 个记录在 2 个不同的父目录下
        records = [
            BRecord("/b/file1.strm", "/cloud/dir1/file1.strm", "/cloud/dir1", "/a/file1.strm", "fp1", "valid", "2024-01-01"),
            BRecord("/b/file2.strm", "/cloud/dir1/file2.strm", "/cloud/dir1", "/a/file2.strm", "fp2", "valid", "2024-01-01"),
            BRecord("/b/file3.strm", "/cloud/dir2/file3.strm", "/cloud/dir2", "/a/file3.strm", "fp3", "valid", "2024-01-01"),
        ]
        self.db.get_b_under_root.return_value = records
        self.db.get_b_by_local_full.return_value = None

        # Mock list_directory 返回云端文件列表
        def mock_list_directory(path, **kwargs):
            if path == "/cloud/dir1":
                return {"code": 0, "data": {"content": [
                    {"name": "file1.strm", "is_dir": False},
                    # file2.strm 不在云端（僵尸文件）
                ]}}
            elif path == "/cloud/dir2":
                return {"code": 0, "data": {"content": [
                    {"name": "file3.strm", "is_dir": False},
                ]}}
            return None

        # 使用 Mock 包装以便追踪调用次数
        list_dir_mock = Mock(side_effect=mock_list_directory)
        self.app.admin_api.list_directory = list_dir_mock

        with patch.object(self.app, "_handle_b_zombie") as mock_handle:
            self.app.cleanup_b_zombies_under_folder("/cloud")

            # 验证：只调用 list_directory 2 次（每个父目录一次），而不是 check_exists 3 次
            assert list_dir_mock.call_count == 2
            # 验证：file2.strm 被识别为僵尸文件
            assert mock_handle.call_count == 1
            call_args = mock_handle.call_args[0]
            assert call_args[0] == "/b/file2.strm"  # local_path
            assert call_args[1] == "/cloud/dir1/file2.strm"  # webdav_path

    def test_cleanup_b_zombies_handles_api_failure(self):
        """cleanup_b_zombies_under_folder skips directory when list_directory fails."""
        records = [
            BRecord("/b/file1.strm", "/cloud/dir1/file1.strm", "/cloud/dir1", "/a/file1.strm", "fp1", "valid", "2024-01-01"),
        ]
        self.db.get_b_under_root.return_value = records

        # Mock list_directory 返回失败
        self.app.admin_api.list_directory.return_value = {"code": 500, "data": {}}

        with patch.object(self.app, "_handle_b_zombie") as mock_handle:
            self.app.cleanup_b_zombies_under_folder("/cloud")

            # 验证：API 失败时不处理任何文件
            mock_handle.assert_not_called()


# ===========================================================================
# TestHandleBDeletedTriggersCleanup
# ===========================================================================


class TestHandleBDeletedTriggersCleanup:
    """Verify handle_b_deleted triggers delayed cleanup for the parent directory."""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.a_to_b_restore_delay_seconds = 30

        db = Mock(spec=Database)
        db.get_b_by_local_full.return_value = BRecord(
            "/b/file1.strm", "/cloud/dir1/file1.strm", "/cloud/dir1",
            "/a/file1.strm", "fp1", "valid", "2024-01-01"
        )
        db.has_other_b_instance.return_value = False
        self.db = db

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_handle_b_deleted_triggers_delayed_cleanup(self):
        """handle_b_deleted calls trigger_delayed_cleanup with parent_webdav_path."""
        local_path = self.b_dir / "file1.strm"
        local_path.touch()

        with patch.object(self.app, "_execute_webdav_deletion"), \
             patch.object(self.app, "_delete_a_file_by_webdav"), \
             patch.object(self.app, "refresh_identity_current_b_path"), \
             patch.object(self.app, "_check_fingerprint_exists_in_b", return_value=False), \
             patch.object(self.app, "trigger_delayed_cleanup") as mock_trigger:
            self.app.handle_b_deleted(str(local_path))

            # 验证：调用 trigger_delayed_cleanup 并传入父目录路径
            mock_trigger.assert_called_once_with("/cloud/dir1")
