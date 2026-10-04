"""
refresh_service.py 单元测试

测试范围：
- RefreshService.start / stop（启停逻辑）
- RefreshService.execute_refresh_cycle（编排方法，验证调用序列）
- RefreshService._analyze_paths（纯逻辑：refresh_paths vs engine_paths 集合运算）

运行方式：
  pytest src/tests/test_refresh_service.py -v
"""
from __future__ import annotations

import sqlite3
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 确保 src/ 在 sys.path 中（conftest.py 也会处理，此处冗余保护）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from domain.sync.sync_service import (
    AuditCoverageIncompleteError,
    AuditIncompleteError,
)
from refresh_service import RefreshService, PartialRefreshError
from _test_helpers import build_mock_app


# ============================================================
# 辅助函数
# ============================================================


def _make_app(
    *,
    refresh_enabled: bool = True,
    refresh_paths: list[str] | None = None,
    interval_seconds: int = 300,
    strm_engine_paths: list[str] | None = None,
    full_audit_interval_days: int = 0,
) -> MagicMock:
    """构建最小化 mock AppService，供 RefreshService 使用。

    委托给 conftest.build_mock_app，消除重复实现。
    """
    return build_mock_app(  # type: ignore[return-value]
        None,
        refresh_enabled=refresh_enabled,
        refresh_paths=refresh_paths,
        interval_seconds=interval_seconds,
        strm_engine_paths=strm_engine_paths,
        full_audit_interval_days=full_audit_interval_days,
    )


# ============================================================
# start / stop
# ============================================================

class TestRefreshServiceStartStop:
    """测试 RefreshService 的启停逻辑"""

    def test_start_disabled_returns_immediately(self):
        """refresh.enabled = False → 不启动线程"""
        app = _make_app(refresh_enabled=False, refresh_paths=["/strm"])
        svc = RefreshService(app)
        svc.start()
        assert svc._running is False
        assert svc._thread is None

    def test_start_no_refresh_paths_returns(self):
        """refresh_paths 为空 → 不启动线程"""
        app = _make_app(refresh_enabled=True, refresh_paths=[])
        svc = RefreshService(app)
        svc.start()
        assert svc._running is False
        assert svc._thread is None

    @patch("refresh_service.threading.Thread")
    def test_start_launches_worker_thread(self, mock_thread_cls):
        """正常启动 → 创建 daemon 线程并 start"""
        app = _make_app(refresh_paths=["/strm"])
        svc = RefreshService(app)
        svc.start()

        assert svc._running is True
        mock_thread_cls.assert_called_once()
        # 验证 daemon=True
        assert mock_thread_cls.call_args[1]["daemon"] is True

    @patch("refresh_service.threading.Thread")
    def test_stop_joins_thread(self, mock_thread_cls):
        """stop → 设置 _running=False 并 join 线程"""
        app = _make_app(refresh_paths=["/strm"])
        svc = RefreshService(app)
        svc._running = True
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        svc._thread = mock_thread

        svc.stop()
        assert svc._running is False
        mock_thread.join.assert_called_once_with(timeout=5)

    def test_stop_no_thread_is_safe(self):
        """stop 在没有线程时不抛异常"""
        app = _make_app()
        svc = RefreshService(app)
        svc.stop()  # 不应抛异常
        assert svc._running is False


# ============================================================
# _analyze_paths
# ============================================================

class TestAnalyzePaths:
    """测试 _analyze_paths 的前缀匹配逻辑

    新逻辑：refresh_path 是某个 engine 的子路径时视为匹配。
    即 refresh_path.startswith(engine + "/") 或 refresh_path == engine。
    """

    def test_empty_engine_set(self):
        """engine_paths 为空 → 所有 refresh_paths 都是 valid，无交集分析"""
        app = _make_app(
            refresh_paths=["/a", "/b"],
            strm_engine_paths=[],
        )
        svc = RefreshService(app)
        analysis = svc._analyze_paths()

        # engine_set 为空时 valid_refresh_paths = list(refresh_set)，顺序不保证
        assert set(analysis.valid_refresh_paths) == {"/a", "/b"}
        assert analysis.only_refresh == set()
        assert analysis.only_engine == set()
        assert analysis.engine_set == set()

    def test_refresh_equals_engine_exact(self):
        """refresh_path 精确等于 engine 路径 → 匹配"""
        app = _make_app(
            refresh_paths=["/strm", "/data"],
            strm_engine_paths=["/strm", "/data"],
        )
        svc = RefreshService(app)
        analysis = svc._analyze_paths()

        assert set(analysis.valid_refresh_paths) == {"/strm", "/data"}
        assert analysis.only_refresh == set()
        assert analysis.only_engine == set()

    def test_refresh_is_subpath_of_engine(self):
        """refresh_path 是 engine 的子路径 → 前缀匹配"""
        app = _make_app(
            refresh_paths=["/strm/电影", "/strm/番剧"],
            strm_engine_paths=["/strm"],
        )
        svc = RefreshService(app)
        analysis = svc._analyze_paths()

        # 两条 refresh_path 都匹配 /strm 前缀
        assert set(analysis.valid_refresh_paths) == {"/strm/电影", "/strm/番剧"}
        assert analysis.only_refresh == set()
        assert analysis.only_engine == set()

    def test_refresh_subpath_plus_exact_engine(self):
        """混合用例：子路径 + 精确匹配 + 不匹配路径"""
        app = _make_app(
            refresh_paths=["/strm", "/strm/电影", "/extra_refresh"],
            strm_engine_paths=["/strm", "/extra_engine"],
        )
        svc = RefreshService(app)
        analysis = svc._analyze_paths()

        # /strm 精确匹配 /strm
        # /strm/电影 前缀匹配 /strm
        assert set(analysis.valid_refresh_paths) == {"/strm", "/strm/电影"}
        # /extra_refresh 不匹配任何 engine
        assert analysis.only_refresh == {"/extra_refresh"}
        # /extra_engine 没有对应 refresh_path
        assert analysis.only_engine == {"/extra_engine"}

    def test_prefix_boundary_no_false_match(self):
        """边界保护：/strm/电影 不匹配 /str（缺少斜杠分隔）"""
        app = _make_app(
            refresh_paths=["/strm/电影"],
            strm_engine_paths=["/str"],
        )
        svc = RefreshService(app)
        analysis = svc._analyze_paths()

        # /strm/电影.startswith("/str/") == False → 不匹配
        assert analysis.valid_refresh_paths == []
        assert analysis.only_refresh == {"/strm/电影"}
        assert analysis.only_engine == {"/str"}

    def test_trailing_slash_normalization(self):
        """尾部斜杠不影响匹配"""
        app = _make_app(
            refresh_paths=["/strm/电影/", "/strm/a"],
            strm_engine_paths=["/strm/"],
        )
        svc = RefreshService(app)
        analysis = svc._analyze_paths()

        assert set(analysis.valid_refresh_paths) == {"/strm/电影/", "/strm/a"}
        assert analysis.only_refresh == set()
        assert analysis.only_engine == set()

    def test_valid_refresh_paths_sorted(self):
        """valid_refresh_paths 应该是排序列表"""
        app = _make_app(
            refresh_paths=["/z", "/a", "/m"],
            strm_engine_paths=["/z", "/a", "/m"],
        )
        svc = RefreshService(app)
        analysis = svc._analyze_paths()

        assert analysis.valid_refresh_paths == ["/a", "/m", "/z"]

    def test_disjoint_sets(self):
        """完全不相交 → valid 为空"""
        app = _make_app(
            refresh_paths=["/refresh1"],
            strm_engine_paths=["/engine1"],
        )
        svc = RefreshService(app)
        analysis = svc._analyze_paths()

        assert analysis.valid_refresh_paths == []
        assert analysis.only_refresh == {"/refresh1"}
        assert analysis.only_engine == {"/engine1"}


# ============================================================
# _calculate_safe_refresh_paths
# ============================================================

class TestCalculateSafeRefreshPaths:
    """测试 _calculate_safe_refresh_paths 的前缀匹配逻辑"""

    def test_empty_engine_set_returns_all(self):
        """engine_set 为空 → 返回所有 valid_refresh_paths"""
        app = _make_app()
        svc = RefreshService(app)
        from refresh_service import PathAnalysis
        analysis = PathAnalysis(
            valid_refresh_paths=["/a", "/b"],
            only_refresh=set(),
            only_engine=set(),
            engine_set=set(),
        )
        result = svc._calculate_safe_refresh_paths(analysis, set())
        assert result == ["/a", "/b"]

    def test_subpath_matches_accessible_engine(self):
        """引擎子路径匹配到可访问引擎 → 安全"""
        app = _make_app()
        svc = RefreshService(app)
        from refresh_service import PathAnalysis
        analysis = PathAnalysis(
            valid_refresh_paths=["/strm/电影", "/strm/番剧"],
            only_refresh=set(),
            only_engine=set(),
            engine_set={"/strm"},
        )
        result = svc._calculate_safe_refresh_paths(analysis, {"/strm"})
        assert set(result) == {"/strm/电影", "/strm/番剧"}

    def test_subpath_no_accessible_engine_skipped(self):
        """引擎子路径匹配到的引擎不在可访问集合中 → 跳过"""
        app = _make_app()
        svc = RefreshService(app)
        from refresh_service import PathAnalysis
        analysis = PathAnalysis(
            valid_refresh_paths=["/strm/电影", "/other/data"],
            only_refresh=set(),
            only_engine=set(),
            engine_set={"/strm", "/other"},
        )
        # 只有 /strm 可访问，/other 不可访问
        result = svc._calculate_safe_refresh_paths(analysis, {"/strm"})
        assert result == ["/strm/电影"]

    def test_exact_engine_path_matches_accessible(self):
        """精确等于引擎挂载点的路径 → 引擎可访问时安全"""
        app = _make_app()
        svc = RefreshService(app)
        from refresh_service import PathAnalysis
        analysis = PathAnalysis(
            valid_refresh_paths=["/strm"],
            only_refresh=set(),
            only_engine=set(),
            engine_set={"/strm"},
        )
        result = svc._calculate_safe_refresh_paths(analysis, {"/strm"})
        assert result == ["/strm"]


# ============================================================
# execute_refresh_cycle
# ============================================================

class TestExecuteRefreshCycle:
    """测试 execute_refresh_cycle 的编排逻辑（验证方法调用序列）"""

    def test_scan_and_sync_passes_explicit_root_filter(self):
        app = _make_app(refresh_paths=["/strm"], strm_engine_paths=["/strm"])
        app.get_a_roots_for_refresh_paths.return_value = [Path("C:/a1")]
        svc = RefreshService(app)
        with patch.object(svc, "_wait_for_sync"), patch.object(svc, "_scan_and_sync") as scan:
            svc.execute_refresh_cycle()
        scan.assert_called_once()
        assert scan.call_args.kwargs["a_roots"] == [Path("C:/a1")]

    def test_empty_refresh_paths_does_not_scan_a_roots(self):
        app = _make_app(refresh_paths=[], strm_engine_paths=["/strm"])
        app.get_a_roots_for_refresh_paths.return_value = []
        svc = RefreshService(app)
        with patch.object(svc, "_sync_and_scan_protected_roots") as sync_roots, \
             patch.object(svc, "_scan_and_sync") as scan:
            svc.execute_refresh_cycle()
        sync_roots.assert_not_called()
        scan.assert_not_called()

    def test_full_audit_runs_after_interval(self):
        app = _make_app(refresh_paths=[], full_audit_interval_days=7)
        app.db.get_control.return_value = "0"
        svc = RefreshService(app)
        with patch("refresh_service.time.time", return_value=8 * 86400), \
             patch.object(app, "initial_scan_a") as scan_a, \
             patch.object(app, "scan_a_to_b_full_sync") as sync:
            svc._maybe_run_full_audit()
        # 全量审计为 A 区快照权威自愈触发源：必须绕过内容读跳检
        scan_a.assert_called_once_with(use_bulk=False, a_roots=None, use_snapshot=False)
        sync.assert_called_once_with(valid_engine_paths=None, use_bulk=False)
        app.db.set_control.assert_called_once()

    def test_full_audit_zero_disables_scan(self):
        app = _make_app(refresh_paths=[], full_audit_interval_days=0)
        svc = RefreshService(app)
        with patch.object(app, "initial_scan_a") as scan_a, \
             patch.object(app, "scan_a_to_b_full_sync") as sync:
            svc._maybe_run_full_audit()
        scan_a.assert_not_called()
        sync.assert_not_called()

    def test_execute_refresh_cycle_calls_all_steps(self):
        """execute_refresh_cycle 应该按序调用所有步骤（不再调用 _cleanup_a_for_update_mode）"""
        app = _make_app(refresh_paths=["/strm"], strm_engine_paths=["/strm"])
        svc = RefreshService(app)

        # mock 所有内部方法
        with patch.object(svc, "_sync_and_scan_protected_roots") as m_sync, \
             patch.object(svc, "_check_engine_accessibility", return_value={"/strm"}) as m_check, \
             patch.object(svc, "_cleanup_a_for_update_mode") as m_cleanup, \
             patch.object(svc, "_calculate_safe_refresh_paths", return_value=["/strm"]) as m_calc, \
             patch.object(svc, "_execute_webdav_refreshes", return_value=[]) as m_exec, \
             patch.object(svc, "_wait_for_sync") as m_wait, \
             patch.object(svc, "_scan_and_sync") as m_scan, \
             patch.object(svc, "_persist_snapshot") as m_persist:

            svc.execute_refresh_cycle()

            # 验证所有步骤都被调用
            m_sync.assert_called_once()
            m_check.assert_called_once()
            # 冗余清理已改为局部触发，不再在定期刷新时调用
            m_cleanup.assert_not_called()
            m_calc.assert_called_once()
            m_exec.assert_called_once()
            m_wait.assert_called_once()
            m_scan.assert_called_once()
            m_persist.assert_called_once()

    def test_empty_engine_set_completes_without_error(self):
        """可访问引擎为空集合时，完整编排流程仍正常完成不抛异常"""
        app = _make_app(refresh_paths=["/strm"], strm_engine_paths=["/strm"])
        svc = RefreshService(app)

        with patch.object(svc, "_sync_and_scan_protected_roots"), \
             patch.object(svc, "_check_engine_accessibility", return_value=set()), \
             patch.object(svc, "_cleanup_a_for_update_mode") as m_cleanup, \
             patch.object(svc, "_calculate_safe_refresh_paths", return_value=[]), \
             patch.object(svc, "_execute_webdav_refreshes", return_value=[]), \
             patch.object(svc, "_wait_for_sync"), \
             patch.object(svc, "_scan_and_sync"), \
             patch.object(svc, "_persist_snapshot"):
            # 空引擎路径应该正常完成
            svc.execute_refresh_cycle()
            # 冗余清理已改为局部触发，不再在定期刷新时调用
            m_cleanup.assert_not_called()


# ============================================================
# _check_engine_accessibility
# ============================================================

class TestCheckEngineAccessibility:
    """测试 _check_engine_accessibility"""

    def test_empty_engine_set_returns_empty(self):
        """engine_set 为空 → 返回空集合"""
        app = _make_app()
        svc = RefreshService(app)
        result = svc._check_engine_accessibility(set())
        assert result == set()

    def test_api_validation_success(self):
        """API 验证成功 → 返回验证结果"""
        app = _make_app()
        svc = RefreshService(app)
        with patch.object(svc, "_validate_strm_storages_via_api",
                          return_value={"/strm"}) as m_validate:
            result = svc._check_engine_accessibility({"/strm"})
            assert result == {"/strm"}
            m_validate.assert_called_once()

    def test_api_returns_none_returns_empty(self):
        """API 验证返回 None → 返回空集合"""
        app = _make_app()
        svc = RefreshService(app)
        with patch.object(svc, "_validate_strm_storages_via_api",
                          return_value=None):
            result = svc._check_engine_accessibility({"/strm"})
            assert result == set()


class TestRefreshServiceHotReloadContract:
    def test_interval_change_wakes_waiting_worker(self):
        app = _make_app(refresh_paths=["/strm"], interval_seconds=3600)
        svc = RefreshService(app)
        svc._run_cycle_with_breaker = MagicMock()
        svc._running = True
        worker = threading.Thread(target=svc._worker, daemon=True)
        svc._thread = worker
        worker.start()
        time.sleep(0.05)
        app.config.refresh.interval_seconds = 1
        svc.notify_config_changed()
        time.sleep(0.05)
        svc.stop()
        assert svc._run_cycle_with_breaker.call_count >= 2

    def test_disabled_worker_returns_without_running_cycles(self):
        app = _make_app(refresh_paths=["/strm"], refresh_enabled=False)
        svc = RefreshService(app)
        svc._run_cycle_with_breaker = MagicMock()
        svc._running = True
        worker = threading.Thread(target=svc._worker, daemon=True)
        svc._thread = worker
        worker.start()
        time.sleep(0.05)
        # 首轮 enabled 检查使禁用状态下 worker 直接返回，不执行任何周期。
        # 见 refresh_service.py._worker 头部注释。
        assert svc._run_cycle_with_breaker.call_count == 0
        assert not worker.is_alive()

    def test_reconfigure_does_not_create_duplicate_worker(self):
        app = _make_app(refresh_paths=["/strm"])
        svc = RefreshService(app)
        with patch("refresh_service.threading.Thread") as thread_cls:
            svc.start()
            svc.reconfigure()
        assert thread_cls.call_count == 1

    def test_audit_completion_advances_generation(self):
        app = _make_app(refresh_paths=[], full_audit_interval_days=7)
        app.db.get_control.return_value = "0"
        app._current_mapping_ids.return_value = ["m1"]
        with patch.object(app, "initial_scan_a"), patch.object(app, "scan_a_to_b_full_sync"):
            svc = RefreshService(app)
            with patch("refresh_service.time.time", return_value=8 * 86400):
                svc._maybe_run_full_audit()
        app.db.complete_index_generation.assert_called_once_with(["m1"])

    def test_audit_with_empty_mappings_does_not_complete_generation(self):
        app = _make_app(refresh_paths=[], full_audit_interval_days=7)
        app.db.get_control.return_value = "0"
        app._current_mapping_ids.return_value = []
        with patch.object(app, "initial_scan_a"), patch.object(app, "scan_a_to_b_full_sync"):
            svc = RefreshService(app)
            with patch("refresh_service.time.time", return_value=8 * 86400):
                svc._maybe_run_full_audit()
        app.db.complete_index_generation.assert_not_called()


class TestWorkerDeferFirstCycle:
    """R-新6（c.9.2 Task 11）：首轮刷新周期去「立即」特化。

    start(defer_first_cycle=True) 首轮先等 interval（期间 notify_config_changed
    仍立即唤醒）；defer=False 行为不变；stop→start 复用同一实例时 stop()
    预置 set 的 _config_changed 必须 clear-before-wait，否则 defer 静默失效
    （第 4 例竞态红测；WebUI 重启链路复用同一 RefreshService 实例）。
    """

    def _make_svc(self, interval_seconds=1):
        app = _make_app(refresh_paths=["/strm"], interval_seconds=interval_seconds)
        svc = RefreshService(app)
        svc._run_cycle_with_breaker = MagicMock()
        return svc, app

    def test_defer_first_cycle_zero_calls_within_interval_and_notify_wakes(self):
        svc, app = self._make_svc()
        svc.start(defer_first_cycle=True)
        assert svc._running is True
        time.sleep(0.3)
        assert svc._run_cycle_with_breaker.call_count == 0, \
            "defer=True 首轮在 interval 内不得立即执行"
        svc.notify_config_changed()
        deadline = time.time() + 2.0
        while svc._run_cycle_with_breaker.call_count == 0 and time.time() < deadline:
            time.sleep(0.02)
        assert svc._run_cycle_with_breaker.call_count >= 1, \
            "notify_config_changed 应立即唤醒被 defer 的首轮"
        svc.stop()

    def test_no_defer_first_run_immediate(self):
        """defer=False（默认）行为不变：首轮立即执行"""
        svc, app = self._make_svc()
        svc.start()
        deadline = time.time() + 2.0
        while svc._run_cycle_with_breaker.call_count == 0 and time.time() < deadline:
            time.sleep(0.02)
        assert svc._run_cycle_with_breaker.call_count >= 1, \
            "defer=False 首轮应立即执行"
        svc.stop()

    def test_start_defer_thread_kwargs_passthrough(self):
        """start(defer_first_cycle=True) 以 kwargs 形态传入 worker，daemon 形态保持"""
        app = _make_app(refresh_paths=["/strm"])
        svc = RefreshService(app)
        with patch("refresh_service.threading.Thread") as thread_cls:
            svc.start(defer_first_cycle=True)
        assert thread_cls.call_args[1]["daemon"] is True
        assert thread_cls.call_args[1]["kwargs"] == {"defer_first_cycle": True}

    def test_restart_after_stop_still_defers(self):
        """竞态复现红测：stop() 预置 set 事件 + start(defer=True) 复用同一
        实例——不 clear 则 wait 立即返回、defer 静默失效（新实例红测测不出）"""
        svc, app = self._make_svc()
        svc.start()
        deadline = time.time() + 2.0
        while svc._run_cycle_with_breaker.call_count == 0 and time.time() < deadline:
            time.sleep(0.02)
        svc.stop()
        svc._run_cycle_with_breaker.call_count = 0
        svc.start(defer_first_cycle=True)
        time.sleep(0.3)
        assert svc._run_cycle_with_breaker.call_count == 0, \
            "stop→start(defer=True) 后预置事件未 clear，defer 静默失效"
        svc.notify_config_changed()
        deadline = time.time() + 2.0
        while svc._run_cycle_with_breaker.call_count == 0 and time.time() < deadline:
            time.sleep(0.02)
        assert svc._run_cycle_with_breaker.call_count >= 1
        svc.stop()

    def test_defer_disabled_wake_keeps_worker_alive(self):
        """c.9.3 Task C（复查② R1 红测）：defer 等待期被禁用 → 唤醒后线程
        必须存活（挂起主循环 disabled 分支等 notify）而非死亡——旧行为
        return 会把既有微秒竞态窗放大为整个 interval 的「worker 永久静默」
        （线程死亡而 _running 恒 True，reconfigure 仅 notify 无线程消费）"""
        svc, app = self._make_svc()
        svc.start(defer_first_cycle=True)
        time.sleep(0.2)
        app.config.refresh.enabled = False
        svc.notify_config_changed()
        time.sleep(0.3)
        assert svc._thread is not None and svc._thread.is_alive(), \
            "defer 窗口禁用唤醒后 worker 线程死亡（R1：_running 恒 True 的永久静默）"
        assert svc._run_cycle_with_breaker.call_count == 0, \
            "禁用态唤醒后不得执行周期"
        # 重开 → notify → 从主循环 disabled 分支唤醒恢复
        app.config.refresh.enabled = True
        svc.notify_config_changed()
        deadline = time.time() + 2.0
        while svc._run_cycle_with_breaker.call_count == 0 and time.time() < deadline:
            time.sleep(0.02)
        assert svc._run_cycle_with_breaker.call_count >= 1, \
            "enable 重开后 notify 应恢复周期执行"
        svc.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ============================================================
# 缺口测试：7 天全量审计与 B 区删除独立性
# ============================================================

class TestFullAuditGap:
    """补齐 7 天全量审计的缺口场景。"""

    def test_full_audit_not_due_skips(self):
        """未到期时不重复执行全量审计。"""
        app = _make_app(refresh_paths=[], full_audit_interval_days=7)
        # 设置 last_full_audit_at 为 1 天前
        app.db.get_control.return_value = str(int(time.time()) - 86400)
        svc = RefreshService(app)

        with patch.object(app, "initial_scan_a") as scan_a, \
             patch.object(app, "scan_a_to_b_full_sync") as sync:
            svc._maybe_run_full_audit()

        scan_a.assert_not_called()
        sync.assert_not_called()
        app.db.set_control.assert_not_called()

    def test_full_audit_persists_timestamp_after_run(self):
        """全量审计执行后必须重新记录 last_full_audit_at。"""
        app = _make_app(refresh_paths=[], full_audit_interval_days=7)
        app.db.get_control.return_value = "0"
        svc = RefreshService(app)

        with patch("refresh_service.time.time", return_value=8 * 86400), \
             patch.object(app, "initial_scan_a") as scan_a, \
             patch.object(app, "scan_a_to_b_full_sync") as sync:
            svc._maybe_run_full_audit()

        # 验证 set_control 被调用且值为当前时间
        app.db.set_control.assert_called_once()
        call_args = app.db.set_control.call_args
        assert call_args[0][0] == "last_full_audit_at"
        assert int(call_args[0][1]) == 8 * 86400

    def test_full_audit_incomplete_skips_all_advancement(self):
        """审计不完整（快照行失效失败）时：B 区收敛照常，但四项推进全跳过，
        周期路径返回 False（交 execute_refresh_cycle 计健康失败）。"""
        app = _make_app(refresh_paths=[], full_audit_interval_days=7)
        app.db.get_control.return_value = "0"
        svc = RefreshService(app)
        last_before = svc._last_full_audit_at

        with patch.object(app, "initial_scan_a",
                          side_effect=AuditIncompleteError("旧快照行失效失败: db boom")), \
             patch.object(app, "scan_a_to_b_full_sync") as sync:
            ok = svc._maybe_run_full_audit()

        assert ok is False, (
            "审计不完整必须以 False 上报（不得静默成功）")
        sync.assert_called_once_with(valid_engine_paths=None, use_bulk=False), (
            "B 区收敛不得因快照失效失败被跳过")
        app.db.complete_index_generation.assert_not_called()
        app.db.touch_verified_by_mapping.assert_not_called()
        app.db.set_control.assert_not_called()
        assert svc._last_full_audit_at == last_before, (
            "内存审计时间戳不得推进（否则下轮周期静默跳过审计）")
        assert svc._full_audit_in_progress is False, (
            "finally 必须复位 in-progress 标志（否则审计永久锁死）")

    def test_manual_audit_incomplete_returns_incomplete_status(self):
        """手动审计不完整：返回可区分的 incomplete 终态且不推进时间戳。"""
        app = _make_app(refresh_paths=[], full_audit_interval_days=7)
        svc = RefreshService(app)

        with patch.object(app, "initial_scan_a",
                          side_effect=AuditIncompleteError("旧快照行失效失败: db boom")), \
             patch.object(app, "scan_a_to_b_full_sync"):
            result = svc.run_full_audit_now()

        assert result["ok"] is False, f"实际: {result!r}"
        assert result["status"] == "incomplete", (
            f"不完整必须返回可区分状态，实际: {result!r}")
        assert "旧快照行失效失败" in result["error"], (
            f"必须透出根因，实际: {result!r}")
        app.db.complete_index_generation.assert_not_called()
        app.db.touch_verified_by_mapping.assert_not_called()
        app.db.set_control.assert_not_called()

    def test_full_audit_coverage_incomplete_skips_stamps_but_keeps_cadence(self):
        """覆盖缺口（根不可达）：跳过索引代次与核对盖章，但**仍推进审计节拍**
        （last_full_audit_at）——否则挂载故障期间每个周期重跑全量审计。"""
        app = _make_app(refresh_paths=[], full_audit_interval_days=7)
        app.db.get_control.return_value = "0"
        svc = RefreshService(app)
        app._current_mapping_ids.return_value = ["m1"]

        with patch.object(app, "initial_scan_a",
                          side_effect=AuditCoverageIncompleteError("1/2 个 A 根不可达")), \
             patch.object(app, "scan_a_to_b_full_sync"):
            ok = svc._maybe_run_full_audit()

        assert ok is True, "覆盖缺口不阻断审计节拍（周期路径应计为已执行）"
        app.db.complete_index_generation.assert_not_called(), (
            "覆盖不完整不得宣称索引代次已刷新")
        app.db.touch_verified_by_mapping.assert_not_called(), (
            "覆盖不完整不得给未巡查行盖核对章")
        app.db.set_control.assert_called_once()
        assert app.db.set_control.call_args[0][0] == "last_full_audit_at", (
            "审计节拍必须推进")
        current_failures = svc._consecutive_failures
        _ = current_failures  # 覆盖缺口不计入健康失败（节拍已推进）

    def test_manual_audit_coverage_incomplete_flags_result(self):
        """手动审计覆盖缺口：status 仍为 completed，但带 coverage_incomplete 标记
        与 warning 文本，且不推进索引代次/盖章。"""
        app = _make_app(refresh_paths=[], full_audit_interval_days=7)
        svc = RefreshService(app)

        with patch.object(app, "initial_scan_a",
                          side_effect=AuditCoverageIncompleteError("1/2 个 A 根不可达")), \
             patch.object(app, "scan_a_to_b_full_sync"):
            result = svc.run_full_audit_now()

        assert result["ok"] is True, f"实际: {result!r}"
        assert result["status"] == "completed", f"实际: {result!r}"
        assert result["coverage_incomplete"] is True, f"实际: {result!r}"
        assert "未巡查" in (result.get("warning") or ""), f"实际: {result!r}"
        app.db.complete_index_generation.assert_not_called()
        app.db.touch_verified_by_mapping.assert_not_called()
        app.db.set_control.assert_called_once()


class TestBDeleteIndependence:
    """验证 B 区删除不受 refresh_paths 影响。

    B 区删除由 BAreaEventHandler 处理，使用 BRecord.webdav_path 执行云端删除，
    与 RefreshService 的 refresh_paths 配置完全解耦。
    """

    def test_refresh_cycle_with_empty_paths_does_not_call_b_delete(self):
        """refresh_paths=[] 时，刷新周期不应调用任何 B 区删除相关方法。"""
        app = _make_app(refresh_paths=[], strm_engine_paths=["/strm"])
        svc = RefreshService(app)

        with patch.object(svc, "_maybe_run_full_audit"), \
             patch.object(svc, "_sync_and_scan_protected_roots") as m_sync, \
             patch.object(svc, "_scan_and_sync") as m_scan, \
             patch.object(app, "cleanup_b_redundant") as m_cleanup_b, \
             patch.object(app, "cleanup_b_zombies_under_folder") as m_cleanup_zombies:
            svc.execute_refresh_cycle()

        # 刷新周期不应触发 B 区冗余清理（那是局部触发的）
        m_cleanup_b.assert_not_called()
        m_cleanup_zombies.assert_not_called()
        # 但也不会阻止 watchdog 的 handle_b_deleted（那是异步事件驱动的）

    def test_refresh_cycle_with_paths_does_not_call_b_delete(self):
        """refresh_paths 非空时，刷新周期也不应调用 B 区删除相关方法。"""
        app = _make_app(refresh_paths=["/strm"], strm_engine_paths=["/strm"])
        svc = RefreshService(app)

        with patch.object(svc, "_maybe_run_full_audit"), \
             patch.object(svc, "_sync_and_scan_protected_roots"), \
             patch.object(svc, "_check_engine_accessibility", return_value={"/strm"}), \
             patch.object(svc, "_calculate_safe_refresh_paths", return_value=["/strm"]), \
             patch.object(svc, "_execute_webdav_refreshes", return_value=[]), \
             patch.object(svc, "_wait_for_sync"), \
             patch.object(svc, "_scan_and_sync"), \
             patch.object(svc, "_persist_snapshot"), \
             patch.object(app, "cleanup_b_redundant") as m_cleanup_b, \
             patch.object(app, "cleanup_b_zombies_under_folder") as m_cleanup_zombies:
            svc.execute_refresh_cycle()

        m_cleanup_b.assert_not_called()
        m_cleanup_zombies.assert_not_called()


# ============================================================
# _run_cycle_with_breaker（熔断器）
# ============================================================


class TestCircuitBreaker:
    """测试 _run_cycle_with_breaker 的连续失败熔断逻辑。"""

    def test_success_resets_counter(self):
        """成功执行 → _consecutive_failures 归零。"""
        app = _make_app(refresh_paths=["/strm"])
        svc = RefreshService(app)
        svc._consecutive_failures = 5  # 模拟之前有失败
        svc._last_error_summary = "old error"

        with patch.object(svc, "execute_refresh_cycle"):
            svc._run_cycle_with_breaker()

        assert svc._consecutive_failures == 0
        assert svc._last_error_summary == ""

    def test_first_failures_log_error_with_traceback(self):
        """前 N 次失败 → ERROR 级别 + exc_info（全栈）。"""
        app = _make_app(refresh_paths=["/strm"])
        svc = RefreshService(app)

        with patch.object(svc, "execute_refresh_cycle",
                          side_effect=sqlite3.OperationalError("readonly")):
            with patch("refresh_service.logging") as mock_log:
                svc._run_cycle_with_breaker()

        assert svc._consecutive_failures == 1
        mock_log.error.assert_called_once()
        # exc_info=True 在 error 调用中
        assert mock_log.error.call_args[1].get("exc_info") is True

    def test_after_threshold_logs_warning_summary(self):
        """超过阈值后 → WARNING 级别摘要，不再打全栈。"""
        app = _make_app(refresh_paths=["/strm"])
        svc = RefreshService(app)
        svc._consecutive_failures = svc._CIRCUIT_BREAKER_THRESHOLD  # 已达阈值

        with patch.object(svc, "execute_refresh_cycle",
                          side_effect=sqlite3.OperationalError("readonly")):
            with patch("refresh_service.logging") as mock_log:
                svc._run_cycle_with_breaker()

        assert svc._consecutive_failures == svc._CIRCUIT_BREAKER_THRESHOLD + 1
        mock_log.warning.assert_called_once()
        mock_log.error.assert_not_called()
        # 摘要包含错误类型
        assert "OperationalError" in svc._last_error_summary

    def test_recovery_after_failures_logs_info(self):
        """连续失败后恢复 → INFO 级别记录恢复信息。"""
        app = _make_app(refresh_paths=["/strm"])
        svc = RefreshService(app)
        svc._consecutive_failures = 5
        svc._last_error_summary = "old error"

        with patch.object(svc, "execute_refresh_cycle"):
            with patch("refresh_service.logging") as mock_log:
                svc._run_cycle_with_breaker()

        assert svc._consecutive_failures == 0
        mock_log.info.assert_called_once()
        assert "恢复正常" in mock_log.info.call_args[0][0]
        assert "5" in str(mock_log.info.call_args[0])

    def test_consecutive_failures_accumulate(self):
        """连续失败正确累加。"""
        app = _make_app(refresh_paths=["/strm"])
        svc = RefreshService(app)

        with patch.object(svc, "execute_refresh_cycle",
                          side_effect=RuntimeError("boom")):
            with patch("refresh_service.logging"):
                svc._run_cycle_with_breaker()
                svc._run_cycle_with_breaker()
                svc._run_cycle_with_breaker()

        assert svc._consecutive_failures == 3

    def test_threshold_is_three(self):
        """熔断阈值为 3。"""
        assert RefreshService._CIRCUIT_BREAKER_THRESHOLD == 3


# ============================================================
# Admin API 不可信时保护根快照
# ============================================================


class TestPersistSnapshotFailClosed:
    """验证 _persist_snapshot 在 Admin API 不可用时不覆盖已有快照。"""

    def test_empty_accessible_engines_preserves_snapshot(self):
        """engine_set 非空但 accessible_engines 为空时，不调用 persist。"""
        app = _make_app(refresh_paths=["/strm"])
        svc = RefreshService(app)

        svc._persist_snapshot(
            accessible_engines=set(),
            engine_set={"/strm_m1", "/strm_m2"},
        )
        # 不应调用 persist_current_roots_snapshot
        app.persist_current_roots_snapshot.assert_not_called()

    def test_empty_engine_set_clears_snapshot(self):
        """engine_set 为空时，传递 None（清除快照）。"""
        app = _make_app(refresh_paths=["/strm"])
        svc = RefreshService(app)

        svc._persist_snapshot(
            accessible_engines=set(),
            engine_set=set(),
        )
        app.persist_current_roots_snapshot.assert_called_once_with(
            valid_engine_paths=None)

    def test_accessible_engines_saves_snapshot(self):
        """有可访问引擎时，正常保存快照。"""
        app = _make_app(refresh_paths=["/strm"])
        svc = RefreshService(app)

        svc._persist_snapshot(
            accessible_engines={"/strm_m1"},
            engine_set={"/strm_m1", "/strm_m2"},
        )
        app.persist_current_roots_snapshot.assert_called_once_with(
            valid_engine_paths=["/strm_m1"])


class TestFullAuditTouchVerified:
    """D'.1: 测试 _maybe_run_full_audit 成功后 touch_verified_by_mapping 被调用"""

    def test_full_audit_calls_touch_verified_by_mapping(self):
        """全量审计完成后，每个 mapping 应调用 touch_verified_by_mapping"""
        app = _make_app(refresh_paths=[], full_audit_interval_days=7)
        app.db.get_control.return_value = "0"
        app._current_mapping_ids.return_value = ["m1", "m2"]
        # 提供 mock mapping 对象（需有 mapping_id 和 a_root 属性）
        mock_m1 = MagicMock()
        mock_m1.mapping_id = "m1"
        mock_m1.a_root = "/a_root_m1"
        mock_m2 = MagicMock()
        mock_m2.mapping_id = "m2"
        mock_m2.a_root = "/a_root_m2"
        app.a_b_mappings = [mock_m1, mock_m2]
        svc = RefreshService(app)

        with patch("refresh_service.time.time", return_value=8 * 86400), \
             patch.object(app, "initial_scan_a"), \
             patch.object(app, "scan_a_to_b_full_sync"), \
             patch.object(app.db, "touch_verified_by_mapping") as m_touch:
            svc._maybe_run_full_audit()

        # 应对每个 mapping 调用一次 touch_verified_by_mapping
        assert m_touch.call_count == 2
        calls = {c.args[0] for c in m_touch.call_args_list}
        assert calls == {"m1", "m2"}

    def test_full_audit_touch_uses_current_timestamp(self):
        """touch_verified_by_mapping 应使用当前时间戳"""
        app = _make_app(refresh_paths=[], full_audit_interval_days=7)
        app.db.get_control.return_value = "0"
        app._current_mapping_ids.return_value = ["m1"]
        mock_m1 = MagicMock()
        mock_m1.mapping_id = "m1"
        mock_m1.a_root = "/a_root_m1"
        app.a_b_mappings = [mock_m1]
        svc = RefreshService(app)

        with patch("refresh_service.time.time", return_value=8 * 86400), \
             patch.object(app, "initial_scan_a"), \
             patch.object(app, "scan_a_to_b_full_sync"), \
             patch.object(app.db, "touch_verified_by_mapping") as m_touch:
            svc._maybe_run_full_audit()

        call_args = m_touch.call_args
        # 第三个参数应该是当前时间戳（8 * 86400）
        assert call_args.args[2] == 8 * 86400


class TestRunFullAuditNow:
    """测试 RefreshService.run_full_audit_now() 薄封装"""

    def test_run_full_audit_now_calls_correct_sequence(self):
        """run_full_audit_now 应按序调用 initial_scan_a → scan_a_to_b_full_sync → complete_index_generation"""
        app = _make_app(refresh_paths=[], full_audit_interval_days=7)
        app.db.get_control.return_value = "0"
        app._current_mapping_ids.return_value = ["m1"]
        app.db.get_index_metadata.return_value = {"index_generation": 2, "index_generation_at": 999.0}
        svc = RefreshService(app)

        with patch("refresh_service.time.time", return_value=8 * 86400), \
             patch.object(app, "initial_scan_a") as m_scan, \
             patch.object(app, "scan_a_to_b_full_sync") as m_sync:
            result = svc.run_full_audit_now()

        # 全量审计为 A 区快照权威自愈触发源：必须绕过内容读跳检
        m_scan.assert_called_once_with(use_bulk=False, a_roots=None, use_snapshot=False)
        m_sync.assert_called_once_with(valid_engine_paths=None, use_bulk=False)
        app.db.complete_index_generation.assert_called_once()
        app.db.set_control.assert_called_once_with("last_full_audit_at", str(8 * 86400))
        assert result["ok"] is True
        assert result["status"] == "completed"
        assert result["index_generation"] == 2

    def test_run_full_audit_now_resets_last_full_audit_at(self):
        """run_full_audit_now 必须重置 _last_full_audit_at 以对齐周期审计"""
        app = _make_app(refresh_paths=[], full_audit_interval_days=7)
        app.db.get_control.return_value = "0"
        app._current_mapping_ids.return_value = []
        app.db.get_index_metadata.return_value = {}
        svc = RefreshService(app)

        assert svc._last_full_audit_at == 0.0

        with patch("refresh_service.time.time", return_value=8 * 86400), \
             patch.object(app, "initial_scan_a"), \
             patch.object(app, "scan_a_to_b_full_sync"):
            svc.run_full_audit_now()

        assert svc._last_full_audit_at == 8 * 86400

    def test_run_full_audit_now_returns_already_running_if_periodic_in_progress(self):
        """如果周期审计正在进行，run_full_audit_now 应返回 already_running"""
        app = _make_app(refresh_paths=[], full_audit_interval_days=7)
        svc = RefreshService(app)
        svc._full_audit_in_progress = True  # 模拟周期审计正在进行

        result = svc.run_full_audit_now()
        assert result["status"] == "already_running"
        assert result["ok"] is False

    def test_periodic_skips_if_manual_in_progress(self):
        """如果手动审计正在进行，_maybe_run_full_audit 应跳过（返回 False）"""
        app = _make_app(refresh_paths=[], full_audit_interval_days=7)
        app.db.get_control.return_value = "0"
        svc = RefreshService(app)
        svc._full_audit_in_progress = True  # 模拟手动审计正在进行

        with patch.object(app, "initial_scan_a") as m_scan:
            result = svc._maybe_run_full_audit()

        assert result is False
        m_scan.assert_not_called()


# ============================================================
# P0 Step 4: _scan_and_sync 双变量拆分接线（mount 闸门 × 云前缀过滤）
# ============================================================


class TestScanAndSyncEngineFilterWiring:
    """v6 P0 接线：mount 交集闸门与云资源前缀过滤变量严格分离。

    红线：严禁把 helper 展开值直接喂进 mount∩mount 交集（全失配 → no-op）。
    """

    def test_positive_passes_cloud_prefixes_to_sync(self):
        """闸门命中时，传给同步的是 get_engine_filter_paths 的云前缀而非挂载值。"""
        app = _make_app(refresh_paths=["/strm"], strm_engine_paths=["/strm"])
        app.get_engine_paths_for_a_roots.return_value = ["/strm"]
        app.get_engine_filter_paths.return_value = ["/云盘X/番剧"]
        svc = RefreshService(app)
        root = Path("C:/a1")
        with patch.object(app, "initial_scan_a") as m_scan_a, \
             patch.object(app, "scan_a_to_b_full_sync") as m_sync, \
             patch.object(app, "cleanup_local_empty_dirs"):
            svc._scan_and_sync({"/strm"}, a_roots=[root])
        m_scan_a.assert_called_once_with(use_bulk=False, a_roots=[root])
        # 关键断言：同步收到的是 helper 的云前缀输出
        assert m_sync.call_args.kwargs["valid_engine_paths"] == ["/云盘X/番剧"]
        assert m_sync.call_args.kwargs["use_bulk"] is False
        # helper 以 mount 交集作闸门入参
        assert app.get_engine_filter_paths.call_args.kwargs["allowed_mounts"] == {"/strm"}

    def test_root_mount_gate_comparable_with_helper_engine_set(self):
        """F-A 接线钉：闸门侧根挂载 '/' 与 helper 引擎集合可比（不被交集吞掉）。"""
        app = _make_app(refresh_paths=["/strm"], strm_engine_paths=["/"])
        app.get_engine_paths_for_a_roots.return_value = ["/"]
        app.get_engine_filter_paths.return_value = ["/云盘X/番剧"]
        svc = RefreshService(app)
        with patch.object(app, "initial_scan_a"), \
             patch.object(app, "scan_a_to_b_full_sync") as m_sync, \
             patch.object(app, "cleanup_local_empty_dirs"):
            svc._scan_and_sync({"/"}, a_roots=[Path("C:/a1")])
        # 根挂载 '/' 在 mount∩mount 中存活 → 同步执行且 helper 收到 {"/"}
        m_sync.assert_called_once()
        assert app.get_engine_filter_paths.call_args.kwargs["allowed_mounts"] == {"/"}

    def test_empty_accessible_engines_skips_sync(self):
        """accessible_engines 为空 → 跳过 A→B（现行告警保持），helper 不被调用。"""
        app = _make_app(refresh_paths=["/strm"], strm_engine_paths=["/strm"])
        app.get_engine_paths_for_a_roots.return_value = ["/strm"]
        svc = RefreshService(app)
        with patch.object(app, "initial_scan_a"), \
             patch.object(app, "scan_a_to_b_full_sync") as m_sync, \
             patch.object(app, "get_engine_filter_paths") as m_helper:
            svc._scan_and_sync(set(), a_roots=[Path("C:/a1")])
        m_sync.assert_not_called()
        m_helper.assert_not_called()

    def test_mount_intersection_empty_skips_sync(self):
        """A 根映射的 mount 与可访问引擎交集为空 → 跳过，helper 不被调用。"""
        app = _make_app(refresh_paths=["/strm"], strm_engine_paths=["/strm"])
        app.get_engine_paths_for_a_roots.return_value = ["/other"]
        svc = RefreshService(app)
        with patch.object(app, "initial_scan_a"), \
             patch.object(app, "scan_a_to_b_full_sync") as m_sync, \
             patch.object(app, "get_engine_filter_paths") as m_helper:
            svc._scan_and_sync({"/strm"}, a_roots=[Path("C:/a1")])
        m_sync.assert_not_called()
        m_helper.assert_not_called()

    def test_no_roots_skips_before_any_engine_logic(self):
        """无匹配 A 根 → 早退，initial_scan_a 与同步都不执行。"""
        app = _make_app(refresh_paths=["/strm"], strm_engine_paths=["/strm"])
        svc = RefreshService(app)
        with patch.object(app, "initial_scan_a") as m_scan_a, \
             patch.object(app, "scan_a_to_b_full_sync") as m_sync:
            svc._scan_and_sync({"/strm"}, a_roots=[])
        m_scan_a.assert_not_called()
        m_sync.assert_not_called()


# ============================================================
# 缺陷 A：readonly per-root 隔离 + 聚合 raise（c7 §一，红测先行）
# ============================================================

class TestPartialRefreshIsolation:
    """单 root 失败不中断整周期；聚合为 PartialRefreshError 在末尾抛出。

    桩定前提（v7/v8-F1 防陷阱）：build_mock_app 默认 full_audit_interval_days=0
    ——execute_refresh_cycle 开头的全量审计分支既不预增 _consecutive_failures，
    也不因 full_audit_ran=True 跳过 _scan_and_sync。
    """

    def _make_svc(self, refresh_paths):
        app = _make_app(refresh_paths=refresh_paths, strm_engine_paths=["/e"])
        return app, RefreshService(app)

    def test_second_root_readonly_still_refreshes_all_and_raises(self):
        """3 root 第 2 个抛 readonly → 仍刷 3 root、三段均执行、末尾 raise"""
        app, svc = self._make_svc(["/e/a", "/e/b", "/e/c"])
        calls = []

        def _fake_refresh(root, depth):
            calls.append(root)
            if root == "/e/b":
                raise sqlite3.OperationalError("attempt to write a readonly database")

        with patch.object(svc, "_sync_and_scan_protected_roots"), \
             patch.object(svc, "_check_engine_accessibility", return_value={"/e"}), \
             patch.object(app, "refresh_webdav_root", side_effect=_fake_refresh) as m_refresh, \
             patch.object(svc, "_wait_for_sync") as m_wait, \
             patch.object(svc, "_scan_and_sync") as m_scan, \
             patch.object(svc, "_persist_snapshot") as m_persist:
            with pytest.raises(PartialRefreshError) as ei:
                svc.execute_refresh_cycle()
        assert m_refresh.call_count == 3
        assert calls == ["/e/a", "/e/b", "/e/c"]
        m_wait.assert_called_once()
        m_scan.assert_called_once()
        m_persist.assert_called_once()
        msg = str(ei.value)
        # c.9.2 Task 1：%-style 多参对 RuntimeError 永不插值（str(exc) 为元组
        # repr），"1"/"3" 在元组字面量中平凡成立属自证断言——收紧为前缀匹配。
        assert msg.startswith("刷新周期部分失败: 1/3"), msg  # 失败计数 1 / 总 root 数 3
        assert "readonly" in msg          # 摘要含失败类型

    def test_only_refresh_section_isolated_too(self):
        """only_refresh 段 readonly 同样隔离并聚合 raise"""
        app, svc = self._make_svc(["/solo"])  # 不属于 /e 引擎 → only_refresh
        with patch.object(svc, "_sync_and_scan_protected_roots"), \
             patch.object(svc, "_check_engine_accessibility", return_value={"/e"}), \
             patch.object(app, "refresh_webdav_root_readonly",
                          side_effect=sqlite3.OperationalError("attempt to write a readonly database")) as m_ro, \
             patch.object(svc, "_wait_for_sync") as m_wait, \
             patch.object(svc, "_scan_and_sync") as m_scan, \
             patch.object(svc, "_persist_snapshot") as m_persist:
            with pytest.raises(PartialRefreshError):
                svc.execute_refresh_cycle()
        m_ro.assert_called_once()
        m_wait.assert_called_once()
        m_scan.assert_called_once()
        m_persist.assert_called_once()

    def test_all_success_no_raise_no_warning(self, caplog):
        """全成功 → 不 raise、零新 WARNING"""
        import logging as _logging
        app, svc = self._make_svc(["/e/a"])
        with caplog.at_level(_logging.WARNING):
            with patch.object(svc, "_sync_and_scan_protected_roots"), \
                 patch.object(svc, "_check_engine_accessibility", return_value={"/e"}), \
                 patch.object(app, "refresh_webdav_root"), \
                 patch.object(svc, "_wait_for_sync"), \
                 patch.object(svc, "_scan_and_sync"), \
                 patch.object(svc, "_persist_snapshot"):
                svc.execute_refresh_cycle()  # 不 raise
        warnings = [r for r in caplog.records if r.levelno >= _logging.WARNING]
        assert warnings == []

    def test_breaker_counts_partial_refresh_error(self):
        """breaker 集成：PartialRefreshError → _consecutive_failures==1、summary 含类型"""
        app, svc = self._make_svc(["/e/a"])
        with patch.object(svc, "_sync_and_scan_protected_roots"), \
             patch.object(svc, "_check_engine_accessibility", return_value={"/e"}), \
             patch.object(app, "refresh_webdav_root",
                          side_effect=sqlite3.OperationalError("attempt to write a readonly database")), \
             patch.object(svc, "_wait_for_sync"), \
             patch.object(svc, "_scan_and_sync"), \
             patch.object(svc, "_persist_snapshot"):
            svc._run_cycle_with_breaker()
        assert svc._consecutive_failures == 1
        assert "PartialRefreshError" in svc._last_error_summary

    def test_no_failure_keeps_breaker_clean(self):
        """全成功周期 → _consecutive_failures 归零路径不受影响"""
        app, svc = self._make_svc(["/e/a"])
        svc._consecutive_failures = 2  # 预置历史失败
        with patch.object(svc, "_sync_and_scan_protected_roots"), \
             patch.object(svc, "_check_engine_accessibility", return_value={"/e"}), \
             patch.object(app, "refresh_webdav_root"), \
             patch.object(svc, "_wait_for_sync"), \
             patch.object(svc, "_scan_and_sync"), \
             patch.object(svc, "_persist_snapshot"):
            svc._run_cycle_with_breaker()
        assert svc._consecutive_failures == 0
        assert svc._last_error_summary == ""
