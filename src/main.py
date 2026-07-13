# autopep8: off
# isort: off

from __future__ import annotations

import os
import sys

# BASE_DIR = src/ 目录（代码目录）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# PROJECT_ROOT = 项目根目录（配置文件目录）
PROJECT_ROOT = os.path.dirname(BASE_DIR)

# 确保 src 目录在 sys.path 最前面
sys.path.insert(0, BASE_DIR)

from app_service import AppService
from webdav_client import OpenListAdminClient
from logger_setup import setup_logging
from database import Database
from config import AppConfig
from webui.server import WebUIServer

import logging
import time

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

# autopep8: on
# isort: on


def main() -> None:
    # 配置文件在项目根目录
    config = AppConfig.from_file(os.path.join(PROJECT_ROOT, "config.toml"))
    setup_logging(
        level=config.log.level,
        log_file=config.log.file,
        max_size_mb=config.log.max_size_mb,
        backup_count=config.log.backup_count,
    )
    db = Database(config.local.db_file)
    
    # --- 配置迁移：首次启动时将 config.toml 迁移到 DB ---
    from tmdb_watchlist_db import TmdbWatchlistDb
    from config import migrate_config_to_db
    _migrate_db_path = os.path.join(PROJECT_ROOT, "tmdb_watchlist.db")
    try:
        _migrate_wdb = TmdbWatchlistDb(_migrate_db_path)
        migrate_config_to_db(config, _migrate_wdb)
        # 从 DB 加载配置覆盖（优先级: DB > config.toml）
        config.update_from_db(_migrate_wdb)
    except Exception as exc:
        logging.warning("[Migration] 迁移过程异常: %s", exc)
    # ---------------------------------------------------
    
    # 从 OpenList API 加载 STRM 存储映射（需要网络）
    try:
        config.load_strm_storage_from_api()
    except Exception as exc:
        logging.warning("[STRM存储] 加载失败: %s", exc)
    
    # 创建 OpenListAdminClient 并用 Admin API 验证
    admin_client = OpenListAdminClient(
        config.webdav.host,
        config.webdav.user,
        config.webdav.password,
        totp_secret=config.webdav.totp_secret,
    )
    # 强制重新登录，不使用缓存 token，确保真实验证连接
    if not admin_client.login(force=True):
        error_msg = admin_client.last_error_message or "未知错误"
        error_type = admin_client.last_error_type or "unknown"
        logging.error("[AdminAPI] 登录失败: %s (类型: %s)", error_msg, error_type)
        sys.exit(2)
    # 验证是否能列出根目录
    if not admin_client.check_exists("/"):
        logging.error("[AdminAPI] 连接验证失败")
        sys.exit(2)
    logging.info("[AdminAPI] 连接验证成功")
    app = AppService(config, db, admin_client)  # 只传 admin_client
    # WebUI 不再由 main.py 启动，需要单独运行 webui/server.py
    try:
        app.start()
        # ---------- 启动后验证 STRM 存储 ----------
        try:
            validation = app.validate_strm_storages()
            logging.info("[启动] STRM 存储验证完成")
        except Exception as exc:
            logging.error("[启动] STRM 存储验证失败: %s", exc)
        # ------------------------------------------
        print("\n主程序已启动。按 q 退出\n")
        while True:
            try:
                user_input = input().strip().lower()
                if user_input == "q":
                    break
            except EOFError:
                time.sleep(1)
            except KeyboardInterrupt:
                break
    finally:
        app.stop()
        logging.info("[停止] 程序已退出")


if __name__ == "__main__":
    main()
