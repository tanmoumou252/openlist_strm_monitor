from __future__ import annotations
from pathlib import Path
import posixpath
import urllib.parse
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Generator, Optional
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
# autopep8: off
# isort: off
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def ensure_base_dir_first():
    normalized_base_dir = os.path.normcase(os.path.abspath(BASE_DIR))
    sys.path[:] = [p for p in sys.path if os.path.normcase(
        os.path.abspath(p or os.getcwd())) != normalized_base_dir]
    sys.path.insert(0, BASE_DIR)

ensure_base_dir_first()


from utils import (
    make_strm_fingerprint,
    read_strm_webdav_path,
    webdav_parent,
    build_webdav_trash_path,
    quarantine_file,
    safe_remove_file,
    remove_empty_dirs,
    move_file,
)
from refresh_service import RefreshService
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
# autopep8: on
# isort: on


def load_local_module(module_name: str, filename: str,
                      base_dir: str | None = None):
    import importlib.util
    from types import ModuleType

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


try:
    import tomllib
except ImportError:
    import tomli as tomllib

# ==================== STRM 存储管理类 ====================


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
        """获取所有 STRM 存储"""
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
        """获取有效的更新模式存储"""
        return [s for s in self.get_strm_storages(
        ) if s.is_working and s.is_sync_mode]

    def validate_against_local_paths(
            self, local_strm_engine_paths: list[str]) -> dict:
        """验证本地配置与 API 的一致性"""
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


# =========================================================
class AppService:
    def __init__(self, config: AppConfig, db: Database,
                 admin_api: OpenListAdminClient) -> None:
        self.config = config
        self.db = db
        self.admin_api = admin_api
        self._observers: list[Observer] = []
        self.observer: Observer | None = None
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
        self._restoring_markers: set[str] = set()  # 正在恢复的文件指纹集合
        self._restoring_lock = threading.Lock()
        self.db.init_subtitle_table()   # 初始化字幕表

    def get_path_lock(self, path: str | Path) -> threading.Lock:
        key = str(Path(path).resolve())
        with self._path_locks_lock:
            lock = self._path_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._path_locks[key] = lock
        return lock

    def is_path_under_any_root(self, path: str, roots: list[str]) -> bool:
        """判断 path 是否属于 roots 中任意一个根路径的子路径或本身。"""
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
        """判断主动刷新根路径是否在 STRM 引擎监控范围内。
        当 strm_engine_paths 为空时，保持向后兼容：认为所有 refresh_paths 都有效。
        """
        if not self.config.strm_engine_paths:
            return True
        return self.is_path_under_any_root(
            root_path, self.config.strm_engine_paths)

    def is_strm_engine_monitored(self, root_path: str) -> bool:
        """判断路径是否在 STRM 引擎监控范围内。
        当 strm_engine_paths 为空时，默认所有路径都受监控（向后兼容）。
        否则，检查 root_path 是否被包含在 strm_engine_paths 中。
        """
        return self.is_valid_refresh_root(root_path)

    def _find_matching_engine_path(self, webdav_path: str) -> str | None:
        """根据 WebDAV 路径找到对应的 STRM 引擎入口路径。
        STRM引擎规则：引擎入口只显示最后一层，中间路径省略。
        例如：真实路径 /测试a/测试 的引擎入口是 /测试a
        匹配逻辑：找到能作为 webdav_path 前缀的最长 engine_path
        """
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

    def sync_protected_roots_from_config(self) -> None:
        """从配置同步受保护的根目录到数据库。基于引擎监控路径。"""
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
        """扫描并清理已移除的受保护根目录。"""
        current_roots = set(self.db.get_protected_root_paths())
        snapshot_roots = set(self.db.get_protected_roots_snapshot_paths())
        removed_roots = sorted(snapshot_roots - current_roots)
        for root_path in removed_roots:
            logging.warning("[保护根目录] 检测到已移除路径: %s", root_path)
            self.migrate_b_under_root_to_c(root_path)
            self.db.remove_known_folder_prefix(root_path)

    def persist_current_roots_snapshot(
            self, valid_engine_paths: list[str] | None = None) -> None:
        """持久化当前根目录快照，可选只保存有效的引擎路径。"""
        roots = [
            (root_path, trash_path)
            for root_path, trash_path, active, _updated_at in self.db.get_protected_roots()
            if active and (valid_engine_paths is None or root_path in valid_engine_paths)
        ]
        self.db.save_protected_roots_snapshot(roots)

    def refresh_webdav_root(self, root_path: str, depth: int) -> None:
        """递归刷新 WebDAV 根目录，将发现的目录结构同步到数据库。
        如果根路径不属于 STRM 引擎监控范围，只执行 WebDAV 刷新和 known_folders 维护，
        不触发 B 区冗余清理，避免误删非 STRM 引擎管理的目录。
        """
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
        """只读刷新 WebDAV 根目录，同步目录结构到数据库，但不清理 B 区。
        用于处理"程序监控但引擎未监控"的路径，避免误删B区文件。
        """
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
                # 将相对名称拼接到父路径上
                sub_path = f"{normalized_path}/{folder_name}"
                self._refresh_webdav_recursive(
                    sub_path, max_depth, current_depth + 1)
        return True

    def start(self) -> None:
        # 调整启动顺序，必须先准备环境和初始化数据库！
        logging.info("[启动] 准备环境并初始化数据库...")
        self.prepare_environment()
        self.db.init_db()
        logging.info("[启动] 数据库初始化完成")

        self.update_engine_configs()
        logging.info("[启动] 引擎配置加载完成")

        self.initial_scan_b()  # 现在数据库表已经存在，可以安全扫描了

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
        from area_watchers import (
            AAreaEventHandler,
            BAreaEventHandler,
            CAreaEventHandler,
        )

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
        """将 A 中的文件映射到 B"""
        a_local = Path(a_local_path).resolve()
        a_root = self.get_a_root_for_path(a_local)
        if a_root is None:
            raise ValueError(f"文件不属于任何A根目录: {a_local}")

        rel = a_local.relative_to(a_root)

        # 1. 电影检测：保持原有结构，不做番剧重命名
        is_movie = self._should_treat_as_movie(a_local, webdav_path)
        if is_movie:
            return self.b_root / rel

        # 2. 尝试自动重命名（suggest_rename 驱动）
        suggested_name = suggest_rename(a_local)
        if suggested_name and webdav_path:
            # 优先从 webdav_path 提取季信息
            season = self._extract_season_from_webdav_path(webdav_path)
            # 回退从路径中的中文季目录提取
            if season is None:
                season = extract_season_from_path(a_local)
            # 回退到从文件名提取
            if season is None:
                season, _ = _extract_season_episode(a_local.name)

            # 从文件名提取集信息
            _, episode = _extract_season_episode(a_local.name)

            if season is not None and episode is not None:
                # suggest_rename 返回完整格式 S01E01.ext
                if suggested_name:
                    standard_name = suggested_name
                else:
                    # 兼容旧格式或异常情况
                    standard_name = f"S{
                        season:02d}E{
                        episode:02d}{
                        Path(a_local).suffix}"

                # ===== 关键修复：保留 A 区完整相对目录结构，只替换文件名并插入 Season =====
                rel_parts = list(rel.parts)

                # 检查 A 区路径中是否已有 Season 目录或中文季目录
                has_season_dir = False
                season_dir_index = -1
                cn_season_dir_index = -1
                for i, part in enumerate(rel_parts[:-1]):  # 排除文件名
                    if re.match(r"(?i)^season\s*\d+$", part):
                        has_season_dir = True
                        season_dir_index = i
                        break
                    # 检测中文季目录如 "第二季"
                    if re.match(r"^第[一二三四五六七八九十\d]+季$", part):
                        cn_season_dir_index = i

                if has_season_dir:
                    # 已有 Season 目录：替换为正确的季号，标准化文件名
                    # 保留 Season 之前的所有目录结构
                    new_rel = Path(
                        *rel_parts[:season_dir_index]) / f"Season {season:02d}" / standard_name
                elif cn_season_dir_index >= 0:
                    # 有中文季目录：替换为 Season XX，移除中文季目录
                    new_rel = Path(
                        *rel_parts[:cn_season_dir_index]) / f"Season {season:02d}" / standard_name
                else:
                    # 无 Season 目录：在文件名的父目录下添加 Season XX
                    # 保留所有父目录结构
                    new_rel = Path(*rel_parts[:-1]) / \
                        f"Season {season:02d}" / standard_name

                return self.b_root / new_rel
                # ========================================================================

        # 默认行为：保持原有结构
        return self.b_root / rel

    def _reverse_map_b_to_a(self, b_local_path: str | Path) -> str | None:
        """根据 B 区路径，反推其合法的 A 区对应路径"""
        b_local = Path(b_local_path).resolve()
        try:
            # 去掉 B区根目录 前缀
            rel = b_local.relative_to(self.b_root)
        except ValueError:
            return None

        if not rel.parts:
            return None

        sub_path = Path(rel)

        # 遍历所有 A 区根目录，尝试构建匹配路径
        for a_root in self.a_roots:
            candidate = a_root / sub_path
            if candidate.exists():
                return str(candidate)
        return None

    def update_engine_configs(self):
        """从 API 获取并缓存引擎配置，用于血统校验和路径拼凑"""
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

                # 兼容 paths 为列表的解析
                paths_val = addition.get("paths", "")
                if isinstance(paths_val, list):
                    source_paths = [str(p).strip()
                                    for p in paths_val if str(p).strip()]
                else:
                    source_paths = [p.strip()
                                    for p in paths_val.split("\n") if p.strip()]

                if not save_path:
                    logging.warning(
                        "[引擎配置] 存储 [%s] 缺少 'SaveStrmLocalPath' 配置或为空，已跳过此引擎映射！", mount_path
                    )
                    continue

                resolved_save_path = str(Path(save_path).resolve())
                self.engine_configs.append(
                    {
                        "a_root_norm": resolved_save_path,
                        "mount_path": mount_path,
                        "source_paths": source_paths,
                    }
                )
                logging.info(
                    "[引擎配置] 成功加载引擎映射: 挂载点 [%s] -> 本地 A区 [%s] (包含 %d 个云端监控源)",
                    mount_path,
                    resolved_save_path,
                    len(source_paths),
                )
            except Exception as e:
                logging.error("[引擎配置] 解析存储 [%s] 配置失败: %s", mount_path, e)

    def _verify_b_path_lineage(
            self, b_local_path: str, webdav_path: str, is_sync_phase: bool = False) -> bool:
        """
        [血统校验]
        适配 build_b_path_from_a 行为：保留A区完整层级 + 插入 Season XX
        """
        fingerprint = make_strm_fingerprint(webdav_path)
        b_local = Path(b_local_path).resolve()

        # 1. 通过指纹查找 A 区源记录（最可靠）
        a_record = self.db.get_a_by_webdav(webdav_path)
        if not a_record:
            identity = self.db.get_identity_by_fingerprint(fingerprint)
            if identity and identity[2]:
                a_local_path = Path(identity[2])
                if a_local_path.exists():
                    a_record = (str(a_local_path), webdav_path, "", 0)

        if not a_record:
            logging.debug("[血统校验失败] 无A区源记录: %s", b_local_path)
            return False

        a_local_path = Path(a_record[0]).resolve()
        if not a_local_path.exists():
            logging.debug("[血统校验失败] A区源文件不存在: %s", a_local_path)
            return False

        # 2. 找到 A 区根目录
        a_root = self.get_a_root_for_path(a_local_path)
        if not a_root:
            logging.debug("[血统校验失败] A区源不在任何根目录下: %s", a_local_path)
            return False

        # 3. 提取相对路径
        try:
            a_rel = a_local_path.relative_to(a_root)
            b_rel = b_local.relative_to(self.b_root)
        except ValueError:
            logging.debug("[血统校验失败] 路径超出根目录")
            return False

        a_rel_dir = a_rel.parent
        b_rel_dir = b_rel.parent

        # 4. 快速通过：完全一致（不含 Season 的情况）
        if a_rel_dir == b_rel_dir:
            logging.debug("[血统校验通过] 路径完全一致: %s", b_local_path)
            return True

        # 5. 处理 Season 层级差异
        a_parts = list(a_rel_dir.parts)
        b_parts = list(b_rel_dir.parts)

        # 检查 B 是否比 A 多一层 Season XX
        if len(b_parts) == len(a_parts) + 1:
            # B 的前缀应该等于 A
            if b_parts[:len(a_parts)] == a_parts:
                # 最后一层是 Season XX
                last_part = b_parts[-1]
                if re.match(r"(?i)^season\s*\d+$", last_part):
                    logging.debug("[血统校验通过] B区自动添加Season层级: %s", b_local_path)
                    return True

        # 5.5 处理多层 Season 变化（如 S01 -> S02）
        # 检查是否是同一媒体文件夹下的 Season 变化
        if len(a_parts) >= 1 and len(b_parts) >= 1:
            # 提取媒体文件夹名（假设是 Season 的父级或更上级）
            a_media_name = None
            b_media_name = None

            for i, part in enumerate(a_parts):
                if re.match(r"(?i)^season\s*\d+$", part):
                    if i > 0:
                        a_media_name = a_parts[i - 1]
                    break

            for i, part in enumerate(b_parts):
                if re.match(r"(?i)^season\s*\d+$", part):
                    if i > 0:
                        b_media_name = b_parts[i - 1]
                    break

            # 如果媒体文件夹名相同，或者通过边界映射关联，允许
            if a_media_name and b_media_name:
                if a_media_name == b_media_name:
                    logging.debug("[血统校验通过] 同一媒体不同Season: %s", b_local_path)
                    return True

                # 检查边界映射
                boundary = self.db.get_media_boundary_by_source_name_only(
                    a_media_name)
                if boundary:
                    _, mapped_source, mapped_current, _, _ = boundary
                    if b_media_name in (mapped_source, mapped_current):
                        logging.debug(
                            "[血统校验通过] 边界映射Season变化: %s", b_local_path)
                        return True

        # 6. 引擎配置检查（用于后续边界映射）
        if not hasattr(self, "engine_configs") or not self.engine_configs:
            logging.debug("[血统校验放行] 引擎配置未加载: %s", b_local_path)
            return True

        # 找到匹配的引擎配置
        a_root_norm = str(a_root.resolve())
        config = next(
            (c for c in self.engine_configs if c["a_root_norm"]
             == a_root_norm),
            None,
        )
        if not config:
            logging.debug("[血统校验放行] 未找到引擎配置: %s", b_local_path)
            return True

        # 7. 识别云端媒体根目录
        source_path = next(
            (sp for sp in config["source_paths"] if webdav_path.startswith(
                sp.rstrip("/") + "/")), None)
        if not source_path:
            logging.debug("[血统校验放行] 不在监控范围内: %s", b_local_path)
            return True

        rel_cloud_str = webdav_path[len(source_path.rstrip("/")):].lstrip("/")
        rel_parts = rel_cloud_str.split("/")
        cloud_show_name = rel_parts[0] if len(rel_parts) >= 2 else None

        # 8. 识别 B 区物理层级中的"媒体文件夹"
        # 当前 B 区路径：b_root / 测试番剧 / [2023] 女神的露天咖啡厅 / Season 01
        # physical_media_folder_name 应该是 [2023] 女神的露天咖啡厅（Season 的父级）
        physical_media_folder_name = None
        for i, part in enumerate(b_parts):
            if re.match(r"(?i)^season\s*\d+$", part):
                # Season 的前一级是媒体文件夹
                if i > 0:
                    physical_media_folder_name = b_parts[i - 1]
                break

        # 如果没有 Season，取最后一级
        if physical_media_folder_name is None and b_parts:
            physical_media_folder_name = b_parts[-1]

        # 9. 边界校验：不能提到引擎根目录
        if len(b_parts) < 2:
            if len(rel_parts) < 2:
                logging.debug("[血统校验] 根目录电影，放行: %s", b_local_path)
                return True
            logging.warning("[血统校验失败] 越界文件: %s", b_local_path)
            return False

        # 10. 边界映射检查
        boundary = self.db.get_media_boundary_by_fingerprint(fingerprint)
        if boundary:
            _, source_media_name, current_media_name, _, _ = boundary
            if physical_media_folder_name == current_media_name:
                logging.debug(
                    "[血统校验] 边界映射匹配: %s (路径: %s)",
                    current_media_name,
                    b_local_path)
                return True
            if physical_media_folder_name == source_media_name:
                logging.debug(
                    "[血统校验] 回到源边界: %s (路径: %s)",
                    source_media_name,
                    b_local_path)
                return True

        # 10.5 交叉边界映射检查（处理更新番剧场景）
        # 检查：当前物理位置是否匹配某个"源媒体名"对应的"当前媒体名"
        # 或者：当前物理位置是"源媒体名"，而某个映射的"当前媒体名"是另一个已知位置
        if cloud_show_name and physical_media_folder_name:
            # 情况A：当前在"源边界"位置，检查是否有映射到"当前边界"
            boundary_by_source = self.db.get_media_boundary_by_source_name_only(
                physical_media_folder_name
            )
            if boundary_by_source:
                # 找到了：physical_media_folder_name 作为 source_media_name 的映射
                _, mapped_source, mapped_current, _, _ = boundary_by_source
                # 验证 cloud_show_name 是否匹配（确保是同一部番剧）
                if cloud_show_name == mapped_source or cloud_show_name == mapped_current:
                    logging.debug(
                        "[血统校验] 交叉边界映射匹配(源->当前): %s -> %s (路径: %s)",
                        physical_media_folder_name, mapped_current, b_local_path)
                    return True

            # 情况B：当前在"当前边界"位置，检查反向映射
            boundary_by_current = self.db.get_media_boundary_by_current_name(
                physical_media_folder_name, str(self.b_root)
            )
            if boundary_by_current:
                _, mapped_source, mapped_current, _, _ = boundary_by_current
                if cloud_show_name == mapped_source or cloud_show_name == mapped_current:
                    logging.debug(
                        "[血统校验] 交叉边界映射匹配(当前->源): %s <- %s (路径: %s)",
                        physical_media_folder_name, mapped_source, b_local_path)
                    return True

            # 情况C：检查 cloud_show_name 是否有映射记录
            boundary_by_cloud = self.db.get_media_boundary_by_source_name_only(
                cloud_show_name
            )
            if boundary_by_cloud:
                _, mapped_source, mapped_current, _, _ = boundary_by_cloud
                # 如果当前物理位置是映射的任一端，都允许
                if physical_media_folder_name in (
                        mapped_source, mapped_current):
                    logging.debug(
                        "[血统校验] 交叉边界映射匹配(云端): %s -> %s (路径: %s)",
                        cloud_show_name, mapped_current, b_local_path)
                    return True

        # 11. 同步阶段：记录边界映射
        if is_sync_phase and cloud_show_name and physical_media_folder_name != cloud_show_name:
            existing = self.db.get_media_boundary_by_fingerprint(fingerprint)
            if not existing:
                self.db.upsert_media_boundary(
                    fingerprint=fingerprint,
                    source_media_name=cloud_show_name,
                    current_media_name=physical_media_folder_name,
                    engine_entry_path=str(self.b_root),
                )
                logging.info(
                    "[边界映射] 记录新映射: %s -> %s",
                    cloud_show_name, physical_media_folder_name,
                )
            elif existing[2] != physical_media_folder_name:
                self.db.upsert_media_boundary(
                    fingerprint=fingerprint,
                    source_media_name=existing[1],
                    current_media_name=physical_media_folder_name,
                    engine_entry_path=str(self.b_root),
                )
                logging.info(
                    "[边界映射] 更新映射: %s -> %s",
                    existing[1], physical_media_folder_name,
                )
            return True

        # 12. 群体改名 / 单兵改名检测
        if cloud_show_name and physical_media_folder_name != cloud_show_name:
            cloud_media_root = f"{source_path.rstrip('/')}/{cloud_show_name}"

            # 检查整个番剧只有一集
            total_a_episodes = self.db.get_a_count_under_root(cloud_media_root)
            if total_a_episodes <= 1:
                logging.debug("[血统校验] 单集番剧，放行: %s", b_local_path)
                return True

            # 检查是否是群体改名（B 区该目录下多个文件来自同一云端媒体）
            physical_media_root_dir = self.b_root
            # 构建物理媒体根目录路径
            for i, part in enumerate(b_parts):
                if part == physical_media_folder_name:
                    physical_media_root_dir = self.b_root / \
                        Path(*b_parts[:i + 1])
                    break

            local_matches = 0
            if physical_media_root_dir.exists():
                for p in physical_media_root_dir.rglob("*.strm"):
                    s_webdav = read_strm_webdav_path(p)
                    if s_webdav and s_webdav.startswith(
                            cloud_media_root + "/"):
                        local_matches += 1

            if local_matches <= 1:
                logging.info(
                    "[血统校验] 潜在单兵重命名，进入观察期: %s",
                    physical_media_root_dir,
                )
                self.trigger_delayed_solo_check(
                    str(physical_media_root_dir), cloud_media_root)
                return True

        logging.debug("[血统校验通过] 默认放行: %s", b_local_path)
        return True

    def trigger_delayed_solo_check(
            self, physical_dir: str, cloud_media_root: str):
        """为某个物理文件夹开启单兵存活校验"""
        with self.cleanup_lock:
            # 如果该文件夹已经在观察名单了，刷新时间（防止批量重命名过程中反复触发）
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
        """30秒时间到，进行最终判定"""
        logging.info("[单兵审判] 观察期结束，开始判定: %s", physical_dir)
        p_dir = Path(physical_dir)
        if not p_dir.exists():
            return

        # 再次清点
        matches = []
        for p in p_dir.rglob("*.strm"):
            s_webdav = read_strm_webdav_path(p)
            if s_webdav and s_webdav.startswith(cloud_media_root + "/"):
                matches.append(p)

        # 查一下云端总数
        total_a = self.db.get_a_count_under_root(cloud_media_root)

        # 最终审判：
        # 如果 30 秒后，这个文件夹里还是只有 1 个人，而云端明明有好多集
        # 说明这真的是一次错误的单兵越界操作，或者是用户只移了一集就停手了
        if len(matches) == 1 and total_a > 1:
            bad_file = matches[0]
            logging.warning("[B区清理] 审判结果：确认单兵脱离集体，执行物理删除: %s", bad_file)
            safe_remove_file(bad_file)
            self.db.delete_b_by_local(str(bad_file))
            self.cleanup_local_empty_dirs()
        else:
            logging.info("[单兵审判] 审判结果：判定为合法的批量操作或单集作品，予以保留。")

    def initial_scan_b(self) -> None:
        """启动时扫描 B 区现有文件，与数据库对比进行自同步"""
        logging.info("[初始化] 开始扫描 B 区现有文件...")
        b_root = Path(self.config.paths.b_root)
        if not b_root.exists():
            logging.info("[初始化] B 区根目录不存在，跳过扫描")
            return

        # 1. 收集本地磁盘上的实际文件
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

        # 2. 获取数据库中所有 B 区记录
        try:
            all_b_records = self.db.get_all_b_records()
            logging.info("[初始化] 成功读取 B 区历史数据库记录: %d 条", len(all_b_records))
        except Exception as e:
            logging.error("[初始化] 查询历史记录失败 (通常是因为表不存在): %s", e)
            return

        processed_disk_paths = set()

        # 3. 处理数据库中有，但磁盘上没有或发生了重命名的数据
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
                # 更新数据库路径
                self.db.move_b_record(db_local_path, valid_new_path)
                identity = self.db.get_identity_by_fingerprint(db_fingerprint)
                if identity and identity[3] == db_local_path:
                    self.db.update_identity_b_path(
                        db_fingerprint, valid_new_path)

                # 物理删除旧路径文件（如果存在且不同于新路径）
                try:
                    old_path_obj = Path(db_local_path)
                    if old_path_obj.exists() and str(old_path_obj.resolve()) != str(
                            Path(valid_new_path).resolve()):
                        safe_remove_file(old_path_obj)
                        logging.debug("[B区自同步] 删除旧路径物理文件: %s", db_local_path)
                except Exception as e:
                    logging.warning(
                        "[B区自同步] 删除旧路径物理文件失败: %s (%s)", db_local_path, e)

                # 触发单实例检查，确保评分机制生效
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

        # 4. 处理磁盘上有，但数据库中没有的文件
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
                    status="valid",
                )

                self.db.upsert_identity(
                    fingerprint=fingerprint,
                    webdav_path=webdav_path,
                    source_a_path=source_a_path,
                    current_b_path=disk_path,
                )

                self.ensure_single_visible_instance(fingerprint, disk_path)
                new_insert_count += 1
                logging.debug("[B区自同步] 新增本地文件入库: %s", disk_path)

        logging.info("[初始化] B 区扫描自同步完成，本次共新增入库 %d 个文件记录", new_insert_count)

    def initial_scan_a(self) -> None:
        logging.info("[初始化] 扫描A区")
        total_strm = 0
        total_subtitle = 0
        for a_root in self.a_roots:
            if not a_root.exists():
                continue
            for root, _dirs, files in os.walk(a_root):
                for name in files:
                    file_path = Path(root) / name
                    if name.lower().endswith(".strm"):
                        self.handle_a_created_or_modified(str(file_path))
                        total_strm += 1
                    elif is_subtitle_file(file_path):
                        self.process_subtitle_file(file_path)
                        total_subtitle += 1
        logging.info(
            "[初始化] A区扫描完成，共处理 %s 个 STRM 文件，%s 个字幕文件",
            total_strm,
            total_subtitle)

    def scan_a_to_b_full_sync(
            self, valid_engine_paths: list[str] | None = None) -> None:
        """A -> B 全量同步，可选只同步指定引擎路径下的文件。

        Args:
            valid_engine_paths: 如果提供，只同步这些路径下的文件；
                            如果为 None，同步所有（兼容旧逻辑）。
        """
        logging.info("[初始化] A -> B 全量同步")
        if valid_engine_paths is not None:
            logging.info("[初始化] 限制同步范围: %s", valid_engine_paths)
        total_records = 0
        success_count = 0
        fail_count = 0
        skip_count = 0
        for local_path, webdav_path, parent_webdav_path, _ in self.db.get_all_a():
            # ===== 修复：检查源文件是否存在 =====
            if not Path(local_path).exists():
                logging.warning("[A->B跳过] 源文件不存在: %s", local_path)
                continue
            # ====================================
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

    def cleanup_b_redundant(self) -> None:
        """清理 B 区的冗余文件（.duplicate、.quarantined、.invalid）和空文件夹"""
        b_root = Path(self.config.paths.b_root)
        if not b_root.exists():
            return

        # 清理冗余后缀文件（包括带时间戳的）
        redundant_keywords = ["duplicate", "quarantined", "invalid"]
        for keyword in redundant_keywords:
            for file_path in b_root.rglob(f"*.{keyword}"):
                try:
                    safe_remove_file(file_path)
                    self.db.delete_b_by_local(str(file_path))
                    logging.info("[冗余清理] 已删除冗余文件: %s", file_path)
                except OSError as e:
                    logging.warning("[冗余清理] 删除冗余文件失败: %s (%s)", file_path, e)

            # 清理带时间戳的冗余文件（如 .duplicate.1234567890）
            for file_path in b_root.rglob(f"*.{keyword}.*"):
                try:
                    safe_remove_file(file_path)
                    self.db.delete_b_by_local(str(file_path))
                    logging.info("[冗余清理] 已删除带时间戳的冗余文件: %s", file_path)
                except OSError as e:
                    logging.warning(
                        "[冗余清理] 删除带时间戳冗余文件失败: %s (%s)", file_path, e)

        # ========== 三层校验：清理 B 区中 A 区源已不存在的冗余 STRM ==========
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
            local_path = row[0]  # local_path
            webdav_path = row[1]  # webdav_path
            source_a_path = row[3]  # source_a_path
            fingerprint = row[4]  # fingerprint

            if not webdav_path:
                continue

            # 第 1 层：幽灵保护检查
            if self.db.is_ghost_protected(webdav_path):
                continue

            # 第 2 层：A 区源文件存在性检查
            source_exists = False
            if source_a_path and Path(source_a_path).exists():
                source_exists = True
            else:
                alt_source = self.find_a_source_by_webdav(webdav_path)
                if alt_source:
                    source_exists = True

            if not source_exists:
                # A 区源文件不存在，检查 WebDAV 上是否仍存在
                # 如果 WebDAV 上存在，说明 A 区只是暂时不可用（如 OpenList 同步模式中），
                # 应该跳过清理，等待 A 区重建
                if self.admin_api.check_exists(webdav_path):
                    logging.debug(
                        "[冗余清理跳过] A区源文件暂不可用但WebDAV存在，跳过清理: %s",
                        webdav_path)
                    continue

                # A 区源文件不存在且 WebDAV 上也不存在，迁移到 C 区

            if not source_exists:
                # A 区源文件不存在，迁移到 C 区
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
                    webdav_parent(webdav_path),
                )
                self.db.delete_b_by_local(local_path)
                if fingerprint:
                    self.refresh_identity_current_b_path(fingerprint)
                migrated_count += 1
                logging.info(
                    "[冗余清理→C区] A区源文件已不存在，迁移至C区: %s -> %s",
                    local_path,
                    webdav_path)
                continue

            # 第 3 层：WebDAV 存在性检查
            if self.admin_api.check_exists(webdav_path):
                continue

            # WebDAV 上文件不存在，直接删除
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

    def handle_a_created_or_modified(self, local_path: str) -> None:
        local = Path(local_path).resolve()
        if not local.exists():
            return
        if self.get_a_root_for_path(local) is None:
            logging.debug("[A区跳过] 不属于任何A根目录: %s", local)
            return

        # 字幕文件独立处理，不进入 STRM 流程
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
        # 如果 WebDAV 已经没有该文件，直接在 A 区删除 STRM 并同步 DB
        if not self.admin_api.check_exists(webdav_path):
            logging.warning(
                "[A区即时清理] WebDAV 已不存在，删除本地冗余 STRM: %s",
                local,
            )
            safe_remove_file(str(local))
            self.db.delete_a_by_local(str(local))
            self.db.set_ghost_protection(
                webdav_path,
                self.config.behavior.ghost_protect_seconds,
                reason="webdav_not_exists",
            )
            return
        # -----------------------------------------
        old_identity = self.db.get_identity_by_fingerprint(fingerprint)
        current_b_path = old_identity[3] if old_identity else None

        self.db.upsert_identity(
            fingerprint=fingerprint,
            webdav_path=webdav_path,
            source_a_path=str(local),
            current_b_path=current_b_path,
        )

        if self.db.is_ghost_protected(webdav_path):
            logging.info("[A->B阻断] ghost保护中，跳过复制: %s", webdav_path)
            return

        try:
            b_local = self.build_b_path_from_a(local, webdav_path)
        except ValueError as exc:
            logging.warning("[A->B跳过] %s", exc)
            return

        # =======================================================
        # 拦截逻辑：防止反复复制劣质重名文件到B区再被隔离
        # 如果在复制前，B区已经有该指纹的有效主实例，进行评分比较。
        # 如果A区这个文件的名字并不比B区现有的好，直接拒绝物理复制！
        # =======================================================
        valid_b_instance = self.db.get_valid_b_instance_by_fingerprint(
            fingerprint)
        if valid_b_instance:
            existing_main_path = valid_b_instance[0]
            # 如果将要复制的目标路径与B区主实例路径不同 (说明是同义但不同名的文件)
            if existing_main_path != str(b_local):
                # 获取评分 (分数越小越优)
                new_score = self._b_file_score(str(b_local))
                old_score = self._b_file_score(existing_main_path)

                # 如果新文件分数 >= B区主实例，说明它是劣质名或同级名，直接抛弃物理复制！
                if new_score >= old_score:
                    # logging.debug("[A->B前置拦截] B区已有更优命名，丢弃冗余复制: %s", b_local.name)
                    return
        # =======================================================

        if b_local.exists():
            existing_webdav_path = read_strm_webdav_path(b_local)
            if existing_webdav_path == webdav_path:
                self.db.upsert_b(
                    str(b_local),
                    webdav_path,
                    parent,
                    str(local),
                    fingerprint=fingerprint,
                    status="valid",
                )
                self.db.upsert_identity(
                    fingerprint=fingerprint,
                    webdav_path=webdav_path,
                    source_a_path=str(local),
                    current_b_path=str(b_local),
                )
                self.ensure_single_visible_instance(fingerprint, str(b_local))
                return

        if old_identity and current_b_path is None:
            if not self.admin_api.check_exists(webdav_path):
                logging.warning(
                    "[A->B跳过] WebDAV源文件已不存在，跳过复制并清理A区: %s",
                    webdav_path,
                )
                # 清理 A 区冗余文件
                a_local_path = str(local)
                if local.exists():
                    safe_remove_file(a_local_path)
                    logging.info("[A区清理] 删除冗余STRM: %s", a_local_path)
                self.db.delete_a_by_local(a_local_path)
                self.db.set_ghost_protection(
                    webdav_path,
                    self.config.behavior.ghost_protect_seconds,
                    reason="webdav_not_exists",
                )
                return

        self.copy_a_record_to_b(str(local), webdav_path, parent)

    def handle_a_deleted(self, local_path: str) -> None:
        """A 区删除处理（update 模式下）。
        OpenList 的 update 模式不会自动删除本地 STRM，
        所以监控到的删除都是真实的用户删除或程序清理。
        这里清理 A 区索引，并在后续主动刷新 / 延迟清理中修正 B 区冗余。
        """
        # 如果文件仍然存在，说明是修改操作而非真正的删除，跳过清理
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
        """复制 A→B，但会先检查指纹是否已存在。如果存在则跳过。
        返回：
            True: 复制成功
            None: 跳过（指纹已存在）
            False: 复制失败
        """
        if self.db.is_ghost_protected(webdav_path):
            return None
        fingerprint = make_strm_fingerprint(webdav_path)
        if self.db.b_fingerprint_exists(fingerprint):
            return None  # 会被统计为 skip_count
        return self.copy_a_record_to_b(
            a_local_path, webdav_path, parent_webdav_path)

    def copy_a_record_to_b(self, a_local_path: str,
                           webdav_path: str, parent: str) -> bool | None:
        try:
            # 1. 计算物理路径
            b_local = self.build_b_path_from_a(a_local_path, webdav_path)

            # 2. 血统校验（同步阶段）
            if not self._verify_b_path_lineage(
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
                    self.ensure_single_visible_instance(
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
            self.ensure_single_visible_instance(fingerprint, str(b_local))
            return True
        except Exception as e:
            logging.error("[A->B复制失败] DB错误: %s", e)
            safe_remove_file(b_local)
            return False

    def _find_related_subtitles(self, strm_path: str | Path) -> list[Path]:
        """查找与STRM文件同目录下的所有字幕文件"""
        strm = Path(strm_path)
        if not strm.parent.exists():
            return []

        subtitles = []
        for ext in SUBTITLE_EXTS:
            subtitles.extend(strm.parent.glob(f"*{ext}"))
            subtitles.extend(strm.parent.glob(f"*{ext.upper()}"))

        # 去重并排序
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
        """判断是否应该按电影处理"""
        # 1. 检查路径中的目录名
        media_type = detect_media_type_from_path(a_local_path)
        if media_type == "movie":
            return True
        if media_type == "anime":
            return False

        # 2. 检查webdav_path
        if webdav_path:
            media_type = detect_media_type_from_path(webdav_path)
            if media_type == "movie":
                return True
            if media_type == "anime":
                return False

        # 3. 默认：如果无法识别季集信息，可能是电影
        season, episode = _extract_season_episode(Path(a_local_path).name)
        if season is None or episode is None:
            # 检查是否是单文件目录
            parent = Path(a_local_path).parent
            strm_count = len(list(parent.glob("*.strm")))
            if strm_count <= 1:
                # 可能是电影或单集
                return True

        return False

    def process_subtitle_file(self, a_subtitle_path: str | Path) -> None:
        """
        独立处理单个字幕文件：从文件名提取季集信息，直接复制到B区标准目录。
        使用数据库记录已处理字幕，避免重复处理。
        """
        sub_file = Path(a_subtitle_path).resolve()
        if not sub_file.exists():
            return

        # 获取A区根目录
        a_root = self.get_a_root_for_path(sub_file)
        if a_root is None:
            return

        # 计算字幕指纹（基于文件路径和内容修改时间）
        stat = sub_file.stat()
        fingerprint = hashlib.sha256(
            f"{sub_file}:{stat.st_size}:{stat.st_mtime}".encode()
        ).hexdigest()

        # 检查数据库：已存在且目标文件仍在，跳过
        existing = self.db.get_subtitle_by_local(str(sub_file))
        logging.debug("[字幕数据库] 查询 %s: %s", sub_file, existing is not None)
        if existing:
            target_path = existing[2]
            logging.debug(
                "[字幕数据库] 目标路径: %s, 存在: %s",
                target_path,
                Path(target_path).exists())
            if Path(target_path).exists():
                logging.debug("[字幕跳过] 已处理且目标存在: %s", sub_file)
                return
            # 目标不存在，重新处理

        # ========== 关键修复：先判断媒体类型，再决定处理方式 ==========

        # 1. 优先基于路径判断媒体类型
        media_type = detect_media_type_from_path(sub_file)
        logging.debug("[字幕处理] 路径: %s, 媒体类型: %s", sub_file, media_type)

        # 2. 如果是电影，直接走电影模式
        if media_type == "movie":
            self._process_movie_subtitle(sub_file, a_root, fingerprint)
            return

        # 3. 检查同目录STRM文件，辅助判断是电影还是番剧
        parent_dir = sub_file.parent
        strm_files = list(parent_dir.glob("*.strm"))

        # 单STRM且无法提取季集 → 电影
        if len(strm_files) <= 1:
            if not strm_files:
                # 无STRM，按电影处理
                self._process_movie_subtitle(sub_file, a_root, fingerprint)
                return
            # 有1个STRM，检查是否能提取季集
            strm_season, strm_episode = _extract_season_episode(
                strm_files[0].name)
            if strm_season is None or strm_episode is None:
                # STRM无季集信息，是电影
                self._process_movie_subtitle(sub_file, a_root, fingerprint)
                return

        # ========== 番剧模式 ==========
        self._process_anime_subtitle(sub_file, a_root, fingerprint)

    def _process_movie_subtitle(
            self, sub_file: Path, a_root: Path, fingerprint: str) -> None:
        """处理电影字幕：复制到B区同目录，重命名为 电影名.forced.zho.简体.ass"""
        # 查找同目录下的STRM文件作为关联目标
        parent_dir = sub_file.parent
        strm_files = list(parent_dir.glob("*.strm"))

        if strm_files:
            # 使用STRM文件名（不含扩展名）作为基础
            movie_stem = strm_files[0].stem
        else:
            # 没有STRM，使用字幕文件名（去掉语言标识）
            movie_stem = sub_file.stem
            # 去掉常见的语言后缀
            for suffix in [".forced", ".zho", ".简体", ".繁体",
                           ".sc", ".tc", ".chs", ".cht", ".scjp"]:
                movie_stem = movie_stem.replace(suffix, "")
            movie_stem = movie_stem.rstrip(".")

        # 构建目标路径：保持同目录结构
        rel_parent = sub_file.relative_to(a_root).parent
        b_target_dir = self.b_root / rel_parent
        b_target_dir.mkdir(parents=True, exist_ok=True)

        # 语言信息
        lang_info = detect_subtitle_language(sub_file.name)
        if lang_info is None:
            new_name = f"{movie_stem}.forced.zho.中文{sub_file.suffix.lower()}"
        else:
            _code, _label, _priority = lang_info
            new_name = f"{movie_stem}.forced.{_code}.{_label}{
                sub_file.suffix.lower()}"

        target = b_target_dir / new_name

        # 如果目标已存在，更新数据库并跳过
        if target.exists():
            logging.debug("[字幕跳过] 目标文件已存在: %s", target)
            self.db.upsert_subtitle(
                local_path=str(sub_file),
                target_path=str(target),
                fingerprint=fingerprint,
                season=None,
                episode=None,
                lang_code=lang_info[0] if lang_info else None,
            )
            return

        try:
            shutil.copyfile(sub_file, target)
            logging.info("[字幕复制] 电影字幕: %s -> %s", sub_file, target)

            self.db.upsert_subtitle(
                local_path=str(sub_file),
                target_path=str(target),
                fingerprint=fingerprint,
                season=None,
                episode=None,
                lang_code=lang_info[0] if lang_info else None,
            )
        except Exception as e:
            logging.warning("[字幕复制失败] %s: %s", sub_file, e)

    def _process_anime_subtitle(
            self, sub_file: Path, a_root: Path, fingerprint: str) -> None:
        """处理番剧字幕：提取季集，复制到 Season XX/S01E01..."""
        # 提取季集信息
        season, episode = _extract_season_episode(sub_file.name)

        # 如果字幕本身没有季集信息，尝试从相邻的 STRM 文件提取
        if season is None or episode is None:
            parent_dir = sub_file.parent

            # 1. 先查同目录
            for strm_file in parent_dir.glob("*.strm"):
                season, episode = _extract_season_episode(strm_file.name)
                if season is not None and episode is not None:
                    logging.debug("[字幕关联] 从同目录STRM提取: %s -> S%02dE%02d",
                                  strm_file.name, season, episode)
                    break

            # 2. 如果当前在媒体根目录（不是Season目录），查子目录
            if (season is None or episode is None) and not re.match(
                    r"(?i)^season\s*\d+$", parent_dir.name):
                for sub_dir in parent_dir.iterdir():
                    if sub_dir.is_dir() and re.match(r"(?i)^season\s*\d+$", sub_dir.name):
                        for strm_file in sub_dir.glob("*.strm"):
                            season, episode = _extract_season_episode(
                                strm_file.name)
                            if season is not None and episode is not None:
                                logging.debug("[字幕关联] 从子目录STRM提取: %s -> S%02dE%02d",
                                              strm_file.name, season, episode)
                                break
                        if season is not None and episode is not None:
                            break

        # 如果还是无法提取，降级为电影处理
        if season is None or episode is None:
            logging.warning("[字幕处理] 无法提取番剧季集，降级为电影模式: %s", sub_file)
            self._process_movie_subtitle(sub_file, a_root, fingerprint)
            return

        # 构建标准路径
        rel = sub_file.relative_to(a_root)
        rel_parts = list(rel.parts)

        # 检查是否已有 Season 目录或中文季目录
        has_season_dir = False
        cn_season_index = -1
        for i, part in enumerate(rel_parts[:-1]):
            if re.match(r"(?i)^season\s*\d+$", part):
                has_season_dir = True
                break
            if re.match(r"^第[一二三四五六七八九十\d]+季$", part):
                cn_season_index = i

        if has_season_dir:
            b_target_dir = self.b_root / rel.parent
        elif cn_season_index >= 0:
            b_target_dir = self.b_root / \
                Path(*rel_parts[:cn_season_index]) / f"Season {season:02d}"
        else:
            if len(rel_parts) >= 2:
                b_target_dir = self.b_root / \
                    Path(*rel_parts[:-1]) / f"Season {season:02d}"
            else:
                b_target_dir = self.b_root / rel.parent

        b_target_dir.mkdir(parents=True, exist_ok=True)

        base_name = _build_standard_name(season, episode)
        lang_info = detect_subtitle_language(sub_file.name)

        if lang_info is None:
            new_name = f"{base_name}.forced.zho.中文{sub_file.suffix.lower()}"
        else:
            _code, _label, _priority = lang_info
            new_name = f"{base_name}.forced.{_code}.{_label}{
                sub_file.suffix.lower()}"

        target = b_target_dir / new_name

        # 如果目标已存在，更新数据库并跳过
        if target.exists():
            logging.debug("[字幕跳过] 目标文件已存在: %s", target)
            self.db.upsert_subtitle(
                local_path=str(sub_file),
                target_path=str(target),
                fingerprint=fingerprint,
                season=season,
                episode=episode,
                lang_code=lang_info[0] if lang_info else None,
            )
            return

        try:
            shutil.copyfile(sub_file, target)
            logging.info("[字幕复制] 番剧字幕: %s -> %s", sub_file, target)

            self.db.upsert_subtitle(
                local_path=str(sub_file),
                target_path=str(target),
                fingerprint=fingerprint,
                season=season,
                episode=episode,
                lang_code=lang_info[0] if lang_info else None,
            )
        except Exception as e:
            logging.warning("[字幕复制失败] %s: %s", sub_file, e)

    def _is_standard_media_name(self, name: str) -> bool:
        """
        兼容 tinyMediaManager 重命名器规范的标准媒体文件名检测。
        优先级：标准命名 > 非标准命名（无论文件名长短）
        """
        name = name.lower()

        # 1. S01E01 格式（带前导零或不带）
        #    例如：S01E21.strm, S1E1.strm, s01e01.mp4
        if re.search(r"s\d{1,2}e\d{1,2}", name):
            return True

        # 2. 1x01 格式（带前导零或不带）
        #    例如：1x01.strm, 1x1.mkv, 01x21.mp4
        if re.search(r"\d{1,2}x\d{1,2}", name):
            return True

        # 3. ShowTitle - S01E01 - EpisodeTitle 格式
        #    例如：Breaking Bad - S01E01 - Pilot.strm
        if re.search(r".*- s\d{1,2}e\d{1,2} -", name):
            return True

        # 4. Season 01/Episode 01 格式
        #    例如：Season 01/Episode 01.strm
        if re.search(r"season \d{1,2}/episode \d{1,2}", name):
            return True

        # 5. 纯数字集数格式（如 01.mp4, 21.mkv）
        #    注意：这种格式容易误判，建议谨慎启用
        # if re.search(r"^\d{1,3}\.\w+$", name):
        #     return True

        return False

    def _canonicalize_webdav_path(self, path: str) -> str:
        """规范化 WebDAV 路径"""
        if not path:
            return ""
        path = path.replace("\\", "/")
        path = urllib.parse.unquote(path)
        return posixpath.normpath(path)

    def _b_file_score(self, path: str) -> tuple:
        """
        分数越小越优先保留。
        规则：
        1. 标准命名（0） > 非标准命名（1）【绝对优先】
        2. 同标准/非标准内：B区路径与WebDAV路径从右向左匹配，匹配越少（差异越大）越优先（说明用户手动调整过）
        3. 差异相同时：路径短优先
        4. 文件名作为稳定排序键
        """
        p = Path(path)
        name = p.name.lower()

        is_standard = self._is_standard_media_name(name)

        # 1. 提取 B 区相对路径的所有部分（不含 B 区根目录）
        try:
            b_rel_parts = p.relative_to(self.b_root).parts
        except ValueError:
            b_rel_parts = p.parts

        # 2. 从数据库查该文件对应的 WebDAV 路径并提取其路径部分
        webdav_parts = []
        try:
            row = self.db.get_b_by_local_full(path)
            if row:
                webdav_path = row[1]  # webdav_path 是第2列（0-indexed）
                if webdav_path:
                    canonical_webdav = self._canonicalize_webdav_path(
                        webdav_path)
                    # 去掉前后的斜杠后拆分
                    webdav_parts = [
                        part for part in canonical_webdav.strip("/").split("/") if part]
        except Exception:
            pass

        # 3. 计算从右向左的匹配层级数
        # 如果查不到 WebDAV 信息，默认匹配数设为最大（代表未修改，优先级最低）
        if not webdav_parts:
            match_count = len(b_rel_parts)
        else:
            match_count = 0
            # 从右向左逐级对比（忽略大小写差异）
            for b_part, w_part in zip(
                    reversed(b_rel_parts), reversed(webdav_parts)):
                if b_part.lower() == w_part.lower():
                    match_count += 1
                else:
                    break

        # 4. 路径长度（作为次要参考，路径越短越优先）
        path_len = len(str(p))

        # 返回元组用于排序。match_count 越小，说明改动越多，分数越低，越优先保留。
        return (0 if is_standard else 1, match_count, path_len, name)

    def ensure_single_visible_instance(
        self,
        fingerprint: str,
        trigger_path: str,
    ) -> None:
        """
        按“质量”选主实例，而不是按触发顺序
        """

        # 拿到所有 valid 实例（不是只处理一个）
        all_instances = self.db.get_all_b_by_fingerprint(fingerprint)

        if not all_instances:
            return

        # 只处理 valid 状态
        valid_files = [row[0] for row in all_instances if row[5]
                       == "valid" and Path(row[0]).exists()]

        if not valid_files:
            return

        # 排序决定“主实例”
        valid_files.sort(key=self._b_file_score)

        keep = valid_files[0]
        duplicates = valid_files[1:]

        # 更新 DB（只标记非主的）
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
        """根据 webdav_path 从 A 区索引中查找对应的源 STRM 文件。
        使用数据库索引查询，避免全表扫描。
        """
        local_path = self.db.get_a_local_path_by_webdav(webdav_path)
        if local_path and Path(local_path).exists():
            return local_path
        return None

    def restore_b_file_from_a(
        self,
        b_local_path: str,
        webdav_path: str,
        parent_webdav_path: str,
        source_a_path: str | None,
    ) -> bool:
        """从 A 区恢复 B 区 STRM 文件。"""
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
                status="valid",
            )
            self.db.upsert_identity(
                fingerprint=fingerprint,
                webdav_path=webdav_path,
                source_a_path=source,
                current_b_path=str(target),
            )
            self.ensure_single_visible_instance(fingerprint, str(target))
            return True
        except sqlite3.Error as exc:
            logging.error("[B区修复失败] 数据库写入失败: %s", exc)
            return False
        except (TypeError, ValueError) as exc:
            logging.error("[B区修复失败] 指纹生成失败: %s", exc)
            return False

    def handle_b_created_or_modified(self, local_path: str) -> None:
        """处理 B 区 STRM 新增或修改。"""
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

            # =========================================================
            # 热重载跨区越界检测（血统校验）
            # =========================================================
            if not self._verify_b_path_lineage(str(local), webdav_path):
                logging.warning("[B区越界拦截] 拒绝非法复制，该路径无对应A区源: %s", local)
                self._restore_b_from_a_after_violation(
                    local, webdav_path, fingerprint)
                return

            parent = webdav_parent(webdav_path)

            # =========================================================
            # A区源文件存在性检查（考虑边界映射）
            # =========================================================
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
        self,
        local: Path,
        webdav_path: str,
        fingerprint: str,
    ) -> None:
        """血统校验失败后：删除越界文件，从 A 区恢复到正确位置，并临时暂停 B 区监控。"""
        local_path = str(local)

        # 1. 先强制删除越界的 B 区文件
        deleted = self._force_delete_and_verify(local)
        self.db.delete_b_by_local(local_path)
        if not deleted:
            logging.error("[B区越界恢复] 无法删除越界文件，跳过恢复: %s", local_path)
            return
        logging.info("[B区越界恢复] 已删除越界文件: %s", local_path)

        # 2. 查找 identity 表中该指纹的合法历史位置（优先使用）
        identity = self.db.get_identity_by_fingerprint(fingerprint)
        correct_b_path: str | None = None
        source_a_path: str | None = None

        if identity:
            # identity: (fingerprint, webdav_path, source_a_path, current_b_path, updated_at)
            historical_b_path = identity[3]  # current_b_path
            source_a_path = identity[2]  # source_a_path

            # 验证历史路径是否仍然有效
            if historical_b_path and historical_b_path != local_path:
                historical = Path(historical_b_path)
                if historical.exists():
                    # 验证内容是否匹配
                    existing_webdav = read_strm_webdav_path(historical_b_path)
                    if existing_webdav == webdav_path:
                        correct_b_path = historical_b_path
                        logging.debug(
                            "[B区越界恢复] 历史合法路径仍有效，直接使用: %s", correct_b_path)

        # 3. 如果历史路径不可用，尝试从 A 区源恢复（但保持历史路径位置）
        if not correct_b_path:
            # 尝试找到 A 区源文件
            if not source_a_path or not Path(source_a_path).exists():
                source_a_path = self.find_a_source_by_webdav(webdav_path)

            if source_a_path and Path(source_a_path).exists():
                # 如果有历史路径，恢复到历史路径位置
                if identity and identity[3]:
                    correct_b_path = identity[3]
                else:
                    # 获取源文件的 webdav_path
                    src_webdav = read_strm_webdav_path(source_a_path)
                    correct_b_path = str(
                        self.build_b_path_from_a(source_a_path, src_webdav))

                try:
                    correct_b = Path(correct_b_path)
                    correct_b.parent.mkdir(parents=True, exist_ok=True)

                    # 标记恢复操作，避免被误认为是用户操作
                    with self._restoring_lock:
                        self._restoring_markers.add(fingerprint)

                    try:
                        shutil.copyfile(source_a_path, correct_b)
                        logging.info(
                            "[B区越界恢复] 已从 A 区恢复到正确位置: %s -> %s",
                            source_a_path,
                            correct_b_path)
                    finally:
                        # 延迟移除标记，确保文件系统事件处理完成
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

        # 4. 更新数据库
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
                status="valid",
            )
            self.db.upsert_identity(
                fingerprint=fingerprint,
                webdav_path=webdav_path,
                source_a_path=final_source_a,
                current_b_path=correct_b_path,
            )
            self.ensure_single_visible_instance(fingerprint, correct_b_path)

    def _verify_a_source_exists(
            self, b_local_path: str, webdav_path: str, fingerprint: str) -> bool:
        """校验 B 区 strm 在 A 区是否有对应的源文件。"""
        # 1. 查 identity 表
        identity = self.db.get_identity_by_fingerprint(fingerprint)
        if identity and identity[2]:  # source_a_path
            if Path(identity[2]).exists():
                return True

        # 2. 通过 webdav_path 反查 A 区
        a_source = self.find_a_source_by_webdav(webdav_path)
        if a_source and Path(a_source).exists():
            return True

        # 3. 检查边界映射：如果血统校验已通过（有边界映射），放宽检查
        boundary = self.db.get_media_boundary_by_fingerprint(fingerprint)
        if boundary:
            # 有边界映射说明是合法的重命名/层级调整，允许通过
            logging.debug(
                "[A区源校验] 边界映射存在，放宽检查: %s (指纹: %s...)",
                b_local_path,
                fingerprint[:8],
            )
            return True

        # 4. 都找不到，说明 A 区没有这个源文件
        logging.debug(
            "[A区源校验] A区无对应源文件: %s (指向: %s)",
            b_local_path,
            webdav_path)
        return False

    def _force_delete_and_verify(self, path: Path) -> bool:
        """强制删除文件并验证结果，失败时尝试多种方法。"""
        path_str = str(path)

        if not path.exists():
            return True

        # 方法 1: 使用 safe_remove_file
        safe_remove_file(path)
        if not path.exists():
            logging.info("[B区越界恢复] 已删除越界文件: %s", path_str)
            return True

        # 方法 2: 直接使用 os.remove
        try:
            os.remove(path_str)
            if not path.exists():
                logging.info("[B区越界恢复] 已删除越界文件(os.remove): %s", path_str)
                return True
        except OSError as exc:
            logging.warning("[B区越界恢复] os.remove 失败 %s: %s", path_str, exc)

        # 方法 3: 尝试修改文件权限后删除
        try:
            import stat
            os.chmod(path_str, stat.S_IWRITE | stat.S_IREAD | stat.S_IRWXU)
            os.remove(path_str)
            if not path.exists():
                logging.info("[B区越界恢复] 已删除越界文件(chmod+remove): %s", path_str)
                return True
        except Exception as exc:
            logging.warning("[B区越界恢复] chmod+remove 失败 %s: %s", path_str, exc)

        # 最终验证
        if path.exists():
            logging.error("[B区越界恢复] 无法删除越界文件: %s", path_str)
            return False

        return True

    def _handle_unparseable_strm(self, local: Path, row: tuple | None) -> None:
        """处理无法解析的 STRM 文件：尝试恢复，失败则隔离。"""
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
        self,
        local: Path,
        webdav_path: str,
        parent: str,
        fingerprint: str,
        row: tuple,
    ) -> None:
        """处理已存在的 B 区文件记录。"""
        _, old_webdav_path, old_parent, source_a_path, old_fingerprint, status, _ = row
        if old_fingerprint == fingerprint or old_webdav_path == webdav_path:
            # 内容未变，刷新记录
            self._refresh_b_record(
                local,
                webdav_path,
                parent,
                source_a_path,
                fingerprint,
                status)
            return
        # 内容变化，尝试恢复
        if self.restore_b_file_from_a(
                str(local), old_webdav_path, old_parent, source_a_path):
            logging.warning("[B区修复] 内容被修改，已从A区恢复: %s", local)
            return
        # 恢复失败，隔离
        self._quarantine_modified_b_file(local)

    def _refresh_b_record(
        self,
        local: Path,
        webdav_path: str,
        parent: str,
        source_a_path: str | None,
        fingerprint: str,
        status: str | None,
    ) -> None:
        """刷新 B 区文件的数据库记录。"""
        normalized_status = status or "valid"
        self.db.upsert_b(
            str(local),
            webdav_path,
            parent,
            source_a_path,
            fingerprint=fingerprint,
            status=normalized_status,
        )
        self.db.upsert_identity(
            fingerprint=fingerprint,
            webdav_path=webdav_path,
            source_a_path=source_a_path,
            current_b_path=str(
                local) if normalized_status == "valid" else None,
        )
        if normalized_status == "valid":
            self.ensure_single_visible_instance(fingerprint, str(local))

    def _quarantine_modified_b_file(self, local: Path) -> None:
        """隔离内容被修改且无法恢复的 B 区文件。"""
        quarantined = quarantine_file(local, suffix=".invalid")
        if quarantined:
            self.db.move_b_record(str(local), str(quarantined))
            self.db.mark_b_instance_status(str(quarantined), "quarantined")
            logging.warning("[B区隔离] 内容身份变化且恢复失败: %s -> %s", local, quarantined)

    def _handle_new_b_file(
        self,
        local: Path,
        webdav_path: str,
        parent: str,
        fingerprint: str,
    ) -> None:
        """处理新出现的 B 区文件。"""
        identity = self.db.get_identity_by_fingerprint(fingerprint)
        source_a_path = identity[2] if identity else self.find_a_source_by_webdav(
            webdav_path)

        # 检查并记录边界映射
        self._maybe_record_boundary_mapping(local, webdav_path, fingerprint)

        self.db.upsert_b(
            str(local),
            webdav_path,
            parent,
            source_a_path,
            fingerprint=fingerprint,
            status="valid",
        )
        self.db.upsert_identity(
            fingerprint=fingerprint,
            webdav_path=webdav_path,
            source_a_path=source_a_path,
            current_b_path=str(local),
        )
        self.ensure_single_visible_instance(fingerprint, str(local))

    def _cloud_path_to_engine_paths(self, cloud_path: str) -> list[str]:
        """将真实云盘路径映射为引擎入口路径。

        例如：
          cloud_path: /天翼云盘家庭云30GB/番剧/[1998] 头文字D/Season 1
          -> engine_path: /测试a/番剧/[1998] 头文字D/Season 1
        """
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
        """删除/MOVE 成功后，通知 OpenList 更新索引。"""
        del webdav_path

        # 将真实云盘路径映射为引擎入口路径
        engine_paths = self._cloud_path_to_engine_paths(parent_webdav_path)
        if not engine_paths:
            logging.debug(
                "[OpenListAdmin]"
                " 无法映射引擎路径，跳过索引更新: %s",
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
        """处理 B 区 STRM 删除事件。"""
        local = Path(local_path).resolve()
        lock = self.get_path_lock(local)
        with lock:
            row = self.db.get_b_by_local_full(str(local))
            if not row:
                return

            _, webdav_path, _parent_webdav_path, _source_a_path, fingerprint, _status, _ = row

            # 检查是否是程序正在恢复的文件
            with self._restoring_lock:
                if fingerprint in self._restoring_markers:
                    logging.info("[B区删除] 检测到程序恢复操作，跳过追删: %s", local_path)
                    return

            # 检查是否还有其他同指纹文件存在（数据库层面）
            if self.db.has_other_b_instance(fingerprint, str(local)):
                logging.info("[B区删除联动] B区中仍存在同指纹文件，跳过WebDAV删除: %s", local_path)
                self.db.delete_b_by_local(str(local))
                return

            # 文件可能只是移动了位置（如从 Season 03 移动到上级目录），而不是真正被删除
            if fingerprint and self._check_fingerprint_exists_in_b(
                    fingerprint, exclude_path=str(local)):
                logging.info(
                    "[B区删除联动] B区文件系统中仍存在同指纹文件，跳过WebDAV删除: %s",
                    local_path)
                self.db.delete_b_by_local(str(local))
                return

            # 执行 WebDAV 源文件删除/MOVE（带 ghost 保护）
            if webdav_path:
                self._execute_webdav_deletion(webdav_path, _parent_webdav_path)
                # ===== 更新模式触发钩子也不会删除strm,所以删除 A 区文件需要程序来做 =====
                self._delete_a_file_by_webdav(webdav_path)
                # ========================================================================
            self.db.delete_b_by_local(str(local))
            if fingerprint:
                self.refresh_identity_current_b_path(fingerprint)

    def _check_fingerprint_exists_in_b(
            self, fingerprint: str, exclude_path: str | None = None) -> bool:
        """检查 B 区文件系统中是否还有指定指纹的文件存在。"""
        # 首先检查数据库中其他记录对应的文件是否存在
        b_instances = self.db.get_b_instances_by_fingerprint(fingerprint)
        for instance in b_instances:
            instance_path = instance[0]
            if exclude_path and instance_path == exclude_path:
                continue
            if Path(instance_path).exists():
                return True

        # 如果数据库中没有找到，扫描 B 区文件系统
        # 这是一个兜底检查，防止 watchdog 事件顺序问题
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
        """执行 WebDAV 源文件删除/MOVE 操作。"""
        lock = self.get_path_lock(webdav_path)
        with lock, self._dav_write_lock:
            ok = self._perform_webdav_action(webdav_path)
            if ok:
                self.request_openlist_index_update(
                    webdav_path, parent_webdav_path)
                self.db.set_ghost_protection(
                    webdav_path,
                    self.config.behavior.ghost_protect_seconds,
                    reason="b_deleted",
                )
                logging.info("[B区删除联动] 已处理WebDAV: %s", webdav_path)
            else:
                logging.warning("[B区删除联动] WebDAV处理失败: %s", webdav_path)
            return ok

    def _delete_a_file_by_webdav(self, webdav_path: str) -> None:
        """根据 WebDAV 路径找到并删除对应的 A 区文件"""
        # 从数据库查找 A 区路径
        a_record = self.db.get_a_by_webdav(webdav_path)
        if a_record:
            a_path = a_record[0]  # local_path
            if Path(a_path).exists():
                safe_remove_file(a_path)
                logging.info("[A区删除] B区删除联动，清理A区: %s", a_path)
            self.db.delete_a_by_local(a_path)

    # ---------- 下方是修复真实的 WebDAV 请求动作 ----------

    def _perform_webdav_action(self, webdav_path: str) -> bool:
        """执行具体的 WebDAV 删除或移动操作。"""
        cloud_path = webdav_path
        logging.info("[WebDAV操作] 真实云盘路径 = %s", cloud_path)

        if self.config.behavior.action == "MOVE":
            trash_path = self._build_trash_path(cloud_path)
            logging.info("[WebDAV回收站] 目标路径 = %s", trash_path)

            if not trash_path:
                logging.error("[B区删除联动] 无法构建回收站路径: %s", cloud_path)
                return False

            if not self._ensure_trash_dirs(trash_path):
                logging.error("[B区删除联动] 创建回收站目录失败: %s", trash_path)
                return False

            ok = self.admin_api.move(cloud_path, trash_path)
            if not ok:
                logging.error(
                    "[B区删除联动] MOVE失败: %s -> %s",
                    cloud_path,
                    trash_path)
            return ok

        ok = self.admin_api.remove(cloud_path)
        if not ok:
            logging.error("[B区删除联动] DELETE失败: %s", cloud_path)
        return ok

    def _webdav_to_cloud_path(self, webdav_path: str) -> str:
        # 该方法已废弃，无需转换
        return webdav_path

    def _build_trash_path(self, cloud_path: str) -> str | None:
        """构建回收站路径。"""
        # cloud_path: /天翼云盘家庭云30GB/番剧/[1998] 头文字D/Season 1/S01E01.mkv
        # trash_dir_name: strm_回收站_测试
        parts = cloud_path.strip("/").split("/")
        if len(parts) < 2:
            return None
        # 第一级目录 + trash_dir_name + 剩余路径
        first_dir = parts[0]  # 天翼云盘家庭云30GB
        remaining = "/".join(parts[1:])  # 番剧/[1998] 头文字D/Season 1/S01E01.mkv
        trash_dir_name = self.config.behavior.trash_dir_name
        return f"/{first_dir}/{trash_dir_name}/{remaining}"

    def _ensure_trash_dirs(self, trash_path: str) -> bool:
        """递归创建回收站目录结构。"""
        # 确保已经登录
        if not self.admin_api.token and not self.admin_api.login():
            logging.error("[回收站] Admin API 登录失败")
            return False
        # 逐级创建目录
        parts = trash_path.strip("/").split("/")
        # 从根开始逐级创建
        current = ""
        for i, part in enumerate(parts[:-1]):  # 去掉文件名
            if i == 0:
                current = f"/{part}"
            else:
                current = f"{current}/{part}"
            # 创建目录
            if not self.admin_api.mkdir(current):
                logging.warning("[回收站] 创建目录失败或已存在: %s", current)
        return True

    def _dir_exists(self, path: str) -> bool:
        """检查目录是否存在。"""
        # 使用 fs/list 检查
        try:
            result = self.admin_api.list_directory(path)
            return result is not None and not isinstance(result, str)
        except Exception:
            return False

    def _cleanup_after_b_deletion(
        self,
        local_path: str,
        webdav_path: str,
        parent_webdav_path: str,
        fingerprint: str | None,
    ) -> None:
        """B 区删除后的数据库和状态清理。"""
        if fingerprint:
            # 检查该 fingerprint 对应的 B 区文件是否仍然存在于文件系统中
            # 如果存在，说明只是路径发生了合法变化（血缘校验通过），
            # 不应触发源文件删除流程
            b_instances = self.db.get_b_instances_by_fingerprint(fingerprint)
            any_b_instance_exists = False
            for instance in b_instances:
                instance_path = instance[0]
                if Path(instance_path).exists():
                    any_b_instance_exists = True
                    break

            if any_b_instance_exists:
                logging.info(
                    "[B区删除联动] B区中仍存在同指纹文件，跳过源文件删除检测: %s (指纹: %s)",
                    local_path,
                    fingerprint,
                )
                # 只清理当前这条记录，不清除所有同指纹实例
                self.db.delete_b_by_local(local_path)
            else:
                # B 区中真的不存在该 fingerprint 的文件了，才触发源文件删除
                self.cleanup_all_b_instances_for_deleted_source(
                    fingerprint=fingerprint,
                    deleted_webdav_path=webdav_path,
                )
                self.db.clear_identity_b_path_by_fingerprint(fingerprint)
        else:
            self.db.delete_b_by_local(local_path)
        self.trigger_delayed_cleanup(parent_webdav_path)
        self.cleanup_local_empty_dirs()

    def handle_b_moved(self, src_path: str, dest_path: str) -> None:
        """处理 B 区文件移动或重命名。"""
        with self._b_move_lock:
            # 防御 1: 如果目标文件已经被之前的"血统拦截"删除了，直接结束
            if not Path(dest_path).exists():
                return

            src_row = self.db.get_b_by_local_full(src_path)
            if not src_row:
                if Path(dest_path).exists(
                ) and dest_path.lower().endswith(".strm"):
                    self.handle_b_created_or_modified(dest_path)
                return

            _, webdav_path, _parent_webdav_path, _source_a_path, fingerprint, status, _ = src_row

            # 检查是否是程序正在恢复的文件
            with self._restoring_lock:
                if fingerprint in self._restoring_markers:
                    logging.info(
                        "[B区移动] 检测到程序恢复操作，跳过处理: %s -> %s",
                        src_path,
                        dest_path)
                    return

            # 防御 2: read_strm_webdav_path 此时可能返回 None
            dest_webdav_path = read_strm_webdav_path(dest_path)
            if not dest_webdav_path:
                return

            if dest_webdav_path == webdav_path:
                # 即使路径变了，只要内容指纹没变，且经过了 handle_b_created_or_modified 里的血统校验
                # 我们就更新数据库记录
                moved = self.db.move_b_record(src_path, dest_path)
                if moved:
                    if status:
                        self.db.mark_b_instance_status(dest_path, status)
                    if fingerprint and status == "valid":
                        self.db.update_identity_b_path(fingerprint, dest_path)
                    logging.info("[B区移动] 已更新路径: %s -> %s", src_path, dest_path)
                return

            # 移动后内容变化，需要校验目标位置是否合法
            self.db.delete_b_by_local(src_path)
            if fingerprint:
                self.refresh_identity_current_b_path(fingerprint)
            if Path(dest_path).exists() and dest_path.lower().endswith(".strm"):
                # 直接调用 handle_b_created_or_modified，它会处理血统校验和恢复
                self.handle_b_created_or_modified(dest_path)
            logging.info("[B区移动] 内容不一致，已按新文件处理: %s -> %s", src_path, dest_path)

    def cleanup_all_b_instances_for_deleted_source(
            self, fingerprint: str, deleted_webdav_path: str) -> None:
        """删除 WebDAV 源文件成功后，清理 B 区中所有同 fingerprint 实例和冗余后缀文件。"""

        # 1. 清理同 fingerprint 的所有数据库记录
        for instance in self.db.get_b_instances_by_fingerprint(fingerprint):
            instance_path = instance[0]

            # 保险检查：如果文件仍然存在于磁盘上，说明只是路径发生了合法变化
            # （如层级调整、改名等血缘校验通过的情况），不应删除
            if Path(instance_path).exists():
                logging.info(
                    "[B区删除联动] 跳过清理，文件仍存在于磁盘: %s (指纹: %s)",
                    instance_path,
                    fingerprint,
                )
                continue

            safe_remove_file(instance_path)
            self.db.delete_b_by_local(instance_path)
            logging.info(
                "[B区删除联动] 已清理同源实例: %s (源: %s)",
                instance_path,
                deleted_webdav_path,
            )

        # 2. 合并清理：一次性处理所有冗余后缀文件
        b_root = Path(self.config.paths.b_root)
        if not b_root.exists():
            return

        # 定义需要清理的后缀关键词（会匹配 .keyword 和 .keyword.*）
        redundant_keywords = ["duplicate", "quarantined", "invalid"]

        for keyword in redundant_keywords:
            for file_path in b_root.rglob(f"*.{keyword}"):
                try:
                    safe_remove_file(file_path)
                    self.db.delete_b_by_local(str(file_path))
                    logging.info("[冗余清理] 已删除冗余文件: %s", file_path)
                except OSError as e:
                    logging.warning("[冗余清理] 删除冗余文件失败: %s (%s)", file_path, e)

    def trigger_delayed_cleanup(self, folder_path: str) -> None:
        """为指定 WebDAV 目录安排延迟冗余清理。
        1. 先等待一段时间，让 openlist 的同步重建尽量完成
        2. 到期后先执行目录刷新（预检查）
        3. 再等待一段确认时间
        4. 最后才真正清理
        """
        # 将真实云盘路径映射为引擎入口路径
        engine_paths = self._cloud_path_to_engine_paths(folder_path)
        if engine_paths:
            # 使用第一个匹配的引擎路径
            normalized_folder = engine_paths[0].rstrip("/") or "/"
        else:
            normalized_folder = folder_path.rstrip("/") or "/"

        # 直接内置默认值，不再依赖外部配置
        delay = 10
        with self.cleanup_lock:
            old_timer = self.pending_cleanups.pop(normalized_folder, None)
            if old_timer:
                old_timer.cancel()

            timer = threading.Timer(
                delay,
                self.execute_targeted_cleanup_precheck,
                args=(normalized_folder,),
            )
            timer.daemon = True
            self.pending_cleanups[normalized_folder] = timer
            timer.start()
            logging.debug(
                "[延迟清理] 已安排预检查 %s 秒后执行: %s",
                delay,
                normalized_folder)

    def execute_targeted_cleanup(self, folder_path: str) -> None:
        """执行指定 WebDAV 目录下的 B 区冗余清理。

        这里是最终清理阶段：
        - 不再负责刷新目录
        - 只做真正的冗余清理与本地空目录清理
        """
        normalized_folder = folder_path.rstrip("/") or "/"
        with self.cleanup_lock:
            self.pending_cleanups.pop(normalized_folder, None)

        logging.info("[延迟清理-确认] 开始最终清理: %s", normalized_folder)
        self.cleanup_b_zombies_under_folder(normalized_folder)
        self.cleanup_local_empty_dirs()
        logging.info("[延迟清理-确认] 完成: %s", normalized_folder)

    def execute_targeted_cleanup_precheck(self, folder_path: str) -> None:
        """延迟清理的预检查阶段。

        作用：
        1. 先主动刷新一次 openlist 目录，尽量让 A 区重建完成
        2. 再安排一次二次确认
        3. 二次确认到期后才执行真正清理
        """
        normalized_folder = folder_path.rstrip("/") or "/"
        with self.cleanup_lock:
            self.pending_cleanups.pop(normalized_folder, None)

        logging.info("[延迟清理-预检查] 开始刷新目录: %s", normalized_folder)

        # 确保已登录
        if not self.admin_api.token and not self.admin_api.login():
            logging.warning("[延迟清理-预检查] 登录失败，无法主动刷新目录: %s", normalized_folder)
            return

        # 主动触发 openlist 目录刷新
        res = self.admin_api.list_directory(normalized_folder)
        if not (isinstance(res, dict) and res.get("code") in (0, 200)):
            logging.warning("[延迟清理-预检查] 目录 API 刷新失败: %s", normalized_folder)
            return

        # 直接内置默认值，不再依赖外部配置
        confirm_delay = 5
        with self.cleanup_lock:
            old_timer = self.pending_cleanups.pop(normalized_folder, None)
            if old_timer:
                old_timer.cancel()

            timer = threading.Timer(
                confirm_delay,
                self.execute_targeted_cleanup,
                args=(normalized_folder,),
            )
            timer.daemon = True
            self.pending_cleanups[normalized_folder] = timer
            timer.start()
            logging.debug(
                "[延迟清理-预检查] 已安排二次确认 %s 秒后执行: %s",
                confirm_delay,
                normalized_folder,
            )

    def cleanup_b_zombies_under_folder(self, folder_path: str) -> None:
        """清理指定 WebDAV 目录下已经不存在的 B 区 STRM 记录和本地文件。

        三层校验：
        1. 幽灵保护检查
        2. A 区源文件存在性检查 — A 区源文件不存在则迁移到 C 区
        3. WebDAV 存在性检查 — WebDAV 上文件不存在则直接删除

        安全策略：
        - 只清理同时被"程序监控"和"STRM引擎监控"的路径
        - 如果路径只在 refresh_paths 中但不在 strm_engine_paths 中，跳过清理
        - 如果 STRM 引擎路径不可访问，跳过清理（避免网络中断导致误删）
        """
        normalized_folder = folder_path.rstrip("/") or "/"
        if not self.is_valid_refresh_root(normalized_folder):
            logging.info(
                "[B区冗余清理跳过] %s 不在有效的 STRM 引擎监控范围内，跳过清理",
                normalized_folder,
            )
            return
        if self.config.strm_engine_paths:
            engine_path = self._find_matching_engine_path(normalized_folder)
            if engine_path:
                result = self.admin_api.list_contents(engine_path)
                if isinstance(result, str):
                    logging.warning(
                        "[B区冗余清理跳过] STRM引擎路径 %s (对应 %s) 当前不可访问,跳过清理以避免网络中断导致误删",
                        engine_path,
                        normalized_folder,
                    )
                    return
                elif isinstance(result, dict):
                    if not (result.get("code") in (0, 200)
                            or "folders" in result or "files" in result):
                        logging.warning(
                            "[B区冗余清理跳过] STRM引擎路径 %s (对应 %s) 返回异常,跳过清理以避免网络中断导致误删",
                            engine_path,
                            normalized_folder,
                        )
                        return
                else:
                    logging.warning(
                        "[B区冗余清理跳过] STRM引擎路径 %s (对应 %s) 返回未知类型 %s,跳过清理以避免网络中断导致误删",
                        engine_path,
                        normalized_folder,
                        type(result).__name__,
                    )
                    return
        rows = self.db.get_b_under_root(normalized_folder)
        if not rows:
            return
        removed_count = 0
        migrated_count = 0
        for local_path, webdav_path, _parent, _source_a_path, _updated_at in rows:
            if self.db.is_ghost_protected(webdav_path):
                continue

            # 第 2 层：A 区源文件存在性检查
            source_exists = False
            if _source_a_path and Path(_source_a_path).exists():
                source_exists = True
            else:
                alt_source = self.find_a_source_by_webdav(webdav_path)
                if alt_source:
                    source_exists = True

            if not source_exists:
                # A 区源文件不存在，检查 WebDAV 上是否仍存在
                # 如果 WebDAV 上存在，说明 A 区只是暂时不可用（如 OpenList 同步模式中），
                # 应该跳过清理，等待 A 区重建
                if self.admin_api.check_exists(webdav_path):
                    logging.debug(
                        "[B区冗余清理跳过] A区源文件暂不可用但WebDAV存在，跳过清理: %s",
                        webdav_path)
                    continue
                # A 区源文件不存在且 WebDAV 上也不存在，迁移到 C 区

            if not source_exists:
                # A 区源文件不存在，迁移到 C 区
                local = Path(local_path)
                if not local.exists():
                    self.db.delete_b_by_local(local_path)
                    try:
                        fingerprint = make_strm_fingerprint(webdav_path)
                        self.refresh_identity_current_b_path(fingerprint)
                    except (TypeError, ValueError):
                        pass
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
                            "[B区冗余清理→C区] 迁移失败: %s -> %s (%s)", local, target, exc)
                        safe_remove_file(local_path)
                        self.db.delete_b_by_local(local_path)
                        try:
                            fingerprint = make_strm_fingerprint(webdav_path)
                            self.refresh_identity_current_b_path(fingerprint)
                        except (TypeError, ValueError):
                            pass
                        continue

                self.db.upsert_c(
                    str(target),
                    webdav_path,
                    local_path,
                    normalized_folder,
                )
                self.db.delete_b_by_local(local_path)
                try:
                    fingerprint = make_strm_fingerprint(webdav_path)
                    self.refresh_identity_current_b_path(fingerprint)
                except (TypeError, ValueError):
                    pass
                migrated_count += 1
                logging.info(
                    "[B区冗余清理→C区] A区源文件已不存在，迁移至C区: %s -> %s",
                    local_path,
                    webdav_path)
                continue

            # 第 3 层：WebDAV 存在性检查
            if self.admin_api.check_exists(webdav_path):
                continue

            safe_remove_file(local_path)
            self.db.delete_b_by_local(local_path)
            try:
                fingerprint = make_strm_fingerprint(webdav_path)
                self.refresh_identity_current_b_path(fingerprint)
            except (TypeError, ValueError):
                pass
            removed_count += 1
            logging.info(
                "[B区冗余清理] 已移除失效STRM: %s -> %s",
                local_path,
                webdav_path)

        if migrated_count:
            logging.warning(
                "[B区冗余清理→C区] %s 下共迁移 %s 个 A 区源已删除的 STRM", normalized_folder, migrated_count
            )
        if removed_count:
            logging.warning(
                "[B区冗余清理] %s 下共清理 %s 个失效 STRM",
                normalized_folder,
                removed_count)

    def refresh_identity_current_b_path(self, fingerprint: str) -> None:
        """根据当前 B 区 valid 实例刷新 strm_identity.current_b_path。

        如果没有 valid 实例，则清空 current_b_path。
        """
        row = self.db.get_valid_b_instance_by_fingerprint(fingerprint)
        if row:
            self.db.update_identity_b_path(fingerprint, row[0])
        else:
            self.db.clear_identity_b_path_by_fingerprint(fingerprint)

    def handle_c_deleted(self, local_path: str) -> None:
        """C 区文件被人工删除时，只清理 C 区数据库记录，不回写 WebDAV。"""
        self.db.delete_c_by_local(local_path)
        logging.info("[C区] 已清理幽灵记录: %s", local_path)

    def move_b_root_to_c(self, webdav_root: str) -> None:
        """兼容旧版命名：将某个 WebDAV 根路径下的 B 区 STRM 迁移到 C 区。"""
        self.migrate_b_under_root_to_c(webdav_root)

    def migrate_b_under_root_to_c(self, webdav_root: str) -> None:
        """将某个 WebDAV 根路径下的 B 区 STRM 迁移到 C 区。

        安全策略：
        - 只迁移同时被"程序监控"和"STRM引擎监控"的路径
        - 如果路径只在 refresh_paths 中但不在 strm_engine_paths 中，不迁移
        - 如果 STRM 引擎路径不可访问，不迁移
        """
        if not self.is_valid_refresh_root(webdav_root):
            logging.info(
                "[幽灵迁移跳过] %s 不在有效的 STRM 引擎监控范围内，不迁移",
                webdav_root,
            )
            return
        if self.config.strm_engine_paths:
            engine_path = self._find_matching_engine_path(webdav_root)
            if engine_path:
                result = self.admin_api.list_contents(engine_path)
                if isinstance(result, str):
                    logging.warning(
                        "[幽灵迁移跳过] STRM引擎路径 %s 当前不可访问,不迁移以避免网络中断导致误操作",
                        engine_path,
                    )
                    return
                elif isinstance(result, dict):
                    if not (result.get("code") in (0, 200)
                            or "folders" in result or "files" in result):
                        logging.warning(
                            "[幽灵迁移跳过] STRM引擎路径 %s 返回异常,不迁移以避免网络中断导致误操作",
                            engine_path,
                        )
                        return
                else:
                    logging.warning(
                        "[幽灵迁移跳过] STRM引擎路径 %s 返回未知类型 %s,不迁移以避免网络中断导致误操作",
                        engine_path,
                        type(result).__name__,
                    )
                    return
        rows = self.db.get_b_under_root(webdav_root)
        if not rows:
            return
        for local_path, webdav_path, _parent, _source_a_path, _updated_at in rows:
            local = Path(local_path)
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
                        "[幽灵迁移] 文件迁移失败: %s -> %s (%s)", local, target, exc)
                    continue
            self.db.upsert_c(
                str(target),
                webdav_path,
                local_path,
                webdav_root,
            )
            try:
                fingerprint = make_strm_fingerprint(webdav_path)
                self.refresh_identity_current_b_path(fingerprint)
            except (TypeError, ValueError):
                pass
        self.db.delete_b_under_root(webdav_root)
        self.cleanup_local_empty_dirs()
        logging.warning("[幽灵迁移] 已迁移失效根路径下的 B 区记录: %s", webdav_root)

    def validate_strm_storages(self) -> dict:
        """验证本地 STRM 配置与 OpenList API 的一致性。

        返回验证结果字典，包含有效的存储列表。
        """
        # OpenListAdminClient 已在 webdav_client.py 中定义，通过动态加载可用
        admin_client = OpenListAdminClient(
            host=self.config.webdav.host,
            user=self.config.webdav.user,
            password=self.config.webdav.password,
            totp_secret=self.config.webdav.totp_secret,
        )
        if not admin_client.login():
            logging.error("[STRM存储验证] Admin API 登录失败")
            return {"error": "login_failed", "valid": []}
        manager = StrmStorageManager(admin_client)
        result = manager.validate_against_local_paths(
            self.config.strm_engine_paths)
        # 记录验证结果
        if result["missing_in_api"]:
            logging.warning(
                "[STRM存储验证] 本地配置但 API 中不存在的路径: %s",
                result["missing_in_api"])
        if result["extra_in_api"]:
            extra_paths = [s.mount_path for s in result["extra_in_api"]]
            logging.info("[STRM存储验证] API 中存在但本地未配置的路径: %s", extra_paths)
        if result["non_working"]:
            for s in result["non_working"]:
                logging.warning(
                    "[STRM存储验证] 状态异常的存储: %s (status=%s)",
                    s.mount_path,
                    s.status,
                )
        if result["non_sync_mode"]:
            for s in result["non_sync_mode"]:
                logging.warning(
                    "[STRM存储验证] 非更新模式的存储: %s (mode=%s, 需要改为更新模式)",
                    s.mount_path,
                    s.save_local_mode,
                )
        valid_paths = [s.mount_path for s in result["valid"]]
        logging.info("[STRM存储验证] 有效的 STRM 存储: %s", valid_paths)
        return result

    def build_strm_access_path(self, mount_path: str,
                               sub_path: str = "") -> str:
        """构建 STRM 引擎入口路径（用于 API 访问如 fs list）"""
        mapping = self.config.strm_storage_map.get(mount_path)
        if mapping:
            return mapping.get_engine_entry_path(sub_path)
        if sub_path:
            return f"{mount_path.rstrip('/')}/{sub_path.lstrip('/')}"
        return mount_path

    def build_cloud_path(self, mount_path: str, sub_path: str = "") -> str:
        """构建实际云盘路径（用于删除/移动真实文件）"""
        mapping = self.config.strm_storage_map.get(mount_path)
        if mapping:
            return mapping.get_cloud_path(sub_path)
        if sub_path:
            return f"{mount_path.rstrip('/')}/{sub_path.lstrip('/')}"
        return mount_path

    def build_local_a_path(self, mount_path: str, sub_path: str = "") -> str:
        """构建本地 A 区路径"""
        mapping = self.config.strm_storage_map.get(mount_path)
        if mapping:
            return mapping.get_local_path(sub_path)
        return os.path.join(self.config.local.a_dir, sub_path.lstrip("/\\"))

    def _maybe_record_boundary_mapping(
        self,
        local: Path,
        webdav_path: str,
        fingerprint: str,
    ) -> None:
        """根据当前文件位置和 webdav_path，记录可能的边界映射。"""
        try:
            # 从 B 区路径提取媒体文件夹名
            rel_to_b = local.relative_to(self.b_root)
            if len(rel_to_b.parts) < 3:  # 至少需要: a_root_name/媒体名/文件
                return

            # 提取媒体文件夹名（跳过 a_root_name）
            media_folder_name = rel_to_b.parts[1]

            # 从 webdav_path 提取云端媒体名
            webdav_parts = webdav_path.strip("/").split("/")
            if len(webdav_parts) < 2:
                return

            # 找到引擎入口路径对应的 source_path
            cloud_show_name = None
            for config in getattr(self, "engine_configs", []):
                for sp in config.get("source_paths", []):
                    if webdav_path.startswith(sp.rstrip("/") + "/"):
                        # 提取相对路径的第一部分作为媒体名
                        rel_cloud = webdav_path[len(
                            sp.rstrip("/")):].lstrip("/")
                        cloud_show_name = rel_cloud.split(
                            "/")[0] if rel_cloud else None
                        break
                if cloud_show_name:
                    break

            if not cloud_show_name:
                return

            # 如果名称不同，记录映射
            if media_folder_name != cloud_show_name:
                existing = self.db.get_media_boundary_by_fingerprint(
                    fingerprint)
                if not existing:
                    self.db.upsert_media_boundary(
                        fingerprint=fingerprint,
                        source_media_name=cloud_show_name,
                        current_media_name=media_folder_name,
                        engine_entry_path=str(self.b_root / rel_to_b.parts[0]),
                    )
                    logging.info(
                        "[边界映射] 自动记录: %s -> %s (指纹: %s...)",
                        cloud_show_name,
                        media_folder_name,
                        fingerprint[:8],
                    )
        except (ValueError, IndexError):
            pass

    @staticmethod
    def _extract_season_from_webdav_path(webdav_path: str) -> int | None:
        """从 WebDAV 路径中提取季信息。

        例如：/d/测试本地/番剧/fanju4/第二季/第01集.mp4 -> 2
               /d/测试本地/番剧/fanju4/Season 2/第01集.mp4 -> 2
               /d/测试本地/番剧/fanju4/S02/第01集.mp4 -> 2
        """
        if not webdav_path:
            return None

        # 解析路径中的目录部分（去掉文件名）
        parsed = urllib.parse.urlparse(webdav_path)
        path = parsed.path

        # 分割路径
        parts = path.strip("/").split("/")

        # 从右向左遍历目录部分，寻找季信息
        for part in reversed(parts[:-1]):  # 排除最后一个（文件名）
            part_lower = part.lower()

            # 匹配 "Season XX" 或 "SeasonX" 格式
            season_match = re.match(r"^season\s*(\d{1,2})$", part_lower)
            if season_match:
                return int(season_match.group(1))

            # 匹配 "SXX" 格式（如 S02, s2）
            s_match = re.match(r"^s(\d{1,2})$", part_lower)
            if s_match:
                return int(s_match.group(1))

            cn_match = re.match(r"^第([一二三四五六七八九十\d]+)季$", part_lower)
            if cn_match:
                from media_renamer import _cn_to_int as _cn_to_int_func
                return _cn_to_int_func(cn_match.group(1))

        return None

    def cleanup_a_deleted_on_cloud(self, engine_path: str) -> None:
        """扫描 A 区，删除云端已不存在的 STRM 文件（update 模式核心逻辑）"""
        # 从配置中找到对应的 A 区根目录
        a_root = None
        for entry_path, mapping in self.config.strm_storage_map.items():
            if engine_path == entry_path or engine_path.startswith(
                    entry_path + "/"):
                a_root = Path(mapping.local_path)
                # 拼接 paths[0] 的最后一级目录
                if mapping.paths:
                    last_dir = mapping.paths[0].rstrip("/").split("/")[-1]
                    a_root = a_root / last_dir
                break

        if not a_root or not a_root.exists():
            return

        for strm_file in a_root.rglob("*.strm"):
            webdav_path = read_strm_webdav_path(str(strm_file))
            if not webdav_path:
                continue

            if not self.admin_api.check_exists(webdav_path):
                self._safe_delete_a_file(str(strm_file), webdav_path)

    def _safe_delete_a_file(self, local_path: str, webdav_path: str) -> None:
        """安全删除 A 区文件，同步清理数据库和 B 区"""
        local = Path(local_path)

        # 1. 删除 A 区物理文件
        if local.exists():
            safe_remove_file(local)
            logging.info("[A区删除] 云端已删除，清理本地: %s", local_path)

        # 2. 清理 A 区数据库记录
        self.db.delete_a_by_local(local_path)

        # 3. 触发 B 区延迟清理
        parent = webdav_parent(webdav_path)
        self.trigger_delayed_cleanup(parent)

        # 4. 清理空目录
        self.cleanup_local_empty_dirs()
