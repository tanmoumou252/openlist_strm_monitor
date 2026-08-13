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
from _test_helpers import FakeConfigDb  # noqa: E402


# ============================================================
# 公共构造
# ============================================================

# start() 中位于配置检查之后的重量级阶段方法，测试时统一 patch 掉
_START_PHASES = (
    "initial_scan_b",
    "sync_protected_roots_from_config",
    "scan_removed_protected_roots",
    "persist_current_roots_snapshot",
    "initial_scan_a",
    "scan_a_to_b_full_sync",
    "start_watchers",
    "_scan_a_subtitles_on_startup",
    "update_engine_configs",
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
        self.app.refresh_service.start.side_effect = (
            lambda: order.append("refresh_service.start"))

        with patch.multiple(self.app, **patches):
            self.app.start()
        return order

    def test_key_phase_order(self):
        """顺序：环境 → DB → B 扫描 → 保护根 → A 扫描 → A→B → watcher → 字幕 → refresh。"""
        order = self._run_start_recording_order()

        expected = [
            "prepare_environment",
            "init_db",
            "update_engine_configs",
            "initial_scan_b",
            "sync_protected_roots_from_config",
            "scan_removed_protected_roots",
            "persist_current_roots_snapshot",
            "initial_scan_a",
            "scan_a_to_b_full_sync",
            "start_watchers",
            "_scan_a_subtitles_on_startup",
            "refresh_service.start",
        ]
        assert order == expected

    def test_watcher_starts_before_refresh_service(self):
        """watcher 必须先于 refresh service 启动。"""
        order = self._run_start_recording_order()
        assert order.index("start_watchers") < order.index(
            "refresh_service.start")

    def test_b_scan_precedes_a_scan(self):
        """B 区扫描先于 A 区扫描（B 区状态是 A→B 同步的前置）。"""
        order = self._run_start_recording_order()
        assert order.index("initial_scan_b") < order.index("initial_scan_a")

    def test_invalid_subtitles_cleaned_on_start(self):
        """启动时清理失效字幕记录。"""
        ctx, _ = self._patch_phases()
        with ctx:
            self.app.start()

        self.db.cleanup_invalid_subtitles.assert_called_once()

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
# 未覆盖行为的显式记录
# ============================================================


class TestLifecycleKnownGaps(_LifecycleBase):
    """记录当前实现未提供的生命周期保证，避免后续误认为已有契约。

    这些不是缺陷断言，而是把"当前没有该行为"固化下来，
    防止文档或后续测试虚构生产承诺。
    """

    def test_stop_does_not_currently_reset_running_flag(self):
        """当前 stop() 不会把 _running 置为 False（仅 fail-safe 分支会设置）。"""
        self.app._running = True
        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = True
        self.app.observer = mock_observer

        self.app.stop()

        # 记录当前行为：stop() 不修改 _running
        assert self.app._running is True

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
             patch.object(self.app, "_scan_a_subtitles_on_startup"), \
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
             patch.object(self.app, "_scan_a_subtitles_on_startup"), \
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


# ============================================================
# 启动期日志格式化
# ============================================================

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
