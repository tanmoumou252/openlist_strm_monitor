#!/usr/bin/env python3
"""
WebUI 管理员密码重置脚本。

用法:
  python reset_admin.py              → 生成随机新密码并打印
  python reset_admin.py 我的密码     → 使用指定密码

该脚本会直接操作数据库，对密码加盐 SHA256 哈希后写入 webui_config 表。
登录验证实时读取数据库，无需重启 WebUI 即可使用新密码。
"""

import hashlib
import os
import secrets
import sqlite3
import sys
from pathlib import Path


def find_db_path() -> str:
    """查找 tmdb_watchlist.db 或 webui_config 所在的数据库文件。"""
    # 搜索可能的路径
    candidates = [
        Path.cwd() / "tmdb_watchlist.db",
        Path.cwd() / "data" / "tmdb_watchlist.db",
        Path(__file__).parent / "tmdb_watchlist.db",
        Path(__file__).parent / "data" / "tmdb_watchlist.db",
    ]
    # 尝试从 config.toml 读取
    for conf_path in [Path.cwd() / "config.toml", Path(__file__).parent / "config.toml"]:
        if conf_path.exists():
            try:
                import tomllib
                with open(conf_path, "rb") as f:
                    cfg = tomllib.load(f)
                db_path = cfg.get("tmdb", {}).get("watchlist_db", "")
                if db_path:
                    p = Path(db_path)
                    if not p.is_absolute():
                        p = conf_path.parent / p
                    if p.exists():
                        return str(p.resolve())
            except Exception:
                pass
    
    for p in candidates:
        if p.exists():
            return str(p.resolve())
    
    # 最后尝试：搜索当前目录
    for f in Path.cwd().rglob("tmdb_watchlist.db"):
        return str(f)
    
    print("错误: 找不到数据库文件 (tmdb_watchlist.db)")
    print("请指定数据库路径: python reset_admin.py --db /path/to/tmdb_watchlist.db")
    sys.exit(1)


def hash_password(password: str) -> str:
    """对密码加盐 PBKDF2-HMAC-SHA256 哈希，返回 salt$iterations$hash 格式。"""
    salt = secrets.token_hex(16)
    iterations = 600000
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return f"{salt}${iterations}${h.hex()}"


def main():
    # 检查 --db 参数
    db_path = None
    args = [a for a in sys.argv[1:] if not a.startswith("--db=")]
    for a in sys.argv[1:]:
        if a.startswith("--db="):
            db_path = a[5:]
    
    if not db_path:
        db_path = find_db_path()
    
    if not os.path.isfile(db_path):
        print(f"错误: 数据库文件不存在: {db_path}")
        sys.exit(1)
    
    # 确定密码
    custom_pass = [a for a in args if not a.startswith("-")]
    if custom_pass:
        new_password = " ".join(custom_pass)
        if len(new_password) < 4:
            print("错误: 密码长度至少 4 个字符")
            sys.exit(1)
    else:
        new_password = secrets.token_urlsafe(12)
    
    # 哈希并写入
    hashed = hash_password(new_password)
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS webui_config (
                scope      TEXT NOT NULL,
                key        TEXT NOT NULL,
                value      TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (scope, key)
            )
        """)
        now = __import__("time").time()
        conn.execute(
            "INSERT OR REPLACE INTO webui_config (scope, key, value, updated_at) VALUES (?, ?, ?, ?)",
            ("ui", "admin_password", hashed, now),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"错误: 写入数据库失败: {e}")
        sys.exit(1)
    
    print("=" * 50)
    print("  WebUI 管理员密码已重置")
    print(f"  新密码: {new_password}")
    print("=" * 50)
    print()
    print("新密码已写入数据库，登录验证实时读取，无需重启 WebUI 即可使用。")
    print("如忘记此密码，再次运行本脚本即可重新生成。")


if __name__ == "__main__":
    main()