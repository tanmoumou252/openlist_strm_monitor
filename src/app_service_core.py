# autopep8: off
# isort: off

"""App service core implementation."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import re
import shutil
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
try:
    import tomllib
except ImportError:
    import tomli as tomllib

from config import AppConfig
from database import Database
from domain.media.subtitle_handler import SubtitleHandler
from domain.sync.sync_service import SyncService
from domain.events.handlers import AAreaEventHandler, BAreaEventHandler, CAreaEventHandler
from refresh_service import RefreshService
from utils import (
    make_strm_fingerprint,
    read_strm_webdav_path,
    webdav_parent,
    build_webdav_trash_path,
    quarantine_file,
    safe_remove_file,
    remove_empty_dirs,
    move_file,
    _canonicalize_webdav_path_for_cloud,
)
from webdav_client import OpenListAdminClient
from media_renamer import (
    suggest_rename,
    build_season_path,
    _extract_season_episode,
    _build_standard_name,
    detect_media_type_from_path,
    is_subtitle_file,
    detect_subtitle_language,
    SUBTITLE_EXTS,
    extract_season_from_path,
)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def ensure_base_dir_first():
    normalized_base_dir = os.path.normcase(os.path.abspath(BASE_DIR))
    sys.path[:] = [p for p in sys.path if os.path.normcase(
        os.path.abspath(p or os.getcwd())) != normalized_base_dir]
    sys.path.insert(0, BASE_DIR)


ensure_base_dir_first()

# autopep8: on
# isort: on


@dataclass(slots=True, frozen=True)
class StrmStorageInfo:
    """STRM 存储信息"""

    id: int
    mount_path: str
    status: str
    paths: list[str]
    save_local_mode: str

    @property
    def is_working(self) -> bool:
        return self.status == "work"

    @property
    def is_sync_mode(self) -> bool:
        return self.save_local_mode.lower() == "update"


class StrmStorageManager:
    """STRM 存储管理器"""

    def __init__(self, client: OpenListAdminClient) -> None:
        self.client = client

    @staticmethod
    def _extract_paths_from_addition(addition: str) -> list[str]:
        if not addition:
            return []
        try:
            addition_dict = json.loads(addition)
            paths = addition_dict.get("paths", "")
            if isinstance(paths, str):
                return [p.strip() for p in paths.split("\n") if p.strip()]
            elif isinstance(paths, list):
                return [str(p).strip() for p in paths if str(p).strip()]
            return []
        except json.JSONDecodeError:
            logging.warning("解析 addition 失败: %s", addition[:200])
            return []

    @staticmethod
    def _extract_save_local_mode(addition: str) -> str:
        if not addition:
            return ""
        try:
            addition_dict = json.loads(addition)
            return addition_dict.get("SaveLocalMode", "")
        except json.JSONDecodeError:
            return ""

    def get_strm_storages(self) -> list[StrmStorageInfo]:
        storages = self.client.list_storages()
        if not storages:
            return []
        data = storages.get("data", {})
        content = data.get("content", []) if isinstance(data, dict) else []
        result: list[StrmStorageInfo] = []
        for storage in content:
            if storage.get("driver", "").lower() != "strm":
                continue
            addition = storage.get("addition", "")
            result.append(
                StrmStorageInfo(
                    id=storage.get("id", 0),
                    mount_path=storage.get("mount_path", ""),
                    status=storage.get("status", "unknown"),
                    paths=self._extract_paths_from_addition(addition),
                    save_local_mode=self._extract_save_local_mode(addition),
                )
            )
        return result

    def get_working_sync_storages(self) -> list[StrmStorageInfo]:
        return [s for s in self.get_strm_storages(
        ) if s.is_working and s.is_sync_mode]

    def validate_against_local_paths(
            self, local_strm_engine_paths: list[str]) -> dict:
        api_storages = self.get_strm_storages()
        api_mount_paths = {s.mount_path for s in api_storages}
        local_strm_set = set(p.rstrip("/")
                             for p in local_strm_engine_paths if p.strip())
        result: dict = {
            "api_storages": api_storages,
            "missing_in_api": [],
            "extra_in_api": [],
            "non_working": [],
            "non_sync_mode": [],
            "valid": [],
        }
        for local_path in local_strm_set:
            if local_path not in api_mount_paths:
                result["missing_in_api"].append(local_path)
        for storage in api_storages:
            mount = storage.mount_path.rstrip("/")
            if mount not in local_strm_set:
                result["extra_in_api"].append(storage)
                continue
            if not storage.is_working:
                result["non_working"].append(storage)
                continue
            if not storage.is_sync_mode:
                result["non_sync_mode"].append(storage)
                continue
            result["valid"].append(storage)
        return result


class AppService:
    def __init__(self, config: AppConfig, db: Database,
                 admin_api: OpenListAdminClient) -> None:
        self.config = config
        self.db = db
        self.admin_api = admin_api
        self._observers: list[object] = []
        self.observer = None
        self._running = False
        self.refresh_service = RefreshService(self)
        self._dav_write_lock = threading.Lock()
        self._b_move_lock = threading.Lock()
        self._b_repair_lock = threading.Lock()
        self._path_locks_lock = threading.Lock()
        self._path_locks: dict[str, threading.Lock] = {}
        self.cleanup_lock = threading.Lock()
        self.pending_cleanups: dict[str, threading.Timer] = {}
        self.a_roots = [Path(p).resolve() for p in config.a_folders]
        self.b_root = Path(config.paths.b_root).resolve()
        self.c_root = Path(config.paths.c_root).resolve()
        self.engine_configs: list[dict] = []
        self._restoring_markers: set[str] = set()
        self._restoring_lock = threading.Lock()
        self._lineage_log_lock = threading.Lock()
        self._lineage_log_keys: set[str] = set()
        self.db.init_subtitle_table()
        self.sync_service = SyncService(self)
        self.subtitle_handler = SubtitleHandler(self)

    def get_path_lock(self, path: str | Path) -> threading.Lock:
        key = str(Path(path).resolve())
        with self._path_locks_lock:
            lock = self._path_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._path_locks[key] = lock
        return lock

    def is_path_under_any_root(self, path: str, roots: list[str]) -> bool:
        if not path or path == "/":
            return False
        normalized_path = path.rstrip("/") or "/"
        for root in roots:
            if not root or root == "/":
                continue
            normalized_root = root.rstrip("/") or "/"
            if normalized_path == normalized_root or normalized_path.startswith(
                    normalized_root + "/"):
                return True
        return False

    def is_valid_refresh_root(self, root_path: str) -> bool:
        if not self.config.strm_engine_paths:
            return True
        return self.is_path_under_any_root(
            root_path, self.config.strm_engine_paths)

    def is_strm_engine_monitored(self, root_path: str) -> bool:
        return self.is_valid_refresh_root(root_path)

    def _find_matching_engine_path(self, webdav_path: str) -> str | None:
        if not self.config.strm_engine_paths:
            return None
        candidates = []
        for engine_path in self.config.strm_engine_paths:
            if webdav_path == engine_path or webdav_path.startswith(
                    engine_path + "/"):
                candidates.append(engine_path)
        if not candidates:
            return None
        return max(candidates, key=len)

    def _log_lineage_pass_once(self, reason: str, b_local_path: str) -> None:
        """把同类通过日志按目录前缀压缩成一行。"""
        summary_path = str(Path(b_local_path).parent)
        if not summary_path.endswith(os.sep):
            summary_path += os.sep
        log_key = f"{reason}|{summary_path}"
        with self._lineage_log_lock:
            if log_key in self._lineage_log_keys:
                return
            self._lineage_log_keys.add(log_key)
        logging.debug("[血统校验通过] %s: %s", reason, summary_path)

    def sync_protected_roots_from_config(self) -> None:
        roots: list[tuple[str, str]] = []
        for root_path in self.config.strm_engine_paths:
            try:
                trash_path = build_webdav_trash_path(
                    root_path.rstrip("/") + "/.__root_placeholder__",
                    self.config.behavior.trash_dir_name,
                )
                trash_root = webdav_parent(trash_path)
            except ValueError:
                logging.warning("[保护根目录] 跳过非法路径: %s", root_path)
                continue
            roots.append((root_path.rstrip("/") or "/", trash_root))
        self.db.replace_protected_roots(roots)
        logging.debug("[保护根目录] 已同步 %s 个根目录", len(roots))

    def scan_removed_protected_roots(self) -> None:
        current_roots = set(self.db.get_protected_root_paths())
        snapshot_roots = set(self.db.get_protected_roots_snapshot_paths())
        removed_roots = sorted(snapshot_roots - current_roots)
        for root_path in removed_roots:
            logging.warning("[保护根目录] 检测到已移除路径: %s", root_path)
            self.migrate_b_under_root_to_c(root_path)
            self.db.remove_known_folder_prefix(root_path)

    def persist_current_roots_snapshot(
            self, valid_engine_paths: list[str] | None = None) -> None:
        roots = [
            (root_path, trash_path)
            for root_path, trash_path, active, _updated_at in self.db.get_protected_roots()
            if active and (valid_engine_paths is None or root_path in valid_engine_paths)
        ]
        self.db.save_protected_roots_snapshot(roots)

    def refresh_webdav_root(self, root_path: str, depth: int) -> None:
        root_path = root_path.rstrip("/") or "/"
        cleanup_allowed = self.is_valid_refresh_root(root_path)
        if not cleanup_allowed:
            logging.info("[WebDAV刷新] %s 不在 STRM 引擎监控范围内，仅刷新不清理 B 区", root_path)
        exists = self._refresh_webdav_recursive(
            root_path, depth, current_depth=0)
        if not exists:
            logging.warning("[WebDAV刷新] 根路径不存在或不可访问: %s", root_path)
            if cleanup_allowed:
                self.migrate_b_under_root_to_c(root_path)
                self.db.remove_known_folder_prefix(root_path)
            return
        if cleanup_allowed:
            self.cleanup_b_zombies_under_folder(root_path)

    def refresh_webdav_root_readonly(self, root_path: str, depth: int) -> None:
        root_path = root_path.rstrip("/") or "/"
        logging.info("[WebDAV只读刷新] %s (不清理B区)", root_path)
        exists = self._refresh_webdav_recursive(
            root_path, depth, current_depth=0)
        if not exists:
            logging.warning("[WebDAV只读刷新] 根路径不存在或不可访问: %s", root_path)
        self.db.save_known_folder(root_path, source="webdav_refresh_readonly")

    def _refresh_webdav_recursive(
            self, path: str, max_depth: int, current_depth: int) -> bool:
        if current_depth > max_depth:
            return True
        normalized_path = path.rstrip("/") or "/"
        # 按路径去重，同一路径的扫描日志只输出一次
        if not hasattr(self, "_webdav_scan_logged"):
            self._webdav_scan_logged: set[str] = set()
        if normalized_path not in self._webdav_scan_logged:
            self._webdav_scan_logged.add(normalized_path)
            logging.debug(
                "[WebDAV刷新] 扫描 %s (深度 %s/%s)",
                normalized_path,
                current_depth,
                max_depth)
        result = self.admin_api.list_contents(normalized_path)
        if isinstance(result, str):
            if result == "404_NOT_FOUND":
                logging.warning("[WebDAV刷新] 路径不存在: %s", normalized_path)
                return False
            logging.error("[WebDAV刷新] 无法列出目录 %s: %s", normalized_path, result)
            return False
        self.db.save_known_folder(normalized_path, source="webdav_refresh")
        for folder in result.get("folders", []):
            if isinstance(folder, dict):
                folder_name = folder.get("name", "")
            else:
                folder_name = str(folder)
            if folder_name:
                sub_path = f"{normalized_path}/{folder_name}"
                self._refresh_webdav_recursive(
                    sub_path, max_depth, current_depth + 1)
        return True

    def start(self) -> None:
        logging.info("[启动] 准备环境并初始化数据库...")
        self.prepare_environment()
        self.db.init_db()
        logging.info("[启动] 数据库初始化完成")
        self.update_engine_configs()
        logging.info("[启动] 引擎配置加载完成")
        self.initial_scan_b()
        self.sync_protected_roots_from_config()
        self.scan_removed_protected_roots()
        self.persist_current_roots_snapshot()
        self.initial_scan_a()
        self.scan_a_to_b_full_sync()
        self.cleanup_b_redundant()
        self.start_watchers()
        self.refresh_service.start()
        logging.info("嗨嗨，应用启动成功咯！")

    def stop(self) -> None:
        for timer in list(self.pending_cleanups.values()):
            timer.cancel()
        self.pending_cleanups.clear()
        self.refresh_service.stop()
        if self.observer is not None and self.observer.is_alive():
            self.observer.stop()
            self.observer.join()

    def prepare_environment(self) -> None:
        for a_root in self.a_roots:
            if not a_root.exists():
                logging.warning("[A区路径不存在] %s", a_root)
        self.b_root.mkdir(parents=True, exist_ok=True)
        self.c_root.mkdir(parents=True, exist_ok=True)

    def start_watchers(self) -> None:
        from watchdog.observers import Observer
        self.observer = Observer()
        active_a = 0
        for a_root in self.a_roots:
            if a_root.exists():
                self.observer.schedule(
                    AAreaEventHandler(self), str(a_root), recursive=True)
                active_a += 1
                logging.info("[监控启动] A区: %s", a_root)
            else:
                logging.warning("[监控跳过] A区不存在: %s", a_root)
        self.observer.schedule(
            BAreaEventHandler(self), str(
                self.b_root), recursive=True)
        self.observer.schedule(
            CAreaEventHandler(self), str(
                self.c_root), recursive=True)
        self.observer.start()
        logging.info("[监控启动] B区: %s", self.b_root)
        logging.info("[监控启动] C区: %s", self.c_root)
        if active_a == 0:
            logging.warning("[提示] 没有可用的 A 区监控目录，程序将依赖后续目录出现或主动刷新")

    def get_a_root_for_path(self, local_path: str | Path) -> Path | None:
        target = Path(local_path).resolve()
        for a_root in self.a_roots:
            try:
                target.relative_to(a_root)
                return a_root
            except ValueError:
                continue
        return None

    def build_b_path_from_a(self, a_local_path: str | Path,
                            webdav_path: str | None = None) -> Path:
        a_local = Path(a_local_path).resolve()
        a_root = self.get_a_root_for_path(a_local)
        if a_root is None:
            raise ValueError(f"文件不属于任何A根目录: {a_local}")
        rel = a_local.relative_to(a_root)
        is_movie = self._should_treat_as_movie(a_local, webdav_path)
        if is_movie:
            return self.b_root / rel
        suggested_name = suggest_rename(a_local)
        if suggested_name and webdav_path:
            season = extract_season_from_path(webdav_path)
            if season is None:
                season = extract_season_from_path(a_local)
            if season is None:
                season, _ = _extract_season_episode(a_local.name)
            _, episode = _extract_season_episode(a_local.name)
            if season is not None and episode is not None:
                if suggested_name:
                    standard_name = suggested_name
                else:
                    standard_name = f"S{
                        season:02d}E{
                        episode:02d}{
                        Path(a_local).suffix}"
                rel_parts = list(rel.parts)
                has_season_dir = False
                season_dir_index = -1
                cn_season_dir_index = -1
                for i, part in enumerate(rel_parts[:-1]):
                    if re.match(r"(?i)^season\s*\d+$", part):
                        has_season_dir = True
                        season_dir_index = i
                        break
                    if re.match(r"^第[一二三四五六七八九十\d]+季$", part):
                        cn_season_dir_index = i
                if has_season_dir:
                    new_rel = Path(
                        *rel_parts[:season_dir_index]) / f"Season {season:02d}" / standard_name
                elif cn_season_dir_index >= 0:
                    new_rel = Path(
                        *rel_parts[:cn_season_dir_index]) / f"Season {season:02d}" / standard_name
                else:
                    new_rel = Path(*rel_parts[:-1]) / \
                        f"Season {season:02d}" / standard_name
                return self.b_root / new_rel
        return self.b_root / rel

    def _reverse_map_b_to_a(self, b_local_path: str | Path) -> str | None:
        b_local = Path(b_local_path).resolve()
        try:
            rel = b_local.relative_to(self.b_root)
        except ValueError:
            return None
        if not rel.parts:
            return None
        sub_path = Path(rel)
        for a_root in self.a_roots:
            candidate = a_root / sub_path
            if candidate.exists():
                return str(candidate)
        return None

    def update_engine_configs(self):
        logging.info("[引擎配置] 正在向服务器请求 STRM 存储配置...")
        storages = self.admin_api.list_storages()
        self.engine_configs = []
        if not storages:
            logging.warning("[引擎配置] 无法获取存储列表，API 返回为空！")
            return
        content = storages.get("data", {}).get("content", [])
        logging.info("[引擎配置] 服务器共返回 %d 个存储设备", len(content))
        for s in content:
            driver = s.get("driver", "")
            mount_path = s.get("mount_path", "unknown")
            if driver.lower() != "strm":
                logging.debug(
                    "[引擎配置] 忽略非 STRM 驱动存储: %s (driver=%s)",
                    mount_path,
                    driver)
                continue
            addition_str = s.get("addition", "{}")
            logging.debug(
                "[引擎配置] 发现 STRM 存储 [%s], addition 内容: %s",
                mount_path,
                addition_str)
            try:
                addition = json.loads(addition_str)
                save_path = addition.get("SaveStrmLocalPath")
                paths_val = addition.get("paths", "")
                if isinstance(paths_val, list):
                    source_paths = [str(p).strip()
                                    for p in paths_val if str(p).strip()]
                else:
                    source_paths = [p.strip()
                                    for p in paths_val.split("\n") if p.strip()]
                if not save_path:
                    logging.warning(
                        "[引擎配置] 存储 [%s] 缺少 'SaveStrmLocalPath' 配置或为空，已跳过此引擎映射！", mount_path)
                    continue
                resolved_save_path = str(Path(save_path).resolve())
                self.engine_configs.append(
                    {"a_root_norm": resolved_save_path, "mount_path": mount_path, "source_paths": source_paths})
                logging.info(
                    "[引擎配置] 成功加载引擎映射: 挂载点 [%s] -> 本地 A区 [%s] (包含 %d 个云端监控源)",
                    mount_path,
                    resolved_save_path,
                    len(source_paths))
            except Exception as e:
                logging.error("[引擎配置] 解析存储 [%s] 配置失败: %s", mount_path, e)

    def _verify_b_path_lineage(
            self, b_local_path: str, webdav_path: str, is_sync_phase: bool = False) -> bool:
        """验证 B 区文件路径的血统关系，确保其合法存在于 B 区。"""
        fingerprint = make_strm_fingerprint(webdav_path)
        b_local = Path(b_local_path).resolve()

        # 1. 解析 A 区源文件
        a_source = self._resolve_a_source(b_local_path, webdav_path, fingerprint)
        if not a_source:
            return False

        a_local_path, a_root, a_rel_dir, b_rel_dir = a_source
        a_parts = list(a_rel_dir.parts)
        b_parts = list(b_rel_dir.parts)

        # 2. 基础层级检查：目录完全一致
        if self._check_basic_lineage(a_rel_dir, b_rel_dir, b_local_path):
            return True

        # 3. B 区自动添加 Season 层级
        if self._check_season_layer_addition(a_parts, b_parts, b_local_path):
            return True

        # 4. 媒体名称匹配检查
        a_media_name, b_media_name = self._extract_media_names_from_path_parts(a_parts, b_parts)
        if a_media_name and b_media_name:
            if self._check_media_name_match(a_media_name, b_media_name, b_local_path):
                return True

        # 5. 引擎配置与云端/物理名称解析
        config, source_path, cloud_show_name, physical_media_folder_name, rel_parts = (
            self._resolve_cloud_and_physical_names(
                webdav_path, a_root, b_parts, fingerprint))
        if config is None:
            return True  # 无引擎配置，默认放行

        # 6. 越界文件检查
        if not self._check_boundary_files(b_parts, rel_parts, b_local_path):
            return False

        # 7. 边界映射匹配检查
        if self._check_boundary_mappings(
                fingerprint, cloud_show_name, physical_media_folder_name, b_local_path):
            return True

        # 8. 同步阶段边界记录
        if is_sync_phase and cloud_show_name and physical_media_folder_name != cloud_show_name:
            self._handle_sync_phase_boundary(
                fingerprint, cloud_show_name, physical_media_folder_name)
            return True

        # 9. 单集/批量检测
        if cloud_show_name and physical_media_folder_name != cloud_show_name:
            return self._check_solo_episode(
                fingerprint, cloud_show_name, physical_media_folder_name,
                source_path, b_parts, b_local_path)

        return True

    def _resolve_a_source(
            self, b_local_path: str, webdav_path: str, fingerprint: str) -> tuple | None:
        """解析 A 区源文件路径，返回 (a_local_path, a_root, a_rel_dir, b_rel_dir) 或 None。"""
        b_local = Path(b_local_path).resolve()
        a_record = self.db.get_a_by_webdav(webdav_path)
        if not a_record:
            identity = self.db.get_identity_by_fingerprint(fingerprint)
            if identity and identity[2]:
                a_local_path = Path(identity[2])
                if a_local_path.exists():
                    a_record = (str(a_local_path), webdav_path, "", 0)
        if not a_record:
            logging.debug("[血统校验失败] 无A区源记录: %s", b_local_path)
            return None
        a_local_path = Path(a_record[0]).resolve()
        if not a_local_path.exists():
            logging.debug("[血统校验失败] A区源文件不存在: %s", a_local_path)
            return None
        a_root = self.get_a_root_for_path(a_local_path)
        if not a_root:
            logging.debug("[血统校验失败] A区源不在任何根目录下: %s", a_local_path)
            return None
        try:
            a_rel = a_local_path.relative_to(a_root)
            b_rel = b_local.relative_to(self.b_root)
        except ValueError:
            logging.debug("[血统校验失败] 路径超出根目录")
            return None
        return (a_local_path, a_root, a_rel.parent, b_rel.parent)

    def _check_basic_lineage(self, a_rel_dir: Path, b_rel_dir: Path, b_local_path: str) -> bool:
        """检查基础血统：A/B 目录完全一致则放行。"""
        if a_rel_dir == b_rel_dir:
            self._log_lineage_pass_once("默认放行", b_local_path)
            return True
        return False

    def _check_season_layer_addition(
            self, a_parts: list[str], b_parts: list[str], b_local_path: str) -> bool:
        """检查 B 区是否自动添加了 Season 层级。"""
        if len(b_parts) == len(a_parts) + 1:
            if b_parts[:len(a_parts)] == a_parts:
                last_part = b_parts[-1]
                if re.match(r"(?i)^season\s*\d+$", last_part):
                    self._log_lineage_pass_once("B区自动添加Season层级", b_local_path)
                    return True
        return False

    @staticmethod
    def _extract_media_name_from_parts(rel_parts: list[str]) -> str | None:
        """从路径部件中提取媒体名称（Season 前一级的目录名）。"""
        for i, part in enumerate(rel_parts):
            if re.match(r"(?i)^season\s*\d+$", part):
                if i > 0:
                    return rel_parts[i - 1]
                break
        return None

    def _extract_media_names_from_path_parts(
            self, a_parts: list[str], b_parts: list[str]) -> tuple[str | None, str | None]:
        """提取 A/B 路径的媒体名称。"""
        a_media_name = self._extract_media_name_from_parts(a_parts)
        b_media_name = self._extract_media_name_from_parts(b_parts)
        return a_media_name, b_media_name

    def _check_media_name_match(
            self, a_media_name: str, b_media_name: str, b_local_path: str) -> bool:
        """检查媒体名称匹配关系。"""
        if a_media_name == b_media_name:
            self._log_lineage_pass_once("同一媒体不同Season", b_local_path)
            return True
        boundary = self.db.get_media_boundary_by_source_name_only(a_media_name)
        if boundary:
            _, mapped_source, mapped_current, _, _ = boundary
            if b_media_name in (mapped_source, mapped_current):
                self._log_lineage_pass_once("边界映射Season变化", b_local_path)
                return True
        return False

    def _resolve_cloud_and_physical_names(
            self, webdav_path: str, a_root: Path, b_parts: list[str], fingerprint: str) -> tuple:
        """解析引擎配置、云端显示名称和物理媒体文件夹名称。"""
        if not hasattr(self, "engine_configs") or not self.engine_configs:
            return (None, None, None, None, None)

        a_root_norm = str(a_root.resolve())
        config = next(
            (c for c in self.engine_configs if c["a_root_norm"] == a_root_norm),
            None)
        if not config:
            return (None, None, None, None, None)

        source_path = next(
            (sp for sp in config["source_paths"] if webdav_path.startswith(
                sp.rstrip("/") + "/")), None)
        if not source_path:
            return (None, None, None, None, None)

        rel_cloud_str = webdav_path[len(source_path.rstrip("/")):].lstrip("/")
        rel_parts = rel_cloud_str.split("/")
        cloud_show_name = rel_parts[0] if len(rel_parts) >= 2 else None

        physical_media_folder_name = None
        for i, part in enumerate(b_parts):
            if re.match(r"(?i)^season\s*\d+$", part):
                if i > 0:
                    physical_media_folder_name = b_parts[i - 1]
                break
        if physical_media_folder_name is None and b_parts:
            physical_media_folder_name = b_parts[-1]

        return (config, source_path, cloud_show_name, physical_media_folder_name, rel_parts)

    @staticmethod
    def _check_boundary_files(
            b_parts: list[str], rel_parts: list[str], b_local_path: str) -> bool:
        """检查越界文件，返回 True 表示检查完成（无论放行还是拒绝）。"""
        if len(b_parts) < 2:
            if len(rel_parts) < 2:
                return True
            logging.warning("[血统校验失败] 越界文件: %s", b_local_path)
            return False
        return True

    def _check_boundary_mappings(
            self, fingerprint: str, cloud_show_name: str | None,
            physical_media_folder_name: str | None, b_local_path: str) -> bool:
        """检查边界映射匹配关系。"""
        if fingerprint:
            boundary = self.db.get_media_boundary_by_fingerprint(fingerprint)
            if boundary:
                _, source_media_name, current_media_name, _, _ = boundary
                if physical_media_folder_name == current_media_name:
                    self._log_lineage_pass_once("边界映射匹配", b_local_path)
                    return True
                if physical_media_folder_name == source_media_name:
                    self._log_lineage_pass_once("回到源边界", b_local_path)
                    return True

        if cloud_show_name and physical_media_folder_name:
            boundary_by_source = self.db.get_media_boundary_by_source_name_only(
                physical_media_folder_name)
            if boundary_by_source:
                _, mapped_source, mapped_current, _, _ = boundary_by_source
                if cloud_show_name == mapped_source or cloud_show_name == mapped_current:
                    self._log_lineage_pass_once("交叉边界映射匹配(源->当前)", b_local_path)
                    return True

            boundary_by_current = self.db.get_media_boundary_by_current_name(
                physical_media_folder_name, str(self.b_root))
            if boundary_by_current:
                _, mapped_source, mapped_current, _, _ = boundary_by_current
                if cloud_show_name == mapped_source or cloud_show_name == mapped_current:
                    self._log_lineage_pass_once("交叉边界映射匹配(当前->源)", b_local_path)
                    return True

            boundary_by_cloud = self.db.get_media_boundary_by_source_name_only(
                cloud_show_name)
            if boundary_by_cloud:
                _, mapped_source, mapped_current, _, _ = boundary_by_cloud
                if physical_media_folder_name in (mapped_source, mapped_current):
                    self._log_lineage_pass_once("交叉边界映射匹配(云端)", b_local_path)
                    return True

        return False

    def _handle_sync_phase_boundary(
            self, fingerprint: str, cloud_show_name: str,
            physical_media_folder_name: str) -> None:
        """处理同步阶段的边界映射记录。"""
        existing = self.db.get_media_boundary_by_fingerprint(fingerprint)
        if not existing:
            self.db.upsert_media_boundary(
                fingerprint=fingerprint,
                source_media_name=cloud_show_name,
                current_media_name=physical_media_folder_name,
                engine_entry_path=str(self.b_root))
            logging.info(
                "[边界映射] 记录新映射: %s -> %s",
                cloud_show_name,
                physical_media_folder_name)
        elif existing[2] != physical_media_folder_name:
            self.db.upsert_media_boundary(
                fingerprint=fingerprint,
                source_media_name=existing[1],
                current_media_name=physical_media_folder_name,
                engine_entry_path=str(self.b_root))
            logging.info(
                "[边界映射] 更新映射: %s -> %s",
                existing[1],
                physical_media_folder_name)

    def _check_solo_episode(
            self, fingerprint: str, cloud_show_name: str | None,
            physical_media_folder_name: str | None, source_path: str,
            b_parts: list[str], b_local_path: str) -> bool:
        """检查是否为单集脱离集体的情况。"""
        if cloud_show_name and physical_media_folder_name != cloud_show_name:
            cloud_media_root = f"{source_path.rstrip('/')}/{cloud_show_name}"
            total_a_episodes = self.db.get_a_count_under_root(cloud_media_root)
            if total_a_episodes <= 1:
                return True

            physical_media_root_dir = self.b_root
            for i, part in enumerate(b_parts):
                if part == physical_media_folder_name:
                    physical_media_root_dir = self.b_root / Path(*b_parts[:i + 1])
                    break

            local_matches = 0
            if physical_media_root_dir.exists():
                for p in physical_media_root_dir.rglob("*.strm"):
                    s_webdav = read_strm_webdav_path(p)
                    if s_webdav and s_webdav.startswith(cloud_media_root + "/"):
                        local_matches += 1

            if local_matches <= 1:
                self.trigger_delayed_solo_check(
                    str(physical_media_root_dir), cloud_media_root)
                return True
        return True

    def trigger_delayed_solo_check(
            self, physical_dir: str, cloud_media_root: str):
        with self.cleanup_lock:
            old_timer = self.pending_cleanups.pop(physical_dir, None)
            if old_timer:
                old_timer.cancel()
            timer = threading.Timer(
                30, self.execute_solo_judgment, args=(
                    physical_dir, cloud_media_root))
            timer.daemon = True
            self.pending_cleanups[physical_dir] = timer
            timer.start()

    def execute_solo_judgment(self, physical_dir: str, cloud_media_root: str):
        logging.info("[单兵审判] 观察期结束，开始判定: %s", physical_dir)
        p_dir = Path(physical_dir)
        if not p_dir.exists():
            return
        matches = []
        for p in p_dir.rglob("*.strm"):
            s_webdav = read_strm_webdav_path(p)
            if s_webdav and s_webdav.startswith(cloud_media_root + "/"):
                matches.append(p)
        total_a = self.db.get_a_count_under_root(cloud_media_root)
        if len(matches) == 1 and total_a > 1:
            bad_file = matches[0]
            logging.warning("[B区清理] 审判结果：确认单兵脱离集体，执行物理删除: %s", bad_file)
            safe_remove_file(bad_file)
            self.db.delete_b_by_local(str(bad_file))
            self.cleanup_local_empty_dirs()
        else:
            logging.info("[单兵审判] 审判结果：判定为合法的批量操作或单集作品，予以保留。")

    def initial_scan_b(self) -> None:
        logging.info("[初始化] 开始扫描 B 区现有文件...")
        b_root = Path(self.config.paths.b_root)
        if not b_root.exists():
            logging.info("[初始化] B 区根目录不存在，跳过扫描")
            return
        disk_fingerprint_to_paths = {}
        disk_path_to_data = {}
        scanned_count = 0
        for strm_file in b_root.rglob("*.strm"):
            try:
                scanned_count += 1
                webdav_path = read_strm_webdav_path(strm_file)
                if webdav_path:
                    fingerprint = make_strm_fingerprint(webdav_path)
                    path_str = str(strm_file)
                    if fingerprint not in disk_fingerprint_to_paths:
                        disk_fingerprint_to_paths[fingerprint] = set()
                    disk_fingerprint_to_paths[fingerprint].add(path_str)
                    disk_path_to_data[path_str] = {
                        "webdav": webdav_path, "fp": fingerprint}
            except Exception as e:
                logging.warning("[初始化] 读取 B 区文件失败: %s (%s)", strm_file, e)
        logging.info("[初始化] B 区磁盘扫描完毕，共发现 %d 个 STRM 文件", scanned_count)
        try:
            all_b_records = self.db.get_all_b_records()
            logging.info("[初始化] 成功读取 B 区历史数据库记录: %d 条", len(all_b_records))
        except Exception as e:
            logging.error("[初始化] 查询历史记录失败 (通常是因为表不存在): %s", e)
            return
        processed_disk_paths = set()
        for row in all_b_records:
            db_local_path = row[0]
            db_fingerprint = row[4]
            if not db_fingerprint:
                self.db.delete_b_by_local(db_local_path)
                continue
            if db_local_path in disk_path_to_data and disk_path_to_data[
                    db_local_path]["fp"] == db_fingerprint:
                webdav_path = disk_path_to_data[db_local_path]["webdav"]
                if not self._verify_b_path_lineage(db_local_path, webdav_path):
                    logging.warning(
                        "[B区历史越界清理] 物理删除历史遗留越界文件: %s", db_local_path)
                    safe_remove_file(db_local_path)
                    self.db.delete_b_by_local(db_local_path)
                    self.refresh_identity_current_b_path(db_fingerprint)
                    processed_disk_paths.add(db_local_path)
                    continue
                processed_disk_paths.add(db_local_path)
                continue
            disk_paths_for_fp = disk_fingerprint_to_paths.get(
                db_fingerprint, set())
            available_paths = [
                p for p in disk_paths_for_fp if p not in processed_disk_paths]
            valid_new_path = None
            for candidate_path in available_paths:
                candidate_webdav = disk_path_to_data[candidate_path]["webdav"]
                if self._verify_b_path_lineage(
                        candidate_path, candidate_webdav):
                    valid_new_path = candidate_path
                    break
                else:
                    logging.warning(
                        "[B区越界清理] 发现非法跨目录移动，物理删除: %s", candidate_path)
                    safe_remove_file(candidate_path)
                    processed_disk_paths.add(candidate_path)
            if valid_new_path:
                self.db.move_b_record(db_local_path, valid_new_path)
                identity = self.db.get_identity_by_fingerprint(db_fingerprint)
                if identity and identity[3] == db_local_path:
                    self.db.update_identity_b_path(
                        db_fingerprint, valid_new_path)
                try:
                    old_path_obj = Path(db_local_path)
                    if old_path_obj.exists() and str(old_path_obj.resolve()) != str(
                            Path(valid_new_path).resolve()):
                        safe_remove_file(old_path_obj)
                        logging.debug("[B区自同步] 删除旧路径物理文件: %s", db_local_path)
                except Exception as e:
                    logging.warning(
                        "[B区自同步] 删除旧路径物理文件失败: %s (%s)", db_local_path, e)
                if db_fingerprint:
                    self.ensure_single_visible_instance(
                        db_fingerprint, valid_new_path)
                processed_disk_paths.add(valid_new_path)
                logging.info(
                    "[B区自同步] 更新路径(合法重命名): %s -> %s",
                    db_local_path,
                    valid_new_path)
            else:
                self.db.delete_b_by_local(db_local_path)
                self.refresh_identity_current_b_path(db_fingerprint)
                logging.debug("[B区自同步] 删除失效数据库记录: %s", db_local_path)
        new_insert_count = 0
        for disk_path, data in disk_path_to_data.items():
            if disk_path not in processed_disk_paths:
                webdav_path = data["webdav"]
                fingerprint = data["fp"]
                if not self._verify_b_path_lineage(disk_path, webdav_path):
                    logging.warning(
                        "[B区越界清理] 发现非法新增跨区复制文件，物理删除: %s", disk_path)
                    safe_remove_file(disk_path)
                    continue
                parent_webdav_path = webdav_parent(
                    webdav_path) if webdav_path else ""
                identity = self.db.get_identity_by_fingerprint(fingerprint)
                source_a_path = identity[2] if identity else self.find_a_source_by_webdav(
                    webdav_path)
                self.db.upsert_b(
                    local_path=disk_path,
                    webdav_path=webdav_path,
                    parent_webdav_path=parent_webdav_path,
                    source_a_path=source_a_path,
                    fingerprint=fingerprint,
                    status="valid")
                self.db.upsert_identity(
                    fingerprint=fingerprint,
                    webdav_path=webdav_path,
                    source_a_path=source_a_path,
                    current_b_path=disk_path)
                self.ensure_single_visible_instance(fingerprint, disk_path)
                new_insert_count += 1
                logging.debug("[B区自同步] 新增本地文件入库: %s", disk_path)
        logging.info("[初始化] B 区扫描自同步完成，本次共新增入库 %d 个文件记录", new_insert_count)

    def initial_scan_a(self):
        return self.sync_service.initial_scan_a()

    def scan_a_to_b_full_sync(
            self, valid_engine_paths: list[str] | None = None) -> None:
        return self.sync_service.scan_a_to_b_full_sync(valid_engine_paths)

    def cleanup_b_redundant(self) -> None:
        b_root = Path(self.config.paths.b_root)
        if not b_root.exists():
            return
        redundant_keywords = ["duplicate", "quarantined", "invalid"]
        for keyword in redundant_keywords:
            for file_path in b_root.rglob(f"*.{keyword}"):
                try:
                    safe_remove_file(file_path)
                    self.db.delete_b_by_local(str(file_path))
                    logging.info("[冗余清理] 已删除冗余文件: %s", file_path)
                except OSError as e:
                    logging.warning("[冗余清理] 删除冗余文件失败: %s (%s)", file_path, e)
            for file_path in b_root.rglob(f"*.{keyword}.*"):
                try:
                    safe_remove_file(file_path)
                    self.db.delete_b_by_local(str(file_path))
                    logging.info("[冗余清理] 已删除带时间戳的冗余文件: %s", file_path)
                except OSError as e:
                    logging.warning(
                        "[冗余清理] 删除带时间戳冗余文件失败: %s (%s)", file_path, e)
        try:
            all_b_records = self.db.get_all_b_records()
        except Exception as e:
            logging.error("[冗余清理] 查询 B 区记录失败: %s", e)
            return
        if not all_b_records:
            logging.info("[冗余清理] B 区冗余清理完成")
            return
        removed_count = 0
        migrated_count = 0
        for row in all_b_records:
            local_path = row[0]
            webdav_path = row[1]
            source_a_path = row[3]
            fingerprint = row[4]
            if not webdav_path:
                continue
            if self.db.is_ghost_protected(webdav_path):
                continue
            source_exists = False
            if source_a_path and Path(source_a_path).exists():
                source_exists = True
            else:
                alt_source = self.find_a_source_by_webdav(webdav_path)
                if alt_source:
                    source_exists = True
            if not source_exists:
                if self.admin_api.check_exists(webdav_path):
                    logging.debug(
                        "[冗余清理跳过] A区源文件暂不可用但WebDAV存在，跳过清理: %s", webdav_path)
                    continue
            if not source_exists:
                local = Path(local_path)
                if not local.exists():
                    self.db.delete_b_by_local(local_path)
                    if fingerprint:
                        self.refresh_identity_current_b_path(fingerprint)
                    continue
                try:
                    rel = local.resolve().relative_to(self.b_root)
                except ValueError:
                    rel = Path(local.name)
                target = self.c_root / rel
                if local.exists():
                    try:
                        move_file(local, target)
                    except OSError as exc:
                        logging.warning(
                            "[冗余清理→C区] 迁移失败: %s -> %s (%s)", local, target, exc)
                        safe_remove_file(local_path)
                        self.db.delete_b_by_local(local_path)
                        if fingerprint:
                            self.refresh_identity_current_b_path(fingerprint)
                        continue
                self.db.upsert_c(
                    str(target),
                    webdav_path,
                    local_path,
                    webdav_parent(webdav_path))
                self.db.delete_b_by_local(local_path)
                if fingerprint:
                    self.refresh_identity_current_b_path(fingerprint)
                migrated_count += 1
                logging.info(
                    "[冗余清理→C区] A区源文件已不存在，迁移至C区: %s -> %s",
                    local_path,
                    webdav_path)
                continue
            if self.admin_api.check_exists(webdav_path):
                continue
            safe_remove_file(local_path)
            self.db.delete_b_by_local(local_path)
            if fingerprint:
                self.refresh_identity_current_b_path(fingerprint)
            removed_count += 1
            logging.info(
                "[冗余清理] 已移除失效STRM(WebDAV不存在): %s -> %s",
                local_path,
                webdav_path)
        if migrated_count:
            logging.warning(
                "[冗余清理→C区] 共迁移 %s 个 A 区源已删除的 STRM 到 C 区",
                migrated_count)
        if removed_count:
            logging.warning("[冗余清理] 共清理 %s 个 WebDAV 已不存在的 STRM", removed_count)
        self.cleanup_local_empty_dirs()
        logging.info("[冗余清理] B 区冗余清理完成")

    def cleanup_local_empty_dirs(self) -> None:
        for a_root in self.a_roots:
            if a_root.exists():
                remove_empty_dirs(a_root)
        remove_empty_dirs(self.b_root)
        remove_empty_dirs(self.c_root)

    def cleanup_a_deleted_on_cloud(self, engine_path: str) -> None:
        """在 update 模式下，清理 A 区中云端已删除的文件"""
        if not engine_path:
            return
        # 遍历 A 区，找出指向该引擎路径下但云端已不存在的 STRM 文件
        a_records = self.db.get_all_a_records()
        for record in a_records:
            local_path, webdav_path, parent_path, _updated_at = record
            # 只处理属于当前 engine_path 范围的记录
            if not webdav_path.startswith(engine_path):
                continue
            if not self.admin_api.check_exists(webdav_path):
                logging.info(
                    "[A区清理] 云端已删除，移除本地 STRM: %s (WebDAV: %s)",
                    local_path,
                    webdav_path,
                )
                safe_remove_file(local_path)
                self.db.delete_a_by_local(local_path)
                self.db.set_ghost_protection(
                    webdav_path,
                    self.config.behavior.ghost_protect_seconds,
                    reason="cloud_deleted",
                )

    def validate_strm_storages(self) -> dict:
        """验证 STRM 存储状态，返回验证结果"""
        logging.info("[STRM存储验证] 开始验证...")
        try:
            storages = self.admin_api.list_storages() if hasattr(self.admin_api, 'list_storages') else {}
            data = storages.get("data", {}) if isinstance(storages, dict) else {}
            content = data.get("content", []) if isinstance(data, dict) else []
            total = len(content)
            working = sum(1 for s in content if s.get("status") == "work")
            logging.info("[STRM存储验证] 总计 %d 个存储，其中 %d 个状态正常", total, working)
            return {
                "total": total,
                "working": working,
                "storages": content,
            }
        except Exception as exc:
            logging.warning("[STRM存储验证] 验证过程发生异常: %s", exc)
            return {"total": 0, "working": 0, "storages": [], "error": str(exc)}

    def handle_a_created_or_modified(self, local_path: str) -> None:
        local = Path(local_path).resolve()
        if not local.exists():
            return
        if self.get_a_root_for_path(local) is None:
            logging.debug("[A区跳过] 不属于任何A根目录: %s", local)
            return
        if is_subtitle_file(local):
            self.process_subtitle_file(local)
            return
        webdav_path = read_strm_webdav_path(local)
        if not webdav_path:
            logging.warning("[A区] 无法解析STRM: %s", local)
            return
        parent = webdav_parent(webdav_path)
        self.db.upsert_a(str(local), webdav_path, parent)
        self.db.save_known_folder(parent, source="a")
        fingerprint = make_strm_fingerprint(webdav_path)
        if not self.admin_api.check_exists(webdav_path):
            logging.warning("[A区即时清理] WebDAV 已不存在，删除本地冗余 STRM: %s", local)
            safe_remove_file(str(local))
            self.db.delete_a_by_local(str(local))
            self.db.set_ghost_protection(
                webdav_path,
                self.config.behavior.ghost_protect_seconds,
                reason="webdav_not_exists")
            return
        old_identity = self.db.get_identity_by_fingerprint(fingerprint)
        current_b_path = old_identity[3] if old_identity else None
        self.db.upsert_identity(
            fingerprint=fingerprint,
            webdav_path=webdav_path,
            source_a_path=str(local),
            current_b_path=current_b_path)
        if self.db.is_ghost_protected(webdav_path):
            logging.info("[A->B阻断] ghost保护中，跳过复制: %s", webdav_path)
            return
        try:
            b_local = self.build_b_path_from_a(local, webdav_path)
        except ValueError as exc:
            logging.warning("[A->B跳过] %s", exc)
            return
        valid_b_instance = self.db.get_valid_b_instance_by_fingerprint(
            fingerprint)
        if valid_b_instance:
            existing_main_path = valid_b_instance[0]
            if existing_main_path != str(b_local):
                new_score = self._b_file_score(str(b_local))
                old_score = self._b_file_score(existing_main_path)
                if new_score >= old_score:
                    return
        if b_local.exists():
            existing_webdav_path = read_strm_webdav_path(b_local)
            if existing_webdav_path == webdav_path:
                self.db.upsert_b(
                    str(b_local),
                    webdav_path,
                    parent,
                    str(local),
                    fingerprint=fingerprint,
                    status="valid")
                self.db.upsert_identity(
                    fingerprint=fingerprint,
                    webdav_path=webdav_path,
                    source_a_path=str(local),
                    current_b_path=str(b_local))
                self.ensure_single_visible_instance(fingerprint, str(b_local))
                return
        if old_identity and current_b_path is None:
            if not self.admin_api.check_exists(webdav_path):
                logging.warning(
                    "[A->B跳过] WebDAV源文件已不存在，跳过复制并清理A区: %s",
                    webdav_path)
                a_local_path = str(local)
                if local.exists():
                    safe_remove_file(a_local_path)
                    logging.info("[A区清理] 删除冗余STRM: %s", a_local_path)
                self.db.delete_a_by_local(a_local_path)
                self.db.set_ghost_protection(
                    webdav_path,
                    self.config.behavior.ghost_protect_seconds,
                    reason="webdav_not_exists")
                return
        self.copy_a_record_to_b(str(local), webdav_path, parent)

    def handle_a_deleted(self, local_path: str) -> None:
        if Path(local_path).exists():
            logging.debug(
                "[A区跳过] 文件仍存在，可能是openlist引擎的同步操作:删除strm又新建: %s",
                local_path)
            return
        row = self.db.get_a_by_local(local_path)
        self.db.delete_a_by_local(local_path)
        if row:
            _local_path, webdav_path, parent_webdav_path, _updated_at = row
            self.trigger_delayed_cleanup(parent_webdav_path)
            logging.debug("[A区删除] 已清理A索引并安排延迟清理: %s", webdav_path)
        else:
            logging.debug("[A区删除] 未找到A索引: %s", local_path)

    def copy_a_record_to_b_if_needed(
            self, a_local_path: str, webdav_path: str, parent_webdav_path: str) -> bool | None:
        return self.sync_service.copy_a_record_to_b_if_needed(
            a_local_path, webdav_path, parent_webdav_path)

    def copy_a_record_to_b(self, a_local_path: str,
                           webdav_path: str, parent: str) -> bool | None:
        return self.sync_service.copy_a_record_to_b(
            a_local_path, webdav_path, parent)

    def _find_related_subtitles(self, strm_path: str | Path) -> list[Path]:
        strm = Path(strm_path)
        if not strm.parent.exists():
            return []
        subtitles = []
        for ext in SUBTITLE_EXTS:
            subtitles.extend(strm.parent.glob(f"*{ext}"))
            subtitles.extend(strm.parent.glob(f"*{ext.upper()}"))
        seen = set()
        result = []
        for sub in subtitles:
            key = sub.resolve()
            if key not in seen:
                seen.add(key)
                result.append(sub)
        return result

    def _should_treat_as_movie(
            self, a_local_path: str | Path, webdav_path: str | None = None) -> bool:
        media_type = detect_media_type_from_path(a_local_path)
        if media_type == "movie":
            return True
        if media_type == "anime":
            return False
        if webdav_path:
            media_type = detect_media_type_from_path(webdav_path)
            if media_type == "movie":
                return True
            if media_type == "anime":
                return False
        season, episode = _extract_season_episode(Path(a_local_path).name)
        if season is None or episode is None:
            parent = Path(a_local_path).parent
            strm_count = len(list(parent.glob("*.strm")))
            if strm_count <= 1:
                return True
        return False

    def process_subtitle_file(self, a_subtitle_path: str | Path) -> None:
        return self.subtitle_handler.process_subtitle_file(a_subtitle_path)

    def _process_movie_subtitle(
            self, sub_file: Path, a_root: Path, fingerprint: str) -> None:
        return self.subtitle_handler._process_movie_subtitle(
            sub_file, a_root, fingerprint)

    def _process_anime_subtitle(
            self, sub_file: Path, a_root: Path, fingerprint: str) -> None:
        return self.subtitle_handler._process_anime_subtitle(
            sub_file, a_root, fingerprint)

    def _is_standard_media_name(self, name: str) -> bool:
        name = name.lower()
        if re.search(r"s\d{1,2}e\d{1,2}", name):
            return True
        if re.search(r"\d{1,2}x\d{1,2}", name):
            return True
        if re.search(r".*- s\d{1,2}e\d{1,2} -", name):
            return True
        if re.search(r"season \d{1,2}/episode \d{1,2}", name):
            return True
        return False

    def _b_file_score(self, path: str) -> tuple:
        p = Path(path)
        name = p.name.lower()
        is_standard = self._is_standard_media_name(name)
        try:
            b_rel_parts = p.relative_to(self.b_root).parts
        except ValueError:
            b_rel_parts = p.parts
        webdav_parts = []
        try:
            row = self.db.get_b_by_local_full(path)
            if row:
                webdav_path = row[1]
                if webdav_path:
                    canonical_webdav = _canonicalize_webdav_path_for_cloud(
                        webdav_path)
                    webdav_parts = [
                        part for part in canonical_webdav.strip("/").split("/") if part]
        except Exception:
            pass
        if not webdav_parts:
            match_count = len(b_rel_parts)
        else:
            match_count = 0
            for b_part, w_part in zip(
                    reversed(b_rel_parts), reversed(webdav_parts)):
                if b_part.lower() == w_part.lower():
                    match_count += 1
                else:
                    break
        path_len = len(str(p))
        return (0 if is_standard else 1, match_count, path_len, name)

    def ensure_single_visible_instance(
            self, fingerprint: str, trigger_path: str) -> None:
        all_instances = self.db.get_all_b_by_fingerprint(fingerprint)
        if not all_instances:
            return
        valid_files = [row[0] for row in all_instances if row[5]
                       == "valid" and Path(row[0]).exists()]
        if not valid_files:
            return
        valid_files.sort(key=self._b_file_score)
        keep = valid_files[0]
        duplicate_paths = self.db.mark_other_b_instances_duplicate(
            fingerprint, keep)
        for dup_path in duplicate_paths:
            dup = Path(dup_path)
            if not dup.exists():
                continue
            quarantined = quarantine_file(dup, suffix=".duplicate")
            if quarantined:
                moved = self.db.move_b_record(str(dup), str(quarantined))
                if moved:
                    self.db.mark_b_instance_status(
                        str(quarantined), "duplicate")
                logging.warning(
                    "[B区重复] 已隔离重复实例: %s -> %s (保留=%s)",
                    dup,
                    quarantined,
                    keep)
            else:
                logging.warning("[B区重复] 重复实例隔离失败: %s", dup)

    def find_a_source_by_webdav(self, webdav_path: str) -> str | None:
        local_path = self.db.get_a_local_path_by_webdav(webdav_path)
        if local_path and Path(local_path).exists():
            return local_path
        return None

    def restore_b_file_from_a(self, b_local_path: str, webdav_path: str,
                              parent_webdav_path: str, source_a_path: str | None) -> bool:
        source = source_a_path
        if not source or not Path(source).exists():
            source = self.find_a_source_by_webdav(webdav_path)
        if not source:
            logging.warning("[B区修复失败] A区不存在对应源文件: %s", webdav_path)
            return False
        target = Path(b_local_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(source, target)
        except FileNotFoundError as exc:
            logging.error("[B区修复失败] 源文件不存在: %s", exc)
            return False
        except PermissionError as exc:
            logging.error("[B区修复失败] 权限不足: %s", exc)
            return False
        except OSError as exc:
            logging.error("[B区修复失败] 文件系统错误: %s", exc)
            return False
        try:
            fingerprint = make_strm_fingerprint(webdav_path)
            self.db.upsert_b(
                str(target),
                webdav_path,
                parent_webdav_path,
                source,
                fingerprint=fingerprint,
                status="valid")
            self.db.upsert_identity(
                fingerprint=fingerprint,
                webdav_path=webdav_path,
                source_a_path=source,
                current_b_path=str(target))
            self.ensure_single_visible_instance(fingerprint, str(target))
            return True
        except sqlite3.Error as exc:
            logging.error("[B区修复失败] 数据库写入失败: %s", exc)
            return False
        except (TypeError, ValueError) as exc:
            logging.error("[B区修复失败] 指纹生成失败: %s", exc)
            return False

    def handle_b_created_or_modified(self, local_path: str) -> None:
        local = Path(local_path).resolve()
        if not local.exists():
            return
        lock = self.get_path_lock(local)
        with lock:
            webdav_path = read_strm_webdav_path(local)
            row = self.db.get_b_by_local_full(str(local))
            if not webdav_path:
                self._handle_unparseable_strm(local, row)
                return
            fingerprint = make_strm_fingerprint(webdav_path)
            if not self._verify_b_path_lineage(str(local), webdav_path):
                logging.warning("[B区越界拦截] 拒绝非法复制，该路径无对应A区源: %s", local)
                self._restore_b_from_a_after_violation(
                    local, webdav_path, fingerprint)
                return
            parent = webdav_parent(webdav_path)
            if not self._verify_a_source_exists(
                    str(local), webdav_path, fingerprint):
                logging.warning("[B区拦截] A区无对应源文件，拒绝非法strm: %s", local)
                safe_remove_file(local)
                if row:
                    self.db.delete_b_by_local(str(local))
                return
            if row:
                self._handle_existing_b_file(
                    local, webdav_path, parent, fingerprint, row)
            else:
                self._handle_new_b_file(
                    local, webdav_path, parent, fingerprint)

    def _restore_b_from_a_after_violation(
            self, local: Path, webdav_path: str, fingerprint: str) -> None:
        local_path = str(local)
        deleted = self._force_delete_and_verify(local)
        self.db.delete_b_by_local(local_path)
        if not deleted:
            logging.error("[B区越界恢复] 无法删除越界文件，跳过恢复: %s", local_path)
            return
        logging.info("[B区越界恢复] 已删除越界文件: %s", local_path)
        identity = self.db.get_identity_by_fingerprint(fingerprint)
        correct_b_path: str | None = None
        source_a_path: str | None = None
        if identity:
            historical_b_path = identity[3]
            source_a_path = identity[2]
            if historical_b_path and historical_b_path != local_path:
                historical = Path(historical_b_path)
                if historical.exists():
                    existing_webdav = read_strm_webdav_path(historical_b_path)
                    if existing_webdav == webdav_path:
                        correct_b_path = historical_b_path
                        logging.debug(
                            "[B区越界恢复] 历史合法路径仍有效，直接使用: %s", correct_b_path)
        if not correct_b_path:
            if not source_a_path or not Path(source_a_path).exists():
                source_a_path = self.find_a_source_by_webdav(webdav_path)
            if source_a_path and Path(source_a_path).exists():
                if identity and identity[3]:
                    correct_b_path = identity[3]
                else:
                    src_webdav = read_strm_webdav_path(source_a_path)
                    correct_b_path = str(
                        self.build_b_path_from_a(
                            source_a_path, src_webdav))
                try:
                    correct_b = Path(correct_b_path)
                    correct_b.parent.mkdir(parents=True, exist_ok=True)
                    with self._restoring_lock:
                        self._restoring_markers.add(fingerprint)
                    try:
                        shutil.copyfile(source_a_path, correct_b)
                        logging.info(
                            "[B区越界恢复] 已从 A 区恢复到正确位置: %s -> %s",
                            source_a_path,
                            correct_b_path)
                    finally:
                        def _remove_marker():
                            time.sleep(2)
                            with self._restoring_lock:
                                self._restoring_markers.discard(fingerprint)
                        threading.Thread(
                            target=_remove_marker, daemon=True).start()
                except Exception as exc:
                    logging.error("[B区越界恢复] 从 A 区恢复失败: %s", exc)
                    with self._restoring_lock:
                        self._restoring_markers.discard(fingerprint)
                    correct_b_path = None
            else:
                logging.warning("[B区越界恢复] 找不到 A 区源文件，无法恢复: %s", webdav_path)
        if correct_b_path:
            parent = webdav_parent(webdav_path)
            final_source_a = source_a_path or (
                identity[2] if identity else self.find_a_source_by_webdav(webdav_path))
            self.db.upsert_b(
                correct_b_path,
                webdav_path,
                parent,
                final_source_a,
                fingerprint=fingerprint,
                status="valid")
            self.db.upsert_identity(
                fingerprint=fingerprint,
                webdav_path=webdav_path,
                source_a_path=final_source_a,
                current_b_path=correct_b_path)
            self.ensure_single_visible_instance(fingerprint, correct_b_path)

    def _verify_a_source_exists(
            self, b_local_path: str, webdav_path: str, fingerprint: str) -> bool:
        identity = self.db.get_identity_by_fingerprint(fingerprint)
        if identity and identity[2]:
            if Path(identity[2]).exists():
                return True
        a_source = self.find_a_source_by_webdav(webdav_path)
        if a_source and Path(a_source).exists():
            return True
        boundary = self.db.get_media_boundary_by_fingerprint(fingerprint)
        if boundary:
            logging.debug("[A区源校验] 边界映射存在，放宽检查: %s (指纹: %s...)",
                          b_local_path, fingerprint[:8])
            return True
        logging.debug(
            "[A区源校验] A区无对应源文件: %s (指向: %s)",
            b_local_path,
            webdav_path)
        return False

    def _force_delete_and_verify(self, path: Path) -> bool:
        path_str = str(path)
        if not path.exists():
            return True
        safe_remove_file(path)
        if not path.exists():
            logging.info("[B区越界恢复] 已删除越界文件: %s", path_str)
            return True
        try:
            os.remove(path_str)
            if not path.exists():
                logging.info("[B区越界恢复] 已删除越界文件(os.remove): %s", path_str)
                return True
        except OSError as exc:
            logging.warning("[B区越界恢复] os.remove 失败 %s: %s", path_str, exc)
        try:
            import stat
            os.chmod(path_str, stat.S_IWRITE | stat.S_IREAD | stat.S_IRWXU)
            os.remove(path_str)
            if not path.exists():
                logging.info("[B区越界恢复] 已删除越界文件(chmod+remove): %s", path_str)
                return True
        except Exception as exc:
            logging.warning("[B区越界恢复] chmod+remove 失败 %s: %s", path_str, exc)
        if path.exists():
            logging.error("[B区越界恢复] 无法删除越界文件: %s", path_str)
            return False
        return True

    def _handle_unparseable_strm(self, local: Path, row: tuple | None) -> None:
        if row:
            _, old_webdav_path, parent, source_a_path, _fingerprint, _status, _ = row
            if self.restore_b_file_from_a(
                    str(local), old_webdav_path, parent, source_a_path):
                logging.warning("[B区修复] 已从A区恢复异常STRM: %s", local)
                return
        quarantined = quarantine_file(local, suffix=".invalid")
        if quarantined:
            if row:
                self.db.move_b_record(str(local), str(quarantined))
                self.db.mark_b_instance_status(str(quarantined), "quarantined")
            logging.warning(
                "[B区隔离] 无法解析STRM，已隔离: %s -> %s",
                local,
                quarantined)
        else:
            logging.warning("[B区隔离失败] 无法解析STRM: %s", local)

    def _handle_existing_b_file(
            self, local: Path, webdav_path: str, parent: str, fingerprint: str, row: tuple) -> None:
        _, old_webdav_path, old_parent, source_a_path, old_fingerprint, status, _ = row
        if old_fingerprint == fingerprint or old_webdav_path == webdav_path:
            self._refresh_b_record(
                local,
                webdav_path,
                parent,
                source_a_path,
                fingerprint,
                status)
            return
        if self.restore_b_file_from_a(
                str(local), old_webdav_path, old_parent, source_a_path):
            logging.warning("[B区修复] 内容被修改，已从A区恢复: %s", local)
            return
        self._quarantine_modified_b_file(local)

    def _refresh_b_record(self, local: Path, webdav_path: str, parent: str,
                          source_a_path: str | None, fingerprint: str, status: str | None) -> None:
        normalized_status = status or "valid"
        self.db.upsert_b(
            str(local),
            webdav_path,
            parent,
            source_a_path,
            fingerprint=fingerprint,
            status=normalized_status)
        self.db.upsert_identity(
            fingerprint=fingerprint,
            webdav_path=webdav_path,
            source_a_path=source_a_path,
            current_b_path=str(local) if normalized_status == "valid" else None)
        if normalized_status == "valid":
            self.ensure_single_visible_instance(fingerprint, str(local))

    def _quarantine_modified_b_file(self, local: Path) -> None:
        quarantined = quarantine_file(local, suffix=".invalid")
        if quarantined:
            self.db.move_b_record(str(local), str(quarantined))
            self.db.mark_b_instance_status(str(quarantined), "quarantined")
            logging.warning("[B区隔离] 内容身份变化且恢复失败: %s -> %s", local, quarantined)

    def _handle_new_b_file(self, local: Path, webdav_path: str,
                           parent: str, fingerprint: str) -> None:
        identity = self.db.get_identity_by_fingerprint(fingerprint)
        source_a_path = identity[2] if identity else self.find_a_source_by_webdav(
            webdav_path)
        self._maybe_record_boundary_mapping(local, webdav_path, fingerprint)
        self.db.upsert_b(
            str(local),
            webdav_path,
            parent,
            source_a_path,
            fingerprint=fingerprint,
            status="valid")
        self.db.upsert_identity(
            fingerprint=fingerprint,
            webdav_path=webdav_path,
            source_a_path=source_a_path,
            current_b_path=str(local))
        self.ensure_single_visible_instance(fingerprint, str(local))

    def _cloud_path_to_engine_paths(self, cloud_path: str) -> list[str]:
        result = []
        for entry_path, mapping in self.config.strm_storage_map.items():
            for mp in mapping.paths:
                if cloud_path.startswith(mp):
                    relative = cloud_path[len(mp.rstrip("/")):].lstrip("/")
                    engine_path = f"{
                        entry_path.rstrip('/')}/{relative}" if relative else entry_path
                    result.append(engine_path)
                    break
        return result

    def request_openlist_index_update(
            self, webdav_path: str, parent_webdav_path: str) -> None:
        del webdav_path
        engine_paths = self._cloud_path_to_engine_paths(parent_webdav_path)
        if not engine_paths:
            logging.debug(
                "[OpenListAdmin] 无法映射引擎路径，跳过索引更新: %s",
                parent_webdav_path)
            return
        if not self.admin_api.token and not self.admin_api.login():
            logging.warning("[OpenListAdmin] 登录失败，跳过索引更新")
            return
        ok = self.admin_api.trigger_refresh_via_fs_list(engine_paths)
        if ok:
            logging.info("[OpenListAdmin] 已请求更新strm索引: %s", engine_paths)
        else:
            logging.warning("[OpenListAdmin] 索引更新触发失败: %s", engine_paths)

    def handle_b_deleted(self, local_path: str) -> None:
        local = Path(local_path).resolve()
        lock = self.get_path_lock(local)
        with lock:
            row = self.db.get_b_by_local_full(str(local))
            if not row:
                return
            _, webdav_path, _parent_webdav_path, _source_a_path, fingerprint, _status, _ = row
            with self._restoring_lock:
                if fingerprint in self._restoring_markers:
                    logging.info("[B区删除] 检测到程序恢复操作，跳过追删: %s", local_path)
                    return
            if self.db.has_other_b_instance(fingerprint, str(local)):
                logging.info("[B区删除联动] B区中仍存在同指纹文件，跳过WebDAV删除: %s", local_path)
                self.db.delete_b_by_local(str(local))
                return
            if fingerprint and self._check_fingerprint_exists_in_b(
                    fingerprint, exclude_path=str(local)):
                logging.info(
                    "[B区删除联动] B区文件系统中仍存在同指纹文件，跳过WebDAV删除: %s",
                    local_path)
                self.db.delete_b_by_local(str(local))
                return
            if webdav_path:
                self._execute_webdav_deletion(webdav_path, _parent_webdav_path)
                self._delete_a_file_by_webdav(webdav_path)
            self.db.delete_b_by_local(str(local))
            if fingerprint:
                self.refresh_identity_current_b_path(fingerprint)

    def _check_fingerprint_exists_in_b(
            self, fingerprint: str, exclude_path: str | None = None) -> bool:
        b_instances = self.db.get_b_instances_by_fingerprint(fingerprint)
        for instance in b_instances:
            instance_path = instance[0]
            if exclude_path and instance_path == exclude_path:
                continue
            if Path(instance_path).exists():
                return True
        try:
            b_root = Path(self.config.paths.b_root)
            if b_root.exists():
                for strm_file in b_root.rglob("*.strm"):
                    if exclude_path and str(strm_file) == exclude_path:
                        continue
                    try:
                        file_webdav = read_strm_webdav_path(str(strm_file))
                        if file_webdav:
                            file_fingerprint = make_strm_fingerprint(
                                file_webdav)
                            if file_fingerprint == fingerprint:
                                return True
                    except Exception:
                        continue
        except Exception as e:
            logging.debug("[指纹检查] 扫描 B 区文件系统失败: %s", e)
        return False

    def _execute_webdav_deletion(
            self, webdav_path: str, parent_webdav_path: str) -> bool:
        logging.debug("[WebDAV删除] 进入，路径=%s, 父目录=%s", webdav_path, parent_webdav_path)
        lock = self.get_path_lock(webdav_path)
        with lock, self._dav_write_lock:
            ok = self._perform_webdav_action(webdav_path)
            logging.debug("[WebDAV删除] _perform_webdav_action 返回: %s", ok)
            if ok:
                self.request_openlist_index_update(
                    webdav_path, parent_webdav_path)
                self.db.set_ghost_protection(
                    webdav_path,
                    self.config.behavior.ghost_protect_seconds,
                    reason="b_deleted")
                logging.info("[WebDAV删除] 已处理: %s", webdav_path)
            else:
                logging.warning("[WebDAV删除] 处理失败: %s", webdav_path)
            logging.debug("[WebDAV删除] 退出，返回=%s", ok)
            return ok

    def _delete_a_file_by_webdav(self, webdav_path: str) -> None:
        a_record = self.db.get_a_by_webdav(webdav_path)
        if a_record:
            a_path = a_record[0]
            if Path(a_path).exists():
                safe_remove_file(a_path)
                logging.info("[A区删除] B区删除联动，清理A区: %s", a_path)
            self.db.delete_a_by_local(a_path)

    def _perform_webdav_action(self, webdav_path: str) -> bool:
        cloud_path = webdav_path
        action = self.config.behavior.action
        logging.info("[云盘操作] 路径=%s, 动作=%s", cloud_path, action)
        
        if action == "MOVE":
            trash_path = self._build_trash_path(cloud_path)
            logging.info("[回收站] 目标=%s", trash_path)
            if not trash_path:
                logging.error("[回收站] 无法构建路径: %s", cloud_path)
                return False
            
            if not self._ensure_trash_dirs(trash_path):
                logging.error("[回收站] 创建目录失败: %s", trash_path)
                return False
            
            logging.debug("[云盘操作] 执行移动: %s -> %s", cloud_path, trash_path)
            ok = self.admin_api.move(cloud_path, trash_path)
            if not ok:
                logging.error("[云盘操作] 移动失败: %s -> %s", cloud_path, trash_path)
            else:
                logging.info("[云盘操作] 移动成功: %s -> %s", cloud_path, trash_path)
            return ok
        
        # DELETE 操作
        logging.debug("[云盘操作] 执行删除: %s (action=%s)", cloud_path, action)
        ok = self.admin_api.remove(cloud_path)
        if not ok:
            logging.error("[云盘操作] 删除失败: %s", cloud_path)
        else:
            logging.info("[云盘操作] 删除成功: %s", cloud_path)
        return ok

    def _webdav_to_cloud_path(self, webdav_path: str) -> str:
        return webdav_path

    def migrate_b_under_root_to_c(self, root_path: str) -> None:
        del root_path

    def cleanup_b_zombies_under_folder(self, root_path: str) -> None:
        del root_path

    def refresh_identity_current_b_path(self, fingerprint: str) -> None:
        del fingerprint

    def _maybe_record_boundary_mapping(
            self, local: Path, webdav_path: str, fingerprint: str) -> None:
        del local, webdav_path, fingerprint

    def _handle_b_zombie(self, *args, **kwargs):
        del args, kwargs

    def trigger_delayed_cleanup(self, parent_webdav_path: str) -> None:
        del parent_webdav_path

    def _build_trash_path(self, cloud_path: str) -> str | None:
        return build_webdav_trash_path(
            cloud_path, self.config.behavior.trash_dir_name)

    def _ensure_trash_dirs(self, trash_path: str) -> bool:
        """确保 WebDAV 回收站目录存在（通过 admin_api.mkdir 创建远程目录）"""
        try:
            # trash_path 是 WebDAV 路径，如 /mount/trash_dir/subdir/file.mp4
            # 需要提取父目录并使用 admin_api 创建远程目录
            parts = trash_path.rstrip("/").split("/")
            if len(parts) < 2:
                logging.warning("[回收站] 路径格式异常: %s", trash_path)
                return False
            trash_parent = "/".join(parts[:-1])
            logging.debug("[回收站] 创建远程目录: %s", trash_parent)
            ok = self.admin_api.mkdir(trash_parent)
            if not ok:
                logging.warning("[回收站] 目录创建失败: %s", trash_parent)
            else:
                logging.debug("[回收站] 目录创建成功: %s", trash_parent)
            return ok
        except Exception as e:
            logging.error("[回收站] 创建目录异常: %s", e)
            return False
