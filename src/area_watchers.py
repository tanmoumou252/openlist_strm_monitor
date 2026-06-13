from __future__ import annotations

import logging
import threading
from pathlib import Path

from watchdog.events import FileSystemEventHandler


class AAreaEventHandler(FileSystemEventHandler):
    def __init__(self, app) -> None:
        self.app = app

    def on_created(self, event) -> None:
        if not event.is_directory and event.src_path.lower().endswith(".strm"):
            self.app.handle_a_created_or_modified(event.src_path)

    def on_modified(self, event) -> None:
        if not event.is_directory and event.src_path.lower().endswith(".strm"):
            self.app.handle_a_created_or_modified(event.src_path)

    def on_deleted(self, event) -> None:
        if not event.is_directory and event.src_path.lower().endswith(".strm"):
            self.app.handle_a_deleted(event.src_path)


class BAreaEventHandler(FileSystemEventHandler):
    def __init__(self, app) -> None:
        self.app = app

    def _run_async(self, func, *args) -> None:
        threading.Thread(
            target=self._safe_call,
            args=(func, *args),
            daemon=True,
        ).start()

    def _safe_call(self, func, *args) -> None:
        try:
            func(*args)
        except Exception:
            logging.exception("[B区事件处理异常] %s args=%s", func.__name__, args)

    def on_created(self, event) -> None:
        # 移除: if getattr(self.app, '_b_watcher_paused', False): return
        if not event.is_directory and event.src_path.lower().endswith(".strm"):
            self.app.handle_b_created_or_modified(event.src_path)

    def on_modified(self, event) -> None:
        # 移除: if getattr(self.app, '_b_watcher_paused', False): return
        if not event.is_directory and event.src_path.lower().endswith(".strm"):
            self.app.handle_b_created_or_modified(event.src_path)

    def on_deleted(self, event) -> None:
        # 移除: if getattr(self.app, '_b_watcher_paused', False): return
        if not event.is_directory and event.src_path.lower().endswith(".strm"):
            self._run_async(self.app.handle_b_deleted, event.src_path)

    def on_moved(self, event) -> None:
        # 移除: if getattr(self.app, '_b_watcher_paused', False): return
        if event.is_directory:
            return

        src_is_strm = event.src_path.lower().endswith(".strm")
        dst_is_strm = event.dest_path.lower().endswith(".strm")

        if src_is_strm or dst_is_strm:
            self._run_async(
                self.app.handle_b_moved,
                event.src_path,
                event.dest_path)


class CAreaEventHandler(FileSystemEventHandler):
    def __init__(self, app) -> None:
        self.app = app

    def on_deleted(self, event) -> None:
        if not event.is_directory and event.src_path.lower().endswith(".strm"):
            self.app.handle_c_deleted(event.src_path)

    def on_created(self, event) -> None:
        if not event.is_directory and event.src_path.lower().endswith(".strm"):
            logging.info("[C区] 检测到幽灵文件新增: %s", Path(event.src_path).name)

    def on_moved(self, event) -> None:
        if event.is_directory:
            return

        src_is_strm = event.src_path.lower().endswith(".strm")
        dst_is_strm = event.dest_path.lower().endswith(".strm")

        if src_is_strm or dst_is_strm:
            logging.info(
                "[C区] 检测到幽灵文件移动: %s -> %s",
                Path(event.src_path).name,
                Path(event.dest_path).name,
            )
