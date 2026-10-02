"""
pytest 共享配置 — 确保 src/ 在 sys.path 中。

所有 src/tests/ 下的测试文件都依赖此 conftest 自动将 src/ 目录
加入 sys.path，无需在每个测试文件中手动 sys.path.insert。

注意：个别测试文件（test_app_service_core.py 等）内部也保留了
sys.path.insert 作为冗余保护，二者不会冲突。

路径策略：使用 sys.path.append 而非 sys.path.insert(0)。
将项目目录追加到 sys.path 末尾，避免覆盖标准库或已安装第三方包的
模块解析顺序，降低同包名遮蔽（shadowing）风险。
"""
import sys
from pathlib import Path

import pytest

# src/tests/ 的父目录是 src/
_SRC_DIR = str(Path(__file__).resolve().parent.parent)
if _SRC_DIR not in sys.path:
    sys.path.append(_SRC_DIR)

# src/tests/ 自身也需要在 sys.path，以便 import _test_helpers
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.append(_TESTS_DIR)

from _test_helpers import build_mock_app  # noqa: E402


@pytest.fixture
def make_app():
    """pytest fixture 包装 build_mock_app，供新测试直接使用。

    用法：def test_xxx(make_app, tmp_path): app = make_app(tmp_path, ...)
    """
    return build_mock_app


# 独立手动脚本（非 pytest 测试，需运行中的外部服务），不纳入 pytest 收集
collect_ignore_glob = [
    "test_openlist_admin_api.py",
    "test_tmdb_api.py",
    "test_real_server.py",
    "test_webui_standalone.py",
]
