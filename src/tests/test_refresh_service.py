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
    strm_monitored_paths: list[str] | None = None,
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
        strm_monitored_paths=strm_monitored_paths,
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
    """测试 _analyze_paths 的集合运算逻辑"""

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

    def test_full_overlap(self):
        """refresh_paths 和 engine_paths 完全重叠"""
        app = _make_app(
            refresh_paths=["/strm", "/data"],
            strm_engine_paths=["/strm", "/data"],
        )
        svc = RefreshService(app)
        analysis = svc._analyze_paths()

        assert set(analysis.valid_refresh_paths) == {"/strm", "/data"}
        assert analysis.only_refresh == set()
        assert analysis.only_engine == set()

    def test_partial_overlap(self):
        """部分重叠 → valid 是交集，only_refresh / only_engine 是差集"""
        app = _make_app(
            refresh_paths=["/strm", "/extra_refresh"],
            strm_engine_paths=["/strm", "/extra_engine"],
        )
        svc = RefreshService(app)
        analysis = svc._analyze_paths()

        assert analysis.valid_refresh_paths == ["/strm"]
        assert analysis.only_refresh == {"/extra_refresh"}
        assert analysis.only_engine == {"/extra_engine"}

    def test_monitored_paths_merged_into_engine(self):
        """strm_monitored_paths 被合并到 engine_set 中"""
        app = _make_app(
            refresh_paths=["/strm"],
            strm_engine_paths=["/strm"],
            strm_monitored_paths=["/manual_path"],
        )
        svc = RefreshService(app)
        analysis = svc._analyze_paths()

        # /manual_path 通过 monitored 合入 engine_set
        assert "/manual_path" in analysis.engine_set
        # /manual_path 不在 refresh_paths → only_engine
        assert "/manual_path" in analysis.only_engine

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
