from __future__ import annotations

import logging
import os
import sys
import tempfile
import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


class MaxLevelFilter(logging.Filter):
    """
    只允许小于等于 max_level 的日志通过。

    用途：
    - stdout 输出 DEBUG / INFO / WARNING
    - stderr 输出 ERROR / CRITICAL
    避免控制台重复打印 ERROR。
    """

    def __init__(self, max_level: int) -> None:
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.max_level


class EncodingSafeStreamHandler(logging.StreamHandler):
    """控制台 handler：写出前把目标编码无法表示的字符降级为替代符。

    Windows 中文控制台默认 GBK，emoji（如启动横幅的 🚀）或韩文等字符会让
    StreamHandler.emit 抛 UnicodeEncodeError，logging 随即调用 handleError
    打印 traceback，整条记录从控制台消失。文件 handler 已用 utf-8，不受影响。

    刻意不调用 stream.reconfigure()：sys.stdout / sys.stderr 的全局 errors
    策略必须保持原样，否则会连带改变 print() 等其它写入方的行为。

    写-刷用 self.lock 包住，保证 write + flush 的原子性。Handler.handle()
    调用 emit() 时其实已持有该锁，这里是防御直接调用 emit() 绕过 handle()
    的路径；logging.Handler 的 lock 是 RLock（Python 3.14 实测同线程可重入），
    所以重复获取不会死锁。
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            self.handleError(record)
            return
        encoding = getattr(self.stream, "encoding", None) or "utf-8"
        try:
            message.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            message = message.encode(encoding, errors="replace").decode(
                encoding, errors="replace")
        try:
            with self.lock:
                self.stream.write(message + self.terminator)
                self.flush()
        except Exception:
            self.handleError(record)


def _has_available_windows_drive(path: Path) -> bool:
    """Return False only when an absolute Windows path uses a missing drive."""
    if os.name != "nt" or not path.is_absolute():
        return True
    drive = path.drive
    # UNC 路径（\\server\\share\\...）不是盘符路径，保持原有行为。
    if len(drive) != 2 or drive[1] != ":":
        return True
    import ctypes
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    if not mask:
        return True
    drive_index = ord(drive[0].upper()) - ord("A")
    return 0 <= drive_index < 26 and bool(mask & (1 << drive_index))


def setup_logging(
    *,
    level: str = "INFO",
    log_file: str = "strm_bridge.log",
    max_size_mb: int = 10,
    backup_count: int = 5,
) -> None:
    """
    初始化日志系统。

    - 控制台输出
    - 文件输出
    - 按大小轮转
    - 保留 backup_count 个备份
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    original_log_path = Path(log_file)
    final_log_path = original_log_path

    # 缺失 Windows 盘符时跳过 mkdir，直接回退临时目录，避免不可用盘符上阻塞。
    if not _has_available_windows_drive(original_log_path):
        logging.warning("[日志] 日志盘符不可用，将回退到临时日志文件: %s", original_log_path)
        final_log_path = Path(tempfile.gettempdir()) / original_log_path.name
    else:
        try:
            original_log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logging.warning(
                "[日志] 无法创建日志目录 %s: %s。将使用临时日志文件。",
                original_log_path.parent,
                e,
            )
            final_log_path = Path(tempfile.gettempdir()) / original_log_path.name

    # Check write permissions for the chosen log path
    if not os.access(final_log_path.parent, os.W_OK):
        logging.warning(f"[日志] 日志目录 '{final_log_path.parent}' 不可写。将回退到临时日志文件。")
        final_log_path = Path(tempfile.gettempdir()) / original_log_path.name
        # Ensure temp directory exists, though tempfile.gettempdir() should be safe
        final_log_path.parent.mkdir(parents=True, exist_ok=True)

    # If a fallback occurred, log it
    if final_log_path != original_log_path:
        logging.info(f"[日志] 实际日志文件路径已设置为：'{final_log_path}'")

    root = logging.getLogger()
    # 显式关闭旧 handler（尤其 RotatingFileHandler 持有的未 flush 缓冲区），
    # 否则热更新时切换 log_file 会让被困在缓冲区的日志丢失/到达错文件。
    for old_handler in list(root.handlers):
        try:
            old_handler.flush()
            old_handler.close()
        except Exception:
            pass
    root.handlers.clear()
    root.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    # stdout: DEBUG/INFO/WARNING
    stdout_handler = EncodingSafeStreamHandler(sys.stdout)
    stdout_handler.setLevel(log_level)
    stdout_handler.addFilter(MaxLevelFilter(logging.WARNING))
    stdout_handler.setFormatter(formatter)

    # stderr: ERROR/CRITICAL
    stderr_handler = EncodingSafeStreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.ERROR)
    stderr_handler.setFormatter(formatter)

    # file: 按大小轮转
    # [已修复] R11: 日志目标为只读文件/目录路径时 RotatingFileHandler 构造会抛
    # OSError 导致整个启动崩溃。回退到系统临时目录，仅降级不阻断启动。
    try:
        file_handler = RotatingFileHandler(
            filename=str(final_log_path),
            maxBytes=max_size_mb * 1024 * 1024,
            backupCount=backup_count,
            encoding="utf-8",
        )
    except OSError as e:
        fallback_path = os.path.join(
            tempfile.gettempdir(), "openlist_strm_bridge_fallback.log")
        logging.warning(
            "日志文件 %s 无法写入（%s），回退到临时目录 %s",
            final_log_path, e, fallback_path)
        file_handler = RotatingFileHandler(
            filename=fallback_path,
            maxBytes=max_size_mb * 1024 * 1024,
            backupCount=backup_count,
            encoding="utf-8",
        )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)

    root.addHandler(stdout_handler)
    root.addHandler(stderr_handler)
    root.addHandler(file_handler)

    # 第三方库降噪
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("watchdog").setLevel(logging.WARNING)

    logging.info(
        "[日志] 已初始化，level=%s, file=%s, max_size_mb=%s, backup_count=%s",
        level.upper(),
        final_log_path,
        max_size_mb,
        backup_count,
    )

    # 启动分隔标记：区分不同运行周期的日志
    logging.info("")
    logging.info("=" * 70)
    logging.info(" 🚀 strm_bridge 启动 — %s", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logging.info(" ═══════════════════ 以上为上一次日志 ═══════════════════")
    logging.info("")
