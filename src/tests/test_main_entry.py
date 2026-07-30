"""main.py 入口参数检查单元测试

覆盖 main.py 中的参数验证路径：
- --webui-only / --webui 参数 → sys.exit(1)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestMainEntry:
    """测试 main.py 的入口参数检查。"""

    def test_webui_only_flag_exits(self):
        with patch("sys.argv", ["main.py", "--webui-only"]):
            with pytest.raises(SystemExit) as exc_info:
                # 导入 main 模块并调用 main() 函数
                from main import main
                main()
            assert exc_info.value.code == 1

    def test_webui_flag_exits(self):
        with patch("sys.argv", ["main.py", "--webui"]):
            with pytest.raises(SystemExit) as exc_info:
                from main import main
                main()
            assert exc_info.value.code == 1

    def test_no_flags_enters_startup_flow_without_argument_exit(self):
        """无参数时通过参数守卫，并进入后续启动流程。"""
        import main as main_module
        from unittest.mock import MagicMock

        config = MagicMock()
        config.local.db_file = "bridge.db"
        config.log.level = "INFO"
        config.log.file = "test.log"
        config.log.max_size_mb = 1
        config.log.backup_count = 1
        config.webdav.host = "host"
        config.webdav.user = "user"
        config.webdav.password = "pass"
        config.webdav.totp_secret = ""

        fake_app = MagicMock()
        fake_app.validate_strm_storages.return_value = {}

        with patch.object(main_module.AppConfig, "from_file", return_value=config), \
             patch.object(main_module, "setup_logging"), \
             patch.object(main_module, "Database"), \
             patch.object(main_module, "OpenListAdminClient") as client_cls, \
             patch.object(main_module, "AppService", return_value=fake_app), \
             patch("config.migrate_config_to_db"), \
             patch("sys.argv", ["main.py"]), \
             patch("builtins.input", return_value="q"):
            client_cls.return_value.login.return_value = True
            client_cls.return_value.check_exists.return_value = True
            main_module.main()

        fake_app.start.assert_called_once()
        fake_app.stop.assert_called_once()
