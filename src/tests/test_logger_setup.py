"""logger_setup.py 单元测试

测试范围：
- ``setup_logging`` 正常初始化：三个 handler（stdout / stderr / 轮转文件）
- 重复初始化：旧 handler 被 flush + close + 清空，不累积、不丢缓冲日志
- 日志目录不可用回退：父路径被普通文件占用、Windows 缺失盘符
- 级别过滤：``level`` 只影响 handler 级别，root 始终为 DEBUG；
  stdout 只收 WARNING 及以下，stderr 只收 ERROR 及以上
- 启动分隔标记：分隔线、时间戳与"以上为上一次日志"
- ``MaxLevelFilter`` 边界
- ``_has_available_windows_drive`` 判定

与 ``test_logging_system.py`` 的关系：该文件覆盖 TMDB 操作日志表、日志读取
接口、轮转产物与前端标签；本文件专注 ``logger_setup`` 模块自身的 handler
装配、回退与级别语义。

注意：``setup_logging`` 修改全局 root logger，本文件用 autouse fixture 在
每个测试后恢复原始 handler，避免污染同一进程内的其他测试。

运行方式：
  python -m pytest src/tests/test_logger_setup.py -v
"""
from __future__ import annotations

import logging
import logging.handlers
import io
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logger_setup import (  # noqa: E402
    MaxLevelFilter,
    _has_available_windows_drive,
    setup_logging,
)


# ============================================================
# 全局 logger 隔离
# ============================================================

@pytest.fixture(autouse=True)
def _restore_root_logger():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    root.handlers.clear()
    try:
        yield
    finally:
        for handler in list(root.handlers):
            try:
                handler.flush()
                handler.close()
            except Exception:
                pass
        root.handlers.clear()
        root.handlers.extend(saved_handlers)
        root.setLevel(saved_level)


def _file_handler() -> logging.handlers.RotatingFileHandler:
    return next(
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    )


def _stream_handlers() -> list[logging.StreamHandler]:
    return [
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.handlers.RotatingFileHandler)
    ]


# ============================================================
# 正常初始化
# ============================================================

class TestSetupLoggingNormal:
    def test_installs_three_handlers(self, tmp_path):
        setup_logging(level="INFO", log_file=str(tmp_path / "app.log"))
        assert len(logging.getLogger().handlers) == 3

    def test_root_level_is_debug_regardless_of_level(self, tmp_path):
        """root 固定 DEBUG，实际过滤由各 handler 的 level 决定。"""
        setup_logging(level="ERROR", log_file=str(tmp_path / "app.log"))
        assert logging.getLogger().level == logging.DEBUG

    def test_file_handler_targets_requested_path(self, tmp_path):
        target = tmp_path / "app.log"
        setup_logging(level="INFO", log_file=str(target))
        assert Path(_file_handler().baseFilename) == target.resolve()

    def test_creates_missing_parent_directory(self, tmp_path):
        target = tmp_path / "nested" / "deeper" / "app.log"
        setup_logging(level="INFO", log_file=str(target))
        assert target.parent.is_dir()
        assert Path(_file_handler().baseFilename) == target.resolve()

    def test_rotation_parameters_are_applied(self, tmp_path):
        setup_logging(level="INFO", log_file=str(tmp_path / "app.log"),
                      max_size_mb=3, backup_count=7)
        handler = _file_handler()
        assert handler.maxBytes == 3 * 1024 * 1024
        assert handler.backupCount == 7

    def test_file_handler_uses_utf8(self, tmp_path):
        setup_logging(level="INFO", log_file=str(tmp_path / "app.log"))
        assert (_file_handler().encoding or "").lower().replace("-", "") == "utf8"

    def test_stdout_and_stderr_handlers_present(self, tmp_path):
        setup_logging(level="INFO", log_file=str(tmp_path / "app.log"))
        streams = {h.stream for h in _stream_handlers()}
        assert sys.stdout in streams
        assert sys.stderr in streams

    def test_stdout_handler_has_max_level_filter(self, tmp_path):
        setup_logging(level="INFO", log_file=str(tmp_path / "app.log"))
        stdout_handler = next(
            h for h in _stream_handlers() if h.stream is sys.stdout)
        assert any(isinstance(f, MaxLevelFilter) for f in stdout_handler.filters)

    def test_stderr_handler_starts_at_error(self, tmp_path):
        setup_logging(level="DEBUG", log_file=str(tmp_path / "app.log"))
        stderr_handler = next(
            h for h in _stream_handlers() if h.stream is sys.stderr)
        assert stderr_handler.level == logging.ERROR

    def test_stderr_handler_has_no_max_level_filter(self, tmp_path):
        setup_logging(level="INFO", log_file=str(tmp_path / "app.log"))
        stderr_handler = next(
            h for h in _stream_handlers() if h.stream is sys.stderr)
        assert not any(isinstance(f, MaxLevelFilter)
                       for f in stderr_handler.filters)

    def test_third_party_loggers_are_quieted(self, tmp_path):
        setup_logging(level="DEBUG", log_file=str(tmp_path / "app.log"))
        for name in ("urllib3", "requests", "watchdog"):
            assert logging.getLogger(name).level == logging.WARNING

    def test_log_file_is_created_and_written(self, tmp_path):
        target = tmp_path / "app.log"
        setup_logging(level="INFO", log_file=str(target))
        logging.info("初始化探针")
        _file_handler().flush()
        assert "初始化探针" in target.read_text(encoding="utf-8")

    def test_unknown_level_string_falls_back_to_info(self, tmp_path):
        setup_logging(level="NOT_A_LEVEL", log_file=str(tmp_path / "app.log"))
        assert _file_handler().level == logging.INFO

    def test_level_is_case_insensitive(self, tmp_path):
        setup_logging(level="warning", log_file=str(tmp_path / "app.log"))
        assert _file_handler().level == logging.WARNING


# ============================================================
# 启动分隔标记
# ============================================================

class TestStartupSeparator:
    def _content(self, tmp_path) -> str:
        target = tmp_path / "app.log"
        setup_logging(level="INFO", log_file=str(target))
        _file_handler().flush()
        return target.read_text(encoding="utf-8")

    def test_writes_separator_line(self, tmp_path):
        assert "=" * 70 in self._content(tmp_path)

    def test_writes_startup_banner(self, tmp_path):
        assert "strm_bridge 启动" in self._content(tmp_path)

    def test_writes_previous_run_marker(self, tmp_path):
        assert "以上为上一次日志" in self._content(tmp_path)

    def test_writes_init_summary_with_level_and_file(self, tmp_path):
        content = self._content(tmp_path)
        assert "[日志] 已初始化" in content
        assert "level=INFO" in content

    def test_second_run_appends_new_separator(self, tmp_path):
        target = tmp_path / "app.log"
        setup_logging(level="INFO", log_file=str(target))
        setup_logging(level="INFO", log_file=str(target))
        _file_handler().flush()
        content = target.read_text(encoding="utf-8")
        assert content.count("以上为上一次日志") == 2


# ============================================================
# 重复初始化（热更新）
# ============================================================

class TestRepeatedInitialization:
    def test_handler_count_stays_three(self, tmp_path):
        setup_logging(level="INFO", log_file=str(tmp_path / "a.log"))
        setup_logging(level="DEBUG", log_file=str(tmp_path / "b.log"))
        setup_logging(level="WARNING", log_file=str(tmp_path / "c.log"))
        assert len(logging.getLogger().handlers) == 3

    def test_old_file_handler_is_closed(self, tmp_path):
        setup_logging(level="INFO", log_file=str(tmp_path / "a.log"))
        old = _file_handler()
        setup_logging(level="INFO", log_file=str(tmp_path / "b.log"))
        assert old.stream is None or old.stream.closed

    def test_switching_file_flushes_buffered_records(self, tmp_path):
        """切换 log_file 时旧 handler 必须先 flush，缓冲日志不丢。"""
        first = tmp_path / "a.log"
        setup_logging(level="INFO", log_file=str(first))
        logging.info("切换前的日志")
        setup_logging(level="INFO", log_file=str(tmp_path / "b.log"))
        assert "切换前的日志" in first.read_text(encoding="utf-8")

    def test_new_target_receives_subsequent_records(self, tmp_path):
        setup_logging(level="INFO", log_file=str(tmp_path / "a.log"))
        second = tmp_path / "b.log"
        setup_logging(level="INFO", log_file=str(second))
        logging.info("切换后的日志")
        _file_handler().flush()
        assert "切换后的日志" in second.read_text(encoding="utf-8")

    def test_old_target_does_not_receive_new_records(self, tmp_path):
        first = tmp_path / "a.log"
        setup_logging(level="INFO", log_file=str(first))
        setup_logging(level="INFO", log_file=str(tmp_path / "b.log"))
        logging.info("只应进入新文件")
        _file_handler().flush()
        assert "只应进入新文件" not in first.read_text(encoding="utf-8")

    def test_level_change_takes_effect(self, tmp_path):
        target = tmp_path / "app.log"
        setup_logging(level="ERROR", log_file=str(target))
        assert _file_handler().level == logging.ERROR
        setup_logging(level="DEBUG", log_file=str(target))
        assert _file_handler().level == logging.DEBUG

    def test_close_failure_does_not_break_reinit(self, tmp_path, monkeypatch):
        """旧 handler close 抛异常时必须被吞掉，不能阻塞热更新。"""
        setup_logging(level="INFO", log_file=str(tmp_path / "a.log"))
        old = _file_handler()
        monkeypatch.setattr(
            old, "close", lambda: (_ for _ in ()).throw(OSError("locked")))
        setup_logging(level="INFO", log_file=str(tmp_path / "b.log"))
        assert len(logging.getLogger().handlers) == 3


# ============================================================
# 级别过滤
# ============================================================

class TestLevelFiltering:
    def test_below_level_is_not_written(self, tmp_path):
        target = tmp_path / "app.log"
        setup_logging(level="WARNING", log_file=str(target))
        logging.info("不该出现的 INFO")
        logging.warning("应该出现的 WARNING")
        _file_handler().flush()
        content = target.read_text(encoding="utf-8")
        assert "不该出现的 INFO" not in content
        assert "应该出现的 WARNING" in content

    def test_debug_level_records_everything(self, tmp_path):
        target = tmp_path / "app.log"
        setup_logging(level="DEBUG", log_file=str(target))
        logging.debug("调试信息")
        _file_handler().flush()
        assert "调试信息" in target.read_text(encoding="utf-8")

    def test_error_reaches_file_at_info_level(self, tmp_path):
        target = tmp_path / "app.log"
        setup_logging(level="INFO", log_file=str(target))
        logging.error("错误信息")
        _file_handler().flush()
        assert "错误信息" in target.read_text(encoding="utf-8")

    def test_stdout_filter_blocks_error(self, tmp_path):
        """ERROR 只能走 stderr，避免控制台重复打印。"""
        setup_logging(level="INFO", log_file=str(tmp_path / "app.log"))
        stdout_handler = next(
            h for h in _stream_handlers() if h.stream is sys.stdout)
        record = logging.LogRecord(
            "t", logging.ERROR, __file__, 1, "err", None, None)
        # Handler.filter 在新版 Python 返回 record 本身而非 True，只断言真假性。
        assert not stdout_handler.filter(record)

    def test_stdout_filter_allows_warning(self, tmp_path):
        setup_logging(level="INFO", log_file=str(tmp_path / "app.log"))
        stdout_handler = next(
            h for h in _stream_handlers() if h.stream is sys.stdout)
        record = logging.LogRecord(
            "t", logging.WARNING, __file__, 1, "warn", None, None)
        assert stdout_handler.filter(record)

    def test_format_contains_timestamp_and_level(self, tmp_path):
        target = tmp_path / "app.log"
        setup_logging(level="INFO", log_file=str(target))
        logging.info("格式探针")
        _file_handler().flush()
        line = next(
            ln for ln in target.read_text(encoding="utf-8").splitlines()
            if "格式探针" in ln)
        assert "[INFO]" in line
        # 形如 2026-01-01 12:00:00
        assert line[4] == "-" and line[13] == ":"


# ============================================================
# 回退路径
# ============================================================

class TestFallbackPaths:
    def test_file_blocking_parent_falls_back_to_temp(self, tmp_path):
        blocker = tmp_path / "not_a_dir"
        blocker.write_text("blocks mkdir", encoding="utf-8")
        setup_logging(level="INFO", log_file=str(blocker / "app.log"))
        assert Path(_file_handler().baseFilename).parent == Path(
            tempfile.gettempdir())

    def test_fallback_preserves_file_name(self, tmp_path):
        blocker = tmp_path / "not_a_dir"
        blocker.write_text("blocks mkdir", encoding="utf-8")
        setup_logging(level="INFO", log_file=str(blocker / "custom_name.log"))
        assert Path(_file_handler().baseFilename).name == "custom_name.log"

    def test_fallback_target_is_writable(self, tmp_path):
        blocker = tmp_path / "not_a_dir"
        blocker.write_text("blocks mkdir", encoding="utf-8")
        setup_logging(level="INFO", log_file=str(blocker / "fallback_probe.log"))
        handler = _file_handler()
        logging.info("回退后仍可写")
        handler.flush()
        assert "回退后仍可写" in Path(
            handler.baseFilename).read_text(encoding="utf-8")

    def test_fallback_is_logged(self, tmp_path, caplog):
        blocker = tmp_path / "not_a_dir"
        blocker.write_text("blocks mkdir", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            setup_logging(level="INFO", log_file=str(blocker / "app.log"))
        assert any("日志" in r.message for r in caplog.records)

    @pytest.mark.skipif(os.name != "nt", reason="仅 Windows 存在盘符预检")
    def test_missing_drive_falls_back(self, monkeypatch):
        missing = _first_missing_drive_letter()
        if missing is None:
            pytest.skip("当前机器没有空闲盘符可用于测试")
        setup_logging(level="INFO", log_file=f"{missing}:\\nowhere\\app.log")
        assert Path(_file_handler().baseFilename).parent == Path(
            tempfile.gettempdir())


def _first_missing_drive_letter() -> str | None:
    import ctypes
    import string
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return next(
        (letter for letter in string.ascii_uppercase
         if not (mask & (1 << (ord(letter) - ord("A"))))),
        None,
    )


# ============================================================
# _has_available_windows_drive
# ============================================================

class TestHasAvailableWindowsDrive:
    def test_relative_path_is_treated_as_available(self):
        assert _has_available_windows_drive(Path("relative/app.log")) is True

    def test_unc_path_is_treated_as_available(self):
        assert _has_available_windows_drive(
            Path(r"\\server\share\app.log")) is True

    @pytest.mark.skipif(os.name != "nt", reason="仅 Windows 存在盘符预检")
    def test_existing_drive_is_available(self, tmp_path):
        assert _has_available_windows_drive(tmp_path / "app.log") is True

    @pytest.mark.skipif(os.name != "nt", reason="仅 Windows 存在盘符预检")
    def test_missing_drive_is_unavailable(self):
        missing = _first_missing_drive_letter()
        if missing is None:
            pytest.skip("当前机器没有空闲盘符可用于测试")
        assert _has_available_windows_drive(
            Path(f"{missing}:\\nowhere\\app.log")) is False

    @pytest.mark.skipif(os.name == "nt", reason="非 Windows 分支")
    def test_posix_always_available(self):
        assert _has_available_windows_drive(Path("/var/log/app.log")) is True


# ============================================================
# MaxLevelFilter
# ============================================================

class TestMaxLevelFilter:
    @pytest.mark.parametrize("level,expected", [
        (logging.DEBUG, True),
        (logging.INFO, True),
        (logging.WARNING, True),
        (logging.ERROR, False),
        (logging.CRITICAL, False),
    ])
    def test_warning_threshold(self, level, expected):
        record = logging.LogRecord("t", level, __file__, 1, "m", None, None)
        assert MaxLevelFilter(logging.WARNING).filter(record) is expected

    def test_boundary_is_inclusive(self):
        record = logging.LogRecord(
            "t", logging.INFO, __file__, 1, "m", None, None)
        assert MaxLevelFilter(logging.INFO).filter(record) is True

    def test_custom_numeric_threshold(self):
        record = logging.LogRecord("t", 25, __file__, 1, "m", None, None)
        assert MaxLevelFilter(24).filter(record) is False
        assert MaxLevelFilter(25).filter(record) is True


# ============================================================
# 临时目录清理
# ============================================================

class TestTemporaryDirectoryCleanup:
    def test_handler_release_allows_file_removal(self, tmp_path):
        """handler 未关闭时 Windows 会锁文件；关闭后必须能删除临时日志。"""
        target = tmp_path / "app.log"
        setup_logging(level="INFO", log_file=str(target))
        logging.info("清理探针")
        handler = _file_handler()
        handler.flush()
        handler.close()
        logging.getLogger().handlers.remove(handler)
        target.unlink()
        assert not target.exists()

    def test_reinit_into_same_temp_dir_does_not_leak_handlers(self, tmp_path):
        for name in ("a.log", "b.log", "c.log", "d.log"):
            setup_logging(level="INFO", log_file=str(tmp_path / name))
        assert len(logging.getLogger().handlers) == 3
        assert len(list(tmp_path.glob("*.log"))) == 4


# ============================================================
# 窄编码控制台编码兜底
# ============================================================

class TestConsoleEncodingFallback:
    """窄编码控制台（GBK）下，无法编码的字符不得让整条日志丢失。

    回归：启动横幅含 🚀（U+1F680），在 GBK 控制台触发 UnicodeEncodeError，
    logging 吞异常并打 traceback，该条记录从控制台消失。
    文件 handler 有 encoding="utf-8" 所以文件里是好的——既有的
    test_writes_startup_banner 读的正是文件，因此抓不到这个问题。
    """

    def test_stdout_handler_tolerates_unencodable_char(self, tmp_path, monkeypatch):
        raw = io.BytesIO()
        narrow = io.TextIOWrapper(
            raw, encoding="gbk", errors="strict", write_through=True)
        monkeypatch.setattr(sys, "stdout", narrow)

        setup_logging(level="INFO", log_file=str(tmp_path / "app.log"))
        logging.info("probe \U0001f680 tail")
        for handler in _stream_handlers():
            handler.flush()
        narrow.flush()

        console = raw.getvalue().decode("gbk", errors="replace")
        assert "probe" in console and "tail" in console, console

    def test_global_stream_error_policy_is_untouched(self, tmp_path, monkeypatch):
        """兜底只作用于本 handler，不得改写流的全局 errors 策略。

        生产代码大量用 print() 直写控制台（首启密码、启动菜单、用法提示），
        全局改写 errors 会连带改变这些输出的行为。
        """
        raw = io.BytesIO()
        narrow = io.TextIOWrapper(
            raw, encoding="gbk", errors="strict", write_through=True)
        monkeypatch.setattr(sys, "stdout", narrow)

        setup_logging(level="INFO", log_file=str(tmp_path / "app.log"))

        assert narrow.errors == "strict"
        assert any(h.stream is narrow for h in _stream_handlers())
