"""Event Handlers - filesystem event handlers for A/B/C areas."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app_service import AppService
    from database import Database
    from webdav_client import OpenListAdminClient
    from config import AppConfig

from watchdog.events import FileSystemEventHandler


class AAreaEventHandler(FileSystemEventHandler):
    """A 区事件处理器"""

    def __init__(self, app: AppService) -> None:
        self.app = app

    def on_created(self, event):
        if not event.is_directory:
            self.app.handle_a_created_or_modified(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self.app.handle_a_created_or_modified(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self.app.handle_a_deleted(event.src_path)


class BAreaEventHandler(FileSystemEventHandler):
    """B 区事件处理器"""

    def __init__(self, app: AppService) -> None:
        self.app = app

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".strm"):
            self.app.handle_b_created_or_modified(event.src_path)

    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(".strm"):
            self.app.handle_b_created_or_modified(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory and event.src_path.endswith(".strm"):
            self.app.handle_b_deleted(event.src_path)

    def on_moved(self, event):
        if not event.is_directory and event.src_path.endswith(".strm"):
            # 处理重命名：先删除旧路径，再创建新路径
            self.app.handle_b_deleted(event.src_path)
            self.app.handle_b_created_or_modified(event.dest_path)


class CAreaEventHandler(FileSystemEventHandler):
    """C 区事件处理器"""

    def __init__(self, app: AppService) -> None:
        self.app = app
        self._c_event_logged: set[str] = set()
        self._c_event_lock = threading.Lock()

    def _log_c_event_once(self, event_type: str, src_path: str) -> None:
        """C区事件日志按目录前缀去重"""
        parent = str(Path(src_path).parent) + os.sep
        log_key = f"{event_type}|{parent}"
        with self._c_event_lock:
            if log_key in self._c_event_logged:
                return
            self._c_event_logged.add(log_key)
        logging.debug("[C区事件] %s: %s", event_type, parent)

    def on_created(self, event):
        if not event.is_directory:
            self._log_c_event_once("文件创建", event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self._log_c_event_once("文件删除", event.src_path)
