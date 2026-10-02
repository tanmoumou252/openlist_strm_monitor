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

    def test_calls_all_orchestration_steps(self):
        """execute_refresh_cycle 应该按序调用所有步骤"""
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
            m_cleanup.assert_called_once()
            m_calc.assert_called_once()
            m_exec.assert_called_once()
            m_wait.assert_called_once()
            m_scan.assert_called_once()
            m_persist.assert_called_once()

    def test_cleanup_receives_accessible_engines(self):
        """_cleanup_a_for_update_mode 接收可访问的引擎路径"""
        app = _make_app(refresh_paths=["/strm"], strm_engine_paths=["/strm"])
        svc = RefreshService(app)

        with patch.object(svc, "_sync_and_scan_protected_roots"), \
             patch.object(svc, "_check_engine_accessibility", return_value={"/strm", "/data"}), \
             patch.object(svc, "_cleanup_a_for_update_mode") as m_cleanup, \
             patch.object(svc, "_calculate_safe_refresh_paths", return_value=[]), \
             patch.object(svc, "_execute_webdav_refreshes"), \
             patch.object(svc, "_wait_for_sync"), \
             patch.object(svc, "_scan_and_sync"), \
             patch.object(svc, "_persist_snapshot"):

            svc.execute_refresh_cycle()
            m_cleanup.assert_called_once_with({"/strm", "/data"})

    def test_empty_engine_set_completes_without_error(self):
        """可访问引擎为空集合时，完整编排流程仍正常完成不抛异常"""
        app = _make_app(refresh_paths=["/strm"], strm_engine_paths=["/strm"])
        svc = RefreshService(app)

        with patch.object(svc, "_sync_and_scan_protected_roots"), \
             patch.object(svc, "_check_engine_accessibility", return_value=set()), \
             patch.object(svc, "_cleanup_a_for_update_mode"), \
             patch.object(svc, "_calculate_safe_refresh_paths", return_value=[]), \
             patch.object(svc, "_execute_webdav_refreshes"), \
             patch.object(svc, "_wait_for_sync"), \
             patch.object(svc, "_scan_and_sync"), \
             patch.object(svc, "_persist_snapshot"):
            # 空引擎路径应该正常完成
            svc.execute_refresh_cycle()


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


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
