"""
A 区 size+mtime 内容读跳检契约测试（计划 20261002-perf-feasibility B.3：R1–R8 + A-3）。

期望值源自 B.1 行为契约推导，而非抄录现状——现状"永远重读正文"，故对本契约天然为红。
计数打在 `domain.sync.sync_service` 模块的 `read_strm_webdav_path` 函数符号上，
测的是"正文 open 未发生"，而非"结果凑对"（保真反假绿）。

注：夹具用契约 DDL 自建 `a_strm_snapshot` 表（幂等），保证红灯落在行为断言
而非"表不存在"的脚手架错误上；parse_version 字面量 1 对齐
`utils.strm_utils.STRM_PARSE_VERSION` 初版，999 为任意旧版本。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import domain.sync.sync_service as sync_service_mod
from database import Database
from domain.sync.sync_service import SyncService
from _test_helpers import build_mock_app

# 当前解析算法版本（对齐 strm_utils.STRM_PARSE_VERSION = 1；漂移测试用 999）
CUR_PARSE_VERSION = 1
STALE_PARSE_VERSION = 999

SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS a_strm_snapshot (
    local_path TEXT PRIMARY KEY,
    file_size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    webdav_path TEXT NOT NULL,
    parent_webdav_path TEXT NOT NULL,
    parse_version INTEGER NOT NULL,
    indexed_at REAL NOT NULL
)
"""


class _ReadCounter:
    """替代 read_strm_webdav_path：记录每次正文读调用并真实读取内容。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, file_path) -> str | None:
        self.calls.append(str(file_path))
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except (OSError, ValueError):
            return None


def _insert_snapshot(db: Database, row: tuple) -> None:
    with db.connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO a_strm_snapshot "
            "(local_path, file_size, mtime_ns, webdav_path, parent_webdav_path, "
            "parse_version, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            row,
        )
        conn.commit()


def _snapshot_row(db: Database, local_path: str) -> tuple | None:
    with db.read_connection() as conn:
        return conn.execute(
            "SELECT local_path, file_size, mtime_ns, webdav_path, "
            "parent_webdav_path, parse_version "
            "FROM a_strm_snapshot WHERE local_path = ?",
            (local_path,),
        ).fetchone()


def _a_files_row(db: Database, local_path: str) -> tuple | None:
    with db.read_connection() as conn:
        return conn.execute(
            "SELECT webdav_path, parent_webdav_path "
            "FROM a_strm_files WHERE local_path = ?",
            (local_path,),
        ).fetchone()


@pytest.fixture()
def env(tmp_path):
    """Mock AppService + 真实 Database（快照门必须落真库才能验证行为契约）。"""
    app = build_mock_app(tmp_path, use_mock=True)
    db = Database(str(tmp_path / "bridge.db"))
    app.db = db
    with db.connection() as conn:
        conn.execute(SNAPSHOT_DDL)
    svc = SyncService(app)
    return app, db, svc, app.a_roots[0]


class TestASnapshotSkip:
    def test_r1_unchanged_skips_content_read(self, env):
        """R1：size+mtime+parse_version 未变 → 复用缓存，正文读 0 次（含嵌套目录）。"""
        _app, db, svc, a_root = env
        nested = a_root / "show" / "Season 01"
        nested.mkdir(parents=True)
        p = nested / "ep1.strm"
        p.write_text("/mnt/M/ep1", encoding="utf-8")
        st = os.stat(p)
        _insert_snapshot(
            db, (str(p), st.st_size, st.st_mtime_ns, "/mnt/M/ep1", "/mnt/M",
                 CUR_PARSE_VERSION, time.time()))
        counter = _ReadCounter()
        with patch.object(sync_service_mod, "read_strm_webdav_path", counter):
            svc.initial_scan_a(use_bulk=False)
        assert counter.calls == [], (
            f"正文读不应发生，实际读取: {counter.calls}")
        # 复用缓存权威链接：喂给 upsert 的元组与快照一致（D2：仍被索引）
        assert _a_files_row(db, str(p)) == ("/mnt/M/ep1", "/mnt/M")

    def test_r2_size_change_rereads_and_overwrites(self, env):
        """R2：size 不等 → 读正文 + 快照行覆盖写为新 size/新 webdav。"""
        _app, db, svc, a_root = env
        p = a_root / "ep1.strm"
        p.write_text("/mnt/M/ep1", encoding="utf-8")
        st = os.stat(p)
        _insert_snapshot(
            db, (str(p), st.st_size + 1, st.st_mtime_ns, "/mnt/M/old", "/mnt/M",
                 CUR_PARSE_VERSION, time.time()))
        counter = _ReadCounter()
        with patch.object(sync_service_mod, "read_strm_webdav_path", counter):
            svc.initial_scan_a(use_bulk=False)
        assert len(counter.calls) == 1
        row = _snapshot_row(db, str(p))
        assert row is not None
        assert row[1] == st.st_size
        assert row[3] == "/mnt/M/ep1"
        assert row[5] == CUR_PARSE_VERSION

    def test_r3_mtime_change_rereads(self, env):
        """R3：size 相同、mtime_ns 不同 → 必读正文并覆盖快照 mtime。"""
        _app, db, svc, a_root = env
        p = a_root / "ep1.strm"
        p.write_text("/mnt/M/ep1", encoding="utf-8")
        st = os.stat(p)
        _insert_snapshot(
            db, (str(p), st.st_size, st.st_mtime_ns - 1_000_000, "/mnt/M/ep1",
                 "/mnt/M", CUR_PARSE_VERSION, time.time()))
        counter = _ReadCounter()
        with patch.object(sync_service_mod, "read_strm_webdav_path", counter):
            svc.initial_scan_a(use_bulk=False)
        assert len(counter.calls) == 1
        row = _snapshot_row(db, str(p))
        assert row is not None
        assert row[2] == st.st_mtime_ns

    def test_r4_empty_cached_webdav_fail_closed(self, env):
        """R4：快照 webdav_path 为空 → 不采信空权威，fail-closed 回退读正文并重写。"""
        _app, db, svc, a_root = env
        p = a_root / "ep1.strm"
        p.write_text("/mnt/M/ep1", encoding="utf-8")
        st = os.stat(p)
        _insert_snapshot(
            db, (str(p), st.st_size, st.st_mtime_ns, "", "", CUR_PARSE_VERSION,
                 time.time()))
        counter = _ReadCounter()
        with patch.object(sync_service_mod, "read_strm_webdav_path", counter):
            svc.initial_scan_a(use_bulk=False)
        assert len(counter.calls) == 1
        row = _snapshot_row(db, str(p))
        assert row is not None
        assert row[3] == "/mnt/M/ep1"
        assert row[4] == "/mnt/M"

    def test_r5_stat_failure_isomorphic_none(self, env):
        """R5：walk 与 stat 间文件消失（OSError）→ 该条 return None，不写记录/快照、不抛。"""
        _app, db, svc, a_root = env
        p = a_root / "ep1.strm"
        p.write_text("/mnt/M/ep1", encoding="utf-8")
        real_stat = os.stat

        def fake_stat(path, *args, **kwargs):
            if str(path) == str(p):
                raise OSError(2, "vanishing file")
            return real_stat(path, *args, **kwargs)

        with patch.object(sync_service_mod.os, "stat", side_effect=fake_stat):
            svc.initial_scan_a(use_bulk=False)  # 不得抛出
        assert _a_files_row(db, str(p)) is None
        assert _snapshot_row(db, str(p)) is None

    def test_r6_full_audit_bypasses_snapshot_and_prunes(self, env):
        """R6：use_snapshot=False（全量审计）→ 快照全匹配仍读正文，且触发 prune。"""
        _app, db, svc, a_root = env
        p = a_root / "ep1.strm"
        p.write_text("/mnt/M/ep1", encoding="utf-8")
        st = os.stat(p)
        _insert_snapshot(
            db, (str(p), st.st_size, st.st_mtime_ns, "/mnt/M/ep1", "/mnt/M",
                 CUR_PARSE_VERSION, time.time()))
        orig_prune = db.prune_a_snapshot_not_in
        seen: list[list[str]] = []

        def spy_prune(paths):
            seen.append(list(paths))
            orig_prune(paths)

        db.prune_a_snapshot_not_in = spy_prune
        counter = _ReadCounter()
        with patch.object(sync_service_mod, "read_strm_webdav_path", counter):
            svc.initial_scan_a(use_bulk=False, a_roots=[a_root],
                               use_snapshot=False)
        # 权威重扫：快照全匹配也必须读（use_snapshot=False 是唯一触发变量）
        assert len(counter.calls) == 1
        assert seen, "全量审计后必须触发 prune_a_snapshot_not_in"
        assert str(p) in seen[0]
        # 本路径在扫集合内 → 快照行不被清掉
        assert _snapshot_row(db, str(p)) is not None

    def test_r7_new_file_writes_snapshot(self, env):
        """R7：无快照行的新文件 → 读正文一次，扫描后快照新增该键。"""
        _app, db, svc, a_root = env
        p = a_root / "new.strm"
        p.write_text("/mnt/M/new", encoding="utf-8")
        counter = _ReadCounter()
        with patch.object(sync_service_mod, "read_strm_webdav_path", counter):
            svc.initial_scan_a(use_bulk=False)
        assert len(counter.calls) == 1
        row = _snapshot_row(db, str(p))
        assert row is not None, "新文件扫描后必须写入 a_strm_snapshot"
        assert row[3] == "/mnt/M/new"
        assert row[5] == CUR_PARSE_VERSION

    def test_r8_parse_version_drift_rereads(self, env):
        """R8（E1）：快照 parse_version 为旧值 → 必读正文并重写为当前版本。"""
        _app, db, svc, a_root = env
        p = a_root / "ep1.strm"
        p.write_text("/mnt/M/ep1", encoding="utf-8")
        st = os.stat(p)
        _insert_snapshot(
            db, (str(p), st.st_size, st.st_mtime_ns, "/mnt/M/stale", "/mnt/M",
                 STALE_PARSE_VERSION, time.time()))
        counter = _ReadCounter()
        with patch.object(sync_service_mod, "read_strm_webdav_path", counter):
            svc.initial_scan_a(use_bulk=False)
        assert len(counter.calls) == 1, (
            "parse_version 漂移必须重读正文，防止旧算法派生链接被假命中复用")
        row = _snapshot_row(db, str(p))
        assert row is not None
        assert row[5] == CUR_PARSE_VERSION
        assert row[3] == "/mnt/M/ep1"

    def test_a3_deleted_snapshot_invalidated_and_rebuilt(self, env):
        """A-3（E2）：delete_a_snapshot 后 load_a_snapshot_map 不含该键；
        下轮扫描同路径走"新文件"分支重读+重写。"""
        _app, db, svc, a_root = env
        p = a_root / "ep1.strm"
        p.write_text("/mnt/M/ep1", encoding="utf-8")
        st = os.stat(p)
        _insert_snapshot(
            db, (str(p), st.st_size, st.st_mtime_ns, "/mnt/M/ep1", "/mnt/M",
                 CUR_PARSE_VERSION, time.time()))
        db.delete_a_snapshot(str(p))
        assert str(p) not in db.load_a_snapshot_map()
        counter = _ReadCounter()
        with patch.object(sync_service_mod, "read_strm_webdav_path", counter):
            svc.initial_scan_a(use_bulk=False)
        assert len(counter.calls) == 1
        row = _snapshot_row(db, str(p))
        assert row is not None
        assert row[3] == "/mnt/M/ep1"

    def test_bulk_mode_snapshot_writes_after_bulk_ctx_exit(self, env):
        """bulk 模式：快照写入与 prune 必须在 bulk_ctx.__exit__ 之后取写连接。

        若锁序回退（快照写早于 __exit__），bulk 连接仍持有 SQLite 写锁，而
        bulk_connection 走独立连接且绕过 rw_lock，Python 层不会自死锁；真实后果
        是第二个连接的 _probe_writeable（BEGIN IMMEDIATE）撞上 bulk 连接的写锁，
        在 busy_timeout 后抛 sqlite3.OperationalError。本仓库未配置 pytest-timeout，
        故用有界 join 把「锁序回退」转成显式断言失败而非挂死整个测试套件。
        """
        import threading

        _app, db, svc, a_root = env
        p = a_root / "bulk.strm"
        p.write_text("/mnt/M/bulk", encoding="utf-8")
        counter = _ReadCounter()
        outcome: dict = {}

        def _run():
            try:
                with patch.object(sync_service_mod, "read_strm_webdav_path", counter):
                    # use_bulk=True 走 bulk_connection 路径；若快照写早于 __exit__
                    # 取写连接，会撞上仍在持有的 bulk 写锁 → OperationalError
                    svc.initial_scan_a(use_bulk=True)
                outcome["ok"] = True
            except BaseException as exc:  # noqa: BLE001 - 断言需呈现原始异常
                outcome["exc"] = exc

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=20.0)
        assert not t.is_alive(), (
            "bulk 模式扫描未在 20s 内返回：疑似 R-7 锁序回退导致 bulk 连接写锁争用")
        assert outcome.get("ok"), (
            f"bulk 扫描异常（疑似 R-7 锁序回退触发 bulk 写锁争用）: {outcome.get('exc')!r}")
        assert len(counter.calls) == 1, "bulk 模式首扫应读正文一次"
        row = _snapshot_row(db, str(p))
        assert row is not None, "bulk 模式同样须落快照行"
        assert row[3] == "/mnt/M/bulk"

    def test_prune_deletes_out_of_set_snapshot_rows(self, env):
        """全量审计 prune：集外快照行必须真被删除（现有 R6 仅断言 prune 被调用）。"""
        _app, db, svc, a_root = env
        keep = a_root / "keep.strm"
        keep.write_text("/mnt/M/keep", encoding="utf-8")
        st_keep = os.stat(keep)
        gone = a_root / "gone.strm"
        gone.write_text("/mnt/M/gone", encoding="utf-8")
        st_gone = os.stat(gone)
        _insert_snapshot(db, (str(keep), st_keep.st_size, st_keep.st_mtime_ns,
                              "/mnt/M/keep", "/mnt/M", CUR_PARSE_VERSION, time.time()))
        _insert_snapshot(db, (str(gone), st_gone.st_size, st_gone.st_mtime_ns,
                              "/mnt/M/gone", "/mnt/M", CUR_PARSE_VERSION, time.time()))
        # 删除 gone.strm，使其成为「集外行」（扫描集合外 → 应被 prune 清除）
        gone.unlink()
        svc.initial_scan_a(use_bulk=False, a_roots=[a_root], use_snapshot=False)
        assert _snapshot_row(db, str(keep)) is not None, "集内行不得被误删"
        assert _snapshot_row(db, str(gone)) is None, "集外孤儿行必须被 prune 删除"

    def test_load_snapshot_map_fail_open_returns_empty(self, env):
        """load_a_snapshot_map fail-open：读异常时返回空 map（退化为全量重读），
        绝不抛出中断扫描。"""
        _app, db, svc, a_root = env
        p = a_root / "fo.strm"
        p.write_text("/mnt/M/fo", encoding="utf-8")
        # 注入读异常：patch read_connection 抛 sqlite3 错误，模拟表未迁移/DB 瞬时错误
        import contextlib
        import sqlite3

        @contextlib.contextmanager
        def _boom():
            raise sqlite3.OperationalError("no such table: a_strm_snapshot")
            yield  # pragma: no cover
        with patch.object(db, "read_connection", _boom):
            assert db.load_a_snapshot_map() == {}, "读异常须 fail-open 返回空 map"
        # 空 map 退化：本轮仍应全量重读并正确索引（不中断）
        counter = _ReadCounter()
        with patch.object(sync_service_mod, "read_strm_webdav_path", counter):
            svc.initial_scan_a(use_bulk=False)
        assert len(counter.calls) == 1, "空 map 退化路径应全量重读正文"
        assert _a_files_row(db, str(p)) == ("/mnt/M/fo", "/mnt/M")

    def test_e1_redundant_cleanup_invalidates_snapshot(self, env):
        """E1：A 区冗余清理路径（check_exists 权威 False）删除 A 区记录后，
        对应 a_strm_snapshot 行必须同步删除，不留孤儿行等 prune 延迟自愈。"""
        _app, db, svc, a_root = env
        p = a_root / "redundant.strm"
        p.write_text("/mnt/M/redundant", encoding="utf-8")
        st = os.stat(p)
        _insert_snapshot(
            db, (str(p), st.st_size, st.st_mtime_ns, "/mnt/M/redundant",
                 "/mnt/M", CUR_PARSE_VERSION, time.time()))
        # 走 copy_a_record_to_b 的冗余清理分支：B 目标不存在 + 云端权威缺席
        svc.app.admin_api.check_exists = Mock(return_value=False)
        svc.app.build_b_path_from_a = Mock(
            return_value=a_root / "b_target" / "redundant.strm")
        ret = svc.copy_a_record_to_b(
            str(p), "/mnt/M/redundant", "/mnt/M", mapping_id="mid-e1")
        assert ret is False, "冗余清理分支按现状返回 False"
        assert _snapshot_row(db, str(p)) is None, (
            "A 区记录删除后快照行必须同步失效，不得残留孤儿行")

    def test_e3_partial_audit_scope_warning(self, env, caplog):
        """E3：use_snapshot=False 且局部 a_roots（keep 集不完备）→ WARNING；
        全量审计（a_roots=None）不告警。"""
        import logging
        _app, db, svc, a_root = env
        p = a_root / "w.strm"
        p.write_text("/mnt/M/w", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            svc.initial_scan_a(use_bulk=False, a_roots=[a_root],
                               use_snapshot=False)
        assert any("误剪" in r.message for r in caplog.records), (
            "局部 a_roots + use_snapshot=False 必须记 prune 误剪风险 WARNING")
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            svc.initial_scan_a(use_bulk=False, use_snapshot=False)
        assert not any("误剪" in r.message for r in caplog.records), (
            "全量审计（a_roots=None）keep 集完备，不得告警")
