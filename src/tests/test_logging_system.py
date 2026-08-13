"""
日志系统专项测试

测试覆盖：
1. TMDB 操作日志的写入/查询一致性
2. TMDB 操作日志的行数限制清理机制
3. 主程序日志文件的读取优化
4. 日志系统热更新功能
5. 日志文件轮转行为
6. 回退路径实际写入验证
"""
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 添加 src 到路径
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logger_setup import setup_logging, MaxLevelFilter
from tmdb_watchlist_db import TmdbWatchlistDb


class TestTmdbOperationLog:
    """TMDB 操作日志测试"""

    def test_log_write_and_query_consistency(self, tmp_path):
        """测试 TMDB 日志写入后能正确查询"""
        db_path = str(tmp_path / "test_tmdb.db")
        db = TmdbWatchlistDb(db_path)

        # 写入测试日志
        db.log_tmdb_operation("sync", "info", "测试同步操作", detail='{"count": 10}')
        db.log_tmdb_operation("match", "success", "测试匹配操作")
        db.log_tmdb_operation("error_test", "error", "测试错误操作")

        # 查询日志
        logs = db.get_tmdb_logs(limit=10)

        assert len(logs) == 3
        # 验证时间倒序
        assert logs[0]["op"] == "error_test"
        assert logs[1]["op"] == "match"
        assert logs[2]["op"] == "sync"

        # 验证字段完整性
        assert logs[0]["level"] == "error"
        assert logs[0]["msg"] == "测试错误操作"
        assert logs[2]["detail"] == '{"count": 10}'

    def test_log_auto_cleanup_after_7_days(self, tmp_path):
        """测试 7 天前的日志自动清理

        注意：_prune_tmdb_logs() 仅在写侧 log_tmdb_operation() 调用，
        get_tmdb_logs() 只做 SELECT。因此插入旧日志后需显式调用 _prune_tmdb_logs()
        触发清理，再通过 get_tmdb_logs() 验证结果。
        """
        db_path = str(tmp_path / "test_tmdb.db")
        db = TmdbWatchlistDb(db_path)

        # 写入当前日志
        db.log_tmdb_operation("recent", "info", "最近的日志")

        # 手动插入一条 8 天前的日志
        eight_days_ago = time.time() - 8 * 86400
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO tmdb_operation_log (ts, op, level, msg, detail) VALUES (?, ?, ?, ?, ?)",
                (eight_days_ago, "old", "info", "8天前的日志", None)
            )
            conn.commit()

        # 显式触发写侧清理（_prune_tmdb_logs 只在 log_tmdb_operation 调用）
        db._prune_tmdb_logs()

        # 查询应只返回 recent 日志
        logs = db.get_tmdb_logs(limit=10)

        # 应该只有 1 条（旧的被清理）
        assert len(logs) == 1
        assert logs[0]["op"] == "recent"

    def test_log_row_limit_cleanup(self, tmp_path):
        """测试日志行数限制清理机制（新增功能）"""
        db_path = str(tmp_path / "test_tmdb.db")
        db = TmdbWatchlistDb(db_path)

        # 写入 1500 条日志（超过默认的 1000 条限制）
        for i in range(1500):
            db.log_tmdb_operation(f"op_{i}", "info", f"日志 {i}")

        # 查询应触发行数清理
        logs = db.get_tmdb_logs(limit=2000)

        # 应该只保留最新的 1000 条
        assert len(logs) == 1000
        # 验证保留的是最新的（op_1499 到 op_500）
        assert logs[0]["op"] == "op_1499"
        assert logs[-1]["op"] == "op_500"

    def test_log_limit_configurable(self, tmp_path):
        """测试日志行数限制可配置"""
        db_path = str(tmp_path / "test_tmdb.db")
        db = TmdbWatchlistDb(db_path, tmdb_log_max_rows=500)

        # 写入 800 条日志
        for i in range(800):
            db.log_tmdb_operation(f"op_{i}", "info", f"日志 {i}")

        logs = db.get_tmdb_logs(limit=2000)

        # 应该只保留最新的 500 条
        assert len(logs) == 500


class TestMainLogReading:
    """主程序日志读取测试"""

    def test_read_log_with_multibyte_characters(self, tmp_path):
        """测试读取包含多字节字符的日志文件"""
        log_file = tmp_path / "test.log"

        # 写入包含大量中文的日志
        lines = []
        for i in range(100):
            line = f"2026-07-12 09:30:{i:02d} [INFO] [TMDB] 电影同步完成 ({i} 项) - 这是一段很长的中文日志内容用于测试多字节字符"
            lines.append(line)

        log_file.write_text("\n".join(lines), encoding="utf-8")

        # 模拟读取最后 50 行
        from webui.routes import _read_log_file_tail
        result = _read_log_file_tail(str(log_file), 50)

        assert len(result) == 50
        # 验证读取的是最后 50 行（索引 50-99）
        assert "电影同步完成 (99 项)" in result[-1]
        assert "电影同步完成 (50 项)" in result[0]

    def test_read_log_file_not_exists(self, tmp_path):
        """测试读取不存在的日志文件"""
        from webui.routes import _read_log_file_tail
        result = _read_log_file_tail(str(tmp_path / "nonexistent.log"), 100)
        assert result == []

    def test_read_log_empty_file(self, tmp_path):
        """测试读取空日志文件"""
        log_file = tmp_path / "empty.log"
        log_file.write_text("", encoding="utf-8")

        from webui.routes import _read_log_file_tail
        result = _read_log_file_tail(str(log_file), 100)
        assert result == []

    def test_read_log_fewer_lines_than_requested(self, tmp_path):
        """测试日志文件行数少于请求行数"""
        log_file = tmp_path / "small.log"
        lines = [f"Line {i}" for i in range(10)]
        log_file.write_text("\n".join(lines), encoding="utf-8")

        from webui.routes import _read_log_file_tail
        result = _read_log_file_tail(str(log_file), 100)

        assert len(result) == 10


class TestLogHotReload:
    """日志热更新测试"""

    def test_setup_logging_clears_old_handlers(self, tmp_path):
        """测试 setup_logging 会清理旧的 handler"""
        log_file = tmp_path / "test1.log"

        # 第一次初始化
        setup_logging(level="INFO", log_file=str(log_file), max_size_mb=2, backup_count=5)
        root_logger = logging.getLogger()
        initial_handler_count = len(root_logger.handlers)

        # 第二次初始化（模拟热更新）
        log_file2 = tmp_path / "test2.log"
        setup_logging(level="DEBUG", log_file=str(log_file2), max_size_mb=5, backup_count=10)

        # handler 数量应该保持一致（旧的被清理）
        assert len(root_logger.handlers) == initial_handler_count

        # 验证新配置生效
        file_handler = next(
            (h for h in root_logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)),
            None
        )
        assert file_handler is not None
        assert file_handler.maxBytes == 5 * 1024 * 1024
        assert file_handler.backupCount == 10

    def test_setup_logging_changes_log_level(self, tmp_path):
        """测试热更新能改变日志级别"""
        log_file = tmp_path / "test.log"

        # 初始化为 INFO
        setup_logging(level="INFO", log_file=str(log_file), max_size_mb=2, backup_count=5)
        root_logger = logging.getLogger()

        file_handler = next(
            (h for h in root_logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)),
            None
        )
        assert file_handler.level == logging.INFO

        # 热更新为 DEBUG
        setup_logging(level="DEBUG", log_file=str(log_file), max_size_mb=2, backup_count=5)

        # 重新获取 handler（因为 setup_logging 会清理并重建 handlers）
        file_handler = next(
            (h for h in root_logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)),
            None
        )
        assert file_handler.level == logging.DEBUG


class TestLogRotation:
    """日志轮转测试"""

    def test_log_rotation_creates_backup(self, tmp_path):
        """测试日志文件达到大小限制后创建备份"""
        log_file = tmp_path / "test.log"

        # 设置很小的轮转大小（1KB）
        setup_logging(level="INFO", log_file=str(log_file), max_size_mb=0.001, backup_count=3)

        # 写入超过 1KB 的日志
        logger = logging.getLogger("test_rotation")
        for i in range(100):
            logger.info(f"这是一条测试日志消息 {i} " * 10)

        # 刷新 handler
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.handlers.RotatingFileHandler):
                handler.flush()
                handler.close()

        # 验证备份文件存在
        backup_files = list(tmp_path.glob("test.log.*"))
        assert len(backup_files) > 0

    def test_log_rotation_respects_backup_count(self, tmp_path):
        """测试日志轮转遵守备份数量限制"""
        log_file = tmp_path / "test.log"

        # 设置备份数量为 2
        setup_logging(level="INFO", log_file=str(log_file), max_size_mb=0.001, backup_count=2)

        logger = logging.getLogger("test_rotation_count")
        # 写入大量日志触发多次轮转
        for i in range(500):
            logger.info(f"测试日志 {i} " * 20)

        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.handlers.RotatingFileHandler):
                handler.flush()
                handler.close()

        # 验证备份文件数量不超过 2
        backup_files = list(tmp_path.glob("test.log.*"))
        assert len(backup_files) <= 2


class TestLogFallback:
    """日志回退路径测试"""

    def test_fallback_to_temp_on_unwritable_path(self, tmp_path):
        """测试不可写路径回退到临时目录（父路径是普通文件，mkdir 立即失败）"""
        blocking_parent = tmp_path / "not_a_directory"
        blocking_parent.write_text("file blocks mkdir", encoding="utf-8")
        unwritable_path = str(blocking_parent / "test.log")

        setup_logging(level="INFO", log_file=unwritable_path, max_size_mb=2, backup_count=5)

        root_logger = logging.getLogger()
        file_handler = next(
            (h for h in root_logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)),
            None
        )

        assert file_handler is not None
        # 验证日志文件在临时目录
        log_path = Path(file_handler.baseFilename)
        assert log_path.parent == Path(tempfile.gettempdir())

        # 验证实际能写入日志
        logger = logging.getLogger("test_fallback")
        logger.info("测试回退路径")
        file_handler.flush()

        assert log_path.exists()
        content = log_path.read_text(encoding="utf-8")
        assert "测试回退路径" in content

    @pytest.mark.skipif(os.name != "nt", reason="Windows drive preflight only")
    def test_missing_windows_drive_falls_back_without_mkdir(self, monkeypatch):
        """缺失盘符在 mkdir 前直接回退，且不对缺失盘符执行 mkdir"""
        import string
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()
        missing = next(
            (
                letter
                for letter in string.ascii_uppercase
                if not (mask & (1 << (ord(letter) - ord("A"))))
            ),
            None,
        )
        if missing is None:
            pytest.skip("no missing drive letter available")

        handler_path = f"{missing}:\\unreachable\\test.log"
        original_mkdir = Path.mkdir
        mkdir_calls = []

        def record_mkdir(self, *args, **kwargs):
            mkdir_calls.append(self)
            return original_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", record_mkdir)
        setup_logging(level="INFO", log_file=handler_path, max_size_mb=2, backup_count=1)
        file_handler = next(
            h for h in logging.getLogger().handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        )
        assert Path(file_handler.baseFilename).parent == Path(tempfile.gettempdir())
        assert all(not str(path).startswith(f"{missing}:") for path in mkdir_calls)


class TestMaxLevelFilter:
    """MaxLevelFilter 测试"""

    def test_filter_allows_messages_up_to_max_level(self):
        """测试过滤器允许小于等于最大级别的消息"""
        filter_obj = MaxLevelFilter(logging.WARNING)

        # DEBUG < WARNING，应该通过
        debug_record = logging.LogRecord(
            "test", logging.DEBUG, "", 0, "debug msg", (), None
        )
        assert filter_obj.filter(debug_record) is True

        # INFO < WARNING，应该通过
        info_record = logging.LogRecord(
            "test", logging.INFO, "", 0, "info msg", (), None
        )
        assert filter_obj.filter(info_record) is True

        # WARNING == WARNING，应该通过
        warning_record = logging.LogRecord(
            "test", logging.WARNING, "", 0, "warning msg", (), None
        )
        assert filter_obj.filter(warning_record) is True

        # ERROR > WARNING，应该被过滤
        error_record = logging.LogRecord(
            "test", logging.ERROR, "", 0, "error msg", (), None
        )
        assert filter_obj.filter(error_record) is False


class TestLogFormat:
    """日志格式测试"""

    def test_log_format_includes_timestamp_and_level(self, tmp_path):
        """测试日志格式包含时间戳和级别"""
        log_file = tmp_path / "test.log"
        setup_logging(level="INFO", log_file=str(log_file), max_size_mb=2, backup_count=5)

        logger = logging.getLogger("test_format")
        logger.info("测试消息")

        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.handlers.RotatingFileHandler):
                handler.flush()

        content = log_file.read_text(encoding="utf-8")
        # 验证格式：时间戳 [级别] 消息
        assert "2026-" in content or "2025-" in content  # 时间戳
        assert "[INFO]" in content
        assert "测试消息" in content


class TestNewLogPoints:
    """本轮新增日志点覆盖（sync_* 系列 + openlist_config_save）"""

    def test_openlist_config_save_logged(self, tmp_path):
        """OpenList 配置保存日志点可写入/查询"""
        db = TmdbWatchlistDb(str(tmp_path / "test_tmdb.db"))
        db.log_tmdb_operation(
            "openlist_config_save", "success",
            "OpenList 配置已保存 (3 项配置)",
            detail='{"keys":["webdav_host"]}',
        )
        logs = db.get_tmdb_logs(limit=10)
        assert len(logs) == 1
        assert logs[0]["op"] == "openlist_config_save"
        assert logs[0]["level"] == "success"
        assert logs[0]["msg"] == "OpenList 配置已保存 (3 项配置)"
        assert logs[0]["detail"] == '{"keys":["webdav_host"]}'

    def test_sync_info_series_logged(self, tmp_path):
        """sync_* info 系列日志点全部可查询，时间倒序，level 正确"""
        db = TmdbWatchlistDb(str(tmp_path / "test_tmdb.db"))
        ops = [
            ("sync_cache_expired", "info"),
            ("sync_movies_done", "info"),
            ("sync_tv_done", "info"),
            ("sync_tv_details_start", "info"),
            ("sync_tv_details_done", "info"),
            ("sync_summary", "success"),
        ]
        for op, level in ops:
            db.log_tmdb_operation(op, level, f"msg-{op}")

        logs = db.get_tmdb_logs(limit=20)
        assert len(logs) == len(ops)
        # 时间倒序：最后写入的 sync_summary 在首位
        assert logs[0]["op"] == "sync_summary"
        assert logs[0]["level"] == "success"
        assert logs[-1]["op"] == "sync_cache_expired"
        # 所有 info 级别正确
        info_ops = [l for l in logs if l["level"] == "info"]
        assert len(info_ops) == 5

    def test_sync_error_series_logged(self, tmp_path):
        """sync_* error 系列日志点 level 正确"""
        db = TmdbWatchlistDb(str(tmp_path / "test_tmdb.db"))
        for op in ("sync_movies_error", "sync_tv_error", "sync_tv_details_error"):
            db.log_tmdb_operation(op, "error", f"err-{op}")
        logs = db.get_tmdb_logs(limit=10)
        assert len(logs) == 3
        assert all(l["level"] == "error" for l in logs)
        assert {l["op"] for l in logs} == {
            "sync_movies_error", "sync_tv_error", "sync_tv_details_error",
        }

    def test_all_new_ops_have_frontend_labels(self):
        """所有新增 op key 在前端 opLabel 映射中都有中文标签，避免显示原始 key"""
        logs_js = Path(__file__).resolve().parent.parent / "webui" / "modules" / "pages" / "logs.js"
        content = logs_js.read_text(encoding="utf-8")
        new_ops = [
            "sync_cache_expired",
            "sync_movies_done",
            "sync_movies_error",
            "sync_tv_done",
            "sync_tv_error",
            "sync_tv_details_start",
            "sync_tv_details_done",
            "sync_tv_details_error",
            "sync_summary",
            "openlist_config_save",
        ]
        for op in new_ops:
            assert op in content, f"前端 opLabel 缺少 {op} 标签"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
