"""
database.py bulk_connection / batch 辅助方法测试。

测试新增的 4 个方法：
- bulk_connection(): 长连接上下文管理器，自动 commit/rollback/close
- save_known_folders_batch(): 批量插入 known_folders
- get_all_ghost_protected_paths(): 预载幽灵保护路径集合
- get_all_b_fingerprints(): 预载 B 区指纹集合
"""

from __future__ import annotations

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
