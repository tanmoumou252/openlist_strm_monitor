from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler

from utils import make_strm_fingerprint, read_strm_webdav_path


class _DebouncedFileEventHandler(FileSystemEventHandler):
    """A/B 区事件处理共享基类（c7 缺陷 C：C1 去抖 + C3 告警聚合）。

    职责（自 AAreaEventHandler/BAreaEventHandler 复制粘贴合并迁移）：
    - `_run_async` 并发上限 + 信号量背压：`BoundedSemaphore(8)` 数值不动，
      降级仍在 watchdog 派发线程内同步执行（有意背压，事件不丢失）；
    - `_safe_call` 吞异常契约（抛出将杀死 watchdog 线程，勿改 re-raise）；
    - 健康信号 `_failure_count`/`_watchers_healthy`（dashboard 消费）；
    - C3 降级告警 60s 聚合窗：窗内首条完整 WARNING，其后仅计数，窗满后由
      下一条降级事件输出摘要行（M-13：窗口状态以实例级锁保护，防
      每事件新建线程下的并发竞态）。只改告警呈现，不改派发/降级/信号量行为；
    - C1 同路径去抖（仅 .strm，后缀门在派发层 `_submit_or_debounce` 内）：
      单一常驻定时器 + pending dict 惰性到期扫描（禁按 path 逐建
      threading.Timer——引擎批量重写风暴万级文件下会堆积数千 Timer）；
      重置式窗口 + max-defer 硬顶防饥饿；`on_deleted`/`on_moved` 不去抖；
      字幕（非 .strm）与 B 区已滤后缀路径不受影响。F-a 有意取舍：
      .strm 单事件首派发延迟 = debounce 窗长（默认 3s）。

    红线：告警文案逐字节保持（`_zone_label` 类属性参数化 [A区]/[B区] 前缀，
    F-6 红测锁定）。
    """

    # 事件处理线程并发上限。事件风暴时不再无限创建线程，
    # 超出上限的事件降级为在 watchdog 派发线程内同步执行（背压），
    # 保证事件不丢失同时限制线程数。
    _MAX_ASYNC_THREADS = 8
    # C1 默认去抖窗长（c.6 论证：OpenList 引擎单批重写 3-5s 窗口）
    _DEBOUNCE_SECONDS_DEFAULT = 3.0
    # C1 重置式窗口的 max-defer 硬顶（防连续 touch 饥饿）
    _MAX_DEFER_SECONDS_DEFAULT = 30.0
    # C3 降级告警聚合窗长
    _DEGRADE_WINDOW_SECONDS_DEFAULT = 60.0
    # 派生类必须覆盖：日志/告警的区标签（"A区"/"B区"）
    _zone_label: str = "区"

    def __init__(
        self,
        app,
        *,
        debounce_seconds: float | None = None,
        max_defer_seconds: float | None = None,
        degrade_window_seconds: float | None = None,
    ) -> None:
        self.app = app
        # 健康信号 - 失败计数
        self._failure_count = 0
        self._last_failure_time = 0
        self._health_lock = threading.Lock()
        self._HEALTH_THRESHOLD = 10  # 连续失败阈值
        self._async_semaphore = threading.BoundedSemaphore(self._MAX_ASYNC_THREADS)
        # C1/C3 可注入参数（测试置 0 / 小窗）
        self._debounce_seconds = (
            self._DEBOUNCE_SECONDS_DEFAULT if debounce_seconds is None else debounce_seconds)
        self._max_defer_seconds = (
            self._MAX_DEFER_SECONDS_DEFAULT if max_defer_seconds is None else max_defer_seconds)
        self._degrade_window_seconds = (
            self._DEGRADE_WINDOW_SECONDS_DEFAULT if degrade_window_seconds is None
            else degrade_window_seconds)
        # C1 pending dict：path -> (first_seen, deadline, func, args)
        self._pending: dict[str, tuple[float, float, object, tuple]] = {}
        self._pending_lock = threading.Lock()  # M-13：实例级锁保护 pending/定时器状态
        self._debounce_timer: threading.Timer | None = None
        self._closed = False
        # C3 聚合窗状态（M-13：独立实例级锁）
        self._degrade_lock = threading.Lock()
        self._degrade_window_start = 0.0
        self._degrade_count = 0
        self._degrade_full_emitted = False

    # ==================== C1 同路径去抖（派发层入口） ====================

    def _submit_or_debounce(self, func, path, *rest) -> None:
        """created/modified 派发入口：.strm 且未关闭时同 path 合并窗，否则立即派发。

        后缀门必须落在本 helper（派发层）内：A 区 handler 无任何后缀过滤，
        非 .strm（含字幕 .ass/.srt/.ssa）在此即时派发；B 区 handler 已滤
        .strm，双重判断语义一致。
        """
        if self._debounce_seconds <= 0 or not str(path).lower().endswith(".strm"):
            # R-新1 停机第三面（c.9.2 Task 3）：即时分支与定时器派发循环
            # 同源——close 后字幕/debounce=0 事件照发会拖慢停机路径，
            # 丢弃由下次启动 boundary 单扫 + A→B 全量同步兜底。
            if self._closed:
                return
            self._run_async(func, path, *rest)
            return
        now = time.monotonic()
        with self._pending_lock:
            if self._closed:
                # close 后事件直接丢弃（停机路径，由下次启动 boundary 单扫兜底）
                return
            prev = self._pending.get(path)
            # 重置式窗口：deadline = now + 窗长；但首见起算不超过 max-defer 硬顶
            first_seen = prev[0] if prev is not None else now
            deadline = min(now + self._debounce_seconds, first_seen + self._max_defer_seconds)
            self._pending[path] = (first_seen, deadline, func, (path, *rest))
            self._ensure_timer_locked(now)

    def _ensure_timer_locked(self, now: float) -> None:
        """单一常驻定时器：仅在无定时器时按最近 deadline 排期（锁内调用）。"""
        if self._debounce_timer is not None or self._closed or not self._pending:
            return
        next_deadline = min(e[1] for e in self._pending.values())
        timer = threading.Timer(max(0.0, next_deadline - now), self._on_debounce_timer)
        timer.daemon = True
        self._debounce_timer = timer
        timer.start()

    def _on_debounce_timer(self) -> None:
        """惰性到期扫描：锁内收集到期项并自排后续，锁外派发（勿持锁执行慢 handler）。"""
        due: list[tuple[str, object, tuple]] = []
        with self._pending_lock:
            self._debounce_timer = None
            if self._closed:
                return
            now = time.monotonic()
            for p, e in list(self._pending.items()):
                if e[1] <= now:
                    due.append((p, e[2], e[3]))
                    del self._pending[p]
            if self._pending:
                self._ensure_timer_locked(now)
        for _p, func, args in due:
            self._run_async(func, *args)

    def close(self) -> None:
        """observer stop 同批调用（stop_watchers）：丢弃而非 flush pending。

        v7 语义明示：flush 会在停机路径同步执行慢 handler（fp 锁内
        check_exists HTTP）拖死关闭流程；丢弃事件由下次启动 boundary 单扫
        （`_reconcile_boundary_catch_up`）+ A→B 全量同步双重兜底恢复。
        同时 flush 未满窗的 C3 聚合摘要。
        """
        with self._pending_lock:
            self._closed = True
            dropped = len(self._pending)
            self._pending.clear()
            timer, self._debounce_timer = self._debounce_timer, None
        if timer is not None:
            timer.cancel()
        if dropped:
            logging.info(
                "[%s] 停机丢弃 %d 个 pending 去抖事件（由下次启动 boundary 单扫 + A→B 全量同步兜底恢复）",
                self._zone_label, dropped)
        # C3 未满窗摘要 flush（若有聚合计数）
        with self._degrade_lock:
            count = self._degrade_count
            self._degrade_count = 0
            # c.9.2 Task 3 Minor（防御性）：重置聚合窗状态，stop() 每轮新建
            # handler 实例下属死代码，但防未来实例复用时窗状态跨生命周期泄漏。
            self._degrade_full_emitted = False
            self._degrade_window_start = 0.0
        if count > 1:
            logging.warning(
                "[%s] 降级告警聚合: 停机前窗内共 %d 次同步降级（首条已完整输出）",
                self._zone_label, count)

    # ==================== C3 降级告警聚合 ====================

    def _record_degrade_warning(self, func_name: str) -> None:
        """信号量耗尽降级的告警呈现（C3 聚合窗，M-13 实例级锁）。

        窗内首条完整 WARNING（文案逐字节保持），其后仅计数；窗满后由
        下一条降级事件先输出摘要行再开新窗。
        """
        now = time.monotonic()
        with self._degrade_lock:
            if now - self._degrade_window_start > self._degrade_window_seconds:
                # 窗满：先输出上一窗摘要（若有聚合计数），再开新窗
                if self._degrade_count > 1:
                    logging.warning(
                        "[%s] 降级告警聚合: 过去 %.0fs 窗内共 %d 次同步降级（首条已完整输出）",
                        self._zone_label, self._degrade_window_seconds, self._degrade_count)
                self._degrade_window_start = now
                self._degrade_count = 0
                self._degrade_full_emitted = False
            if not self._degrade_full_emitted:
                logging.warning(
                    "[%s] 并发处理线程已达上限(%d)，事件同步执行降级: %s",
                    self._zone_label, self._MAX_ASYNC_THREADS, func_name)
                self._degrade_full_emitted = True
            self._degrade_count += 1

    # ==================== 并发派发 + 健康信号（自 A/B 区合并迁移） ====================

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
            # 事件不丢失，也不突破线程上限。（C3：告警经聚合窗呈现）
            self._record_degrade_warning(getattr(func, "__name__", repr(func)))
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
            logging.exception(
                f"[{self._zone_label}事件处理异常] %s args=%s (连续失败: %d)",
                getattr(func, "__name__", repr(func)), args, failure_count)

            # 超过阈值时发出警告并标记健康状态
            if failure_count >= self._HEALTH_THRESHOLD:
                logging.warning(
                    "[%s] 连续失败 %d 次，可能存在系统性问题，请检查日志",
                    self._zone_label, failure_count
                )
                # 此标志由 dashboard 状态 API 消费，勿当死代码删除
                if hasattr(self.app, '_watchers_healthy'):
                    self.app._watchers_healthy = False


class AAreaEventHandler(_DebouncedFileEventHandler):
    _zone_label = "A区"

    def on_created(self, event) -> None:
        # 不过滤扩展名：字幕文件（.ass/.ssa/.srt）由 handle_a_created_or_modified
        # 内部的 is_subtitle_file 分流处理；非字幕非 STRM 文件会在该方法中安全跳过。
        # C1：.strm 同路径合并窗，其余即时（后缀门在 _submit_or_debounce 内）。
        if not event.is_directory:
            self._submit_or_debounce(self.app.handle_a_created_or_modified, event.src_path)

    def on_modified(self, event) -> None:
        if not event.is_directory:
            self._submit_or_debounce(self.app.handle_a_created_or_modified, event.src_path)

    def on_deleted(self, event) -> None:
        if not event.is_directory:
            self._run_async(self.app.handle_a_deleted, event.src_path)

    def on_moved(self, event) -> None:
        if event.is_directory:
            return
        # A 区移动：源路径视为删除，目标路径视为新增（均即时，不去抖）
        self._run_async(self.app.handle_a_deleted, event.src_path)
        self._run_async(self.app.handle_a_created_or_modified, event.dest_path)

class BAreaEventHandler(_DebouncedFileEventHandler):
    _zone_label = "B区"

    def on_created(self, event) -> None:
        # 移除: if getattr(self.app, '_b_watcher_paused', False): return
        if not event.is_directory and Path(event.src_path).suffix.lower() == ".strm":
            self._submit_or_debounce(self.app.handle_b_created_or_modified, event.src_path)

    def on_modified(self, event) -> None:
        # 移除: if getattr(self.app, '_b_watcher_paused', False): return
        if not event.is_directory and Path(event.src_path).suffix.lower() == ".strm":
            self._submit_or_debounce(self.app.handle_b_created_or_modified, event.src_path)

    def on_deleted(self, event) -> None:
        # 移除: if getattr(self.app, '_b_watcher_paused', False): return
        if event.is_directory:
            return
        path = Path(event.src_path)
        suffix = path.suffix.lower()
        if suffix == ".strm":
            self._run_async(self.app.handle_b_deleted, event.src_path)
        elif ".duplicate" in path.name.lower() or ".invalid" in path.name.lower() or ".quarantined" in path.name.lower():
            # 必须保留实际隔离路径，禁止剥离后缀后走普通用户删除链路。
            self._run_async(self.app.handle_b_quarantined_deleted, event.src_path)

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
