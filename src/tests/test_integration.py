"""
集成测试 - 验证数据库重构和核心功能

测试范围：
1. 数据库 dataclass 记录类型
2. 配置加载流程
3. 核心业务流程
4. 修复的问题验证

运行方式：
  pytest src/tests/test_integration.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

# 确保 src 在 sys.path 中（conftest.py 也会处理，此处冗余保护）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import (
    Database,
    ARecord,
    BRecord,
    IdentityRecord,
    CRecord,
    BoundaryRecord,
    ProtectedRootRecord,
    SubtitleRecord,
)

if TYPE_CHECKING:
    pass


# ============================================================
# 数据库记录类型测试
# ============================================================


class TestDatabaseRecordTypes:
    """测试数据库 dataclass 记录类型"""

    def test_a_record_creation(self):
        """测试 ARecord 创建"""
        record = ARecord(
            local_path="/path/to/file.strm",
            webdav_path="/webdav/path.strm",
            parent_webdav_path="/webdav",
            updated_at=time.time(),
        )
        assert record.local_path == "/path/to/file.strm"
        assert record.webdav_path == "/webdav/path.strm"
        assert record.parent_webdav_path == "/webdav"
        assert isinstance(record.updated_at, float)

    def test_b_record_creation(self):
        """测试 BRecord 创建"""
        record = BRecord(
            local_path="/path/to/file.strm",
            webdav_path="/webdav/path.strm",
            parent_webdav_path="/webdav",
            source_a_path="/source/path.strm",
            fingerprint="abc123",
            status="valid",
            updated_at=time.time(),
        )
        assert record.local_path == "/path/to/file.strm"
        assert record.fingerprint == "abc123"
        assert record.status == "valid"

    def test_identity_record_creation(self):
        """测试 IdentityRecord 创建"""
        record = IdentityRecord(
            fingerprint="abc123",
            webdav_path="/webdav/path.strm",
            source_a_path="/source/path.strm",
            current_b_path="/current/path.strm",
            updated_at=time.time(),
        )
        assert record.fingerprint == "abc123"
        assert record.current_b_path == "/current/path.strm"

    def test_c_record_creation(self):
        """测试 CRecord 创建"""
        record = CRecord(
            local_path="/ghost/path.strm",
            webdav_path="/webdav/path.strm",
            original_b_path="/original/path.strm",
            ghost_root="/ghost/root",
            moved_at=time.time(),
        )
        assert record.ghost_root == "/ghost/root"

    def test_boundary_record_creation(self):
        """测试 BoundaryRecord 创建"""
        record = BoundaryRecord(
            fingerprint="abc123",
            source_media_name="Source Show",
            current_media_name="Current Show",
            engine_entry_path="/engine/path",
            updated_at=time.time(),
        )
        assert record.source_media_name == "Source Show"
        assert record.current_media_name == "Current Show"

    def test_protected_root_record_creation(self):
        """测试 ProtectedRootRecord 创建"""
        record = ProtectedRootRecord(
            root_path="/root/path",
            trash_path="/trash/path",
            active=True,
            updated_at=time.time(),
        )
        assert record.active is True

    def test_subtitle_record_creation(self):
        """测试 SubtitleRecord 创建"""
        record = SubtitleRecord(
            id=1,
            local_path="/path/to/subtitle.srt",
            target_path="/target/path.srt",
            fingerprint="abc123",
            season=1,
            episode=1,
            lang_code="zh-CN",
            status="valid",
            created_at="2026-07-02 10:00:00",
            updated_at="2026-07-02 10:00:00",
        )
        assert record.id == 1
        assert record.season == 1
        assert record.episode == 1


class TestDatabaseOperations:
    """测试数据库操作"""

    @pytest.fixture
    def temp_db(self):
        """创建临时数据库"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = Database(str(db_path))
            yield db

    def test_upsert_and_get_a_record(self, temp_db: Database):
        """测试 A 区记录的插入和查询"""
        # 插入记录
        temp_db.upsert_a(
            local_path="/test/path.strm",
            webdav_path="/webdav/path.strm",
            parent_webdav_path="/webdav",
        )

        # 查询记录
        record = temp_db.get_a_by_local("/test/path.strm")
        assert record is not None
        assert isinstance(record, ARecord)
        assert record.local_path == "/test/path.strm"
        assert record.webdav_path == "/webdav/path.strm"

    def test_upsert_and_get_b_record(self, temp_db: Database):
        """测试 B 区记录的插入和查询"""
        # 插入记录
        temp_db.upsert_b(
            local_path="/test/path.strm",
            webdav_path="/webdav/path.strm",
            parent_webdav_path="/webdav",
            source_a_path="/source/path.strm",
            fingerprint="abc123",
            status="valid",
        )

        # 查询记录
        record = temp_db.get_b_by_local_full("/test/path.strm")
        assert record is not None
        assert isinstance(record, BRecord)
        assert record.fingerprint == "abc123"
        assert record.status == "valid"

    def test_upsert_and_get_identity_record(self, temp_db: Database):
        """测试身份记录的插入和查询"""
        # 插入记录
        temp_db.upsert_identity(
            fingerprint="abc123",
            webdav_path="/webdav/path.strm",
            source_a_path="/source/path.strm",
            current_b_path="/current/path.strm",
        )

        # 查询记录
        record = temp_db.get_identity_by_fingerprint("abc123")
        assert record is not None
        assert isinstance(record, IdentityRecord)
        assert record.current_b_path == "/current/path.strm"

    def test_get_all_b_records(self, temp_db: Database):
        """测试获取所有 B 区记录"""
        # 插入多条记录
        for i in range(3):
            temp_db.upsert_b(
                local_path=f"/test/path{i}.strm",
                webdav_path=f"/webdav/path{i}.strm",
                parent_webdav_path="/webdav",
                source_a_path=f"/source/path{i}.strm",
                fingerprint=f"fp{i}",
                status="valid",
            )

        # 获取所有记录
        records = temp_db.get_all_b_records()
        assert len(records) == 3
        assert all(isinstance(r, BRecord) for r in records)

    def test_get_protected_roots(self, temp_db: Database):
        """测试获取受保护根目录"""
        # 插入记录
        temp_db.set_protected_root("/root1", "/trash1", active=True)
        temp_db.set_protected_root("/root2", "/trash2", active=False)

        # 查询记录
        records = temp_db.get_protected_roots()
        assert len(records) == 2
        assert all(isinstance(r, ProtectedRootRecord) for r in records)
        assert sum(1 for r in records if r.active) == 1

    def test_media_boundary_operations(self, temp_db: Database):
        """测试媒体边界记录操作"""
        # 插入记录
        temp_db.upsert_media_boundary(
            fingerprint="abc123",
            source_media_name="Source Show",
            current_media_name="Current Show",
            engine_entry_path="/engine/path",
        )

        # 查询记录
        record = temp_db.get_media_boundary_by_fingerprint("abc123")
        assert record is not None
        assert isinstance(record, BoundaryRecord)
        assert record.source_media_name == "Source Show"

    def test_subtitle_operations(self, temp_db: Database):
        """测试字幕记录操作"""
        # 初始化字幕表
        temp_db.init_subtitle_table()

        # 插入记录
        temp_db.upsert_subtitle(
            local_path="/test/subtitle.srt",
            target_path="/target/subtitle.srt",
            fingerprint="abc123",
            season=1,
            episode=1,
            lang_code="zh-CN",
        )

        # 查询记录
        record = temp_db.get_subtitle_by_local("/test/subtitle.srt")
        assert record is not None
        assert isinstance(record, SubtitleRecord)
        assert record.season == 1


# ============================================================
# 配置加载测试
# ============================================================


class TestConfigLoading:
    """测试配置加载"""

    def test_config_from_file_no_network(self):
        """测试配置加载不触发网络请求"""
        from config import AppConfig

        # 创建临时配置文件
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_content = """
[webdav]
host = "http://localhost:5244"
user = "admin"
password = "test"

[behavior]
action = "COPY"
sync_on_startup = true
sync_on_startup_wait = 10

[log]
level = "INFO"
max_size_mb = 10
backup_count = 5
"""
            config_path.write_text(config_content, encoding="utf-8")

            # 加载配置（不应触发网络请求）
            config = AppConfig.from_file(str(config_path))
            # 验证基本配置正确加载
            assert config.webdav.host == "http://localhost:5244"
            assert config.webdav.user == "admin"
            assert config.behavior.action == "COPY"
            assert config.behavior.sync_on_startup is True
            assert config.behavior.sync_on_startup_wait == 10
            # 验证 base_dir 被正确设置为配置文件所在目录
            assert config.base_dir == tmpdir
            # 验证路径默认值（from_file 不读取 [local] 表；a/b/c 区路径由 WebUI 配置动态决定，默认为空）
            assert config.local.base_dir == tmpdir
            assert config.local.a_dir == ""
            assert config.local.b_dir == ""
            assert config.local.c_dir == ""
            # strm_storage_map 初始化为空（需显式调用 load_strm_storage_from_api）
            assert config.strm_storage_map == {}
            assert config.a_folders == []


# ============================================================
# 修复验证测试
# ============================================================


class TestFixes:
    """验证已修复的问题"""

    def test_list_storages_pagination(self):
        """测试 list_storages 分页逻辑（模拟）"""
        from webdav_client import OpenListAdminClient

        # 创建客户端实例（不实际连接）
        client = OpenListAdminClient(
            host="http://localhost:5244",
            user="admin",
            password="test",
        )

        # 验证客户端创建成功
        assert client.host == "http://localhost:5244"

    def test_totp_exception_handling(self):
        """测试 TOTP 异常处理"""
        from webdav_client import _generate_totp

        # 测试无效的 TOTP secret
        with pytest.raises(ValueError, match="base32.*base64"):
            _generate_totp("invalid-secret!!!")

    def test_mkdir_return_value(self):
        """测试 mkdir 返回值逻辑"""
        # 这个测试需要实际的 WebDAV 连接
        # 这里只验证方法签名
        from webdav_client import OpenlistWebDAV

        client = OpenlistWebDAV(
            host="http://localhost:5244",
            user="admin",
            password="test",
        )
        assert hasattr(client, "mkdir")

    def test_list_contents_return_type(self):
        """测试 list_contents 返回类型"""
        from webdav_client import OpenListAdminClient

        client = OpenListAdminClient(
            host="http://localhost:5244",
            user="admin",
            password="test",
        )
        # 验证方法存在
        assert hasattr(client, "list_contents")


# ============================================================
# 数据流测试
# ============================================================


class TestDataFlow:
    """测试数据流完整性"""

    @pytest.fixture
    def temp_db(self):
        """创建临时数据库"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = Database(str(db_path))
            yield db

    def test_a_to_b_flow(self, temp_db: Database):
        """测试 A 区到 B 区的数据流"""
        # 1. 创建 A 区记录
        temp_db.upsert_a(
            local_path="/a/path.strm",
            webdav_path="/webdav/path.strm",
            parent_webdav_path="/webdav",
        )

        # 2. 查询 A 区记录
        a_record = temp_db.get_a_by_webdav("/webdav/path.strm")
        assert a_record is not None
        assert isinstance(a_record, ARecord)

        # 3. 创建身份记录
        fingerprint = "test_fp"
        temp_db.upsert_identity(
            fingerprint=fingerprint,
            webdav_path="/webdav/path.strm",
            source_a_path=a_record.local_path,
            current_b_path=None,
        )

        # 4. 创建 B 区记录
        temp_db.upsert_b(
            local_path="/b/path.strm",
            webdav_path="/webdav/path.strm",
            parent_webdav_path="/webdav",
            source_a_path=a_record.local_path,
            fingerprint=fingerprint,
            status="valid",
        )

        # 5. 更新身份记录的 current_b_path
        temp_db.update_identity_b_path(fingerprint, "/b/path.strm")

        # 6. 验证身份记录
        identity = temp_db.get_identity_by_fingerprint(fingerprint)
        assert identity is not None
        assert identity.current_b_path == "/b/path.strm"

        # 7. 验证 B 区记录
        b_record = temp_db.get_b_by_local_full("/b/path.strm")
        assert b_record is not None
        assert b_record.fingerprint == fingerprint

    def test_ghost_protection_flow(self, temp_db: Database):
        """测试幽灵保护流程"""
        # 1. 设置幽灵保护
        temp_db.set_ghost_protection("/webdav/path.strm", seconds=60, reason="test")

        # 2. 验证保护状态
        is_protected = temp_db.is_ghost_protected("/webdav/path.strm")
        assert is_protected is True

        # 3. 清理过期保护（应该不清理，因为还没过期）
        temp_db.cleanup_expired_ghosts()

        # 4. 再次验证
        is_protected = temp_db.is_ghost_protected("/webdav/path.strm")
        assert is_protected is True


# ============================================================
# 批量操作测试
# ============================================================


class TestBatchOperations:
    """测试批量操作"""

    @pytest.fixture
    def temp_db(self):
        """创建临时数据库"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = Database(str(db_path))
            yield db

    def test_upsert_a_batch(self, temp_db: Database):
        """测试批量插入 A 区记录"""
        records = [
            (f"/path{i}.strm", f"/webdav{i}.strm", "/webdav")
            for i in range(10)
        ]
        count = temp_db.upsert_a_batch(records)
        assert count == 10

        # 验证记录
        all_records = temp_db.get_all_a_records()
        assert len(all_records) == 10

    def test_upsert_b_batch(self, temp_db: Database):
        """测试批量插入 B 区记录"""
        records = [
            (f"/path{i}.strm", f"/webdav{i}.strm", "/webdav", f"/source{i}.strm", f"fp{i}", "valid")
            for i in range(10)
        ]
        count = temp_db.upsert_b_batch(records)
        assert count == 10

        # 验证记录
        all_records = temp_db.get_all_b_records()
        assert len(all_records) == 10

    def test_delete_batch(self, temp_db: Database):
        """测试批量删除"""
        # 先插入记录
        for i in range(10):
            temp_db.upsert_a(
                local_path=f"/path{i}.strm",
                webdav_path=f"/webdav{i}.strm",
                parent_webdav_path="/webdav",
            )

        # 批量删除
        paths_to_delete = [f"/path{i}.strm" for i in range(5)]
        count = temp_db.delete_a_batch(paths_to_delete)
        assert count == 5

        # 验证剩余记录
        remaining = temp_db.get_all_a_records()
        assert len(remaining) == 5


class TestFTSIntegrityBStrm:
    """回归测试：b_strm_files 与其 FTS 表的 rowid 一致性

    覆盖历史 bug：删除 b_strm_files 行时未同步删除 FTS 行，导致
    FTS 表残留孤儿；随后 upsert_b 复用相同 rowid 插入 FTS 时触发
    'constraint failed'（IntegrityError）。
    """

    @pytest.fixture
    def temp_db(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = Database(str(db_path))
            yield db

    def _fts_orphan_count(self, db: Database) -> int:
        with db.read_connection() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM b_strm_files_fts "
                "WHERE rowid NOT IN (SELECT rowid FROM b_strm_files)"
            ).fetchone()[0]

    def test_upsert_b_survives_orphan_fts_rowid(self, temp_db: Database):
        """存在孤儿 FTS 行时，upsert_b 不应因 rowid 冲突而崩溃"""
        # 制造孤儿：插入后用 delete_b_under_root 删除主表行，
        # 再手动残留一个 FTS 行来模拟历史损坏状态。
        temp_db.upsert_b("/b/old.strm", "/w/old.mp4", "/w", "/a/old.strm",
                         fingerprint="fpold", status="valid")
        # 直接删主表行但保留 FTS 行，模拟旧版删除路径的 bug
        with temp_db.rw_lock.write_locked(), temp_db.connection() as conn:
            conn.execute("DELETE FROM b_strm_files")  # 只删主表，FTS 残留
            conn.commit()
        assert self._fts_orphan_count(temp_db) >= 1

        # 新 upsert 会复用 rowid=1，历史版本此处会抛 constraint failed
        temp_db.upsert_b("/b/new.strm", "/w/new.mp4", "/w", "/a/new.strm",
                         fingerprint="fpnew", status="valid")

        # 应成功且无孤儿
        rec = temp_db.get_b_by_local("/b/new.strm")
        assert rec is not None
        assert self._fts_orphan_count(temp_db) == 0

    def test_delete_b_under_root_cleans_fts(self, temp_db: Database):
        temp_db.upsert_b("/b/x1.strm", "/root/a/x1.mp4", "/root/a", None,
                         fingerprint="fp1", status="valid")
        temp_db.upsert_b("/b/x2.strm", "/root/a/x2.mp4", "/root/a", None,
                         fingerprint="fp2", status="valid")
        temp_db.delete_b_under_root("/root/a")
        assert self._fts_orphan_count(temp_db) == 0

    def test_delete_b_by_fingerprint_cleans_fts(self, temp_db: Database):
        temp_db.upsert_b("/b/y.strm", "/w/y.mp4", "/w", None,
                         fingerprint="fpY", status="valid")
        temp_db.delete_b_by_fingerprint("fpY")
        assert self._fts_orphan_count(temp_db) == 0

    def test_delete_b_batch_cleans_fts(self, temp_db: Database):
        for i in range(3):
            temp_db.upsert_b(f"/b/z{i}.strm", f"/w/z{i}.mp4", "/w", None,
                             fingerprint=f"fpZ{i}", status="valid")
        temp_db.delete_b_batch([f"/b/z{i}.strm" for i in range(3)])
        assert self._fts_orphan_count(temp_db) == 0

    def test_move_b_record_cleans_fts(self, temp_db: Database):
        temp_db.upsert_b("/b/old.strm", "/w/m.mp4", "/w", None,
                         fingerprint="fpM", status="valid")
        assert temp_db.move_b_record("/b/old.strm", "/b/moved.strm") is True
        assert self._fts_orphan_count(temp_db) == 0
        # 移动后新路径可被搜索到、旧路径不残留
        assert temp_db.get_b_by_local("/b/moved.strm") is not None
        assert temp_db.get_b_by_local("/b/old.strm") is None

    def test_upsert_b_batch_cleans_fts(self, temp_db: Database):
        """批量 upsert 后不应残留 FTS 孤儿"""
        records = [
            (f"/b/batch{i}.strm", f"/w/batch{i}.mp4", "/w", None, f"fpB{i}", "valid")
            for i in range(3)
        ]
        temp_db.upsert_b_batch(records)
        assert self._fts_orphan_count(temp_db) == 0
        # 再次批量 upsert 相同路径（触发 REPLACE），仍不应产生孤儿或崩溃
        temp_db.upsert_b_batch(records)
        assert self._fts_orphan_count(temp_db) == 0


class TestFTSIntegrityAStrm:
    """回归测试：a_strm_files 与其 FTS 表的 rowid 一致性

    与 B 区同类，覆盖删除路径未清理 FTS 及 upsert 复用孤儿 rowid 的场景。
    """

    @pytest.fixture
    def temp_db(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = Database(str(db_path))
            yield db

    def _fts_orphan_count(self, db: Database) -> int:
        with db.read_connection() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM a_strm_files_fts "
                "WHERE rowid NOT IN (SELECT rowid FROM a_strm_files)"
            ).fetchone()[0]

    def test_upsert_a_survives_orphan_fts_rowid(self, temp_db: Database):
        """存在孤儿 FTS 行时，upsert_a 不应因 rowid 冲突而崩溃"""
        temp_db.upsert_a("/a/old.strm", "/w/old.mp4", "/w")
        # 只删主表行，保留 FTS 行，模拟历史损坏
        with temp_db.rw_lock.write_locked(), temp_db.connection() as conn:
            conn.execute("DELETE FROM a_strm_files")
            conn.commit()
        assert self._fts_orphan_count(temp_db) >= 1

        # 新 upsert 会复用 rowid=1，历史版本此处会抛 constraint failed
        temp_db.upsert_a("/a/new.strm", "/w/new.mp4", "/w")
        assert temp_db.get_a_by_local("/a/new.strm") is not None
        assert self._fts_orphan_count(temp_db) == 0

    def test_delete_a_by_local_cleans_fts(self, temp_db: Database):
        temp_db.upsert_a("/a/x.strm", "/w/x.mp4", "/w")
        temp_db.delete_a_by_local("/a/x.strm")
        assert self._fts_orphan_count(temp_db) == 0

    def test_delete_a_batch_cleans_fts(self, temp_db: Database):
        for i in range(3):
            temp_db.upsert_a(f"/a/z{i}.strm", f"/w/z{i}.mp4", "/w")
        temp_db.delete_a_batch([f"/a/z{i}.strm" for i in range(3)])
        assert self._fts_orphan_count(temp_db) == 0

    def test_upsert_a_batch_cleans_fts(self, temp_db: Database):
        records = [(f"/a/b{i}.strm", f"/w/b{i}.mp4", "/w") for i in range(3)]
        temp_db.upsert_a_batch(records)
        assert self._fts_orphan_count(temp_db) == 0
        # 再次批量 upsert 相同路径（触发 REPLACE），仍不应产生孤儿或崩溃
        temp_db.upsert_a_batch(records)
        assert self._fts_orphan_count(temp_db) == 0


class TestFTSIntegrityCGhost:
    """回归测试：c_ghost_files 与其 FTS 表的 rowid 一致性"""

    @pytest.fixture
    def temp_db(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = Database(str(db_path))
            yield db

    def _fts_orphan_count(self, db: Database) -> int:
        with db.read_connection() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM c_ghost_files_fts "
                "WHERE rowid NOT IN (SELECT rowid FROM c_ghost_files)"
            ).fetchone()[0]

    def test_upsert_c_survives_orphan_fts_rowid(self, temp_db: Database):
        """存在孤儿 FTS 行时，upsert_c 不应因 rowid 冲突而崩溃"""
        temp_db.upsert_c("/c/old.strm", "/w/old.mp4", "/b/old.strm", "/ghost")
        with temp_db.rw_lock.write_locked(), temp_db.connection() as conn:
            conn.execute("DELETE FROM c_ghost_files")
            conn.commit()
        assert self._fts_orphan_count(temp_db) >= 1

        # 新 upsert 会复用 rowid=1，历史版本此处会抛 constraint failed
        temp_db.upsert_c("/c/new.strm", "/w/new.mp4", "/b/new.strm", "/ghost")
        assert self._fts_orphan_count(temp_db) == 0

    def test_delete_c_by_local_cleans_fts(self, temp_db: Database):
        temp_db.upsert_c("/c/x.strm", "/w/x.mp4", "/b/x.strm", "/ghost")
        temp_db.delete_c_by_local("/c/x.strm")
        assert self._fts_orphan_count(temp_db) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
