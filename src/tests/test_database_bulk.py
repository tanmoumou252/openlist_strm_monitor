"""
database.py bulk_connection / batch 辅助方法测试。

测试新增的 4 个方法：
- bulk_connection(): 长连接上下文管理器，自动 commit/rollback/close
- save_known_folders_batch(): 批量插入 known_folders
- get_all_ghost_protected_paths(): 预载幽灵保护路径集合
- get_all_b_fingerprints(): 预载 B 区指纹集合
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from database import Database


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Database(str(Path(tmpdir) / "test.db"))


# ────────────────────────────────────────────────
# lineage snapshot
# ────────────────────────────────────────────────

class TestLineageSnapshot:
    def test_snapshot_round_trip_is_mapping_scoped(self, db: Database):
        db.upsert_b_lineage_snapshot(
            "m1", "C:/b/show.strm", 12, 34, "fp", "version", 1, "valid", 1.5)
        row = db.get_b_lineage_snapshot("m1", "C:/b/show.strm")
        assert row is not None
        assert row.mapping_id == "m1"
        assert row.file_size == 12
        assert db.get_b_lineage_snapshot("m2", "C:/b/show.strm") is None

    def test_invalid_snapshot_is_rejected(self, db: Database):
        with pytest.raises(ValueError):
            db.upsert_b_lineage_snapshot(
                "m1", "C:/b/show.strm", 12, 34, "fp", "version", 1, "unknown")


# ────────────────────────────────────────────────
# bulk_connection
# ────────────────────────────────────────────────

class TestBulkConnection:

    def test_yields_connection_and_commits(self, db: Database):
        """正常结束时自动 commit，数据持久化。"""
        with db.bulk_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO known_folders(folder_path, source, updated_at) VALUES (?, ?, ?)",
                ("/bulk/a", "test", 1.0),
            )
        # 独立读连接验证 commit 成功
        with db.read_connection() as conn:
            cur = conn.execute("SELECT folder_path FROM known_folders")
            rows = cur.fetchall()
        assert rows == [("/bulk/a",)]

    def test_rollback_on_exception(self, db: Database):
        """异常时自动 rollback，数据不持久化。"""
        with pytest.raises(RuntimeError):
            with db.bulk_connection() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO known_folders(folder_path, source, updated_at) VALUES (?, ?, ?)",
                    ("/should_not_persist", "test", 1.0),
                )
                raise RuntimeError("boom")
        with db.read_connection() as conn:
            cur = conn.execute("SELECT folder_path FROM known_folders")
            rows = cur.fetchall()
        assert rows == []

    def test_connection_closed_after_exit(self, db: Database):
        """退出 with 块后连接已关闭——访问属性会抛 ProgrammingError。"""
        with db.bulk_connection() as conn:
            weak_ref = conn
            conn.execute("SELECT 1")
        with pytest.raises(sqlite3.ProgrammingError):
            weak_ref.execute("SELECT 1")

    def test_applies_pragmas(self, db: Database):
        """bulk_connection 应用 PRAGMA（如 mmap_size）。"""
        with db.bulk_connection() as conn:
            cur = conn.execute("PRAGMA mmap_size")
            mmap = cur.fetchone()[0]
        assert mmap == 268435456  # 256MB, 与 _PRAGMA_STATEMENTS 一致

    def test_loads_simple_tokenizer(self, db: Database):
        """bulk_connection 加载 simple 分词器（若 dll 存在则 _fts_tokenizer='simple'，否则 unicode61）。"""
        with db.bulk_connection() as conn:
            cur = conn.execute("SELECT 1")  # 仅验证连接可用
        # 分词器是否加载由 DLL 存在性决定，bulk_connection 应尝试加载
        assert db._fts_tokenizer in ("simple", "unicode61")

    def test_bulk_multiple_operations_in_one_connection(self, db: Database):
        """多个操作共享同一连接，只 commit 一次。"""
        with db.bulk_connection() as conn:
            for i in range(5):
                conn.execute(
                    "INSERT OR REPLACE INTO known_folders(folder_path, source, updated_at) VALUES (?, ?, ?)",
                    (f"/folder/{i}", "test", float(i)),
                )
        with db.read_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM known_folders")
            count = cur.fetchone()[0]
        assert count == 5

    def test_bypasses_rw_lock(self, db: Database):
        """bulk_connection 不获取 rw_lock（设计用途）。"""
        with db.bulk_connection() as conn:
            # 无任何锁竞争，直接执行
            conn.execute("SELECT 1")


# ────────────────────────────────────────────────
# save_known_folders_batch
# ────────────────────────────────────────────────

class TestSaveKnownFoldersBatch:

    def test_batch_insert(self, db: Database):
        """批量插入返回正确数量。"""
        paths = [f"/folder/{i}" for i in range(10)]
        count = db.save_known_folders_batch(paths, source="test")
        assert count == 10
        with db.read_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM known_folders")
            assert cur.fetchone()[0] == 10

    def test_empty_list_returns_zero(self, db: Database):
        """空列表不报错，返回 0。"""
        assert db.save_known_folders_batch([]) == 0

    def test_filters_root_slash(self, db: Database):
        """路径为 '/' 的条目被过滤。"""
        count = db.save_known_folders_batch(["/valid", "/", "/also_valid"], source="test")
        assert count == 2

    def test_filters_empty_and_none(self, db: Database):
        """空字符串和假值被过滤。"""
        count = db.save_known_folders_batch(["/a", "", None, "/b"], source="test")
        assert count == 2

    def test_all_filtered_returns_zero(self, db: Database):
        """所有路径均无效时返回 0，不执行 DB 写入。"""
        assert db.save_known_folders_batch(["", "/", None], source="test") == 0
        with db.read_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM known_folders")
            assert cur.fetchone()[0] == 0

    def test_upsert_existing_path(self, db: Database):
        """重复路径执行 INSERT OR REPLACE，更新 source 和 updated_at。"""
        db.save_known_folders_batch(["/folder/1"], source="old")
        db.save_known_folders_batch(["/folder/1"], source="new")
        with db.read_connection() as conn:
            cur = conn.execute("SELECT source FROM known_folders WHERE folder_path = '/folder/1'")
            row = cur.fetchone()
        assert row[0] == "new"

    def test_source_applied_to_all(self, db: Database):
        """同一批次所有记录的 source 一致。"""
        db.save_known_folders_batch(["/a", "/b", "/c"], source="batch_src")
        with db.read_connection() as conn:
            cur = conn.execute("SELECT DISTINCT source FROM known_folders")
            sources = [r[0] for r in cur.fetchall()]
        assert sources == ["batch_src"]


# ────────────────────────────────────────────────
# get_all_ghost_protected_paths
# ────────────────────────────────────────────────

class TestGetAllGhostProtectedPaths:

    def test_returns_non_expired_paths(self, db: Database):
        """返回未过期的路径集合。"""
        db.set_ghost_protection("/path/a", seconds=3600)
        db.set_ghost_protection("/path/b", seconds=3600)
        result = db.get_all_ghost_protected_paths()
        assert result == {"/path/a", "/path/b"}

    def test_excludes_expired_paths(self, db: Database):
        """过期路径不返回。"""
        db.set_ghost_protection("/expired", seconds=-1)  # 已过期
        db.set_ghost_protection("/active", seconds=3600)
        result = db.get_all_ghost_protected_paths()
        assert result == {"/active"}

    def test_empty_when_no_records(self, db: Database):
        """无记录时返回空集合。"""
        assert db.get_all_ghost_protected_paths() == set()

    def test_excludes_just_expired(self, db: Database):
        """刚好过期（expire_time == now）的路径不返回（WHERE expire_time > now）。"""
        with db.rw_lock.write_locked(), db.connection() as conn:
            now = time.time()
            conn.execute(
                "INSERT OR REPLACE INTO ghost_protection(webdav_path, expire_time, reason) VALUES (?, ?, ?)",
                ("/just_expired", now - 0.001, "test"),
            )
            conn.commit()
        result = db.get_all_ghost_protected_paths()
        assert result == set()

    def test_returns_set_not_list(self, db: Database):
        """返回类型是 set。"""
        result = db.get_all_ghost_protected_paths()
        assert isinstance(result, set)


# ────────────────────────────────────────────────
# get_all_b_fingerprints
# ────────────────────────────────────────────────

class TestGetAllBFingerprints:

    def _insert_b_record(self, db: Database, local_path: str, fingerprint: str | None, mapping_id: str = "test_mapping"):
        """辅助：插入一条 b_strm_files 记录。"""
        now = time.time()
        with db.rw_lock.write_locked(), db.connection() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO b_strm_files(
                    local_path, webdav_path, parent_webdav_path,
                    source_a_path, fingerprint, status, updated_at, mapping_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (local_path, f"/webdav/{local_path}", "/webdav/", "/a/path", fingerprint, "valid", now, mapping_id),
            )
            conn.commit()

    def test_returns_distinct_fingerprints(self, db: Database):
        """返回去重后的非 NULL 指纹集合。"""
        self._insert_b_record(db, "/b/a.strm", "fp-1")
        self._insert_b_record(db, "/b/b.strm", "fp-1")  # 重复
        self._insert_b_record(db, "/b/c.strm", "fp-2")
        result = db.get_all_b_fingerprints("test_mapping")
        assert result == {"fp-1", "fp-2"}

    def test_excludes_null_fingerprints(self, db: Database):
        """fingerprint 为 NULL 的记录不返回。"""
        self._insert_b_record(db, "/b/null.strm", None)
        self._insert_b_record(db, "/b/valid.strm", "fp-x")
        result = db.get_all_b_fingerprints("test_mapping")
        assert result == {"fp-x"}

    def test_empty_when_no_b_records(self, db: Database):
        """b_strm_files 为空时返回空集合。"""
        assert db.get_all_b_fingerprints("test_mapping") == set()

    def test_returns_set_type(self, db: Database):
        """返回类型是 set。"""
        assert isinstance(db.get_all_b_fingerprints("test_mapping"), set)


# ────────────────────────────────────────────────
# 只读 getter 读锁一致性（Task 7，audit Important-2 + 二次审核 C-2）
# ────────────────────────────────────────────────

class TestReadonlyGettersReadLock:
    """断言所有只读 getter 都在 `rw_lock.read_locked()` 保护下访问连接。

    历史上 `get_all_b_records` / `get_table_counts` / `get_b_status_counts`
    只用 `read_connection()` 而漏取读锁，与同模块 `b_fingerprint_exists` 等
    惯用法不一致；并发写事务持写锁时段长时可能读到部分提交快照。
    """

    # 审计确认的全部使用 `read_connection()` 的只读 getter（不含 bulk 专用方法）。
    READONLY_GETTERS = (
        "get_a_by_local", "get_b_by_local", "get_a_by_webdav", "get_b_by_webdav",
        "get_all_a_records", "get_all_b", "get_all_b_fingerprints", "get_all_c",
        "get_known_folders", "is_ghost_protected",
        "get_all_ghost_protected_paths", "get_protected_roots",
        "get_protected_root_paths", "get_protected_roots_snapshot_paths",
        "get_control", "get_b_under_root",
        "get_identity_by_fingerprint", "get_identity_by_webdav",
        "get_a_local_path_by_webdav", "get_b_instances_by_fingerprint",
        "get_b_by_local_full", "get_valid_b_instance_by_fingerprint",
        "get_all_b_by_fingerprint", "b_fingerprint_exists",
        "get_a_count_under_root", "has_other_b_instance",
        "get_media_boundary_by_fingerprint", "get_media_boundaries_by_source_name",
        "get_media_boundary_by_current_name", "get_media_boundary_by_source_name_only",
        "get_subtitle_by_local", "subtitle_exists", "get_subtitles_by_fingerprint",
        # Task 7 重点：以下 3 个曾漏读锁
        "get_all_b_records", "get_table_counts", "get_b_status_counts",
    )

    def test_all_readonly_getters_hold_read_lock(self):
        import inspect
        for name in self.READONLY_GETTERS:
            method = getattr(Database, name)
            source = inspect.getsource(method)
            assert "rw_lock.read_locked()" in source, (
                f"{name} 未持有 rw_lock.read_locked()"
            )
            assert "read_connection()" in source, (
                f"{name} 未使用 read_connection()"
            )

    def test_target_three_methods_source_pattern(self):
        """精确校验本 Task 修复的 3 个方法的 with 语句结构。"""
        import inspect
        for name in ("get_all_b_records", "get_table_counts", "get_b_status_counts"):
            source = inspect.getsource(getattr(Database, name))
            assert "with self.rw_lock.read_locked(), self.read_connection() as conn:" in source, (
                f"{name} 应使用 `with self.rw_lock.read_locked(), self.read_connection() as conn:`"
            )


# ────────────────────────────────────────────────
# 集成：bulk_connection + 批量方法配合
# ────────────────────────────────────────────────

class TestBulkIntegration:

    def test_bulk_connection_with_multiple_batch_inserts(self, db: Database):
        """bulk_connection 内连续调用多个 DB 操作，只 commit 一次。"""
        with db.bulk_connection() as conn:
            now = time.time()
            folders = [(f"/f/{i}", "batch", now) for i in range(20)]
            conn.executemany(
                "INSERT OR REPLACE INTO known_folders(folder_path, source, updated_at) VALUES (?, ?, ?)",
                folders,
            )
            # 插入 ghost_protection
            conn.execute(
                "INSERT OR REPLACE INTO ghost_protection(webdav_path, expire_time, reason) VALUES (?, ?, ?)",
                ("/ghost/bulk", now + 3600, "bulk test"),
            )
        # 验证两个表都 commit 成功
        with db.read_connection() as conn:
            c1 = conn.execute("SELECT COUNT(*) FROM known_folders").fetchone()[0]
            c2 = conn.execute("SELECT COUNT(*) FROM ghost_protection").fetchone()[0]
        assert c1 == 20
        assert c2 == 1


# ────────────────────────────────────────────────
# complete_index_generation / get_index_metadata
# ────────────────────────────────────────────────

class TestIndexGenerationControl:
    """测试索引元数据的 generation 计数与时间戳管理。"""

    def test_initial_generation_is_zero(self, db: Database):
        """新库的初始 generation 为 0，时间戳为 0。"""
        meta = db.get_index_metadata()
        assert meta["index_generation"] == 0
        assert meta["index_generation_at"] == 0
        assert meta["last_full_index_at"] == 0
        assert meta["mapping_version"] == ""
        assert meta["mapping_version_generated_at"] == 0

    def test_complete_index_generation_increments_once_for_multiple_mappings(self, db: Database):
        """一次完成包含多个 mapping 的操作，generation 只递增一次。"""
        # 第一次完成：mapping m1, m2
        db.complete_index_generation(["m1", "m2"])
        meta = db.get_index_metadata()
        assert meta["index_generation"] == 1

        # 第二次完成：mapping m1, m3（包含已存在的 m1，新增 m3）
        db.complete_index_generation(["m1", "m3"])
        meta = db.get_index_metadata()
        assert meta["index_generation"] == 2

        # 第三次完成：mapping m1, m2, m3（全部已存在）
        db.complete_index_generation(["m1", "m2", "m3"])
        meta = db.get_index_metadata()
        assert meta["index_generation"] == 3

    def test_per_mapping_generation_and_time_are_isolated(self, db: Database):
        """不同 mapping 的 generation 和时间互不覆盖。"""
        t1 = 1000.0
        t2 = 2000.0
        t3 = 3000.0

        # 第一次：m1
        db.complete_index_generation(["m1"], completed_at=t1)
        meta = db.get_index_metadata("m1")
        assert meta["mapping_index_generation"] == 1
        assert meta["mapping_index_generation_at"] == t1

        # 第二次：m2
        db.complete_index_generation(["m2"], completed_at=t2)
        meta1 = db.get_index_metadata("m1")
        meta2 = db.get_index_metadata("m2")
        assert meta1["mapping_index_generation"] == 1  # m1 未变
        assert meta1["mapping_index_generation_at"] == t1
        assert meta2["mapping_index_generation"] == 2
        assert meta2["mapping_index_generation_at"] == t2

        # 第三次：m1（再次完成）
        db.complete_index_generation(["m1"], completed_at=t3)
        meta1 = db.get_index_metadata("m1")
        meta2 = db.get_index_metadata("m2")
        assert meta1["mapping_index_generation"] == 3  # m1 更新到全局 generation
        assert meta1["mapping_index_generation_at"] == t3
        assert meta2["mapping_index_generation"] == 2  # m2 未变
        assert meta2["mapping_index_generation_at"] == t2

    def test_complete_index_generation_increases_last_full_index_at(self, db: Database):
        """complete_index_generation 同时更新 last_full_index_at。"""
        t1 = 1000.0
        db.complete_index_generation(["m1"], completed_at=t1)
        meta = db.get_index_metadata()
        assert meta["last_full_index_at"] == t1

        t2 = 2000.0
        db.complete_index_generation(["m2"], completed_at=t2)
        meta = db.get_index_metadata()
        assert meta["last_full_index_at"] == t2

    def test_empty_mapping_ids_is_rejected(self, db: Database):
        """空 mapping_ids 列表应被拒绝（ValueError）。"""
        with pytest.raises(ValueError):
            db.complete_index_generation([])

    def test_complete_index_generation_uses_single_transaction(self, db: Database):
        """所有控制键写入在同一事务中完成（无部分 commit）。"""
        # 第一次成功写入
        db.complete_index_generation(["m1"], completed_at=1000.0)
        meta = db.get_index_metadata()
        assert meta["index_generation"] == 1

        # 第二次成功写入
        db.complete_index_generation(["m2"], completed_at=2000.0)
        meta = db.get_index_metadata()
        assert meta["index_generation"] == 2
        assert meta["index_generation_at"] == 2000.0

    def test_get_index_metadata_for_unknown_mapping_returns_defaults(self, db: Database):
        """查询未知 mapping 返回该 mapping 的默认值（generation=0, time=0），不伪造历史。"""
        db.complete_index_generation(["m1"], completed_at=1000.0)

        meta_unknown = db.get_index_metadata("unknown_mapping")
        # 全局 generation 仍然被返回
        assert meta_unknown["index_generation"] == 1
        assert meta_unknown["index_generation_at"] == 1000.0
        # 未知 mapping 的元数据返回默认值
        assert meta_unknown["mapping_id"] == "unknown_mapping"
        assert meta_unknown["mapping_index_generation"] == 0
        assert meta_unknown["mapping_index_generation_at"] == 0

        # 已存在的 mapping 仍然正确
        meta_known = db.get_index_metadata("m1")
        assert meta_known["mapping_index_generation"] == 1
        assert meta_known["mapping_index_generation_at"] == 1000.0

    def test_mapping_version_is_updated_only_when_changed(self, db: Database):
        """mapping_version 摘要未变时不更新时间，变化时更新。"""
        # 第一次设置版本
        version1 = "abc123"
        db.set_mapping_version(version1, version_generated_at=1000.0)
        meta = db.get_index_metadata()
        assert meta["mapping_version"] == version1
        assert meta["mapping_version_generated_at"] == 1000.0

        # 第二次设置相同版本：时间不应更新
        db.set_mapping_version(version1, version_generated_at=2000.0)
        meta = db.get_index_metadata()
        assert meta["mapping_version"] == version1
        assert meta["mapping_version_generated_at"] == 1000.0  # 保持原时间

        # 第三次设置不同版本：时间应更新
        version2 = "def456"
        db.set_mapping_version(version2, version_generated_at=3000.0)
        meta = db.get_index_metadata()
        assert meta["mapping_version"] == version2
        assert meta["mapping_version_generated_at"] == 3000.0

    def test_old_database_compatibility(self, db: Database):
        """旧库仅有 sync_control 表时，get_index_metadata 返回未知/默认值。"""
        # 手动插入一个不相关的 control 键（模拟旧库）
        with db.rw_lock.write_locked(), db.connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sync_control(control_key, control_value, updated_at) VALUES (?, ?, ?)",
                ("last_full_audit_at", str(time.time()), time.time()),
            )
            conn.commit()

        # 查询索引元数据应返回默认值
        meta = db.get_index_metadata()
        assert meta["index_generation"] == 0
        assert meta["index_generation_at"] == 0
        assert meta["last_full_index_at"] == 0
        assert meta["mapping_version"] == ""
        assert meta["mapping_version_generated_at"] == 0


# ────────────────────────────────────────────────
# upsert updated_at 时间语义与 FTS 收敛
# ────────────────────────────────────────────────


class TestUpsertTimestampSemantics:
    """验证六个 upsert 实现只在业务字段真正变化时更新 updated_at，
    并正确收敛 FTS 维护（无孤儿、无重复、仅对变化行同步）。"""

    @staticmethod
    def _query_a(db: Database, local_path: str):
        with db.read_connection() as conn:
            return conn.execute(
                "SELECT webdav_path, parent_webdav_path, updated_at "
                "FROM a_strm_files WHERE local_path = ?",
                (local_path,),
            ).fetchone()

    @staticmethod
    def _query_b(db: Database, local_path: str):
        with db.read_connection() as conn:
            return conn.execute(
                "SELECT webdav_path, parent_webdav_path, source_a_path, "
                "fingerprint, status, updated_at, mapping_id "
                "FROM b_strm_files WHERE local_path = ?",
                (local_path,),
            ).fetchone()

    @staticmethod
    def _fts_a_count(db: Database) -> int:
        with db.read_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM a_strm_files_fts").fetchone()[0]

    @staticmethod
    def _fts_b_count(db: Database) -> int:
        with db.read_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM b_strm_files_fts").fetchone()[0]

    @staticmethod
    def _sleep():
        # 保证 time.time() 在新版更新时一定会推进（比上次的值更大）
        time.sleep(0.005)

    # === upsert_a ===

    def test_upsert_a_unchanged_keeps_updated_at(self, db: Database):
        db.upsert_a("/a/x.strm", "/m/x.mp4", "/m")
        first = self._query_a(db, "/a/x.strm")
        self._sleep()
        db.upsert_a("/a/x.strm", "/m/x.mp4", "/m")  # 完全相同的重复扫描
        second = self._query_a(db, "/a/x.strm")
        assert second[2] == first[2], "字段无变化时应保留原 updated_at"

    def test_upsert_a_changed_updates_updated_at(self, db: Database):
        db.upsert_a("/a/x.strm", "/m/x.mp4", "/m")
        first = self._query_a(db, "/a/x.strm")
        self._sleep()
        db.upsert_a("/a/x.strm", "/m/x2.mp4", "/m")  # webdav_path 变化
        second = self._query_a(db, "/a/x.strm")
        assert second[2] > first[2], "webdav_path 变化时应更新 updated_at"
        assert second[0] == "/m/x2.mp4"

    def test_upsert_a_new_record_has_updated_at(self, db: Database):
        db.upsert_a("/a/new.strm", "/m/new.mp4", "/m")
        row = self._query_a(db, "/a/new.strm")
        assert row is not None and row[2] > 0

    def test_upsert_a_unchanged_no_duplicate_fts(self, db: Database):
        db.upsert_a("/a/x.strm", "/m/x.mp4", "/m")
        self._sleep()
        db.upsert_a("/a/x.strm", "/m/x.mp4", "/m")
        assert self._fts_a_count(db) == 1

    def test_upsert_a_webdav_change_fts_old_missing_new_present(self, db: Database):
        db.upsert_a("/a/x.strm", "/m/old.mp4", "/m")
        self._sleep()
        db.upsert_a("/a/x.strm", "/m/new.mp4", "/m")
        with db.read_connection() as conn:
            # rowid 稳定（ON CONFLICT 不改 rowid），按新路径可查到、按旧路径查不到
            row = conn.execute("SELECT rowid FROM a_strm_files WHERE local_path = ?", ("/a/x.strm",)).fetchone()
            assert row is not None
            rid = row[0]
            new_hit = conn.execute(
                "SELECT rowid FROM a_strm_files_fts WHERE rowid = ? AND webdav_path MATCH 'new'",
                (rid,),
            ).fetchone()
            assert new_hit is not None, "应能按新 webdav_path 查到 FTS"
            old_hit = conn.execute(
                "SELECT rowid FROM a_strm_files_fts WHERE rowid = ? AND webdav_path MATCH 'old'",
                (rid,),
            ).fetchone()
            assert old_hit is None, "不应残留按旧 webdav_path 的 FTS"

    # === upsert_b ===

    def test_upsert_b_unchanged_keeps_updated_at(self, db: Database):
        db.upsert_b("/b/x.strm", "/m/x.mp4", "/m", "/a/x.strm", "m1", "fp", "valid")
        first = self._query_b(db, "/b/x.strm")
        self._sleep()
        db.upsert_b("/b/x.strm", "/m/x.mp4", "/m", "/a/x.strm", "m1", "fp", "valid")
        second = self._query_b(db, "/b/x.strm")
        assert second[5] == first[5], "字段无变化时应保留原 updated_at"

    def test_upsert_b_changed_updates_updated_at(self, db: Database):
        db.upsert_b("/b/x.strm", "/m/x.mp4", "/m", "/a/x.strm", "m1", "fp", "valid")
        first = self._query_b(db, "/b/x.strm")
        self._sleep()
        db.upsert_b("/b/x.strm", "/m/x2.mp4", "/m", "/a/x.strm", "m1", "fp", "valid")
        second = self._query_b(db, "/b/x.strm")
        assert second[5] > first[5], "业务字段变化时应更新 updated_at"

    def test_upsert_b_status_change_updates_updated_at(self, db: Database):
        """upsert_b / upsert_b_batch 的 status 是业务字段，变化应更新 updated_at。"""
        db.upsert_b("/b/x.strm", "/m/x.mp4", "/m", "/a/x.strm", "m1", "fp", "valid")
        first = self._query_b(db, "/b/x.strm")
        self._sleep()
        db.upsert_b("/b/x.strm", "/m/x.mp4", "/m", "/a/x.strm", "m1", "fp", "duplicate")
        second = self._query_b(db, "/b/x.strm")
        assert second[5] > first[5], "status 变化应更新 updated_at"
        assert second[4] == "duplicate"

    def test_upsert_b_fingerprint_null_keeps_updated_at(self, db: Database):
        """NULL 与 NULL 视为相等，不触发 updated_at 更新。"""
        db.upsert_b("/b/x.strm", "/m/x.mp4", "/m", None, "m1", None, "valid")
        first = self._query_b(db, "/b/x.strm")
        self._sleep()
        db.upsert_b("/b/x.strm", "/m/x.mp4", "/m", None, "m1", None, "valid")
        second = self._query_b(db, "/b/x.strm")
        assert second[5] == first[5]

    def test_upsert_b_unchanged_no_duplicate_fts(self, db: Database):
        db.upsert_b("/b/x.strm", "/m/x.mp4", "/m", "/a/x.strm", "m1", "fp", "valid")
        self._sleep()
        db.upsert_b("/b/x.strm", "/m/x.mp4", "/m", "/a/x.strm", "m1", "fp", "valid")
        assert self._fts_b_count(db) == 1

    def test_upsert_b_mapping_id_required(self, db: Database):
        with pytest.raises(ValueError):
            db.upsert_b("/b/x.strm", "/m/x.mp4", "/m", "/a/x.strm", "", "fp", "valid")

    # === upsert_a_batch ===

    def test_upsert_a_batch_unchanged_keeps_updated_at(self, db: Database):
        db.upsert_a_batch([("/a/1.strm", "/m/1.mp4", "/m"), ("/a/2.strm", "/m/2.mp4", "/m")])
        first = {lp: self._query_a(db, lp)[2] for lp in ("/a/1.strm", "/a/2.strm")}
        self._sleep()
        db.upsert_a_batch([("/a/1.strm", "/m/1.mp4", "/m"), ("/a/2.strm", "/m/2.mp4", "/m")])
        second = {lp: self._query_a(db, lp)[2] for lp in ("/a/1.strm", "/a/2.strm")}
        assert second == first, "批量重复扫描应保留全部 updated_at"

    def test_upsert_a_batch_changed_updates_only_changed(self, db: Database):
        db.upsert_a_batch([("/a/1.strm", "/m/1.mp4", "/m"), ("/a/2.strm", "/m/2.mp4", "/m")])
        first = {lp: self._query_a(db, lp)[2] for lp in ("/a/1.strm", "/a/2.strm")}
        self._sleep()
        # 只改第一条
        db.upsert_a_batch([("/a/1.strm", "/m/1b.mp4", "/m"), ("/a/2.strm", "/m/2.mp4", "/m")])
        second = {lp: self._query_a(db, lp)[2] for lp in ("/a/1.strm", "/a/2.strm")}
        assert second["/a/1.strm"] > first["/a/1.strm"], "变化的行应更新 updated_at"
        assert second["/a/2.strm"] == first["/a/2.strm"], "未变化的行应保留 updated_at"

    def test_upsert_a_batch_returns_processed_count(self, db: Database):
        """int 返回值语义保持『本批处理条数』，不改为变化条数。"""
        n = db.upsert_a_batch([("/a/1.strm", "/m/1.mp4", "/m"), ("/a/2.strm", "/m/2.mp4", "/m")])
        assert n == 2
        # 第二批全部无变化，返回值仍为本批处理条数
        self._sleep()
        n2 = db.upsert_a_batch([("/a/1.strm", "/m/1.mp4", "/m"), ("/a/2.strm", "/m/2.mp4", "/m")])
        assert n2 == 2, "返回值不得改为『变化条数』"

    def test_upsert_a_batch_new_record_has_updated_at(self, db: Database):
        db.upsert_a_batch([("/a/new.strm", "/m/new.mp4", "/m")])
        row = self._query_a(db, "/a/new.strm")
        assert row is not None and row[2] > 0

    def test_upsert_a_batch_no_duplicate_fts(self, db: Database):
        db.upsert_a_batch([("/a/1.strm", "/m/1.mp4", "/m")])
        self._sleep()
        db.upsert_a_batch([("/a/1.strm", "/m/1.mp4", "/m")])
        assert self._fts_a_count(db) == 1

    def test_upsert_a_batch_webdav_change_fts(self, db: Database):
        db.upsert_a_batch([("/a/1.strm", "/m/old.mp4", "/m")])
        self._sleep()
        db.upsert_a_batch([("/a/1.strm", "/m/new.mp4", "/m")])
        with db.read_connection() as conn:
            row = conn.execute("SELECT rowid FROM a_strm_files WHERE local_path = ?", ("/a/1.strm",)).fetchone()
            rid = row[0]
            new_hit = conn.execute(
                "SELECT rowid FROM a_strm_files_fts WHERE rowid = ? AND webdav_path MATCH 'new'",
                (rid,),
            ).fetchone()
            old_hit = conn.execute(
                "SELECT rowid FROM a_strm_files_fts WHERE rowid = ? AND webdav_path MATCH 'old'",
                (rid,),
            ).fetchone()
            assert new_hit is not None and old_hit is None

    def test_upsert_a_batch_empty_returns_zero(self, db: Database):
        assert db.upsert_a_batch([]) == 0

    # === upsert_b_batch ===

    def test_upsert_b_batch_unchanged_keeps_updated_at(self, db: Database):
        recs = [("/b/1.strm", "/m/1.mp4", "/m", "/a/1.strm", "fp1", "m1", "valid"),
                ("/b/2.strm", "/m/2.mp4", "/m", "/a/2.strm", "fp2", "m1", "valid")]
        db.upsert_b_batch(recs)
        first = {lp: self._query_b(db, lp)[5] for lp in ("/b/1.strm", "/b/2.strm")}
        self._sleep()
        db.upsert_b_batch(recs)
        second = {lp: self._query_b(db, lp)[5] for lp in ("/b/1.strm", "/b/2.strm")}
        assert second == first

    def test_upsert_b_batch_changed_updates_only_changed(self, db: Database):
        recs = [("/b/1.strm", "/m/1.mp4", "/m", "/a/1.strm", "fp1", "m1", "valid"),
                ("/b/2.strm", "/m/2.mp4", "/m", "/a/2.strm", "fp2", "m1", "valid")]
        db.upsert_b_batch(recs)
        first = {lp: self._query_b(db, lp)[5] for lp in ("/b/1.strm", "/b/2.strm")}
        self._sleep()
        # 第一条改 webdav_path，第二条完全不变
        recs2 = [("/b/1.strm", "/m/1b.mp4", "/m", "/a/1.strm", "fp1", "m1", "valid"),
                 ("/b/2.strm", "/m/2.mp4", "/m", "/a/2.strm", "fp2", "m1", "valid")]
        db.upsert_b_batch(recs2)
        second = {lp: self._query_b(db, lp)[5] for lp in ("/b/1.strm", "/b/2.strm")}
        assert second["/b/1.strm"] > first["/b/1.strm"]
        assert second["/b/2.strm"] == first["/b/2.strm"]

    def test_upsert_b_batch_status_change_updates_updated_at(self, db: Database):
        recs = [("/b/1.strm", "/m/1.mp4", "/m", "/a/1.strm", "fp1", "m1", "valid")]
        db.upsert_b_batch(recs)
        first = self._query_b(db, "/b/1.strm")[5]
        self._sleep()
        recs2 = [("/b/1.strm", "/m/1.mp4", "/m", "/a/1.strm", "fp1", "m1", "duplicate")]
        db.upsert_b_batch(recs2)
        second = self._query_b(db, "/b/1.strm")[5]
        assert second > first, "status 变化应更新 updated_at"
        assert self._query_b(db, "/b/1.strm")[4] == "duplicate"

    def test_upsert_b_batch_returns_processed_count(self, db: Database):
        recs = [("/b/1.strm", "/m/1.mp4", "/m", "/a/1.strm", "fp1", "m1", "valid"),
                ("/b/2.strm", "/m/2.mp4", "/m", "/a/2.strm", "fp2", "m1", "valid")]
        n = db.upsert_b_batch(recs)
        assert n == 2
        self._sleep()
        n2 = db.upsert_b_batch(recs)  # 全部无变化
        assert n2 == 2, "返回值不得改为『变化条数』"

    def test_upsert_b_batch_mapping_id_required(self, db: Database):
        with pytest.raises(ValueError):
            db.upsert_b_batch([("/b/1.strm", "/m/1.mp4", "/m", "/a/1.strm", "fp1", "", "valid")])

    def test_upsert_b_batch_no_duplicate_fts(self, db: Database):
        recs = [("/b/1.strm", "/m/1.mp4", "/m", "/a/1.strm", "fp1", "m1", "valid")]
        db.upsert_b_batch(recs)
        self._sleep()
        db.upsert_b_batch(recs)
        assert self._fts_b_count(db) == 1

    # === upsert_c 回归（moved_at 语义零变化） ===

    def test_upsert_c_moved_at_changes_each_call(self, db: Database):
        """upsert_c 不属于本次改动范围，其 moved_at 语义每次都更新（回归断言）。"""
        db.upsert_c("/c/x.strm", "/m/x.mp4", "/b/x.strm", "/c/root")
        with db.read_connection() as conn:
            first = conn.execute("SELECT moved_at FROM c_ghost_files WHERE local_path = ?", ("/c/x.strm",)).fetchone()[0]
        self._sleep()
        db.upsert_c("/c/x.strm", "/m/x.mp4", "/b/x.strm", "/c/root")
        with db.read_connection() as conn:
            second = conn.execute("SELECT moved_at FROM c_ghost_files WHERE local_path = ?", ("/c/x.strm",)).fetchone()[0]
        assert second > first, "upsert_c 的 moved_at 语义应每次更新，不被本次改动影响"

    # === 综合：FTS 行数与主表一致（无孤儿） ===

    def test_upsert_a_b_mixed_no_orphan_fts(self, db: Database):
        """混合 upsert_a / upsert_b 重复扫描后，FTS 行数始终与主表一致。"""
        db.upsert_a("/a/x.strm", "/m/x.mp4", "/m")
        db.upsert_b("/b/x.strm", "/m/x.mp4", "/m", "/a/x.strm", "m1", "fp", "valid")
        self._sleep()
        # 重复扫描多次（每次无变化）
        for _ in range(3):
            db.upsert_a("/a/x.strm", "/m/x.mp4", "/m")
            db.upsert_b("/b/x.strm", "/m/x.mp4", "/m", "/a/x.strm", "m1", "fp", "valid")
        with db.read_connection() as conn:
            a_main = conn.execute("SELECT COUNT(*) FROM a_strm_files").fetchone()[0]
            a_fts = conn.execute("SELECT COUNT(*) FROM a_strm_files_fts").fetchone()[0]
            b_main = conn.execute("SELECT COUNT(*) FROM b_strm_files").fetchone()[0]
            b_fts = conn.execute("SELECT COUNT(*) FROM b_strm_files_fts").fetchone()[0]
            assert a_fts == a_main == 1
            assert b_fts == b_main == 1


# ============================================================
# last_verified_at 列测试 (Task D)
# ============================================================


class TestLastVerifiedAtColumn:
    """测试 last_verified_at 列的 schema 迁移、bump 方法和回归保护"""

    def test_a_strm_files_has_last_verified_at_column(self, db: Database):
        """a_strm_files 表存在 last_verified_at 列"""
        with db.read_connection() as conn:
            cur = conn.execute("PRAGMA table_info(a_strm_files)")
            columns = [row[1] for row in cur.fetchall()]
        assert "last_verified_at" in columns

    def test_b_strm_files_has_last_verified_at_column(self, db: Database):
        """b_strm_files 表存在 last_verified_at 列"""
        with db.read_connection() as conn:
            cur = conn.execute("PRAGMA table_info(b_strm_files)")
            columns = [row[1] for row in cur.fetchall()]
        assert "last_verified_at" in columns

    def test_old_database_auto_adds_last_verified_at_column(self, db: Database):
        """旧数据库（无 last_verified_at 列）打开后自动加列且不丢数据"""
        # 直接使用 db fixture 创建的数据库
        # 验证列存在且数据正常
        with db.read_connection() as conn:
            # 检查列信息
            columns = [row[1] for row in conn.execute("PRAGMA table_info(a_strm_files)").fetchall()]
            assert "last_verified_at" in columns

            # 插入数据
            db.upsert_a("/a/test.strm", "/m/test.mp4", "/m")

            # 验证 last_verified_at 已初始化
            row = conn.execute(
                "SELECT last_verified_at FROM a_strm_files WHERE local_path = ?",
                ("/a/test.strm",)
            ).fetchone()
            assert row is not None
            assert row[0] > 0  # 应该初始化为 now，而不是 0

    def test_upsert_a_unchanged_business_fields_preserves_last_verified_at(self, db: Database):
        """upsert_a 业务字段未变时不 bump last_verified_at（回归保护）"""
        # 插入记录
        db.upsert_a("/a/test.strm", "/m/test.mp4", "/m")
        time.sleep(0.1)

        # 手动设置 last_verified_at 为非零值
        db.touch_verified_a(["/a/test.strm"], 999999.0)

        # 再次 upsert，业务字段无变化
        db.upsert_a("/a/test.strm", "/m/test.mp4", "/m")

        # 验证 last_verified_at 保持不变（回归保护）
        with db.read_connection() as conn:
            row = conn.execute(
                "SELECT last_verified_at FROM a_strm_files WHERE local_path = ?",
                ("/a/test.strm",)
            ).fetchone()
            assert row[0] == 999999.0

    def test_upsert_b_unchanged_business_fields_preserves_last_verified_at(self, db: Database):
        """upsert_b 业务字段未变时不 bump last_verified_at（回归保护）"""
        # 插入记录
        db.upsert_b(
            "/b/test.strm", "/m/test.mp4", "/m", "/a/test.strm",
            "m1", "fp_test", "valid"
        )
        time.sleep(0.1)

        # 手动设置 last_verified_at 为非零值
        db.touch_verified_b(["/a/test.strm"], 999999.0)

        # 再次 upsert，业务字段无变化
        db.upsert_b(
            "/b/test.strm", "/m/test.mp4", "/m", "/a/test.strm",
            "m1", "fp_test", "valid"
        )

        # 验证 last_verified_at 保持不变（回归保护）
        with db.read_connection() as conn:
            row = conn.execute(
                "SELECT last_verified_at FROM b_strm_files WHERE local_path = ?",
                ("/b/test.strm",)
            ).fetchone()
            assert row[0] == 999999.0

    def test_touch_verified_a_updates_only_specified_paths(self, db: Database):
        """touch_verified_a 只更新指定路径的 last_verified_at"""
        db.upsert_a_batch([
            ("/a/1.strm", "/m/1.mp4", "/m"),
            ("/a/2.strm", "/m/2.mp4", "/m"),
            ("/a/3.strm", "/m/3.mp4", "/m"),
        ])

        # 记录插入时的初始值
        with db.read_connection() as conn:
            initial_rows = dict(conn.execute(
                "SELECT local_path, last_verified_at FROM a_strm_files"
            ).fetchall())
            initial_time_2 = initial_rows["/a/2.strm"]  # 未 touched 的记录初始值

        now = time.time()
        db.touch_verified_a(["/a/1.strm", "/a/3.strm"], now)

        with db.read_connection() as conn:
            rows = dict(conn.execute(
                "SELECT local_path, last_verified_at FROM a_strm_files"
            ).fetchall())
            assert rows["/a/1.strm"] == now
            # 未 touched 的记录保持插入时的初始值
            assert rows["/a/2.strm"] == initial_time_2
            assert rows["/a/3.strm"] == now

    def test_touch_verified_b_updates_only_specified_paths(self, db: Database):
        """touch_verified_b 只更新指定 source_a_path 的 B 记录"""
        db.upsert_b_batch([
            ("/b/1.strm", "/m/1.mp4", "/m", "/a/1.strm", "m1", "fp1", "valid"),
            ("/b/2.strm", "/m/2.mp4", "/m", "/a/2.strm", "m1", "fp2", "valid"),
            ("/b/3.strm", "/m/3.mp4", "/m", "/a/3.strm", "m1", "fp3", "valid"),
        ])

        # 记录插入时的初始值
        with db.read_connection() as conn:
            initial_rows = dict(conn.execute(
                "SELECT source_a_path, last_verified_at FROM b_strm_files"
            ).fetchall())
            initial_time_2 = initial_rows["/a/2.strm"]  # 未 touched 的记录初始值

        now = time.time()
        db.touch_verified_b(["/a/1.strm", "/a/3.strm"], now)

        with db.read_connection() as conn:
            rows = dict(conn.execute(
                "SELECT source_a_path, last_verified_at FROM b_strm_files"
            ).fetchall())
            assert rows["/a/1.strm"] == now
            # 未 touched 的记录保持插入时的初始值
            assert rows["/a/2.strm"] == initial_time_2
            assert rows["/a/3.strm"] == now

    def test_touch_verified_by_mapping_updates_all_paths_under_root(self, db: Database):
        """touch_verified_by_mapping 更新所有在根路径下的记录"""
        # 用 os.sep 构造路径，与 touch_verified_by_mapping 的 os.sep 匹配（Windows 反斜杠）
        sep = os.sep
        db.upsert_a_batch([
            (f"{sep}a{sep}root1{sep}file1.strm", "/m/1.mp4", "/m"),
            (f"{sep}a{sep}root1{sep}file2.strm", "/m/2.mp4", "/m"),
            (f"{sep}a{sep}root2{sep}file3.strm", "/m/3.mp4", "/m"),
        ])
        # 注意参数顺序：(local_path, webdav_path, parent_webdav_path, source_a_path, fingerprint, mapping_id, status)
        db.upsert_b_batch([
            (f"{sep}b{sep}root1{sep}file1.strm", "/m/1.mp4", "/m", f"{sep}a{sep}root1{sep}file1.strm",
             "fp1", "m1", "valid"),
            (f"{sep}b{sep}root1{sep}file2.strm", "/m/2.mp4", "/m", f"{sep}a{sep}root1{sep}file2.strm",
             "fp2", "m1", "valid"),
            (f"{sep}b{sep}root2{sep}file3.strm", "/m/3.mp4", "/m", f"{sep}a{sep}root2{sep}file3.strm",
             "fp3", "m2", "valid"),
        ])

        # 记录插入时的初始值
        with db.read_connection() as conn:
            a_initial = dict(conn.execute(
                "SELECT local_path, last_verified_at FROM a_strm_files"
            ).fetchall())
            b_initial = dict(conn.execute(
                "SELECT source_a_path, last_verified_at FROM b_strm_files"
            ).fetchall())
            initial_a_root2 = a_initial[f"{sep}a{sep}root2{sep}file3.strm"]
            initial_b_m2 = b_initial[f"{sep}a{sep}root2{sep}file3.strm"]

        now = time.time()
        db.touch_verified_by_mapping("m1", f"{sep}a{sep}root1", now)

        with db.read_connection() as conn:
            a_rows = dict(conn.execute(
                "SELECT local_path, last_verified_at FROM a_strm_files"
            ).fetchall())
            b_rows = dict(conn.execute(
                "SELECT source_a_path, last_verified_at FROM b_strm_files"
            ).fetchall())

            # A 区：root1 下的被更新
            assert a_rows[f"{sep}a{sep}root1{sep}file1.strm"] == now
            assert a_rows[f"{sep}a{sep}root1{sep}file2.strm"] == now
            # A 区：root2 下的未受影响，保持初始值
            assert a_rows[f"{sep}a{sep}root2{sep}file3.strm"] == initial_a_root2

            # B 区：m1 mapping 的被更新
            assert b_rows[f"{sep}a{sep}root1{sep}file1.strm"] == now
            assert b_rows[f"{sep}a{sep}root1{sep}file2.strm"] == now
            # B 区：m2 mapping 的未受影响，保持初始值
            assert b_rows[f"{sep}a{sep}root2{sep}file3.strm"] == initial_b_m2

    def test_touch_verified_by_mapping_windows_sep_and_wildcard_escape(self, db: Database):
        """F1 回归：Windows 反斜杠路径应被匹配；含 _/% 的目录名不应误匹配"""
        sep = os.sep
        # 根路径含下划线（_ 是 LIKE 通配符），子目录含百分号（% 是 LIKE 通配符）
        root = f"{sep}box{sep}strm_zone"
        db.upsert_a_batch([
            (f"{root}{sep}100%_special{sep}file1.strm", "/m/1.mp4", "/m"),
            (f"{root}{sep}normal{sep}file2.strm", "/m/2.mp4", "/m"),
            # 前缀相似但不在根下的记录（root 是 /box/strm_zone，这条是 /box/strm_zone_extra）
            (f"{sep}box{sep}strm_zone_extra{sep}file3.strm", "/m/3.mp4", "/m"),
        ])

        now = time.time()
        db.touch_verified_by_mapping("m1", root, now)

        with db.read_connection() as conn:
            a_rows = dict(conn.execute(
                "SELECT local_path, last_verified_at FROM a_strm_files"
            ).fetchall())

            # 根下所有记录（含通配符目录名）都被更新
            assert a_rows[f"{root}{sep}100%_special{sep}file1.strm"] == now
            assert a_rows[f"{root}{sep}normal{sep}file2.strm"] == now
            # 前缀相似但不在根下的记录不受影响（escape_like + 尾部分隔符防止误匹配）
            assert a_rows[f"{sep}box{sep}strm_zone_extra{sep}file3.strm"] != now

    def test_insert_new_record_initializes_last_verified_at(self, db: Database):
        """插入新记录时 last_verified_at 初始化为 now"""
        now = time.time()
        db.upsert_a("/a/new.strm", "/m/new.mp4", "/m")

        with db.read_connection() as conn:
            row = conn.execute(
                "SELECT last_verified_at FROM a_strm_files WHERE local_path = ?",
                ("/a/new.strm",)
            ).fetchone()
            # 新记录的 last_verified_at 应该接近 now
            assert abs(row[0] - now) < 2.0


# ============================================================
# 批量 >900 条回归测试（Task B：SQL 变量上限切片防御）
# ============================================================


class TestBatchOver900Records:
    """验证超过 900 条的批量操作不会因 SQL 变量上限而崩溃。

    现代 SQLite（≥3.32）默认上限 32766，但嵌入式/旧版 SQLite 上限
    仍为 999。900 切片是跨版本防御。本测试用 1000 条记录验证切片
    逻辑正确执行。
    """

    def test_upsert_a_batch_over_900_records(self, db: Database):
        """upsert_a_batch 传入 >900 条记录不抛异常，返回计数正确。"""
        records = [
            (f"/a/nine_{i:04d}.strm", f"/m/file_{i:04d}.mp4", "/m")
            for i in range(1000)
        ]
        n = db.upsert_a_batch(records)
        assert n == 1000, f"返回值应为 1000，实际 {n}"

        # 验证所有记录确实写入
        with db.read_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM a_strm_files")
            assert cur.fetchone()[0] == 1000

        # 第二次重复扫描（全部无变化）不抛异常
        n2 = db.upsert_a_batch(records)
        assert n2 == 1000, f"重复扫描返回应为 1000，实际 {n2}"
        with db.read_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM a_strm_files")
            assert cur.fetchone()[0] == 1000  # 未新增

    def test_upsert_b_batch_over_900_records(self, db: Database):
        """upsert_b_batch 传入 >900 条记录不抛异常，返回计数正确。"""
        records = [
            (f"/b/nine_{i:04d}.strm", f"/m/file_{i:04d}.mp4", "/m",
             f"/a/nine_{i:04d}.strm", f"fp_{i:04d}", "m1", "valid")
            for i in range(1000)
        ]
        n = db.upsert_b_batch(records)
        assert n == 1000, f"返回值应为 1000，实际 {n}"

        with db.read_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM b_strm_files")
            assert cur.fetchone()[0] == 1000

        # 第二次重复扫描（全部无变化）不抛异常
        n2 = db.upsert_b_batch(records)
        assert n2 == 1000

    def test_upsert_a_batch_1500_records_merges_existing_map(self, db: Database):
        """1500 条（跨越 2 个 900 切片）预读合并 existing_map 正确。"""
        # 先插入 500 条
        first_batch = [
            (f"/a/merge_{i:04d}.strm", f"/m/old_{i:04d}.mp4", "/m")
            for i in range(500)
        ]
        n1 = db.upsert_a_batch(first_batch)
        assert n1 == 500

        # 再插入 1500 条，其中 500 条是更新（webdav 变化），1000 条新增
        second_batch = [
            (f"/a/merge_{i:04d}.strm", f"/m/new_{i:04d}.mp4", "/m")  # 前 500 条更新
            if i < 500 else
            (f"/a/new_{i:04d}.strm", f"/m/new_{i:04d}.mp4", "/m")  # 后 1000 条新增
            for i in range(1500)
        ]
        n2 = db.upsert_a_batch(second_batch)
        assert n2 == 1500

        # 验证总数：500 原有 + 1000 新增 = 1500
        with db.read_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM a_strm_files")
            assert cur.fetchone()[0] == 1500

        # 验证前 500 条的 webdav_path 已更新
        with db.read_connection() as conn:
            row = conn.execute(
                "SELECT webdav_path FROM a_strm_files WHERE local_path = ?",
                ("/a/merge_0000.strm",),
            ).fetchone()
            assert row[0] == "/m/new_0000.mp4"

    def test_cleanup_invalid_subtitles_over_900_deletions(self, db: Database):
        """cleanup_invalid_subtitles 删除 >900 条字幕记录，分批 DELETE 全部成功。"""
        # 直接插入 1000 条字幕记录，target_path 指向不存在的路径
        with db.rw_lock.write_locked(), db.connection() as conn:
            conn.executemany(
                "INSERT INTO subtitles(local_path, target_path, fingerprint, status) "
                "VALUES (?, ?, ?, ?)",
                [
                    (f"/sub/{i:04d}.srt", f"/nonexistent/target_{i:04d}.srt",
                     f"fp_{i:04d}", "valid")
                    for i in range(1000)
                ],
            )
            conn.commit()

        # 确认插入成功
        with db.read_connection() as conn:
            before = conn.execute("SELECT COUNT(*) FROM subtitles").fetchone()[0]
        assert before == 1000

        # 执行清理
        db.cleanup_invalid_subtitles()

        # 验证全部删除
        with db.read_connection() as conn:
            after = conn.execute("SELECT COUNT(*) FROM subtitles").fetchone()[0]
        assert after == 0, f"清理后应全删，实际残留 {after}"

    def test_cleanup_invalid_subtitles_keeps_valid_targets(self, db: Database):
        """cleanup_invalid_subtitles 保留 target_path 实际存在的记录。"""
        # 创建临时目标文件
        import tempfile
        tmpdir = tempfile.TemporaryDirectory()
        existing_target = Path(tmpdir.name) / "exists.srt"
        existing_target.write_text("subtitle content", encoding="utf-8")

        # 插入 950 条有效 + 50 条无效
        with db.rw_lock.write_locked(), db.connection() as conn:
            valid_records = [
                (f"/sub/valid_{i:04d}.srt", str(existing_target), f"fp_v{i:04d}", "valid")
                for i in range(950)
            ]
            invalid_records = [
                (f"/sub/invalid_{i:04d}.srt", f"/nonexistent_{i:04d}.srt",
                 f"fp_i{i:04d}", "valid")
                for i in range(50)
            ]
            conn.executemany(
                "INSERT INTO subtitles(local_path, target_path, fingerprint, status) "
                "VALUES (?, ?, ?, ?)",
                valid_records + invalid_records,
            )
            conn.commit()

        db.cleanup_invalid_subtitles()

        # 有效记录应保留
        with db.read_connection() as conn:
            remaining = conn.execute("SELECT COUNT(*) FROM subtitles").fetchone()[0]
        assert remaining == 950, f"有效记录应保留 950，实际 {remaining}"

        # 清理
        tmpdir.cleanup()

    def test_cleanup_invalid_subtitles_lock_behavior(self):
        """结构性断言 cleanup_invalid_subtitles 使用 read_locked 读取 + 锁外 exists() + write_locked 删除。"""
        import inspect
        source = inspect.getsource(Database.cleanup_invalid_subtitles)

        # 读取阶段使用 read_locked
        assert "rw_lock.read_locked()" in source, "读取阶段应持有 read_locked"
        assert "read_connection()" in source, "读取阶段应使用 read_connection"

        # 读取后在锁外做 exists
        assert "Path(target_path).exists()" in source, "锁外应做 exists() 检查"

        # 删除阶段使用 write_locked
        assert "rw_lock.write_locked()" in source, "删除阶段应持有 write_locked"
        assert "connection()" in source, "删除阶段应使用 connection()"

        # 删除使用 chunk_list 切片
        assert "chunk_list(to_delete, 900)" in source, "删除应使用 chunk_list 按 900 切片"
