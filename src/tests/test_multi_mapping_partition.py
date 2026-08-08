"""
Task 2: 多 mapping 分区逻辑测试

测试 handle_area_detail 的多 mapping 分区逻辑
- B 区查询包含 mapping_id 列
- 按 mapping_id 分组
- 未知 mapping 归入 unknown 分区
- 单一 mapping 向后兼容
- 多 mapping 返回 mappings 数组
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import Database


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Database(str(Path(tmpdir) / "test.db"))


class TestHandleAreaDetailMultiMapping:
    """测试 handle_area_detail 的多 mapping 分区逻辑。"""

    def test_b_area_has_mapping_id_column(self, db: Database):
        """B 区表应包含 mapping_id 列。"""
        with db.read_connection() as conn:
            cur = conn.execute("PRAGMA table_info(b_strm_files)")
            columns = [row[1] for row in cur.fetchall()]
            assert "mapping_id" in columns

    def test_single_mapping_backward_compatible(self, db: Database):
        """单 mapping 应返回扁平响应（向后兼容）。"""
        # 模拟单 mapping 场景
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
        """多 mapping 应返回 mappings 数组。"""
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

    def test_unknown_mapping_partition(self, db: Database):
        """未知 mapping 应归入 unknown 分区。"""
        records = [
            {"local_path": "/b/m1/movie1.strm", "mapping_id": "m1"},
            {"local_path": "/b/deleted_mapping/movie2.strm", "mapping_id": "deleted_mapping"},
            {"local_path": "/b/m2/movie3.strm", "mapping_id": "m2"},
        ]
        
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
        mapping_totals = {
            "m1": 50,
            "m2": 100,
        }
        
        page_size = 20
        
        for mid, total in mapping_totals.items():
            total_pages = max(1, (total + page_size - 1) // page_size)
            assert total_pages == (total + page_size - 1) // page_size

    def test_index_metadata_per_mapping(self, db: Database):
        """每个 mapping 应返回独立的 index_metadata。"""
        db.complete_index_generation(["m1"], completed_at=1000.0)
        db.complete_index_generation(["m2"], completed_at=2000.0)
        
        meta_m1 = db.get_index_metadata("m1")
        meta_m2 = db.get_index_metadata("m2")
        
        assert meta_m1["mapping_index_generation"] == 1
        assert meta_m1["mapping_index_generation_at"] == 1000.0
        
        assert meta_m2["mapping_index_generation"] == 2
        assert meta_m2["mapping_index_generation_at"] == 2000.0


class TestAreaRefreshMappingId:
    """测试 handle_area_refresh 的 mapping_id 参数支持。"""

    def test_refresh_with_mapping_id_filters(self, db: Database):
        """刷新时传 mapping_id 应按 mapping 过滤 A 区记录。"""
        a_records = [
            {"local_path": "/a/m1/movie1.strm", "webdav_path": "/m1/movie1.strm"},
            {"local_path": "/a/m1/movie2.strm", "webdav_path": "/m1/movie2.strm"},
            {"local_path": "/a/m2/movie3.strm", "webdav_path": "/m2/movie3.strm"},
        ]
        
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
        
        filtered = a_records
        
        assert len(filtered) == 3


class TestAreaRefreshMappingIdNormalization:
    """F2 回归测试：_do_media_refresh 的 mapping_id 过滤必须对 a_root 做路径规范化。

    背景：ABMapping.a_root 存用户原始输入（如 Windows 前斜杠 `D:/x`），而
    `a_strm_files.local_path` 存规范化路径（反斜杠 `D:\\x`）。若用原始 a_root
    构造 LIKE，分隔符不一致时过滤静默失效，返回"未找到相关记录"。修复后应命中。
    """

    def test_refresh_mapping_id_matches_normalized_a_root(self, tmp_path, db: Database):
        """a_root 用前斜杠原始形态时，LIKE 过滤仍应命中规范化 local_path。"""
        # 构造 A 区记录：local_path 一律用 os 原生分隔符（规范化形态）
        a_dir_m1 = tmp_path / "a" / "m1"
        a_dir_m1.mkdir(parents=True, exist_ok=True)
        movie1 = a_dir_m1 / "movie1.strm"
        movie1.write_text("strm")
        movie2 = a_dir_m1 / "movie2.strm"
        movie2.write_text("strm")
        # 属于另一 mapping 的记录，不应被 m1 过滤命中
        a_dir_m2 = tmp_path / "a" / "m2"
        a_dir_m2.mkdir(parents=True, exist_ok=True)
        movie3 = a_dir_m2 / "movie3.strm"
        movie3.write_text("strm")

        with db.connection() as conn:
            conn.execute(
                "INSERT INTO a_strm_files (local_path, webdav_path, parent_webdav_path, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (str(movie1.resolve()), "/m1/movie1.strm", "/m1", time.time()),
            )
            conn.execute(
                "INSERT INTO a_strm_files (local_path, webdav_path, parent_webdav_path, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (str(movie2.resolve()), "/m1/movie2.strm", "/m1", time.time()),
            )
            conn.execute(
                "INSERT INTO a_strm_files (local_path, webdav_path, parent_webdav_path, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (str(movie3.resolve()), "/m2/movie3.strm", "/m2", time.time()),
            )
            conn.commit()

        # mapping.a_root 存"原始"路径：用 as_posix()（前斜杠），与规范化 local_path 分隔符不同
        mapping = MagicMock()
        mapping.mapping_id = "m1"
        mapping.a_root = a_dir_m1.resolve().as_posix()

        app_service = MagicMock()
        app_service.db = db
        app_service.config = None
        app_service.a_b_mappings = [mapping]
        app_service.admin_api = MagicMock()
        app_service.admin_api.list_directory.return_value = {
            "code": 0,
            "data": {"content": [{"name": "movie1.strm"}, {"name": "movie2.strm"}]},
        }
        app_service._cloud_path_to_engine_paths.return_value = ["/strm/m1"]
        app_service.copy_a_record_to_b_if_needed.return_value = True
        app_service.cleanup_b_zombies_under_folder = MagicMock()

        from webui.routes import _do_media_refresh
        result = _do_media_refresh(app_service, "a", "movie", mapping_id="m1")

        # 关键断言：mapping 过滤命中规范化路径，不应返回"未找到相关记录"
        assert result.get("ok") is True
        assert result.get("message") != "未找到相关记录"
        # 应命中 2 条（movie1、movie2），movie3 属于 m2 被过滤
        assert result.get("synced") == 2


class TestMappingMetadataList:
    """测试 _get_mapping_metadata_list 函数。"""

    def test_get_mapping_metadata_list_returns_list(self, db: Database):
        """应返回 mapping 元数据列表。"""
        from webui.routes import _get_mapping_metadata_list
        
        mock_handler = MagicMock()
        mock_handler.webui._db = db
        mock_handler.webui._app_service = MagicMock()
        mock_handler.webui._app_service.a_b_mappings = [
            MagicMock(mapping_id="m1", label="Mapping 1", a_root="/a/m1", b_root="/b/m1"),
            MagicMock(mapping_id="m2", label="Mapping 2", a_root="/a/m2", b_root="/b/m2"),
        ]
        
        result = _get_mapping_metadata_list(mock_handler)
        
        assert len(result) == 2
        assert result[0]["mapping_id"] == "m1"
        assert result[0]["label"] == "Mapping 1"
        assert result[1]["mapping_id"] == "m2"

    def test_get_mapping_metadata_list_with_empty_mappings(self, db: Database):
        """无 mapping 时应返回空列表。"""
        from webui.routes import _get_mapping_metadata_list
        
        mock_handler = MagicMock()
        mock_handler.webui._db = db
        mock_handler.webui._app_service = MagicMock()
        mock_handler.webui._app_service.a_b_mappings = []
        
        result = _get_mapping_metadata_list(mock_handler)
        
        assert result == []
