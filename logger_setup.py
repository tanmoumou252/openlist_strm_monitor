from __future__ import annotations

import logging
import os
import sys
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


def setup_logging(
    *,
    level: str = "INFO",
    log_file: str = "./activity.log",
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

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    # stdout: DEBUG/INFO/WARNING
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(log_level)
    stdout_handler.addFilter(MaxLevelFilter(logging.WARNING))
    stdout_handler.setFormatter(formatter)

    # stderr: ERROR/CRITICAL
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.ERROR)
    stderr_handler.setFormatter(formatter)

    # file: 按大小轮转
    file_handler = RotatingFileHandler(
        filename=str(log_path),
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
        log_path,
        max_size_mb,
        backup_count,
    )
