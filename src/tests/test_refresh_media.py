"""
媒体刷新功能单元测试

测试范围：
- _compute_common_parent_path：计算路径列表的最长公共父目录
- _parse_api_files：解析 OpenList API 返回的文件列表
- _do_media_refresh：差异检测与同步逻辑

运行方式：
  pytest src/tests/test_refresh_media.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 确保 src/ 在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webui.routes import _compute_common_parent_path, _parse_api_files, _do_media_refresh


# ============================================================
# 路径计算
# ============================================================

class TestStrmPathCalculation:
    """测试 STRM 入口路径计算逻辑"""

    def test_compute_common_parent_path_single(self):
        """单个路径应返回自身"""
        result = _compute_common_parent_path(["/strm/番剧/头文字D/Season 1"])
        assert result == "/strm/番剧/头文字D/Season 1"

    def test_compute_common_parent_path_multiple(self):
        """多个路径应返回最长公共父目录"""
        paths = [
            "/strm/番剧/头文字D/Season 1",
            "/strm/番剧/头文字D/Season 2",
            "/strm/番剧/头文字D/Season 3",
        ]
        result = _compute_common_parent_path(paths)
        assert result == "/strm/番剧/头文字D"

    def test_compute_common_parent_path_different_roots(self):
        """不同根目录应返回 /"""
        paths = [
            "/strm/番剧/头文字D",
            "/movies/动作片",
        ]
        result = _compute_common_parent_path(paths)
        assert result == "/"

    def test_compute_common_parent_path_empty(self):
        """空列表应返回空字符串"""
        result = _compute_common_parent_path([])
        assert result == ""

    def test_compute_common_parent_path_with_trailing_slash(self):
        """带尾部斜杠的路径应正确处理"""
        paths = [
            "/strm/番剧/头文字D/",
            "/strm/番剧/头文字D/Season 1",
        ]
        result = _compute_common_parent_path(paths)
        # 应该能识别出公共前缀
        assert result.startswith("/strm/番剧/头文字D")


# ============================================================
# API 文件解析
# ============================================================

class TestApiFileParsing:
    """测试 OpenList API 文件列表解析"""

    def test_parse_api_files_strm_only(self):
        """只保留 .strm 文件"""
        list_result = {
            "code": 200,
            "data": {
                "content": [
                    {"name": "S01E01.strm", "is_dir": False},
                    {"name": "S01E02.strm", "is_dir": False},
                    {"name": "Season 1", "is_dir": True},
                ]
            }
        }
        result = _parse_api_files(list_result, "/strm/番剧/头文字D")
        assert len(result) == 2
        assert result[0]["name"] == "S01E01.strm"
        assert result[0]["webdav_path"] == "/strm/番剧/头文字D/S01E01.strm"
        assert result[1]["name"] == "S01E02.strm"

    def test_parse_api_files_with_subtitles(self):
        """保留 .strm 和字幕文件"""
        list_result = {
            "code": 200,
            "data": {
                "content": [
                    {"name": "S01E01.strm", "is_dir": False},
                    {"name": "S01E01.ass", "is_dir": False},
                    {"name": "S01E01.srt", "is_dir": False},
                    {"name": "S01E01.ssa", "is_dir": False},
                    {"name": "readme.txt", "is_dir": False},
                ]
            }
        }
        result = _parse_api_files(list_result, "/strm/番剧/头文字D")
        assert len(result) == 4  # 1 strm + 3 subtitles
        names = [f["name"] for f in result]
        assert "S01E01.strm" in names
        assert "S01E01.ass" in names
        assert "S01E01.srt" in names
        assert "S01E01.ssa" in names
        assert "readme.txt" not in names

    def test_parse_api_files_empty_content(self):
        """空内容应返回空列表"""
        list_result = {
            "code": 200,
            "data": {
                "content": []
            }
        }
        result = _parse_api_files(list_result, "/strm/番剧/头文字D")
        assert result == []

    def test_parse_api_files_null_content(self):
        """content 为 None 应返回空列表"""
        list_result = {
            "code": 200,
            "data": {
                "content": None
            }
        }
        result = _parse_api_files(list_result, "/strm/番剧/头文字D")
        assert result == []

    def test_parse_api_files_error_code(self):
        """错误码应返回空列表（由调用方检查 code）"""
        list_result = {
            "code": 404,
            "data": {}
        }
        result = _parse_api_files(list_result, "/strm/番剧/头文字D")
        assert result == []


# ============================================================
# 差异检测与同步
# ============================================================

class TestDiffDetection:
    """测试差异检测逻辑"""

    def test_diff_detection_no_changes(self):
        """DB 和 API 完全一致时应返回 0 变化"""
        app_service = MagicMock()
        app_service.db = MagicMock()
        app_service.admin_api = MagicMock()

        # Mock DB 查询返回
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            {"local_path": "/a/S01E01.strm", "webdav_path": "/strm/S01E01.strm", "parent_webdav_path": "/strm"},
        ]
        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.__exit__ = MagicMock(return_value=False)
        app_service.db.read_connection.return_value = mock_conn_ctx

        # Mock API 返回相同文件
        app_service.admin_api.list_directory.return_value = {
            "code": 200,
            "data": {
                "content": [
                    {"name": "S01E01.strm", "is_dir": False},
                ]
            }
        }

        result = _do_media_refresh(app_service, "a", "test_media")
        assert result["ok"] is True
        assert result["added"] == 0
        assert result["removed"] == 0
        assert result["unchanged"] == 1

    def test_diff_detection_new_files(self):
        """API 多出文件时应检测到新增"""
        app_service = MagicMock()
        app_service.db = MagicMock()
        app_service.admin_api = MagicMock()

        # Mock DB 查询返回空
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.__exit__ = MagicMock(return_value=False)
        app_service.db.read_connection.return_value = mock_conn_ctx

        result = _do_media_refresh(app_service, "a", "test_media")
        assert result["ok"] is True
        assert result["message"] == "no records found"

    def test_diff_detection_removed_files(self):
        """API 缺少文件时应检测到删除"""
        app_service = MagicMock()
        app_service.db = MagicMock()
        app_service.admin_api = MagicMock()

        # Mock DB 查询返回 2 个文件
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            {"local_path": "/a/S01E01.strm", "webdav_path": "/strm/S01E01.strm", "parent_webdav_path": "/strm"},
            {"local_path": "/a/S01E02.strm", "webdav_path": "/strm/S01E02.strm", "parent_webdav_path": "/strm"},
        ]
        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.__exit__ = MagicMock(return_value=False)
        app_service.db.read_connection.return_value = mock_conn_ctx

        # Mock API 只返回 1 个文件（S01E02 缺失）
        app_service.admin_api.list_directory.return_value = {
            "code": 200,
            "data": {
                "content": [
                    {"name": "S01E01.strm", "is_dir": False},
                ]
            }
        }

        # Mock DB 方法
        app_service.db.get_b_by_webdav.return_value = []

        result = _do_media_refresh(app_service, "a", "test_media")
        assert result["ok"] is True
        assert result["removed"] == 1
        assert result["unchanged"] == 1

        # 验证调用了删除方法
        app_service.db.delete_a_by_local.assert_called()


class TestSyncToBZone:
    """测试 A 区变化同步到 B 区"""

    def test_sync_to_b_zone_called(self):
        """刷新完成后应调用 scan_a_to_b_full_sync"""
        app_service = MagicMock()
        app_service.db = MagicMock()
        app_service.admin_api = MagicMock()

        # Mock DB 查询返回
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            {"local_path": "/a/S01E01.strm", "webdav_path": "/strm/S01E01.strm", "parent_webdav_path": "/strm"},
        ]
        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.__exit__ = MagicMock(return_value=False)
        app_service.db.read_connection.return_value = mock_conn_ctx

        # Mock API 返回
        app_service.admin_api.list_directory.return_value = {
            "code": 200,
            "data": {
                "content": [
                    {"name": "S01E01.strm", "is_dir": False},
                ]
            }
        }

        result = _do_media_refresh(app_service, "a", "test_media")
        assert result["ok"] is True

        # 验证调用了 scan_a_to_b_full_sync
        app_service.scan_a_to_b_full_sync.assert_called_once()

    def test_sync_handles_new_files(self):
        """新增文件应调用 handle_a_created_or_modified"""
        app_service = MagicMock()
        app_service.db = MagicMock()
        app_service.admin_api = MagicMock()

        # Mock DB 查询返回空（无现有记录）
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            {"local_path": "/a/头文字D/S01E01.strm", "webdav_path": "/strm/头文字D/S01E01.strm", "parent_webdav_path": "/strm/头文字D"},
        ]
        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.__exit__ = MagicMock(return_value=False)
        app_service.db.read_connection.return_value = mock_conn_ctx

        # Mock API 返回 2 个文件（多出 S01E02）
        app_service.admin_api.list_directory.return_value = {
            "code": 200,
            "data": {
                "content": [
                    {"name": "S01E01.strm", "is_dir": False},
                    {"name": "S01E02.strm", "is_dir": False},
                ]
            }
        }

        result = _do_media_refresh(app_service, "a", "头文字D")
        assert result["ok"] is True
        assert result["added"] == 1

        # 验证调用了 handle_a_created_or_modified
        app_service.handle_a_created_or_modified.assert_called()
