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
import time
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

# Add src/ to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app_service_core
from app_service_core import AppService, StrmStorageManager, StrmStorageInfo
from database import Database, ARecord, BRecord
from config import AppConfig, ABMapping, StrmStorageMapping
from utils import make_strm_fingerprint


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

    def test_extract_save_local_mode_null_returns_empty(self):
        # 远端 API 返回 "SaveLocalMode": null 时 .get 默认值不生效，
        # 必须显式类型守卫返回 ""，否则 is_sync_mode 调 .lower() 抛 AttributeError
        assert self.manager._extract_save_local_mode('{"SaveLocalMode": null}') == ""
        assert self.manager._extract_save_local_mode('{"SaveLocalMode": 123}') == ""
        assert self.manager._extract_save_local_mode('{}') == ""

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
        config.a_b_mappings = []  # 新增：多 A↔多 B 映射
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
        # 新增：多 A↔多 B 映射 - 需要包含测试用的 A 根目录
        from config import ABMapping
        config.a_b_mappings = [
            ABMapping(
                mapping_id="test1",
                a_root=a_dir,
                b_root=os.path.join(self.tmp, "b"),
                label="测试映射"
            )
        ]
        config.paths = Mock()
        config.paths.b_root = os.path.join(self.tmp, "b")
        config.paths.c_root = os.path.join(self.tmp, "c")
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()
        db.is_ghost_protected = Mock(return_value=False)
        db.upsert_a = Mock()
        db.save_known_folder = Mock()
        db.upsert_identity = Mock()
        db.get_identity_by_fingerprint = Mock(return_value=None)
        db.get_a_by_webdav = Mock(return_value=None)

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
        self.app.db.get_valid_b_instance_by_fingerprint = Mock(return_value=None)

        self.app.handle_a_created_or_modified(a_file)
        self.app.db.is_ghost_protected.assert_called_with(webdav_path)


# ===========================================================================
# TestBuildBPathFromA
# ===========================================================================


class TestHandleANonStrm:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.a_dir.mkdir()
        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        # 新增：多 A↔多 B 映射 - 需要包含测试用的 A 根目录
        from config import ABMapping
        config.a_b_mappings = [
            ABMapping(
                mapping_id="test1",
                a_root=str(self.a_dir),
                b_root=str(self.a_dir / "b"),
                label="测试映射"
            )
        ]
        config.paths = Mock(b_root=str(self.a_dir / "b"), c_root=str(self.a_dir / "c"))
        config.behavior = Mock(ghost_protect_seconds=300)
        config.strm_engine_paths = []
        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()
        with patch("app_service_core.RefreshService"), patch("app_service_core.SyncService"), patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_binary_non_strm_is_skipped_before_decode(self):
        image = self.a_dir / "cover.jpg"
        image.write_bytes(b"\xff\xd8\xff\xe0")
        with patch("app_service_core.read_strm_webdav_path") as read_strm:
            self.app.handle_a_created_or_modified(str(image))
        read_strm.assert_not_called()
        self.app.db.upsert_a.assert_not_called()

    def test_handle_a_created_or_modified_routes_subtitle_to_process(self):
        """字幕文件进入 handle_a_created_or_modified 应路由到 process_subtitle_file"""
        sub = self.a_dir / "movie.srt"
        sub.write_text("content", encoding="utf-8")
        with patch.object(self.app, "process_subtitle_file") as mock:
            self.app.handle_a_created_or_modified(str(sub))
        mock.assert_called_once()


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
        config.a_b_mappings = [Mock(a_root=str(self.a_dir), b_root=str(self.b_dir))]  # 新增：多 A↔多 B 映射
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

    def test_webdav_basename_preserves_original_episode_padding(self):
        """B 区文件名保留 WebDAV basename 的原始集数 padding"""
        a_file = self.a_dir / "Show Name" / "S20E10.strm"
        a_file.parent.mkdir(parents=True, exist_ok=True)
        a_file.write_text("/mount/show/episode.mp4", encoding="utf-8")

        with patch.object(self.app, "_should_treat_as_movie", return_value=False), \
             patch("app_service_core.suggest_rename", return_value="S20E10.strm"), \
             patch("app_service_core._extract_season_episode", return_value=(20, 10)), \
             patch("app_service_core.extract_season_from_path", return_value=None):
            result = self.app.build_b_path_from_a(
                str(a_file), "/番剧/Show/episode - 24 END.mkv")

        assert result.name == "episode - 24 END.strm"

    def test_season_not_derived_from_webdav_path(self):
        """season 只来自 A 区本地路径/文件名，不从 WebDAV 路径推导"""
        # A 区本地路径无季信息，但 WebDAV 路径含 Season 5 目录
        a_file = self.a_dir / "Show Name" / "S01E01.strm"
        a_file.parent.mkdir(parents=True, exist_ok=True)
        a_file.write_text("/mount/show/episode.mp4", encoding="utf-8")

        # extract_season_from_path 对本地路径返回 None（本地无 Season 目录）
        # 对 WebDAV 路径会返回 5（如果仍读取 webdav_path）
        def _season_side_effect(path):
            p = str(path)
            if "Season 5" in p or "season 5" in p:
                return 5
            return None

        with patch.object(self.app, "_should_treat_as_movie", return_value=False), \
             patch("app_service_core.suggest_rename", return_value="S01E01.strm"), \
             patch("app_service_core._extract_season_episode", return_value=(1, 1)), \
             patch("app_service_core.extract_season_from_path", side_effect=_season_side_effect):
            result = self.app.build_b_path_from_a(
                str(a_file), "/番剧/Show/Season 5/episode.mkv")

        # season 应来自本地 _extract_season_episode (1)，而非 WebDAV 的 Season 5
        assert "Season 01" in str(result)
        assert "Season 05" not in str(result)

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
        # 新增：多 A↔多 B 映射
        from config import ABMapping
        config.a_b_mappings = [
            ABMapping(
                mapping_id="test1",
                a_root=str(self.a_dir),
                b_root=str(self.b_dir),
                label="测试映射"
            )
        ]
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
        config.a_b_mappings = [ABMapping(
            mapping_id="test_m1", a_root=str(self.a_dir), b_root=str(self.b_dir))]
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
            mapping_id="test_m1",
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
        config.a_b_mappings = [ABMapping(
            mapping_id="test_m1", a_root=str(self.a_dir), b_root=str(self.b_dir))]
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
            mapping_id="test_m1",
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
            mapping_id="test_m1",
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
        config.a_b_mappings = [ABMapping(
            mapping_id="test_m1", a_root=str(self.a_dir), b_root=str(self.b_dir))]
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

    def test_cleanup_keeps_unknown_keyword_suffixed_files(self):
        """后缀文件身份未知时必须保留，不能按文件名直接删除。"""
        bad1 = self.b_dir / "old.strm.duplicate"
        bad2 = self.b_dir / "old.strm.invalid"
        bad1.write_text("x")
        bad2.write_text("x")

        self.db.get_all_b_records.return_value = []
        self.db.get_b_by_local_full.return_value = None

        self.app.cleanup_b_redundant()

        assert bad1.exists()
        assert bad2.exists()

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
            mapping_id="test_m1",
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
            mapping_id="test_m1",
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
            mapping_id="test_m1",
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

    # ----------------------------------------------------------
    # fail-closed：check_exists() is None
    # ----------------------------------------------------------

    def _make_b_record(self, b_file: Path, webdav_path: str, *,
                       source_a_path: str, mapping_id: str = "test_m1") -> BRecord:
        return BRecord(
            local_path=str(b_file),
            webdav_path=webdav_path,
            parent_webdav_path=webdav_path.rsplit("/", 1)[0] or "/",
            source_a_path=source_a_path,
            fingerprint="fp_x",
            status="valid",
            updated_at=0,
            mapping_id=mapping_id,
        )

    def test_untrusted_existence_with_missing_a_source_keeps_all(self):
        """A 源不存在 + check_exists() is None → B 文件/DB/来源全部保留。"""
        b_file = self.b_dir / "untrusted.strm"
        b_file.write_text("/mount/untrusted.mp4", encoding="utf-8")

        record = self._make_b_record(
            b_file, "/mount/untrusted.mp4",
            source_a_path=str(self.a_dir / "missing.strm"))
        self.db.get_all_b_records.return_value = [record]
        self.db.is_ghost_protected.return_value = False
        self.db.get_a_local_path_by_webdav.return_value = None
        self.admin_api.check_exists.return_value = None  # 不可信

        self.app.cleanup_b_redundant()

        assert b_file.exists(), "None（不可信）不得删除 B 文件"
        self.db.delete_b_by_local.assert_not_called()
        self.db.upsert_c.assert_not_called()
        # C 区不应出现任何迁移产物
        assert list(self.c_dir.rglob("*.strm")) == []

    def test_untrusted_existence_with_existing_a_source_keeps_all(self):
        """A 源存在 + check_exists() is None → 不进入删除分支。"""
        a_file = self.a_dir / "src.strm"
        a_file.write_text("/mount/src.mp4", encoding="utf-8")
        b_file = self.b_dir / "src.strm"
        b_file.write_text("/mount/src.mp4", encoding="utf-8")

        record = self._make_b_record(
            b_file, "/mount/src.mp4", source_a_path=str(a_file))
        self.db.get_all_b_records.return_value = [record]
        self.db.is_ghost_protected.return_value = False
        self.admin_api.check_exists.return_value = None  # 不可信

        self.app.cleanup_b_redundant()

        assert b_file.exists(), "None（不可信）不得删除 B 文件"
        assert a_file.exists()
        self.db.delete_b_by_local.assert_not_called()

    # ----------------------------------------------------------
    # mapping 边界
    # ----------------------------------------------------------

    def test_mapping_unresolvable_keeps_source(self):
        """get_mapping_for_b() 返回 None → 不迁移 C 区、不删来源。"""
        b_file = self.b_dir / "orphan.strm"
        b_file.write_text("/mount/gone.mp4", encoding="utf-8")

        record = self._make_b_record(
            b_file, "/mount/gone.mp4",
            source_a_path=str(self.a_dir / "missing.strm"))
        self.db.get_all_b_records.return_value = [record]
        self.db.is_ghost_protected.return_value = False
        self.db.get_a_local_path_by_webdav.return_value = None
        self.admin_api.check_exists.return_value = False  # 云端权威缺失

        with patch.object(self.app, "get_mapping_for_b", return_value=None):
            self.app.cleanup_b_redundant()

        assert b_file.exists(), "mapping 不明确时必须保留来源"
        self.db.upsert_c.assert_not_called()
        self.db.delete_b_by_local.assert_not_called()
        assert list(self.c_dir.rglob("*.strm")) == []

    def test_mapping_id_mismatch_keeps_source(self):
        """记录 mapping_id 与解析出的 mapping 不一致 → 保留来源。"""
        b_file = self.b_dir / "orphan.strm"
        b_file.write_text("/mount/gone.mp4", encoding="utf-8")

        # 记录声明属于 other_m，与实际解析出的 test_m1 冲突
        record = self._make_b_record(
            b_file, "/mount/gone.mp4",
            source_a_path=str(self.a_dir / "missing.strm"),
            mapping_id="other_m")
        self.db.get_all_b_records.return_value = [record]
        self.db.is_ghost_protected.return_value = False
        self.db.get_a_local_path_by_webdav.return_value = None
        self.admin_api.check_exists.return_value = False

        self.app.cleanup_b_redundant()

        assert b_file.exists(), "mapping_id 不匹配时必须保留来源"
        self.db.upsert_c.assert_not_called()
        self.db.delete_b_by_local.assert_not_called()

    def test_empty_mapping_id_keeps_source(self):
        """记录缺少 mapping_id → 无法证明隔离边界，保留来源。"""
        b_file = self.b_dir / "orphan.strm"
        b_file.write_text("/mount/gone.mp4", encoding="utf-8")

        record = self._make_b_record(
            b_file, "/mount/gone.mp4",
            source_a_path=str(self.a_dir / "missing.strm"),
            mapping_id="")
        self.db.get_all_b_records.return_value = [record]
        self.db.is_ghost_protected.return_value = False
        self.db.get_a_local_path_by_webdav.return_value = None
        self.admin_api.check_exists.return_value = False

        self.app.cleanup_b_redundant()

        assert b_file.exists()
        self.db.upsert_c.assert_not_called()
        self.db.delete_b_by_local.assert_not_called()

    # ----------------------------------------------------------
    # C 目标已存在但异源
    # ----------------------------------------------------------

    def test_existing_c_target_with_different_source_keeps_source(self):
        """C 目标已存在但与来源 WebDAV 不同 → 保留来源，不删不覆盖。"""
        b_file = self.b_dir / "orphan.strm"
        b_file.write_text("/mount/mine.mp4", encoding="utf-8")

        record = self._make_b_record(
            b_file, "/mount/mine.mp4",
            source_a_path=str(self.a_dir / "missing.strm"))
        self.db.get_all_b_records.return_value = [record]
        self.db.is_ghost_protected.return_value = False
        self.db.get_a_local_path_by_webdav.return_value = None
        self.admin_api.check_exists.return_value = False

        # 预置一个异源的 C 目标
        c_target = self.app.get_c_path_for_b(
            "test_m1", b_file, self.b_dir)
        c_target.parent.mkdir(parents=True, exist_ok=True)
        c_target.write_text("/mount/OTHER.mp4", encoding="utf-8")

        self.app.cleanup_b_redundant()

        assert b_file.exists(), "C 目标异源时必须保留来源"
        assert c_target.read_text(encoding="utf-8") == "/mount/OTHER.mp4", \
            "异源 C 目标不得被覆盖"
        self.db.delete_b_by_local.assert_not_called()

    def test_existing_c_target_same_source_removes_source_only(self):
        """C 目标已存在且同源 → 删除来源、删 DB 记录，不重复写 C 记录。"""
        b_file = self.b_dir / "orphan.strm"
        b_file.write_text("/mount/same.mp4", encoding="utf-8")

        record = self._make_b_record(
            b_file, "/mount/same.mp4",
            source_a_path=str(self.a_dir / "missing.strm"))
        self.db.get_all_b_records.return_value = [record]
        self.db.is_ghost_protected.return_value = False
        self.db.get_a_local_path_by_webdav.return_value = None
        self.admin_api.check_exists.return_value = False
        self.db.get_identity_by_fingerprint = Mock(return_value=None)
        self.db.get_all_b_by_fingerprint = Mock(return_value=[])

        c_target = self.app.get_c_path_for_b("test_m1", b_file, self.b_dir)
        c_target.parent.mkdir(parents=True, exist_ok=True)
        c_target.write_text("/mount/same.mp4", encoding="utf-8")

        self.app.cleanup_b_redundant()

        assert not b_file.exists(), "同源 C 目标存在时应清理来源"
        self.db.delete_b_by_local.assert_called_once_with(str(b_file))
        # 目标已存在，不再写入新的 C 记录
        self.db.upsert_c.assert_not_called()

    def test_upsert_c_failure_keeps_migrated_file_and_b_record(self):
        """db.upsert_c() 抛异常 → 保留已迁移文件待恢复，不删 B 记录。

        断言当前代码实际承诺的结果：物理文件已移动到 C，
        但 DB B 记录保留（不调用 delete_b_by_local），供后续恢复。
        """
        b_file = self.b_dir / "orphan.strm"
        b_file.write_text("/mount/gone.mp4", encoding="utf-8")

        record = self._make_b_record(
            b_file, "/mount/gone.mp4",
            source_a_path=str(self.a_dir / "missing.strm"))
        self.db.get_all_b_records.return_value = [record]
        self.db.is_ghost_protected.return_value = False
        self.db.get_a_local_path_by_webdav.return_value = None
        self.admin_api.check_exists.return_value = False
        self.db.upsert_c.side_effect = RuntimeError("db write failed")

        self.app.cleanup_b_redundant()

        c_target = self.app.get_c_path_for_b("test_m1", b_file, self.b_dir)
        # 物理迁移已发生
        assert not b_file.exists()
        assert c_target.exists(), "迁移后的文件应保留在 C 区待恢复"
        # DB B 记录未被删除（异常后 continue）
        self.db.delete_b_by_local.assert_not_called()


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
        config.a_b_mappings = [ABMapping(
            mapping_id="test_m1", a_root=str(self.a_dir), b_root=str(self.b_dir))]
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
            mapping_id="test_m1",
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
        config.a_b_mappings = [ABMapping(
            mapping_id="test_m1", a_root=str(self.a_dir), b_root=str(self.b_dir))]
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
        """B has files but DB is empty → insert new records（C10：重复意图用例走会话批量路径，run_group 恰一次）。"""
        webdav = "/mount/file.mp4"
        b_file = self.b_dir / "file.strm"
        b_dup = self.b_dir / "file (1).strm"
        b_file.write_text(webdav, encoding="utf-8")
        b_dup.write_text(webdav, encoding="utf-8")
        from utils import make_strm_fingerprint
        fp = make_strm_fingerprint(webdav)
        self.db.get_all_b_records.return_value = []
        self.db.get_identity_by_fingerprint.return_value = None
        self.db.get_a_local_path_by_webdav.return_value = None
        # 批写契约：返回实际持久化 B 行数（本批 2 条）
        self.db.upsert_b_records_and_snapshots_batch.return_value = 2
        # Task 4 批量身份刷新需要的 mock 返回值
        self.db.get_identities_for_fingerprints.return_value = {}
        self.db.get_all_identity_projections_for_fingerprint.return_value = []
        # C10：同指纹双实例 B 行（upsert 后的联动状态）
        def _record(path):
            return BRecord(
                local_path=path, webdav_path=webdav, parent_webdav_path="/mount",
                source_a_path=None, fingerprint=fp, status="valid",
                updated_at=0, mapping_id="test_m1",
            )
        records = [_record(str(b_file)), _record(str(b_dup))]
        self.db.get_all_b_rows_for_fp_pairs.return_value = records
        # C10：预筛返回该重复组（启动上下文 → 会话批量路径）
        self.db.get_duplicate_fingerprint_groups.return_value = [
            ("test_m1", fp, str(b_file))]
        # 会话 mock（run_group 恰一次断言目标）
        mock_session = Mock()
        mock_session.run_group.return_value = 1
        mock_ctx = Mock()
        mock_ctx.__enter__ = Mock(return_value=mock_session)
        mock_ctx.__exit__ = Mock(return_value=False)
        self.db.quarantine_session.return_value = mock_ctx

        with patch.object(self.app, "_b_lineage_preflight", return_value=True):
            self.app.initial_scan_b()

        self.db.upsert_b_records_and_snapshots_batch.assert_called_once()
        self.db.upsert_identities_batch.assert_called_once()
        # 会话批量路径：一组两文件入队 → run_group 恰一次（双入队消除）
        mock_session.run_group.assert_called_once()
        called_fp, called_mid, called_keep, renamed = mock_session.run_group.call_args[0]
        assert called_fp == fp
        assert called_mid == "test_m1"
        # 改名结果按 keep/dup 传入：恰好 1 个非 None 改名项
        assert len(renamed) == 1
        old_path, quarantined = renamed[0]
        assert old_path in (str(b_file), str(b_dup))
        assert quarantined is not None and quarantined.endswith(".duplicate")

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
            mapping_id="test_m1",
        )
        self.db.get_all_b_records.return_value = [record]

        # C9 双钉：reconcile 语义用例 patch verify 时必须钉 preflight=False，
        # 否则 Wave1 预检短路使 wave2 串行回退段（_verify_b_path_lineage）不被行使
        with patch.object(self.app, "_b_lineage_preflight", return_value=False), \
             patch.object(self.app, "_verify_b_path_lineage", return_value=True):
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
            mapping_id="test_m1",
        )
        self.db.get_all_b_records.return_value = [record]

        self.app.initial_scan_b()

        self.db.delete_b_by_local.assert_called_once()

    def test_reconcile_logs_phase_start_and_completion(self, caplog):
        """B 区历史记录核对有明确的阶段开始和完成日志"""
        import logging
        # 设置 B 区磁盘文件和 DB 记录匹配
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
            mapping_id="test_m1",
        )
        self.db.get_all_b_records.return_value = [record]

        # 有意冒烟测试，断言代码路径执行，非行为正确性验证
        # C9 双钉：preflight=False 强制走 wave2 串行回退段
        with caplog.at_level(logging.INFO), \
             patch.object(self.app, "_b_lineage_preflight", return_value=False), \
             patch.object(self.app, "_verify_b_path_lineage", return_value=True):
            self.app.initial_scan_b()

        assert any("B 区历史记录核对开始" in msg for msg in caplog.messages)
        assert any("B 区历史记录核对完成" in msg for msg in caplog.messages)

    # 有意冒烟测试，断言代码路径执行，非行为正确性验证
    def test_reconcile_pre_call_debug_logs(self, caplog):
        """B 区历史核对中关键函数调用前有 DEBUG 日志包含路径"""
        import logging
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
            mapping_id="test_m1",
        )
        self.db.get_all_b_records.return_value = [record]

        # 有意冒烟测试：C9 双钉 preflight=False，wave2 回退段的
        # "lineage 校验" DEBUG 日志必然发射
        with caplog.at_level(logging.DEBUG), \
             patch.object(self.app, "_b_lineage_preflight", return_value=False), \
             patch.object(self.app, "_verify_b_path_lineage", return_value=True):
            self.app.initial_scan_b()

        # 验证调用前日志包含当前路径
        assert any("lineage 校验" in msg and str(b_file) in msg for msg in caplog.messages)

    def test_b_disk_scan_corrupted_file_isolation(self):
        """B 区并发扫描时单文件损坏异常隔离，正常文件正确汇总。"""
        b_file1 = self.b_dir / "good1.strm"
        b_file1.write_text("/mount/good1.mp4", encoding="utf-8")
        b_file2 = self.b_dir / "good2.strm"
        b_file2.write_text("/mount/good2.mp4", encoding="utf-8")
        bad_file = self.b_dir / "bad.strm"
        bad_file.write_bytes(b"\xff\xfe\x00\x00corrupt")  # 异常文件

        with patch("app_service_core.read_strm_webdav_path", side_effect=lambda p: None if "bad" in str(p) else f"/mount/{p.name}"):
            res = self.app._scan_b_disk()
            assert res is not None
            fps, path_data = res
            assert len(path_data) == 2
            assert str(bad_file) not in path_data

    def test_update_engine_configs_reuses_raw_strm_storages_snapshot(self):
        """update_engine_configs 优先复用 raw_strm_storages 快照，不发起二次网络请求。"""
        import json
        self.app.config.raw_strm_storages = [
            {
                "mount_path": "/strm",
                "addition": json.dumps({"SaveStrmLocalPath": str(self.a_dir), "paths": "/cloud/anime"}),
            }
        ]
        self.app.config.strm_engine_paths = ["/strm"]
        self.app.config.paths.strm_engine_paths = ["/strm"]

        self.app.update_engine_configs()

        # admin_api.get_strm_storages_full_info 不应被调用
        self.app.admin_api.get_strm_storages_full_info.assert_not_called()
        assert len(self.app.engine_configs) == 1
        assert self.app.engine_configs[0]["mount_path"] == "/strm"

    def test_insert_new_b_records_projection_and_dedup_order(self):
        """断言 B 区新增记录批写在 commit 前不调用投影刷新，commit 后执行投影与延迟去重。

        C10 适配：dedup 锚点改为会话批量入口（启动上下文默认路径）。
        """
        call_order = []
        self.db.upsert_b_records_and_snapshots_batch.side_effect = lambda *args, **kwargs: (
            call_order.append("batch_write") or 1
        )
        self.db.get_identity_by_fingerprint.return_value = None
        self.db.get_all_b_by_fingerprint.return_value = []
        # C10：预筛返回重复组 → 启动上下文（observer 为 None）走会话批量路径
        self.db.get_duplicate_fingerprint_groups.return_value = [
            ("test_m1", "fp1", str(self.b_dir / "f1.strm"))]

        with patch.object(self.app, "_b_lineage_preflight", return_value=True), \
             patch.object(self.app, "_refresh_identities_batch", side_effect=lambda *args, **kwargs: call_order.append("refresh_projection")), \
             patch.object(self.app, "_quarantine_duplicate_groups_batch", side_effect=lambda *args, **kwargs: call_order.append("dedup")):

            disk_data = (
                {"fp1": {str(self.b_dir / "f1.strm")}},
                {str(self.b_dir / "f1.strm"): {"webdav": "/mount/f1.mp4", "fp": "fp1"}},
            )
            processed = set()
            self.app._insert_new_b_records(disk_data, processed)

        # 严格验证执行时序：先 batch_write，再 refresh_projection，最后锁外 dedup
        assert call_order == ["batch_write", "refresh_projection", "dedup"]

    def test_insert_new_b_records_failure_no_dedup_no_quarantine(self):
        """Task 2 (R5): 批写失败时整批回滚并上抛，不刷新身份、不执行 dedup 隔离改名，既有家族与磁盘原样保留。"""
        b1 = self.b_dir / "f1.strm"
        b2 = self.b_dir / "f2.strm"
        fp = "fp_family"
        webdav = "/mount/family.mp4"
        b1.write_text(webdav, encoding="utf-8")
        b2.write_text(webdav, encoding="utf-8")

        # 批写注入 SQL 错误（模拟磁盘满/杀软锁）
        self.db.upsert_b_records_and_snapshots_batch.side_effect = RuntimeError("disk full simulation")

        with patch.object(self.app, "_b_lineage_preflight", return_value=True), \
             patch.object(self.app, "ensure_single_visible_instance") as mock_ensure, \
             patch.object(self.app, "refresh_identity_current_b_path") as mock_refresh, \
             patch("app_service_core.quarantine_file") as mock_qf:

            disk_data = (
                {fp: {str(b1), str(b2)}},
                {str(b1): {"webdav": webdav, "fp": fp}, str(b2): {"webdav": webdav, "fp": fp}},
            )
            processed = set()
            with pytest.raises(RuntimeError) as excinfo:
                self.app._insert_new_b_records(disk_data, processed)

            assert "disk full simulation" in str(excinfo.value)

        # 断言无任何去重/隔离改名副作用（R5 机制）
        mock_ensure.assert_not_called()
        mock_qf.assert_not_called()
        mock_refresh.assert_not_called()
        # 磁盘原样保留，无 .duplicate 文件
        assert b1.exists()
        assert b2.exists()
        assert not list(self.b_dir.rglob("*.duplicate"))

    def test_insert_new_b_records_count_mismatch_raises(self):
        """Task 2: 批写返回持久化行数 != 本批行数时抛出 RuntimeError。"""
        b1 = self.b_dir / "f1.strm"
        b1.write_text("/mount/f1.mp4", encoding="utf-8")

        # 模拟返回成功行数不足（例如静默丢失）
        self.db.upsert_b_records_and_snapshots_batch.return_value = 0

        with patch.object(self.app, "_b_lineage_preflight", return_value=True):
            disk_data = (
                {"fp1": {str(b1)}},
                {str(b1): {"webdav": "/mount/f1.mp4", "fp": "fp1"}},
            )
            with pytest.raises(RuntimeError) as excinfo:
                self.app._insert_new_b_records(disk_data, set())
            assert "批写数量不符" in str(excinfo.value)


class TestQuarantineDuplicateGroupsBatch:
    """T1：启动期隔离会话批量化（quarantine_session + _quarantine_duplicate_groups_batch）。

    真实 Database + 真实文件沙盒。覆盖定向规格：成功隔离终态等价 /
    组事务失败回滚 + B3-B 镜像 / 目标冲突恢复（C7）/ 改名失败置 valid /
    空组跳过 / mapping 隔离 / watcher 活跃回退 / 预筛失败回退（C13-4）/
    双入队消除。
    """

    def setup_method(self):
        self._sandbox_tmps: list[Path] = []
        self.db, self.app, self.b_dir, self.a_dir = self._make_sandbox("main")

    def _make_sandbox(self, name: str, mapping_ids: tuple[str, ...] = ("test_m1",)):
        tmp = Path(tempfile.mkdtemp(prefix=f"batch_quar_{name}_"))
        self._sandbox_tmps.append(tmp)
        a_dirs = [tmp / f"a{i+1}" for i in range(len(mapping_ids))]
        b_dirs = [tmp / f"b{i+1}" for i in range(len(mapping_ids))]
        c_dir = tmp / "c"
        for d in (*a_dirs, *b_dirs, c_dir):
            d.mkdir(parents=True, exist_ok=True)
        db = Database(str(tmp / "bridge.db"))
        config = Mock(spec=AppConfig)
        config.a_folders = [str(a_dirs[0])]
        config.a_b_mappings = [
            ABMapping(mapping_id=mid, a_root=str(a_dirs[i]), b_root=str(b_dirs[i]))
            for i, mid in enumerate(mapping_ids)]
        config.paths = Mock()
        config.paths.b_root = str(b_dirs[0])
        config.paths.c_root = str(c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []
        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            app = AppService(config, db, Mock())
        return db, app, b_dirs[0], a_dirs[0]

    def teardown_method(self):
        for tmp in self._sandbox_tmps:
            shutil.rmtree(tmp, ignore_errors=True)

    def _seed_instance(self, webdav: str, name: str, show: str = "dup_show",
                       status: str = "valid", mapping_id: str = "test_m1") -> Path:
        p = self.b_dir / show / "Season 01" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(webdav, encoding="utf-8")
        self.db.upsert_b(
            str(p), webdav, f"/cloud/{show}", None,
            fingerprint=make_strm_fingerprint(webdav),
            status=status, mapping_id=mapping_id)
        return p

    def _rows(self, fp: str, mapping_id: str = "test_m1") -> dict[str, str]:
        return {
            r.local_path: r.status
            for r in self.db.get_all_b_by_fingerprint(fp, mapping_id)}

    def _fts_aligned(self) -> bool:
        with self.db.read_connection() as conn:
            fts = conn.execute("SELECT count(*) FROM b_strm_files_fts").fetchone()[0]
            main = conn.execute("SELECT count(*) FROM b_strm_files").fetchone()[0]
        return fts == main

    def _terminal_signature(self, fp: str, mapping_id: str = "test_m1"):
        rows = sorted(
            (Path(p).name, s) for p, s in self._rows(fp, mapping_id).items())
        files = sorted(
            p.name for p in self.b_dir.rglob("*") if p.is_file())
        return rows, files

    def test_batch_success_terminal_equivalent_to_per_item(self):
        """会话批量隔离与现行逐条 ensure 终态等价（含 FTS 行数对齐）。"""
        webdav = "/cloud/dup_show/S01E01.mp4"
        fp = make_strm_fingerprint(webdav)
        f1 = self._seed_instance(webdav, "a.strm")
        f2 = self._seed_instance(webdav, "b.strm")
        # 对照沙盒：同一初始状态走现行逐条路径
        db2, app2, b_dir2, _a2 = self._make_sandbox("peritem")
        p1 = b_dir2 / "dup_show" / "Season 01" / "a.strm"
        p2 = b_dir2 / "dup_show" / "Season 01" / "b.strm"
        for p in (p1, p2):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(webdav, encoding="utf-8")
            db2.upsert_b(str(p), webdav, "/cloud/dup_show", None,
                         fingerprint=fp, status="valid", mapping_id="test_m1")
        app2.ensure_single_visible_instance(fp, str(p1), mapping_id="test_m1")

        moved = self.app._quarantine_duplicate_groups_batch(
            [("test_m1", fp, str(f1))])
        assert moved == 1
        # 每组恰 1 valid + 其余 .duplicate + DB 行迁移
        rows = self._rows(fp)
        dup_rows = [p for p in rows if p.endswith(".duplicate")]
        assert len(dup_rows) == 1
        assert rows[dup_rows[0]] == "duplicate"
        assert Path(dup_rows[0]).exists()
        kept = [p for p in rows if not p.endswith(".duplicate")]
        assert len(kept) == 1 and rows[kept[0]] == "valid"
        # FTS 行数对齐
        assert self._fts_aligned()
        # 与现行逐条路径终态一致（文件名+状态签名）
        rows2 = {
            r.local_path: r.status
            for r in db2.get_all_b_by_fingerprint(fp, "test_m1")}
        sig_batch = sorted(
            (Path(p).name, s) for p, s in rows.items())
        sig_peritem = sorted(
            (Path(p).name, s) for p, s in rows2.items())
        assert sig_batch == sig_peritem

    def test_group_transaction_failure_rolls_back_and_continues(self):
        """组事务 SQL 异常 → 整组回滚 + 物理回滚改名 + 后续组继续 + ERROR。"""
        import sqlite3 as _s3
        from database import _QuarantineSession
        webdav1 = "/cloud/show1/S01E01.mp4"
        webdav2 = "/cloud/show2/S01E01.mp4"
        fp1 = make_strm_fingerprint(webdav1)
        fp2 = make_strm_fingerprint(webdav2)
        f1 = self._seed_instance(webdav1, "a.strm", show="show1")
        f2 = self._seed_instance(webdav1, "b.strm", show="show1")
        g1 = self._seed_instance(webdav2, "a.strm", show="show2")
        g2 = self._seed_instance(webdav2, "b.strm", show="show2")
        groups = [("test_m1", fp1, str(f1)), ("test_m1", fp2, str(g1))]
        real_run_group = _QuarantineSession.run_group
        calls = {"n": 0}

        def flaky_run_group(session, fingerprint, mapping_id, keep, renamed):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _s3.Error("injected group failure")
            return real_run_group(session, fingerprint, mapping_id, keep, renamed)

        with patch.object(_QuarantineSession, "run_group", flaky_run_group):
            moved = self.app._quarantine_duplicate_groups_batch(groups)
        assert moved == 1  # 第二组成功
        # 第一组：物理回滚 → 两文件回 .strm、DB 行 valid、无 .duplicate
        rows1 = self._rows(fp1)
        assert all(s == "valid" for s in rows1.values())
        assert f1.exists() and f2.exists()
        assert not list((self.b_dir / "show1").rglob("*.duplicate"))
        # 第二组：正常隔离（1 valid + 1 duplicate）
        rows2 = self._rows(fp2)
        assert len([p for p in rows2 if p.endswith(".duplicate")]) == 1
        assert self._fts_aligned()

    def test_group_failure_with_unrecoverable_rename_aligns_db(self):
        """组失败且回滚改名失败 → 独立连接 move_b_record 对齐 DB（B3-B 镜像）。"""
        import sqlite3 as _s3
        from database import _QuarantineSession
        webdav = "/cloud/dup_show/S01E01.mp4"
        fp = make_strm_fingerprint(webdav)
        f1 = self._seed_instance(webdav, "a.strm")
        f2 = self._seed_instance(webdav, "b.strm")
        real_qf = app_service_core.quarantine_file

        def blocking_qf(path, suffix=".invalid"):
            res = real_qf(path, suffix=suffix)
            if res is not None:
                # 原路径放置阻挡文件使组回滚 rename 失败（Windows 目标已存在 → OSError）
                Path(path).write_text("blocked", encoding="utf-8")
            return res

        with patch("app_service_core.quarantine_file", blocking_qf), \
             patch.object(_QuarantineSession, "run_group",
                          side_effect=_s3.Error("injected group failure")):
            self.app._quarantine_duplicate_groups_batch(
                [("test_m1", fp, str(f1))])
        # B3-B 镜像：DB 对齐到 .duplicate 路径且状态 duplicate
        rows = self._rows(fp)
        dup_rows = [p for p in rows if p.endswith(".duplicate")]
        assert len(dup_rows) == 1
        assert rows[dup_rows[0]] == "duplicate"
        assert Path(dup_rows[0]).exists()

    def test_move_conflict_instance_recovered_and_group_continues(self):
        """_move_b_record_on_conn 返回 False（目标冲突）→ 回滚改名 + 置 valid + 组内其余成功（C7）。"""
        import database as _dbmod
        webdav = "/cloud/dup_show/S01E01.mp4"
        fp = make_strm_fingerprint(webdav)
        f1 = self._seed_instance(webdav, "a.strm")
        self._seed_instance(webdav, "b.strm")
        self._seed_instance(webdav, "c.strm")
        real_move = _dbmod._move_b_record_on_conn
        calls = {"n": 0}

        def flaky_move(conn, old, new):
            calls["n"] += 1
            if calls["n"] == 1:
                return False  # 注入目标冲突
            return real_move(conn, old, new)

        with patch("database._move_b_record_on_conn", flaky_move):
            moved = self.app._quarantine_duplicate_groups_batch(
                [("test_m1", fp, str(f1))])
        assert moved == 1
        rows = self._rows(fp)
        dup_rows = [p for p in rows if p.endswith(".duplicate")]
        assert len(dup_rows) == 1 and rows[dup_rows[0]] == "duplicate"
        valid_rows = [p for p in rows if not p.endswith(".duplicate")]
        # keep + 冲突恢复实例均 valid 且物理存在
        assert len(valid_rows) == 2
        assert all(rows[p] == "valid" and Path(p).exists() for p in valid_rows)
        assert self._fts_aligned()

    def test_rename_failure_marks_instance_valid(self):
        """quarantine_file 返回 None → 该实例置回 valid、原 .strm 保留。"""
        webdav = "/cloud/dup_show/S01E01.mp4"
        fp = make_strm_fingerprint(webdav)
        f1 = self._seed_instance(webdav, "a.strm")
        f2 = self._seed_instance(webdav, "b.strm")
        real_qf = app_service_core.quarantine_file

        def failing_qf(path, suffix=".invalid"):
            if Path(path).name == "b.strm":
                return None  # 模拟 rename OSError
            return real_qf(path, suffix=suffix)

        with patch("app_service_core.quarantine_file", failing_qf):
            moved = self.app._quarantine_duplicate_groups_batch(
                [("test_m1", fp, str(f1))])
        assert moved == 0
        rows = self._rows(fp)
        assert rows[str(f2)] == "valid"
        assert f2.exists()
        assert not list(self.b_dir.rglob("*.duplicate"))
        assert rows[str(f1)] == "valid"

    def test_group_without_valid_existing_skipped(self):
        """全组无 valid+existing 实例 → 整组跳过（不调用 run_group/不改名）。"""
        from database import _QuarantineSession
        webdav = "/cloud/dup_show/S01E01.mp4"
        fp = make_strm_fingerprint(webdav)
        f1 = self._seed_instance(webdav, "a.strm", status="duplicate")
        f2 = self._seed_instance(webdav, "b.strm", status="duplicate")
        with patch.object(_QuarantineSession, "run_group") as mock_rg:
            moved = self.app._quarantine_duplicate_groups_batch(
                [("test_m1", fp, str(f1))])
        assert moved == 0
        mock_rg.assert_not_called()
        assert f1.exists() and f2.exists()
        assert not list(self.b_dir.rglob("*.duplicate"))

    def test_mapping_isolation_dual_mapping(self):
        """双 mapping 同指纹不互相隔离：m1 组隔离不影响 m2 单实例。"""
        db2, app2, b_dir2, _a2 = self._make_sandbox(
            "dual", mapping_ids=("m1", "m2"))
        webdav = "/cloud/dup_show/S01E01.mp4"
        fp = make_strm_fingerprint(webdav)

        def _seed(b_root, mapping_id, name):
            p = b_root / "dup_show" / "Season 01" / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(webdav, encoding="utf-8")
            db2.upsert_b(str(p), webdav, "/cloud/dup_show", None,
                         fingerprint=fp, status="valid", mapping_id=mapping_id)
            return p

        b1_root = b_dir2  # m1 的 B 根
        b2_root = b_dir2.parent / "b2"  # m2 的 B 根
        m1_a = _seed(b1_root, "m1", "a.strm")
        _seed(b1_root, "m1", "b.strm")
        m2_solo = _seed(b2_root, "m2", "solo.strm")

        moved = app2._quarantine_duplicate_groups_batch([("m1", fp, str(m1_a))])
        assert moved == 1
        # m1：1 valid + 1 duplicate
        rows_m1 = {
            r.local_path: r.status
            for r in db2.get_all_b_by_fingerprint(fp, "m1")}
        assert len([p for p in rows_m1 if p.endswith(".duplicate")]) == 1
        # m2 单实例完全不受影响
        rows_m2 = {
            r.local_path: r.status
            for r in db2.get_all_b_by_fingerprint(fp, "m2")}
        assert rows_m2 == {str(m2_solo): "valid"}
        assert m2_solo.exists()
        assert not list(b2_root.rglob("*.duplicate"))

    def _seed_insert_new_pair(self, webdav: str):
        """为 _insert_new_b_records 直测准备 A 源 + 同指纹双实例 B 文件。"""
        fp = make_strm_fingerprint(webdav)
        a_src = self.a_dir / "dup_show" / "Season 01" / "a.strm"
        a_src.parent.mkdir(parents=True, exist_ok=True)
        a_src.write_text(webdav, encoding="utf-8")
        self.db.upsert_a(str(a_src), webdav, "/cloud/dup_show")
        f1 = self.b_dir / "dup_show" / "Season 01" / "a.strm"
        f2 = self.b_dir / "dup_show" / "Season 01" / "b.strm"
        for p in (f1, f2):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(webdav, encoding="utf-8")
        return fp, f1, f2

    def test_double_queue_single_run_group(self):
        """双入队消除：一组两文件入队 → run_group 恰一次（真实 DB 全流程）。"""
        from database import _QuarantineSession
        webdav = "/cloud/dup_show/S01E01.mp4"
        fp, f1, f2 = self._seed_insert_new_pair(webdav)
        real_run_group = _QuarantineSession.run_group
        calls: list[int] = []

        def spy_run_group(session, fingerprint, mapping_id, keep, renamed):
            calls.append(1)
            return real_run_group(session, fingerprint, mapping_id, keep, renamed)

        with patch.object(_QuarantineSession, "run_group", spy_run_group):
            disk_data = (
                {fp: {str(f1), str(f2)}},
                {str(f1): {"webdav": webdav, "fp": fp},
                 str(f2): {"webdav": webdav, "fp": fp}},
            )
            self.app._insert_new_b_records(disk_data, set())
        assert len(calls) == 1
        rows = self._rows(fp)
        assert len([p for p in rows if p.endswith(".duplicate")]) == 1

    def test_insert_new_falls_back_when_watchers_live(self):
        """_watchers_live() 为 True → _insert_new_b_records 回退逐条（不创建会话）。"""
        webdav = "/cloud/dup_show/S01E01.mp4"
        fp, f1, f2 = self._seed_insert_new_pair(webdav)
        # observer 存根：is_alive → True（watcher 活跃）
        self.app.observer = Mock()
        self.app.observer.is_alive.return_value = True
        with patch.object(self.db, "quarantine_session") as mock_session, \
             patch.object(self.app, "ensure_single_visible_instance",
                          wraps=self.app.ensure_single_visible_instance) as mock_ensure:
            disk_data = (
                {fp: {str(f1), str(f2)}},
                {str(f1): {"webdav": webdav, "fp": fp},
                 str(f2): {"webdav": webdav, "fp": fp}},
            )
            self.app._insert_new_b_records(disk_data, set())
        mock_session.assert_not_called()
        # 现行逐条路径：两个队列项各触发一次 ensure（双入队现行行为）
        assert mock_ensure.call_count == 2
        # 去重仍完成
        rows = self._rows(fp)
        assert len([p for p in rows if p.endswith(".duplicate")]) == 1

    def test_prefilter_query_failure_falls_back_per_item(self):
        """C13-4：预筛查询抛异常 → 回退现行逐条 ensure，无异常冒泡、去重仍完成。"""
        webdav = "/cloud/dup_show/S01E01.mp4"
        fp, f1, f2 = self._seed_insert_new_pair(webdav)
        with patch.object(self.db, "get_duplicate_fingerprint_groups",
                          side_effect=RuntimeError("prefilter boom")), \
             patch.object(self.app, "ensure_single_visible_instance",
                          wraps=self.app.ensure_single_visible_instance) as mock_ensure:
            disk_data = (
                {fp: {str(f1), str(f2)}},
                {str(f1): {"webdav": webdav, "fp": fp},
                 str(f2): {"webdav": webdav, "fp": fp}},
            )
            self.app._insert_new_b_records(disk_data, set())  # 不冒泡
        assert mock_ensure.call_count == 2  # 全量逐条回退
        rows = self._rows(fp)
        assert len([p for p in rows if p.endswith(".duplicate")]) == 1

    def test_cleanup_startup_duplicates_uses_batch_when_watchers_not_live(self):
        """_cleanup_startup_duplicates 复用自身 GROUP BY 结果走批量路径（C12-2）。"""
        webdav = "/cloud/dup_show/S01E01.mp4"
        fp = make_strm_fingerprint(webdav)
        f1 = self._seed_instance(webdav, "a.strm")
        self._seed_instance(webdav, "b.strm")
        with patch.object(
                self.app, "_quarantine_duplicate_groups_batch",
                wraps=self.app._quarantine_duplicate_groups_batch) as mock_batch, \
             patch.object(self.db, "get_duplicate_fingerprint_groups",
                          wraps=self.db.get_duplicate_fingerprint_groups) as mock_groups:
            self.app._cleanup_startup_duplicates()
        mock_groups.assert_called_once()  # 不二次查询
        mock_batch.assert_called_once()
        args = mock_batch.call_args[0][0]
        assert args == [("test_m1", fp, str(f1))]
        rows = self._rows(fp)
        assert len([p for p in rows if p.endswith(".duplicate")]) == 1


class TestBLineagePreflight:
    """T2b：血统预检提取等价与并行等价。

    提取不变量：
    - preflight True ⇒ 完整 _verify_b_path_lineage 必 True（步骤 1-4 放行即终放行）；
    - preflight False（步骤 1 失败：无 A 源）⇒ 完整校验必 False（确定性重跑同结果）；
    - 并行预检 (ok, stat) 表与串行逐条结果一致；
    - 越界新增文件（无 A 源）在 _insert_new_b_records 仍被物理删除（wave2 串行段）。
    """

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="preflight_"))
        self.a_dir = self.tmp / "a"
        self.b_dir = self.tmp / "b"
        self.c_dir = self.tmp / "c"
        for d in (self.a_dir, self.b_dir, self.c_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.db = Database(str(self.tmp / "bridge.db"))
        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.a_b_mappings = [
            ABMapping(mapping_id="test_m1", a_root=str(self.a_dir), b_root=str(self.b_dir))]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []
        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, self.db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_pair(self, webdav: str, name: str = "a.strm", show: str = "show"):
        """播种 A 源 + 同构 B 文件并入库 A 记录（基础血统通过）。"""
        a_src = self.a_dir / show / "Season 01" / name
        a_src.parent.mkdir(parents=True, exist_ok=True)
        a_src.write_text(webdav, encoding="utf-8")
        self.db.upsert_a(str(a_src), webdav, f"/cloud/{show}")
        b_file = self.b_dir / show / "Season 01" / name
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text(webdav, encoding="utf-8")
        return b_file

    def test_preflight_true_implies_verify_true(self):
        webdav = "/cloud/show/S01E01.mp4"
        b_file = self._seed_pair(webdav)
        assert self.app._b_lineage_preflight(str(b_file), webdav) is True
        assert self.app._verify_b_path_lineage(str(b_file), webdav) is True

    def test_preflight_step1_failure_implies_verify_false(self):
        webdav = "/cloud/ghost/S01E01.mp4"
        b_file = self.b_dir / "ghost" / "Season 01" / "a.strm"
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text(webdav, encoding="utf-8")
        # 无 A 源：预检步骤 1 失败 → 完整校验同样 False（确定性重跑）
        assert self.app._b_lineage_preflight(str(b_file), webdav) is False
        assert self.app._verify_b_path_lineage(str(b_file), webdav) is False

    def test_parallel_preflight_matches_sequential(self):
        files = []
        for i in range(12):
            webdav = f"/cloud/show{i}/S01E01.mp4"
            b_file = self._seed_pair(webdav, show=f"show{i}")
            files.append((str(b_file), webdav))
        # 混入无 A 源文件（预检 False 路径）
        orphan = self.b_dir / "orphan" / "Season 01" / "x.strm"
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_text("/cloud/orphan/S01E01.mp4", encoding="utf-8")
        files.append((str(orphan), "/cloud/orphan/S01E01.mp4"))

        ok_map, stat_map, resolved_map = self.app._preflight_b_lineage_parallel(files)
        for path, webdav in files:
            assert ok_map[path] == self.app._b_lineage_preflight(path, webdav)
            assert (stat_map[path] is not None) == Path(path).exists()
            # T5: resolved_map 提供已 resolve 规范路径（wave2 fast mapping 复用）
            assert resolved_map[path] == Path(path).resolve()

    def test_fast_mapping_resolved_target_equivalence(self):
        """T5：_resolved_target 透传与默认 normalize_local_root 路径结果等价。"""
        self.app.ensure_scan_mapping_roots()
        # 预计算根已初始化（fast 生效）
        assert self.app._scan_mapping_roots is not None
        samples = [
            (self.a_dir / "show" / "Season 01" / "a.strm", "a"),
            (self.b_dir / "show" / "Season 01" / "a.strm", "b"),
            (self.tmp / "outside.strm", "b"),  # 根外路径 → 零命中 fail-closed
        ]
        for path, kind in samples:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("/w/x.mp4", encoding="utf-8")
            resolved = Path(path).resolve()
            if kind == "a":
                default_result = self.app._get_mapping_for_a_fast(path)
                resolved_result = self.app._get_mapping_for_a_fast(
                    path, _resolved_target=resolved)
            else:
                default_result = self.app._get_mapping_for_b_fast(path)
                resolved_result = self.app._get_mapping_for_b_fast(
                    path, _resolved_target=resolved)
            assert resolved_result == default_result, (
                f"_resolved_target 透传结果不一致: {path}")
        # 根外路径 fail-closed（两种调用形态一致为 None）
        assert self.app._get_mapping_for_b_fast(
            self.tmp / "outside.strm") is None
        assert self.app._get_mapping_for_b_fast(
            self.tmp / "outside.strm",
            _resolved_target=(self.tmp / "outside.strm").resolve()) is None

    def test_insert_new_deletes_out_of_bounds_file(self):
        """越界新增文件（无 A 源）经 wave2 串行回退物理删除、不入库。"""
        webdav = "/cloud/ghost/S01E01.mp4"
        fp = make_strm_fingerprint(webdav)
        b_file = self.b_dir / "ghost" / "Season 01" / "a.strm"
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text(webdav, encoding="utf-8")
        disk_data = (
            {fp: {str(b_file)}},
            {str(b_file): {"webdav": webdav, "fp": fp}},
        )
        self.app._insert_new_b_records(disk_data, set())
        assert not b_file.exists(), "越界文件应被物理删除"
        assert self.db.get_b_by_local(str(b_file)) is None

    def test_insert_new_mixed_files_terminal_state(self):
        """混合 fixture：合法文件入库、越界文件删除、顺序无关（终态断言）。"""
        webdav_ok = "/cloud/show/S01E01.mp4"
        ok_file = self._seed_pair(webdav_ok)
        webdav_bad = "/cloud/ghost/S01E01.mp4"
        fp_bad = make_strm_fingerprint(webdav_bad)
        bad_file = self.b_dir / "ghost" / "Season 01" / "b.strm"
        bad_file.parent.mkdir(parents=True, exist_ok=True)
        bad_file.write_text(webdav_bad, encoding="utf-8")
        disk_data = (
            {make_strm_fingerprint(webdav_ok): {str(ok_file)},
             fp_bad: {str(bad_file)}},
            {str(ok_file): {"webdav": webdav_ok, "fp": make_strm_fingerprint(webdav_ok)},
             str(bad_file): {"webdav": webdav_bad, "fp": fp_bad}},
        )
        self.app._insert_new_b_records(disk_data, set())
        assert ok_file.exists()
        assert self.db.get_b_by_local(str(ok_file)) is not None
        assert not bad_file.exists()
        assert self.db.get_b_by_local(str(bad_file)) is None
        # 快照仅对合法文件写入
        assert self.db.get_b_lineage_snapshot("test_m1", str(ok_file)) is not None
        assert self.db.get_b_lineage_snapshot("test_m1", str(bad_file)) is None


# ===========================================================================
# P1：B 区历史核对 Wave0/1/2（镜像 _insert_new_b_records 并行预检模式）
# ===========================================================================


class TestReconcileWavePreflight:
    """v6 P1：reconcile 拆两波——wave1 并行预检放行免完整校验；未放行串行回退。

    等价性前提（计划 §四 Step 5 登记）：`_b_lineage_preflight` True ⇒
    `_verify_b_path_lineage` True（verify 顶部即调预检并返回），副作用面
    （越界删除/迁移/单集 Timer/边界写）严格留主线程串行段（X8）。
    """

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="reconcile_wave_"))
        self.a_dir = self.tmp / "a"
        self.b_dir = self.tmp / "b"
        self.c_dir = self.tmp / "c"
        for d in (self.a_dir, self.b_dir, self.c_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.db = Database(str(self.tmp / "bridge.db"))
        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.a_b_mappings = [
            ABMapping(mapping_id="test_m1", a_root=str(self.a_dir), b_root=str(self.b_dir))]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []
        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, self.db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_pair(self, webdav: str, name: str = "a.strm", show: str = "show") -> Path:
        """播种 A 源 + 同构 B 文件 + A 记录 + B 行（分支 B 合法形态）。"""
        a_src = self.a_dir / show / "Season 01" / name
        a_src.parent.mkdir(parents=True, exist_ok=True)
        a_src.write_text(webdav, encoding="utf-8")
        self.db.upsert_a(str(a_src), webdav, f"/cloud/{show}")
        b_file = self.b_dir / show / "Season 01" / name
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text(webdav, encoding="utf-8")
        self.db.upsert_b(
            str(b_file), webdav, f"/cloud/{show}", str(a_src),
            mapping_id="test_m1", fingerprint=make_strm_fingerprint(webdav))
        return b_file

    def _seed_orphan_b(self, webdav: str, name: str = "x.strm", show: str = "ghost") -> Path:
        """仅 B 文件 + B 行，无 A 源——预检 False 形态（wave2 完整校验回退）。"""
        b_file = self.b_dir / show / "Season 01" / name
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text(webdav, encoding="utf-8")
        self.db.upsert_b(
            str(b_file), webdav, f"/cloud/{show}", None,
            mapping_id="test_m1", fingerprint=make_strm_fingerprint(webdav))
        return b_file

    def _run_reconcile(self, **kwargs):
        disk_data = self.app._scan_b_disk()
        db_records = self.db.get_all_b_records()
        processed: set[str] = set()
        self.app._reconcile_b_historical_records(
            disk_data, db_records, processed, **kwargs)
        return processed

    def _terminal_digest(self) -> dict:
        """差分等价 digest：B 行集、快照集、磁盘 .strm 集（X10 排除隔离后缀）。"""
        b_rows = sorted(
            (r.local_path, r.webdav_path, r.fingerprint or "", r.mapping_id, r.status)
            for r in self.db.get_all_b_records())
        snaps = sorted(
            (s.local_path, s.fingerprint, s.validation_state, s.mapping_version)
            for s in self.db.get_all_lineage_snapshots("test_m1"))
        disk = sorted(
            str(p) for p in self.b_dir.rglob("*.strm")
            if ".duplicate" not in p.name and ".invalid" not in p.name)
        return {"b": b_rows, "snap": snaps, "disk": disk}

    def test_wave1_preflight_pass_skips_serial_verify(self):
        """同路径同指纹、无快照 → 预检放行：不调完整校验，processed+快照。"""
        webdav = "/cloud/show/S01E01.mp4"
        b_file = self._seed_pair(webdav)
        with patch.object(
                self.app, "_verify_b_path_lineage",
                side_effect=AssertionError("预检放行后不应调用完整校验")) as mock_verify:
            processed = self._run_reconcile()
        mock_verify.assert_not_called()
        assert str(b_file) in processed
        snap = self.db.get_b_lineage_snapshot("test_m1", str(b_file))
        assert snap is not None and snap.validation_state == "valid"

    def test_wave1_preflight_fail_falls_back_to_serial_verify(self):
        """预检未放行 → wave2 串行完整校验被行使；False → 越界删除 + DB 清理。"""
        webdav = "/cloud/ghost/S01E01.mp4"
        b_file = self._seed_orphan_b(webdav)
        with patch.object(
                self.app, "_verify_b_path_lineage",
                wraps=self.app._verify_b_path_lineage) as mock_verify:
            processed = self._run_reconcile()
        # 回退段必须行使完整校验（C9 双钉关注点）
        mock_verify.assert_called_once_with(str(b_file), webdav)
        assert str(b_file) in processed
        assert not b_file.exists(), "预检 False 且完整校验 False → 越界物理删除"
        assert self.db.get_b_by_local(str(b_file)) is None

    def test_snapshot_reuse_short_circuit_before_preflight(self):
        """有效快照命中 → Wave0 直接 processed，不进预检清单。"""
        webdav = "/cloud/show/S01E01.mp4"
        b_file = self._seed_pair(webdav)
        self.app._store_valid_lineage_snapshot(str(b_file), make_strm_fingerprint(webdav))
        with patch.object(self.app, "_verify_b_path_lineage") as mock_verify, \
             patch.object(self.app, "_b_lineage_preflight") as mock_pf:
            processed = self._run_reconcile()
        mock_verify.assert_not_called()
        mock_pf.assert_not_called()
        assert str(b_file) in processed

    def test_force_full_disables_snapshot_reuse_and_preflights(self):
        """F7：force_full=True 不做快照复用判定，全部行进预检（预检仍放行免 verify）。"""
        webdav = "/cloud/show/S01E01.mp4"
        b_file = self._seed_pair(webdav)
        self.app._store_valid_lineage_snapshot(str(b_file), make_strm_fingerprint(webdav))
        with patch.object(self.app, "_verify_b_path_lineage") as mock_verify:
            processed = self._run_reconcile(force_full=True)
        mock_verify.assert_not_called()  # 预检放行
        assert str(b_file) in processed

    def test_wave_design_differential_equivalence_vs_serial_fallback(self):
        """差分等价：自然波次 vs 强制预检全 False（纯串行回退）终态一致。

        构造混合 fixture：预检放行行 + 预检未放行行（含越界删除与合法迁移）。
        强制回退侧完整校验触发次数 == 预检清单长度（与重构前串行口径一致）。
        """
        def build(case_tmp: Path) -> dict:
            a_dir = case_tmp / "a"
            b_dir = case_tmp / "b"
            webdav_ok = "/cloud/ok/S01E01.mp4"
            webdav_ghost = "/cloud/ghost/S01E02.mp4"
            # 合法同构行
            a_ok = a_dir / "ok" / "Season 01" / "e1.strm"
            a_ok.parent.mkdir(parents=True)
            a_ok.write_text(webdav_ok, encoding="utf-8")
            b_ok = b_dir / "ok" / "Season 01" / "e1.strm"
            b_ok.parent.mkdir(parents=True)
            b_ok.write_text(webdav_ok, encoding="utf-8")
            # 越界孤行（无 A 源）
            b_ghost = b_dir / "ghost" / "Season 01" / "e2.strm"
            b_ghost.parent.mkdir(parents=True)
            b_ghost.write_text(webdav_ghost, encoding="utf-8")
            db = Database(str(case_tmp / "bridge.db"))
            db.upsert_a(str(a_ok), webdav_ok, "/cloud/ok")
            db.upsert_b(str(b_ok), webdav_ok, "/cloud/ok", str(a_ok),
                        mapping_id="test_m1", fingerprint=make_strm_fingerprint(webdav_ok))
            db.upsert_b(str(b_ghost), webdav_ghost, "/cloud/ghost", None,
                        mapping_id="test_m1", fingerprint=make_strm_fingerprint(webdav_ghost))
            return {"db": db, "b_ok": b_ok, "b_ghost": b_ghost}

        results = []
        for mode in ("natural", "forced-serial"):
            tmp = Path(tempfile.mkdtemp(prefix="reconcile_diff_"))
            try:
                (tmp / "a").mkdir()
                (tmp / "b").mkdir()
                (tmp / "c").mkdir()
                seeded = build(tmp)
                config = Mock(spec=AppConfig)
                config.a_folders = [str(tmp / "a")]
                config.a_b_mappings = [ABMapping(
                    mapping_id="test_m1", a_root=str(tmp / "a"), b_root=str(tmp / "b"))]
                config.paths = Mock()
                config.paths.b_root = str(tmp / "b")
                config.paths.c_root = str(tmp / "c")
                config.behavior = Mock()
                config.behavior.ghost_protect_seconds = 300
                config.strm_engine_paths = []
                with patch("app_service_core.RefreshService"), \
                     patch("app_service_core.SyncService"), \
                     patch("app_service_core.SubtitleHandler"):
                    app = AppService(config, seeded["db"], Mock())
                serial_calls: list[str] = []
                if mode == "forced-serial":
                    def _all_false(files):
                        return ({p: False for p, _ in files},
                                {p: (Path(p).stat() if Path(p).exists() else None)
                                 for p, _ in files},
                                {p: Path(p).resolve() for p, _ in files})
                    orig_verify = app._verify_b_path_lineage

                    def _spy_verify(path, webdav, is_sync_phase=False):
                        serial_calls.append(path)
                        return orig_verify(path, webdav, is_sync_phase=is_sync_phase)
                    with patch.object(app, "_preflight_b_lineage_parallel", side_effect=_all_false), \
                         patch.object(app, "_verify_b_path_lineage", side_effect=_spy_verify):
                        disk_data = app._scan_b_disk()
                        app._reconcile_b_historical_records(
                            disk_data, seeded["db"].get_all_b_records(), set())
                    # 串行回退触发次数 == 预检清单长度（2 条 B 行）
                    assert len(serial_calls) == 2, (
                        "强制回退侧每条预检 False 行都必须行使完整校验")
                else:
                    disk_data = app._scan_b_disk()
                    app._reconcile_b_historical_records(
                        disk_data, seeded["db"].get_all_b_records(), set())
                b_rows = sorted(
                    (r.local_path.replace(str(tmp), "<T>"),
                     r.webdav_path, r.fingerprint or "", r.status)
                    for r in seeded["db"].get_all_b_records())
                snaps = sorted(
                    (s.local_path.replace(str(tmp), "<T>"), s.fingerprint, s.validation_state)
                    for s in seeded["db"].get_all_lineage_snapshots("test_m1"))
                disk_files = sorted(
                    str(p).replace(str(tmp), "<T>") for p in (tmp / "b").rglob("*.strm"))
                results.append({"b": b_rows, "snap": snaps, "disk": disk_files})
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        assert results[0] == results[1], (
            f"波次终态与纯串行回退不一致:\nnatural={results[0]}\nserial={results[1]}")
        # 越界文件在两种形态下都被物理删除
        assert not any("ghost" in p for p in results[0]["disk"])
        # 合法行保留、快照在位
        assert any("e1.strm" in row[0] for row in results[0]["b"])
        assert any("e1.strm" in s[0] for s in results[0]["snap"])


class TestFastMappingDifferentialEquivalence:
    """Task 3: fast mapping 与权威 mapping 的差分等价性与回退规格。"""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a1 = Path(self.tmp) / "A1"
        self.b1 = Path(self.tmp) / "B1"
        self.a2 = Path(self.tmp) / "A2"
        self.b2 = Path(self.tmp) / "B2"
        for d in [self.a1, self.b1, self.a2, self.b2]:
            d.mkdir()

        self.c1 = Path(self.tmp) / "C"
        self.c1.mkdir()
        config = Mock(spec=AppConfig)
        config.a_b_mappings = [
            ABMapping(mapping_id="m1", a_root=str(self.a1), b_root=str(self.b1)),
            ABMapping(mapping_id="m2", a_root=str(self.a2), b_root=str(self.b2)),
        ]
        config.paths = Mock()
        config.paths.c_root = str(self.c1)
        config.paths.b_root = str(self.b1)
        config.behavior = Mock()
        self.config = config
        self.db = Mock(spec=Database)

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, self.db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fast_mapping_fallback_when_roots_not_initialized(self):
        """N1 硬性规格：预计算根未初始化时必须回退权威实现，禁止返回 None。"""
        self.app.clear_scan_mapping_roots()
        assert self.app._scan_mapping_roots is None

        p_a = self.a1 / "show" / "ep.strm"
        res_fast = self.app._get_mapping_for_a_fast(p_a)
        res_orig = self.app.get_mapping_for_a(p_a)
        assert res_fast is not None
        assert res_fast == res_orig

        p_b = self.b2 / "show" / "ep.strm"
        res_fast_b = self.app._get_mapping_for_b_fast(p_b)
        res_orig_b = self.app.get_mapping_for_b(p_b)
        assert res_fast_b is not None
        assert res_fast_b == res_orig_b

    def test_fast_mapping_differential_equivalence_when_initialized(self):
        """预计算根初始化后，fast 结果与权威结果逐项严格一致（含零命中/多命中 fail-closed）。"""
        self.app.ensure_scan_mapping_roots()
        assert self.app._scan_mapping_roots is not None

        test_cases_a = [
            self.a1 / "show" / "ep.strm",
            self.a2 / "movie.strm",
            Path(self.tmp) / "outside" / "ep.strm",  # 越界
        ]
        for p in test_cases_a:
            fast = self.app._get_mapping_for_a_fast(p)
            orig = self.app.get_mapping_for_a(p)
            assert fast == orig, f"A mapping mismatch for {p}: fast={fast}, orig={orig}"

        test_cases_b = [
            self.b1 / "show" / "Season 01" / "ep.strm",
            self.b2 / "movie.strm",
            Path(self.tmp) / "outside" / "ep.strm",
        ]
        for p in test_cases_b:
            fast = self.app._get_mapping_for_b_fast(p)
            orig = self.app.get_mapping_for_b(p)
            assert fast == orig, f"B mapping mismatch for {p}: fast={fast}, orig={orig}"

    def test_build_b_path_from_a_with_and_without_roots_is_identical(self):
        """build_b_path_from_a 注入预计算根与不注入预计算根产出严格相同的目标路径。"""
        self.app.ensure_scan_mapping_roots()
        a_file = self.a1 / "Show_0001" / "Season 01" / "S01E01.strm"
        a_file.parent.mkdir(parents=True, exist_ok=True)
        a_file.write_text("/dav/map1/Show_0001/Season 01/S01E01.mp4", encoding="utf-8")
        webdav = "/dav/map1/Show_0001/Season 01/S01E01.mp4"

        # 不注入预计算根（回退原路径解析）
        path_without = self.app.build_b_path_from_a(a_file, webdav)

        # 注入预计算根
        a_root, b_root = self.app._get_scan_roots_for_mapping("m1")
        path_with = self.app.build_b_path_from_a(a_file, webdav, a_root=a_root, b_root=b_root)

        assert path_without == path_with
        assert path_with == self.b1 / "Show_0001" / "Season 01" / "S01E01.strm"

    def test_scan_uses_fast_mapping_and_bypasses_authoritative_resolution(self):
        """Task 3 (R8): 扫描阶段预计算根初始化后，initial_scan_b 与 scan_a_to_b 全流程
        不再调用权威逐条 mapping 解析（get_mapping_for_a/get_mapping_for_b），消除 resolve 风暴。"""
        from database import Database

        db = Database(str(Path(self.tmp) / "bridge.db"))
        app = AppService(self.config, db, Mock())
        # 真实 A/B 根目录
        self.a1.mkdir(parents=True, exist_ok=True)
        self.b1.mkdir(parents=True, exist_ok=True)
        self.a2.mkdir(parents=True, exist_ok=True)
        self.b2.mkdir(parents=True, exist_ok=True)

        # A1 下构造一个 B 区可见文件（existing_b 场景），A2 下构造一个待复制文件
        a1_file = self.a1 / "Show_0001" / "Season 01" / "S01E01.strm"
        a1_file.parent.mkdir(parents=True, exist_ok=True)
        a1_file.write_text("/dav/map1/Show_0001/Season 01/S01E01.mp4", encoding="utf-8")
        b1_file = self.b1 / "Show_0001" / "Season 01" / "S01E01.strm"
        b1_file.parent.mkdir(parents=True, exist_ok=True)
        b1_file.write_text("/dav/map1/Show_0001/Season 01/S01E01.mp4", encoding="utf-8")
        a2_file = self.a2 / "Show_0001" / "Season 01" / "S01E02.strm"
        a2_file.parent.mkdir(parents=True, exist_ok=True)
        a2_file.write_text("/dav/map2/Show_0001/Season 01/S01E02.mp4", encoding="utf-8")

        # 构造 engine_configs 使 lineage 校验走完
        app.engine_configs = [
            {"a_root_norm": str(self.a1.resolve()), "mount_path": "/dav/map1",
             "source_paths": ["/dav/map1"]},
            {"a_root_norm": str(self.a2.resolve()), "mount_path": "/dav/map2",
             "source_paths": ["/dav/map2"]},
        ]

        app.ensure_scan_mapping_roots()
        assert app._scan_mapping_roots is not None

        with patch.object(app, "get_mapping_for_a") as mock_a, \
             patch.object(app, "get_mapping_for_b") as mock_b:
            # 先索引 A 区（initial_scan_a 不调用 mapping 解析）
            app.sync_service.initial_scan_a(use_bulk=True, a_roots=app.a_roots)
            # B 区扫描：新增记录 + 同步
            app.initial_scan_b()
            app.sync_service.scan_a_to_b_full_sync(use_bulk=True)

        # 扫描全程不得调用权威逐条 mapping 解析（resolve 风暴消除）
        mock_a.assert_not_called()
        mock_b.assert_not_called()

    def test_cleanup_startup_duplicates_calls_ensure_per_group(self):
        """Task 6: 启动级清扫对每个重复指纹组调用一次 ensure_single_visible_instance。"""
        self.app.db = Mock(spec=Database)
        self.app.db.get_duplicate_fingerprint_groups.return_value = [
            ("m1", "fp1", "/b/a1.strm"),
            ("m2", "fp1", "/c/a1.strm"),
        ]
        with patch.object(self.app, "ensure_single_visible_instance") as mock_ensure:
            self.app._cleanup_startup_duplicates()
        assert mock_ensure.call_count == 2
        mock_ensure.assert_any_call("fp1", "/b/a1.strm", mapping_id="m1")
        mock_ensure.assert_any_call("fp1", "/c/a1.strm", mapping_id="m2")

    def test_cleanup_startup_duplicates_no_groups_no_ensure(self):
        """Task 6: 无重复组时仅一次 GROUP BY，零额外 ensure 调用（零额外建连）。"""
        self.app.db = Mock(spec=Database)
        self.app.db.get_duplicate_fingerprint_groups.return_value = []
        with patch.object(self.app, "ensure_single_visible_instance") as mock_ensure:
            self.app._cleanup_startup_duplicates()
        self.app.db.get_duplicate_fingerprint_groups.assert_called_once()
        mock_ensure.assert_not_called()

    def test_refresh_identities_batch_differential_equivalence(self):
        """Task 4: 批量身份刷新与逐条 refresh 终态逐项等价（覆盖标准命名、复杂重命名、无有效实例、identity 缺失）。"""
        from database import Database

        db = Database(str(Path(self.tmp) / "id_batch.db"))
        app = AppService(self.config, db, Mock())
        self.a1.mkdir(parents=True, exist_ok=True)
        self.b1.mkdir(parents=True, exist_ok=True)
        self.a2.mkdir(parents=True, exist_ok=True)
        self.b2.mkdir(parents=True, exist_ok=True)

        webdav = "/dav/map1/Show_0001/Season 01/S01E01.mp4"
        fp = make_strm_fingerprint(webdav)
        now = time.time()
        # 创建磁盘文件使 valid 过滤通过
        b1_file = self.b1 / "Show_0001" / "Season 01" / "S01E01.strm"
        b1_file.parent.mkdir(parents=True, exist_ok=True)
        b1_file.write_text(webdav, encoding="utf-8")
        b2_file = self.b1 / "Show_0001" / "Season 01" / "S01E01 (1).strm"
        b2_file.write_text(webdav, encoding="utf-8")
        with db.rw_lock.write_locked(), db.connection() as conn:
            conn.execute(
                """INSERT INTO b_strm_files(
                    local_path, webdav_path, parent_webdav_path,
                    source_a_path, fingerprint, status, updated_at, mapping_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(b1_file),
                 webdav, "/dav/map1/Show_0001/Season 01", None, fp,
                 "valid", now, "m1"),
            )
            # 复杂重命名实例（更长的路径，评分更低 → 不应被选为 best）
            conn.execute(
                """INSERT INTO b_strm_files(
                    local_path, webdav_path, parent_webdav_path,
                    source_a_path, fingerprint, status, updated_at, mapping_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(b2_file),
                 webdav, "/dav/map1/Show_0001/Season 01", None, fp,
                 "valid", now, "m1"),
            )
            conn.commit()

        # 逐条刷新参考状态
        app.refresh_identity_current_b_path(fp, "m1")
        ref_proj = db.get_all_identity_projections_for_fingerprint(fp)
        ref_identity = db.get_identity_by_fingerprint(fp)
        ref_best = ref_identity.current_b_path if ref_identity else None

        # 重置 identity（删除）再批量刷新，验证等价
        db.delete_identity_by_fingerprint(fp)
        db.delete_identity_projection(fp, "m1")
        app._refresh_identities_batch([(fp, "m1")])

        new_proj = db.get_all_identity_projections_for_fingerprint(fp)
        new_identity = db.get_identity_by_fingerprint(fp)
        assert new_proj == ref_proj, f"投影不等价: {new_proj} != {ref_proj}"
        assert new_identity is not None
        assert new_identity.current_b_path == ref_best

    def test_refresh_identities_batch_no_valid_instance_clears_projection(self):
        """Task 4: 无有效实例时批量刷新删投影并置空 current_b_path（与逐条语义一致）。"""
        from database import Database

        db = Database(str(Path(self.tmp) / "id_batch2.db"))
        app = AppService(self.config, db, Mock())
        self.a1.mkdir(parents=True, exist_ok=True)
        self.b1.mkdir(parents=True, exist_ok=True)
        self.a2.mkdir(parents=True, exist_ok=True)
        self.b2.mkdir(parents=True, exist_ok=True)

        fp = make_strm_fingerprint("/dav/map1/Show_0001/Season 01/S01E01.mp4")
        now = time.time()
        file_path = self.b1 / "S01E01.strm"
        file_path.write_text("/dav/map1/Show_0001/Season 01/S01E01.mp4", encoding="utf-8")
        with db.rw_lock.write_locked(), db.connection() as conn:
            # 只有一条 valid 且磁盘存在 → 应选为 best
            conn.execute(
                """INSERT INTO b_strm_files(
                    local_path, webdav_path, parent_webdav_path,
                    source_a_path, fingerprint, status, updated_at, mapping_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(file_path),
                 "/dav/map1/Show_0001/Season 01/S01E01.mp4",
                 "/dav/map1/Show_0001/Season 01", None, fp, "valid", now, "m1"),
            )
            conn.commit()

        app._refresh_identities_batch([(fp, "m1")])
        identity = db.get_identity_by_fingerprint(fp)
        assert identity is not None
        assert identity.current_b_path == str(file_path)
        proj = db.get_all_identity_projections_for_fingerprint(fp)
        assert proj and proj[0][0] == "m1" and proj[0][1] == str(file_path)

        # 磁盘删除该文件 → 无有效实例 → 批量刷新删投影 + 置空 current_b_path
        file_path.unlink()
        app._refresh_identities_batch([(fp, "m1")])
        assert db.get_all_identity_projections_for_fingerprint(fp) == []
        assert db.get_identity_by_fingerprint(fp).current_b_path is None

    def test_cleanup_startup_duplicates_single_visible_instance_restored(self):
        """Task 6 (R1 闭合): 失败启动遗留双可见实例经清扫后恢复单实例约束（一个被隔离为 .duplicate）。"""
        from database import Database

        db = Database(str(Path(self.tmp) / "sweep.db"))
        app = AppService(self.config, db, Mock())
        self.a1.mkdir(parents=True, exist_ok=True)
        self.b1.mkdir(parents=True, exist_ok=True)
        self.a2.mkdir(parents=True, exist_ok=True)
        self.b2.mkdir(parents=True, exist_ok=True)

        webdav = "/dav/map1/Show_0001/Season 01/S01E01.mp4"
        # B 区两个可见实例（同指纹，均 valid）
        b1 = self.b1 / "Show_0001" / "Season 01" / "S01E01.strm"
        b1.parent.mkdir(parents=True, exist_ok=True)
        b1.write_text(webdav, encoding="utf-8")
        b2 = self.b1 / "Show_0001" / "Season 01" / "S01E01 (1).strm"
        b2.write_text(webdav, encoding="utf-8")
        fp = make_strm_fingerprint(webdav)

        # 手工写入两个 valid 记录模拟失败启动遗留
        now = time.time()
        with db.rw_lock.write_locked(), db.connection() as conn:
            conn.executemany(
                """INSERT INTO b_strm_files(
                    local_path, webdav_path, parent_webdav_path,
                    source_a_path, fingerprint, status, updated_at, mapping_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (str(b1), webdav, "/dav/map1/Show_0001/Season 01", None,
                     fp, "valid", now, "m1"),
                    (str(b2), webdav, "/dav/map1/Show_0001/Season 01", None,
                     fp, "valid", now, "m1"),
                ],
            )
            conn.commit()

        groups = db.get_duplicate_fingerprint_groups()
        assert len(groups) == 1 and groups[0][1] == fp

        app._cleanup_startup_duplicates()

        # 清扫后恰好一个可见 .strm 实例，另一个被隔离为 .duplicate
        strm_instances = [p for p in (self.b1 / "Show_0001" / "Season 01").glob("*.strm")]
        assert len(strm_instances) == 1, f"单实例约束未恢复: {strm_instances}"
        dup_files = list((self.b1 / "Show_0001" / "Season 01").glob("*.duplicate*"))
        assert len(dup_files) == 1, f"应隔离 1 个副本: {dup_files}"



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
        config.a_b_mappings = [ABMapping(
            mapping_id="test_m1", a_root=str(self.a_dir), b_root=str(self.b_dir))]
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

    def test_cleanup_skips_when_db_returns_non_iterable(self):
        """Background cleanup must not raise when DB returns an iterable-unfriendly value.

        Regression guard: a background timer was calling
        cleanup_b_zombies_under_folder and iterating directly over whatever
        ``get_b_under_root()`` returned.  When an unconfigured mock or a
        regression produced a plain ``Mock`` object instead of a list, the
        background thread raised ``TypeError: 'Mock' object is not iterable``,
        surfacing as a Pytest Unhandled Thread Exception warning in the full
        test run.

        Fail-closed behavior is preserved: the cleanup simply skips rather than
        exposing destructive or half-baked operations to the A/B/C pipeline.
        """
        self.db.get_b_under_root.return_value = Mock(name="unexpected_db_value")

        self.app.cleanup_b_zombies_under_folder("/mount")

        self.admin_api.list_directory.assert_not_called()

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
            mapping_id="test_m1",
        )
        self.db.get_b_under_root.return_value = [record]
        # 新实现使用 list_directory 批量获取云端文件列表（而非逐条 check_exists）
        # 返回空列表表示云端没有该文件 → 判定为僵尸文件
        self.admin_api.list_directory.return_value = {
            "code": 0, "data": {"content": [], "total": 0}
        }
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
            mapping_id="test_m1",
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
        config.a_b_mappings = [ABMapping(
            mapping_id="test_m1", a_root=str(self.a_dir), b_root=str(self.b_dir))]
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

    def test_is_standard_media_name_large_episode_boundary(self):
        assert self.app._is_standard_media_name("Show.S21E1088.strm") is True
        assert self.app._is_standard_media_name("Show.S01E9999.strm") is True
        assert self.app._is_standard_media_name("Show.S01E10000.strm") is False

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
        config.a_b_mappings = [ABMapping(
            mapping_id="test_m1", a_root=str(self.a_dir), b_root=str(self.b_dir))]
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

    def test_rollback_failure_raises(self):
        """回滚失败时：logging.error + 抛出异常使清理中止，不静默继续。"""
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
        # move_b_record 返回 False 模拟 DB 迁移失败
        self.db.move_b_record = Mock(return_value=False)

        from unittest.mock import patch as _patch
        original_rename = Path.rename

        def mock_rename(self_path, target):
            # 仅回滚 rename（quarantined 文件 rename 回原路径）失败
            src_name = str(self_path.name)
            if ".duplicate" in src_name:
                raise OSError("模拟磁盘满：回滚失败")
            return original_rename(self_path, target)

        # quarantine_file 内部会调用 Path.rename 做 quarantine；让它正常，
        # 仅在回滚 rename 时抛 OSError。
        with _patch.object(Path, "rename", mock_rename):
            with pytest.raises(OSError):
                self.app.ensure_single_visible_instance("fp", str(keep_file))


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
        config.a_b_mappings = [ABMapping(
            mapping_id="test_m1", a_root=str(self.a_dir), b_root=str(self.b_dir))]
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
# TestRestoreBFromAAfterViolation
# ===========================================================================


class TestRestoreBFromAAfterViolation:
    """_restore_b_from_a_after_violation 的 C 区隔离解构修复测试。

    get_mapping_for_b 返回 (mapping_id, b_root, a_root)；原实现解构成
    (mapping_id, a_root, b_root)，导致 get_c_path_for_b 拿到 a_root 当 b_root、
    relative_to 永远失败、C 区隔离失效、回退直接删除。本测试 mock
    get_mapping_for_b 返回 (mid, b_root, a_root)，验证文件被移入 C 区而非删除。
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
        config.a_b_mappings = [ABMapping(
            mapping_id="test_m1", a_root=str(self.a_dir), b_root=str(self.b_dir))]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        db.init_subtitle_table = Mock()
        db.move_b_record.return_value = True
        db.mark_b_instance_status = Mock()
        self.db = db

        with patch("app_service_core.RefreshService"), \
              patch("app_service_core.SyncService"), \
              patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_c_zone_isolation_uses_correct_b_root(self):
        """get_mapping_for_b 返回 (mid, b_root, a_root) → 文件移入 C 区而非删除。"""
        b_file = self.b_dir / "show" / "ep.strm"
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text("/mount/show/ep.mp4", encoding="utf-8")

        # 模拟 get_mapping_for_b 返回 (mapping_id, b_root, a_root)
        with patch.object(self.app, "get_mapping_for_b",
                          return_value=("test_m1", str(self.b_dir), str(self.a_dir))):
            with patch.object(self.app, "get_c_path_for_b",
                              wraps=self.app.get_c_path_for_b) as mock_c:
                self.app._restore_b_from_a_after_violation(
                    b_file, "/mount/show/ep.mp4", "dummy-fingerprint")

        # get_c_path_for_b 必须收到 b_root=self.b_dir（而非 a_dir）
        assert mock_c.call_count == 1
        _mid, _b_path, b_root_arg = mock_c.call_args[0]
        assert str(b_root_arg) == str(self.b_dir)

        # 文件应被隔离到 C 区（相对路径 show/ep.strm 保留），而非删除
        c_target = self.c_dir / "test_m1" / "show" / "ep.strm"
        assert c_target.exists()
        assert not b_file.exists()
        self.db.mark_b_instance_status.assert_called_once()
        # 未回退到直接删除
        self.db.delete_b_by_local.assert_not_called()


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

    def test_untrusted_parent_records_excluded_from_diff(self):
        """不可信父目录（首页返 None）下本地记录整组不参与冗余差集。"""
        a_trusted = self.a_dir / "trusted.strm"
        a_trusted.write_text("/dir_ok/trusted.strm", encoding="utf-8")
        a_untrusted = self.a_dir / "untrusted.strm"
        a_untrusted.write_text("/dir_bad/untrusted.strm", encoding="utf-8")
        self.db.get_all_a_records.return_value = [
            Mock(local_path=str(a_trusted), webdav_path="/dir_ok/trusted.strm",
                 parent_webdav_path="/dir_ok"),
            Mock(local_path=str(a_untrusted), webdav_path="/dir_bad/untrusted.strm",
                 parent_webdav_path="/dir_bad"),
        ]
        # dir_ok：权威空目录 → trusted.strm 冗余
        # dir_bad：首页 None → 不可信，整组排除
        def mock_list(path, **kwargs):
            if path == "/dir_ok":
                return {"code": 200, "data": {"total": 0, "content": []}}
            return None
        self.admin_api.list_directory.side_effect = mock_list

        self.app.cleanup_a_redundant_using_api()

        # trusted 应被删（权威空目录）
        self.db.delete_a_by_local.assert_any_call(str(a_trusted))
        # untrusted 不应被删（不可信父目录整组排除）
        self.db.delete_a_by_local_str = [c.args[0] for c in self.db.delete_a_by_local.call_args_list]
        assert str(a_untrusted) not in self.db.delete_a_by_local_str


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
        result = self.app._collect_cloud_files_concurrent("/mount")

        # 路径应从 cloud_path + name 重构，而非使用 API 的 path 字段
        assert result == {"/mount/f1.strm", "/mount/f2.strm"}
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

        result = self.app._collect_cloud_files_concurrent("/mount")

        assert result is not None
        assert len(result) == 150

    def test_empty_directory(self):
        """Empty directory → no files collected."""
        self.admin_api.list_directory.return_value = {
            "code": 200,
            "data": {"total": 0, "content": []},
        }
        result = self.app._collect_cloud_files_concurrent("/mount")

        assert result is not None
        assert len(result) == 0

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
        result = self.app._collect_cloud_files_concurrent("/mount")

        assert result == {"/mount/good.strm"}

    def test_api_failure_returns_empty(self):
        """API failure → returns None (不可信), no exception."""
        self.admin_api.list_directory.return_value = None
        result = self.app._collect_cloud_files_concurrent("/mount")

        assert result is None

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
        result = self.app._collect_cloud_files_concurrent("/mount")

        assert result == {"/mount/good.strm"}

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
        result = self.app._collect_cloud_files_concurrent("/mount/show")

        # Should be cloud_path + "/" + name, NOT item["path"]
        assert result == {"/mount/show/episode.strm"}

    # ----------------------------------------------------------
    # 后续页失败 / 不可信 → 整组 fail-closed
    # ----------------------------------------------------------

    def _page(self, total: int, names: list[str]) -> dict:
        return {
            "code": 200,
            "data": {
                "total": total,
                "content": [
                    {"name": n, "is_dir": False, "path": f"D:\\storage\\{n}"}
                    for n in names
                ],
            },
        }

    def test_second_page_none_returns_none(self):
        """第 2 页返回 None（重试耗尽）→ 整组返回 None，不返回部分集。"""
        page1 = self._page(150, [f"f{i}.strm" for i in range(100)])
        self.admin_api.list_directory.side_effect = [page1, None, None, None]

        result = self.app._collect_cloud_files_concurrent("/mount")

        assert result is None, "后续页不可信时必须整组 fail-closed"

    def test_second_page_malformed_contract_returns_none(self):
        """第 2 页响应违反契约（code 非 0/200）→ 整组返回 None。"""
        page1 = self._page(150, [f"f{i}.strm" for i in range(100)])
        bad_page = {"code": 500, "data": {"total": 150, "content": []}}
        self.admin_api.list_directory.side_effect = [page1, bad_page]

        result = self.app._collect_cloud_files_concurrent("/mount")

        assert result is None

    def test_second_page_non_dict_data_returns_none(self):
        """第 2 页 data 非 dict → 整组返回 None。"""
        page1 = self._page(150, [f"f{i}.strm" for i in range(100)])
        bad_page = {"code": 200, "data": "not-a-dict"}
        self.admin_api.list_directory.side_effect = [page1, bad_page]

        result = self.app._collect_cloud_files_concurrent("/mount")

        assert result is None

    def test_second_page_raises_exhausts_retries_returns_none(self):
        """第 2 页始终抛异常 → 重试耗尽后整组返回 None。"""
        page1 = self._page(150, [f"f{i}.strm" for i in range(100)])
        calls = {"n": 0}

        def side_effect(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return page1
            raise ConnectionError("network down")

        self.admin_api.list_directory.side_effect = side_effect

        result = self.app._collect_cloud_files_concurrent("/mount")

        assert result is None


# ===========================================================================
# TestCleanupAPaginationFailClosed — 分页不可信时上层不做 destructive cleanup
# ===========================================================================


class TestCleanupAPaginationFailClosed:
    """A 区分页收集不可信时，cleanup_a_redundant_using_api() 不得删除本地记录。"""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.a_b_mappings = []
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

    def _seed_a_record(self, name: str, parent: str):
        a_file = self.a_dir / name
        webdav_path = f"{parent}/{name}"
        a_file.write_text(webdav_path, encoding="utf-8")
        self.db.get_all_a_records.return_value = [
            ARecord(local_path=str(a_file), webdav_path=webdav_path,
                    parent_webdav_path=parent, updated_at=0.0)
        ]
        return a_file

    def test_second_page_failure_blocks_cleanup(self):
        """第 2 页失败使父目录不可信 → 该目录下本地记录一个都不删。"""
        a_file = self._seed_a_record("keep.strm", "/mount")
        page1 = {
            "code": 200,
            "data": {
                "total": 150,
                "content": [
                    {"name": f"other{i}.strm", "is_dir": False,
                     "path": f"D:\\s\\other{i}.strm"}
                    for i in range(100)
                ],
            },
        }
        # 首页权威但不含 keep.strm；第 2 页始终失败 → 整组不可信
        self.admin_api.list_directory.side_effect = (
            [page1] + [None] * 20)

        self.app.cleanup_a_redundant_using_api()

        assert a_file.exists(), "父目录不可信时不得删除本地 STRM"
        self.db.delete_a_by_local.assert_not_called()
        self.db.set_ghost_protection.assert_not_called()

    def test_malformed_second_page_blocks_cleanup(self):
        """第 2 页响应契约不可信 → 该目录下本地记录一个都不删。"""
        a_file = self._seed_a_record("keep.strm", "/mount")
        page1 = {
            "code": 200,
            "data": {
                "total": 150,
                "content": [
                    {"name": f"other{i}.strm", "is_dir": False,
                     "path": f"D:\\s\\other{i}.strm"}
                    for i in range(100)
                ],
            },
        }
        bad_page = {"code": 200, "data": {"total": 150, "content": "bad"}}
        self.admin_api.list_directory.side_effect = [page1, bad_page]

        self.app.cleanup_a_redundant_using_api()

        assert a_file.exists()
        self.db.delete_a_by_local.assert_not_called()


# ===========================================================================
# TestCollectCloudFilesInDirectorySafetyValve — B 区分页安全阀
# ===========================================================================


class TestCollectCloudFilesInDirectorySafetyValve:
    """B 区 _collect_cloud_files_in_directory() 安全阀与 fail-closed。"""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.a_b_mappings = [ABMapping(
            mapping_id="test_m1", a_root=str(self.a_dir), b_root=str(self.b_dir))]
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

    def test_safety_valve_exhausted_returns_none(self):
        """每页都满 100 条且始终有下一页 → 安全阀耗尽后返回 None，不返回部分集。"""
        full_page = {
            "code": 200,
            "data": {
                "total": 100000,
                "content": [
                    {"name": f"f{i}.strm", "is_dir": False}
                    for i in range(100)
                ],
            },
        }
        self.admin_api.list_directory.return_value = full_page

        result = self.app._collect_cloud_files_in_directory("/mount")

        assert result is None, "安全阀耗尽必须 fail-closed"
        # 安全阀上限为 100 页
        assert self.admin_api.list_directory.call_count == 100

    def test_untrusted_page_returns_none(self):
        """任一页响应不可信 → 立即返回 None。"""
        self.admin_api.list_directory.return_value = {"code": 500}

        result = self.app._collect_cloud_files_in_directory("/mount")

        assert result is None

    def test_cleanup_b_zombies_does_not_delete_on_untrusted_collection(self):
        """收集结果不可信时，B 区僵尸清理不得删除/迁移任何文件。"""
        b_file = self.b_dir / "zombie.strm"
        b_file.write_text("/mount/zombie.mp4", encoding="utf-8")

        record = BRecord(
            local_path=str(b_file),
            webdav_path="/mount/zombie.mp4",
            parent_webdav_path="/mount",
            source_a_path=str(self.a_dir / "missing.strm"),
            fingerprint="fp_z",
            status="valid",
            updated_at=0,
            mapping_id="test_m1",
        )
        # cleanup_b_zombies_under_folder 通过 get_b_under_root 读取记录
        self.db.get_b_under_root.return_value = [record]
        self.db.is_ghost_protected.return_value = False
        # 分页收集不可信（任一页 code 非 0/200）
        self.admin_api.list_directory.return_value = {"code": 500}

        with patch.object(self.app, "_handle_b_zombie") as mock_zombie:
            self.app.cleanup_b_zombies_under_folder("/mount")

        # 收集器返回 None → 该父目录被跳过，destructive 处理不得发生
        mock_zombie.assert_not_called()
        assert b_file.exists(), "不可信收集结果不得触发删除"
        self.db.delete_b_by_local.assert_not_called()
        self.db.upsert_c.assert_not_called()

    def test_cleanup_b_zombies_safety_valve_exhausted_does_not_delete(self):
        """安全阀耗尽（始终满页）→ 视为不可信，B 区僵尸清理不删除文件。"""
        b_file = self.b_dir / "zombie.strm"
        b_file.write_text("/mount/zombie.mp4", encoding="utf-8")

        record = BRecord(
            local_path=str(b_file),
            webdav_path="/mount/zombie.mp4",
            parent_webdav_path="/mount",
            source_a_path=str(self.a_dir / "missing.strm"),
            fingerprint="fp_z",
            status="valid",
            updated_at=0,
            mapping_id="test_m1",
        )
        self.db.get_b_under_root.return_value = [record]
        self.db.is_ghost_protected.return_value = False
        # 每页都满 100 条 → 永远认为还有下一页 → 安全阀耗尽
        # 且返回内容不含 zombie.strm，若误判权威会导致删除
        self.admin_api.list_directory.return_value = {
            "code": 200,
            "data": {
                "total": 100000,
                "content": [
                    {"name": f"other{i}.strm", "is_dir": False}
                    for i in range(100)
                ],
            },
        }

        with patch.object(self.app, "_handle_b_zombie") as mock_zombie:
            self.app.cleanup_b_zombies_under_folder("/mount")

        mock_zombie.assert_not_called()
        assert b_file.exists(), "安全阀耗尽必须 fail-closed，不得删除"
        self.db.delete_b_by_local.assert_not_called()


# ===========================================================================
# TestParseFsListContent — /api/fs/list 响应契约校验（fail-closed）
# ===========================================================================


class TestParseFsListContent:
    """Test _parse_fs_list_content contract validation (fail-closed).

    直接单元测试 _parse_fs_list_content 的所有 guard 分支，与
    test_log_issues_simulation.py::TestParseFsListContent 互为补充。
    本类聚焦边界：bool 穿透、非 dict item 在 _collect 路径的健壮性、
    content=[] total>0 矛盾。
    """

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        config = Mock(spec=AppConfig)
        config.a_folders = [self.tmp]
        config.paths = Mock()
        config.paths.b_root = self.tmp
        config.paths.c_root = self.tmp
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

    def test_bool_total_rejected(self):
        """bool 是 int 子类，必须被拒（True/False 不应被当作权威 total）。"""
        # total=True + 非空 content：旧实现会错误返回 (content, True)
        res = {"code": 200, "data": {"total": True, "content": [
            {"name": "a.strm", "is_dir": False}]}}
        assert self.app._parse_fs_list_content(res) is None
        # total=False 同样应被拒
        res2 = {"code": 200, "data": {"total": False, "content": []}}
        assert self.app._parse_fs_list_content(res2) is None

    def test_content_empty_total_positive_rejected(self):
        """content=[] 但 total>0：自相矛盾，视为截断/畸形。"""
        res = {"code": 0, "data": {"total": 5, "content": []}}
        assert self.app._parse_fs_list_content(res) is None

    def test_authoritative_empty_accepted(self):
        """权威空目录：content=[] 且 total=0，应返回 ([], 0)。"""
        res = {"code": 200, "data": {"total": 0, "content": []}}
        assert self.app._parse_fs_list_content(res) == ([], 0)

    def test_authoritative_success_accepted(self):
        """权威成功：有文件，total 与 content 一致。"""
        content = [{"name": "a.strm", "is_dir": False}]
        res = {"code": 0, "data": {"total": 1, "content": content}}
        assert self.app._parse_fs_list_content(res) == (content, 1)

    def test_non_dict_item_in_collect_does_not_raise(self):
        """_collect_cloud_files_concurrent 首页 content 含非 dict 元素时
        不应抛 AttributeError（与串行路径一致，跳过非 dict item）。"""
        self.app.admin_api.list_directory.return_value = {
            "code": 200,
            "data": {
                "total": 2,
                "content": [
                    "not-a-dict",  # 非 dict 元素
                    {"name": "good.strm", "is_dir": False},
                ],
            },
        }
        # 不应抛异常，且 good.strm 仍被收集
        result = self.app._collect_cloud_files_concurrent("/mount")
        assert result is not None
        assert "/mount/good.strm" in result


# ===========================================================================
# TestNaturalSortKey — 区域详情 STRM 列表自然排序（修复字典序 bug）
# ===========================================================================


class TestNaturalSortKey:
    """Test _natural_sort_key fixes the dictionary-order bug.

    旧实现用纯字典序对 local_path 排序，缺前导零时出现
    `1, 10, 2, 21` 错乱。自然排序把 basename 的连续数字按整数比较。
    直接对 routes 模块的 _natural_sort_key 做纯函数测试。
    """

    def _key(self):
        from webui.routes import _natural_sort_key
        return _natural_sort_key

    def test_episode_numbers_compared_as_integers(self):
        """E1 < E2 < E10 < E21，而非字典序的 E1 < E10 < E2 < E21。"""
        key = self._key()
        paths = [
            "/b/Show/Season 01/S01E21.strm",
            "/b/Show/Season 01/S01E2.strm",
            "/b/Show/Season 01/S01E10.strm",
            "/b/Show/Season 01/S01E1.strm",
        ]
        sorted_paths = sorted(paths, key=key)
        endings = [p.rsplit("/", 1)[-1] for p in sorted_paths]
        assert endings == ["S01E1.strm", "S01E2.strm",
                           "S01E10.strm", "S01E21.strm"]

    def test_padded_zero_still_sorted_correctly(self):
        """带前导零的文件名自然排序与字典序一致。"""
        key = self._key()
        paths = [
            "/b/Show/Season 01/S01E10.strm",
            "/b/Show/Season 01/S01E01.strm",
            "/b/Show/Season 01/S01E02.strm",
        ]
        sorted_paths = sorted(paths, key=key)
        endings = [p.rsplit("/", 1)[-1] for p in sorted_paths]
        assert endings == ["S01E01.strm", "S01E02.strm", "S01E10.strm"]

    def test_local_path_as_tiebreaker(self):
        """basename 相同时，local_path 作为 tiebreaker 保持稳定。"""
        key = self._key()
        paths = [
            "/b/ShowB/Season 01/S01E01.strm",
            "/b/ShowA/Season 01/S01E01.strm",
        ]
        sorted_paths = sorted(paths, key=key)
        # basename 相同，按完整路径字典序（tiebreaker）
        assert sorted_paths[0].startswith("/b/ShowA")


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
        config.a_b_mappings = [ABMapping(
            mapping_id="test_m1", a_root=str(self.a_dir), b_root=str(self.b_dir))]
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

    # ---- c.9.2 Task 12：字幕启动扫描批量预读 ----

    def test_preload_hit_with_existing_target_skips_processing(self):
        """预读命中（row 存在 ∧ target_path 存在）→ 不调 process_subtitle_file。
        与 process_subtitle_file 的现行 skip 判定逐条等价（不比对指纹）。"""
        sub = self.a_dir / "movie.srt"
        sub.write_text("subtitle content", encoding="utf-8")
        target = self.b_dir / "movie.zho.ass"
        target.write_text("target", encoding="utf-8")
        self.db.get_subtitle_path_targets.return_value = {
            str(sub.resolve()): str(target)}
        with patch.object(self.app, "process_subtitle_file") as mock_proc:
            self.app._scan_a_subtitles_on_startup()
        mock_proc.assert_not_called()

    def test_preload_hit_but_target_missing_still_processes(self):
        """row 命中但目标文件缺失 → 仍走原 process_subtitle_file（重新处理）"""
        sub = self.a_dir / "movie.srt"
        sub.write_text("subtitle content", encoding="utf-8")
        self.db.get_subtitle_path_targets.return_value = {
            str(sub.resolve()): str(self.b_dir / "gone.ass")}
        with patch.object(self.app, "process_subtitle_file") as mock_proc:
            self.app._scan_a_subtitles_on_startup()
        mock_proc.assert_called_once()

    def test_preload_key_resolves_unnormalized_walk_path(self):
        """查询键硬约束（I-3，c.9.3 Task E 真红化）：DB 键是 resolve 后的串
        ——walk 原始串携带 ".." 段（pathlib 保留不折叠；"/." 段会在构造期被
        折叠故不可用作判别）时仍须命中跳过。实现若不 resolve 查询键则
        miss → process 被调 = 红；resolve 则命中 = 绿。"""
        sub = self.a_dir / "movie.srt"
        sub.write_text("subtitle content", encoding="utf-8")
        target = self.b_dir / "movie.zho.ass"
        target.write_text("target", encoding="utf-8")
        self.db.get_subtitle_path_targets.return_value = {
            str(sub.resolve()): str(target)}
        # 人工 walk root 携带 ".." 段（"/_/.." 解析后回落到 a_dir 本身）：
        # Path(root)/name 的字符串形态含 ".."，str 与 resolve 形态必不等值
        fake_root = str(self.a_dir) + os.sep + "_" + os.sep + ".."
        with patch("app_service_core.os.walk",
                   return_value=[(fake_root, [], ["movie.srt"])]):
            with patch.object(self.app, "process_subtitle_file") as mock_proc:
                self.app._scan_a_subtitles_on_startup()
        mock_proc.assert_not_called()

    def test_count_semantics_total_includes_skipped_and_skip_info(self, caplog):
        """count 口径（对账不破）：完成日志「处理 N 个文件」照现行口径计
        （含被跳过的）；跳过数另行输出一条 INFO。"""
        import logging as _logging
        sub1 = self.a_dir / "m1.srt"
        sub1.write_text("s1", encoding="utf-8")
        sub2 = self.a_dir / "m2.srt"
        sub2.write_text("s2", encoding="utf-8")
        target = self.b_dir / "m1.zho.ass"
        target.write_text("t", encoding="utf-8")
        self.db.get_subtitle_path_targets.return_value = {
            str(sub1.resolve()): str(target)}
        with patch.object(self.app, "process_subtitle_file"), \
             caplog.at_level(_logging.INFO):
            self.app._scan_a_subtitles_on_startup()
        total_line = [r.getMessage() for r in caplog.records
                      if "A 区字幕扫描完成" in r.getMessage()]
        assert any("处理 2 个文件" in m for m in total_line), total_line
        skip_line = [r.getMessage() for r in caplog.records
                     if "已处理跳过" in r.getMessage()]
        assert any("1 个" in m for m in skip_line), skip_line


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
                ], "total": 1}}
            elif path == "/cloud/dir2":
                return {"code": 0, "data": {"content": [
                    {"name": "file3.strm", "is_dir": False},
                ], "total": 1}}
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
        config.a_b_mappings = [ABMapping(
            mapping_id="test_m1", a_root=str(self.a_dir), b_root=str(self.b_dir))]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.a_to_b_restore_delay_seconds = 30

        db = Mock(spec=Database)
        db.get_b_by_local_full.return_value = BRecord(
            "/b/file1.strm", "/cloud/dir1/file1.strm", "/cloud/dir1",
            "/a/file1.strm", "fp1", "valid", "2024-01-01", "test_m1"
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


# ===========================================================================
# TestRefreshPathMappingScoped  (Task 1: 修复 strm_storage_map 引用)
# ===========================================================================


class TestRefreshPathMappingScoped:
    """验证 get_a_roots_for_refresh_paths / get_engine_paths_for_a_roots
    使用 config.strm_storage_map 且 mapping-scoped。"""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_m1 = os.path.join(self.tmp, "a_m1")
        self.a_m2 = os.path.join(self.tmp, "a_m2")
        self.b_m1 = os.path.join(self.tmp, "b_m1")
        self.b_m2 = os.path.join(self.tmp, "b_m2")
        self.c_dir = os.path.join(self.tmp, "c")
        for d in [self.a_m1, self.a_m2, self.b_m1, self.b_m2, self.c_dir]:
            os.makedirs(d)

        config = Mock(spec=AppConfig)
        config.a_folders = [self.a_m1, self.a_m2]
        config.a_b_mappings = [
            ABMapping(mapping_id="m1", a_root=self.a_m1, b_root=self.b_m1),
            ABMapping(mapping_id="m2", a_root=self.a_m2, b_root=self.b_m2),
        ]
        config.strm_storage_map = {
            "m1": StrmStorageMapping(mount_path="/strm_m1", paths=[], local_path=self.a_m1),
            "m2": StrmStorageMapping(mount_path="/strm_m2", paths=[], local_path=self.a_m2),
        }
        config.refresh_paths = ["/strm_m1"]
        config.paths = Mock()
        config.paths.b_root = self.b_m1
        config.paths.c_root = self.c_dir
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_refresh_paths_uses_config_storage_map(self):
        """refresh_paths=/strm_m1 只返回 m1 的 A 根。"""
        result = self.app.get_a_roots_for_refresh_paths()
        assert len(result) == 1
        assert result[0] == Path(self.a_m1)

    def test_engine_paths_is_mapping_scoped(self):
        """指定 m1 A 根只返回 m1 的 engine mount path。"""
        result = self.app.get_engine_paths_for_a_roots([Path(self.a_m1)])
        assert result == ["/strm_m1"]
        result2 = self.app.get_engine_paths_for_a_roots([Path(self.a_m2)])
        assert result2 == ["/strm_m2"]


class TestRefreshPathMappingScopedFailClosed:
    """storage map 为空时 fail-closed：不返回全部 A 根。"""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = os.path.join(self.tmp, "a")
        self.b_dir = os.path.join(self.tmp, "b")
        self.c_dir = os.path.join(self.tmp, "c")
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            os.makedirs(d)

        config = Mock(spec=AppConfig)
        config.a_folders = [self.a_dir]
        config.a_b_mappings = [
            ABMapping(mapping_id="m1", a_root=self.a_dir, b_root=self.b_dir),
        ]
        config.strm_storage_map = {}
        config.refresh_paths = ["/any_engine_path"]
        config.paths = Mock()
        config.paths.b_root = self.b_dir
        config.paths.c_root = self.c_dir
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_storage_map_returns_no_roots(self):
        """storage map 为空时不能把 refresh path 套到所有 mapping。"""
        result = self.app.get_a_roots_for_refresh_paths()
        assert result == []

    def test_empty_storage_map_engine_paths_empty(self):
        """storage map 为空时 engine paths 也应为空列表。"""
        result = self.app.get_engine_paths_for_a_roots([Path(self.a_dir)])
        assert result == []


class TestCloudPathToEnginePathsBoundary:
    """R30: _cloud_path_to_engine_paths 前缀匹配需带路径边界。

    避免 "/cloud/番剧" 误配 "/cloud/番剧2/x.strm"。
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
        config.a_b_mappings = [
            ABMapping(mapping_id="m1", a_root=str(self.a_dir), b_root=str(self.b_dir)),
        ]
        # entry_path="engine" 映射到挂载路径 ["/cloud/番剧"]
        config.strm_storage_map = {
            "engine": StrmStorageMapping(
                mount_path="/engine", paths=["/cloud/番剧"], local_path=str(self.a_dir)),
        }
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []

        db = Mock(spec=Database)
        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prefix_boundary_not_fuzzy_match(self):
        """"/cloud/番剧2/x" 不得匹配 "/cloud/番剧" 前缀。"""
        result = self.app._cloud_path_to_engine_paths("/cloud/番剧2/x.strm")
        assert result == []

    def test_exact_match_returns_engine_root(self):
        """精确等于挂载路径时返回引擎根路径。"""
        result = self.app._cloud_path_to_engine_paths("/cloud/番剧")
        assert result == ["engine"]

    def test_child_match_builds_engine_path(self):
        """子路径匹配时拼接相对路径到引擎路径。"""
        result = self.app._cloud_path_to_engine_paths("/cloud/番剧/进击的巨人/x.strm")
        assert result == ["engine/进击的巨人/x.strm"]


# ===========================================================================
# TestCleanupADeletedOnCloud —— update 模式云端删除清理的直调安全测试
# ===========================================================================


class TestCleanupADeletedOnCloud:
    """直调 cleanup_a_deleted_on_cloud()，验证前缀边界与三态 fail-closed。

    仅使用临时目录中的真实文件与 mock DB / mock admin_api，
    不访问真实 OpenList、不触碰工作区文件。
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
        config.a_b_mappings = []
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

    def _make_a_file(self, name: str, webdav_path: str) -> Path:
        """在临时 A 目录写入一个真实 STRM 文件。"""
        path = self.a_dir / name
        path.write_text(webdav_path, encoding="utf-8")
        return path

    def _record(self, local_path: Path, webdav_path: str) -> ARecord:
        return ARecord(
            local_path=str(local_path),
            webdav_path=webdav_path,
            parent_webdav_path=webdav_path.rsplit("/", 1)[0] or "/",
            updated_at=0.0,
        )

    # ----------------------------------------------------------
    # 空 engine_path
    # ----------------------------------------------------------

    def test_empty_engine_path_short_circuits(self):
        """engine_path 为空时不读 DB、不调用 API、不删除任何文件。"""
        self.app.cleanup_a_deleted_on_cloud("")

        self.db.get_all_a_records.assert_not_called()
        self.admin_api.check_exists.assert_not_called()
        self.db.delete_a_by_local.assert_not_called()
        self.db.set_ghost_protection.assert_not_called()

    # ----------------------------------------------------------
    # 前缀边界：/movies 不得误匹配 /movies_extra
    # ----------------------------------------------------------

    def test_similar_prefix_directory_not_matched(self):
        """/movies 只处理 /movies/ 下记录，不误伤 /movies_extra/。"""
        in_scope = self._make_a_file("in_scope.strm", "/movies/item.strm")
        out_scope = self._make_a_file("out_scope.strm", "/movies_extra/item.strm")
        self.db.get_all_a_records.return_value = [
            self._record(in_scope, "/movies/item.strm"),
            self._record(out_scope, "/movies_extra/item.strm"),
        ]
        # 云端权威判定：范围内的记录已删除
        self.admin_api.check_exists.return_value = False

        self.app.cleanup_a_deleted_on_cloud("/movies")

        # 只对范围内的 WebDAV 路径做存在性查询
        checked = [c.args[0] for c in self.admin_api.check_exists.call_args_list]
        assert checked == ["/movies/item.strm"]
        # 范围内文件被删除，范围外文件保留
        assert not in_scope.exists()
        assert out_scope.exists()
        self.db.delete_a_by_local.assert_called_once_with(str(in_scope))

    def test_webdav_path_equal_to_engine_path_is_in_scope(self):
        """webdav_path 恰好等于 engine_path 时属于处理范围（当前方法契约）。"""
        exact = self._make_a_file("exact.strm", "/movies")
        self.db.get_all_a_records.return_value = [
            self._record(exact, "/movies"),
        ]
        self.admin_api.check_exists.return_value = False

        self.app.cleanup_a_deleted_on_cloud("/movies")

        self.admin_api.check_exists.assert_called_once_with("/movies")
        assert not exact.exists()
        self.db.delete_a_by_local.assert_called_once_with(str(exact))

    def test_engine_path_with_trailing_slash_normalized(self):
        """engine_path 带尾部斜杠时前缀规范化，不产生双斜杠导致漏匹配。"""
        a_file = self._make_a_file("item.strm", "/movies/item.strm")
        self.db.get_all_a_records.return_value = [
            self._record(a_file, "/movies/item.strm"),
        ]
        self.admin_api.check_exists.return_value = False

        self.app.cleanup_a_deleted_on_cloud("/movies/")

        self.admin_api.check_exists.assert_called_once_with("/movies/item.strm")
        assert not a_file.exists()

    # ----------------------------------------------------------
    # check_exists 三态
    # ----------------------------------------------------------

    def test_check_exists_true_keeps_everything(self):
        """云端仍存在（True）→ 不删本地、不删 DB、不写 ghost。"""
        a_file = self._make_a_file("keep.strm", "/movies/keep.strm")
        self.db.get_all_a_records.return_value = [
            self._record(a_file, "/movies/keep.strm"),
        ]
        self.admin_api.check_exists.return_value = True

        self.app.cleanup_a_deleted_on_cloud("/movies")

        assert a_file.exists()
        self.db.delete_a_by_local.assert_not_called()
        self.db.set_ghost_protection.assert_not_called()

    def test_check_exists_false_deletes_and_sets_ghost(self):
        """云端权威缺失（False）→ 删本地 + 删 DB + 写 ghost protection。"""
        a_file = self._make_a_file("gone.strm", "/movies/gone.strm")
        self.db.get_all_a_records.return_value = [
            self._record(a_file, "/movies/gone.strm"),
        ]
        self.admin_api.check_exists.return_value = False

        self.app.cleanup_a_deleted_on_cloud("/movies")

        assert not a_file.exists()
        self.db.delete_a_by_local.assert_called_once_with(str(a_file))
        self.db.set_ghost_protection.assert_called_once_with(
            "/movies/gone.strm", 300, reason="cloud_deleted")

    def test_check_exists_none_is_fail_closed(self):
        """存在性不可信（None）→ 保留本地文件、保留 DB、不写 ghost。"""
        a_file = self._make_a_file("untrusted.strm", "/movies/untrusted.strm")
        self.db.get_all_a_records.return_value = [
            self._record(a_file, "/movies/untrusted.strm"),
        ]
        self.admin_api.check_exists.return_value = None

        self.app.cleanup_a_deleted_on_cloud("/movies")

        assert a_file.exists(), "None（不可信）绝不能触发删除"
        self.db.delete_a_by_local.assert_not_called()
        self.db.set_ghost_protection.assert_not_called()

    def test_mixed_three_states_only_false_deleted(self):
        """同一批记录混合三态时，只有 False 记录被清理。"""
        keep = self._make_a_file("keep.strm", "/movies/keep.strm")
        gone = self._make_a_file("gone.strm", "/movies/gone.strm")
        untrusted = self._make_a_file("untrusted.strm", "/movies/untrusted.strm")
        self.db.get_all_a_records.return_value = [
            self._record(keep, "/movies/keep.strm"),
            self._record(gone, "/movies/gone.strm"),
            self._record(untrusted, "/movies/untrusted.strm"),
        ]
        states = {
            "/movies/keep.strm": True,
            "/movies/gone.strm": False,
            "/movies/untrusted.strm": None,
        }
        self.admin_api.check_exists.side_effect = lambda p: states[p]

        self.app.cleanup_a_deleted_on_cloud("/movies")

        assert keep.exists()
        assert not gone.exists()
        assert untrusted.exists()
        deleted = [c.args[0] for c in self.db.delete_a_by_local.call_args_list]
        assert deleted == [str(gone)]
        ghosted = [c.args[0] for c in self.db.set_ghost_protection.call_args_list]
        assert ghosted == ["/movies/gone.strm"]

    def test_no_a_records_no_api_calls(self):
        """DB 中无 A 记录时不调用存在性查询。"""
        self.db.get_all_a_records.return_value = []

        self.app.cleanup_a_deleted_on_cloud("/movies")

        self.admin_api.check_exists.assert_not_called()
        self.db.delete_a_by_local.assert_not_called()


# ===========================================================================
# TestForceDeleteAndVerify  —  _force_delete_and_verify 直调测试
# ===========================================================================


class TestForceDeleteAndVerify:
    """验证 _force_delete_and_verify 的三层删除回退与权限失败行为。"""

    @pytest.fixture
    def app_service(self, tmp_path):
        """构造最小 AppService，仅用于调用 _force_delete_and_verify。"""
        from config import AppConfig
        app = AppService.__new__(AppService)
        app.config = MagicMock()
        app.db = MagicMock()
        return app

    def test_file_already_gone_returns_true(self, app_service, tmp_path):
        """路径不存在时直接返回 True。"""
        target = tmp_path / "nonexistent.strm"
        assert not target.exists()
        result = app_service._force_delete_and_verify(target)
        assert result is True

    def test_normal_delete_returns_true(self, app_service, tmp_path):
        """存在且可删除的文件正常删除后返回 True。"""
        target = tmp_path / "normal.strm"
        target.write_text("test")
        assert target.exists()

        with patch("app_service_core.safe_remove_file") as mock_safe:
            mock_safe.side_effect = lambda p: Path(p).unlink() if Path(p).exists() else True
            result = app_service._force_delete_and_verify(target)

        assert result is True
        assert not target.exists()

    def test_all_three_tiers_fail_file_persists_returns_false(self, app_service, tmp_path):
        """三级删除全部失败，文件仍存在时返回 False。"""
        target = tmp_path / "stubborn.strm"
        target.write_text("stubborn")
        assert target.exists()

        class FakePath(str):
            def exists(self): return True

        with patch("app_service_core.safe_remove_file", return_value=False), \
             patch("os.remove", side_effect=OSError("access denied")), \
             patch("os.chmod", side_effect=OSError("permission denied")):
            result = app_service._force_delete_and_verify(target)

        assert result is False
        assert target.exists()

    def test_safe_remove_succeeds_returns_true(self, app_service, tmp_path):
        """第一级 safe_remove_file 成功删除后不再走后续重试。"""
        target = tmp_path / "safe_remove_ok.strm"
        target.write_text("data")

        def safe_remove_then_check(p):
            Path(p).unlink()
            return True

        with patch("app_service_core.safe_remove_file", side_effect=safe_remove_then_check):
            result = app_service._force_delete_and_verify(target)

        assert result is True
        assert not target.exists()

    def test_os_remove_fallback_succeeds(self, app_service, tmp_path):
        """第一级失败但 os.remove 成功后返回 True。"""
        target = tmp_path / "fallback.strm"
        target.write_text("data")

        def os_remove_then_check(p):
            Path(p).unlink()
        with patch("app_service_core.safe_remove_file", return_value=False), \
             patch("os.remove", side_effect=os_remove_then_check):
            result = app_service._force_delete_and_verify(target)

        assert result is True
        assert not target.exists()

    def test_chmod_remove_fallback_succeeds(self, app_service, tmp_path):
        """前两级均失败后 chmod+remove 第三级成功返回 True。"""
        target = tmp_path / "readonly.strm"
        target.write_text("locked")

        remove_calls = [0]

        def os_remove_fake(path):
            remove_calls[0] += 1
            if remove_calls[0] == 1:
                raise OSError("access denied")  # 第一级失败
            else:
                Path(path).unlink()  # 第三级：chmod 后 os.remove 成功

        with patch("app_service_core.safe_remove_file", return_value=False), \
             patch("os.remove", side_effect=os_remove_fake), \
             patch("os.chmod"):
            result = app_service._force_delete_and_verify(target)

        assert result is True
        # File should be gone after chmod+remove
        assert not target.exists()


# ===========================================================================
# TestCleanupLocalEmptyDirs  —  cleanup_local_empty_dirs 直调测试
# ===========================================================================


class TestCleanupLocalEmptyDirs:
    """验证 cleanup_local_empty_dirs 对空目录的递归清理行为。"""

    @pytest.fixture
    def app_service(self, tmp_path):
        """构造 AppService 实例，a_roots、_a_to_b_map、c_root 指向临时目录。"""
        a_root = tmp_path / "A"
        b_root = tmp_path / "B"
        c_root = tmp_path / "C"
        a_root.mkdir(parents=True, exist_ok=True)
        b_root.mkdir(parents=True, exist_ok=True)
        c_root.mkdir(parents=True, exist_ok=True)
        app = AppService.__new__(AppService)
        app.config = MagicMock()
        app.config.paths = MagicMock()
        app.config.paths.c_root = str(c_root)
        app.db = MagicMock()
        app.a_roots = [a_root]
        app._a_to_b_map = {"map_id": b_root}
        return app

    def test_empty_dir_removed(self, app_service, tmp_path):
        """空目录被正常删除，非空目录保留。"""
        a_empty = app_service.a_roots[0] / "empty_sub"
        a_empty.mkdir(parents=True)
        b_deep_empty = app_service._a_to_b_map["map_id"] / "deep" / "nested"
        b_deep_empty.mkdir(parents=True)

        app_service.cleanup_local_empty_dirs()

        assert not a_empty.exists()
        assert not b_deep_empty.parent.exists()  # deep/ should be removed recursively
        assert not b_deep_empty.exists()

    def test_non_empty_dir_not_removed(self, app_service, tmp_path):
        """有内容的目录不被删除。"""
        a_root = app_service.a_roots[0]
        file = a_root / "keep.strm"
        file.write_text("content")
        sub_dir = a_root / "nonempty"
        sub_dir.mkdir()
        (sub_dir / "inside.txt").write_text("data")  # 确保子目录非空

        app_service.cleanup_local_empty_dirs()

        assert a_root.exists()
        assert file.exists()
        assert sub_dir.exists()

    def test_deeply_nested_empty_dirs_removed(self, app_service, tmp_path):
        """多级嵌套空目录——最内层先删除，递归向上。"""
        b_root = app_service._a_to_b_map["map_id"]
        (b_root / "S1" / "S1" / "S1").mkdir(parents=True)

        app_service.cleanup_local_empty_dirs()

        assert not (b_root / "S1").exists()

    def test_missing_root_dir_not_raise(self, app_service, tmp_path):
        """根目录不存在时不抛异常。"""
        app_service.a_roots = [tmp_path / "does_not_exist"]
        app_service.cleanup_local_empty_dirs()  # no exception

    def test_c_root_empty_subdirs_removed(self, app_service, tmp_path):
        """C 区空子目录也被清理。"""
        c_root = app_service.c_root
        (c_root / "ghost" / "entry").mkdir(parents=True)

        app_service.cleanup_local_empty_dirs()

        assert not (c_root / "ghost").exists()

    def test_multiple_a_roots_all_cleaned(self, app_service, tmp_path):
        """多个 A 根目录的空目录都被清理。"""
        a2 = tmp_path / "A2"
        a2.mkdir()
        (a2 / "empty_sub").mkdir()
        app_service.a_roots = [app_service.a_roots[0], a2]

        app_service.cleanup_local_empty_dirs()

        assert not (a2 / "empty_sub").exists()
