"""secret_manager 单元测试。

覆盖：
- 正常路径：encrypt/decrypt 往返、空串、is_encrypted、非 ENC 前缀原样返回、
  解密失败返回空串、多次加密密文不同（Fernet 非确定性）、主密钥不匹配降级
- 降级路径：mock cryptography 不可用，验证 encrypt 返回明文、
  decrypt 对 ENC: 前缀返回空串
- 主密钥文件：首次生成、复用、权限收紧（Unix only）

关键约束：
- secret_manager 的 _cached_fernet 和 _cryptography_available 是模块级全局，
  每个测试必须通过 isolated_key_file fixture 清理，避免测试间状态污染。
- 降级测试后必须恢复 _cryptography_available = None。
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

# 冗余保护：确保 src/ 在 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import secret_manager  # noqa: E402


# ============================================================
# Fixture：隔离主密钥文件
# ============================================================

@pytest.fixture
def isolated_key_file(tmp_path, monkeypatch):
    """隔离主密钥文件到 tmp_path，每个测试独立。

    yield 后清理 _cached_fernet 和 _cryptography_available，
    避免降级测试污染后续测试。
    """
    key_file = tmp_path / ".secret_key"
    monkeypatch.setattr(secret_manager, "_KEY_FILE", str(key_file))
    secret_manager.reset_master_key_for_testing()
    yield secret_manager, key_file
    # 测试后清理：恢复模块级缓存
    secret_manager.reset_master_key_for_testing()
    secret_manager._cryptography_available = None


# ============================================================
# 测试类 1：正常路径（需 cryptography）
# ============================================================

class TestEncryptDecrypt:
    """encrypt/decrypt 正常路径测试。"""

    def test_encrypt_empty_returns_empty(self, isolated_key_file):
        """encrypt("") 返回 ""，不写 ENC: 前缀。"""
        sm, _ = isolated_key_file
        assert sm.encrypt("") == ""

    def test_encrypt_nonempty_returns_enc_prefix(self, isolated_key_file):
        """encrypt("test") 以 "ENC:" 开头，且 != "test"。"""
        sm, _ = isolated_key_file
        result = sm.encrypt("test")
        assert result.startswith("ENC:")
        assert result != "test"

    def test_decrypt_roundtrip(self, isolated_key_file):
        """decrypt(encrypt("hello")) == "hello"。"""
        sm, _ = isolated_key_file
        ciphertext = sm.encrypt("hello")
        assert sm.decrypt(ciphertext) == "hello"

    def test_encrypt_different_ciphertext_each_call(self, isolated_key_file):
        """两次 encrypt("same") 密文不同（Fernet 非确定性）。"""
        sm, _ = isolated_key_file
        c1 = sm.encrypt("same")
        c2 = sm.encrypt("same")
        assert c1 != c2
        # 但都能解密回原文
        assert sm.decrypt(c1) == "same"
        assert sm.decrypt(c2) == "same"

    def test_is_encrypted_true_for_enc_prefix(self, isolated_key_file):
        """is_encrypted(encrypt("x")) 为 True。"""
        sm, _ = isolated_key_file
        assert sm.is_encrypted(sm.encrypt("x")) is True

    def test_is_encrypted_false_for_plaintext(self, isolated_key_file):
        """is_encrypted("plain") 为 False。"""
        sm, _ = isolated_key_file
        assert sm.is_encrypted("plain") is False

    def test_is_encrypted_false_for_empty(self, isolated_key_file):
        """is_encrypted("") 为 False。"""
        sm, _ = isolated_key_file
        assert sm.is_encrypted("") is False

    def test_is_encrypted_false_for_non_string(self, isolated_key_file):
        """is_encrypted(None) / is_encrypted(123) 为 False。"""
        sm, _ = isolated_key_file
        assert sm.is_encrypted(None) is False
        assert sm.is_encrypted(123) is False

    def test_decrypt_plaintext_returns_as_is(self, isolated_key_file):
        """decrypt("plain") == "plain"（兼容迁移期历史明文）。"""
        sm, _ = isolated_key_file
        assert sm.decrypt("plain") == "plain"

    def test_decrypt_empty_returns_empty(self, isolated_key_file):
        """decrypt("") == ""。"""
        sm, _ = isolated_key_file
        assert sm.decrypt("") == ""

    def test_decrypt_non_string_returns_empty(self, isolated_key_file):
        """decrypt(None) == ""、decrypt(123) == ""。"""
        sm, _ = isolated_key_file
        assert sm.decrypt(None) == ""
        assert sm.decrypt(123) == ""

    def test_decrypt_corrupted_ciphertext_returns_empty(self, isolated_key_file):
        """decrypt("ENC:invalidbase64") 返回 "" 不抛异常。"""
        sm, _ = isolated_key_file
        assert sm.decrypt("ENC:invalidbase64") == ""

    def test_decrypt_wrong_master_key_returns_empty(self, isolated_key_file, tmp_path):
        """用密钥 A 加密，换密钥 B 后 decrypt 返回 ""（主密钥不匹配降级）。"""
        sm, key_file = isolated_key_file
        ciphertext = sm.encrypt("secret")
        assert sm.decrypt(ciphertext) == "secret"

        # 清空缓存 + 删除密钥文件 + 重新生成新密钥
        sm.reset_master_key_for_testing()
        key_file.unlink()
        # 新密钥会生成（因为 _KEY_FILE 已被 monkeypatch 到 tmp_path）
        new_ciphertext = sm.encrypt("other")
        # 用新密钥解密旧密文应失败返回 ""
        assert sm.decrypt(ciphertext) == ""
        # 新密文可正常解密
        assert sm.decrypt(new_ciphertext) == "other"

    def test_encrypt_rejects_non_str(self, isolated_key_file):
        """encrypt(123) raises TypeError。"""
        sm, _ = isolated_key_file
        with pytest.raises(TypeError):
            sm.encrypt(123)


# ============================================================
# 测试类 2：降级路径（monkeypatch 强制 cryptography 不可用）
# ============================================================

class TestDegradedMode:
    """降级路径测试：mock cryptography 不可用。"""

    def test_encrypt_returns_plaintext_when_no_cryptography(
        self, isolated_key_file, monkeypatch
    ):
        """降级下 encrypt("secret") == "secret"（降级明文）。"""
        sm, _ = isolated_key_file
        monkeypatch.setattr(sm, "_cryptography_available", False)
        sm.reset_master_key_for_testing()
        assert sm.encrypt("secret") == "secret"

    def test_encrypt_empty_still_empty_when_degraded(
        self, isolated_key_file, monkeypatch
    ):
        """降级下 encrypt("") == ""。"""
        sm, _ = isolated_key_file
        monkeypatch.setattr(sm, "_cryptography_available", False)
        sm.reset_master_key_for_testing()
        assert sm.encrypt("") == ""

    def test_decrypt_enc_prefix_returns_empty_when_no_cryptography(
        self, isolated_key_file, monkeypatch
    ):
        """降级下 decrypt("ENC:something") == ""（无法解密）。"""
        sm, _ = isolated_key_file
        monkeypatch.setattr(sm, "_cryptography_available", False)
        sm.reset_master_key_for_testing()
        assert sm.decrypt("ENC:something") == ""

    def test_decrypt_plaintext_returns_as_is_when_degraded(
        self, isolated_key_file, monkeypatch
    ):
        """降级下 decrypt("plain") == "plain"（兼容迁移仍有效）。"""
        sm, _ = isolated_key_file
        monkeypatch.setattr(sm, "_cryptography_available", False)
        sm.reset_master_key_for_testing()
        assert sm.decrypt("plain") == "plain"

    def test_is_encrypted_unaffected_by_degradation(
        self, isolated_key_file, monkeypatch
    ):
        """is_encrypted 仅判断前缀，降级下仍正常工作。"""
        sm, _ = isolated_key_file
        monkeypatch.setattr(sm, "_cryptography_available", False)
        sm.reset_master_key_for_testing()
        assert sm.is_encrypted("ENC:xxx") is True
        assert sm.is_encrypted("plain") is False
        assert sm.is_encrypted("") is False


# ============================================================
# 测试类 3：主密钥文件
# ============================================================

class TestMasterKeyFile:
    """主密钥文件行为测试。"""

    def test_master_key_file_created_on_first_use(self, isolated_key_file):
        """首次 encrypt("x") 后 key_file.exists() 为 True。"""
        sm, key_file = isolated_key_file
        assert not key_file.exists()
        sm.encrypt("x")
        assert key_file.exists()

    def test_master_key_file_reused_across_calls(self, isolated_key_file):
        """两次 encrypt 用同一密钥文件（reset 后重读，密文可被对方解密）。"""
        sm, key_file = isolated_key_file
        c1 = sm.encrypt("hello")
        # 重置缓存（模拟重启）
        sm.reset_master_key_for_testing()
        # 重新读取同一密钥文件
        c2 = sm.encrypt("world")
        # 两个密文都能解密（说明用了同一密钥）
        assert sm.decrypt(c1) == "hello"
        assert sm.decrypt(c2) == "world"

    @pytest.mark.skipif(os.name != "posix", reason="Windows 无 POSIX 权限模型")
    def test_master_key_file_permissions(self, isolated_key_file):
        """Unix 下主密钥文件权限应为 0o600。"""
        sm, key_file = isolated_key_file
        sm.encrypt("x")
        mode = stat.S_IMODE(os.stat(key_file).st_mode)
        assert mode == 0o600, f"期望 0o600，实际 {oct(mode)}"
