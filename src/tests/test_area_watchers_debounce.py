"""C1 同路径去抖 + C3 降级告警聚合（c7 §二，红测先行）。

新用例注入小窗（0.2s）+ Event.wait 必达断言；既有用例（test_area_watchers.py、
test_log_issues_simulation.py）统一注入 debounce_seconds=0 保持原即时派发语义。
F-6 文案逐字节断言锁定基类迁移后的 A/B 区告警文本。
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from area_watchers import AAreaEventHandler, BAreaEventHandler


class MockEvent:
    """模拟 watchdog 事件（与 test_area_watchers.py 同构）"""

    def __init__(self, src_path: str, is_directory: bool = False, dest_path: str | None = None):
        self.src_path = src_path
        self.is_directory = is_directory
        self.dest_path = dest_path or ""


class TestDebouncedDispatch:
    """C1/C3 行为红测：去抖合并、窗满必达、max-defer、close 丢弃、告警聚合。"""

    def test_c1_same_path_events_merge_to_one(self):
        """同 path 窗内 4 次 modified -> handler 恰 1 次"""
        app = MagicMock()
        handler = AAreaEventHandler(app, debounce_seconds=0.2)
        done = threading.Event()
        app.handle_a_created_or_modified.side_effect = lambda *a: done.set()
        for _ in range(4):
            handler.on_modified(MockEvent("/b/x/S01E01.strm"))
            time.sleep(0.02)
        assert done.wait(timeout=3), "窗满必达失败"
        time.sleep(0.3)
        assert app.handle_a_created_or_modified.call_count == 1
        handler.close()

    def test_c1_different_paths_independent(self):
        """异 path 互不影响：各派发 1 次"""
        app = MagicMock()
        handler = AAreaEventHandler(app, debounce_seconds=0.2)
        done = threading.Event()
        app.handle_a_created_or_modified.side_effect = (
            lambda p: done.set() if p.endswith("b.strm") else None)
        handler.on_modified(MockEvent("/b/a.strm"))
        handler.on_modified(MockEvent("/b/b.strm"))
        assert done.wait(timeout=3)
        assert app.handle_a_created_or_modified.call_count == 2
        handler.close()

    def test_c1_deleted_immediate_not_debounced(self):
        """deleted 立即派发，不去抖"""
        app = MagicMock()
        handler = AAreaEventHandler(app, debounce_seconds=5.0)
        handler.on_deleted(MockEvent("/b/a.strm"))
        deadline = time.time() + 1.0
        while not app.handle_a_deleted.called and time.time() < deadline:
            time.sleep(0.01)
        assert app.handle_a_deleted.called
        handler.close()

    def test_c1_moved_immediate_not_debounced(self):
        """moved 立即派发（A 区：源删除 + 目标新增均即时）"""
        app = MagicMock()
        handler = AAreaEventHandler(app, debounce_seconds=5.0)
        handler.on_moved(MockEvent("/b/a.strm", dest_path="/b/c.strm"))
        deadline = time.time() + 1.0
        while not app.handle_a_deleted.called and time.time() < deadline:
            time.sleep(0.01)
        assert app.handle_a_deleted.called
        assert app.handle_a_created_or_modified.called
        handler.close()

    def test_c1_subtitle_immediate_not_debounced(self):
        """A 区字幕（非 .strm）不被去抖，立即派发（后缀门在派发层 helper 内）"""
        app = MagicMock()
        handler = AAreaEventHandler(app, debounce_seconds=5.0)
        handler.on_created(MockEvent("/a/x/S01E01.ass"))
        deadline = time.time() + 1.0
        while not app.handle_a_created_or_modified.called and time.time() < deadline:
            time.sleep(0.01)
        assert app.handle_a_created_or_modified.called
        handler.close()

    def test_c1_create_then_delete_dispatches_once_after_window(self):
        """c.9.2 Task 4：create→delete 竞态红测——A 区 on_created 进去抖窗，
        on_deleted 即时派发；窗满后 create 恰派发 1 次且路径逐字节正确。
        文件已删时下流 handle_a_created_or_modified 首行 exists() 守卫空转，
        本测锁定「事件不丢、不重复、路径不错」的派发层契约。"""
        app = MagicMock()
        handler = AAreaEventHandler(app, debounce_seconds=0.2)
        done = threading.Event()
        received: list[str] = []
        app.handle_a_created_or_modified.side_effect = (
            lambda p: (received.append(p), done.set())[1])
        handler.on_created(MockEvent("/b/gone/S01E01.strm"))
        handler.on_deleted(MockEvent("/b/gone/S01E01.strm"))
        # c.9.3 Task D（复查② R2）：on_deleted 经 _run_async 独立线程派发，
        # 禁止零等待断言——deadline 轮询（同文件既有模式）
        deleted_deadline = time.time() + 1.0
        while not app.handle_a_deleted.called and time.time() < deleted_deadline:
            time.sleep(0.01)
        assert app.handle_a_deleted.called, "deleted 应即时派发（不去抖）"
        assert done.wait(timeout=3.0), "窗满必达失败：create 事件在去抖窗后未派发"
        assert received == ["/b/gone/S01E01.strm"], received
        time.sleep(0.3)
        assert app.handle_a_created_or_modified.call_count == 1
        assert app.handle_a_deleted.call_count == 1
        handler.close()

    def test_c1_max_defer_hardcap(self):
        """连续 touch 重置窗超过 max-defer -> 至少执行一次（防饥饿）"""
        app = MagicMock()
        handler = AAreaEventHandler(app, debounce_seconds=0.3, max_defer_seconds=1.0)
        done = threading.Event()
        app.handle_a_created_or_modified.side_effect = lambda *a: done.set()
        start = time.time()
        while not done.is_set() and time.time() - start < 5:
            handler.on_modified(MockEvent("/b/x.strm"))
            time.sleep(0.1)
        assert done.wait(timeout=1.5), "max-defer 硬顶失效：事件被无限饥饿"
        assert time.time() - start < 4.5
        handler.close()

    def test_fb_window_expiry_must_fire(self):
        """F-b 窗满必达：单事件后 handler 必在窗内执行（拦截定时器丢失/续期错误）"""
        app = MagicMock()
        handler = AAreaEventHandler(app, debounce_seconds=0.2)
        done = threading.Event()
        app.handle_a_created_or_modified.side_effect = lambda *a: done.set()
        handler.on_created(MockEvent("/b/only.strm"))
        assert done.wait(timeout=2.0), "窗满必达失败：0.2s 窗内未派发"
        handler.close()

    def test_c1_close_drops_pending_with_info_log(self, caplog):
        """close 丢弃 pending（不 flush）+ INFO 计数行；close 后新事件不再派发"""
        app = MagicMock()
        handler = AAreaEventHandler(app, debounce_seconds=30)
        handler.on_created(MockEvent("/b/pending1.strm"))
        handler.on_created(MockEvent("/b/pending2.strm"))
        with caplog.at_level(logging.INFO):
            handler.close()
        assert not app.handle_a_created_or_modified.called
        assert any("丢弃" in r.message and "pending" in r.message.lower()
                   for r in caplog.records)
        handler.on_created(MockEvent("/b/after_close.strm"))
        time.sleep(0.2)
        assert not app.handle_a_created_or_modified.called

    def test_c1_closed_immediate_branch_drops_subtitle(self):
        """R-新1 停机第三面（c.9.2 Task 3）：close 后即时分支（字幕/非 .strm/
        debounce=0）不再派发——与定时器派发循环同源竞态的另一半"""
        app = MagicMock()
        handler = AAreaEventHandler(app, debounce_seconds=0)
        handler.close()
        handler.on_created(MockEvent("/a/x/S01E01.ass"))
        time.sleep(0.2)
        assert not app.handle_a_created_or_modified.called, \
            "close 后即时分支仍派发事件（停机竞态未闭合）"

    def test_c3_close_resets_aggregation_flags(self):
        """c.9.2 Task 3 Minor（防御性）：close 重置 C3 聚合窗状态"""
        app = MagicMock()
        app.handle_a_created_or_modified.__name__ = "handle_a_created_or_modified"
        handler = AAreaEventHandler(app, debounce_seconds=0, degrade_window_seconds=0.05)
        permits = [handler._async_semaphore.acquire(blocking=False) for _ in range(8)]
        assert all(permits)
        try:
            handler.on_modified(MockEvent("/b/flood.strm"))
            assert handler._degrade_count == 1
            handler.close()
            assert handler._degrade_count == 0
            assert handler._degrade_full_emitted is False
            assert handler._degrade_window_start == 0.0
        finally:
            for _ in permits:
                handler._async_semaphore.release()
            handler.close()

    def test_c3_degrade_window_aggregates(self, caplog):
        """C3：窗内 10 次降级 -> 1 完整 WARNING；窗满后下一条降级事件输出摘要行"""
        app = MagicMock()
        app.handle_a_created_or_modified.__name__ = "handle_a_created_or_modified"
        handler = AAreaEventHandler(app, debounce_seconds=0, degrade_window_seconds=0.05)
        permits = [handler._async_semaphore.acquire(blocking=False) for _ in range(8)]
        assert all(permits), "测试前置：占满信号量失败"
        try:
            with caplog.at_level(logging.WARNING):
                for _ in range(10):
                    handler.on_modified(MockEvent("/b/flood.strm"))
                full = [r for r in caplog.records if "已达上限" in r.message]
                assert len(full) == 1, f"窗内应仅 1 条完整 WARNING，实际 {len(full)}"
                assert not any("降级告警聚合" in r.message for r in caplog.records), \
                    "窗未满不应提前输出摘要"
            time.sleep(0.1)  # 过窗（50ms）
            n_before = len(caplog.records)
            with caplog.at_level(logging.WARNING):
                handler.on_modified(MockEvent("/b/flood2.strm"))
            new_records = caplog.records[n_before:]
            assert any("降级告警聚合" in r.message and "共 10 次" in r.message
                       for r in new_records), "窗满后未输出聚合摘要"
            assert any("已达上限" in r.message for r in new_records), \
                "新窗首条应输出完整 WARNING"
        finally:
            for _ in permits:
                handler._async_semaphore.release()
            handler.close()

    def test_f6_degrade_warning_text_exact(self, caplog):
        """F-6：A/B 区降级 WARNING 文案逐字节保持"""
        for cls, zone, func_name in [
                (AAreaEventHandler, "A区", "handle_a_created_or_modified"),
                (BAreaEventHandler, "B区", "handle_b_created_or_modified")]:
            app = MagicMock()
            getattr(app, func_name).__name__ = func_name
            handler = cls(app, debounce_seconds=0)
            permits = [handler._async_semaphore.acquire(blocking=False) for _ in range(8)]
            assert all(permits)
            with caplog.at_level(logging.WARNING):
                handler.on_modified(MockEvent("/x/f.strm"))
            expected = f"[{zone}] 并发处理线程已达上限(8)，事件同步执行降级: {func_name}"
            assert any(r.getMessage() == expected for r in caplog.records), expected
            for _ in permits:
                handler._async_semaphore.release()
            handler.close()

    def test_f6_exception_and_health_texts_exact(self, caplog):
        """F-6：A 区事件异常与健康告警文案逐字节保持"""
        app = MagicMock()
        app._watchers_healthy = True
        handler = AAreaEventHandler(app, debounce_seconds=0)
        app.handle_a_created_or_modified.side_effect = RuntimeError("boom")
        with caplog.at_level(logging.ERROR):
            handler.on_created(MockEvent("/x/f.strm"))
            deadline = time.time() + 1.0
            while not any("[A区事件处理异常]" in r.getMessage()
                          for r in caplog.records) and time.time() < deadline:
                time.sleep(0.01)
        assert any("[A区事件处理异常]" in r.getMessage() for r in caplog.records)
        handler._failure_count = handler._HEALTH_THRESHOLD - 1
        with caplog.at_level(logging.WARNING):
            handler._safe_call(app.handle_a_created_or_modified, "/x/f.strm")
        expected_health = "[A区] 连续失败 10 次，可能存在系统性问题，请检查日志"
        assert any(r.getMessage() == expected_health for r in caplog.records)
        assert app._watchers_healthy is False

    def test_f6_b_zone_exception_text_exact(self, caplog):
        """F-6：B 区事件异常文案逐字节保持"""
        app = MagicMock()
        handler = BAreaEventHandler(app, debounce_seconds=0)
        app.handle_b_created_or_modified.side_effect = RuntimeError("boom")
        with caplog.at_level(logging.ERROR):
            handler.on_created(MockEvent("/x/f.strm"))
            deadline = time.time() + 1.0
            while not any("[B区事件处理异常]" in r.getMessage()
                          for r in caplog.records) and time.time() < deadline:
                time.sleep(0.01)
        assert any("[B区事件处理异常]" in r.getMessage() for r in caplog.records)

    def test_f6_b_zone_health_text_exact(self, caplog):
        """F-6（c.9.2 Task 7 Minor）：B 区健康告警文案逐字节保持——
        原 6 文案锁 5 条，本例补齐第 6 条（B 区连续失败阈值告警）"""
        app = MagicMock()
        app._watchers_healthy = True
        handler = BAreaEventHandler(app, debounce_seconds=0)
        # 与 A 区健康文案同构：必须让 handler 真实抛错（成功分支会把
        # _failure_count 归零、永不触达告警分支）
        app.handle_b_created_or_modified.side_effect = RuntimeError("boom")
        with caplog.at_level(logging.ERROR):
            handler._safe_call(app.handle_b_created_or_modified, "/x/f.strm")
        handler._failure_count = handler._HEALTH_THRESHOLD - 1
        with caplog.at_level(logging.WARNING):
            handler._safe_call(app.handle_b_created_or_modified, "/x/f.strm")
        expected_health = "[B区] 连续失败 10 次，可能存在系统性问题，请检查日志"
        assert any(r.getMessage() == expected_health for r in caplog.records), \
            [r.getMessage() for r in caplog.records]
        assert app._watchers_healthy is False
