# autopep8: off
# isort: off

"""
主程序入口 - 仅启动 AppService（同步引擎）

重要：WebUI 不在此处启动！

启动方式：
  - 仅启动主程序：python src/main.py
  - 启动 WebUI（含交互菜单）：python src/webui/server.py

WebUI 入口文件：src/webui/server.py 的 main() 函数
"""

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

import logging
import time

# autopep8: on
# isort: on


def main() -> None:
    # 检查是否误用 WebUI 参数
    if '--webui-only' in sys.argv or '--webui' in sys.argv:
        print("\n" + "="*60)
        print("错误: main.py 不启动 WebUI")
        print("="*60)
        print("\n启动方式:")
        print("  - 仅启动同步引擎: python src/main.py")
        print("  - 启动 WebUI:     python src/webui/server.py")
        print("\nWebUI 入口文件: src/webui/server.py")
        print("="*60 + "\n")
        sys.exit(1)

    # 配置文件在项目根目录
    config = AppConfig.from_file(os.path.join(PROJECT_ROOT, "config.toml"))
    setup_logging(
        level=config.log.level,
        log_file=config.log.file,
        max_size_mb=config.log.max_size_mb,
        backup_count=config.log.backup_count,
    )
    db = Database(config.local.db_file)

    # --- 检查 simple 分词器状态 ---
    if db._fts_tokenizer == 'unicode61':
        # simple 分词器未加载，中文搜索将失效
        logging.warning(
            "[启动] Simple 分词器未加载（src/tokenizers/simple/simple.dll 不存在或加载失败），"
            "中文搜索功能将降级为 unicode61（对中文名返回空结果）。"
            "请确保 src/tokenizers/simple/simple.dll 存在，否则中文媒体名搜索无法使用。"
        )

    # --- 配置迁移：首次启动时将 config.toml 迁移到 DB ---
    from tmdb_watchlist_db import TmdbWatchlistDb
    from config import migrate_config_to_db
    _migrate_db_path = os.path.join(PROJECT_ROOT, "tmdb_watchlist.db")
    try:
        _migrate_wdb = TmdbWatchlistDb(_migrate_db_path)
        migrate_config_to_db(config, _migrate_wdb)
        # 从 DB 加载配置覆盖（优先级: DB > config.toml）
        # 记录 DB 覆盖前的日志配置，若 DB 覆盖了 log.level/file，
        # 需重新调用 setup_logging 重建日志处理器，否则 DB 覆盖不生效。
        _log_before = (config.log.level, config.log.file,
                       config.log.max_size_mb, config.log.backup_count)
        config.update_from_db(_migrate_wdb)
        _log_after = (config.log.level, config.log.file,
                      config.log.max_size_mb, config.log.backup_count)
        if _log_before != _log_after:
            logging.info("[Migration] DB 覆盖日志配置，重建日志处理器: %s", _log_after)
            setup_logging(
                level=config.log.level,
                log_file=config.log.file,
                max_size_mb=config.log.max_size_mb,
                backup_count=config.log.backup_count,
            )
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
    if not admin_client.login(force=True, source="startup"):
        error_msg = admin_client.last_error_message or "未知错误"
        error_type = admin_client.last_error_type or "unknown"
        logging.error("[AdminAPI] 登录失败: %s (类型: %s)", error_msg, error_type)
        sys.exit(2)
    # 验证是否能列出根目录（三态：仅 True 视为连通）
    if admin_client.check_exists("/") is not True:
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
        if app._running:
            print("\n主程序已启动。按 q 退出\n")
        else:
            print("\n主程序已启动（配置未就绪，引擎未运行）。按 q 退出\n")
        # 这是有意的交互式等待循环（等用户输入 `q`、EOF 或 Ctrl-C 触发 finally: app.stop()）。
        # 即使引擎未启动也保持进程存活，非死循环。BRIDGE_HEADLESS 由 server.py 处理（server.py:1435），
        # main.py 不处理此环境变量。后台模式走 server.py 的 headless 分支跳过交互菜单。
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
