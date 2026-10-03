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
    ctime_ns INTEGER NOT NULL DEFAULT 0,
    indexed_at REAL NOT NULL
)
"""


def _ensure_ctime_column(db: Database) -> None:
    """契约列补齐：Database.__init__ 建表若尚无 ctime_ns（迁移未落地），
    经 PRAGMA table_info 探测式 ALTER 补列（与 production 迁移同型幂等），
    保证红灯落在行为断言而非"列不存在"的脚手架错误上。"""
    with db.connection() as conn:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(a_strm_snapshot)").fetchall()}
        if cols and "ctime_ns" not in cols:
            conn.execute(
                "ALTER TABLE a_strm_snapshot "
                "ADD COLUMN ctime_ns INTEGER NOT NULL DEFAULT 0")
        conn.commit()


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


def _insert_snapshot(db: Database, row: tuple, ctime_ns: int) -> None:
    """row 为既有七元组（local_path..indexed_at）；ctime_ns 显式传入，
    由各用例决定真值或失配值，不默认取真值。"""
    with db.connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO a_strm_snapshot "
            "(local_path, file_size, mtime_ns, webdav_path, parent_webdav_path, "
            "parse_version, ctime_ns, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (*row[:6], ctime_ns, row[6]),
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
    _ensure_ctime_column(db)
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
                 CUR_PARSE_VERSION, time.time()), ctime_ns=st.st_ctime_ns)
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
                 CUR_PARSE_VERSION, time.time()), ctime_ns=0)
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
                 "/mnt/M", CUR_PARSE_VERSION, time.time()), ctime_ns=0)
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
                 time.time()), ctime_ns=0)
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
                 CUR_PARSE_VERSION, time.time()), ctime_ns=0)
        orig_prune = db.prune_a_snapshot_not_in
        seen: list[list[str]] = []

        def spy_prune(paths):
            seen.append(list(paths))
            orig_prune(paths)

        db.prune_a_snapshot_not_in = spy_prune
        counter = _ReadCounter()
        with patch.object(sync_service_mod, "read_strm_webdav_path", counter):
            # a_roots=None 全量审计（env 夹具单根，与原局部根等价覆盖）
            svc.initial_scan_a(use_bulk=False, use_snapshot=False)
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
                 STALE_PARSE_VERSION, time.time()), ctime_ns=0)
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
                 CUR_PARSE_VERSION, time.time()), ctime_ns=0)
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
                              "/mnt/M/keep", "/mnt/M", CUR_PARSE_VERSION, time.time()),
                         ctime_ns=0)
        _insert_snapshot(db, (str(gone), st_gone.st_size, st_gone.st_mtime_ns,
                              "/mnt/M/gone", "/mnt/M", CUR_PARSE_VERSION, time.time()),
                         ctime_ns=0)
        # 删除 gone.strm，使其成为「集外行」（扫描集合外 → 应被 prune 清除）
        gone.unlink()
        # a_roots=None 全量审计（env 夹具单根，与原局部根等价覆盖）
        svc.initial_scan_a(use_bulk=False, use_snapshot=False)
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
                 "/mnt/M", CUR_PARSE_VERSION, time.time()), ctime_ns=0)
        # 走 copy_a_record_to_b 的冗余清理分支：B 目标不存在 + 云端权威缺席
        svc.app.admin_api.check_exists = Mock(return_value=False)
        svc.app.build_b_path_from_a = Mock(
            return_value=a_root / "b_target" / "redundant.strm")
        ret = svc.copy_a_record_to_b(
            str(p), "/mnt/M/redundant", "/mnt/M", mapping_id="mid-e1")
        assert ret is False, "冗余清理分支按现状返回 False"
        assert _snapshot_row(db, str(p)) is None, (
            "A 区记录删除后快照行必须同步失效，不得残留孤儿行")

    def test_audit_mode_snapshot_sharded_writes_complete(self, env):
        """审计模式分片提交防回归锚：>1000 条时快照行经 flush_batch 分片落库，
        全量等值（不丢行/不重复）。"""
        _app, db, svc, a_root = env
        sub = a_root / "many"
        sub.mkdir()
        n = 1100
        for i in range(n):
            (sub / f"f{i:04d}.strm").write_text(f"/mnt/M/f{i:04d}", encoding="utf-8")
        svc.initial_scan_a(use_bulk=False, use_snapshot=False)
        with db.read_connection() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM a_strm_snapshot").fetchone()[0]
        assert cnt == n, f"快照行应恰为 {n} 条（分片提交不得丢行/重复），实际 {cnt}"

    def test_redundant_index_not_created(self, env):
        """idx_a_strm_snapshot_idx(indexed_at) 无任何查询使用，不得再创建
        （DROP 兼容既有库，新库 schema 中该索引必须缺席）。"""
        _app, db, _svc, _root = env
        with db.read_connection() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name='idx_a_strm_snapshot_idx'"
            ).fetchone()
        assert row is None, "冗余索引 idx_a_strm_snapshot_idx 不应存在"

    def test_snapshot_mode_sharded_commits_mid_scan(self, env):
        """非 bulk 快照模式分片提交：>BATCH_SIZE 扫描时快照写随批次分片落库
        （与审计模式同型），消除 snap_batch 无界内存累积；行数全量等值。"""
        from unittest.mock import patch as _patch
        _app, db, svc, a_root = env
        sub = a_root / "shard"
        sub.mkdir()
        n = 1100
        for i in range(n):
            (sub / f"s{i:04d}.strm").write_text(f"/mnt/M/s{i:04d}", encoding="utf-8")
        with _patch.object(db, "upsert_a_snapshot_bulk",
                           wraps=db.upsert_a_snapshot_bulk) as spy:
            svc.initial_scan_a(use_bulk=False)
        assert spy.call_count >= 2, (
            f"非 bulk 快照模式必须随批次分片提交快照写，实际提交 {spy.call_count} 次")
        with db.read_connection() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM a_strm_snapshot").fetchone()[0]
        assert cnt == n, f"快照行应恰为 {n} 条（分片提交不得丢行/重复），实际 {cnt}"

    def test_same_size_same_mtime_new_ctime_must_reread(self, env):
        """E2 采信门第五维：同 size 同 mtime_ns 但 ctime 失配（还原场景）→
        不得复用旧快照链接，必须读正文并重写快照 ctime。"""
        _app, db, svc, a_root = env
        p = a_root / "restored.strm"
        p.write_text("/mnt/M/restored", encoding="utf-8")
        st = os.stat(p)
        _insert_snapshot(
            db, (str(p), st.st_size, st.st_mtime_ns, "/mnt/M/restored", "/mnt/M",
                 CUR_PARSE_VERSION, time.time()),
            ctime_ns=st.st_ctime_ns + 1)
        counter = _ReadCounter()
        with patch.object(sync_service_mod, "read_strm_webdav_path", counter):
            svc.initial_scan_a(use_bulk=False)
        assert len(counter.calls) == 1, (
            "同 size 同 mtime 但 ctime 失配（还原场景）必须重读正文，"
            f"实际读取: {counter.calls}")
        with db.read_connection() as conn:
            row = conn.execute(
                "SELECT ctime_ns FROM a_strm_snapshot WHERE local_path = ?",
                (str(p),)).fetchone()
        assert row is not None and row[0] == st.st_ctime_ns, (
            "重读后快照行 ctime 必须重写为真值")


class TestAuditPruneGuard:
    """全量审计 prune 守卫：keep 集不完整（根不可达/遍历出错/解析失败）时
    fail-closed 跳过 prune；walk 成功的空结果（合法清空）照常放行。"""

    def test_case_a_unreachable_root_rows_survive(self, env):
        """部分根不可达：不可达根的快照行不得被整片误剪。"""
        _app, db, svc, a_root = env
        live = a_root / "live.strm"
        live.write_text("/mnt/M/live", encoding="utf-8")
        st = os.stat(live)
        _insert_snapshot(db, (str(live), st.st_size, st.st_mtime_ns,
                              "/mnt/M/live", "/mnt/M", CUR_PARSE_VERSION, time.time()),
                         ctime_ns=0)
        ghost = a_root.parent / "missing-root" / "ghost.strm"
        _insert_snapshot(db, (str(ghost), 10, 123, "/mnt/G/ghost", "/mnt/G",
                              CUR_PARSE_VERSION, time.time()), ctime_ns=0)
        # a_roots=None 契约下经 app.a_roots 注入"部分根不可达"场景，
        # prune fail-closed 检测力不变（traversed_roots < len(roots) 跳过 prune）
        _app.a_roots.append(a_root.parent / "missing-root")
        svc.initial_scan_a(use_bulk=False, use_snapshot=False)
        assert _snapshot_row(db, str(ghost)) is not None, (
            "根不可达 ≠ 云端文件已消失，该根快照行不得被 prune 整片误剪")

    def test_case_b_all_roots_unreachable_no_full_wipe(self, env):
        """全部根不可达：keep 集为空，绝不全表清空。"""
        _app, db, svc, a_root = env
        ghost = a_root.parent / "missing-root" / "ghost.strm"
        _insert_snapshot(db, (str(ghost), 10, 123, "/mnt/G/ghost", "/mnt/G",
                              CUR_PARSE_VERSION, time.time()), ctime_ns=0)
        # a_roots=None 契约下经 app.a_roots 注入"全部根不可达"场景，
        # "不得全表清空"检测力不变
        _app.a_roots = [a_root.parent / "missing-root"]
        svc.initial_scan_a(use_bulk=False, use_snapshot=False)
        assert _snapshot_row(db, str(ghost)) is not None, (
            "全部根不可达时不得走 DELETE FROM a_strm_snapshot 全表清空")

    def test_case_c_unparseable_file_row_deleted_after_audit(self, env):
        """文件在场但正文不可解析：prune keep 集仍收录该文件（防误剪其它
        行），但其旧快照行在审计后显式删除——防后续普通扫描经采信门复用
        过期链接（契约 C4，替代旧"行保留"语义）。"""
        _app, db, svc, a_root = env
        p = a_root / "broken.strm"
        p.write_text("", encoding="utf-8")
        st = os.stat(p)
        _insert_snapshot(db, (str(p), st.st_size, st.st_mtime_ns,
                              "/mnt/M/broken", "/mnt/M", CUR_PARSE_VERSION,
                              time.time()), ctime_ns=0)
        # 可解析对照行：keep 集保护力——若 keep 集不再收录不可解析文件，
        # prune 会连本行一起误删，删除断言失去区分力
        good = a_root / "good-keep.strm"
        good.write_text("/mnt/M/good", encoding="utf-8")
        st_good = os.stat(good)
        _insert_snapshot(db, (str(good), st_good.st_size, st_good.st_mtime_ns,
                              "/mnt/M/good", "/mnt/M", CUR_PARSE_VERSION,
                              time.time()), ctime_ns=st_good.st_ctime_ns)
        counter = _ReadCounter()
        with patch.object(sync_service_mod, "read_strm_webdav_path", counter):
            svc.initial_scan_a(use_bulk=False, use_snapshot=False)
        assert str(p) in counter.calls, (
            "不可解析文件必须真实被读取（分支真实执行，而非被采信门短路）")
        assert _snapshot_row(db, str(p)) is None, (
            "审计后不可解析文件的旧快照行必须删除，防采信门复用过期链接")
        assert _snapshot_row(db, str(good)) is not None, (
            "keep 集必须保护可解析对照行的快照行（防误剪检测力）")

    def test_case_d_legal_empty_all_roots_walked_prune_proceeds(self, env):
        """合法清空：全部根 walk 无错但 .strm 已全部移除 → prune 照常执行。"""
        _app, db, svc, a_root = env
        p = a_root / "stale.strm"
        p.write_text("/mnt/M/stale", encoding="utf-8")
        st = os.stat(p)
        _insert_snapshot(db, (str(p), st.st_size, st.st_mtime_ns,
                              "/mnt/M/stale", "/mnt/M", CUR_PARSE_VERSION,
                              time.time()), ctime_ns=0)
        p.unlink()
        svc.initial_scan_a(use_bulk=False, use_snapshot=False)
        assert _snapshot_row(db, str(p)) is None, (
            "walk 成功返回空即权威空目录，陈旧快照行必须被 prune 清除")

    def test_walk_error_skips_prune(self, env):
        """os.walk onerror 触发（子树不可读被静默跳过）：keep 集不完整，跳过 prune。"""
        _app, db, svc, a_root = env
        p = a_root / "ok.strm"
        p.write_text("/mnt/M/ok", encoding="utf-8")
        st = os.stat(p)
        _insert_snapshot(db, (str(p), st.st_size, st.st_mtime_ns,
                              "/mnt/M/ok", "/mnt/M", CUR_PARSE_VERSION, time.time()),
                         ctime_ns=0)
        hidden = a_root.parent / "unwalkable-root" / "deep.strm"
        _insert_snapshot(db, (str(hidden), 10, 123, "/mnt/U/deep", "/mnt/U",
                              CUR_PARSE_VERSION, time.time()), ctime_ns=0)
        real_walk = sync_service_mod.os.walk

        def fake_walk(top, onerror=None, *args, **kwargs):
            for item in real_walk(top, *args, **kwargs):
                yield item
            # 模拟该根遍历末尾触发 onerror（子树不可读）；旧实现不传 onerror
            # 则静默无事发生，prune 照跑 → 语义红（误剪）而非脚手架异常
            if onerror:
                onerror(OSError(13, "permission denied"))

        with patch.object(sync_service_mod.os, "walk", side_effect=fake_walk):
            # a_roots=None 全量审计（env 夹具单根；fake_walk 对该根触发
            # onerror → traversed_roots 不计数 → prune fail-closed 跳过）
            svc.initial_scan_a(use_bulk=False, use_snapshot=False)
        assert _snapshot_row(db, str(hidden)) is not None, (
            "遍历出错（onerror 触发）时 keep 集不完整，prune 必须 fail-closed 跳过")

    def test_e3_partial_audit_rejected_full_audit_no_warning(self, env, caplog):
        """局部 a_roots + use_snapshot=False → 直接拒绝（fail-closed）；
        全量审计（a_roots=None）keep 集完备，不告警。"""
        import logging
        _app, db, svc, a_root = env
        p = a_root / "w.strm"
        p.write_text("/mnt/M/w", encoding="utf-8")
        with pytest.raises(ValueError):
            svc.initial_scan_a(use_bulk=False, a_roots=[a_root],
                               use_snapshot=False)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            svc.initial_scan_a(use_bulk=False, use_snapshot=False)
        assert not any("误剪" in r.message for r in caplog.records), (
            "全量审计（a_roots=None）keep 集完备，不得告警")


# ===========================================================================
# 审计非法组合 fail-closed 拒绝契约
# ===========================================================================

class TestAuditLocalRootsRejected:
    """全量审计（use_snapshot=False）与局部 a_roots 同用必须直接拒绝。

    期望值由行为契约推导：prune keep 集来自本轮实际扫描集合，与局部根同用
    会误剪范围外快照行——fail-closed 抛 ValueError，而非 WARNING 后照跑。
    """

    def test_audit_with_local_roots_raises(self, tmp_path):
        app = build_mock_app(tmp_path)
        svc = SyncService(app)
        with pytest.raises(ValueError, match="a_roots=None"):
            svc.initial_scan_a(use_snapshot=False, a_roots=[tmp_path / "a"])

    def test_audit_with_empty_roots_list_also_raises(self, tmp_path):
        """a_roots=[] 也是"限定局部根"（空 keep 集会剪光快照），同样拒绝；
        拒绝判定必须先于 a_roots==[] 的提前返回分支。"""
        app = build_mock_app(tmp_path)
        svc = SyncService(app)
        with pytest.raises(ValueError):
            svc.initial_scan_a(use_snapshot=False, a_roots=[])

    def test_audit_with_none_roots_allowed(self, tmp_path):
        """合法组合 a_roots=None + use_snapshot=False 不受影响。"""
        app = build_mock_app(tmp_path, a_dirs=[tmp_path / "a"])
        (tmp_path / "a").mkdir(parents=True, exist_ok=True)
        svc = SyncService(app)
        svc.initial_scan_a(use_snapshot=False, a_roots=None)

    def test_snapshot_with_local_roots_allowed(self, tmp_path):
        """合法组合 a_roots 非空 + use_snapshot=True 不受影响。"""
        app = build_mock_app(tmp_path, a_dirs=[tmp_path / "a"])
        (tmp_path / "a").mkdir(parents=True, exist_ok=True)
        svc = SyncService(app)
        svc.initial_scan_a(use_snapshot=True, a_roots=[tmp_path / "a"])

    def test_empty_roots_early_return_still_works_for_snapshot(self, tmp_path):
        """a_roots=[] + use_snapshot=True 走既有提前返回分支，不抛异常。"""
        app = build_mock_app(tmp_path)
        svc = SyncService(app)
        svc.initial_scan_a(use_snapshot=True, a_roots=[])


# ===========================================================================
# flush_batch 解耦早返回——快照提交不得被 batch 空判定静默跳过
# ===========================================================================

class TestFlushBatchDecoupledEarlyReturn:
    """batch 空 + snap_batch 非空时快照仍必须被提交。

    选型说明：batch 与 snap_batch 在公开面上逐条同追加（process_strm_file
    结果处理），"batch 空而 snap 非空"经公开入口不可达，纯黑盒行为断言
    无法区分新旧实现。故本用例采用双锚：① 端到端端态正向回归锚（两根
    扫描、第二根为空时 flush_batch 以 batch 空被再次调用，快照行必须仍在
    终态写入——行为不变即应保持通过，防解耦改动反向破坏既有终态）；
    ② inspect.getsource 机械防回退锚：早返回条件不得再单独键于 batch，
    缺该形态时本用例失败。
    """

    def test_snapshot_survives_batch_empty_flush(self, tmp_path):
        app = build_mock_app(tmp_path, a_dirs=[tmp_path / "a", tmp_path / "b"])
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        root_a.mkdir(parents=True, exist_ok=True)
        root_b.mkdir(parents=True, exist_ok=True)
        (root_a / "movie.strm").write_text("/mount/movie.mp4", encoding="utf-8")
        # root_b 无任何 .strm → 该根尾部的 flush_batch() 以 batch 空被调用
        svc = SyncService(app)
        svc.initial_scan_a(use_snapshot=True, a_roots=[root_a, root_b])
        rows = app.db.upsert_a_snapshot_bulk.call_args_list
        assert rows, "快照行必须被提交（终态守恒：解耦不得反向吞掉快照写）"

    def test_flush_early_return_not_solely_keyed_on_batch(self):
        import inspect
        src = inspect.getsource(SyncService.initial_scan_a)
        assert "if not batch and not snap_batch:" in src, (
            "flush_batch 早返回必须解耦：batch 与 snap_batch 均空才返回，"
            "防止快照提交被 batch 空判定静默跳过")


def test_audit_scan_deletes_snapshot_rows_of_unparseable_files(env, monkeypatch):
    """契约 C4：审计扫描（use_snapshot=False）中正文不可解析且存在旧快照行的
    文件，扫描结束后其快照行必须被删除，防后续普通扫描经采信门复用过期链接；
    其它文件快照行不受影响；快照模式扫描不触发删除。"""
    app, db, svc, root = env
    root = Path(root)
    counter = _ReadCounter()
    monkeypatch.setattr(sync_service_mod, "read_strm_webdav_path", counter)

    # good.strm：正文可解析；bad.strm：写非法内容使正文不可解析（读得空串）
    good = root / "good.strm"
    good.write_text("/cloud/good.mkv", encoding="utf-8")
    bad = root / "bad.strm"
    bad.write_text("", encoding="utf-8")

    for lp in (str(good), str(bad)):
        st = os.stat(lp)
        _insert_snapshot(db, (lp, st.st_size, st.st_mtime_ns, "/cloud/old.mkv",
                              "/cloud", CUR_PARSE_VERSION, time.time()),
                         st.st_ctime_ns)

    svc.initial_scan_a(use_snapshot=False, use_bulk=False)

    assert _snapshot_row(db, str(bad)) is None, (
        "审计后不可解析文件的旧快照行必须已删除")
    row = _snapshot_row(db, str(good))
    assert row is not None, "可解析文件的快照行不受影响"
    # webdav_path 为 _snapshot_row SELECT 列序索引 3：断言该行是被扫描重写
    # （/cloud/good.mkv）而非被误删后缺席/残留旧值
    assert row[3] == "/cloud/good.mkv", (
        f"可解析文件的快照行应被重写为最新 webdav_path，实际 {row[3]!r}")


def test_snapshot_mode_scan_does_not_delete_unparseable_rows(env, monkeypatch):
    """契约 C4 不该触发域：快照模式（use_snapshot=True）对正文不可解析文件
    不收集不删除。用例不预插快照行，使文件真实走进不可解析分支（而非被
    采信门短路），守门断言该分支在快照模式下不做任何快照行写入/删除——
    该文件的快照行保持缺席，处置交由下轮审计。"""
    app, db, svc, root = env
    root = Path(root)
    counter = _ReadCounter()
    monkeypatch.setattr(sync_service_mod, "read_strm_webdav_path", counter)

    # 不预插快照行：bad2.strm 无采信门命中，真实走进不可解析分支
    bad = root / "bad2.strm"
    bad.write_text("", encoding="utf-8")

    svc.initial_scan_a(use_snapshot=True, use_bulk=False)

    assert str(bad) in counter.calls, (
        "不可解析分支必须真实被执行（文件确实被读且读得不可解析）")
    assert _snapshot_row(db, str(bad)) is None, (
        "快照模式对不可解析文件不得写入或保留快照行")


def test_unparseable_snapshot_deletion_is_batched_and_exception_isolated(
        env, caplog):
    """契约 W4/C1：审计模式对不可解析文件的快照行删除必须经批量接口
    delete_a_snapshots_batch（去重 + 单事务），且批量失败仅 WARNING 不中断
    审计余下阶段（save_known_folders_batch 等侧效仍完成）。"""
    import logging
    app, db, svc, a_root = env
    good = a_root / "iso-good.strm"
    good.write_text("/mnt/M/iso-good", encoding="utf-8")
    bad1 = a_root / "iso-bad1.strm"
    bad1.write_text("", encoding="utf-8")
    bad2 = a_root / "iso-bad2.strm"
    bad2.write_text("", encoding="utf-8")
    for lp in (str(good), str(bad1), str(bad2)):
        st = os.stat(lp)
        _insert_snapshot(db, (lp, st.st_size, st.st_mtime_ns, "/mnt/M/old",
                              "/mnt/M", CUR_PARSE_VERSION, time.time()),
                         st.st_ctime_ns)

    # 场景一：批量删除抛异常 → 审计不中断，仅告警；余下阶段（目录登记）仍完成
    with caplog.at_level(logging.WARNING):
        with patch.object(db, "delete_a_snapshots_batch",
                          side_effect=RuntimeError("db boom")):
            with patch.object(db, "save_known_folders_batch",
                              wraps=db.save_known_folders_batch) as folders_spy:
                svc.initial_scan_a(use_snapshot=False, use_bulk=False)
    assert any("批量删除失败" in r.message for r in caplog.records), (
        "批量删除失败必须落 WARNING（异常隔离可见性）")
    assert folders_spy.call_count >= 1, (
        "审计余下阶段必须完成（删除失败不得中断 save_known_folders_batch）")

    # 场景二：放行真实批量删除 → 不可解析行清除、可解析行存活（下轮审计重试自愈）
    svc.initial_scan_a(use_snapshot=False, use_bulk=False)
    assert _snapshot_row(db, str(bad1)) is None, (
        "批量删除放行后不可解析文件快照行必须被删除")
    assert _snapshot_row(db, str(bad2)) is None, (
        "批量删除放行后不可解析文件快照行必须被删除")
    assert _snapshot_row(db, str(good)) is not None, (
        "可解析文件快照行不受批量删除影响")


def test_delete_a_snapshots_batch_dedupes_and_deletes(env):
    """契约 W4：delete_a_snapshots_batch 去重（保序）、批量删除并返回去重后
    条数；空列表早退 0 且不获取写锁。"""
    app, db, svc, a_root = env
    x = a_root / "x.strm"
    x.write_text("/mnt/M/x", encoding="utf-8")
    y = a_root / "y.strm"
    y.write_text("/mnt/M/y", encoding="utf-8")
    for lp in (str(x), str(y)):
        st = os.stat(lp)
        _insert_snapshot(db, (lp, st.st_size, st.st_mtime_ns, "/mnt/M/old",
                              "/mnt/M", CUR_PARSE_VERSION, time.time()),
                         st.st_ctime_ns)

    n = db.delete_a_snapshots_batch([str(x), str(x), str(y)])
    assert n == 2, f"返回值必须为去重后的删除意图条数 2，实际 {n}"
    assert _snapshot_row(db, str(x)) is None
    assert _snapshot_row(db, str(y)) is None

    # 空列表：早退 0，不获取写锁（Mock 探针确认）
    with patch.object(db.rw_lock, "write_locked",
                      wraps=db.rw_lock.write_locked) as spy:
        ret = db.delete_a_snapshots_batch([])
    assert ret == 0, f"空列表必须返回 0，实际 {ret}"
    assert spy.call_count == 0, (
        f"空列表必须早退、不获取写锁，实际获取 {spy.call_count} 次")
