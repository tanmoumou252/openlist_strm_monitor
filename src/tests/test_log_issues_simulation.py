"""三类真实日志问题的沙盒实验与回归验证。

本文件不是纯 mock 单元测试：A 区和 B 区使用测试目录，数据库使用真实临时
SQLite，核心同步流程使用真实 ``Database``、``AppService`` 与 ``SyncService``。
每组实验先用受控的旧行为复现问题，再验证生产修复路径。

目录策略：
- ``strm.test.A``：幂等刷新后保留，便于下次复用和人工检查；
- ``strm.test.B``：每个测试结束后删除；
- ``test_logs/log_issues_sim_<timestamp>.log``：保留本轮日志；
- 数据库和 C 区：使用 ``tempfile``，测试结束后删除。
"""
from __future__ import annotations

import inspect
import logging
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

_SRC_DIR = str(Path(__file__).resolve().parent.parent)
if _SRC_DIR not in sys.path:
    sys.path.append(_SRC_DIR)

from app_service_core import AppService  # noqa: E402
from config import AppConfig  # noqa: E402
from database import Database  # noqa: E402
from media_renamer import _extract_season_episode  # noqa: E402
from utils.strm_utils import make_strm_fingerprint, read_strm_webdav_path  # noqa: E402


_THIS_DIR = Path(__file__).resolve().parent
A_DIR = _THIS_DIR / "strm.test.A"
B_DIR = _THIS_DIR / "strm.test.B"
_LOG_DIR = _THIS_DIR.parent.parent / "test_logs"
_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 64
_GARBLED_BYTES = b"\xff\xfe\x00\x01\x02\x03\x7f\x80\xff" * 8


def _generate_test_files(a_dir: Path) -> dict:
    """幂等生成超过 100 个文件，并覆盖三类问题的样本。"""
    files: list[Path] = []
    collision_candidates: list[tuple[str, list[Path]]] = []

    def write_strm(rel: str, webdav_path: str) -> Path:
        path = a_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(webdav_path, encoding="utf-8")
        files.append(path)
        return path

    def write_bytes(rel: str, content: bytes) -> Path:
        path = a_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        files.append(path)
        return path

    def write_text(rel: str, content: str) -> Path:
        path = a_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        files.append(path)
        return path

    # 50 个标准番剧 STRM，作为真实同步基准。
    shows = [
        ("ShowA", 1, 8), ("ShowB", 2, 8), ("ShowC", 1, 8),
        ("ShowD", 3, 8), ("ShowE", 1, 8), ("OnePiece", 18, 5),
        ("OnePiece", 19, 5),
    ]
    for name, season, count in shows:
        for episode in range(1, count + 1):
            write_strm(
                f"anime/{name}/Season {season:02d}/S{season:02d}E{episode:02d}.strm",
                f"/cloud/mount/anime/{name}/S{season:02d}E{episode:02d}.mp4",
            )

    # 5 个电影 STRM。
    for name in ("Inception", "Matrix", "Interstellar", "Dune", "Tenet"):
        write_strm(
            f"movies/{name}/{name}.strm",
            f"/cloud/mount/movies/{name}/{name}.mkv",
        )

    # 问题 2：同一季集的不同原始 padding，旧 builder 会压成同一目标。
    padding_groups = [
        ("padding_s04e01", "S04E01", "S4E01"),
        ("padding_s04e02", "S04E02", "S4E02"),
    ]
    for group_name, padded, unpadded in padding_groups:
        paths: list[Path] = []
        for stem in (padded, unpadded):
            path = write_strm(
                f"anime/Padding Collision/Season 04/{stem}.strm",
                f"/cloud/mount/anime/Padding Collision/{stem}.mkv",
            )
            paths.append(path)
        collision_candidates.append((group_name, paths))

    # 旧日志中的复杂命名样本，确保生成器仍覆盖噪音、中文和超深路径。
    for episode in range(1, 25):
        write_strm(
            "anime/[Moozzi2] Mawaru Penguin Drum/Season 20/"
            f"[Moozzi2] Mawaru Penguin Drum - {episode:02d} "
            "(BD 1920x1080 x.264 FLACx2).strm",
            f"/cloud/mount/anime/Penguin/{episode:02d}.mkv",
        )
    for episode in (757, 758, 759):
        write_strm(
            f"anime/海贼王/Season 18/航海王 - S18E{episode} - 第 {episode} 集.strm",
            f"/cloud/mount/anime/海贼王/S18E{episode}.mkv",
        )
    for episode in range(1, 13):
        write_strm(
            "anime/地獄模式/Season 20/"
            f"Dynamis_One_..._{episode:02d}_Baha_1920x1080_AVC.strm",
            f"/cloud/mount/anime/地獄模式/{episode:02d}.mp4",
        )

    # 非 STRM 干扰和真实二进制 JPEG。
    for rel, text in (
        ("anime/ShowA/cover.jpg", "fake jpg"),
        ("anime/ShowA/poster.png", "fake png"),
        ("anime/ShowA/show.nfo", "<movie/>"),
        ("anime/ShowA/banner.jpg", "fake banner"),
        ("anime/ShowB/fanart.jpg", "fake fanart"),
        ("anime/ShowB/backdrop.png", "fake backdrop"),
        ("anime/ShowC/cover.jpg", "fake cover"),
        ("anime/ShowD/thumbnail.png", "fake thumbnail"),
        ("movies/Inception/poster.jpg", "fake poster"),
        ("movies/Inception/fanart.png", "fake fanart"),
        ("movies/Matrix/cover.jpg", "fake cover"),
        ("movies/Interstellar/backdrop.png", "fake backdrop"),
        ("movies/Dune/info.nfo", "<movie/>"),
        ("movies/Tenet/poster.jpg", "fake poster"),
    ):
        write_text(rel, text)
    for index in (1, 2, 9):
        write_bytes(
            f"anime/收集/[2013] 动漫3D杂/Season 4/图包/猫猫酱 ({index}).jpg",
            _JPEG_BYTES,
        )

    # 字幕样本。
    subtitle_text = "1\n00:00:01,000 --> 00:00:02,000\n字幕内容\n"
    for rel in (
        "anime/ShowA/Season 01/S01E01.chs.简体.srt",
        "anime/ShowA/Season 01/S01E01.eng.srt",
        "anime/ShowA/Season 01/S01E01.cht.繁體.ass",
        "anime/ShowA/Season 01/S01E02.chs.简体.srt",
        "anime/ShowB/Season 02/S02E01.chs.简体.srt",
        "anime/ShowB/Season 02/S02E02.eng.ass",
        "anime/ShowC/Season 01/S01E01.chs.简体.srt",
        "movies/Inception/Inception.chs.简体.srt",
        "movies/Matrix/Matrix.cht.繁體.ass",
    ):
        write_text(rel, subtitle_text)

    # 畸形 STRM 和边缘命名。
    write_bytes("bad_strm/empty.strm", b"")
    write_bytes("bad_strm/binary_garbage.strm", _GARBLED_BYTES)
    write_bytes("bad_strm/garbled.strm", b"\xff\xfe\x00\x01" * 5)
    write_strm("bad_strm/not_a_path.strm", "just some text no slash")
    for index in range(5):
        write_strm(
            f"deleted/missing_{index}.strm",
            f"/cloud/mount/deleted/missing_{index}.mp4",
        )
    write_strm("anime/中文季测试/第一季/S01E01.strm", "/cloud/mount/中文季测试/S01E01.mp4")
    write_strm("anime/仅集号/E01.strm", "/cloud/mount/仅集号/E01.mp4")
    write_strm("anime/全角Ｔｅｔｌｅ/Season 01/S01E01.strm", "/cloud/mount/全角/Title/S01E01.mp4")
    write_strm("anime/连续  空格/Season 01/S01E01.strm", "/cloud/mount/连续空格/S01E01.mp4")

    strm_files = [path for path in files if path.suffix == ".strm"]
    non_strm_files = [path for path in files if path.suffix != ".strm"]
    return {
        "files": files,
        "strm_count": len(strm_files),
        "non_strm_count": len(non_strm_files),
        "collision_candidates": collision_candidates,
    }


class SimulationBase:
    """建立真实 DB/AppService 沙盒，并负责目录及日志清理。"""

    def setup_method(self):
        A_DIR.mkdir(parents=True, exist_ok=True)
        B_DIR.mkdir(parents=True, exist_ok=True)
        self.manifest = _generate_test_files(A_DIR)
        self.tmp = Path(tempfile.mkdtemp(prefix="log_sim_"))
        self.db = Database(str(self.tmp / "bridge_sim.db"))
        self.c_dir = self.tmp / "c"
        self.c_dir.mkdir()

        self.config = Mock(spec=AppConfig)
        self.config.a_folders = [str(A_DIR)]
        self.config.paths = Mock()
        self.config.paths.b_root = str(B_DIR)
        self.config.paths.c_root = str(self.c_dir)
        self.config.behavior = Mock()
        self.config.behavior.ghost_protect_seconds = 300
        self.config.strm_engine_paths = []
        self.config.refresh = Mock(enabled=False)
        self.config.refresh_paths = []

        self.admin_api = Mock()
        self.admin_api.check_exists.return_value = True
        with patch("app_service_core.RefreshService"), patch("app_service_core.SubtitleHandler"):
            self.app = AppService(self.config, self.db, self.admin_api)

        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.log_file = _LOG_DIR / f"log_issues_sim_{timestamp}.log"
        self.handler = logging.FileHandler(self.log_file, encoding="utf-8")
        self.handler.setLevel(logging.DEBUG)
        self.handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        root = logging.getLogger()
        self.previous_log_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.handler)

    def teardown_method(self):
        root = logging.getLogger()
        root.removeHandler(self.handler)
        self.handler.close()
        root.setLevel(self.previous_log_level)
        shutil.rmtree(B_DIR, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_full_sync(self, *, use_bulk: bool = False):
        self.app.initial_scan_a(use_bulk=use_bulk)
        self.app.scan_a_to_b_full_sync(use_bulk=use_bulk)

    def _read_log(self) -> str:
        self.handler.flush()
        return self.log_file.read_text(encoding="utf-8")

    def _seed_committed_b_record(self, local_path: Path, webdav_path: str) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(webdav_path, encoding="utf-8")
        self.db.upsert_b(
            str(local_path), webdav_path, "/cloud/mount", None,
            fingerprint=make_strm_fingerprint(webdav_path), status="valid",
        )


class TestFileGeneration(SimulationBase):
    def test_total_count_ge_100(self):
        assert len(self.manifest["files"]) >= 100

    def test_strm_count_ge_65(self):
        assert self.manifest["strm_count"] >= 65

    def test_non_strm_count_ge_20(self):
        assert self.manifest["non_strm_count"] >= 20

    def test_collision_candidates_ge_2(self):
        assert len(self.manifest["collision_candidates"]) >= 2

    def test_binary_jpg_exists(self):
        binary_jpgs = [
            path for path in self.manifest["files"]
            if path.suffix == ".jpg" and path.read_bytes().startswith(b"\xff\xd8\xff\xe0")
        ]
        assert len(binary_jpgs) >= 2


class TestNewIssue1_DBLockContention(SimulationBase):
    """问题1：验证 B watcher 读路径不会抢写锁。"""

    def _hold_uncommitted_write(self):
        context = self.db.bulk_connection()
        conn = context.__enter__()
        conn.execute(
            "INSERT INTO a_strm_files(local_path, webdav_path, parent_webdav_path, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("/lock-holder.strm", "/lock-holder.mp4", "/", 0.0),
        )
        return context

    def test_baseline_db_lock_reproduced(self):
        """受控旧 getter 使用 connection() 时，WAL RESERVED 锁会阻塞它。"""
        target = B_DIR / "locked.strm"
        self._seed_committed_b_record(target, "/cloud/mount/locked.mp4")
        lock_context = self._hold_uncommitted_write()
        try:
            def legacy_getter(local_path: str):
                with self.db.rw_lock.read_locked(), self.db.connection() as conn:
                    row = conn.execute(
                        "SELECT local_path, webdav_path, parent_webdav_path, source_a_path, "
                        "fingerprint, status, updated_at FROM b_strm_files WHERE local_path = ?",
                        (local_path,),
                    ).fetchone()
                    return row

            with patch.object(self.db, "_PRAGMA_STATEMENTS", (
                "PRAGMA journal_mode=WAL", "PRAGMA busy_timeout=1",
            )):
                with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                    legacy_getter(str(target))
        finally:
            lock_context.__exit__(None, None, None)

    def test_fix_get_b_by_local_full_uses_read_conn(self):
        target = B_DIR / "locked.strm"
        webdav = "/cloud/mount/locked.mp4"
        self._seed_committed_b_record(target, webdav)
        lock_context = self._hold_uncommitted_write()
        try:
            result = self.db.get_b_by_local_full(str(target))
            assert result is not None
            assert result.webdav_path == webdav
        finally:
            lock_context.__exit__(None, None, None)

    def test_fix_all_readonly_getters_uses_read_conn(self):
        target = B_DIR / "locked.strm"
        webdav = "/cloud/mount/locked.mp4"
        self._seed_committed_b_record(target, webdav)
        self.db.upsert_a(str(A_DIR / "ShowA" / "Season 01" / "S01E01.strm"), webdav, "/cloud/mount")
        self.db.upsert_identity(
            make_strm_fingerprint(webdav), webdav,
            str(A_DIR / "ShowA" / "Season 01" / "S01E01.strm"), str(target),
        )
        lock_context = self._hold_uncommitted_write()
        try:
            calls = [
                lambda: self.db.get_a_by_local("missing"),
                lambda: self.db.get_b_by_local("missing"),
                lambda: self.db.get_a_by_webdav(webdav),
                lambda: self.db.get_b_by_webdav(webdav),
                self.db.get_all_a_records,
                self.db.get_all_b,
                self.db.get_all_c,
                self.db.get_known_folders,
                lambda: self.db.is_ghost_protected(webdav),
                self.db.get_protected_roots,
                self.db.get_protected_root_paths,
                self.db.get_protected_roots_snapshot_paths,
                lambda: self.db.get_control("missing"),
                lambda: self.db.get_b_under_root("/cloud/mount"),
                lambda: self.db.get_identity_by_fingerprint(make_strm_fingerprint(webdav)),
                lambda: self.db.get_identity_by_webdav(webdav),
                lambda: self.db.get_a_local_path_by_webdav(webdav),
                lambda: self.db.get_b_instances_by_fingerprint(make_strm_fingerprint(webdav)),
                lambda: self.db.get_b_by_local_full(str(target)),
                lambda: self.db.get_valid_b_instance_by_fingerprint(make_strm_fingerprint(webdav)),
                lambda: self.db.get_all_b_by_fingerprint(make_strm_fingerprint(webdav)),
                lambda: self.db.b_fingerprint_exists(make_strm_fingerprint(webdav)),
                lambda: self.db.get_a_count_under_root("/cloud/mount"),
                lambda: self.db.has_other_b_instance(make_strm_fingerprint(webdav), "missing"),
                lambda: self.db.get_media_boundary_by_fingerprint("missing"),
                lambda: self.db.get_media_boundaries_by_source_name("missing", str(B_DIR)),
                lambda: self.db.get_media_boundary_by_current_name("missing", str(B_DIR)),
                lambda: self.db.get_media_boundary_by_source_name_only("missing"),
                lambda: self.db.get_subtitle_by_local("missing"),
                lambda: self.db.subtitle_exists("missing"),
                lambda: self.db.get_subtitles_by_fingerprint("missing"),
            ]
            self.db._last_ghost_cleanup = __import__("time").time()
            for call in calls:
                call()
        finally:
            lock_context.__exit__(None, None, None)

    def test_read_only_methods_source_check(self):
        readonly_methods = (
            "get_a_by_local", "get_b_by_local", "get_a_by_webdav", "get_b_by_webdav",
            "get_all_a_records", "get_all_b", "get_all_c", "get_known_folders",
            "is_ghost_protected", "get_protected_roots", "get_protected_root_paths",
            "get_protected_roots_snapshot_paths", "get_control", "get_b_under_root",
            "get_identity_by_fingerprint", "get_identity_by_webdav", "get_a_local_path_by_webdav",
            "get_b_instances_by_fingerprint", "get_b_by_local_full",
            "get_valid_b_instance_by_fingerprint", "get_all_b_by_fingerprint",
            "b_fingerprint_exists", "get_a_count_under_root", "has_other_b_instance",
            "get_media_boundary_by_fingerprint", "get_media_boundaries_by_source_name",
            "get_media_boundary_by_current_name", "get_media_boundary_by_source_name_only",
            "get_subtitle_by_local", "subtitle_exists", "get_subtitles_by_fingerprint",
        )
        for name in readonly_methods:
            source = inspect.getsource(getattr(Database, name))
            assert "read_connection()" in source, f"{name} 未使用 read_connection()"
            assert "self.connection()" not in source, f"{name} 仍使用写连接"

    def test_b_handler_does_not_crash_sync(self):
        self._run_full_sync(use_bulk=True)
        b_files = list(B_DIR.rglob("*.strm"))
        assert b_files
        self.app.handle_b_created_or_modified(str(b_files[0]))
        log = self._read_log()
        assert "A -> B 全量同步完成" in log
        assert "database is locked" not in log

    def test_handle_b_not_dropping_record(self):
        self._run_full_sync(use_bulk=True)
        b_files = list(B_DIR.rglob("*.strm"))
        assert b_files
        target = b_files[0]
        self.app.handle_b_created_or_modified(str(target))
        record = self.db.get_b_by_local_full(str(target.resolve()))
        assert record is not None
        assert record.local_path == str(target.resolve())


class TestNewIssue2_PathCollisionPadding(SimulationBase):
    """问题2：验证不同原始 padding 不再被压到同一 B 目标。"""

    def _legacy_builder(self, a_local_path, webdav_path=None):
        """旧 builder 的受控副本，用于保留 baseline 实验。"""
        a_local = Path(a_local_path).resolve()
        a_root = self.app.get_a_root_for_path(a_local)
        assert a_root is not None
        rel = a_local.relative_to(a_root)
        if self.app._should_treat_as_movie(a_local, webdav_path):
            return self.app.b_root / rel
        suggested_name = __import__("media_renamer").suggest_rename(a_local)
        if suggested_name and webdav_path:
            season = __import__("media_renamer").extract_season_from_path(a_local)
            if season is None:
                season, _ = _extract_season_episode(a_local.name)
            _, episode = _extract_season_episode(a_local.name)
            if season is not None and episode is not None:
                rel_parts = list(rel.parts)
                season_index = next(
                    (i for i, part in enumerate(rel_parts[:-1])
                     if re.match(r"(?i)^season\s*\d+$", part)),
                    len(rel_parts) - 1,
                )
                return self.app.b_root / Path(
                    *rel_parts[:season_index], f"Season {season:02d}", suggested_name,
                )
        return self.app.b_root / rel

    def _padding_sources(self):
        return [path for _group, paths in self.manifest["collision_candidates"] for path in paths]

    def test_baseline_collides_and_drops(self):
        with patch.object(self.app, "build_b_path_from_a", side_effect=self._legacy_builder):
            self._run_full_sync(use_bulk=False)
        sources = self._padding_sources()
        targets = {str(self._legacy_builder(path, read_strm_webdav_path(path))) for path in sources}
        assert len(targets) < len(sources)
        assert "目标路径冲突" in self._read_log()
        assert list(B_DIR.glob("_MANUAL_REVIEW_*.md"))
        copied = {
            read_strm_webdav_path(path) for path in B_DIR.rglob("*.strm")
        }
        assert not {read_strm_webdav_path(path) for path in sources} <= copied

    def test_candidate_fix_both_sources_in_b(self):
        self._run_full_sync(use_bulk=False)
        source_webdavs = {read_strm_webdav_path(path) for path in self._padding_sources()}
        b_webdavs = {read_strm_webdav_path(path) for path in B_DIR.rglob("*.strm")}
        assert source_webdavs <= b_webdavs
        for _group, paths in self.manifest["collision_candidates"]:
            targets = [self.app.build_b_path_from_a(path, read_strm_webdav_path(path)) for path in paths]
            assert len({str(target) for target in targets}) == len(targets)

    def test_no_collision_warning_after_fix(self):
        self._run_full_sync(use_bulk=False)
        assert "目标路径冲突" not in self._read_log()
        assert not list(B_DIR.glob("_MANUAL_REVIEW_*.md"))

    def test_b_strm_content_represents_original_padding(self):
        self._run_full_sync(use_bulk=False)
        expected = {
            read_strm_webdav_path(path) for path in self._padding_sources()
        }
        actual = {
            read_strm_webdav_path(path) for path in B_DIR.rglob("*.strm")
        }
        assert expected <= actual
        for source in expected:
            assert sum(
                read_strm_webdav_path(path) == source
                for path in B_DIR.rglob("*.strm")
            ) == 1


class TestNewIssue3_BZoneHealthCheck(SimulationBase):
    """问题3：验证越界清理后 DB/磁盘一致且合法文件保留。"""

    def _create_b_file(self, relative: str, webdav: str) -> Path:
        path = B_DIR / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(webdav, encoding="utf-8")
        return path

    def test_out_of_bounds_no_a_record_deleted(self):
        path = self._create_b_file("orphan/no-source.strm", "/cloud/orphan/no-source.mkv")
        self.db.upsert_b(str(path), "/cloud/orphan/no-source.mkv", "/cloud/orphan", None,
                         make_strm_fingerprint("/cloud/orphan/no-source.mkv"))
        self.app.initial_scan_b()
        assert not path.exists()
        assert self.db.get_b_by_local_full(str(path)) is None

    def test_no_a_record_source_missing_path(self):
        path = self._create_b_file("orphan/missing-source.strm", "/cloud/orphan/missing-source.mkv")
        source = self.tmp / "gone" / "source.strm"
        self.db.upsert_b(str(path), "/cloud/orphan/missing-source.mkv", "/cloud/orphan", str(source),
                         make_strm_fingerprint("/cloud/orphan/missing-source.mkv"))
        self.db.upsert_identity(
            make_strm_fingerprint("/cloud/orphan/missing-source.mkv"),
            "/cloud/orphan/missing-source.mkv", str(source), str(path),
        )
        self.app.initial_scan_b()
        assert not path.exists()
        assert self.db.get_b_by_local_full(str(path)) is None

    def test_no_a_record_a_root_gone(self):
        path = self._create_b_file("orphan/gone-root.strm", "/cloud/orphan/gone-root.mkv")
        source = self.tmp / "outside-a-root" / "source.strm"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("/cloud/orphan/gone-root.mkv", encoding="utf-8")
        self.db.upsert_a(str(source), "/cloud/orphan/gone-root.mkv", "/cloud/orphan")
        self.db.upsert_b(str(path), "/cloud/orphan/gone-root.mkv", "/cloud/orphan", str(source),
                         make_strm_fingerprint("/cloud/orphan/gone-root.mkv"))
        self.app.initial_scan_b()
        assert not path.exists()
        assert self.db.get_b_by_local_full(str(path)) is None

    def test_no_engine_cfg_still_pass(self):
        a_source = A_DIR / "anime" / "ShowA" / "Season 01" / "S01E01.strm"
        webdav = "/cloud/mount/anime/ShowA/S01E01.mp4"
        a_source.write_text(webdav, encoding="utf-8")
        path = self._create_b_file("flat/S01E01.strm", webdav)
        fp = make_strm_fingerprint(webdav)
        self.db.upsert_a(str(a_source), webdav, "/cloud/mount/anime/ShowA")
        self.db.upsert_b(str(path), webdav, "/cloud/mount/anime/ShowA", str(a_source), fp)
        self.app.engine_configs = []
        self.app.initial_scan_b()
        assert path.exists()
        assert self.db.get_b_by_local_full(str(path)) is not None

    def test_orphan_engine_cfg_boundary_mismatch(self):
        a_source = A_DIR / "anime" / "ShowA" / "Season 01" / "S01E01.strm"
        webdav = "/cloud/mount/anime/ShowA/S01E01.mp4"
        a_source.write_text(webdav, encoding="utf-8")
        path = self._create_b_file("S01E01.strm", webdav)
        fp = make_strm_fingerprint(webdav)
        self.db.upsert_a(str(a_source), webdav, "/cloud/mount/anime/ShowA")
        self.db.upsert_b(str(path), webdav, "/cloud/mount/anime/ShowA", str(a_source), fp)
        self.app.engine_configs = [{
            "a_root_norm": str(A_DIR.resolve()),
            "mount_path": "/engine",
            "source_paths": ["/cloud/mount"],
        }]
        self.app.initial_scan_b()
        assert not path.exists()
        assert self.db.get_b_by_local_full(str(path)) is None

    def test_valid_b_files_preserved(self):
        self._run_full_sync(use_bulk=False)
        before = {path.resolve() for path in B_DIR.rglob("*.strm")}
        assert before
        self.app.initial_scan_b()
        after = {path.resolve() for path in B_DIR.rglob("*.strm")}
        assert before <= after

    def test_b_records_match_disk_after_cleanup(self):
        orphan = self._create_b_file("orphan/extra.strm", "/cloud/orphan/extra.mkv")
        self.db.upsert_b(str(orphan), "/cloud/orphan/extra.mkv", "/cloud/orphan", None,
                         make_strm_fingerprint("/cloud/orphan/extra.mkv"))
        self._run_full_sync(use_bulk=False)
        self.app.initial_scan_b()
        disk_paths = {str(path.resolve()) for path in B_DIR.rglob("*.strm")}
        db_paths = {str(record.local_path) for record in self.db.get_all_b_records()}
        assert db_paths == disk_paths


class TestSimulationLogRegression(SimulationBase):
    """保留原有日志阶段标记的回归保护。"""

    def test_phase_markers_and_progress(self):
        self._run_full_sync(use_bulk=False)
        log = self._read_log()
        assert "A -> B 全量同步开始" in log
        assert "索引阶段完成" in log
        assert "A -> B 全量同步完成" in log
        assert "成功=" in log or "A -> B 进度" in log

    def test_non_strm_and_binary_inputs_are_safe(self):
        non_strm = [path for path in self.manifest["files"] if path.suffix != ".strm"]
        assert len(non_strm) >= 20
        for path in non_strm:
            with patch("app_service_core.read_strm_webdav_path") as reader:
                self.app.handle_a_created_or_modified(str(path))
                reader.assert_not_called()
        for path in self.manifest["files"]:
            if path.suffix == ".jpg" and path.read_bytes().startswith(b"\xff\xd8\xff\xe0"):
                assert read_strm_webdav_path(path) is None


def test_generated_manifest_is_stable_enough():
    """生成器的核心数量约束也能在无 AppService 夹具时快速验证。"""
    with tempfile.TemporaryDirectory(prefix="manifest_sim_") as raw:
        manifest = _generate_test_files(Path(raw))
        assert len(manifest["files"]) >= 100
        assert manifest["strm_count"] >= 65
        assert manifest["non_strm_count"] >= 20
        assert len(manifest["collision_candidates"]) >= 2
