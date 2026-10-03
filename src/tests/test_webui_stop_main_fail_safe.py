"""stop_main 停止失败的相位恢复契约。

用户可见行为级断言：stop 失败后经 get_main_status() 必须可见 phase == "fail_safe"
（server 侧与 svc 侧相位同步恢复），前端既有 fail_safe 分支即可提供启动按钮重试，
消除「stopping 双按钮全隐」的不可恢复 UI 死角。夹具复用 conftest 的
webui_server_shared（真实 WebUIServer，tmp 落盘）。
"""
from __future__ import annotations

import sys
import threading
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
        # 成功停止后相位须落 stopped，使 stop_main 清句柄前的 is_running
        # 权威复核判为假、放行正常清理（否则残留 stopping 被误判为引擎存活）。
        self._phase = "stopped"


class _RevivingStaleService:
    """fail_safe 残留替身（复活形态）：stop() 返回成功但旧引擎随后照常
    start（get_state_summary().is_running 翻真），模拟旧 worker 停在最后一次
    generation 检查与 svc.start() 之间、stop 先完成、"清理成功"判定后旧引擎
    复活的竞态。"""

    def __init__(self):
        self._phase = "fail_safe"
        self._error = "上次的错误"
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
        # stop 已返回，但旧 worker 随后照常 start 旧引擎
        self._phase = "scanning_a"


class _MockStaleSvc:
    """start/stop/phase 多路可控替身，对齐 AppService 摘要契约：
    - stop_raises：stop() 抛错（模拟停止失败）；
    - start_raises：start() 抛错（保留能力）；
    - is_revive_after_stop：stop() 后把相位翻成 running 域（模拟停止返回但引擎复活）；
    - raise_on_start_phase：set_phase("starting") 抛错——供启动异常回滚用例把异常
      落点后置于句柄赋值之后（get_config_status 已放行、句柄已赋值）；
    - get_config_status() 返回 ready：让 start_main 通过配置就绪闸，推进到句柄赋值。"""

    def __init__(self, stop_raises=False, start_raises=False,
                 is_revive_after_stop=False, raise_on_start_phase=False):
        self._phase = "fail_safe"
        self._error = "上次错误"
        self._stop_raises = stop_raises
        self._start_raises = start_raises
        self._revive = is_revive_after_stop
        self._raise_on_start_phase = raise_on_start_phase
        self.stop_called = 0

    def get_config_status(self):
        return {"status": "ready"}

    def set_phase(self, phase, error=None):
        if self._raise_on_start_phase and phase == "starting":
            raise RuntimeError("startup phase transition boom")
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

    def start(self):
        if self._start_raises:
            raise RuntimeError("start boom")

    def stop(self):
        self.stop_called += 1
        if self._stop_raises:
            raise RuntimeError("stale stop boom")
        if self._revive:
            self._phase = "scanning_a"


class _BlockingWorkerStub:
    """join(5.0) 超时后仍存活的 worker 替身：run() 阻塞在未 set 的 Event 上，
    测试结束后经 release() 释放，避免线程泄漏。

    start_blocked=False 形态：Event 先 set 后启动线程，线程立即退出——
    join 立即返回且线程已消亡，供「复活」用例模拟 join 成功判据被满足。
    """

    def __init__(self, start_blocked=True, exit_on_join=False):
        self._ev = threading.Event()
        self._exit_on_join = exit_on_join
        if not start_blocked:
            self._ev.set()
        self._thread = threading.Thread(target=self._ev.wait, daemon=True)
        self._thread.start()

    def is_alive(self):
        return self._thread.is_alive()

    def join(self, timeout=None):
        # exit_on_join 形态：线程在 join 被调用时才退出——确定性复现生产
        # 时序「join 门槛 is_alive 为真 → join 期间 worker 退出 → join 返回」，
        # 消除"线程先于 is_alive 门槛自然消亡导致 join 块被跳过"的夹具竞态。
        if self._exit_on_join:
            self._ev.set()
        self._thread.join(timeout=timeout)

    def release(self):
        self._ev.set()


def test_start_main_rejects_when_stale_worker_survives_join(webui_server_shared):
    """契约 C2：残留 worker join(5.0) 超时仍存活 → start_main fail-closed
    拒绝且句柄保留（generation 不得推进、句柄不得清空）。"""
    server, _base, _token = webui_server_shared
    svc = _FailSafeStaleService(stop_raises=False)
    worker = _BlockingWorkerStub()
    server._config = None
    server._app_service = svc
    server._app_worker_thread = worker
    gen_before = server._app_generation
    try:
        result = server.start_main()
        assert server._app_generation == gen_before, (
            "拒绝后 _app_generation 不得推进（代次推进=旧 worker 状态面被解管）")
        assert result["success"] is False, (
            f"join 超时仍存活的旧 worker 必须触发拒绝，实际: {result!r}")
        assert "未退出" in result["message"], (
            f"拒绝消息必须指明旧 worker 未退出，实际: {result['message']!r}")
        assert server._app_service is svc, "拒绝后旧 svc 句柄必须保留"
        assert server._app_worker_thread is worker, "拒绝后旧 worker 句柄必须保留"
    finally:
        worker.release()


def test_start_main_rejects_when_stale_engine_revives_after_join(
        webui_server_shared):
    """契约 W1：join 成功 ≠ 引擎已死——旧 worker 停在最后一次 generation
    检查与 svc.start() 之间时，stop 先完成、旧引擎随后照常 start，
    join 正常返回且线程已退出。清理路径必须以 get_state_summary 的
    is_running 权威复核 fail-closed，拒绝启动且句柄保留。"""
    server, _base, _token = webui_server_shared
    svc = _RevivingStaleService()
    worker = _BlockingWorkerStub(start_blocked=True, exit_on_join=True)
    server._config = None
    server._app_service = svc
    server._app_worker_thread = worker
    try:
        result = server.start_main()
        assert result["success"] is False, (
            f"stop 后复活的旧引擎必须触发拒绝，实际: {result!r}")
        assert ("复活" in result["message"]) or ("仍存活" in result["message"]), (
            f"拒绝消息必须指明旧引擎复活/仍存活，实际: {result['message']!r}")
        assert server._app_service is svc, "拒绝后旧 svc 句柄必须保留"
        assert server._app_worker_thread is worker, "拒绝后旧 worker 句柄必须保留"
    finally:
        worker.release()


def test_start_main_gate_failures_land_stopped_with_visible_error(
        webui_server_shared):
    """启动链路配置类失败（配置闸 / not_configured 闸）落 stopped + 具体 error。

    用户配置态不得占用引擎故障态 fail_safe：首次运行未配 mapping 若在 UI 显示
    引擎故障即为误报。stopped 态下仍写 _app_error，经 get_main_status fallback 的
    error 位对用户可见（running=False、phase=stopped、error=具体消息）。
    """
    server, _base, _token = webui_server_shared
    from unittest.mock import Mock
    # 场景一：配置闸（_config=None）
    server._config = None
    server._app_service = None
    server._app_worker_thread = None
    result = server.start_main()
    assert result["success"] is False
    assert server._app_phase == "stopped", (
        f"配置闸失败后相位必须落 stopped，实际 {server._app_phase!r}")
    assert server._app_error == "配置未加载", (
        f"配置闸必须写具体 _app_error，实际 {server._app_error!r}")
    status = server.get_main_status()
    assert status["phase"] == "stopped", (
        f"get_main_status fallback 必须可见 stopped，实际 {status['phase']!r}")
    assert status["running"] is False
    assert "配置未加载" in str(status.get("error")), (
        f"配置闸错误必须经 error 位可见，实际返回体: {status!r}")
    # 场景二：not_configured 闸（Mock config 且 a_b_mappings=[]）
    server._config = Mock(a_b_mappings=[])
    server._app_service = None
    server._app_worker_thread = None
    result = server.start_main()
    assert result["success"] is False
    assert result.get("status") == "not_configured"
    assert server._app_phase == "stopped", (
        f"not_configured 闸失败后相位必须落 stopped，实际 {server._app_phase!r}")
    assert server._app_error == "未配置 A/B mapping", (
        f"not_configured 闸必须写具体 _app_error，实际 {server._app_error!r}")
    status = server.get_main_status()
    assert status["phase"] == "stopped", (
        f"get_main_status fallback 必须可见 stopped，实际 {status['phase']!r}")
    assert status["running"] is False
    assert "未配置 A/B mapping" in str(status.get("error")), (
        f"not_configured 错误必须经 error 位可见，实际返回体: {status!r}")


def test_start_main_cleanup_sets_stopped_with_visible_error_when_gate_blocks(
        webui_server_shared):
    """残留清理成功后被配置闸拦下 → 落 stopped 且陈旧错误被新具体错误覆盖可见。

    清理块把上轮 fail_safe 陈旧错误遗留、新实例就位前的配置闸属用户配置态：
    落 stopped + 写「配置未加载」，不得伪装成「stopped、无错误」丢失失败原因。
    """
    server, _base, _token = webui_server_shared
    server._config = None
    server._app_phase = "fail_safe"
    server._app_error = "上次的错误"
    svc = _FailSafeStaleService(stop_raises=False)
    server._app_service = svc
    server._app_worker_thread = None
    result = server.start_main()
    assert server._app_service is not svc, "清理成功的旧句柄必须已清空"
    assert server._app_phase == "stopped", (
        f"清理成功后被配置闸拦下相位必须落 stopped，实际 {server._app_phase!r}")
    assert "配置未加载" in str(server._app_error), (
        f"配置闸错误必须覆盖写入 _app_error，实际 {server._app_error!r}")
    status = server.get_main_status()
    assert status["phase"] == "stopped", (
        f"get_main_status fallback 必须可见 stopped，实际 {status['phase']!r}")
    assert status["running"] is False
    assert "上次的错误" not in str(status.get("error")), (
        f"陈旧错误必须被新具体错误覆盖，实际返回体: {status!r}")
    assert result["success"] is False
    assert "配置未加载" in result["message"], (
        f"清理后应确定性止于配置闸（封闭夹具），实际: {result!r}")


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


def test_start_main_rejects_when_stale_engine_revives_with_no_worker(
        webui_server_shared):
    """worker 句柄为 None 但残留引擎在 stop 后复活时，权威复核必须无条件拒绝。

    复核曾嵌在 worker.is_alive() 分支内，worker 缺席时被整体跳过，复活引擎
    被放行进入新建链路（句柄被清、generation 推进）。以权威状态复核后，
    worker 缺席亦须拒绝并保留旧句柄。
    """
    server, _base, _token = webui_server_shared
    svc = _RevivingStaleService()
    server._config = None
    server._app_service = svc
    server._app_worker_thread = None
    gen_before = server._app_generation
    result = server.start_main()
    assert result["success"] is False, (
        f"worker 缺席但引擎复活必须拒绝，实际: {result!r}")
    assert ("复活" in result["message"]) or ("仍存活" in result["message"]), (
        f"拒绝消息必须指明旧引擎复活/仍存活，实际: {result['message']!r}")
    assert server._app_service is svc, "拒绝后旧 svc 句柄必须保留，不得被清理链路清空"
    assert server._app_generation == gen_before, "拒绝后 generation 不得推进"


def test_start_main_rolls_back_handles_when_startup_raises_after_assignment(
        webui_server_shared, monkeypatch):
    """新 svc 已赋值后 set_phase("starting") 抛错 → 外层 except 必须回滚句柄再落 fail_safe。

    现状仅落 fail_safe 不回滚：新实例 starting 相位（get_main_status 主路径读
    summary.phase=="starting"）盖掉故障可见性、_app_service 未清导致下次命中
    「已在运行」、_admin_client 被换成未认证 client。回滚后这些句柄必须清空、
    相位可读为 fail_safe。异常落点后置于句柄赋值之后方触发回滚分支。
    """
    import importlib
    import webdav_client as _wc_mod
    import app_service as _as_mod
    importlib.reload(_as_mod)
    importlib.reload(_wc_mod)
    monkeypatch.setitem(sys.modules, "app_service", _as_mod)
    monkeypatch.setitem(sys.modules, "webdav_client", _wc_mod)

    class _Cfg:
        a_b_mappings = [{"mapping_id": "m1"}]
        webdav = type("W", (), {
            "host": "h", "user": "u", "password": "p", "totp_secret": None})()
        log = type("L", (), {
            "level": "INFO", "file": None, "max_size_mb": 1, "backup_count": 1})()

        def load_strm_storage_from_api(self, admin_client=None):
            return None

    def _fake_client(*a, **k):
        return object()

    def _fake_service(*a, **k):
        # get_config_status() 返回 ready 令流程推进过就绪闸，句柄得以赋值；
        # raise_on_start_phase 令 set_phase("starting") 抛错——此时 self._app_service
        # 已赋值、self._app_running 已置 True，异常现场为「赋值后」。
        return _MockStaleSvc(raise_on_start_phase=True)

    monkeypatch.setattr(_wc_mod, "OpenListAdminClient", _fake_client)
    monkeypatch.setattr(_as_mod, "AppService", _fake_service)

    server, _base, _token = webui_server_shared
    server._config = _Cfg()
    server._app_service = None
    server._app_worker_thread = None
    server._app_running = False
    server._app_start_time = None
    server._admin_client = None
    result = server.start_main()
    assert result["success"] is False, f"启动抛错必须失败，实际: {result!r}"
    assert server._app_service is None, (
        "启动异常后新 svc 句柄必须回滚清空，不得残留 starting 实例")
    assert server._app_running is False, "启动异常后 _app_running 必须回滚为 False"
    assert server._admin_client is None, "启动异常后 _admin_client 必须回滚清空"
    status = server.get_main_status()
    assert status["phase"] == "fail_safe", (
        f"句柄回滚后 fallback 必须可见 fail_safe，实际: {status['phase']!r}")


def test_stop_main_rejects_when_worker_survives_join(webui_server_shared):
    """worker join(5.0) 超时仍存活 → stop_main 拒绝落成功、保留句柄供重试。"""
    server, _base, _token = webui_server_shared
    svc = _MockStaleSvc(stop_raises=False)
    worker = _BlockingWorkerStub()  # start_blocked=True，join 期间不退出
    server._app_service = svc
    server._app_worker_thread = worker
    try:
        result = server.stop_main()
        assert result["success"] is False, (
            f"join 超时仍存活的 worker 必须拒绝停止成功，实际: {result!r}")
        assert "未退出" in result["message"], (
            f"拒绝消息须指明 worker 未退出，实际: {result['message']!r}")
        assert server._app_service is svc, "拒绝后必须保留 svc 句柄供重试"
        assert server._app_worker_thread is worker, "拒绝后必须保留 worker 句柄"
        assert server._app_phase == "fail_safe", (
            f"拒绝后相位须落 fail_safe，实际 {server._app_phase!r}")
    finally:
        worker.release()


def test_stop_main_rejects_when_engine_revives_after_stop(webui_server_shared):
    """worker=None、svc.stop() 返回成功但引擎随后 is_running 翻真 →
    stop_main 权威复核拒绝、保留句柄、不落 stopped 成功。"""
    server, _base, _token = webui_server_shared
    svc = _MockStaleSvc(stop_raises=False, is_revive_after_stop=True)
    server._app_service = svc
    server._app_worker_thread = None
    result = server.stop_main()
    assert result["success"] is False, (
        f"stop 后复活的引擎必须拒绝停止成功，实际: {result!r}")
    assert "仍存活" in result["message"], (
        f"拒绝消息须指明引擎仍存活，实际: {result['message']!r}")
    assert server._app_service is svc, "拒绝后必须保留 svc 句柄供重试"
    assert server._app_phase == "fail_safe", (
        f"拒绝后相位须落 fail_safe，实际 {server._app_phase!r}")
