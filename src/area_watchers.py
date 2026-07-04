from __future__ import annotations

import logging
import threading
from pathlib import Path

from watchdog.events import FileSystemEventHandler

from utils import make_strm_fingerprint, read_strm_webdav_path


class AAreaEventHandler(FileSystemEventHandler):
    def __init__(self, app) -> None:
        self.app = app

    def _run_async(self, func, *args) -> None:
        """在独立线程中执行可能阻塞的处理函数，避免阻塞 watchdog 线程。"""
        threading.Thread(
            target=self._safe_call,
            args=(func, *args),
            daemon=True,
        ).start()

    def _safe_call(self, func, *args) -> None:
        try:
            func(*args)
        except Exception:
            logging.exception("[A区事件处理异常] %s args=%s", func.__name__, args)

    def on_created(self, event) -> None:
        # 不过滤扩展名：字幕文件（.ass/.ssa/.srt）由 handle_a_created_or_modified
        # 内部的 is_subtitle_file 分流处理；非字幕非 STRM 文件会在该方法中安全跳过。
        if not event.is_directory:
            self._run_async(self.app.handle_a_created_or_modified, event.src_path)

    def on_modified(self, event) -> None:
        if not event.is_directory:
            self._run_async(self.app.handle_a_created_or_modified, event.src_path)

    def on_deleted(self, event) -> None:
        if not event.is_directory:
            self._run_async(self.app.handle_a_deleted, event.src_path)


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

        if src_is_strm and dst_is_strm:
            # .strm 重命名为 .strm：更新数据库路径（不触发云盘删除）
            moved = self.app.db.move_b_record(event.src_path, event.dest_path)
            if moved:
                logging.info(
                    "[B区重命名] 已更新路径: %s -> %s",
                    Path(event.src_path).name,
                    Path(event.dest_path).name,
                )
                # 刷新 identity 表的 current_b_path
                webdav = read_strm_webdav_path(event.dest_path)
                if webdav:
                    fp = make_strm_fingerprint(webdav)
                    self.app.refresh_identity_current_b_path(fp)
        elif src_is_strm and not dst_is_strm:
            # .strm 重命名为非 .strm：等同于删除
            self._run_async(self.app.handle_b_deleted, event.src_path)
        elif not src_is_strm and dst_is_strm:
            # 非 .strm 重命名为 .strm：等同于新建
            self.app.handle_b_created_or_modified(event.dest_path)


class CAreaEventHandler(FileSystemEventHandler):
    def __init__(self, app) -> None:
        self.app = app

    def on_deleted(self, event) -> None:
        if not event.is_directory and event.src_path.lower().endswith(".strm"):
            # C 区幽灵文件删除事件：仅记录日志
            # （幽灵文件的管理由其他模块负责，此处不做处理）
            logging.info("[C区] 检测到幽灵文件删除: %s", Path(event.src_path).name)

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
