"""
Watchdog 事件处理器集成测试

测试范围：
1. A 区事件处理器（字幕文件处理回归 C1）
2. B 区事件处理器（重命名处理回归 C2）
3. C 区事件处理器

运行方式：
  pytest src/tests/test_area_watchers.py -v
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 确保 src/ 在 sys.path 中（conftest.py 也会处理，此处冗余保护）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from area_watchers import AAreaEventHandler, BAreaEventHandler, CAreaEventHandler
from database import Database, BRecord


# ============================================================
# Mock Event 类
# ============================================================


class MockEvent:
    """模拟 watchdog 事件"""

    def __init__(self, src_path: str, is_directory: bool = False, dest_path: str | None = None):
        self.src_path = src_path
        self.is_directory = is_directory
        self.dest_path = dest_path or ""


# ============================================================
# A 区事件处理器测试
# ============================================================


class TestAAreaEventHandler:
    """测试 A 区事件处理器"""

    def test_on_created_strm_file(self):
        """测试 A 区 .strm 文件创建事件"""
        app = MagicMock()
        handler = AAreaEventHandler(app)

        event = MockEvent("/path/to/file.strm")
        handler.on_created(event)

        # 等待异步线程执行
        time.sleep(0.1)

        # 验证 handle_a_created_or_modified 被调用
        assert app.handle_a_created_or_modified.called

    def test_on_created_subtitle_file_ass(self):
        """测试 A 区 .ass 字幕文件创建事件（C1 回归测试）"""
        app = MagicMock()
        handler = AAreaEventHandler(app)

        event = MockEvent("/path/to/subtitle.ass")
        handler.on_created(event)

        # 等待异步线程执行
        time.sleep(0.1)

        # 验证 handle_a_created_or_modified 被调用（不再被 .strm 过滤拦截）
        assert app.handle_a_created_or_modified.called

    def test_on_created_subtitle_file_srt(self):
        """测试 A 区 .srt 字幕文件创建事件（C1 回归测试）"""
        app = MagicMock()
        handler = AAreaEventHandler(app)

        event = MockEvent("/path/to/subtitle.srt")
        handler.on_created(event)

        time.sleep(0.1)

        assert app.handle_a_created_or_modified.called

    def test_on_created_subtitle_file_ssa(self):
        """测试 A 区 .ssa 字幕文件创建事件（C1 回归测试）"""
        app = MagicMock()
        handler = AAreaEventHandler(app)

        event = MockEvent("/path/to/subtitle.ssa")
        handler.on_created(event)

        time.sleep(0.1)

        assert app.handle_a_created_or_modified.called

    def test_on_modified_subtitle_file(self):
        """测试 A 区字幕文件修改事件（C1 回归测试）"""
        app = MagicMock()
        handler = AAreaEventHandler(app)

        event = MockEvent("/path/to/subtitle.ass")
        handler.on_modified(event)

        time.sleep(0.1)

        assert app.handle_a_created_or_modified.called

    def test_on_deleted_subtitle_file(self):
        """测试 A 区字幕文件删除事件（C1 回归测试）"""
        app = MagicMock()
        handler = AAreaEventHandler(app)

        event = MockEvent("/path/to/subtitle.ass")
        handler.on_deleted(event)

        time.sleep(0.1)

        assert app.handle_a_deleted.called

    def test_on_created_directory_ignored(self):
        """测试 A 区目录创建事件被忽略"""
        app = MagicMock()
        handler = AAreaEventHandler(app)

        event = MockEvent("/path/to/directory", is_directory=True)
        handler.on_created(event)

        time.sleep(0.1)

        # 目录事件不应触发处理
        assert not app.handle_a_created_or_modified.called

    def test_on_created_non_subtitle_non_strm(self):
        """测试 A 区非字幕非 STRM 文件创建事件"""
        app = MagicMock()
        handler = AAreaEventHandler(app)

        event = MockEvent("/path/to/file.txt")
        handler.on_created(event)

        time.sleep(0.1)

        # 文件会被传递给 handle_a_created_or_modified，由内部逻辑决定是否处理
        assert app.handle_a_created_or_modified.called


# ============================================================
# B 区事件处理器测试
# ============================================================


class TestBAreaEventHandler:
    """测试 B 区事件处理器"""

    def test_on_created_strm_file(self):
        """测试 B 区 .strm 文件创建事件"""
        app = MagicMock()
        handler = BAreaEventHandler(app)

        event = MockEvent("/path/to/file.strm")
        handler.on_created(event)

        assert app.handle_b_created_or_modified.called

    def test_on_modified_strm_file(self):
        """测试 B 区 .strm 文件修改事件"""
        app = MagicMock()
        handler = BAreaEventHandler(app)

        event = MockEvent("/path/to/file.strm")
        handler.on_modified(event)

        assert app.handle_b_created_or_modified.called

    def test_on_deleted_strm_file(self):
        """测试 B 区 .strm 文件删除事件"""
        app = MagicMock()
        handler = BAreaEventHandler(app)

        event = MockEvent("/path/to/file.strm")
        handler.on_deleted(event)

        time.sleep(0.1)

        assert app.handle_b_deleted.called

    def test_on_moved_strm_to_strm(self):
        """测试 B 区 .strm 重命名为 .strm（异步化回归测试）"""
        app = MagicMock()
        handler = BAreaEventHandler(app)

        event = MockEvent(
            src_path="/path/to/old.strm",
            dest_path="/path/to/new.strm"
        )

        handler.on_moved(event)

        # 现在通过 _run_async 异步调用 handle_b_moved，而非同步调用 db.move_b_record
        # 等待异步线程执行
        time.sleep(0.1)

        # 验证 handle_b_moved 被调用（而非直接调用 db.move_b_record）
        app.handle_b_moved.assert_called_once_with(
            "/path/to/old.strm",
            "/path/to/new.strm"
        )

        # 验证 refresh_identity_current_b_path 不再在 watcher 层调用（由 handle_b_moved 内部处理）
        assert not app.refresh_identity_current_b_path.called

    def test_on_moved_strm_to_non_strm(self):
        """测试 B 区 .strm 重命名为非 .strm（C2 回归测试）"""
        app = MagicMock()
        handler = BAreaEventHandler(app)

        event = MockEvent(
            src_path="/path/to/file.strm",
            dest_path="/path/to/file.txt"
        )

        handler.on_moved(event)

        time.sleep(0.1)

        # 应触发 renamed_to_non_strm 处理（而非直接删除）
        assert app.handle_b_renamed_to_non_strm.called

    def test_on_moved_non_strm_to_strm(self):
        """测试 B 区非 .strm 重命名为 .strm（C2 回归测试）"""
        app = MagicMock()
        handler = BAreaEventHandler(app)

        event = MockEvent(
            src_path="/path/to/file.txt",
            dest_path="/path/to/file.strm"
        )

        handler.on_moved(event)

        # 应触发创建处理
        assert app.handle_b_created_or_modified.called

    def test_on_moved_directory_ignored(self):
        """测试 B 区目录移动事件被忽略"""
        app = MagicMock()
        handler = BAreaEventHandler(app)

        event = MockEvent(
            src_path="/path/to/old_dir",
            dest_path="/path/to/new_dir",
            is_directory=True
        )

        handler.on_moved(event)

        # 目录事件不应触发任何处理
        assert not app.handle_b_deleted.called
        assert not app.handle_b_created_or_modified.called

    def test_on_moved_non_strm_to_non_strm(self):
        """测试 B 区非 .strm 重命名为非 .strm"""
        app = MagicMock()
        handler = BAreaEventHandler(app)

        event = MockEvent(
            src_path="/path/to/file.txt",
            dest_path="/path/to/file.log"
        )

        handler.on_moved(event)

        # 非 .strm 文件不应触发任何处理
        assert not app.handle_b_deleted.called
        assert not app.handle_b_created_or_modified.called


# ============================================================
# C 区事件处理器测试
# ============================================================


class TestCAreaEventHandler:
    """测试 C 区事件处理器"""

    def test_on_deleted_strm_file(self):
        """测试 C 区 .strm 文件删除事件（仅日志）"""
        app = MagicMock()
        handler = CAreaEventHandler(app)

        event = MockEvent("/path/to/ghost.strm")
        handler.on_deleted(event)

        # C 区事件仅记录日志，不触发业务逻辑
        # 验证无异常即可

    def test_on_created_strm_file(self):
        """测试 C 区 .strm 文件创建事件（仅日志）"""
        app = MagicMock()
        handler = CAreaEventHandler(app)

        event = MockEvent("/path/to/ghost.strm")
        handler.on_created(event)

        # C 区事件仅记录日志

    def test_on_moved_strm_to_strm(self):
        """测试 C 区 .strm 重命名为 .strm（仅日志）"""
        app = MagicMock()
        handler = CAreaEventHandler(app)

        event = MockEvent(
            src_path="/path/to/old.strm",
            dest_path="/path/to/new.strm"
        )

        handler.on_moved(event)

        # C 区事件仅记录日志


# ============================================================
# 集成场景测试
# ============================================================


class TestAreaWatcherIntegration:
    """区域事件处理器集成场景测试"""

    def test_subtitle_processing_flow(self):
        """测试字幕处理完整流程（C1 回归场景）"""
        app = MagicMock()
        handler = AAreaEventHandler(app)

        # 模拟字幕文件创建
        event = MockEvent("/media/show/episode.ass")
        handler.on_created(event)

        time.sleep(0.1)

        # 验证事件被传递给处理器
        assert app.handle_a_created_or_modified.called
        call_args = app.handle_a_created_or_modified.call_args
        assert call_args[0][0] == "/media/show/episode.ass"

    def test_b_rename_updates_database(self):
        """测试 B 区重命名更新数据库（异步化回归场景）"""
        app = MagicMock()
        handler = BAreaEventHandler(app)

        # 模拟 .strm 文件重命名
        event = MockEvent(
            src_path="/b/old_name.strm",
            dest_path="/b/new_name.strm"
        )

        handler.on_moved(event)

        # 现在通过 _run_async 异步调用 handle_b_moved
        # 等待异步线程执行
        time.sleep(0.1)

        # 验证 handle_b_moved 被调用
        app.handle_b_moved.assert_called_once_with(
            "/b/old_name.strm",
            "/b/new_name.strm"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
