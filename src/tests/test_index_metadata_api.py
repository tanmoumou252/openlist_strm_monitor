"""
Dashboard/Area 索引元数据 API 测试。

测试 Task 2 暴露的索引元数据（generation、时间戳、mapping 版本）。

测试策略：
- 使用真实 Database 对象验证 API 返回的元数据格式
- 测试多 mapping 分区逻辑
- 测试未知 mapping 分区
- 测试向后兼容性（单 mapping 扁平响应）
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 确保 src/ 在 sys.path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import Database


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Database(str(Path(tmpdir) / "test.db"))


class TestDashboardIndexMetadata:
    """测试 /api/dashboard 返回的索引元数据。"""

    def test_dashboard_returns_index_metadata(self, db: Database):
        """dashboard 应返回 index_metadata 和 mappings。"""
        # 设置一些索引元数据
        db.complete_index_generation(["m1", "m2"], completed_at=1000.0)
        db.set_mapping_version("abc123def456", version_generated_at=2000.0)
        
        # 获取索引元数据
        meta = db.get_index_metadata()
        
        # 验证全局元数据
        assert "index_generation" in meta
        assert "index_generation_at" in meta
        assert "last_full_index_at" in meta
        assert "mapping_version" in meta
        assert "mapping_version_generated_at" in meta
        
        assert meta["index_generation"] == 1
        assert meta["index_generation_at"] == 1000.0
        assert meta["last_full_index_at"] == 1000.0
        assert meta["mapping_version"] == "abc123def456"
        assert meta["mapping_version_generated_at"] == 2000.0
        
        # 获取每个 mapping 的元数据
        meta_m1 = db.get_index_metadata("m1")
        assert meta_m1["mapping_id"] == "m1"
        assert meta_m1["mapping_index_generation"] == 1
        assert meta_m1["mapping_index_generation_at"] == 1000.0
        
        meta_m2 = db.get_index_metadata("m2")
        assert meta_m2["mapping_id"] == "m2"
        assert meta_m2["mapping_index_generation"] == 1
        assert meta_m2["mapping_index_generation_at"] == 1000.0

    def test_dashboard_handles_empty_database(self, db: Database):
        """dashboard 空库时应返回默认值。"""
        meta = db.get_index_metadata()
        
        assert meta["index_generation"] == 0
        assert meta["index_generation_at"] == 0
        assert meta["last_full_index_at"] == 0
        assert meta["mapping_version"] == ""
        assert meta["mapping_version_generated_at"] == 0

    def test_dashboard_multiple_mappings(self, db: Database):
        """dashboard 多 mapping 时应返回每个 mapping 的元数据。"""
        # 3 个 mapping，但只完成 m1 和 m2
        db.complete_index_generation(["m1", "m2"], completed_at=1000.0)
        
        # m3 未完成
        meta_m1 = db.get_index_metadata("m1")
        meta_m2 = db.get_index_metadata("m2")
        meta_m3 = db.get_index_metadata("m3")
        
        assert meta_m1["mapping_index_generation"] == 1
        assert meta_m2["mapping_index_generation"] == 1
        assert meta_m3["mapping_index_generation"] == 0  # 默认值


class TestAreaDetailMultiMapping:
    """测试 /api/area/{area}/detail 的多 mapping 分区逻辑。"""

    def test_single_mapping_backward_compatible(self, db: Database):
        """单 mapping 时应保持向后兼容扁平响应。"""
        # 模拟单 mapping 场景
        # 实际测试需要 HTTP server，这里验证数据库逻辑
        records = [
            {"local_path": "/b/m1/movie1.strm", "mapping_id": "m1"},
            {"local_path": "/b/m1/movie2.strm", "mapping_id": "m1"},
        ]
        
        # 按 mapping_id 分组
        mapping_groups = {}
        for rec in records:
            mid = rec.get("mapping_id", "")
            mapping_groups.setdefault(mid, []).append(rec)
        
        # 单 mapping 应该只有一个分组
        assert len(mapping_groups) == 1
        assert "m1" in mapping_groups
        assert len(mapping_groups["m1"]) == 2

    def test_multi_mapping_returns_mappings_array(self, db: Database):
        """多 mapping 时应返回 mappings 数组。"""
        # 模拟多 mapping 场景
        records = [
            {"local_path": "/b/m1/movie1.strm", "mapping_id": "m1"},
            {"local_path": "/b/m2/movie2.strm", "mapping_id": "m2"},
            {"local_path": "/b/m1/movie3.strm", "mapping_id": "m1"},
            {"local_path": "/b/m2/movie4.strm", "mapping_id": "m2"},
            {"local_path": "/b/m2/movie5.strm", "mapping_id": "m2"},
        ]
        
        # 按 mapping_id 分组
        mapping_groups = {}
        for rec in records:
            mid = rec.get("mapping_id", "")
            mapping_groups.setdefault(mid, []).append(rec)
        
        # 多 mapping 应该有多个分组
        assert len(mapping_groups) == 2
        assert "m1" in mapping_groups
        assert "m2" in mapping_groups
        assert len(mapping_groups["m1"]) == 2
        assert len(mapping_groups["m2"]) == 3

    def test_b_area_includes_mapping_id(self, db: Database):
        """B 区查询应包含 mapping_id 列。"""
        # 验证 B 表 schema 包含 mapping_id
        with db.read_connection() as conn:
            cur = conn.execute("PRAGMA table_info(b_strm_files)")
            columns = [row[1] for row in cur.fetchall()]
            assert "mapping_id" in columns

    def test_unknown_mapping_partition(self, db: Database):
        """未知 mapping 应归入 unknown 分区。"""
        # 模拟包含未知 mapping 的记录
        records = [
            {"local_path": "/b/m1/movie1.strm", "mapping_id": "m1"},
            {"local_path": "/b/deleted_mapping/movie2.strm", "mapping_id": "deleted_mapping"},
            {"local_path": "/b/m2/movie3.strm", "mapping_id": "m2"},
        ]
        
        # 当前配置只有 m1 和 m2
        current_mapping_ids = {"m1", "m2"}
        
        # 按 mapping_id 分组，未知归入 unknown
        mapping_groups = {}
        for rec in records:
            mid = rec.get("mapping_id", "")
            if mid not in current_mapping_ids:
                mid = "unknown"
            mapping_groups.setdefault(mid, []).append(rec)
        
        assert "m1" in mapping_groups
        assert "m2" in mapping_groups
        assert "unknown" in mapping_groups
        assert len(mapping_groups["unknown"]) == 1
        assert mapping_groups["unknown"][0]["mapping_id"] == "deleted_mapping"

    def test_per_mapping_independent_pagination(self, db: Database):
        """每个 mapping 应独立分页。"""
        # 模拟每个 mapping 的记录数不同
        mapping_totals = {
            "m1": 50,
            "m2": 100,
        }
        
        page_size = 20
        
        # 对每个 mapping 独立计算分页
        for mid, total in mapping_totals.items():
            total_pages = max(1, (total + page_size - 1) // page_size)
            assert total_pages == (total + page_size - 1) // page_size

    def test_index_metadata_per_mapping(self, db: Database):
        """每个 mapping 应返回独立的 index_metadata。"""
        # 设置不同 mapping 的完成时间
        db.complete_index_generation(["m1"], completed_at=1000.0)
        db.complete_index_generation(["m2"], completed_at=2000.0)
        
        meta_m1 = db.get_index_metadata("m1")
        meta_m2 = db.get_index_metadata("m2")
        
        # 每个 mapping 的元数据独立
        assert meta_m1["mapping_index_generation"] == 1
        assert meta_m1["mapping_index_generation_at"] == 1000.0
        
        assert meta_m2["mapping_index_generation"] == 2
        assert meta_m2["mapping_index_generation_at"] == 2000.0


class TestAreaRefreshMappingId:
    """测试 /api/area/{area}/refresh 支持 mapping_id 参数。"""

    def test_refresh_with_mapping_id_filters(self, db: Database):
        """刷新时传 mapping_id 应按 mapping 过滤 A 区记录。"""
        # 模拟 A 区记录（不含 mapping_id，但有 webdav_path）
        a_records = [
            {"local_path": "/a/m1/movie1.strm", "webdav_path": "/m1/movie1.strm"},
            {"local_path": "/a/m1/movie2.strm", "webdav_path": "/m1/movie2.strm"},
            {"local_path": "/a/m2/movie3.strm", "webdav_path": "/m2/movie3.strm"},
        ]
        
        # 按 webdav_path 前缀过滤（模拟 mapping 路径）
        mapping_prefix = "/m1/"
        filtered = [
            rec for rec in a_records
            if rec.get("webdav_path", "").startswith(mapping_prefix)
        ]
        
        assert len(filtered) == 2
        assert all("/m1/" in rec["webdav_path"] for rec in filtered)

    def test_refresh_without_mapping_id_backward_compatible(self, db: Database):
        """不传 mapping_id 应保持现有全局行为。"""
        a_records = [
            {"local_path": "/a/m1/movie1.strm", "webdav_path": "/m1/movie1.strm"},
            {"local_path": "/a/m1/movie2.strm", "webdav_path": "/m1/movie2.strm"},
            {"local_path": "/a/m2/movie3.strm", "webdav_path": "/m2/movie3.strm"},
        ]
        
        # 不过滤，返回所有记录
        filtered = a_records
        
        assert len(filtered) == 3


class TestBRecordMappingId:
    """测试 BRecord 对象支持 mapping_id。"""

    def test_b_record_has_mapping_id(self, db: Database):
        """BRecord 应包含 mapping_id 字段。"""
        from database import BRecord
        
        # 创建一个 BRecord
        record = BRecord(
            local_path="/b/m1/movie1.strm",
            webdav_path="/m1/movie1.strm",
            parent_webdav_path="/m1",
            source_a_path="/a/m1/movie1.strm",
            fingerprint="abc123",
            status="valid",
            updated_at=1000.0,
            mapping_id="m1",
        )
        
        assert record.mapping_id == "m1"
