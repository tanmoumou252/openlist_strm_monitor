"""测试 bootstrap 工具模块"""
import os
import sys
import pytest
from utils.bootstrap import ensure_base_dir_first, load_local_module, BASE_DIR


class TestEnsureBaseDirFirst:
    """测试 ensure_base_dir_first 函数"""

    def test_base_dir_is_first_in_sys_path(self):
        """调用后 BASE_DIR 应在 sys.path 最前面"""
        ensure_base_dir_first()
        assert sys.path[0] == BASE_DIR

    def test_no_duplicates(self):
        """调用后 sys.path 中不应有 BASE_DIR 的重复项"""
        ensure_base_dir_first()
        normalized = os.path.normcase(os.path.abspath(BASE_DIR))
        count = sum(1 for p in sys.path
                    if os.path.normcase(os.path.abspath(p or os.getcwd())) == normalized)
        assert count == 1


class TestLoadLocalModule:
    """测试 load_local_module 函数"""

    def test_load_existing_module(self, tmp_path):
        """加载一个存在的临时模块"""
        mod_file = tmp_path / "test_mod.py"
        mod_file.write_text("VALUE = 42\n", encoding="utf-8")
        mod = load_local_module("test_mod_tmp", "test_mod.py", base_dir=str(tmp_path))
        assert mod.VALUE == 42
        del sys.modules["test_mod_tmp"]

    def test_load_nonexistent_module_raises(self, tmp_path):
        """加载不存在的模块应抛出 FileNotFoundError"""
        with pytest.raises(FileNotFoundError, match="Local module file not found"):
            load_local_module("nonexistent", "no_such_file.py", base_dir=str(tmp_path))

    def test_module_registered_in_sys_modules(self, tmp_path):
        """加载的模块应注册到 sys.modules"""
        mod_file = tmp_path / "registered_mod.py"
        mod_file.write_text("X = 1\n", encoding="utf-8")
        mod = load_local_module("registered_mod_test", "registered_mod.py",
                                base_dir=str(tmp_path))
        assert sys.modules["registered_mod_test"] is mod
        del sys.modules["registered_mod_test"]
