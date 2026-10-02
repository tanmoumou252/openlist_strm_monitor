"""app_service_core.py 保护根目录与快照方法单元测试

覆盖此前无直接测试的低难度方法：
- sync_protected_roots_from_config
- scan_removed_protected_roots
- persist_current_roots_snapshot
"""
from __future__ import annotations

import os
import logging
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app_service_core import AppService
from database import Database
from config import AppConfig, StrmStorageMapping


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


# ===========================================================================
# P0: get_engine_filter_paths —— 引擎范围过滤器（命名空间错配修复）
# 九类红测；谓词与决策零改动，仅修输入构造。
# ===========================================================================


class TestGetEngineFilterPaths:
    """`AppService.get_engine_filter_paths` 的九类失败测试。

    设计规格见主计划 §三 Step 1：
    1. 异源形态（mount=/strm + paths=[云资源前缀]）不被过滤；
    2. 边界保持（webdav 不在 paths 并集下 → 仍 skip_filtered）；
    3. 自指兼容（mount==paths==webdav 前缀）+ map 空回退 mount+告警；
    4. 尾斜杠加固（paths=[/cloud/番剧/] 与 webdav 前缀匹配）；
    5. 云根全域（paths=['/'] → 收集空串，谓词恒真）；
    6. 形态偏差 NF1（无前导斜杠/反斜杠归一）；
    7. F-A 根挂载双规范化（mount 域保 '/'、前缀域转 ''，与闸门侧可比）；
    8. 去重稳定序；
    9. API 失败兜底（空 map/无 entry/空 paths → 回退 mount+1 告警）；
       引擎集合空 → 空列表（现行"全部过滤"语义保持）。
    """

    def _storage(self, mount_path, paths, local_path=""):
        return StrmStorageMapping(
            mount_path=mount_path, paths=list(paths), local_path=local_path)

    def _passes(self, prefixes, webdav):
        """现行谓词：webdav == p or webdav.startswith(p + "/")"""
        return any(webdav == p or webdav.startswith(p + "/") for p in prefixes)

    # --- 1 异源形态 ---
    def test_heterogeneous_namespace_not_filtered(self, tmp_path):
        app = _make_app(tmp_path, strm_engine_paths=["/strm"])
        app.config.strm_storage_map = {
            "/strm/番剧": self._storage("/strm", ["/天翼云盘X/番剧"]),
        }
        result = app.get_engine_filter_paths()
        assert "/天翼云盘X/番剧" in result
        assert self._passes(result, "/天翼云盘X/番剧/a.strm")

    # --- 2 边界保持 ---
    def test_boundary_keeps_filtered(self, tmp_path):
        app = _make_app(tmp_path, strm_engine_paths=["/strm"])
        app.config.strm_storage_map = {
            "/strm/番剧": self._storage("/strm", ["/天翼云盘X/番剧"]),
        }
        result = app.get_engine_filter_paths()
        assert not self._passes(result, "/天翼云盘Y/电影/m.mp4")
        assert not self._passes(result, "/strm/anything")

    # --- 3 自指兼容 + map 空回退 ---
    def test_self_contained_and_empty_map_fallback(self, tmp_path, caplog):
        app = _make_app(tmp_path, strm_engine_paths=["/dav/map1"])
        app.config.strm_storage_map = {
            "/dav/map1/show": self._storage("/dav/map1", ["/dav/map1/show"]),
        }
        result = app.get_engine_filter_paths()
        assert "/dav/map1/show" in result
        assert self._passes(result, "/dav/map1/show/ep.mp4")

        # map 为空 → 回退该引擎 mount_path，显式断言回退告警（基准兼容形态）
        app.config.strm_storage_map = {}
        with caplog.at_level(logging.WARNING):
            result = app.get_engine_filter_paths()
        assert result == ["/dav/map1"]
        assert self._passes(result, "/dav/map1/show/ep.mp4")
        assert "回退挂载前缀" in caplog.text

    # --- 4 尾斜杠加固 ---
    def test_trailing_slash_hardened(self, tmp_path):
        app = _make_app(tmp_path, strm_engine_paths=["/strm"])
        app.config.strm_storage_map = {
            "/strm/番剧": self._storage("/strm", ["/cloud/番剧/"]),
        }
        result = app.get_engine_filter_paths()
        assert "/cloud/番剧" in result
        assert self._passes(result, "/cloud/番剧/x")

    # --- 5 云根全域 ---
    def test_cloud_root_global_domain(self, tmp_path):
        app = _make_app(tmp_path, strm_engine_paths=["/strm"])
        app.config.strm_storage_map = {
            "/strm/root": self._storage("/strm", ["/"]),
        }
        result = app.get_engine_filter_paths()
        assert "" in result
        # 现行谓词对空串前缀恒真（startswith("/")）
        assert self._passes(result, "/任意云盘/番剧/a.strm")

    # --- 6 NF1 形态偏差 ---
    def test_shape_variants_nf1(self, tmp_path):
        app = _make_app(tmp_path, strm_engine_paths=["/strm"])
        app.config.strm_storage_map = {
            "/strm/番剧": self._storage("/strm", ["天翼云盘/番剧"]),
            "/strm/番剧2": self._storage("/strm", ["\\天翼云盘\\番剧2"]),
        }
        result = app.get_engine_filter_paths()
        assert "/天翼云盘/番剧" in result
        assert "/天翼云盘/番剧2" in result
        assert self._passes(result, "/天翼云盘/番剧/x")
        assert self._passes(result, "/天翼云盘/番剧2/x")

    # --- 7 F-A 根挂载双规范化 ---
    def test_root_mount_dual_normalization(self, tmp_path):
        app = _make_app(tmp_path, strm_engine_paths=["/"])
        # mount 匹配域根保持 "/"，前缀输出域根转 ""
        assert app._norm_mount("/") == "/"
        assert app._norm_prefix("/") == ""
        app.config.strm_storage_map = {
            "/r/root": self._storage("/", ["/r/root"]),
        }
        result = app.get_engine_filter_paths(allowed_mounts={"/"})
        assert "/r/root" in result

    def test_root_mount_fallback_prefix(self, tmp_path, caplog):
        """根挂载且无 storage entry → 回退前缀为 ''（全域），告警一条。"""
        app = _make_app(tmp_path, strm_engine_paths=["/"])
        app.config.strm_storage_map = {}
        with caplog.at_level(logging.WARNING):
            result = app.get_engine_filter_paths()
        assert result == [""]
        assert "回退挂载前缀" in caplog.text

    # --- 8 去重稳定序 ---
    def test_dedup_stable_order(self, tmp_path):
        app = _make_app(tmp_path, strm_engine_paths=["/strm"])
        app.config.strm_storage_map = {
            "/strm/a": self._storage("/strm", ["/c/x", "/c/y", "/c/x"]),
            "/strm/b": self._storage("/strm", ["/c/y"]),
        }
        result = app.get_engine_filter_paths()
        assert result == ["/c/x", "/c/y"]

    def test_multiple_entries_same_mount_union(self, tmp_path):
        """last_dir 分组下同 mount 多 entry 全并集，禁用 paths[0] 便捷。"""
        app = _make_app(tmp_path, strm_engine_paths=["/strm"])
        app.config.strm_storage_map = {
            "/strm/番剧": self._storage("/strm", ["/云盘/番剧A"]),
            "/strm/番剧2": self._storage("/strm", ["/云盘/番剧B"]),
        }
        result = app.get_engine_filter_paths()
        assert result == ["/云盘/番剧A", "/云盘/番剧B"]

    # --- 9 API 失败兜底 ---
    def test_engine_without_entry_falls_back_to_mount(self, tmp_path, caplog):
        app = _make_app(tmp_path, strm_engine_paths=["/strm"])
        app.config.strm_storage_map = {
            "/other/m": self._storage("/other", ["/云盘/m"]),
        }
        with caplog.at_level(logging.WARNING):
            result = app.get_engine_filter_paths()
        assert result == ["/strm"]
        assert "回退挂载前缀" in caplog.text

    def test_entry_paths_empty_list_falls_back(self, tmp_path, caplog):
        app = _make_app(tmp_path, strm_engine_paths=["/strm"])
        app.config.strm_storage_map = {
            "/strm/empty": self._storage("/strm", []),
        }
        with caplog.at_level(logging.WARNING):
            result = app.get_engine_filter_paths()
        assert result == ["/strm"]
        assert "回退挂载前缀" in caplog.text

    def test_empty_engine_set_returns_empty_list(self, tmp_path):
        """引擎集合空 → 空列表（"全部过滤"语义保持，不误同步全量）。"""
        app = _make_app(tmp_path, strm_engine_paths=[])
        app.config.strm_storage_map = {
            "/strm/番剧": self._storage("/strm", ["/云盘/番剧"]),
        }
        assert app.get_engine_filter_paths() == []

    def test_allowed_mounts_intersection_with_normalization(self, tmp_path):
        """allowed_mounts 非空时两侧过 _norm_mount 后取交集（F-A）。"""
        app = _make_app(tmp_path, strm_engine_paths=["/strm", "/other"])
        app.config.strm_storage_map = {
            "/strm/x": self._storage("/strm", ["/云盘/番剧"]),
            "/other/y": self._storage("/other", ["/云盘/电影"]),
        }
        result = app.get_engine_filter_paths(allowed_mounts={"/strm"})
        assert "/云盘/番剧" in result
        assert "/云盘/电影" not in result

    def test_mount_match_with_trailing_slash(self, tmp_path):
        """mount 匹配两侧同走 _norm_mount——配置带尾斜杠也能匹配。"""
        app = _make_app(tmp_path, strm_engine_paths=["/strm/"])
        app.config.strm_storage_map = {
            "/strm/番剧": self._storage("/strm", ["/云盘/番剧"]),
        }
        result = app.get_engine_filter_paths()
        assert "/云盘/番剧" in result

    def test_recomputed_each_call_no_cache(self, tmp_path):
        """每次现算不加进程级缓存。"""
        app = _make_app(tmp_path, strm_engine_paths=["/strm"])
        app.config.strm_storage_map = {
            "/strm/a": self._storage("/strm", ["/c/x"]),
        }
        assert app.get_engine_filter_paths() == ["/c/x"]
        app.config.strm_storage_map = {
            "/strm/b": self._storage("/strm", ["/c/y"]),
        }
        assert app.get_engine_filter_paths() == ["/c/y"]


# ===========================================================================
# R-新5（c.9.2 Task 10）：refresh_webdav_root 根级迁移三态确认门
# ===========================================================================


class TestRefreshWebdavRootMigrationGate:
    """根级 B→C 迁移三态确认门（fail-closed，对齐 fs_list 契约）。

    链路：`list_contents` 请求失败/超时/非 200 → None；`_refresh_webdav_recursive`
    收到 None → return False；现行代码以 `if not exists:` 判定 → 直接
    `migrate_b_under_root_to_c` + `remove_known_folder_prefix` —— 一次网络
    超时击中根路径 = 该根下全部 B 区文件物理迁移 C 区（fail-open）。
    门禁后：仅 `OpenListAdminClient.check_exists`（Admin 三态版，非 WebDAV
    两态版）权威 `is False` 才迁移；True/None → WARNING 跳过。
    fixture 前提：`_make_app(strm_engine_paths=[])` → `is_valid_refresh_root`
    空集返回 True → cleanup_allowed=True。
    """

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="root_gate_"))

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_untrusted_list_does_not_migrate(self):
        """红测：list_contents→None（递归 False）时现行代码会迁移 = fail-open；
        门禁后 check_exists 不可信（此处 None）→ 不迁移、不清 known_folder"""
        app = _make_app(self.tmp, strm_engine_paths=[])
        app.admin_api.list_contents.return_value = None
        app.admin_api.check_exists.return_value = None
        with patch.object(app, "migrate_b_under_root_to_c") as mig:
            app.refresh_webdav_root("/strm", depth=1)
        mig.assert_not_called()
        app.db.remove_known_folder_prefix.assert_not_called()

    def test_authoritative_missing_migrates_as_before(self):
        """守恒：check_exists 权威 False → 迁移 + 清 known_folder 照常"""
        app = _make_app(self.tmp, strm_engine_paths=[])
        app.admin_api.list_contents.return_value = None
        app.admin_api.check_exists.return_value = False
        with patch.object(app, "migrate_b_under_root_to_c") as mig:
            app.refresh_webdav_root("/strm", depth=1)
        mig.assert_called_once_with("/strm")
        app.db.remove_known_folder_prefix.assert_called_once_with("/strm")

    def test_check_exists_none_skips_migration(self):
        """三态 None（不可信）→ WARNING 跳过迁移（宁可漏迁不可误迁）"""
        app = _make_app(self.tmp, strm_engine_paths=[])
        app.admin_api.list_contents.return_value = None
        app.admin_api.check_exists.return_value = None
        with patch.object(app, "migrate_b_under_root_to_c") as mig:
            app.refresh_webdav_root("/movies", depth=2)
        mig.assert_not_called()
        app.db.remove_known_folder_prefix.assert_not_called()

    def test_untrusted_list_logs_neutral_warning_when_cleanup_not_allowed(self, caplog):
        """c.9.3 Task A（O-1）：cleanup_allowed=False 且列表失败时，中性根级
        WARNING 必须在位（三态门版曾丢失该日志）且不迁移"""
        import logging as _logging
        app = _make_app(self.tmp, strm_engine_paths=["/other"])
        app.admin_api.list_contents.return_value = None
        app.admin_api.check_exists.return_value = None
        with caplog.at_level(_logging.WARNING):
            with patch.object(app, "migrate_b_under_root_to_c") as mig:
                app.refresh_webdav_root("/strm", depth=1)
        mig.assert_not_called()
        assert any("根路径列表失败或不可信" in r.getMessage() for r in caplog.records), \
            [r.getMessage() for r in caplog.records]
