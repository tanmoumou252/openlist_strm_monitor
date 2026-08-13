from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler

from utils import make_strm_fingerprint, read_strm_webdav_path

class AAreaEventHandler(FileSystemEventHandler):
    # 事件处理线程并发上限。事件风暴时不再无限创建线程，
    # 超出上限的事件降级为在 watchdog 派发线程内同步执行（背压），
    # 保证事件不丢失同时限制线程数。
    _MAX_ASYNC_THREADS = 8

    def __init__(self, app) -> None:
        self.app = app
        # 健康信号 - 失败计数
        self._failure_count = 0
        self._last_failure_time = 0
        self._health_lock = threading.Lock()
        self._HEALTH_THRESHOLD = 10  # 连续失败阈值
        self._async_semaphore = threading.BoundedSemaphore(self._MAX_ASYNC_THREADS)

    def _run_async(self, func, *args) -> None:
        """在独立线程中执行可能阻塞的处理函数，避免阻塞 watchdog 线程。

        用信号量限制并发处理线程数量。信号量耗尽时同步降级执行，
        形成背压，避免事件风暴导致线程爆炸。
        """
        if self._async_semaphore.acquire(blocking=False):
            def _wrapped():
                try:
                    self._safe_call(func, *args)
                finally:
                    self._async_semaphore.release()
            threading.Thread(target=_wrapped, daemon=True).start()
        else:
            # 并发已达上限：同步降级执行（watchdog 派发线程内），
            # 事件不丢失，也不突破线程上限。
            logging.warning(
                "[A区] 并发处理线程已达上限(%d)，事件同步执行降级: %s",
                self._MAX_ASYNC_THREADS, func.__name__)
            self._safe_call(func, *args)

    def _safe_call(self, func, *args) -> None:
        try:
            func(*args)
            # 成功时重置失败计数
            with self._health_lock:
                self._failure_count = 0
            # 成功时恢复健康标志，防止健康状态永久锁定为 False
            if hasattr(self.app, '_watchers_healthy'):
                self.app._watchers_healthy = True
        except Exception:
            # 吞异常是有意设计（抛出将杀死 watchdog 线程）
            # 加失败计数 + _watchers_healthy 健康信号。勿改为 re-raise 或移除 try/except。
            # 记录失败并监控健康状态
            with self._health_lock:
                self._failure_count += 1
                self._last_failure_time = time.time()
                failure_count = self._failure_count
            logging.exception("[A区事件处理异常] %s args=%s (连续失败: %d)", func.__name__, args, failure_count)
            
            # 超过阈值时发出警告并标记健康状态
            if failure_count >= self._HEALTH_THRESHOLD:
                logging.warning(
                    "[A区] 连续失败 %d 次，可能存在系统性问题，请检查日志",
                    failure_count
                )
                # 此标志由 dashboard 状态 API 消费，勿当死代码删除
                if hasattr(self.app, '_watchers_healthy'):
                    self.app._watchers_healthy = False

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

    def on_moved(self, event) -> None:
        if event.is_directory:
            return
        # A 区移动：源路径视为删除，目标路径视为新增
        self._run_async(self.app.handle_a_deleted, event.src_path)
        self._run_async(self.app.handle_a_created_or_modified, event.dest_path)

class BAreaEventHandler(FileSystemEventHandler):
    # 事件处理线程并发上限（同 A 区，见 AAreaEventHandler._MAX_ASYNC_THREADS）
    _MAX_ASYNC_THREADS = 8

    def __init__(self, app) -> None:
        self.app = app
        # 健康信号 - 失败计数
        self._failure_count = 0
        self._last_failure_time = 0
        self._health_lock = threading.Lock()
        self._HEALTH_THRESHOLD = 10  # 连续失败阈值
        self._async_semaphore = threading.BoundedSemaphore(self._MAX_ASYNC_THREADS)

    def _run_async(self, func, *args) -> None:
        """在独立线程中执行可能阻塞的处理函数，避免阻塞 watchdog 线程。

        用信号量限制并发处理线程数量。信号量耗尽时同步降级执行，
        形成背压，避免事件风暴导致线程爆炸。
        """
        if self._async_semaphore.acquire(blocking=False):
            def _wrapped():
                try:
                    self._safe_call(func, *args)
                finally:
                    self._async_semaphore.release()
            threading.Thread(target=_wrapped, daemon=True).start()
        else:
            # 并发已达上限：同步降级执行（watchdog 派发线程内），
            # 事件不丢失，也不突破线程上限。
            logging.warning(
                "[B区] 并发处理线程已达上限(%d)，事件同步执行降级: %s",
                self._MAX_ASYNC_THREADS, func.__name__)
            self._safe_call(func, *args)

    def _safe_call(self, func, *args) -> None:
        try:
            func(*args)
            # 成功时重置失败计数
            with self._health_lock:
                self._failure_count = 0
            # 成功时恢复健康标志，防止健康状态永久锁定为 False
            if hasattr(self.app, '_watchers_healthy'):
                self.app._watchers_healthy = True
        except Exception:
            # 记录失败并监控健康状态
            with self._health_lock:
                self._failure_count += 1
                self._last_failure_time = time.time()
                failure_count = self._failure_count
            logging.exception("[B区事件处理异常] %s args=%s (连续失败: %d)", func.__name__, args, failure_count)
            
            # 超过阈值时发出警告并标记健康状态
            if failure_count >= self._HEALTH_THRESHOLD:
                logging.warning(
                    "[B区] 连续失败 %d 次，可能存在系统性问题，请检查日志",
                    failure_count
                )
                # 此标志由 dashboard 状态 API 消费，勿当死代码删除
                if hasattr(self.app, '_watchers_healthy'):
                    self.app._watchers_healthy = False

    def on_created(self, event) -> None:
        # 移除: if getattr(self.app, '_b_watcher_paused', False): return
        if not event.is_directory and Path(event.src_path).suffix.lower() == ".strm":
            self._run_async(self.app.handle_b_created_or_modified, event.src_path)

    def on_modified(self, event) -> None:
        # 移除: if getattr(self.app, '_b_watcher_paused', False): return
        if not event.is_directory and Path(event.src_path).suffix.lower() == ".strm":
            self._run_async(self.app.handle_b_created_or_modified, event.src_path)

    def on_deleted(self, event) -> None:
        # 移除: if getattr(self.app, '_b_watcher_paused', False): return
        if event.is_directory:
            return
        path = Path(event.src_path)
        suffix = path.suffix.lower()
        if suffix == ".strm":
            self._run_async(self.app.handle_b_deleted, event.src_path)
        # 隔离文件（.duplicate / .invalid）被删除时触发对应的 DB 行清理
        elif suffix in (".duplicate", ".invalid"):
            # 去掉隔离后缀后得到原始路径，再匹配 DB 记录
            orig_path = str(path.with_suffix(""))
            self._run_async(self.app.handle_b_deleted, orig_path)

    def on_moved(self, event) -> None:
        # 移除: if getattr(self.app, '_b_watcher_paused', False): return
        if event.is_directory:
            return

        src_path = event.src_path
        dest_path = event.dest_path

        src_is_strm = Path(src_path).suffix.lower() == ".strm"
        dst_is_strm = Path(dest_path).suffix.lower() == ".strm"

        if src_is_strm and dst_is_strm:
            # .strm 重命名为 .strm 统一异步化 + 双路径锁。
            # 原同步调用在 watchdog 事件线程内执行，与同路径的 created/modified/deleted
            # 异步处理线程竞争，导致 move_b_record 的 SELECT→INSERT/DELETE 序列
            # 与并发插入/删除产生丢失更新（复活已删行 / 删掉刚插入的新行）。
            # 现统一走 _run_async，由 AppService.handle_b_moved 取双路径锁后执行。
            self._run_async(self.app.handle_b_moved, src_path, dest_path)
        elif src_is_strm and not dst_is_strm:
            # .strm 重命名为非 .strm：等同于删除
            self._run_async(self.app.handle_b_renamed_to_non_strm, event.src_path)
        elif not src_is_strm and dst_is_strm:
            # 非 .strm 重命名为 .strm：等同于新建
            self._run_async(self.app.handle_b_created_or_modified, event.dest_path)

class CAreaEventHandler(FileSystemEventHandler):
    def __init__(self, app) -> None:
        self.app = app

    def on_deleted(self, event) -> None:
        if not event.is_directory and Path(event.src_path).suffix.lower() == ".strm":
            # C 区幽灵文件删除事件：仅记录日志
            # （幽灵文件的管理由其他模块负责，此处不做处理）
            logging.info("[C区] 检测到幽灵文件删除: %s", Path(event.src_path).name)

    def on_created(self, event) -> None:
        if not event.is_directory and Path(event.src_path).suffix.lower() == ".strm":
            logging.info("[C区] 检测到幽灵文件新增: %s", Path(event.src_path).name)

    def on_moved(self, event) -> None:
        if event.is_directory:
            return

        src_is_strm = Path(event.src_path).suffix.lower() == ".strm"
        dst_is_strm = Path(event.dest_path).suffix.lower() == ".strm"

        if src_is_strm or dst_is_strm:
            logging.info(
                "[C区] 检测到幽灵文件移动: %s -> %s",
                Path(event.src_path).name,
                Path(event.dest_path).name,
            )
