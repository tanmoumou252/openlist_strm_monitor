"""Sync Service - handles A->B synchronization logic."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app_service import AppService
    from database import Database
    from webdav_client import OpenListAdminClient
    from config import AppConfig

from utils import read_strm_webdav_path, safe_remove_file


class SyncService:
    """A->B 同步服务"""

    def __init__(self, app: AppService) -> None:
        self.app = app
        self.config: AppConfig = app.config
        self.db: Database = app.db
        self.admin_api: OpenListAdminClient = app.admin_api

    def initial_scan_a(self) -> None:
        """启动时扫描 A 区现有文件"""
        logging.info("[初始化] 扫描A区")
        total_strm = 0
        for a_root in self.app.a_roots:
            if not a_root.exists():
                continue
            for root, _dirs, files in os.walk(a_root):
                for name in files:
                    if name.lower().endswith(".strm"):
                        file_path = Path(root) / name
                        self.app.handle_a_created_or_modified(str(file_path))
                        total_strm += 1
        logging.info(
            "[初始化] A区扫描完成，共处理 %s 个 STRM 文件",
            total_strm)

    def scan_a_to_b_full_sync(
            self, valid_engine_paths: list[str] | None = None) -> None:
        """A -> B 全量同步，可选只同步指定引擎路径下的文件。"""
        logging.info("[初始化] A -> B 全量同步")
        if valid_engine_paths is not None:
            logging.info("[初始化] 限制同步范围: %s", valid_engine_paths)
        total_records = 0
        success_count = 0
        fail_count = 0
        skip_count = 0
        for record in self.db.get_all_a_records():
            local_path = record.local_path
            webdav_path = record.webdav_path
            parent_webdav_path = record.parent_webdav_path
            if not Path(local_path).exists():
                logging.warning("[A->B跳过] 源文件不存在: %s", local_path)
                continue
            if valid_engine_paths is not None:
                if not any(webdav_path == p or webdav_path.startswith(
                        p + "/") for p in valid_engine_paths):
                    logging.debug(
                        "[A->B跳过] %s 不在有效引擎路径范围内",
                        webdav_path,
                    )
                    continue
            if self.db.is_ghost_protected(webdav_path):
                continue
            result = self.copy_a_record_to_b_if_needed(
                local_path, webdav_path, parent_webdav_path)
            total_records += 1
            if result is True:
                success_count += 1
            elif result is None:
                skip_count += 1
            else:
                fail_count += 1
        logging.info(
            "[初始化] A -> B 全量同步完成，共 %s 条记录，成功 %s，跳过 %s，失败 %s",
            total_records,
            success_count,
            skip_count,
            fail_count,
        )

    def copy_a_record_to_b_if_needed(
            self, a_local_path: str, webdav_path: str, parent_webdav_path: str) -> bool | None:
        """复制 A→B，但会先检查指纹是否已存在。如果存在则跳过。"""
        if self.db.is_ghost_protected(webdav_path):
            return None
        from utils import make_strm_fingerprint
        fingerprint = make_strm_fingerprint(webdav_path)
        if self.db.b_fingerprint_exists(fingerprint):
            return None  # 会被统计为 skip_count
        return self.copy_a_record_to_b(
            a_local_path, webdav_path, parent_webdav_path)

    def copy_a_record_to_b(self, a_local_path: str,
                           webdav_path: str, parent: str) -> bool | None:
        try:
            # 1. 计算物理路径
            b_local = self.app.build_b_path_from_a(a_local_path, webdav_path)

            # 2. 血统校验（同步阶段）
            if not self.app._verify_b_path_lineage(
                    str(b_local), webdav_path, is_sync_phase=True):
                return False

        except ValueError as exc:
            logging.error("[A->B复制失败] %s", exc)
            return False

        # 3. 检查是否存在同名同内容文件
        if b_local.exists():
            existing_webdav_path = read_strm_webdav_path(b_local)
            if existing_webdav_path == webdav_path:
                try:
                    from utils import make_strm_fingerprint
                    fingerprint = make_strm_fingerprint(webdav_path)
                    self.db.upsert_b(
                        str(b_local), webdav_path, parent, a_local_path, fingerprint=fingerprint, status="valid"
                    )
                    self.db.upsert_identity(
                        fingerprint=fingerprint,
                        webdav_path=webdav_path,
                        source_a_path=a_local_path,
                        current_b_path=str(b_local),
                    )
                    self.app.ensure_single_visible_instance(
                        fingerprint, str(b_local))
                    return None
                except Exception as e:
                    logging.error("[A->B跳过失败] %s", e)
                    return False
        # 如果 WebDAV 源文件已不存在，说明 A 区是冗余文件，清理掉
        if not self.admin_api.check_exists(webdav_path):
            logging.warning(
                "[A->B跳过] WebDAV源文件已不存在，跳过复制并清理A区: %s",
                webdav_path,
            )
            # 清理 A 区冗余文件
            if Path(a_local_path).exists():
                safe_remove_file(a_local_path)
                logging.info("[A区清理] 删除冗余STRM: %s", a_local_path)
            self.db.delete_a_by_local(a_local_path)
            # 设置 ghost 保护，防止再次同步
            self.db.set_ghost_protection(
                webdav_path,
                self.config.behavior.ghost_protect_seconds,
                reason="webdav_not_exists",
            )
            return False
        # ====================================
        # 4. 执行物理拷贝
        try:
            b_local.parent.mkdir(parents=True, exist_ok=True)
            # ===== 修复：检查源文件是否存在 =====
            source_path = Path(a_local_path)
            if not source_path.exists():
                logging.error("[A->B复制失败] 源文件不存在: %s", a_local_path)
                return False
            # ====================================
            shutil.copyfile(a_local_path, b_local)
        except Exception as e:
            logging.error("[A->B复制失败] IO错误: %s", e)
            return False

        # 5. 写入数据库
        try:
            from utils import make_strm_fingerprint
            fingerprint = make_strm_fingerprint(webdav_path)
            self.db.upsert_b(
                str(b_local),
                webdav_path,
                parent,
                a_local_path,
                fingerprint=fingerprint,
                status="valid")
            self.db.upsert_identity(
                fingerprint=fingerprint,
                webdav_path=webdav_path,
                source_a_path=a_local_path,
                current_b_path=str(b_local),
            )
            self.app.ensure_single_visible_instance(fingerprint, str(b_local))
            return True
        except Exception as e:
            logging.error("[A->B复制失败] DB错误: %s", e)
            safe_remove_file(b_local)
            return False
