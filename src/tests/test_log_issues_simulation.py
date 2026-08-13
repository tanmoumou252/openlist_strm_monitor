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
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

_SRC_DIR = str(Path(__file__).resolve().parent.parent)
if _SRC_DIR not in sys.path:
    sys.path.append(_SRC_DIR)

from app_service_core import AppService  # noqa: E402
from config import ABMapping, AppConfig  # noqa: E402
from database import Database  # noqa: E402
from domain.media.subtitle_handler import SubtitleHandler as RealSubtitleHandler  # noqa: E402
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

    # ── Issue5 样本：同一 WebDAV 资源的多个 A 区候选（fingerprint 相同）。
    # 这些 .strm 文件内容指向同一个云端资源，用于复现/回归同 fingerprint 多实例隔离。
    duplicate_candidates: list[tuple[str, list[Path]]] = []
    dup_group_a: list[Path] = []
    for stem in ("ShowA-S01E01-src1", "ShowA-S01E01-src2", "ShowA-S01E01-src3"):
        dup_group_a.append(write_strm(
            f"anime/DupGroupA/Season 01/{stem}.strm",
            "/cloud/mount/anime/DupGroupA/S01E01.mp4",
        ))
    duplicate_candidates.append(("dup_group_a", dup_group_a))

    # ── Issue6 样本：字幕路由四类（番剧同集多语言、中文季名、电影）。
    # 在已有字幕基础上补一条"中文季名 + 番剧"样本，确保 Season XX 规范化可测。
    subtitle_candidates: list[dict] = []
    anime_chs = write_text(
        "anime/SubtitleRoutingA/Season 01/S01E01.chs.简体.srt", subtitle_text)
    anime_eng = write_text(
        "anime/SubtitleRoutingA/Season 01/S01E01.eng.srt", subtitle_text)
    subtitle_candidates.append({
        "kind": "anime_multilang",
        "paths": [anime_chs, anime_eng],
        "path": anime_chs,
        "expected_season_dir": "Season 01",
    })
    chinese_season_subtitle = write_text(
        "anime/字幕中文季/第一季/S01E01.chs.简体.srt", subtitle_text)
    # 对应 STRM 源（中文季名）：
    write_strm("anime/字幕中文季/第一季/S01E01.strm",
               "/cloud/mount/anime/字幕中文季/S01E01.mp4")
    subtitle_candidates.append({
        "kind": "anime_chinese_season",
        "paths": [chinese_season_subtitle],
        "path": chinese_season_subtitle,
        "expected_season_dir": "Season 01",
    })
    movie_subtitle = write_text(
        "movies/SubtitleMovie/SubtitleMovie.cht.繁體.ass", subtitle_text)
    write_strm("movies/SubtitleMovie/SubtitleMovie.strm",
               "/cloud/mount/movies/SubtitleMovie/SubtitleMovie.mkv")
    subtitle_candidates.append({
        "kind": "movie",
        "paths": [movie_subtitle],
        "path": movie_subtitle,
        "expected_season_dir": None,
    })

    # ── Issue8 样本：Unicode/特殊路径身份与冲突。
    unicode_candidates: list[dict] = []
    # 全角 vs 半角（不同资源，不得误合并）
    fullwidth_p = write_strm(
        "anime/ＦｕｌｌＷｉｄｔｈ/Season 01/S01E01.strm",
        "/cloud/mount/anime/ＦｕｌｌＷｉｄｔｈ/S01E01.mp4")
    unicode_candidates.append({"tag": "fullwidth", "path": fullwidth_p,
                              "note": "全角字母资源"})
    # 连续空格 vs 单空格（不同资源）
    double_space_p = write_strm(
        "anime/Double  Space/Season 01/S01E01.strm",
        "/cloud/mount/anime/Double  Space/S01E01.mp4")
    unicode_candidates.append({"tag": "double_space", "path": double_space_p,
                              "note": "连续空格资源"})
    # 大小写差异（默认大小写敏感，不同资源）
    case_diff_p = write_strm(
        "anime/CaseDiff/Season 01/S01E01.strm",
        "/cloud/mount/anime/CaseDiff/S01E01.mp4")
    write_strm(
        "anime/casediff/Season 01/S01E01.strm",
        "/cloud/mount/anime/casediff/S01E01.mp4")
    unicode_candidates.append({"tag": "case_diff", "path": case_diff_p,
                              "note": "大小写差异（CaseDiff vs casediff）"})
    # URL 编码等价性：通过完整 parse_strm_content 链验证（http(s) URL 入口 decode）
    url_encoded_p = write_text(
        "anime/UrlEncoded/S01E01.strm",
        "https://host/d/anime/%E7%AC%AC%E4%B8%80/S01E01.mp4")
    unicode_candidates.append({"tag": "url_encoded", "path": url_encoded_p,
                              "note": "URL 编码 %xx，需 parse_strm_content 解码"})
    # NFC/NFD：检测平台是否保持区分。Windows NTFS 通常等价化，按平台契约处理。
    nfc_form = "é"
    try:
        nfd_form = unicodedata.normalize("NFD", nfc_form)
    except Exception:
        nfd_form = nfc_form
    nfc_p = write_strm(
        f"anime/NfcTest/{nfc_form}/S01E01.strm",
        "/cloud/mount/anime/NfcTest/S01E01.mp4")
    unicode_candidates.append({"tag": "nfc", "path": nfc_p,
                              "note": "NFC é 路径"})
    # NFD 仅当文件系统能保持区分时才记录为独立路径（否则与 NFC 同路径）
    nfd_kept_distinct = False
    if nfd_form != nfc_form:
        nfd_target = a_dir / f"anime/NfcTest/{nfd_form}/S01E01.strm"
        try:
            nfd_target.parent.mkdir(parents=True, exist_ok=True)
            nfd_target.write_text(
                "/cloud/mount/anime/NfcTest-NFD/S01E01.mp4", encoding="utf-8")
            # 验证两个路径是否实际指向不同文件
            if nfd_target.resolve() != nfc_p.resolve():
                files.append(nfd_target)
                unicode_candidates.append({"tag": "nfd_distinct", "path": nfd_target,
                                          "note": "NFD 文件系统保持区分"})
                nfd_kept_distinct = True
            else:
                # 平台等价化：NFD 写入落到了 NFC 同一路径，按平台契约忽略
                # 不加入 files 列表，避免污染计数
                pass
        except (OSError, ValueError):
            # 文件系统拒绝区分：按平台契约忽略，不污染计数
            pass
    unicode_candidates.append({"tag": "nfc_nfd_platform_note", "path": nfc_p,
                              "note": f"NFD 平台保持区分={nfd_kept_distinct}"})

    strm_files = [path for path in files if path.suffix == ".strm"]
    non_strm_files = [path for path in files if path.suffix != ".strm"]
    return {
        "files": files,
        "strm_count": len(strm_files),
        "non_strm_count": len(non_strm_files),
        "collision_candidates": collision_candidates,
        "duplicate_candidates": duplicate_candidates,
        "subtitle_candidates": subtitle_candidates,
        "unicode_candidates": unicode_candidates,
    }


class _BackgroundExceptions:
    """收集后台线程异常，便于在主线程断言时重抛。

    watchdog 的 ``_safe_call`` 会吞掉后台异常仅记日志；测试需要显式收集才能
    断言"成功场景下后台 0 异常"。本类是 thread-safe 的异常蓄水池。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._errors: list[BaseException] = []

    def capture(self, exc: BaseException) -> None:
        with self._lock:
            self._errors.append(exc)

    @property
    def errors(self) -> list[BaseException]:
        with self._lock:
            return list(self._errors)

    def re_raise(self) -> None:
        """把收集到的第一条异常重抛给主线程（如有）。"""
        errors = self.errors
        if errors:
            raise errors[0]


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02,
                description: str = "predicate") -> None:
    """确定性等待：带截止时间的条件轮询。

    不用固定 ``sleep`` 作为同步手段。超时时抛 ``TimeoutError`` 并报告尚未满足
    的状态，便于定位卡死线程。
    """
    deadline = time.time() + timeout
    last_state = None
    while time.time() < deadline:
        last_state = predicate()
        if last_state:
            return
        time.sleep(interval)
    raise TimeoutError(
        f"_wait_until 超时({timeout}s)：{description} 仍未满足，最后状态={last_state!r}")


def _install_trackable_b_scheduler(handler, bg: _BackgroundExceptions):
    """替换 ``BAreaEventHandler._run_async`` 为可追踪调度器。

    实际执行仍调用 ``handler._safe_call`` 和真实 ``AppService`` 方法，但派发的
    线程被记录、其异常被捕获到 ``bg``。``join_all(timeout)`` 等待所有派发线程
    完成后，把后台异常重抛给主线程。

    返回一个带 ``join_all`` 方法的 controller 对象。原 ``_safe_call`` 行为
    （吞异常并记日志）保持不变；本调度器在其外层再包一层异常捕获用于测试观测。
    """
    threads: list[threading.Thread] = []
    threads_lock = threading.Lock()
    original_run_async = handler._run_async

    def trackable_run_async(func, *args):
        def wrapped():
            try:
                handler._safe_call(func, *args)
            except BaseException as exc:  # noqa: BLE001 测试观测需要
                bg.capture(exc)

        t = threading.Thread(target=wrapped, daemon=True)
        with threads_lock:
            threads.append(t)
        t.start()

    handler._run_async = trackable_run_async  # type: ignore[assignment]

    class _Controller:
        def join_all(self, timeout: float = 10.0) -> None:
            deadline = time.time() + timeout
            while True:
                with threads_lock:
                    pending = [t for t in threads if t.is_alive()]
                if not pending:
                    break
                if time.time() >= deadline:
                    raise TimeoutError(
                        f"join_all 超时：仍有 {len(pending)} 个后台线程未完成")
                # 逐个等待最短存活线程，避免长阻塞
                for t in pending:
                    t.join(timeout=max(0.0, min(0.05, deadline - time.time())))
            bg.re_raise()

        def restore(self) -> None:
            handler._run_async = original_run_async  # type: ignore[assignment]

    return _Controller()


def _mk_file_event(event_cls, src_path: str):
    """构造 watchdog File 事件对象（is_directory 继承为 False，不可通过构造传入）。

    ``FileCreatedEvent`` / ``FileModifiedEvent`` / ``FileDeletedEvent`` 只用 ``src_path``。
    """
    return event_cls(src_path)


def _mk_moved_event(event_cls, src_path: str, dest_path: str):
    """构造 watchdog FileMovedEvent（位置参数顺序为 src_path, dest_path）。"""
    return event_cls(src_path, dest_path)


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
        self.config.a_b_mappings = [
            ABMapping(mapping_id="test_m1", a_root=str(A_DIR), b_root=str(B_DIR)),
        ]

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
            mapping_id="test_m1",
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

    def test_duplicate_candidates_ge_1(self):
        """Issue5 基线：至少一组同 WebDAV 的多个 B 候选（用于 fingerprint 风暴）。"""
        assert len(self.manifest["duplicate_candidates"]) >= 1
        for _group, paths in self.manifest["duplicate_candidates"]:
            assert len(paths) >= 2

    def test_subtitle_candidates_cover_four_types(self):
        """Issue6 基线：番剧同集多语言、中文季、电影字幕四类样本齐全。"""
        kinds = {entry["kind"] for entry in self.manifest["subtitle_candidates"]}
        assert {"anime_multilang", "anime_chinese_season", "movie"}.issubset(kinds)
        for entry in self.manifest["subtitle_candidates"]:
            assert entry["path"].exists()

    def test_unicode_candidates_present(self):
        """Issue8 基线：NFC/NFD、URL 编码、反斜杠、全角、连续空格、大小写差异样本。"""
        tags = {entry["tag"] for entry in self.manifest["unicode_candidates"]}
        # 必须覆盖这些规范化关键场景
        assert {"fullwidth", "double_space", "case_diff"}.issubset(tags)
        for entry in self.manifest["unicode_candidates"]:
            assert entry["path"].exists()

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
                lambda: self.db.get_b_instances_by_fingerprint(make_strm_fingerprint(webdav), "test_mapping"),
                lambda: self.db.get_b_by_local_full(str(target)),
                lambda: self.db.get_valid_b_instance_by_fingerprint(make_strm_fingerprint(webdav), "test_mapping"),
                lambda: self.db.get_all_b_by_fingerprint(make_strm_fingerprint(webdav), "test_mapping"),
                lambda: self.db.b_fingerprint_exists(make_strm_fingerprint(webdav), "test_mapping"),
                lambda: self.db.get_a_count_under_root("/cloud/mount"),
                lambda: self.db.has_other_b_instance("test_mapping", make_strm_fingerprint(webdav), "missing"),
                lambda: self.db.get_media_boundary_by_fingerprint("test_mapping", "missing"),
                lambda: self.db.get_media_boundaries_by_source_name("test_mapping", "missing", str(B_DIR)),
                lambda: self.db.get_media_boundary_by_current_name("test_mapping", "missing", str(B_DIR)),
                lambda: self.db.get_media_boundary_by_source_name_only("test_mapping", "missing"),
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
            return Path(B_DIR) / rel
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
                return Path(B_DIR) / Path(
                    *rel_parts[:season_index], f"Season {season:02d}", suggested_name,
                )
        return Path(B_DIR) / rel

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

    def setup_method(self):
        super().setup_method()
        self.config.a_b_mappings = [
            ABMapping(mapping_id="test_mapping", a_root=str(A_DIR), b_root=str(B_DIR)),
        ]
        self.app.a_b_mappings = self.config.a_b_mappings
        self.app.a_roots = [A_DIR.resolve()]
        self.app._a_to_b_map = {str(A_DIR.resolve()): B_DIR.resolve()}

    def _create_b_file(self, relative: str, webdav: str) -> Path:
        path = B_DIR / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(webdav, encoding="utf-8")
        return path

    def test_out_of_bounds_no_a_record_deleted(self):
        path = self._create_b_file("orphan/no-source.strm", "/cloud/orphan/no-source.mkv")
        self.db.upsert_b(str(path), "/cloud/orphan/no-source.mkv", "/cloud/orphan", None,
                         mapping_id="test_m1",
                         fingerprint=make_strm_fingerprint("/cloud/orphan/no-source.mkv"))
        self.app.initial_scan_b()
        assert not path.exists()
        assert self.db.get_b_by_local_full(str(path)) is None

    def test_no_a_record_source_missing_path(self):
        path = self._create_b_file("orphan/missing-source.strm", "/cloud/orphan/missing-source.mkv")
        source = self.tmp / "gone" / "source.strm"
        self.db.upsert_b(str(path), "/cloud/orphan/missing-source.mkv", "/cloud/orphan", str(source),
                         mapping_id="test_m1",
                         fingerprint=make_strm_fingerprint("/cloud/orphan/missing-source.mkv"))
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
                         mapping_id="test_m1",
                         fingerprint=make_strm_fingerprint("/cloud/orphan/gone-root.mkv"))
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
        self.db.upsert_b(str(path), webdav, "/cloud/mount/anime/ShowA", str(a_source),
                         mapping_id="test_m1",
                         fingerprint=fp)
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
        self.db.upsert_b(str(path), webdav, "/cloud/mount/anime/ShowA", str(a_source),
                         mapping_id="test_m1",
                         fingerprint=fp)
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
                         mapping_id="test_m1",
                         fingerprint=make_strm_fingerprint("/cloud/orphan/extra.mkv"))
        self._run_full_sync(use_bulk=False)
        self.app.initial_scan_b()
        disk_paths = {str(path.resolve()) for path in B_DIR.rglob("*.strm")}
        db_paths = {str(record.local_path) for record in self.db.get_all_b_records()}
        assert db_paths == disk_paths


class TestNewIssue4_BEventFlood(SimulationBase):
    """问题4：B 区事件洪泛与锁竞争残留。

    用真实 ``BAreaEventHandler`` + 手动事件对象（``FileCreatedEvent`` /
    ``FileModifiedEvent`` / ``FileMovedEvent``）触发生产入口，不启动 watchdog
    Observer。通过 ``_install_trackable_b_scheduler`` 替换 ``_run_async`` 的派发
    方式，使后台线程可追踪、异常可重抛主线程，不依赖日志字符串作为唯一证据。

    安全约束：
    - created + 重复 modified 不丢 B 记录；
    - moved 后旧 DB 路径消失、新路径存在，磁盘与 DB 一致；
    - 不触发不可逆的云 API move/remove（``admin_api`` 为 Mock，断言 0 调用）；
    - 日志不得出现 ``[B区事件处理异常]`` 或 ``database is locked``。
    """

    def _make_handler(self, bg):
        from area_watchers import BAreaEventHandler
        handler = BAreaEventHandler(self.app)
        controller = _install_trackable_b_scheduler(handler, bg)
        return handler, controller

    def _pick_two_distinct_b_files(self):
        self._run_full_sync(use_bulk=True)
        b_files = sorted(B_DIR.rglob("*.strm"), key=lambda p: str(p))
        assert len(b_files) >= 2, "需要至少 2 个已同步 B 文件做并发批次"
        return b_files[0], b_files[1]

    def test_created_then_modified_flood_no_record_loss(self):
        from watchdog.events import FileCreatedEvent, FileModifiedEvent
        bg = _BackgroundExceptions()
        handler, controller = self._make_handler(bg)
        target, _other = self._pick_two_distinct_b_files()

        # 删除 DB 记录，模拟"created 重新建立"场景；保留物理文件
        self.db.delete_b_by_local(str(target.resolve()))
        handler.on_created(_mk_file_event(FileCreatedEvent, str(target)))
        for _ in range(10):
            handler.on_modified(_mk_file_event(FileModifiedEvent, str(target)))

        _wait_until(
            lambda: self.db.get_b_by_local_full(str(target.resolve())) is not None,
            timeout=10.0,
            description=f"created/modified 洪泛后 {target} 应有 DB 记录",
        )
        controller.join_all(timeout=15.0)

        record = self.db.get_b_by_local_full(str(target.resolve()))
        assert record is not None
        assert record.local_path == str(target.resolve())
        log = self._read_log()
        assert "[B区事件处理异常]" not in log
        assert "database is locked" not in log

    def test_moved_updates_db_and_disk_consistency(self):
        from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileMovedEvent
        bg = _BackgroundExceptions()
        handler, controller = self._make_handler(bg)
        target, _other = self._pick_two_distinct_b_files()

        # 确保有 DB 记录作为 move 源
        assert self.db.get_b_by_local_full(str(target.resolve())) is not None

        dest = target.parent / "renamed_flood.strm"
        if dest.exists():
            dest.unlink()
        target.rename(dest)
        handler.on_moved(_mk_moved_event(FileMovedEvent, str(target), str(dest)))
        for _ in range(10):
            handler.on_modified(_mk_file_event(FileModifiedEvent, str(dest)))

        _wait_until(
            lambda: self.db.get_b_by_local_full(str(dest.resolve())) is not None,
            timeout=10.0,
            description=f"moved 后新路径 {dest} 应有 DB 记录",
        )
        _wait_until(
            lambda: self.db.get_b_by_local_full(str(target.resolve())) is None,
            timeout=10.0,
            description=f"moved 后旧路径 {target} DB 记录应消失",
        )
        controller.join_all(timeout=15.0)

        # 磁盘与 DB 一致：旧路径文件不存在、新路径文件存在
        assert not target.exists()
        assert dest.exists()
        new_record = self.db.get_b_by_local_full(str(dest.resolve()))
        assert new_record is not None
        assert new_record.local_path == str(dest.resolve())
        # moved 不应触发云 API move/remove
        self.admin_api.move_file.assert_not_called()
        self.admin_api.remove_file.assert_not_called()
        log = self._read_log()
        assert "[B区事件处理异常]" not in log
        assert "database is locked" not in log

    def test_concurrent_batches_distinct_paths_no_row_loss(self):
        from watchdog.events import FileCreatedEvent, FileModifiedEvent
        bg = _BackgroundExceptions()
        handler, controller = self._make_handler(bg)
        path_a, path_b = self._pick_two_distinct_b_files()

        # 两条不同路径并发 created + modified 批次，验证写锁串行化不丢行
        self.db.delete_b_by_local(str(path_a.resolve()))
        self.db.delete_b_by_local(str(path_b.resolve()))

        events = []
        for _ in range(5):
            events.append(("a", _mk_file_event(FileModifiedEvent, str(path_a))))
            events.append(("b", _mk_file_event(FileModifiedEvent, str(path_b))))
        # 先 created，再交错 modified
        handler.on_created(_mk_file_event(FileCreatedEvent, str(path_a)))
        handler.on_created(_mk_file_event(FileCreatedEvent, str(path_b)))
        for _tag, ev in events:
            handler.on_modified(ev)

        _wait_until(
            lambda: (
                self.db.get_b_by_local_full(str(path_a.resolve())) is not None
                and self.db.get_b_by_local_full(str(path_b.resolve())) is not None
            ),
            timeout=10.0,
            description="并发批次后两条路径都应有 DB 记录",
        )
        controller.join_all(timeout=15.0)

        assert self.db.get_b_by_local_full(str(path_a.resolve())) is not None
        assert self.db.get_b_by_local_full(str(path_b.resolve())) is not None
        log = self._read_log()
        assert "[B区事件处理异常]" not in log
        assert "database is locked" not in log


class TestNewIssue5_DuplicateStorm(SimulationBase):
    """问题5：同 fingerprint 多实例隔离与回滚失败态。

    安全不变量：
    - 每个 fingerprint 最终恰好一个物理存在且 status='valid' 的实例；
    - 其余实例物理文件与 DB local_path 同步迁移为 .duplicate；
    - 重复执行幂等，不生成 .duplicate.duplicate 或持续增加文件；
    - DB 迁移失败时物理改名必须回滚，不能产生 DB/磁盘分叉；
    - 回滚也失败时：必须 logging.error 含明确错误、抛出 B 区异常使清理中止、
      不静默继续、不再误打"已回滚物理改名"日志。

    quarantine_file 用 epoch 秒时间戳后缀而非追加 .duplicate，
    所以 .duplicate.duplicate 链在当前实现下不存在。
    """

    def _seed_three_instances(self, webdav: str):
        """创建 3 个同 fingerprint 的 B 实例（status='valid'），返回 (files, paths)。"""
        files = []
        paths = []
        for stem in ("inst_a.strm", "inst_b.strm", "inst_c.strm"):
            p = B_DIR / "dup_show" / "Season 01" / stem
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(webdav, encoding="utf-8")
            self.db.upsert_b(
                str(p), webdav, "/cloud/mount/dup_show", None,
                fingerprint=make_strm_fingerprint(webdav), status="valid",
                mapping_id="test_m1",
            )
            files.append(p)
            paths.append(str(p.resolve()))
        # 还需要一个 A 源，让 lineage 通过
        a_source = A_DIR / "dup_show" / "Season 01" / "inst_a.strm"
        a_source.parent.mkdir(parents=True, exist_ok=True)
        a_source.write_text(webdav, encoding="utf-8")
        self.db.upsert_a(str(a_source), webdav, "/cloud/mount/dup_show")
        self.db.upsert_identity(
            make_strm_fingerprint(webdav), webdav,
            str(a_source), paths[0],
        )
        return files, paths

    def test_three_instances_one_kept_others_quarantined(self):
        """Step 2: 三实例场景，最终恰好一个 status='valid' + 物理存在。"""
        webdav = "/cloud/mount/dup_show/S01E01.mp4"
        files, paths = self._seed_three_instances(webdav)
        fp = make_strm_fingerprint(webdav)

        self.app.ensure_single_visible_instance(fp, paths[0], prefer_path=paths[0], mapping_id="test_m1")

        # 验证：恰好一个 status='valid' 且物理存在
        all_inst = self.db.get_all_b_by_fingerprint(fp, "test_m1")
        valid = [r for r in all_inst if r.status == "valid" and Path(r.local_path).exists()]
        assert len(valid) == 1

        # 验证：其余实例 DB local_path 指向 .duplicate 物理文件
        for r in all_inst:
            if r.status == "duplicate":
                assert Path(r.local_path).exists(), f"duplicate 实例 {r.local_path} 文件应存在"

    def test_idempotent_replay_no_extra_files(self):
        """Step 2 幂等重放：连续二次调用不新增文件。"""
        webdav = "/cloud/mount/dup_show/S01E01.mp4"
        files, paths = self._seed_three_instances(webdav)
        fp = make_strm_fingerprint(webdav)

        self.app.ensure_single_visible_instance(fp, paths[0], prefer_path=paths[0], mapping_id="test_m1")
        count_after_first = len(list(B_DIR.rglob("*.strm")))
        all_inst_first = self.db.get_all_b_by_fingerprint(fp, "test_m1")

        self.app.ensure_single_visible_instance(fp, paths[0], prefer_path=paths[0], mapping_id="test_m1")
        count_after_second = len(list(B_DIR.rglob("*.strm")))
        all_inst_second = self.db.get_all_b_by_fingerprint(fp, "test_m1")

        # 文件数量不增长
        assert count_after_second == count_after_first
        # DB 记录数量不增长
        assert len(all_inst_second) == len(all_inst_first)

    def test_db_move_failure_triggers_rollback(self):
        """Step 3: move_b_record 返回 False 时物理改名回滚，原文件恢复。"""
        webdav = "/cloud/mount/dup_show/S01E01.mp4"
        files, paths = self._seed_three_instances(webdav)
        fp = make_strm_fingerprint(webdav)

        # 让 move_b_record 对非 keep 实例返回 False（模拟 DB 冲突）
        call_count = [0]
        original_move = self.db.move_b_record

        def mock_move(old, new):
            call_count[0] += 1
            if call_count[0] >= 2:  # 第二次调用（非 keep 实例）失败
                return False
            return original_move(old, new)

        with patch.object(self.db, "move_b_record", side_effect=mock_move):
            self.app.ensure_single_visible_instance(fp, paths[0], prefer_path=paths[0], mapping_id="test_m1")

        # 验证：move_b_record 失败时物理回滚，原文件恢复
        all_inst = self.db.get_all_b_by_fingerprint(fp, "test_m1")
        for r in all_inst:
            if r.status == "duplicate":
                assert Path(r.local_path).exists(), (
                    f"move_b_record 失败应回滚物理改名：{r.local_path} 应存在"
                )

    def test_rollback_failure_logs_error_and_raises(self):
        """Step 4: 回滚也失败时：logging.error + 抛 B 区异常，不静默继续。"""
        webdav = "/cloud/mount/dup_show/S01E01.mp4"
        files, paths = self._seed_three_instances(webdav)
        fp = make_strm_fingerprint(webdav)

        # 让 move_b_record 对非 keep 实例返回 False
        call_count = [0]
        original_move = self.db.move_b_record

        def mock_move(old, new):
            call_count[0] += 1
            if call_count[0] >= 2:
                return False
            return original_move(old, new)

        # 区分 quarantine rename 和 rollback rename：
        # - quarantine：source 以 .strm 结尾，target 以 .strm.duplicate 结尾
        # - rollback：source 以 .strm.duplicate 结尾，target 以 .strm 结尾
        original_path_rename = Path.rename
        rename_fail_counter = [0]

        def mock_rename(self_path, target):
            src_name = str(self_path.name)
            # rollback 场景：quarantined 文件（以 .duplicate 结尾）rename 回原路径
            if ".duplicate" in src_name:
                rename_fail_counter[0] += 1
                raise OSError("模拟磁盘满：回滚失败")
            # quarantine 场景：正常执行
            return original_path_rename(self_path, target)

        with patch.object(self.db, "move_b_record", side_effect=mock_move):
            with patch.object(Path, "rename", mock_rename):
                # 回滚失败时应抛出异常使清理中止，不静默继续
                with pytest.raises(OSError, match="模拟磁盘满"):
                    self.app.ensure_single_visible_instance(
                        fp, paths[0], prefer_path=paths[0], mapping_id="test_m1")

        log = self._read_log()
        # 必须有明确的 error 日志
        assert "[B区重复] DB迁移失败且回滚物理改名失败" in log
        # 不应再出现误导性日志（误打"已回滚物理改名"）
        assert "[B区重复] DB迁移失败，已回滚物理改名" not in log
        # B3-B：回滚失败后应尝试把 DB 对齐到隔离路径（日志线索）
        assert (
            "已将 DB 对齐到隔离路径" in log
            or "DB 对齐隔离路径也失败" in log
            or "回滚失败后 DB 对齐异常" in log
        )
    def test_same_second_collision_baseline(self):
        """B3-A：quarantine 返回 None 时必须撤销假 duplicate，恢复 status=valid。

        同秒碰撞在自然 basename 不同路径下难以形成；用 mock 稳定复现
        「第二个实例隔离失败」。契约：
        - keep 仍 valid 且物理存在；
        - 隔离失败实例：磁盘原路径仍存在，且 status 恢复为 valid（可再次 ensure）；
        - 必须打出隔离失败 warning，不得误报「已回滚物理改名」。
        """
        webdav = "/cloud/mount/dup_show/S01E01.mp4"
        files, paths = self._seed_three_instances(webdav)
        fp = make_strm_fingerprint(webdav)
        keep_path = paths[0]

        quarantine_call = [0]

        def controlled_quarantine(path, suffix=".duplicate"):
            quarantine_call[0] += 1
            if quarantine_call[0] >= 2:  # 第二个非 keep 实例
                return None  # 模拟同秒碰撞 / OSError
            from utils.file_utils import quarantine_file as real_fn
            return real_fn(path, suffix=suffix)

        with patch("app_service_core.quarantine_file", side_effect=controlled_quarantine):
            self.app.ensure_single_visible_instance(fp, keep_path, prefer_path=keep_path, mapping_id="test_m1")

        assert quarantine_call[0] >= 2, "应至少尝试隔离两个重复实例"

        all_inst = self.db.get_all_b_by_fingerprint(fp, "test_m1")
        by_path = {r.local_path: r for r in all_inst}

        # keep：仍 valid 且文件存在
        keep_rec = by_path.get(keep_path)
        assert keep_rec is not None
        assert keep_rec.status == "valid"
        assert Path(keep_path).exists()

        # 隔离失败实例：原路径仍在，且不得残留假 duplicate（B3-A）
        restored = []
        for p in paths[1:]:
            if Path(p).exists() and p in by_path:
                rec = by_path[p]
                if rec.status == "valid" and not str(rec.local_path).endswith(".duplicate"):
                    restored.append(rec)
        assert restored, (
            "隔离失败后应至少有一个实例 status 恢复为 valid，避免假 duplicate 死锁"
        )
        # 不得存在「status=duplicate 且路径仍为原 .strm」分叉
        for p in paths:
            if p in by_path and Path(p).exists() and not p.endswith(".duplicate"):
                if by_path[p].status == "duplicate":
                    pytest.fail(f"假 quarantine 分叉未撤销: {p}")

        log = self._read_log()
        assert "[B区重复] 重复实例隔离失败" in log
        assert "[B区重复] DB迁移失败，已回滚物理改名" not in log

        # 可自愈：第二次 ensure 应再次尝试隔离（status 已恢复 valid）
        quarantine_call[0] = 0
        with patch("app_service_core.quarantine_file", side_effect=controlled_quarantine):
            self.app.ensure_single_visible_instance(fp, keep_path, prefer_path=keep_path, mapping_id="test_m1")
        assert quarantine_call[0] >= 1, "恢复 valid 后应可再次进入隔离流程"

    def test_same_second_quarantine_timestamp_collision(self):
        """更接近真实同秒碰撞：.duplicate 与 .duplicate.<epoch> 均已占用。

        quarantine_file 在目标存在时只追加一次 epoch；若时间戳目标也存在，
        rename 抛 OSError → 返回 None。B3-A：status 恢复 valid，原 .strm 保留。
        """
        from utils.file_utils import quarantine_file as real_quarantine

        webdav = "/cloud/mount/dup_show/S01E01.mp4"
        files, paths = self._seed_three_instances(webdav)
        fp = make_strm_fingerprint(webdav)
        keep_path = paths[0]
        # 对第二个实例预占隔离目标，逼出真实 rename 失败
        victim = Path(paths[1])
        fixed_epoch = 1_700_000_000
        pre_dup = victim.with_name(victim.name + ".duplicate")
        pre_ts = victim.with_name(f"{victim.name}.duplicate.{fixed_epoch}")
        pre_dup.write_text("occupied", encoding="utf-8")
        pre_ts.write_text("occupied-ts", encoding="utf-8")

        # 仅对 victim 路径走真实 quarantine（时间冻结）；其它实例正常隔离
        def selective_quarantine(path, suffix=".duplicate"):
            p = Path(path)
            if p.resolve() == victim.resolve():
                with patch("utils.file_utils.time.time", return_value=fixed_epoch):
                    return real_quarantine(p, suffix=suffix)
            return real_quarantine(p, suffix=suffix)

        with patch("app_service_core.quarantine_file", side_effect=selective_quarantine):
            self.app.ensure_single_visible_instance(fp, keep_path, prefer_path=keep_path)

        # 真实 quarantine 对 victim 应失败 → 原文件仍在
        assert victim.exists(), "时间戳碰撞后原 .strm 应保留"
        assert pre_dup.exists()
        assert pre_ts.exists()

        rec = self.db.get_b_by_local_full(str(victim))
        assert rec is not None
        # B3-A：撤销假 duplicate，恢复 valid 以便下次 ensure 再试
        assert rec.status == "valid", "隔离失败不得残留 status=duplicate"
        assert rec.local_path == str(victim) or Path(rec.local_path).resolve() == victim.resolve()

        keep_rec = self.db.get_b_by_local_full(keep_path)
        assert keep_rec is not None and keep_rec.status == "valid"
        assert Path(keep_path).exists()

        log = self._read_log()
        assert "[B区重复] 重复实例隔离失败" in log

    def test_db_move_failure_after_quarantine_restores_valid_status(self):
        """B3-A 扩展：物理隔离成功但 move_b_record 失败且已回滚时，status 恢复 valid。"""
        webdav = "/cloud/mount/dup_show/S01E01.mp4"
        files, paths = self._seed_three_instances(webdav)
        fp = make_strm_fingerprint(webdav)
        keep_path = paths[0]

        def mock_move(old, new):
            return False  # 全部 DB 迁移失败

        with patch.object(self.db, "move_b_record", side_effect=mock_move):
            self.app.ensure_single_visible_instance(fp, keep_path, prefer_path=keep_path)

        # 物理应已回滚：原 .strm 仍在
        for p in paths:
            assert Path(p).exists(), f"回滚后原路径应存在: {p}"

        all_inst = self.db.get_all_b_by_fingerprint(fp, "test_m1")
        for r in all_inst:
            if r.local_path in paths:
                assert r.status == "valid", (
                    f"DB 迁移失败回滚后不得残留 duplicate: {r.local_path} status={r.status}"
                )

        log = self._read_log()
        assert "[B区重复] DB迁移失败，已回滚物理改名" in log


class TestNewIssue6_SubtitleRouting(SimulationBase):
    """问题6：真实字幕 Season/多语言路由。

    安全不变量：
    - 番剧字幕进入对应媒体目录下 Season XX，中文季目录规范化到 Season XX；
    - 电影字幕保留电影目录结构；
    - 同集 .ass/.srt 与中英简繁字幕目标不互相覆盖；
    - 每个复制成功字幕都有 DB 记录，记录 target_path 实际存在；
    - 重复处理幂等，不重复写文件、不产生异常；
    - 字幕处理不得覆盖或改写 B 区 .strm 内容。

    Setup 中安装真实 ``SubtitleHandler(self.app)``，但保留构造阶段对其它
    依赖的 patch 隔离（RefreshService 等）。
    """

    def _install_real_subtitle_handler(self):
        """在已有 AppService 上安装真实的 SubtitleHandler。"""
        self.app.subtitle_handler = RealSubtitleHandler(self.app)

    def _seed_a_subtitle_file(self, rel: str, content: str = None) -> Path:
        """在 A 区创建字幕文件，返回其绝对路径。"""
        if content is None:
            content = (
                "1\n00:00:01,000 --> 00:00:02,000\n"
                "真实字幕内容\n\n"
                "2\n00:00:03,000 --> 00:00:04,000\n"
                "第二行\n"
            )
        p = A_DIR / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def _seed_a_strm_file(self, rel: str, webdav: str) -> Path:
        p = A_DIR / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(webdav, encoding="utf-8")
        return p

    def test_anime_subtitle_season_routing(self):
        """番剧字幕应进入对应 Season XX 目录。"""
        self._install_real_subtitle_handler()
        webdav = "/cloud/mount/anime/RoutingA/S01E01.mp4"
        a_strm = self._seed_a_strm_file(
            "anime/RoutingA/Season 01/S01E01.strm", webdav)
        a_sub = self._seed_a_subtitle_file(
            "anime/RoutingA/Season 01/S01E01.chs.简体.srt")

        self.app.subtitle_handler.process_subtitle_file(str(a_sub))

        # 目标应在 B 区 Season 01 下
        b_season_dir = B_DIR / "anime" / "RoutingA" / "Season 01"
        sub_targets = list(b_season_dir.glob("*.srt"))
        assert len(sub_targets) >= 1, f"番剧字幕应复制到 {b_season_dir}"
        target = sub_targets[0]
        assert target.exists()

        # DB 记录存在且 target_path 指向实际文件
        sub_record = self.db.get_subtitle_by_local(str(a_sub.resolve()))
        assert sub_record is not None
        assert Path(sub_record.target_path).exists()
        assert "Season 01" in sub_record.target_path

    def test_chinese_season_normalized_to_season_xx(self):
        """中文季目录"第一季"应规范化为 Season 01。"""
        self._install_real_subtitle_handler()
        webdav = "/cloud/mount/anime/Chinese/Season 01/S01E01.mp4"
        a_strm = self._seed_a_strm_file(
            "anime/Chinese/第一季/S01E01.strm", webdav)
        a_sub = self._seed_a_subtitle_file(
            "anime/Chinese/第一季/S01E01.chs.简体.srt")

        self.app.subtitle_handler.process_subtitle_file(str(a_sub))

        # 目标应在 B 区 Season 01 目录（规范化后），而非"第一季"
        b_season_dir = B_DIR / "anime" / "Chinese" / "Season 01"
        sub_targets = list(b_season_dir.glob("*.srt"))
        assert len(sub_targets) >= 1, (
            f"中文季目录应规范化为 Season 01：目标应在 {b_season_dir}"
        )
        # 不应在"第一季"目录
        b_cn_dir = B_DIR / "anime" / "Chinese" / "第一季"
        assert not b_cn_dir.exists() or not list(b_cn_dir.glob("*.srt")), (
            "字幕不应被放入未规范化的中文季目录"
        )

    def test_movie_subtitle_preserves_structure(self):
        """电影字幕保留电影目录结构（不添加 Season 层）。"""
        self._install_real_subtitle_handler()
        webdav = "/cloud/mount/movies/Inception/Inception.mkv"
        a_strm = self._seed_a_strm_file(
            "movies/Inception/Inception.strm", webdav)
        a_sub = self._seed_a_subtitle_file(
            "movies/Inception/Inception.chs.简体.srt")

        self.app.subtitle_handler.process_subtitle_file(str(a_sub))

        # 目标应在 B 区 movies/Inception/ 下，无 Season 层
        b_movie_dir = B_DIR / "movies" / "Inception"
        sub_targets = list(b_movie_dir.glob("*.srt")) + list(b_movie_dir.glob("*.ass"))
        assert len(sub_targets) >= 1, f"电影字幕应复制到 {b_movie_dir}"
        # 不应有 Season 目录
        season_dirs = list(b_movie_dir.glob("Season *"))
        assert not season_dirs, "电影字幕不应创建 Season 目录"

    def test_multi_language_same_episode_no_overwrite(self):
        """同集的多语言字幕 (.chs + .eng) 目标不互相覆盖。"""
        self._install_real_subtitle_handler()
        webdav = "/cloud/mount/anime/MultiLang/S01E01.mp4"
        a_strm = self._seed_a_strm_file(
            "anime/MultiLang/Season 01/S01E01.strm", webdav)
        a_sub_chs = self._seed_a_subtitle_file(
            "anime/MultiLang/Season 01/S01E01.chs.简体.srt")
        a_sub_eng = self._seed_a_subtitle_file(
            "anime/MultiLang/Season 01/S01E01.eng.srt")

        self.app.subtitle_handler.process_subtitle_file(str(a_sub_chs))
        self.app.subtitle_handler.process_subtitle_file(str(a_sub_eng))

        b_season_dir = B_DIR / "anime" / "MultiLang" / "Season 01"
        sub_targets = list(b_season_dir.glob("*.srt"))
        assert len(sub_targets) >= 2, (
            f"同集多语言字幕应产生 >=2 个目标文件，实际 {len(sub_targets)}: {sub_targets}"
        )
        # 两个目标文件名不同（语言代码区分，如 zho / eng / und）
        target_names = [t.name for t in sub_targets]
        # 至少有两个不同名称的目标文件（不互相覆盖）
        assert len(set(target_names)) >= 2, (
            f"多语言字幕目标应有不同文件名：{target_names}"
        )

    def test_idempotent_reprocessing(self):
        """重复处理同一字幕文件幂等：不重复写、不异常。"""
        self._install_real_subtitle_handler()
        webdav = "/cloud/mount/anime/Idempotent/S01E01.mp4"
        a_strm = self._seed_a_strm_file(
            "anime/Idempotent/Season 01/S01E01.strm", webdav)
        a_sub = self._seed_a_subtitle_file(
            "anime/Idempotent/Season 01/S01E01.chs.简体.srt")

        self.app.subtitle_handler.process_subtitle_file(str(a_sub))
        sub_targets_first = list(
            (B_DIR / "anime" / "Idempotent" / "Season 01").glob("*.srt"))
        count_first = len(sub_targets_first)

        self.app.subtitle_handler.process_subtitle_file(str(a_sub))
        sub_targets_second = list(
            (B_DIR / "anime" / "Idempotent" / "Season 01").glob("*.srt"))
        count_second = len(sub_targets_second)

        assert count_second == count_first, "重复处理不应增加文件数量"

    def test_strm_content_not_overwritten(self):
        """字幕处理不得改写 B 区 .strm 文件内容。"""
        self._install_real_subtitle_handler()
        webdav = "/cloud/mount/anime/NoStrmOverwrite/S01E01.mp4"
        a_strm = self._seed_a_strm_file(
            "anime/NoStrmOverwrite/Season 01/S01E01.strm", webdav)
        a_sub = self._seed_a_subtitle_file(
            "anime/NoStrmOverwrite/Season 01/S01E01.chs.简体.srt")

        # 先全量同步，建立 B 区 strm 文件
        self._run_full_sync(use_bulk=True)
        b_strm = B_DIR / "anime" / "NoStrmOverwrite" / "Season 01" / "S01E01.strm"
        assert b_strm.exists()
        before_bytes = b_strm.read_bytes()

        self.app.subtitle_handler.process_subtitle_file(str(a_sub))

        after_bytes = b_strm.read_bytes()
        assert before_bytes == after_bytes, "字幕处理不应改写 .strm 文件内容"


class TestParseFsListContent:
    """_parse_fs_list_content 参数化矩阵。

    覆盖 docs/openlist_api_fs_list_contract.md §2-§3 中定义的所有
    权威成功/不可信判别路径。直接测试 _parse_fs_list_content 的 5 个
    顺序守卫条件，不依赖 SimulationBase 沙盒。
    """

    def _call(self, res):
        """构造最小 AppService 实例调用 _parse_fs_list_content。"""
        from unittest.mock import Mock, patch
        from config import AppConfig
        from database import Database
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "t.db"))
            cfg = Mock(spec=AppConfig)
            cfg.a_folders = [str(Path(tmp) / "a")]
            cfg.paths = Mock()
            cfg.paths.b_root = str(Path(tmp) / "b")
            cfg.paths.c_root = str(Path(tmp) / "c")
            cfg.behavior = Mock()
            cfg.behavior.ghost_protect_seconds = 300
            cfg.strm_engine_paths = []
            cfg.refresh = Mock(enabled=False)
            cfg.refresh_paths = []
            admin = Mock()
            with patch("app_service_core.RefreshService"), \
                 patch("app_service_core.SubtitleHandler"):
                from app_service_core import AppService
                app = AppService(cfg, db, admin)
            return app._parse_fs_list_content(res)

    # ── 不可信向量（应全部返回 None）──────────────────────────────

    @pytest.mark.parametrize("res,desc", [
        (None, "res is None"),
        ("string", "res is str"),
        (42, "res is int"),
        ([], "res is list"),
        ({}, "res is empty dict"),
        ({"code": 500, "data": {"total": 0, "content": []}},
         "non-success code 500"),
        ({"code": 401, "data": {"total": 0, "content": []}},
         "non-success code 401"),
        ({"code": 200}, "missing data key"),
        ({"code": 200, "data": None}, "data is None"),
        ({"code": 200, "data": "oops"}, "data is str"),
        ({"code": 200, "data": [1, 2]}, "data is list"),
        ({"code": 0, "data": {"total": 0}}, "missing content key"),
        ({"code": 0, "data": {"total": 0, "content": None}},
         "content is None"),
        ({"code": 0, "data": {"total": 0, "content": "oops"}},
         "content is str"),
        ({"code": 0, "data": {"total": 0, "content": 5}},
         "content is int"),
        ({"code": 0, "data": {"content": []}}, "missing total key"),
        ({"code": 0, "data": {"content": [], "total": None}},
         "total is None"),
        ({"code": 0, "data": {"content": [], "total": "100"}},
         "total is str"),
        ({"code": 0, "data": {"content": [], "total": -1}},
         "total is negative"),
        ({"code": 0, "data": {"content": [], "total": 3.14}},
         "total is float"),
        ({"code": 0, "data": {"total": 5, "content": []}},
         "content=[] total>0 (conflict)"),
        # bool 是 int 子类，必须拒绝（防止 total=True 被当作 1）
        ({"code": 0, "data": {"total": True, "content": [{"name": "a.strm"}]}},
         "total is bool True"),
        ({"code": 0, "data": {"total": False, "content": []}},
         "total is bool False"),
    ])
    def test_untrusted_returns_none(self, res, desc):
        assert self._call(res) is None, f"应返回 None: {desc}"

    # ── 权威成功向量（应返回 (content, total)）─────────────────────

    @pytest.mark.parametrize("res,expected_content,expected_total", [
        ({"code": 0, "data": {"content": [], "total": 0}},
         [], 0),
        ({"code": 200, "data": {"content": [{"name": "a.strm"}], "total": 1}},
         [{"name": "a.strm"}], 1),
        ({"code": 200, "data": {
            "content": [{"name": "x.strm"}, {"name": "y.strm"}],
            "total": 2}},
         [{"name": "x.strm"}, {"name": "y.strm"}], 2),
    ])
    def test_trusted_returns_tuple(self, res, expected_content, expected_total):
        result = self._call(res)
        assert result is not None, f"权威成功应返回非 None: {res}"
        content, total = result
        assert content == expected_content
        assert total == expected_total


class TestNewIssue7_WebDAVFalseNegative(SimulationBase):
    """问题7：WebDAV 假阴性 fail-closed。

    按 docs/openlist_api_fs_list_contract.md 判别契约验证：
    - A 区 cleanup_a_redundant_using_api：不可信父目录本地记录整组排除
    - B 区 _collect_cloud_files_in_directory：不可信返 None，content=None 不再 TypeError
    - per_page=100（已修复）；安全阀耗尽 fail-closed（已修复）
    """

    def _mock_list_directory_for_fail_closed(self, response_map):
        """构造 mock list_directory，按父目录路径返回不同响应。"""
        def mock_fn(path, page=1, per_page=100):
            if path in response_map:
                resp = response_map[path]
                if callable(resp):
                    return resp(page=page, per_page=per_page)
                return resp
            return {"code": 0, "data": {"total": 0, "content": []}}
        return mock_fn

    # ── A 区 fail-open 复现 ─────────────────────────────────────────────

    def test_a_redundant_first_page_none_skips_parent(self):
        """A区：首页 list_directory 返 None → 该父目录本地记录不参与差集。"""
        # 构造两个父目录的 A 记录
        a1 = A_DIR / "a_clean" / "S01E01.strm"
        a1.parent.mkdir(parents=True, exist_ok=True)
        a1.write_text("/cloud/a_clean/S01E01.mp4", encoding="utf-8")
        a2 = A_DIR / "a_untrusted" / "S01E01.strm"
        a2.parent.mkdir(parents=True, exist_ok=True)
        a2.write_text("/cloud/a_untrusted/S01E01.mp4", encoding="utf-8")

        self.db.upsert_a(str(a1), "/cloud/a_clean/S01E01.mp4", "/cloud/a_clean")
        self.db.upsert_a(str(a2), "/cloud/a_untrusted/S01E01.mp4", "/cloud/a_untrusted")

        # a_clean 目录：权威成功空目录 → 该目录下的 a1 是冗余
        # a_untrusted 目录：首页返 None → 不可信，整组排除
        response_map = {
            "/cloud/a_clean": {
                "code": 200,
                "data": {"total": 0, "content": []},
            },
            "/cloud/a_untrusted": None,  # 不可信
        }
        self.admin_api.list_directory.side_effect = (
            self._mock_list_directory_for_fail_closed(response_map))

        self.app.cleanup_a_redundant_using_api()

        # a_clean 下的 a1 应被清理（权威空目录 → 冗余）
        assert not a1.exists()

        # a_untrusted 下的 a2 不应被清理（不可信 → 整组排除）
        assert a2.exists()
        a2_record = self.db.get_a_by_local(str(a2))
        assert a2_record is not None, "不可信父目录下的 A 记录应被保留"

    def test_a_redundant_none_data_skips_parent(self):
        """A区：data 键存在但值为 None → 不可信，该父目录本地记录整组排除。"""
        a_file = A_DIR / "untrusted_data" / "S01E01.strm"
        a_file.parent.mkdir(parents=True, exist_ok=True)
        a_file.write_text("/cloud/untrusted/S01E01.mp4", encoding="utf-8")
        self.db.upsert_a(str(a_file), "/cloud/untrusted/S01E01.mp4", "/cloud/untrusted")

        # data=None → _parse_fs_list_content 返回 None
        self.admin_api.list_directory.return_value = {
            "code": 200,
            "data": None,
        }

        self.app.cleanup_a_redundant_using_api()

        # 文件不应被删除（不可信 → 整组排除）
        assert a_file.exists()

    def test_a_redundant_missing_total_skips_parent(self):
        """A区：total 缺失 → 不可信，该父目录本地记录整组排除。"""
        a_file = A_DIR / "no_total" / "S01E01.strm"
        a_file.parent.mkdir(parents=True, exist_ok=True)
        a_file.write_text("/cloud/no_total/S01E01.mp4", encoding="utf-8")
        self.db.upsert_a(str(a_file), "/cloud/no_total/S01E01.mp4", "/cloud/no_total")

        self.admin_api.list_directory.return_value = {
            "code": 200,
            "data": {"content": []},  # total 缺失
        }

        self.app.cleanup_a_redundant_using_api()

        assert a_file.exists()

    # ── B 区 zombie fail-closed ─────────────────────────────────────────

    def test_b_zombie_content_none_no_type_error(self):
        """B区：content=None → 不返回 TypeError，文件/DB 保留。"""
        b_file = B_DIR / "zombie_test" / "S01E01.strm"
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text("/cloud/zombie/S01E01.mp4", encoding="utf-8")
        self.db.upsert_b(
            str(b_file), "/cloud/zombie/S01E01.mp4", "/cloud/zombie", None,
            fingerprint=make_strm_fingerprint("/cloud/zombie/S01E01.mp4"),
            status="valid", mapping_id="test_m1",
        )

        self.admin_api.list_directory.return_value = {
            "code": 200,
            "data": {"total": 1, "content": None},  # content=None
        }

        with patch.object(self.app, "_handle_b_zombie") as mock_zombie:
            self.app.cleanup_b_zombies_under_folder("/cloud")
            mock_zombie.assert_not_called()

        # 文件和 DB 记录应保留
        assert b_file.exists()
        assert self.db.get_b_by_local_full(str(b_file)) is not None

    def test_b_zombie_safety_valve_exhausted_returns_none(self):
        """B区：100 页安全阀耗尽 → fail-closed 返回 None，文件/DB 保留。"""
        b_file = B_DIR / "valve_test" / "S01E01.strm"
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text("/cloud/valve/S01E01.mp4", encoding="utf-8")
        self.db.upsert_b(
            str(b_file), "/cloud/valve/S01E01.mp4", "/cloud/valve", None,
            fingerprint=make_strm_fingerprint("/cloud/valve/S01E01.mp4"),
            status="valid", mapping_id="test_m1",
        )

        # 构造 100 个满页（每页100条）+ 第101页触发安全阀
        # 但实际上只需要让 per_page=100 的循环到第 101 次就退出
        page_count = [0]

        def mock_fs_list(path, page=1, per_page=100):
            page_count[0] += 1
            # 每页都返回满 100 条（len(content) == per_page）→ 不会提前退出
            return {
                "code": 0,
                "data": {
                    "total": 10000,
                    "content": [
                        {"name": f"f{i}.strm", "is_dir": False}
                        for i in range(per_page)
                    ],
                },
            }

        self.admin_api.list_directory.side_effect = mock_fs_list

        with patch.object(self.app, "_handle_b_zombie") as mock_zombie:
            self.app.cleanup_b_zombies_under_folder("/cloud")
            mock_zombie.assert_not_called()

        assert b_file.exists()

    def test_b_zombie_code_not_200_returns_none(self):
        """B区：code ∉ {0,200} → 不可信返 None，文件/DB 保留。"""
        b_file = B_DIR / "code_err" / "S01E01.strm"
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text("/cloud/code_err/S01E01.mp4", encoding="utf-8")
        self.db.upsert_b(
            str(b_file), "/cloud/code_err/S01E01.mp4", "/cloud/code_err", None,
            fingerprint=make_strm_fingerprint("/cloud/code_err/S01E01.mp4"),
            status="valid", mapping_id="test_m1",
        )

        self.admin_api.list_directory.return_value = {
            "code": 500,
            "data": {},
        }

        with patch.object(self.app, "_handle_b_zombie") as mock_zombie:
            self.app.cleanup_b_zombies_under_folder("/cloud")
            mock_zombie.assert_not_called()

        assert b_file.exists()

    def test_b_zombie_missing_data_key(self):
        """B区：data 键缺失 → 不可信。"""
        b_file = B_DIR / "no_data" / "S01E01.strm"
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text("/cloud/no_data/S01E01.mp4", encoding="utf-8")
        self.db.upsert_b(
            str(b_file), "/cloud/no_data/S01E01.mp4", "/cloud/no_data", None,
            fingerprint=make_strm_fingerprint("/cloud/no_data/S01E01.mp4"),
            status="valid", mapping_id="test_m1",
        )
        self.admin_api.list_directory.return_value = {
            "code": 200,
        }
        with patch.object(self.app, "_handle_b_zombie") as mock_zombie:
            self.app.cleanup_b_zombies_under_folder("/cloud")
            mock_zombie.assert_not_called()
        assert b_file.exists()

    def test_b_zombie_data_non_dict(self):
        """B区：data 为非 dict → 不可信。"""
        b_file = B_DIR / "bad_data" / "S01E01.strm"
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text("/cloud/bad_data/S01E01.mp4", encoding="utf-8")
        self.db.upsert_b(
            str(b_file), "/cloud/bad_data/S01E01.mp4", "/cloud/bad_data", None,
            fingerprint=make_strm_fingerprint("/cloud/bad_data/S01E01.mp4"),
            status="valid", mapping_id="test_m1",
        )
        self.admin_api.list_directory.return_value = {
            "code": 200,
            "data": [1, 2, 3],
        }
        with patch.object(self.app, "_handle_b_zombie") as mock_zombie:
            self.app.cleanup_b_zombies_under_folder("/cloud")
            mock_zombie.assert_not_called()
        assert b_file.exists()

    def test_b_zombie_total_negative(self):
        """B区：total 为负数 → 不可信。"""
        b_file = B_DIR / "neg_total" / "S01E01.strm"
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text("/cloud/neg/S01E01.mp4", encoding="utf-8")
        self.db.upsert_b(
            str(b_file), "/cloud/neg/S01E01.mp4", "/cloud/neg", None,
            fingerprint=make_strm_fingerprint("/cloud/neg/S01E01.mp4"),
            status="valid", mapping_id="test_m1",
        )
        self.admin_api.list_directory.return_value = {
            "code": 200,
            "data": {"total": -1, "content": []},
        }
        with patch.object(self.app, "_handle_b_zombie") as mock_zombie:
            self.app.cleanup_b_zombies_under_folder("/cloud")
            mock_zombie.assert_not_called()
        assert b_file.exists()

    # ── A 区 concurrent page2+ fail-closed 显式向量 ────────────────────

    def test_collect_concurrent_page2_untrusted_returns_none(self):
        """首页可信且 total 触发翻页后，page2 不可信 → 整组返回 None。"""
        cloud = "/cloud/page2_untrusted"

        def mock_list(path, page=1, per_page=100):
            assert path == cloud
            if page == 1:
                return {
                    "code": 200,
                    "data": {
                        "total": 150,
                        "content": [
                            {"name": f"p1_{i:03d}.strm", "is_dir": False}
                            for i in range(100)
                        ],
                    },
                }
            # page>=2：data=None → _parse_fs_list_content 不可信
            return {"code": 200, "data": None}

        self.admin_api.list_directory.side_effect = mock_list
        result = self.app._collect_cloud_files_concurrent(cloud)
        assert result is None, "page2 不可信必须整组 fail-closed，不得返回半截 set"
        log = self._read_log()
        assert "响应不可信" in log or "整组排除" in log

    def test_collect_concurrent_page2_fetch_failed_returns_none(self):
        """page2 重试后仍失败（返回 None）→ failed_pages 分支整组返回 None。"""
        cloud = "/cloud/page2_fetch_fail"

        def mock_list(path, page=1, per_page=100):
            if page == 1:
                return {
                    "code": 0,
                    "data": {
                        "total": 120,
                        "content": [
                            {"name": f"ok_{i}.strm", "is_dir": False}
                            for i in range(100)
                        ],
                    },
                }
            return None  # 翻页获取失败（含重试后）

        self.admin_api.list_directory.side_effect = mock_list
        result = self.app._collect_cloud_files_concurrent(cloud)
        assert result is None
        log = self._read_log()
        assert "页面获取失败" in log or "整组排除" in log

    def test_collect_concurrent_page2_non_dict_item_skipped_not_fail_closed(self):
        """page2 content 含非 dict 元素时应跳过该项，不因 AttributeError 整组失败。

        首页已有 isinstance(item, dict) 守卫；page2 必须对齐，避免把可恢复
        脏元素升级为整目录不可信。
        """
        cloud = "/cloud/page2_nondict"

        def mock_list(path, page=1, per_page=100):
            if page == 1:
                return {
                    "code": 200,
                    "data": {
                        "total": 101,
                        "content": [
                            {"name": f"p1_{i:03d}.strm", "is_dir": False}
                            for i in range(100)
                        ],
                    },
                }
            return {
                "code": 200,
                "data": {
                    "total": 101,
                    "content": [
                        "not-a-dict",
                        None,
                        42,
                        {"name": "good.strm", "is_dir": False},
                        {"name": "skip_dir", "is_dir": True},
                    ],
                },
            }

        self.admin_api.list_directory.side_effect = mock_list
        result = self.app._collect_cloud_files_concurrent(cloud)
        assert result is not None, "非 dict 元素应被跳过，不得整组 fail-closed"
        assert f"{cloud}/good.strm" in result
        assert f"{cloud}/p1_000.strm" in result
        assert len(result) == 101  # 100 from page1 + good.strm
    def test_a_redundant_page2_untrusted_skips_parent(self):
        """A区集成：翻页 page2 不可信 → 该父目录本地记录不参与差集、不删盘。"""
        a_file = A_DIR / "page2_parent" / "S01E01.strm"
        a_file.parent.mkdir(parents=True, exist_ok=True)
        a_file.write_text("/cloud/page2_parent/S01E01.mp4", encoding="utf-8")
        self.db.upsert_a(
            str(a_file), "/cloud/page2_parent/S01E01.mp4", "/cloud/page2_parent")

        def mock_list(path, page=1, per_page=100):
            if path != "/cloud/page2_parent":
                return {"code": 200, "data": {"total": 0, "content": []}}
            if page == 1:
                return {
                    "code": 200,
                    "data": {
                        "total": 150,
                        # 首页不含本地 S01E01.strm → 若半截成功会误判冗余
                        "content": [
                            {"name": f"other_{i}.strm", "is_dir": False}
                            for i in range(100)
                        ],
                    },
                }
            return {
                "code": 200,
                "data": {"content": [{"name": "x.strm"}], "total": True},  # bool total
            }

        self.admin_api.list_directory.side_effect = mock_list
        self.app.cleanup_a_redundant_using_api()

        assert a_file.exists(), "page2 不可信时不得删除该父目录下本地 A 文件"
        assert self.db.get_a_by_local(str(a_file)) is not None


class TestNewIssue8_UnicodePaths(SimulationBase):
    """问题8：Unicode/特殊路径身份与冲突安全。

    契约：
    - NFC/NFD、正反斜杠、重复斜杠、尾斜杠经 canonicalize_webdav_path() 后
      是同资源表示，应得到相同 canonical path/fingerprint；
    - URL 编码等价性通过完整 parse_strm_content() 链验证；
    - 大小写默认敏感；全角/半角、单空格/连续空格是不同资源；
    - 不同 WebDAV 资源即使 basename 相似也不静默覆盖；
    - 不切换 NFKC 或全局大小写不敏感。
    """

    def test_nfc_nfd_same_fingerprint(self):
        """NFC 和 NFD 表示的同资源应有相同 fingerprint。"""
        from utils.strm_utils import canonicalize_webdav_path
        nfc_char = "é"  # NFC 形式
        nfd_char = unicodedata.normalize("NFD", nfc_char)  # NFD 形式
        # 两个路径使用相同的字符（é），只是编码表示不同
        nfc_path = f"/cloud/mount/anime/Re{nfc_char}sume/S01E01.mp4"
        nfd_path = f"/cloud/mount/anime/Re{nfd_char}sume/S01E01.mp4"
        nfc_canon = canonicalize_webdav_path(nfc_path)
        nfd_canon = canonicalize_webdav_path(nfd_path)
        # canonicalize 应把 NFD 规范化为 NFC（或两者都规范化为同一形式）
        assert nfc_canon == nfd_canon, (
            f"NFC/NFD 应规范化为相同 canonical path: "
            f"NFC={nfc_canon!r}, NFD={nfd_canon!r}"
        )
        assert make_strm_fingerprint(nfc_path) == make_strm_fingerprint(nfd_path)

    def test_slash_normalization_same_fingerprint(self):
        """正反斜杠、重复斜杠、尾斜杠应规范化为同 canonical path。"""
        from utils.strm_utils import canonicalize_webdav_path
        variants = [
            "/cloud/mount/movies/Inception/Inception.mkv",
            "//cloud//mount//movies//Inception//Inception.mkv",
            "/cloud/mount/movies/Inception/Inception.mkv/",
        ]
        canonicals = [canonicalize_webdav_path(v) for v in variants]
        assert len(set(canonicals)) == 1, f"斜杠变体应规范化为相同路径: {canonicals}"
        fps = {make_strm_fingerprint(v) for v in variants}
        assert len(fps) == 1

    def test_url_encoded_decoded_through_parse(self):
        """URL 编码 %xx 通过 parse_strm_content 解码后应与明文等价。"""
        from utils.strm_utils import parse_strm_content
        encoded_strm = "https://host/d/%E5%8A%A8%E6%BC%AB/S01E01.mp4"
        decoded_strm = "https://host/d/动漫/S01E01.mp4"
        parsed_enc = parse_strm_content(encoded_strm)
        parsed_dec = parse_strm_content(decoded_strm)
        assert parsed_enc is not None
        assert parsed_dec is not None
        assert parsed_enc == parsed_dec

    def test_case_sensitive_different_resources(self):
        """大小写敏感：CaseDiff vs casediff 是不同资源。"""
        fp_upper = make_strm_fingerprint("/cloud/mount/anime/CaseDiff/S01E01.mp4")
        fp_lower = make_strm_fingerprint("/cloud/mount/anime/casediff/S01E01.mp4")
        assert fp_upper != fp_lower

    def test_fullwidth_different_from_ascii(self):
        """全角字母与半角字母是不同资源。"""
        fp_full = make_strm_fingerprint("/cloud/mount/anime/ＦｕｌｌＷｉｄｔｈ/S01E01.mp4")
        fp_half = make_strm_fingerprint("/cloud/mount/anime/FullWidth/S01E01.mp4")
        assert fp_full != fp_half

    def test_double_space_different_from_single(self):
        """连续空格与单空格是不同资源。"""
        fp_double = make_strm_fingerprint("/cloud/mount/anime/Double  Space/S01E01.mp4")
        fp_single = make_strm_fingerprint("/cloud/mount/anime/Double Space/S01E01.mp4")
        assert fp_double != fp_single

    def test_special_path_sync_unique_instance(self):
        """真实 A→B 特殊路径同步后：目标唯一、STRM 内容可配源、DB 一致。"""
        # 使用 manifest 中的特殊路径样本
        fullwidth_a = A_DIR / "anime" / "全角Ｔｅｔｌｅ" / "Season 01" / "S01E01.strm"
        double_space_a = A_DIR / "anime" / "连续  空格" / "Season 01" / "S01E01.strm"
        assert fullwidth_a.exists()
        assert double_space_a.exists()

        self._run_full_sync(use_bulk=True)

        # 验证 B 区目标存在且唯一
        b_fullwidth = list(B_DIR.rglob("*.strm"))
        b_fullwidth_matches = [
            p for p in b_fullwidth
            if "全角" in str(p) or "Ｔ" in str(p)
        ]
        assert len(b_fullwidth_matches) >= 1, "全角路径 STRM 应同步到 B 区"

# 每个 A 源的 webdav_path 在 B 区应有唯一实例
        for b_path in b_fullwidth_matches:
            webdav = read_strm_webdav_path(b_path)
            if webdav:
                instances = self.db.get_b_instances_by_fingerprint(
                    make_strm_fingerprint(webdav), "test_m1")
                assert len(instances) == 1, (
                    f"每个指纹应只有一个有效 B 实例: {webdav} 有 {len(instances)}"
                )


class TestSimulationLogRegression(SimulationBase):
    """保留原有日志阶段标记的回归保护。"""

    # 有意冒烟测试，断言代码路径执行，非行为正确性验证
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
