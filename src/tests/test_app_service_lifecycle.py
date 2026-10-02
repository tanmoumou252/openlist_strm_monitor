"""
AppService 生命周期与 watchdog 编排测试。

隔离覆盖 AppService.start() / stop() / start_watchers() 的当前编排契约：
- 配置未就绪时的 fail-safe（不启动 watcher / refresh service / 后续扫描）
- 配置就绪时关键启动阶段的实际调用顺序
- start_watchers() 对 A/B/C 根的 schedule 与 observer.start()
- stop() 的 pending cleanup 取消、refresh service 停止、observer stop/join
- 重复 stop、未启动即 stop 不抛异常

测试策略：
- 使用 MagicMock 替换 watchdog Observer，绝不启动真实 watchdog
- 使用临时目录作为 A/B/C 根，mock Database / admin_api
- 用 patch.object 替换各启动阶段方法，只验证编排顺序，不执行真实扫描
- 不访问真实 OpenList / TMDB / 用户媒体目录

注意：本文件只固化当前实现已承诺的行为。当前 stop() 没有显式
_running = False、join timeout 或通用异常回滚；start() 也没有部分启动
失败的回滚逻辑，测试不把这些未实现行为当作既定契约。
"""
from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

# 冗余保护：确保 src/ 在 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app_service_core import AppService  # noqa: E402
from config import ABMapping, AppConfig  # noqa: E402
from database import Database  # noqa: E402
from domain.sync.sync_service import SyncService  # noqa: E402
from _test_helpers import FakeConfigDb  # noqa: E402


# ============================================================
# 公共构造
# ============================================================

# start() 中位于配置检查之后的重量级阶段方法，测试时统一 patch 掉
_START_PHASES = (
    "sync_protected_roots_from_config",
    "scan_removed_protected_roots",
    "persist_current_roots_snapshot",
    "initial_scan_a",
    "initial_scan_b",
    "scan_a_to_b_full_sync",
    "start_watchers",
    "_start_subtitle_scan_background",
    "update_engine_configs",
    "_cleanup_startup_duplicates",
)


class _LifecycleBase:
    """提供临时 A/B/C 根 + 最小 AppService 的公共 setup。"""

    #: 是否配置有效的 mapping（子类可覆盖以构造未就绪配置）
    with_valid_mapping = True

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        if self.with_valid_mapping:
            config.a_b_mappings = [ABMapping(
                mapping_id="m1",
                a_root=str(self.a_dir),
                b_root=str(self.b_dir))]
        else:
            config.a_b_mappings = []
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.paths.strm_engine_paths = []
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        # 避免 start() 真的 sleep
        config.behavior.sync_on_startup_wait = 0
        config.behavior.sync_on_startup = True
        config.strm_engine_paths = []

        self.config = config
        self.db = MagicMock(spec=Database)
        self.admin_api = Mock()

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, self.db, self.admin_api)

    def teardown_method(self):
        # 保险：确保测试不会留下运行中的 observer
        observer = getattr(self.app, "observer", None)
        if observer is not None and not isinstance(observer, MagicMock):
            try:
                if observer.is_alive():
                    observer.stop()
                    observer.join(timeout=1)
            except Exception:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch_phases(self, extra: tuple[str, ...] = ()):
        """patch 掉所有重量级启动阶段，返回 (context_manager, mocks dict)。

        显式持有 mock 字典，因为 patch.multiple 传入具体对象时
        不会把它们放入 as 目标（仅 DEFAULT 哨兵才会）。
        """
        mocks = {name: MagicMock() for name in _START_PHASES + extra}
        return patch.multiple(self.app, **mocks), mocks


# ============================================================
# start()：配置未就绪 fail-safe
# ============================================================


class TestStartFailSafeWhenNotConfigured(_LifecycleBase):
    """未配置 mapping 时 start() 必须 fail-safe 退出。"""

    with_valid_mapping = False

    def test_start_returns_early_without_side_effects(self):
        """配置未就绪：不扫描、不启动 watcher、不启动 refresh service。"""
        ctx, mocks = self._patch_phases()
        with ctx:
            self.app.start()

        for name in _START_PHASES:
            mocks[name].assert_not_called()

        self.app.refresh_service.start.assert_not_called()

    def test_start_still_prepares_environment_and_db(self):
        """配置检查发生在 prepare_environment 与 init_db 之后。"""
        ctx, _ = self._patch_phases()
        with patch.object(self.app, "prepare_environment") as mock_prepare, ctx:
            self.app.start()

        mock_prepare.assert_called_once()
        self.db.init_db.assert_called_once()

    def test_start_sets_running_false(self):
        """fail-safe 分支显式标记 _running = False。"""
        ctx, _ = self._patch_phases()
        with ctx:
            self.app.start()

        assert self.app._running is False

    def test_start_does_not_write_full_audit_timestamp(self):
        """未就绪时不得写入 last_full_audit_at。"""
        ctx, _ = self._patch_phases()
        with ctx:
            self.app.start()

        set_control_keys = [
            c.args[0] for c in self.db.set_control.call_args_list]
        assert "last_full_audit_at" not in set_control_keys


# ============================================================
# start()：配置就绪时的编排顺序
# ============================================================


class TestStartOrchestrationOrder(_LifecycleBase):
    """配置就绪时验证关键启动阶段的实际调用顺序。"""

    def _run_start_recording_order(self, **overrides):
        """执行 start() 并返回被调用阶段的顺序列表。"""
        order: list[str] = []
        patches = {}
        for name in _START_PHASES + ("prepare_environment",):
            patches[name] = MagicMock(
                side_effect=lambda *a, _n=name, **kw: order.append(_n))
        patches.update(overrides)

        self.db.init_db.side_effect = lambda: order.append("init_db")
        # c.9.2 Task 11：start(defer_first_cycle=True) 传参——零参 lambda 会
        # 直接 TypeError（*a, **kw 对 assert_called_once 断言无影响）
        self.app.refresh_service.start.side_effect = (
            lambda *a, **kw: order.append("refresh_service.start"))

        with patch.multiple(self.app, **patches):
            self.app.start()
        return order

    def test_key_phase_order(self):
        """顺序：环境 → DB → 保护根 → A 索引 → B 扫描 → A→B → watcher → 字幕后台线程 → refresh。"""
        order = self._run_start_recording_order()

        expected = [
            "prepare_environment",
            "init_db",
            "update_engine_configs",
            "sync_protected_roots_from_config",
            "scan_removed_protected_roots",
            "persist_current_roots_snapshot",
            "initial_scan_a",
            "initial_scan_b",
            "scan_a_to_b_full_sync",
            "_cleanup_startup_duplicates",
            "start_watchers",
            "_start_subtitle_scan_background",
            "refresh_service.start",
        ]
        assert order == expected

    def test_watcher_starts_before_refresh_service(self):
        """watcher 必须先于 refresh service 启动。"""
        order = self._run_start_recording_order()
        assert order.index("start_watchers") < order.index(
            "refresh_service.start")

    def test_a_scan_precedes_b_scan(self):
        """A 区索引先于 B 区扫描，保证历史血统核对命中完整 A 源。"""
        order = self._run_start_recording_order()
        assert order.index("initial_scan_a") < order.index("initial_scan_b")

    def test_invalid_subtitles_cleaned_in_background_worker(self):
        """启动干道不执行字幕清理，字幕后台 Worker 负责清理。"""
        ctx, _ = self._patch_phases()
        with ctx, patch.object(self.app, "_start_subtitle_scan_background"):
            self.app.start()

        self.db.cleanup_invalid_subtitles.assert_not_called()

        self.app._start_subtitle_scan_background()
        self.db.cleanup_invalid_subtitles.assert_called_once_with(
            cancel_event=self.app._subtitle_scan_cancel_event)

    def test_full_audit_timestamp_persisted(self):
        """启动完成一次全量 A 区审计后写入 last_full_audit_at。"""
        ctx, _ = self._patch_phases()
        with ctx:
            self.app.start()

        set_control_keys = [
            c.args[0] for c in self.db.set_control.call_args_list]
        assert "last_full_audit_at" in set_control_keys

    def test_start_does_not_call_global_redundant_cleanups(self):
        """启动不得触发全局冗余清理（destructive，不属于启动编排）。"""
        ctx, _ = self._patch_phases()
        with ctx, \
                patch.object(self.app, "cleanup_a_redundant_using_api") as mock_a, \
                patch.object(self.app, "cleanup_b_redundant") as mock_b:
            self.app.start()

        mock_a.assert_not_called()
        mock_b.assert_not_called()

    def test_sync_on_startup_false_skips_full_sync(self):
        """sync_on_startup=false 时跳过 A→B 全量同步，但仍启动 watcher。"""
        self.config.behavior.sync_on_startup = False

        ctx, mocks = self._patch_phases()
        with ctx:
            self.app.start()

        mocks["scan_a_to_b_full_sync"].assert_not_called()
        mocks["start_watchers"].assert_called_once()
        self.app.refresh_service.start.assert_called_once()

    def test_audit_timestamp_failure_does_not_block_startup(self):
        """写入审计时间失败（OSError）不阻断 watcher 与 refresh service 启动。"""
        self.db.set_control.side_effect = OSError("disk full")

        ctx, mocks = self._patch_phases()
        with ctx:
            self.app.start()

        mocks["start_watchers"].assert_called_once()
        self.app.refresh_service.start.assert_called_once()


# ============================================================
# start_watchers()
# ============================================================


class TestStartWatchers(_LifecycleBase):
    """start_watchers() 的 observer 创建与 schedule 编排。"""

    def test_not_ready_creates_no_observer(self):
        """配置未就绪时不创建 observer。"""
        # 制造未就绪配置：mapping 缺少唯一 ID
        self.app.a_b_mappings = []

        with patch("watchdog.observers.Observer") as mock_observer_cls:
            self.app.start_watchers()

        mock_observer_cls.assert_not_called()
        assert self.app.observer is None

    def test_schedules_a_b_and_c_roots_then_starts(self):
        """配置就绪时为 A/B/C 根 schedule 并调用 observer.start()。"""
        mock_observer = MagicMock()

        with patch("watchdog.observers.Observer", return_value=mock_observer):
            self.app.start_watchers()

        scheduled_paths = [
            c.args[1] for c in mock_observer.schedule.call_args_list]
        assert str(self.a_dir) in scheduled_paths
        assert str(self.b_dir) in scheduled_paths
        assert str(self.c_dir) in scheduled_paths
        mock_observer.start.assert_called_once()
        assert self.app.observer is mock_observer

    def test_schedules_are_recursive(self):
        """所有 schedule 均使用 recursive=True。"""
        mock_observer = MagicMock()

        with patch("watchdog.observers.Observer", return_value=mock_observer):
            self.app.start_watchers()

        for call in mock_observer.schedule.call_args_list:
            assert call.kwargs.get("recursive") is True

    def test_missing_a_root_skipped_but_b_c_still_scheduled(self):
        """A 根不存在时跳过该根，但 B/C 仍被监控且 observer 仍启动。"""
        shutil.rmtree(self.a_dir)
        mock_observer = MagicMock()

        with patch("watchdog.observers.Observer", return_value=mock_observer):
            self.app.start_watchers()

        scheduled_paths = [
            c.args[1] for c in mock_observer.schedule.call_args_list]
        assert str(self.a_dir) not in scheduled_paths
        assert str(self.b_dir) in scheduled_paths
        assert str(self.c_dir) in scheduled_paths
        mock_observer.start.assert_called_once()

    def test_creates_missing_b_and_c_roots(self):
        """B/C 根缺失时会被创建（watcher 需要真实目录）。"""
        shutil.rmtree(self.b_dir)
        shutil.rmtree(self.c_dir)
        mock_observer = MagicMock()

        with patch("watchdog.observers.Observer", return_value=mock_observer):
            self.app.start_watchers()

        assert self.b_dir.is_dir()
        assert self.c_dir.is_dir()

    def test_no_real_watchdog_thread_started(self):
        """使用 mock Observer 时不产生真实 watchdog 线程。"""
        mock_observer = MagicMock()

        with patch("watchdog.observers.Observer", return_value=mock_observer):
            self.app.start_watchers()

        # observer 是 mock，未启动真实线程
        assert isinstance(self.app.observer, MagicMock)


class TestWatchersLive(_LifecycleBase):
    """_watchers_live() 派生判定语义（T1，C12-1：零新增状态源）。

    会话批量隔离路径以 not _watchers_live() 门控（启动上下文专用），
    判定必须与 stop() 的 observer.is_alive() 现行语义同源。
    """

    def test_false_when_observer_none(self):
        """构造后（observer 为 None）→ False（启动窗口语义）。"""
        assert self.app.observer is None
        assert self.app._watchers_live() is False

    def test_true_after_start_watchers_success(self):
        """start_watchers() 成功（observer.start() 后 is_alive() 为 True）→ True。"""
        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = True

        with patch("watchdog.observers.Observer", return_value=mock_observer):
            self.app.start_watchers()

        assert self.app._watchers_live() is True

    def test_false_when_config_not_ready_early_return(self):
        """config 未就绪提前 return → observer 保持 None → False。"""
        self.app.a_b_mappings = []

        with patch("watchdog.observers.Observer") as mock_observer_cls:
            self.app.start_watchers()

        mock_observer_cls.assert_not_called()
        assert self.app.observer is None
        assert self.app._watchers_live() is False

    def test_false_after_stop(self):
        """stop() 后 observer.is_alive() 为 False → False。"""
        mock_observer = MagicMock()
        # stop() 分支判断（True 进入 stop/join），此后判定为 False
        mock_observer.is_alive.side_effect = [True, False, False]
        self.app.observer = mock_observer

        self.app.stop()

        assert self.app._watchers_live() is False

    def test_false_when_observer_dead(self):
        """observer 存在但已死亡（is_alive False）→ False。"""
        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = False
        self.app.observer = mock_observer
        assert self.app._watchers_live() is False


# ============================================================
# stop()
# ============================================================


class TestStop(_LifecycleBase):
    """stop() 的资源清理契约。"""

    def test_stop_without_start_does_not_raise(self):
        """从未启动（observer 为 None）时 stop() 不抛异常。"""
        assert self.app.observer is None
        self.app.stop()  # 不应抛异常
        self.app.refresh_service.stop.assert_called_once()

    def test_stop_stops_refresh_service_and_observer(self):
        """observer alive 时调用 refresh stop + observer stop + join。"""
        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = True
        self.app.observer = mock_observer

        self.app.stop()

        self.app.refresh_service.stop.assert_called_once()
        mock_observer.stop.assert_called_once()
        mock_observer.join.assert_called_once()

    def test_stop_skips_dead_observer(self):
        """observer 已停止（非 alive）时不重复 stop/join。"""
        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = False
        self.app.observer = mock_observer

        self.app.stop()

        self.app.refresh_service.stop.assert_called_once()
        mock_observer.stop.assert_not_called()
        mock_observer.join.assert_not_called()

    def test_repeated_stop_does_not_raise(self):
        """重复 stop()：第二次 observer 已非 alive，不抛异常、不重复 join。"""
        mock_observer = MagicMock()
        alive_states = [True, False]
        mock_observer.is_alive.side_effect = lambda: alive_states.pop(0)
        self.app.observer = mock_observer

        self.app.stop()
        self.app.stop()  # 不应抛异常

        assert self.app.refresh_service.stop.call_count == 2
        mock_observer.stop.assert_called_once()
        mock_observer.join.assert_called_once()

    def test_stop_idle_repeatedly_is_safe(self):
        """空闲态连续多次 stop() 幂等且不抛异常。"""
        for _ in range(3):
            self.app.stop()
        assert self.app.refresh_service.stop.call_count == 3

    def test_stop_after_ready_repeatedly_is_safe(self):
        """进入 READY 态后连续多次 stop() 幂等安全。"""
        self.app._running = True
        self.app.set_phase("ready")
        for _ in range(3):
            self.app.stop()
        assert self.app._running is False

    def test_stop_cancels_pending_cleanup_timers(self):
        """stop() 取消所有待执行的延迟清理定时器并清空登记表。"""
        timer_a = MagicMock()
        timer_b = MagicMock()
        with self.app._cleanup_lock:
            self.app._pending_cleanups["/x/a.strm"] = timer_a
            self.app._pending_cleanups["/x/b.strm"] = timer_b

        self.app.stop()

        timer_a.cancel.assert_called_once()
        timer_b.cancel.assert_called_once()
        assert self.app._pending_cleanups == {}

    def test_stop_after_start_watchers_stops_mock_observer(self):
        """start_watchers() 后 stop() 停止同一个 observer 实例。"""
        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = True

        with patch("watchdog.observers.Observer", return_value=mock_observer):
            self.app.start_watchers()
        self.app.stop()

        mock_observer.start.assert_called_once()
        mock_observer.stop.assert_called_once()
        mock_observer.join.assert_called_once()


# ============================================================
# 生命周期状态机与进度
# ============================================================


class TestEngineStateLifecycle(_LifecycleBase):
    """验证启动阶段对外暴露的原子状态快照。"""

    def test_engine_state_lifecycle_transitions(self):
        """验证 AppService 细粒度状态机转换与只读快照。"""
        summary = self.app.get_state_summary()
        assert summary["phase"] == "stopped"
        assert summary["is_running"] is False
        assert summary["is_ready"] is False

        self.app.set_phase("starting")
        summary = self.app.get_state_summary()
        assert summary["phase"] == "starting"
        assert summary["is_running"] is True
        assert summary["is_ready"] is False

        self.app.set_phase("authenticating")
        assert self.app.get_state_summary()["phase"] == "authenticating"

        self.app.set_phase("scanning_a")
        self.app.update_progress(a_indexed=150, a_discovered=300)
        summary = self.app.get_state_summary()
        assert summary["phase"] == "scanning_a"
        assert summary["progress"]["a_indexed"] == 150
        assert summary["progress"]["a_discovered"] == 300

        self.app.set_phase("ready")
        summary = self.app.get_state_summary()
        assert summary["phase"] == "ready"
        assert summary["is_running"] is True
        assert summary["is_ready"] is True

        self.app.set_phase("fail_safe", error="认证超时")
        summary = self.app.get_state_summary()
        assert summary["phase"] == "fail_safe"
        assert summary["is_running"] is False
        assert summary["is_ready"] is False
        assert summary["error"] == "认证超时"


# ============================================================
# 未覆盖行为的显式记录
# ============================================================


class TestLifecycleKnownGaps(_LifecycleBase):
    """记录当前实现未提供的生命周期保证，避免后续误认为已有契约。

    这些不是缺陷断言，而是把"当前没有该行为"固化下来，
    防止文档或后续测试虚构生产承诺。
    """

    def test_stop_resets_running_flag_and_joins_subtitle_thread(self):
        """stop() 取消后台字幕线程并反映真实运行状态。"""
        self.app._running = True
        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = False
        self.app.observer = mock_observer
        self.app.stop()
        assert self.app._running is False
        assert self.app._subtitle_scan_cancel_event.is_set()

    def test_startup_wait_can_be_cancelled(self):
        """停止请求能够中断启动等待，不继续进入扫描阶段。"""
        import threading
        import time
        self.config.behavior.sync_on_startup_wait = 60
        with patch.object(self.app, "prepare_environment"), \
                patch.object(self.app, "update_engine_configs"), \
                patch.object(self.app, "initial_scan_a") as scan_a:
            thread = threading.Thread(target=self.app.start)
            thread.start()
            time.sleep(0.05)
            self.app.stop()
            thread.join(timeout=2)
        assert not thread.is_alive()
        scan_a.assert_not_called()
        assert self.app._running is False

    def test_stop_join_has_no_timeout_argument(self):
        """当前 stop() 调用 observer.join() 不带 timeout。"""
        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = True
        self.app.observer = mock_observer

        self.app.stop()

        mock_observer.join.assert_called_once_with()

    def test_start_watchers_has_no_partial_failure_rollback(self):
        """start_watchers() 中途 schedule 失败会向上抛出，当前没有回滚已注册的 watch。

        观测到的行为：异常传播、observer 已被赋值、observer.start() 未被调用。
        """
        mock_observer = MagicMock()
        mock_observer.schedule.side_effect = OSError("inotify limit reached")

        with patch("watchdog.observers.Observer", return_value=mock_observer):
            with pytest.raises(OSError):
                self.app.start_watchers()

        assert self.app.observer is mock_observer
        mock_observer.start.assert_not_called()


class TestStartCatchUp(_LifecycleBase):
    """验证 Catch-up 差异收敛时序（v6 R2-A 单扫化后反转原契约）。"""

    def test_single_catch_up_scan_runs_after_watchers_started(self):
        """R2-A：唯一 catch-up 单扫（_reconcile_boundary_catch_up）在 watchers 之后。

        原契约（catch-up 先于 watchers）随 R2-A 单扫化反转：前置全盘扫删除，
        boundary 补扫覆盖 initial_scan_b 以来全部启动窗口。
        """
        order = []
        ctx, _ = self._patch_phases()
        with ctx, \
             patch.object(self.app, "start_watchers", side_effect=lambda: order.append("watchers")), \
             patch.object(self.app, "_reconcile_catch_up", side_effect=lambda: order.append("catch_up_pre")), \
             patch.object(self.app, "_reconcile_boundary_catch_up", side_effect=lambda: order.append("boundary")):
            self.app.start()
        # 前置 catch-up 不再被 start() 调用
        assert "catch_up_pre" not in order
        # 唯一 catch-up 扫在 watchers 之后
        assert "watchers" in order and "boundary" in order
        assert order.index("watchers") < order.index("boundary")


# ============================================================
# 索引 generation 推进
# ============================================================

class TestIndexGenerationPush:
    """测试 AppService.start() 中的 generation 推进逻辑。"""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.a_dir = Path(self.tmp) / "a"
        self.b_dir = Path(self.tmp) / "b"
        self.c_dir = Path(self.tmp) / "c"
        for d in [self.a_dir, self.b_dir, self.c_dir]:
            d.mkdir()

        config = Mock(spec=AppConfig)
        config.a_b_mappings = [ABMapping(
            mapping_id="m1",
            a_root=str(self.a_dir),
            b_root=str(self.b_dir))]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.paths.strm_engine_paths = []
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.behavior.sync_on_startup_wait = 0
        config.behavior.sync_on_startup = True
        config.strm_engine_paths = []

        self.config = config
        self.db = MagicMock(spec=Database)
        self.admin_api = Mock()

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, self.db, self.admin_api)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_generation_pushed_when_sync_on_startup_true(self, db=None):
        """sync_on_startup=True 且成功完成扫描时，generation 应被推进。"""
        mock_db = self.db
        mock_db.get_control.return_value = "0"

        with patch.object(self.app, "initial_scan_a"), \
             patch.object(self.app, "scan_a_to_b_full_sync"), \
             patch.object(self.app, "start_watchers"), \
             patch.object(self.app, "_start_subtitle_scan_background"), \
             patch.object(self.app, "refresh_service"), \
             patch.object(self.app, "prepare_environment"), \
             patch.object(self.app, "update_engine_configs"), \
             patch.object(self.app, "initial_scan_b"), \
             patch.object(self.app, "sync_protected_roots_from_config"), \
             patch.object(self.app, "scan_removed_protected_roots"), \
             patch.object(self.app, "persist_current_roots_snapshot"):
            
            self.config.behavior.sync_on_startup = True
            self.app.start()
            
            # 验证 complete_index_generation 被调用
            mock_db.complete_index_generation.assert_called_once_with(["m1"])

    def test_generation_not_pushed_when_sync_on_startup_false(self):
        """sync_on_startup=False 时，generation 不应被推进。"""
        mock_db = self.db
        mock_db.get_control.return_value = "0"

        with patch.object(self.app, "initial_scan_a"), \
             patch.object(self.app, "scan_a_to_b_full_sync") as mock_sync, \
             patch.object(self.app, "start_watchers"), \
             patch.object(self.app, "_start_subtitle_scan_background"), \
             patch.object(self.app, "refresh_service"), \
             patch.object(self.app, "prepare_environment"), \
             patch.object(self.app, "update_engine_configs"), \
             patch.object(self.app, "initial_scan_b"), \
             patch.object(self.app, "sync_protected_roots_from_config"), \
             patch.object(self.app, "scan_removed_protected_roots"), \
             patch.object(self.app, "persist_current_roots_snapshot"):
            
            self.config.behavior.sync_on_startup = False
            self.app.start()
            
            # 验证 scan_a_to_b_full_sync 未被调用
            mock_sync.assert_not_called()
            
            # 验证 complete_index_generation 未被调用
            mock_db.complete_index_generation.assert_not_called()

    def test_generation_not_pushed_on_exception(self):
        """扫描过程中抛异常时，generation 不应被推进。"""
        mock_db = self.db
        mock_db.get_control.return_value = "0"

        with patch.object(self.app, "initial_scan_a", side_effect=RuntimeError("boom")), \
             patch.object(self.app, "prepare_environment"), \
             patch.object(self.app, "update_engine_configs"), \
             patch.object(self.app, "initial_scan_b"), \
             patch.object(self.app, "sync_protected_roots_from_config"), \
             patch.object(self.app, "scan_removed_protected_roots"), \
             patch.object(self.app, "persist_current_roots_snapshot"):
            
            with pytest.raises(RuntimeError, match="boom"):
                self.app.start()

            # 验证 complete_index_generation 未被调用
            mock_db.complete_index_generation.assert_not_called()


class TestWebUiSavedMappingReachesReady:
    """WebUI 真实保存体经 DB 往返后，引擎门禁必须 ready（D1 端到端回归）。

    这是"前端保存 → webui_config → update_from_db → get_config_status"
    整条接缝的唯一守卫；现有用例都手写 mapping_id，覆盖不到这里。
    """

    def test_gate_ready_and_a_to_b_map_built(self, tmp_path):
        a_dir = tmp_path / "a"
        b_dir = tmp_path / "b"
        c_dir = tmp_path / "c"
        for d in (a_dir, b_dir, c_dir):
            d.mkdir()
        toml_path = tmp_path / "config.toml"
        toml_path.write_text(
            "[paths]\n"
            f'b_root = "{b_dir.as_posix()}"\n'
            f'c_root = "{c_dir.as_posix()}"\n',
            encoding="utf-8")

        cfg = AppConfig.from_file(str(toml_path))
        cfg.update_from_db(FakeConfigDb({"openlist": {
            "a_b_mappings": json.dumps([
                {"a_root": str(a_dir), "b_root": str(b_dir), "label": ""},
            ])}}))

        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            app = AppService(cfg, MagicMock(spec=Database), Mock())

        assert app.get_config_status()["status"] == "ready"
        # 空 mapping_id 会被 __init__ 的 _a_to_b_map 推导过滤掉 → 空 dict
        assert app._a_to_b_map != {}
        assert app._current_mapping_ids() != []


# ============================================================
# start() _running 不变式
# ============================================================

class TestStartMarksRunningWhenReady(_LifecycleBase):
    """start() 成功走完必须把 _running 置 True（start_main 门禁依赖的不变式）。

    历史回归：AppService 从未把 _running 置为 True（该字段只在 __init__ 和
    fail-safe 早退分支被赋 False），而 WebUIServer.start_main() 用它判断引擎
    是否真的起来了。结果 ready 配置也被判为 fail-safe：前端显示"未启动"，
    而 watcher / refresh 线程已在后台运行且因 _app_service 被置 None 而无法停止。

    本类是该不变式的唯一守卫。test_webui_http.py 的 start_main 用例使用替身，
    只能验证门禁逻辑，验证不了引擎是否真的置位。
    """

    def test_running_is_true_after_successful_start(self):
        ctx, _ = self._patch_phases()
        with ctx:
            self.app.start()

        assert self.app._running is True

    def test_running_and_refresh_service_do_not_fork(self):
        """"_running 为真" 与 "refresh service 已启动" 必须同时成立。"""
        ctx, _ = self._patch_phases()
        with ctx:
            self.app.start()

        self.app.refresh_service.start.assert_called_once()
        assert self.app._running is True


class TestCatchUpCandidateProtocol(_LifecycleBase):
    """Catch-up 只读候选队列、窗口补扫与 startup generation 隔离。"""

    def test_delta_contains_read_only_candidate_contract(self):
        path = self.b_dir / "Movie" / "new.strm"
        path.parent.mkdir(parents=True)
        path.write_text("/dav/movie/new.mp4", encoding="utf-8")

        delta = self.app._build_catch_up_delta(
            ({}, {str(path): {
                "webdav": "/dav/movie/new.mp4",
                "fp": "fp-new",
            }}),
            [],
        )

        assert len(delta) == 1
        item = delta[0]
        assert item["action_kind"] == "created_or_unindexed"
        assert item["action"] == "created_or_unindexed"
        assert item["local_path"] == str(path)
        assert item["mapping_id"] == "m1"
        assert item["fingerprint"] == "fp-new"
        assert item["webdav_path"] == "/dav/movie/new.mp4"
        assert isinstance(item["observed_at"], float)
        assert item["mtime_ns"] == path.stat().st_mtime_ns
        assert item["size"] == path.stat().st_size
        assert item["source_generation"] == 0
        assert item["confidence"] == "candidate"
        assert item["requires_destructive_action"] is False

    def test_unknown_mapping_is_fail_closed_and_exposed_in_summary(self):
        path = self.c_dir / "outside.strm"
        path.write_text("/dav/outside.mp4", encoding="utf-8")
        self.app._queue_catch_up_delta(self.app._build_catch_up_delta(
            ({}, {str(path): {"webdav": "/dav/outside.mp4", "fp": "fp"}}),
            [],
        ))

        item = self.app._catch_up_delta[0]
        assert item["mapping_id"] is None
        assert item["confidence"] == "unknown"
        summary = self.app.get_state_summary()["catch_up"]
        assert summary == {"pending_candidates": 0, "unknown": 1, "generation": 0}

    def test_boundary_catch_up_merges_deduplicated_read_only_candidates(self):
        """R2-A 单扫化：delta 合并去重契约改由唯一单扫（boundary）驱动。"""
        path = self.b_dir / "Movie" / "new.strm"
        path.parent.mkdir(parents=True)
        path.write_text("/dav/movie/new.mp4", encoding="utf-8")
        disk = ({}, {str(path): {"webdav": "/dav/movie/new.mp4", "fp": "fp"}})
        self.app._scan_b_disk = MagicMock(return_value=disk)
        self.app._load_b_db_records = MagicMock(return_value=[])
        # 第一次单扫观测候选
        self.app._reconcile_boundary_catch_up()
        assert len(self.app._catch_up_delta) == 1
        self.app.safe_remove_file = MagicMock()
        self.app.delete_b_by_local = MagicMock()
        # 第二次同窗口单扫：merge 去重不膨胀、只读不消费
        self.app._reconcile_boundary_catch_up()
        assert len(self.app._catch_up_delta) == 1
        self.app.safe_remove_file.assert_not_called()
        self.app.delete_b_by_local.assert_not_called()

    def test_startup_scan_observes_unindexed_b_file_end_to_end(self):
        """R2-A 观测保留：启动前落盘未入库 B 文件 → 单扫后 pending_candidates==1。"""
        path = self.b_dir / "Movie" / "late.strm"
        path.parent.mkdir(parents=True)
        path.write_text("/dav/movie/late.mp4", encoding="utf-8")
        # 单扫链路（磁盘扫描 + DB 载入）走真实实现，DB 记录侧只给空表
        self.db.get_all_b_records.return_value = []
        ctx, _ = self._patch_phases()
        with ctx:
            self.app.start()
        summary = self.app.get_state_summary()["catch_up"]
        assert summary["pending_candidates"] == 1

    def test_generation_change_aborts_before_watcher_refresh_and_ready(self):
        self.app.set_startup_generation(1)
        ctx, mocks = self._patch_phases()
        mocks["initial_scan_a"].side_effect = lambda *args, **kwargs: self.app.set_startup_generation(2)

        with ctx:
            self.app.start()

        mocks["start_watchers"].assert_not_called()
        self.app.refresh_service.start.assert_not_called()
        self.db.complete_index_generation.assert_not_called()
        assert self.app.get_state_summary()["is_ready"] is False
        assert self.app._running is False

    def test_stop_invalidates_inflight_startup_generation(self):
        before = self.app._startup_generation
        self.app.stop()
        assert self.app._startup_generation > before
        assert self.app.startup_generation == 0

class TestStartupLogFormatting(_LifecycleBase):
    """启动期日志必须可被格式化，否则记录在控制台与日志文件里双双丢失。

    回归：索引代次日志曾用 %d 占位符，而 Database.get_control 的签名是 -> str。

    为何长期潜伏：pytest 默认 root level 为 WARNING，logging.info 直接短路、
    根本不做格式化，所以整套测试都看不见 %d 与 str 的不匹配。本用例用
    caplog.at_level(logging.INFO) 强制放行 INFO，让格式化真正发生。

    红灯形态：本用例作为回归守卫，若格式化不匹配会报
    TypeError: %d format: a real number is required, not str，
    失败点在 self.app.start() 调用处。
    生产环境不会中止：logging 默认的 handleError 只打 traceback 不重抛，
    complete_index_generation / set_mapping_version 都已在此之前完成。
    """

    def test_index_generation_log_is_formattable(self, caplog):
        self.db.get_control.return_value = "7"
        ctx, _ = self._patch_phases()
        with ctx, caplog.at_level(logging.INFO):
            # 格式化在此处真正发生并抛 TypeError
            self.app.start()

        # 用 str(record.msg) 过滤，避免在筛选阶段就触发格式化
        target = [r for r in caplog.records if "索引代次推进到" in str(r.msg)]
        assert target, "未捕获到索引代次日志"
        assert target[0].getMessage() == "[启动] 索引代次推进到 7"


class TestStartupProgressSemantics(_LifecycleBase):
    """验证启动扫描进度只在对应阶段完成后发布。"""

    def _real_sync_service(self):
        service = SyncService(self.app)
        self.app.sync_service = service
        return service

    def test_initial_scan_a_publishes_discovered_and_indexed_after_each_batch(self):
        service = self._real_sync_service()
        for index in range(1001):
            (self.a_dir / f"file{index}.strm").write_text(
                f"/mount/file{index}.mp4", encoding="utf-8")
        self.db.upsert_a_batch.side_effect = lambda records: len(records)

        with patch.object(self.app, "update_progress",
                          wraps=self.app.update_progress) as update_progress:
            service.initial_scan_a(use_bulk=False)

        progress_calls = update_progress.call_args_list
        assert self.app.get_state_summary()["progress"]["a_discovered"] == 1001
        assert self.app.get_state_summary()["progress"]["a_indexed"] == 1001
        assert [call.kwargs["a_indexed"] for call in progress_calls
                if "a_indexed" in call.kwargs] == [1000, 1001]

    def test_initial_scan_a_publishes_indexed_only_after_bulk_commit(self):
        service = self._real_sync_service()
        (self.a_dir / "file.strm").write_text(
            "/mount/file.mp4", encoding="utf-8")
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = []
        bulk_context = MagicMock()
        bulk_context.__enter__.return_value = connection
        bulk_context.__exit__.return_value = False
        self.db.bulk_connection.return_value = bulk_context

        service.initial_scan_a(use_bulk=True)

        assert self.app.get_state_summary()["progress"]["a_discovered"] == 1
        assert self.app.get_state_summary()["progress"]["a_indexed"] == 1
        assert bulk_context.__exit__.call_args.args[:2] == (None, None)

    def test_initial_scan_b_sets_discovered_and_reconciled(self):
        disk_path = str(self.b_dir / "file.strm")
        self.app._scan_b_disk = MagicMock(return_value=(
            {"fp": {disk_path}},
            {disk_path: {"webdav": "/mount/file.mp4", "fp": "fp"}},
        ))
        self.app._load_b_db_records = MagicMock(return_value=[])
        self.app._reconcile_b_historical_records = MagicMock()
        self.app._insert_new_b_records = MagicMock()

        self.app.initial_scan_b()

        progress = self.app.get_state_summary()["progress"]
        assert progress["b_discovered"] == 1
        assert progress["b_reconciled"] == 1

    def test_full_sync_counts_only_successful_records_after_db_commit(self):
        service = self._real_sync_service()
        records = []
        for index in range(2):
            path = self.a_dir / f"file{index}.strm"
            path.write_text(f"/mount/file{index}.mp4", encoding="utf-8")
            records.append(type("ARecordStub", (), {
                "local_path": str(path),
                "webdav_path": f"/mount/file{index}.mp4",
                "parent_webdav_path": "/mount",
            })())
        self.db.get_all_a_records.return_value = records
        self.db.get_all_ghost_protected_paths.return_value = set()
        self.db.get_all_b_fingerprints.return_value = set()
        # pass1 消耗两次 build（两记录）；pass2 走 T3 prepare → commit 分块路径
        self.app.build_b_path_from_a = MagicMock(side_effect=[
            self.b_dir / "file0.strm", self.b_dir / "file1.strm"])
        connection = MagicMock()
        bulk_context = MagicMock()
        bulk_context.__enter__.return_value = connection
        bulk_context.__exit__.return_value = False
        self.db.bulk_connection.return_value = bulk_context
        from domain.sync.sync_service import _SyncPrep

        def _prep(rec, valid_engine_paths, mapping_id=None):
            return _SyncPrep(
                local_path=rec.local_path,
                b_local=self.b_dir / Path(rec.local_path).name,
                webdav_path=rec.webdav_path, parent="/mount",
                fingerprint=f"fp_{rec.local_path}", mapping_id="m1",
                needs_copy=False)

        service._prepare_sync_one = MagicMock(side_effect=_prep)
        service._commit_sync_one = MagicMock(side_effect=["success", "fail"])

        service.scan_a_to_b_full_sync(use_bulk=True)

        assert self.app.get_state_summary()["progress"]["synced_records"] == 1
        assert connection.commit.call_count == 1
        assert bulk_context.__exit__.call_args.args[:2] == (None, None)

    def test_initial_scan_b_failure_propagates_and_aborts_subsequent_phases(self):
        """Task 2: initial_scan_b 批写失败抛异常时，异常直接沿 start() 向上抛出，后续阶段不执行。"""
        ctx, mocks = self._patch_phases()
        mocks["initial_scan_b"].side_effect = RuntimeError("batch write failure")

        with ctx:
            with pytest.raises(RuntimeError) as excinfo:
                self.app.start()
            assert "batch write failure" in str(excinfo.value)

        # 异常发生后的后续阶段均不得执行
        mocks["scan_a_to_b_full_sync"].assert_not_called()
        mocks["start_watchers"].assert_not_called()
        mocks["_start_subtitle_scan_background"].assert_not_called()
        self.app.refresh_service.start.assert_not_called()
