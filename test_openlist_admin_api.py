""" [测试] OpenList Admin API 测试脚本
功能：
1. 从 config.toml 读取配置
2. 测试登录
3. 测试获取存储列表
4. 筛选 Strm 类型的存储，并提取 id、mount_path、driver、status、paths、SaveLocalMode
5. 使用 Strm 存储的 id 调用 get_storage_info
6. 测试列出目录内容
"""

import json
import logging
import sys
from pathlib import Path
from typing import Optional, Dict, Any, List

# ---------- 路径配置 ----------
_current_dir = Path(__file__).parent.resolve()
if str(_current_dir) not in sys.path:
    sys.path.insert(0, str(_current_dir))

from config import AppConfig
from openlist_admin_api import OpenListAdminClient

# ---------- 日志配置 ----------
LOG_FILE = Path(_current_dir) / "openlist_api_test.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),  # 写入文件
        logging.StreamHandler(sys.stdout),  # 打印到控制台
    ],
)
log = logging.getLogger("openlist_api_test")


def extract_paths_from_addition(addition: str) -> Optional[List[str]]:
    """从 addition 字段中提取 paths 列表"""
    if not addition:
        return None
    try:
        addition_dict = json.loads(addition)
        paths = addition_dict.get("paths", "")
        if isinstance(paths, str):
            return [p.strip() for p in paths.split("\n") if p.strip()]
        elif isinstance(paths, list):
            return paths
        else:
            return None
    except json.JSONDecodeError:
        return None


def extract_save_local_mode(addition: str) -> Optional[str]:
    """从 addition 字段中提取 SaveLocalMode"""
    if not addition:
        return None
    try:
        addition_dict = json.loads(addition)
        return addition_dict.get("SaveLocalMode")
    except json.JSONDecodeError:
        return None


def extract_save_strm_local_path(addition: str) -> Optional[str]:
    """从 addition 字段中提取 SaveStrmLocalPath"""
    if not addition:
        return None
    try:
        addition_dict = json.loads(addition)
        return addition_dict.get("SaveStrmLocalPath")
    except json.JSONDecodeError:
        return None


def main():
    log.info("=" * 70)
    log.info("OpenList API 测试脚本")
    log.info("=" * 70)

    # 1. 加载配置
    config_path = Path(_current_dir) / "config.toml"
    if not config_path.exists():
        log.error(f"❌ 配置文件不存在：{config_path}")
        return
    config = AppConfig.from_file(str(config_path))
    log.info(f"✅ 配置加载成功，Host: {config.webdav.host}")

    # 2. 初始化客户端
    client = OpenListAdminClient(
        host=config.webdav.host,
        user=config.webdav.user,
        password=config.webdav.password,
        totp_secret=config.webdav.totp_secret,
    )

    # 3. 登录
    log.info("=" * 70)
    log.info("【测试】登录 (POST /api/auth/login)")
    log.info("=" * 70)
    if not client.login():
        log.error("❌ 登录失败，跳过后续测试")
        return
    # 通过 client.token 获取 JWT token 并输出到日志
    log.info(f"✅ JWT Token: {client.token}")
    # 4. 获取存储列表
    log.info("=" * 70)
    log.info("【测试】获取存储列表 (GET /api/admin/storage/list)")
    log.info("=" * 70)
    storages = client.list_storages()
    if not storages:
        log.error("❌ 获取存储列表失败")
        return

    # 5. 筛选 Strm 类型的存储，仅提取 ID
    log.info("=" * 70)
    log.info("【处理】筛选 Strm 类型的存储 ID")
    log.info("=" * 70)
    strm_storage_ids = [
        storage["id"]
        for storage in storages.get("data", {}).get("content", [])
        if storage.get("driver", "").lower() == "strm"
    ]
    if not strm_storage_ids:
        log.warning("⚠️ 未找到任何 Strm 类型的存储")
    else:
        log.info(f"✅ 共找到 {len(strm_storage_ids)} 个 Strm 存储，ID: {strm_storage_ids}")

    # 6. 使用 Strm 存储的 ID 调用 get_storage_info
    for storage_id in strm_storage_ids:
        log.info("=" * 70)
        log.info(f"【测试】获取 Strm 存储详情 (GET /api/admin/storage/get?id={storage_id})")
        log.info("=" * 70)
        storage_info = client.get_storage_info(storage_id)
        if storage_info:
            data = storage_info.get("data", {})
            addition = data.get("addition", "")
            paths = extract_paths_from_addition(addition)
            save_local_mode = extract_save_local_mode(addition)
            save_strm_local_path = extract_save_strm_local_path(addition)
            status = data.get("status", "unknown")
            # 简化日志输出
            log.info(
                f"Strm 存储详情: "
                f"id={data.get('id')}, "
                f"mount_path={data.get('mount_path')}, "
                f"driver={data.get('driver')}, "
                f"status={status}, "
                f"SaveLocalMode={save_local_mode}, "
                f"SaveStrmLocalPath={save_strm_local_path}, "
                f"paths={paths}"
            )
            # 保留返回的完整 addition 内容
            log.info(f"完整 addition: {addition}")
        else:
            log.error(f"❌ 获取存储 {storage_id} 详情失败")
    # 7. 列出根目录内容
    log.info("=" * 70)
    log.info("【测试】列出目录内容 (POST /api/fs/list?path=/)")
    log.info("=" * 70)
    directory = client.list_directory(path="/")
    if directory:
        log.info(f"目录内容：{json.dumps(directory, indent=2, ensure_ascii=False)}")
    log.info("=" * 70)
    log.info(f"✅ 测试完成，日志已保存到：{LOG_FILE}")
    log.info("=" * 70)


if __name__ == "__main__":
    main()
