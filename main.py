from __future__ import annotations
import os
import sys
import logging
from pathlib import Path
import importlib.util
import time
from types import ModuleType

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def ensure_base_dir_first():
    normalized_base_dir = os.path.normcase(os.path.abspath(BASE_DIR))
    sys.path[:] = [p for p in sys.path if os.path.normcase(
        os.path.abspath(p or os.getcwd())) != normalized_base_dir]
    sys.path.insert(0, BASE_DIR)


def load_local_module(module_name: str, filename: str,
                      base_dir: str | None = None):
    if base_dir is None:
        base_dir = BASE_DIR
    module_path = os.path.join(base_dir, filename)
    if not os.path.isfile(module_path):
        raise FileNotFoundError(f"本地模块文件不存在: {module_path}")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法创建模块加载规范: {module_name} ({module_path})")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# ==================== 保护区开始 ====================
# autopep8: off
# isort: off

ensure_base_dir_first()
try:
    import tomllib
except ImportError:
    import tomli as tomllib

# --- 导入项目内部模块 ---
load_local_module("area_watchers", "area_watchers.py")
from app_service import AppService
from config import AppConfig
from database import Database
from logger_setup import setup_logging
from webdav_client import OpenListAdminClient

# autopep8: on
# isort: on
# ==================== 保护区结束 ====================


def main() -> None:
    config = AppConfig.from_file(os.path.join(BASE_DIR, "config.toml"))
    setup_logging(
        level=config.log.level,
        log_file=config.log.file,
        max_size_mb=config.log.max_size_mb,
        backup_count=config.log.backup_count,
    )
    db = Database(config.local.db_file)
    # 创建 OpenListAdminClient 并用 Admin API 验证
    admin_client = OpenListAdminClient(
        config.webdav.host,
        config.webdav.user,
        config.webdav.password,
        totp_secret=config.webdav.totp_secret,
    )
    if not admin_client.login():
        logging.error("[AdminAPI] 登录失败")
        sys.exit(2)
    # 验证是否能列出根目录
    if not admin_client.check_exists("/"):
        logging.error("[AdminAPI] 连接验证失败")
        sys.exit(2)
    logging.info("[AdminAPI] 连接验证成功")
    app = AppService(config, db, admin_client)  # 只传 admin_client
    try:
        app.start()
        # ---------- 启动后验证 STRM 存储 ----------
        try:
            validation = app.validate_strm_storages()
            logging.info("[启动] STRM 存储验证完成")
        except Exception as exc:
            logging.error("[启动] STRM 存储验证失败: %s", exc)
        # ------------------------------------------
        print("\n嗨！按 q 退出\n")
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
