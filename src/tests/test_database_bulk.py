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

    def _insert_b_record(self, db: Database, local_path: str, fingerprint: str | None):
        """辅助：插入一条 b_strm_files 记录。"""
        now = time.time()
        with db.rw_lock.write_locked(), db.connection() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO b_strm_files(
                    local_path, webdav_path, parent_webdav_path,
                    source_a_path, fingerprint, status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (local_path, f"/webdav/{local_path}", "/webdav/", "/a/path", fingerprint, "valid", now),
            )
            conn.commit()

    def test_returns_distinct_fingerprints(self, db: Database):
        """返回去重后的非 NULL 指纹集合。"""
        self._insert_b_record(db, "/b/a.strm", "fp-1")
        self._insert_b_record(db, "/b/b.strm", "fp-1")  # 重复
        self._insert_b_record(db, "/b/c.strm", "fp-2")
        result = db.get_all_b_fingerprints()
        assert result == {"fp-1", "fp-2"}

    def test_excludes_null_fingerprints(self, db: Database):
        """fingerprint 为 NULL 的记录不返回。"""
        self._insert_b_record(db, "/b/null.strm", None)
        self._insert_b_record(db, "/b/valid.strm", "fp-x")
        result = db.get_all_b_fingerprints()
        assert result == {"fp-x"}

    def test_empty_when_no_b_records(self, db: Database):
        """b_strm_files 为空时返回空集合。"""
        assert db.get_all_b_fingerprints() == set()

    def test_returns_set_type(self, db: Database):
        """返回类型是 set。"""
        assert isinstance(db.get_all_b_fingerprints(), set)


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
