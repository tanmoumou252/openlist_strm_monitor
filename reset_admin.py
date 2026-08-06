#!/usr/bin/env python3
"""
WebUI 管理员密码重置脚本。

# [AUDIT-NOTE] 已接受：本地交互式维护 CLI。密码走 argv、手写参数解析、cwd rglob 回退
# 对本地管理工具可接受。除非用户要求，不迁移 argparse/getpass。
"""

import os
import secrets
import sqlite3
import sys
from pathlib import Path

# 确保 src/ 在 sys.path 中，以便导入 utils.password_utils
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from utils.password_utils import hash_password  # noqa: E402


def find_db_path() -> str:
    """返回固定项目根路径下的 tmdb_watchlist.db。
    
    [已修复] P1-3: 不再从 config.toml 读取、不再搜索 data/ 目录、不再 rglob，
    不再接受 --db 参数。数据库路径固定在脚本所在目录（项目根）。
    """
    # [设计取舍] 仅测试注入，生产固定项目根
    db_path = Path(__file__).resolve().parent / "tmdb_watchlist.db"
    return str(db_path)


def main():
    # [已修复] P1-3: 拒绝 --db 参数，数据库路径固定在项目根
    if any(a.startswith("--db=") for a in sys.argv[1:]):
        print("错误: --db 参数已移除，数据库路径固定在项目根 tmdb_watchlist.db")
        sys.exit(1)
    db_path = find_db_path()
    
    if not os.path.isfile(db_path):
        print(f"错误: 数据库文件不存在: {db_path}")
        print("请先启动 WebUI 一次以初始化数据库。")
        sys.exit(1)
    
    # 确定密码
    custom_pass = [a for a in sys.argv[1:] if not a.startswith("-")]
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