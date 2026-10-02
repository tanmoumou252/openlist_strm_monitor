"""migrate_plaintext_to_encrypted 单元测试。

覆盖 tmdb_watchlist_db.TmdbWatchlistDb.migrate_plaintext_to_encrypted()：
- 明文凭据被加密
- 已加密值被跳过（幂等）
- 空值被跳过
- 缺失键被跳过
- 二次调用幂等
- 非敏感键不被触碰

关键约束：
- set_config 对敏感键会自动加密（tmdb_watchlist_db.py:845-846），
  测试明文迁移场景必须用 _conn() 直接写原始值，否则写入时已被加密，
  无法验证 migrate 行为。
- secret_manager 的 _cached_fernet 和 _cryptography_available 是模块级全局，
  必须通过 isolated_key_file fixture 隔离，避免污染真实 src/.secret_key。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 冗余保护：确保 src/ 在 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import secret_manager  # noqa: E402
from tmdb_watchlist_db import TmdbWatchlistDb  # noqa: E402


# ============================================================
# Fixture：隔离主密钥文件 + 真实 TmdbWatchlistDb
# ============================================================

@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """隔离主密钥文件 + 创建真实 TmdbWatchlistDb。

    yield (sm, db, key_file) 三元组。
    """
    key_file = tmp_path / ".secret_key"
    monkeypatch.setattr(secret_manager, "_KEY_FILE", str(key_file))
    secret_manager.reset_master_key_for_testing()

    db_path = tmp_path / "tmdb_watchlist.db"
    db = TmdbWatchlistDb(db_path)

    yield secret_manager, db, key_file

    # 测试后清理
    secret_manager.reset_master_key_for_testing()
    secret_manager._cryptography_available = None


def _write_raw(db, scope: str, key: str, value: str) -> None:
    """绕过 set_config 的自动加密，直接写原始值到 webui_config 表。

    用于模拟旧版本 DB 残留明文凭据的场景。
    """
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO webui_config (scope, key, value, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(scope, key) DO UPDATE SET
                   value=excluded.value, updated_at=excluded.updated_at""",
            (scope, key, value, 0),
        )
        conn.commit()


def _read_raw(db, scope: str, key: str) -> str:
    """直接读 DB 原始值（绕过 get_config 的解密层）。"""
    with db._conn() as conn:
        row = conn.execute(
            "SELECT value FROM webui_config WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        return row[0] if row else None


# ============================================================
# 测试类
# ============================================================

class TestMigratePlaintextToEncrypted:
    """migrate_plaintext_to_encrypted 行为测试。"""

    def test_plaintext_gets_encrypted(self, isolated_env):
        """明文敏感键被加密，且 get_config 解密回原文。"""
        sm, db, _ = isolated_env
        # 绕过 set_config 直接写明文
        _write_raw(db, "openlist", "webdav_password", "plainpw")

        db.migrate_plaintext_to_encrypted()

        # DB 原始值应以 ENC: 开头
        raw = _read_raw(db, "openlist", "webdav_password")
        assert raw.startswith("ENC:"), f"期望 ENC: 前缀，实际: {raw}"
        # get_config 应解密回原文
        assert db.get_config("openlist", "webdav_password") == "plainpw"

    def test_already_encrypted_skipped_idempotent(self, isolated_env):
        """已加密值被跳过（幂等，不二次加密）。"""
        sm, db, _ = isolated_env
        # 先加密一次
        encrypted = sm.encrypt("secret")
        _write_raw(db, "openlist", "webdav_password", encrypted)

        db.migrate_plaintext_to_encrypted()

        # 原始值应保持不变（不被二次加密）
        raw_after = _read_raw(db, "openlist", "webdav_password")
        assert raw_after == encrypted, "已加密值不应被二次加密"
        # 解密仍正常
        assert db.get_config("openlist", "webdav_password") == "secret"

    def test_empty_value_skipped(self, isolated_env):
        """空值被跳过（不写 ENC: 空密文）。"""
        sm, db, _ = isolated_env
        _write_raw(db, "openlist", "webdav_password", "")

        db.migrate_plaintext_to_encrypted()

        # 原始值应仍为空
        raw = _read_raw(db, "openlist", "webdav_password")
        assert raw == "", f"空值不应被加密，实际: {raw!r}"

    def test_missing_key_skipped(self, isolated_env):
        """缺失键被跳过（不报错）。"""
        sm, db, _ = isolated_env
        # 不写入任何敏感键
        db.migrate_plaintext_to_encrypted()
        # 验证不报错，且 DB 中无该键
        assert _read_raw(db, "openlist", "webdav_password") is None

    def test_migrate_idempotent_on_second_call(self, isolated_env):
        """连续调用两次 migrate，第二次后原始值与第一次后相同（幂等）。"""
        sm, db, _ = isolated_env
        _write_raw(db, "openlist", "webdav_password", "plainpw")

        db.migrate_plaintext_to_encrypted()
        raw_after_first = _read_raw(db, "openlist", "webdav_password")
        assert raw_after_first.startswith("ENC:")

        db.migrate_plaintext_to_encrypted()
        raw_after_second = _read_raw(db, "openlist", "webdav_password")
        assert raw_after_second == raw_after_first, "二次调用不应改变已加密值"
        # 解密仍正常
        assert db.get_config("openlist", "webdav_password") == "plainpw"

    def test_non_sensitive_key_not_touched(self, isolated_env):
        """非敏感键明文不被 migrate 触碰。"""
        sm, db, _ = isolated_env
        # 写入非敏感键明文
        _write_raw(db, "openlist", "webdav_host", "http://example.com")

        db.migrate_plaintext_to_encrypted()

        # 原始值应仍为明文（migrate 只遍历 _SENSITIVE_KEYS）
        raw = _read_raw(db, "openlist", "webdav_host")
        assert raw == "http://example.com", \
            f"非敏感键不应被加密，实际: {raw!r}"

    def test_migrate_handles_db_error_gracefully(self, isolated_env, monkeypatch):
        """单个键加密异常时，migrate 继续处理其余键，不中断整个迁移。

        覆盖 tmdb_watchlist_db.py:924-927 的 per-key 异常捕获：
        遍历 _SENSITIVE_KEYS 时，若某个键的 encrypt 抛异常，
        应被 except 捕获并记录日志，循环继续处理下一个键。
        """
        sm, db, _ = isolated_env
        # 写入 2 个敏感键明文
        _write_raw(db, "openlist", "webdav_password", "plainpw1")
        _write_raw(db, "openlist", "webdav_totp_secret", "plainsecret2")

        # mock encrypt：第一次调用抛异常，第二次返回正常密文
        call_count = {"n": 0}
        real_encrypt = sm.encrypt

        def flaky_encrypt(plaintext: str) -> str:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("模拟加密失败")
            return real_encrypt(plaintext)

        monkeypatch.setattr(sm, "encrypt", flaky_encrypt)

        # 不应抛出异常
        db.migrate_plaintext_to_encrypted()

        # encrypt 被调用恰好 2 次（两个有明文值的敏感键）
        assert call_count["n"] == 2, f"期望 encrypt 调用 2 次，实际 {call_count['n']}"

        # 两个键中：一个保持明文（加密失败），一个被加密
        raw_pw = _read_raw(db, "openlist", "webdav_password")
        raw_totp = _read_raw(db, "openlist", "webdav_totp_secret")
        encrypted_keys = [r for r in (raw_pw, raw_totp) if r and r.startswith("ENC:")]
        plaintext_keys = [r for r in (raw_pw, raw_totp) if r and not r.startswith("ENC:")]
        assert len(encrypted_keys) == 1, \
            f"期望恰好 1 个键被加密，实际 {encrypted_keys!r}"
        assert len(plaintext_keys) == 1, \
            f"期望恰好 1 个键保持明文，实际 {plaintext_keys!r}"
        # 保持明文的那个键，原始值未被破坏
        assert plaintext_keys[0] in ("plainpw1", "plainsecret2")
