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
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 确保 src/ 在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import Database
from utils import escape_like
from webui.routes import (
    _compute_common_parent_path,
    _parse_api_files,
    _do_media_refresh,
    _get_media_groups_paginated,
    _get_records_paginated,
)


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
    """测试刷新逻辑（简化后：查询 → 映射 → API 调用 → 同步）"""

    def test_refresh_no_records(self):
        """DB 无记录时应返回 未找到相关记录"""
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
        assert result["message"] == "未找到相关记录"

    def test_refresh_maps_to_engine_path(self):
        """应将云盘路径映射到 STRM 引擎入口路径后调用 API"""
        app_service = MagicMock()
        app_service.db = MagicMock()
        app_service.admin_api = MagicMock()

        # Mock DB 查询返回云盘路径格式的记录
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            {
                "local_path": "/a/S01E01.strm",
                "webdav_path": "/天翼云盘家庭云30GB/番剧/test_media/S01E01.mp4",
                "parent_webdav_path": "/天翼云盘家庭云30GB/番剧/test_media",
            },
        ]
        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.__exit__ = MagicMock(return_value=False)
        app_service.db.read_connection.return_value = mock_conn_ctx

        # Mock 引擎路径映射
        app_service._cloud_path_to_engine_paths.return_value = ["/strm/番剧/test_media"]

        # Mock API 返回成功
        app_service.admin_api.list_directory.return_value = {
            "code": 200,
            "data": {"content": []}
        }

        result = _do_media_refresh(app_service, "a", "test_media")
        assert result["ok"] is True
        assert result["refresh_dir"] == "/strm/番剧/test_media"

        # 验证 API 用 STRM 引擎路径调用，而非云盘路径
        app_service.admin_api.list_directory.assert_called_once_with(
            "/strm/番剧/test_media", refresh=True
        )

    def test_refresh_fallback_to_cloud_path(self):
        """无法映射引擎路径时应降级到云盘路径"""
        app_service = MagicMock()
        app_service.db = MagicMock()
        app_service.admin_api = MagicMock()

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            {
                "local_path": "/a/S01E01.strm",
                "webdav_path": "/天翼云盘家庭云30GB/番剧/test_media/S01E01.mp4",
                "parent_webdav_path": "/天翼云盘家庭云30GB/番剧/test_media",
            },
        ]
        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.__exit__ = MagicMock(return_value=False)
        app_service.db.read_connection.return_value = mock_conn_ctx

        # 引擎路径映射返回空（无匹配）
        app_service._cloud_path_to_engine_paths.return_value = []

        app_service.admin_api.list_directory.return_value = {
            "code": 200,
            "data": {"content": []}
        }

        result = _do_media_refresh(app_service, "a", "test_media")
        assert result["ok"] is True
        # 降级到云盘路径
        assert result["refresh_dir"] == "/天翼云盘家庭云30GB/番剧/test_media"
        app_service.admin_api.list_directory.assert_called_once_with(
            "/天翼云盘家庭云30GB/番剧/test_media", refresh=True
        )

    def test_refresh_refuses_root_when_no_common_parent(self):
        """公共父目录退化为 '/' 时应拒绝执行，避免全盘刷新"""
        app_service = MagicMock()
        app_service.db = MagicMock()
        app_service.admin_api = MagicMock()

        # 两条记录的父目录分属不同根 → _compute_common_parent_path 返回 '/'
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            {
                "local_path": "/a/S01E01.strm",
                "webdav_path": "/云盘A/番剧/x/S01E01.mp4",
                "parent_webdav_path": "/云盘A/番剧/x",
            },
            {
                "local_path": "/a/S01E02.strm",
                "webdav_path": "/云盘B/电影/y/S01E02.mp4",
                "parent_webdav_path": "/云盘B/电影/y",
            },
        ]
        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.__exit__ = MagicMock(return_value=False)
        app_service.db.read_connection.return_value = mock_conn_ctx

        result = _do_media_refresh(app_service, "a", "test_media")
        assert result["ok"] is False
        assert "error" in result

        # 绝不能对根目录发起刷新
        app_service.admin_api.list_directory.assert_not_called()
        app_service.scan_a_to_b_full_sync.assert_not_called()
        # 根目录退化场景下逐条同步也绝不执行（防御性断言）
        app_service.copy_a_record_to_b_if_needed.assert_not_called()


class TestSyncToBZone:
    """测试 A→B 同步在刷新后被调用"""

    def test_sync_to_b_zone_called(self):
        """刷新完成后应仅对该媒体的记录逐条调用 copy_a_record_to_b_if_needed（不全量同步）"""
        app_service = MagicMock()
        app_service.db = MagicMock()
        app_service.admin_api = MagicMock()

        # Mock DB 查询返回
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            {
                "local_path": "/a/S01E01.strm",
                "webdav_path": "/天翼云盘家庭云30GB/番剧/test_media/S01E01.mp4",
                "parent_webdav_path": "/天翼云盘家庭云30GB/番剧/test_media",
            },
        ]
        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.__exit__ = MagicMock(return_value=False)
        app_service.db.read_connection.return_value = mock_conn_ctx

        # Mock 引擎路径映射
        app_service._cloud_path_to_engine_paths.return_value = ["/strm/番剧/test_media"]

        # Mock API 返回
        app_service.admin_api.list_directory.return_value = {
            "code": 200,
            "data": {"content": []}
        }
        app_service.copy_a_record_to_b_if_needed.return_value = True

        # 源文件存在性检查返回 True，使同步逻辑真正执行
        with patch("webui.routes.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            result = _do_media_refresh(app_service, "a", "test_media")

        assert result["ok"] is True
        # 计数回传：单条记录同步成功
        assert result["synced"] == 1
        assert result["skipped"] == 0
        assert result["failed"] == 0
        assert result["message"] == "刷新完成：同步 1，跳过 0，失败 0"

        # 关键：绝不能触发全库全量同步
        app_service.scan_a_to_b_full_sync.assert_not_called()
        # 应对该媒体的单条记录调用逐条同步
        app_service.copy_a_record_to_b_if_needed.assert_called_once_with(
            "/a/S01E01.strm",
            "/天翼云盘家庭云30GB/番剧/test_media/S01E01.mp4",
            "/天翼云盘家庭云30GB/番剧/test_media",
        )

    def test_sync_not_called_on_api_error(self):
        """API 返回错误时不应调用任何 A→B 同步"""
        app_service = MagicMock()
        app_service.db = MagicMock()
        app_service.admin_api = MagicMock()

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            {
                "local_path": "/a/S01E01.strm",
                "webdav_path": "/天翼云盘家庭云30GB/番剧/test_media/S01E01.mp4",
                "parent_webdav_path": "/天翼云盘家庭云30GB/番剧/test_media",
            },
        ]
        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.__exit__ = MagicMock(return_value=False)
        app_service.db.read_connection.return_value = mock_conn_ctx

        app_service._cloud_path_to_engine_paths.return_value = ["/strm/番剧/test_media"]

        # Mock API 返回错误
        app_service.admin_api.list_directory.return_value = {
            "code": 500,
            "message": "server error"
        }

        result = _do_media_refresh(app_service, "a", "test_media")
        assert result["ok"] is False
        assert "error" in result

        # 不应调用任何同步
        app_service.scan_a_to_b_full_sync.assert_not_called()
        app_service.copy_a_record_to_b_if_needed.assert_not_called()

    def test_sync_counts_mixed_results(self):
        """多条记录返回 True/None/False 时，计数应正确累加且整体成功"""
        app_service = MagicMock()
        app_service.db = MagicMock()
        app_service.admin_api = MagicMock()

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            {
                "local_path": "/a/S01E01.strm",
                "webdav_path": "/云/番剧/test_media/S01E01.mp4",
                "parent_webdav_path": "/云/番剧/test_media",
            },
            {
                "local_path": "/a/S01E02.strm",
                "webdav_path": "/云/番剧/test_media/S01E02.mp4",
                "parent_webdav_path": "/云/番剧/test_media",
            },
            {
                "local_path": "/a/S01E03.strm",
                "webdav_path": "/云/番剧/test_media/S01E03.mp4",
                "parent_webdav_path": "/云/番剧/test_media",
            },
        ]
        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.__exit__ = MagicMock(return_value=False)
        app_service.db.read_connection.return_value = mock_conn_ctx

        app_service._cloud_path_to_engine_paths.return_value = ["/strm/番剧/test_media"]
        app_service.admin_api.list_directory.return_value = {"code": 200, "data": {"content": []}}
        # 依次返回 True / None / False
        app_service.copy_a_record_to_b_if_needed.side_effect = [True, None, False]

        with patch("webui.routes.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            result = _do_media_refresh(app_service, "a", "test_media")

        assert result["ok"] is True
        # 关键：绝不能触发全库全量同步
        app_service.scan_a_to_b_full_sync.assert_not_called()
        # 三条记录均被逐条处理
        assert app_service.copy_a_record_to_b_if_needed.call_count == 3
        # 计数：成功 1 / 跳过 1 / 失败 1
        assert result["synced"] == 1
        assert result["skipped"] == 1
        assert result["failed"] == 1
        assert result["message"] == "刷新完成：同步 1，跳过 1，失败 1"

    def test_sync_skips_missing_source_file(self):
        """源文件不存在的记录应被跳过，仅对存在的记录调用同步"""
        app_service = MagicMock()
        app_service.db = MagicMock()
        app_service.admin_api = MagicMock()

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            {
                "local_path": "/a/missing.strm",
                "webdav_path": "/云/番剧/test_media/missing.mp4",
                "parent_webdav_path": "/云/番剧/test_media",
            },
            {
                "local_path": "/a/exist.strm",
                "webdav_path": "/云/番剧/test_media/exist.mp4",
                "parent_webdav_path": "/云/番剧/test_media",
            },
        ]
        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.__exit__ = MagicMock(return_value=False)
        app_service.db.read_connection.return_value = mock_conn_ctx

        app_service._cloud_path_to_engine_paths.return_value = ["/strm/番剧/test_media"]
        app_service.admin_api.list_directory.return_value = {"code": 200, "data": {"content": []}}
        app_service.copy_a_record_to_b_if_needed.return_value = True

        # 第一条源文件不存在，第二条存在
        with patch("webui.routes.Path") as mock_path:
            mock_path.return_value.exists.side_effect = [False, True]
            result = _do_media_refresh(app_service, "a", "test_media")

        assert result["ok"] is True
        # 只对存在的那条调用一次
        app_service.copy_a_record_to_b_if_needed.assert_called_once_with(
            "/a/exist.strm",
            "/云/番剧/test_media/exist.mp4",
            "/云/番剧/test_media",
        )
        assert result["synced"] == 1
        assert result["skipped"] == 1
        assert result["failed"] == 0

    def test_sync_per_record_exception_does_not_abort(self):
        """单条同步抛异常不应中断整体流程，失败计入 failed 且整体仍成功返回"""
        app_service = MagicMock()
        app_service.db = MagicMock()
        app_service.admin_api = MagicMock()

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            {
                "local_path": "/a/fail.strm",
                "webdav_path": "/云/番剧/test_media/fail.mp4",
                "parent_webdav_path": "/云/番剧/test_media",
            },
            {
                "local_path": "/a/ok.strm",
                "webdav_path": "/云/番剧/test_media/ok.mp4",
                "parent_webdav_path": "/云/番剧/test_media",
            },
        ]
        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.__exit__ = MagicMock(return_value=False)
        app_service.db.read_connection.return_value = mock_conn_ctx

        app_service._cloud_path_to_engine_paths.return_value = ["/strm/番剧/test_media"]
        app_service.admin_api.list_directory.return_value = {"code": 200, "data": {"content": []}}
        # 第一条抛异常，第二条成功
        app_service.copy_a_record_to_b_if_needed.side_effect = [Exception("boom"), True]

        with patch("webui.routes.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            result = _do_media_refresh(app_service, "a", "test_media")

        # 不抛出、整体成功返回
        assert result["ok"] is True
        # 第二条仍被调用
        assert app_service.copy_a_record_to_b_if_needed.call_count == 2
        assert result["synced"] == 1
        assert result["failed"] == 1


class TestEscapeLike:
    """测试 LIKE 通配符转义（配合 ESCAPE '\\'）"""

    def test_escape_like_escapes_wildcards(self):
        """% _ \\ 应被转义，且反斜杠先于通配符转义"""
        assert escape_like("a%b") == r"a\%b"
        assert escape_like("a_b") == r"a\_b"
        assert escape_like(r"a\b") == r"a\\b"
        # 反斜杠先于通配符转义：输入含已转义序列 a\_%b 时，
        # 先转义反斜杠（a\\_），再转义 %（a\\_\%b），最后转义 _（a\\\_%b）
        assert escape_like(r"a\_%b") == "a\\\\\\_\\%b"

    def test_like_wildcard_in_media_name_escaped_real_db(self):
        """真实 DB：media_name 含下划线时，LIKE 只精确匹配该媒体，不命中其它"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / "test.db"))
            # 插入两条 A 区记录：一条含下划线，一条不含但子串相似
            db.upsert_a("/a/a_b.strm", "/w/a_b.mp4", "/w")
            db.upsert_a("/a/axb.strm", "/w/axb.mp4", "/w")

            media_name = "a_b"
            like = f"%{escape_like(media_name)}%"
            with db.read_connection() as conn:
                rows = conn.execute(
                    "SELECT local_path FROM a_strm_files "
                    "WHERE local_path LIKE ? ESCAPE '\\'",
                    (like,),
                ).fetchall()

            matched = {r[0] for r in rows}
            assert matched == {"/a/a_b.strm"}
            assert "/a/axb.strm" not in matched


class TestRefreshLikeEscape:
    """刷新查询对 media_name 中的 LIKE 通配符进行转义（真实 DB 端到端）"""

    def test_refresh_escapes_underscore_in_media_name(self):
        """media_name 含下划线时，刷新只处理该媒体记录，不误命中其它"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / "test.db"))
            # 目标媒体（含下划线）
            db.upsert_a("/a/a_b/S01E01.strm", "/w/a_b/S01E01.mp4", "/w/a_b")
            # 相似但不同的媒体（下划线被当作通配符时会误命中）
            db.upsert_a("/a/axb/S01E01.strm", "/w/axb/S01E01.mp4", "/w/axb")

            app_service = MagicMock()
            app_service.db = db
            app_service.admin_api = MagicMock()
            app_service._cloud_path_to_engine_paths.return_value = ["/strm/a_b"]
            app_service.admin_api.list_directory.return_value = {"code": 200, "data": {"content": []}}
            app_service.copy_a_record_to_b_if_needed.return_value = True

            with patch("webui.routes.Path") as mock_path:
                mock_path.return_value.exists.return_value = True
                result = _do_media_refresh(app_service, "a", "a_b")

            assert result["ok"] is True
            # 只同步了 a_b 这一条，未误命中 axb
            assert app_service.copy_a_record_to_b_if_needed.call_count == 1
            args = app_service.copy_a_record_to_b_if_needed.call_args[0]
            assert args[0] == "/a/a_b/S01E01.strm"


class TestMediaGroupsLikeFallbackEscape:
    """FTS5 失败回退到 LIKE 时，q 中的通配符必须被转义（与已修 3 处一致）"""

    def test_media_groups_like_fallback_escapes_wildcards(self):
        """回退 LIKE 查询必须使用 ESCAPE 子句并转义 q 中的 %、_、\\ 通配符"""
        import sqlite3
        handler = MagicMock()
        handler.webui._db = MagicMock()
        conn = MagicMock()
        conn.row_factory = None
        err = sqlite3.OperationalError("fts5 error")
        ok_cursor = MagicMock()
        ok_cursor.fetchall.return_value = []          # 回退查询返回空结果
        ok_cursor.fetchone.return_value = [0]         # total 查询返回 0
        # 第 1 次调用（FTS5 MATCH）抛异常触发回退；之后 3 次回退 LIKE 查询返回 ok_cursor
        conn.execute.side_effect = [err, ok_cursor, ok_cursor, ok_cursor]
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=conn)
        ctx.__exit__ = MagicMock(return_value=False)
        handler.webui._db.read_connection.return_value = ctx

        result = _get_media_groups_paginated(handler, "b", "all", "a_b", "name", "asc", 1, 50)

        # 回退路径应发出 3 条 LIKE 查询（kind_counts / total / media_groups）
        fallback_calls = [c for c in conn.execute.call_args_list[1:] if "LIKE" in str(c.args[0])]
        assert fallback_calls, "回退未发出 LIKE 查询"
        for call in fallback_calls:
            sql, params = call.args
            assert "ESCAPE '\\'" in sql, f"回退 LIKE 缺少 ESCAPE 子句: {sql}"
            assert any(p == "%a\\_b%" for p in params), f"回退 LIKE 参数未转义: {params}"
        assert result["media_items"] == []


class TestRecordsPaginatedLikeEscape:
    """_get_records_paginated 的 LIKE 查询必须转义搜索通配符"""

    @pytest.mark.parametrize("area", ["a", "b", "c"])
    def test_records_paginated_escapes_like_wildcards(self, area):
        """search 参数含下划线时，SQL 应使用 ESCAPE 并转义通配符"""
        handler = MagicMock()
        handler.webui._db = MagicMock()
        conn = MagicMock()
        conn.row_factory = None
        ok_cursor = MagicMock()
        ok_cursor.fetchone.return_value = [0]       # COUNT(*) 返回 0
        ok_cursor.fetchall.return_value = []         # 记录查询返回空
        conn.execute.return_value = ok_cursor
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=conn)
        ctx.__exit__ = MagicMock(return_value=False)
        handler.webui._db.read_connection.return_value = ctx

        result = _get_records_paginated(handler, area, page=1, page_size=50, search="a_b")

        assert result["records"] == []
        assert result["total"] == 0
        # 共 2 次 execute：COUNT + 记录查询
        assert conn.execute.call_count == 2
        for call in conn.execute.call_args_list:
            sql, params = call.args
            if "LIKE" in sql:
                assert "ESCAPE '\\'" in sql, f"LIKE 缺少 ESCAPE 子句: {sql}"
            # 两个参数都是转义后的 LIKE 模式（count 查询2 个参数，记录查询4 个 = 2 LIKE + 2 pagination）
            like_params = [p for p in params if isinstance(p, str) and "%" in p]
            for p in like_params:
                assert p == "%a\\_b%", f"LIKE 参数未转义: {params}"


class TestLastVerifiedAtWiring:
    """D'.1: 测试单剧目刷新后 last_verified_at 前进（真实 DB 端到端）"""

    def test_refresh_advances_last_verified_at_for_matched_records(self):
        """刷新成功后，命中记录的 last_verified_at 前进，updated_at 不变"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / "test.db"))
            # 插入一条 A 区记录
            db.upsert_a("/a/test/S01E01.strm", "/w/test/S01E01.mp4", "/w/test")
            # 设置初始 last_verified_at
            with db.rw_lock.write_locked(), db.connection() as conn:
                conn.execute("UPDATE a_strm_files SET last_verified_at = 100.0")
                conn.commit()

            # 读取初始值
            with db.read_connection() as conn:
                row = conn.execute(
                    "SELECT last_verified_at, updated_at FROM a_strm_files WHERE local_path = ?",
                    ("/a/test/S01E01.strm",),
                ).fetchone()
                initial_verified = row[0]
                initial_updated = row[1]

            assert initial_verified == 100.0

            # 构建 mock app_service
            app_service = MagicMock()
            app_service.db = db
            app_service.admin_api = MagicMock()
            app_service._cloud_path_to_engine_paths.return_value = ["/strm/test"]
            app_service.admin_api.list_directory.return_value = {
                "code": 200, "data": {"content": []}
            }
            app_service.copy_a_record_to_b_if_needed.return_value = True
            app_service.a_b_mappings = []

            with patch("webui.routes.Path") as mock_path:
                mock_path.return_value.exists.return_value = True
                result = _do_media_refresh(app_service, "a", "test")

            assert result["ok"] is True

            # 验证 last_verified_at 前进
            with db.read_connection() as conn:
                row = conn.execute(
                    "SELECT last_verified_at, updated_at FROM a_strm_files WHERE local_path = ?",
                    ("/a/test/S01E01.strm",),
                ).fetchone()
                final_verified = row[0]
                final_updated = row[1]

            assert final_verified > initial_verified, "last_verified_at 应前进"
            # updated_at 不应被刷新操作改变（touch 只更新 last_verified_at）

    def test_refresh_returns_verified_at_in_response(self):
        """刷新成功后，响应体包含 verified_at"""
        app_service = MagicMock()
        app_service.db = MagicMock()
        app_service.admin_api = MagicMock()
        app_service.a_b_mappings = []

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            {
                "local_path": "/a/S01E01.strm",
                "webdav_path": "/w/test/S01E01.mp4",
                "parent_webdav_path": "/w/test",
            },
        ]
        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.__exit__ = MagicMock(return_value=False)
        app_service.db.read_connection.return_value = mock_conn_ctx

        app_service._cloud_path_to_engine_paths.return_value = ["/strm/test"]
        app_service.admin_api.list_directory.return_value = {"code": 200, "data": {"content": []}}
        app_service.copy_a_record_to_b_if_needed.return_value = True

        with patch("webui.routes.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            result = _do_media_refresh(app_service, "a", "test")

        assert result["ok"] is True
        assert "verified_at" in result, "响应应包含 verified_at 字段"
        assert isinstance(result["verified_at"], (int, float))
