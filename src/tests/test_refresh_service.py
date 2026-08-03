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

from refresh_service import RefreshService
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
        mock_thread.join.assert_called_once_with(timeout=2)

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
        scan_a.assert_called_once_with(use_bulk=False, a_roots=None)
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

        """execute_refresh_cycle 应该按序调用所有步骤（不再调用 _cleanup_a_for_update_mode）"""
        app = _make_app(refresh_paths=["/strm"], strm_engine_paths=["/strm"])
        svc = RefreshService(app)

        # mock 所有内部方法
        with patch.object(svc, "_sync_and_scan_protected_roots") as m_sync, \
             patch.object(svc, "_check_engine_accessibility", return_value={"/strm"}) as m_check, \
             patch.object(svc, "_cleanup_a_for_update_mode") as m_cleanup, \
             patch.object(svc, "_calculate_safe_refresh_paths", return_value=["/strm"]) as m_calc, \
             patch.object(svc, "_execute_webdav_refreshes") as m_exec, \
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
             patch.object(svc, "_execute_webdav_refreshes"), \
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

    def test_disabled_worker_waits_without_running_cycles(self):
        app = _make_app(refresh_paths=["/strm"], refresh_enabled=False)
        svc = RefreshService(app)
        svc._run_cycle_with_breaker = MagicMock()
        svc._running = True
        worker = threading.Thread(target=svc._worker, daemon=True)
        svc._thread = worker
        worker.start()
        time.sleep(0.05)
        assert svc._run_cycle_with_breaker.call_count == 1
        assert worker.is_alive()
        svc.stop()

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
             patch.object(svc, "_execute_webdav_refreshes"), \
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
# Task 4: Admin API 不可信时保护根快照
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
    """A'.1: 测试 RefreshService.run_full_audit_now() 薄封装"""

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

        m_scan.assert_called_once_with(use_bulk=False, a_roots=None)
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
