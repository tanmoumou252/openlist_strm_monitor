"""
WebUI 入口行为测试（server.py main() 的普通与无头模式）。

验证：
- BRIDGE_HEADLESS=1 跳过 input()、调用 start_main()、进入静默等待
- 普通模式显示交互菜单，选择 1 启动 Bridge，默认仅 WebUI
- 静默等待和交互循环受 KeyboardInterrupt 可控退出
- 退出时清理子程序和服务器
"""

import os
import time
from unittest.mock import MagicMock, patch

import pytest


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture(autouse=True)
def _clean_env():
    """每个测试前后清理 BRIDGE_HEADLESS 环境变量。"""
    old = os.environ.pop("BRIDGE_HEADLESS", None)
    yield
    if old is not None:
        os.environ["BRIDGE_HEADLESS"] = old
    else:
        os.environ.pop("BRIDGE_HEADLESS", None)


@pytest.fixture
def mock_config_path(tmp_path):
    """创建临时 config.toml 并让 PROJECT_ROOT 指向临时目录。"""
    cfg = tmp_path / "config.toml"
    cfg.write_text("[webui]\nport = 8579\nbind = '0.0.0.0'\n[log]\nlevel = 'INFO'\n")
    return tmp_path


@pytest.fixture
def _patch_main_deps(mock_config_path):
    """Mock 掉 main() 的全部外部依赖，返回 mock_server 实例。"""
    mock_server = MagicMock()
    mock_server._app_running = False
    mock_server.start.return_value = None
    mock_server.stop.return_value = None
    mock_server.start_main.return_value = {"success": True, "message": "ok"}

    # AppConfig 和 setup_logging 在 main() 内部 import，需 patch 原模块
    patches = [
        patch("webui.server.PROJECT_ROOT", mock_config_path),
        patch("config.AppConfig"),
        patch("webui.server.Database"),
        patch("webui.server.TmdbWatchlistDb"),
        patch("logger_setup.setup_logging"),
        patch("webui.server.WebUIServer", return_value=mock_server),
        patch("webui.server.create_tmdb_client"),
    ]
    for p in patches:
        p.start()
    yield mock_server
    for p in patches:
        p.stop()


# ============================================================
# Headless mode tests
# ============================================================

class TestHeadlessMode:
    """BRIDGE_HEADLESS=1 行为验证。"""

    def test_headless_skips_input_and_starts_main(self, _patch_main_deps):
        """无头模式不调用 input()，自动调用一次 start_main()。"""
        mock_server = _patch_main_deps
        os.environ["BRIDGE_HEADLESS"] = "1"

        from webui.server import main

        # 让 time.sleep 立即抛出 KeyboardInterrupt 以退出无限循环
        with patch.object(time, "sleep", side_effect=KeyboardInterrupt):
            with patch("builtins.input", side_effect=RuntimeError("input should not be called")):
                main()

        # start_main 应被调用一次
        assert mock_server.start_main.call_count == 1, "无头模式应调用 start_main()"
        # input 不应被调用
        # （如果 input 被调用，side_effect=RuntimeError 会抛出异常导致测试失败）
        # stop 和 stop_main 应被调用（清理路径）
        mock_server.stop.assert_called_once()
        # 如果 _app_running=False，stop_main 不会被调用
        mock_server.start.assert_called_once()

    def test_headless_auto_start_main_flag(self, _patch_main_deps):
        """无头模式 auto_start_main=True，start_main 被调用。"""
        mock_server = _patch_main_deps
        os.environ["BRIDGE_HEADLESS"] = "1"

        from webui.server import main

        with patch.object(time, "sleep", side_effect=KeyboardInterrupt):
            with patch("builtins.input", side_effect=RuntimeError("input should not be called")):
                main()

        assert mock_server.start_main.called, "无头模式应调用 start_main"

    def test_headless_cleanup_stops_main_if_running(self, _patch_main_deps):
        """无头模式退出时若 _app_running=True，应调用 stop_main()。"""
        mock_server = _patch_main_deps
        mock_server._app_running = True
        os.environ["BRIDGE_HEADLESS"] = "1"

        from webui.server import main

        with patch.object(time, "sleep", side_effect=KeyboardInterrupt):
            with patch("builtins.input", side_effect=RuntimeError("input should not be called")):
                main()

        mock_server.stop_main.assert_called_once()

    def test_headless_start_main_failure_logged(self, _patch_main_deps):
        """无头模式 start_main 失败时不应产生异常。"""
        mock_server = _patch_main_deps
        mock_server.start_main.return_value = {"success": False, "message": "config error"}
        os.environ["BRIDGE_HEADLESS"] = "1"

        from webui.server import main

        with patch.object(time, "sleep", side_effect=KeyboardInterrupt):
            with patch("builtins.input", side_effect=RuntimeError("input should not be called")):
                # 不应抛出异常
                main()

        mock_server.start_main.assert_called_once()
        mock_server.stop.assert_called_once()


# ============================================================
# Normal mode tests
# ============================================================

class TestNormalMode:
    """普通交互模式行为验证。"""

    def test_normal_default_choice_no_bridge(self, _patch_main_deps):
        """普通模式默认选项（空输入）不启动 Bridge。"""
        mock_server = _patch_main_deps

        from webui.server import main

        # 模拟用户输入空（默认仅 WebUI）
        with patch("builtins.input", side_effect=["", KeyboardInterrupt]):
            main()

        assert mock_server.start_main.call_count == 0, "默认不应启动 Bridge"
        mock_server.start.assert_called_once()

    def test_normal_choice_1_starts_bridge(self, _patch_main_deps):
        """普通模式选择 1 启动 Bridge。"""
        mock_server = _patch_main_deps

        from webui.server import main

        with patch("builtins.input", side_effect=["1", KeyboardInterrupt]):
            main()

        assert mock_server.start_main.call_count == 1, "选择 1 应启动 Bridge"
        mock_server.start.assert_called_once()

    def test_normal_choice_2_no_bridge(self, _patch_main_deps):
        """普通模式选择 2 不启动 Bridge。"""
        mock_server = _patch_main_deps

        from webui.server import main

        with patch("builtins.input", side_effect=["2", KeyboardInterrupt]):
            main()

        assert mock_server.start_main.call_count == 0, "选择 2 不应启动 Bridge"
        mock_server.start.assert_called_once()

    def test_normal_choice_1_cleanup_stops_main(self, _patch_main_deps):
        """普通模式选择 1 退出时若 _app_running=True，应调用 stop_main()。"""
        mock_server = _patch_main_deps
        mock_server._app_running = True

        from webui.server import main

        with patch("builtins.input", side_effect=["1", KeyboardInterrupt]):
            main()

        mock_server.stop_main.assert_called_once()

    def test_normal_interactive_loop_q_exits(self, _patch_main_deps):
        """普通模式输入 q 退出交互循环。"""
        mock_server = _patch_main_deps

        from webui.server import main

        # 交互循环：输入 q 退出
        with patch("builtins.input", side_effect=["", "q"]):
            main()

        assert mock_server.start_main.call_count == 0
        mock_server.stop.assert_called_once()

    def test_normal_interactive_loop_quit_exits(self, _patch_main_deps):
        """普通模式输入 quit 退出交互循环。"""
        mock_server = _patch_main_deps

        from webui.server import main

        with patch("builtins.input", side_effect=["", "quit"]):
            main()

        mock_server.stop.assert_called_once()

    def test_normal_eof_error_does_not_crash(self, _patch_main_deps):
        """普通模式 EOFError 不应导致崩溃。"""
        mock_server = _patch_main_deps

        from webui.server import main

        with patch("builtins.input", side_effect=EOFError):
            main()

        mock_server.stop.assert_called_once()


# ============================================================
# Config missing test
# ============================================================

class TestConfigMissing:
    """config.toml 缺失时的行为。"""

    def test_missing_config_exits(self, mock_config_path):
        """config.toml 不存在时 main() 应 sys.exit(1)。"""
        # 使用空目录（无 config.toml）
        empty_dir = mock_config_path / "sub"
        empty_dir.mkdir()

        patches = [
            patch("webui.server.PROJECT_ROOT", empty_dir),
            patch("logger_setup.setup_logging"),
        ]
        for p in patches:
            p.start()

        from webui.server import main

        with pytest.raises(SystemExit) as exc:
            main()

        for p in patches:
            p.stop()

        assert exc.value.code == 1, "config.toml 缺失应 exit(1)"


# ============================================================
# Server start failure test
# ============================================================

class TestServerStartFailure:
    """WebUIServer.start() 失败时的行为。"""

    def test_start_runtime_error_exits(self, mock_config_path):
        """WebUIServer.start() 抛出 RuntimeError 应 sys.exit(1)。"""
        mock_server = MagicMock()
        mock_server.start.side_effect = RuntimeError("Port in use")

        patches = [
            patch("webui.server.PROJECT_ROOT", mock_config_path),
            patch("config.AppConfig"),
            patch("webui.server.Database"),
            patch("webui.server.TmdbWatchlistDb"),
            patch("logger_setup.setup_logging"),
            patch("webui.server.WebUIServer", return_value=mock_server),
            patch("webui.server.create_tmdb_client"),
        ]
        for p in patches:
            p.start()

        from webui.server import main

        with pytest.raises(SystemExit) as exc:
            main()

        for p in patches:
            p.stop()

        assert exc.value.code == 1, "WebUI 启动失败应 exit(1)"