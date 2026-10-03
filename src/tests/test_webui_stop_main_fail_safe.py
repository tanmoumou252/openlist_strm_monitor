"""stop_main 停止失败的相位恢复契约。

用户可见行为级断言：stop 失败后经 get_main_status() 必须可见 phase == "fail_safe"
（server 侧与 svc 侧相位同步恢复），前端既有 fail_safe 分支即可提供启动按钮重试，
消除「stopping 双按钮全隐」的不可恢复 UI 死角。夹具复用 conftest 的
webui_server_shared（真实 WebUIServer，tmp 落盘）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

pytestmark = pytest.mark.webui


class _StopFailService:
    """stop() 抛异常的最小 svc 替身；相位/摘要语义对齐 AppService 契约。"""

    def __init__(self):
        self._phase = "scanning_a"
        self._error = None

    def set_phase(self, phase, error=None):
        self._phase = phase
        self._error = error

    def get_state_summary(self):
        return {
            "phase": self._phase,
            "is_running": self._phase not in {"stopped", "fail_safe"},
            "is_ready": False,
            "error": self._error,
            "progress": {},
        }

    def stop(self):
        raise RuntimeError("boom")


class _FailSafeStaleService:
    """fail_safe 残留替身：句柄在、未运行；stop 默认成功，可设为抛异常。"""

    def __init__(self, stop_raises=False):
        self._phase = "fail_safe"
        self._error = "上次的错误"
        self._stop_raises = stop_raises
        self.stop_called = 0

    def set_phase(self, phase, error=None):
        self._phase = phase
        self._error = error

    def get_state_summary(self):
        return {
            "phase": self._phase,
            "is_running": self._phase not in {"stopped", "fail_safe"},
            "is_ready": False,
            "error": self._error,
            "progress": {},
        }

    def stop(self):
        self.stop_called += 1
        if self._stop_raises:
            raise RuntimeError("stale stop boom")


class _RecoverableStopService:
    """首次 stop 抛异常、此后成功的可恢复替身（再次停止清错用例）。"""

    def __init__(self):
        self._phase = "scanning_a"
        self._error = None
        self._fail_next = True

    def set_phase(self, phase, error=None):
        self._phase = phase
        self._error = error

    def get_state_summary(self):
        return {
            "phase": self._phase,
            "is_running": self._phase not in {"stopped", "fail_safe"},
            "is_ready": False,
            "error": self._error,
            "progress": {},
        }

    def stop(self):
        if self._fail_next:
            self._fail_next = False
            raise RuntimeError("boom")


def test_stop_main_failure_lands_fail_safe_visible_via_get_main_status(
        webui_server_shared):
    server, _base, _token = webui_server_shared
    server._app_service = _StopFailService()
    server._app_worker_thread = None
    result = server.stop_main()
    assert result["success"] is False
    # 用户可见行为级断言：主路径经 svc.get_state_summary()，仅改 server 侧
    # _app_phase 不够——svc 相位必须同步落 fail_safe
    status = server.get_main_status()
    assert status["phase"] == "fail_safe", (
        "stop_main 失败后 get_main_status 必须可见 fail_safe 相位（可重试），"
        f"实际 {status['phase']!r}")
    assert status["running"] is False
    # 回归锚（防 svc 摘要 error 直通路径回归）：stop 失败错误文本必须经
    # get_main_status 的 error 语义位对用户可见；本断言不承担新行为的
    # 失败复现职责
    assert "boom" in str(status.get("error")), (
        f"stop 失败错误文本必须经 error 语义位可见，实际返回体: {status!r}")


def test_stop_main_failure_preserves_retry_handles(webui_server_shared):
    """停止失败后保留 _app_service/_app_worker_thread 供再次 stop 重试，
    不得伪造 stopped 终态清理。"""
    server, _base, _token = webui_server_shared
    svc = _StopFailService()
    server._app_service = svc
    server._app_worker_thread = None
    server.stop_main()
    assert server._app_service is svc, (
        "停止失败后必须保留 svc 句柄供重试，不得置 None")
    assert server._app_phase == "fail_safe"


def test_stop_main_failure_then_success_clears_error(webui_server_shared):
    """失败后再次成功 stop → phase=stopped 且 error 不再可见。"""
    server, _base, _token = webui_server_shared
    svc = _RecoverableStopService()
    server._app_service = svc
    server._app_worker_thread = None
    first = server.stop_main()
    assert first["success"] is False
    status_fail = server.get_main_status()
    assert "boom" in str(status_fail), "失败错误必须先可见（红前提）"
    second = server.stop_main()
    assert second["success"] is True
    status_ok = server.get_main_status()
    assert status_ok["phase"] == "stopped"
    assert "boom" not in str(status_ok), (
        f"成功停止后上次错误必须清空，实际返回体: {status_ok!r}")


def test_start_main_after_fail_safe_stale_cleans_or_rejects(webui_server_shared):
    """fail_safe 残留句柄（is_running=False）时 start_main 不得覆盖旧句柄。

    旧 svc.stop() 抛异常 → fail-closed 拒绝启动且句柄保留。
    """
    server, _base, _token = webui_server_shared
    svc = _FailSafeStaleService(stop_raises=True)
    server._app_service = svc
    server._app_worker_thread = None
    result = server.start_main()
    assert result["success"] is False
    assert "fail_safe" in result["message"]
    assert server._app_service is svc, (
        "清理失败时旧句柄必须保留，不得被新建实例覆盖")


def test_start_main_after_fail_safe_stale_cleans_and_proceeds(webui_server_shared):
    """旧 svc.stop() 成功 → 清空残留句柄后继续正常启动链路。

    预置 _config=None 封闭夹具：清理块位于 config 闸之前，fail_safe 残留
    清理路径仍被完整覆盖；清理后确定性止于「配置未加载」闸，防止
    MagicMock truthy config 闯入真实 AppService 构造链（后台线程副作用风险）。
    """
    server, _base, _token = webui_server_shared
    server._config = None
    svc = _FailSafeStaleService(stop_raises=False)
    server._app_service = svc
    server._app_worker_thread = None
    result = server.start_main()
    assert server._app_service is not svc, "旧句柄必须已被清理"
    assert svc.stop_called == 1
    assert result["success"] is False
    assert "配置未加载" in result["message"], (
        f"清理完成后应确定性止于配置闸（封闭夹具），实际: {result!r}")
