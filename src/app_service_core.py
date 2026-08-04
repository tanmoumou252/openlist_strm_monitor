# autopep8: off
# isort: off

"""App service core implementation."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext as _nullcontext
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from config import AppConfig, ABMapping, LINEAGE_VERSION, mapping_version, normalize_local_root
from database import Database, ARecord, BRecord
from domain.media.subtitle_handler import SubtitleHandler
from domain.sync.sync_service import SyncService
from area_watchers import AAreaEventHandler, BAreaEventHandler, CAreaEventHandler
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

# 慢操作阈值（秒），超过此阈值输出 WARNING 以便定位卡顿来源
B_SCAN_SLOW_OPERATION_SECONDS = 3.0

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
        # 注意：list 接口返回的 addition 是精简版，不含 SaveStrmLocalPath / SaveLocalMode，
        # 必须通过 get_strm_storages_full_info() 对每个 STRM 存储调用 get 接口拿完整 addition。
        content = self.client.get_strm_storages_full_info()
        if not content:
            return []
        result: list[StrmStorageInfo] = []
        for storage in content:
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
    """应用核心服务。

    锁获取顺序（必须严格遵守，避免死锁）：
      1. _path_locks_lock（获取 path_lock 时）
      2. _path_locks[path]（单个路径操作；on_moved 取双锁时按 key 全序）
      3. _dav_write_lock（WebDAV 写操作）
      4. _cleanup_lock（延迟清理定时器管理）
      5. _restoring_lock（恢复标记 / 引擎内部删除标记）
      6. _lineage_log_lock（日志记录）
    规则：只能按编号从小到大获取，释放时反向；禁止同时持有非相邻的锁。
    （注：原 _b_file_lock 已移除——B 区移动/修复改由 get_path_lock 按路径串行化。）
    """

    def __init__(self, config: AppConfig, db: Database,
                 admin_api: OpenListAdminClient) -> None:
        self.config = config
        self.db = db
        self.admin_api = admin_api
        self._observers: list[object] = []
        self.observer: Any = None
        self._running = False
        self.refresh_service = RefreshService(self)
        self._dav_write_lock = threading.Lock()
        self._path_locks_lock = threading.Lock()
        self._path_locks: dict[str, threading.Lock] = {}
        self._cleanup_lock = threading.Lock()
        self._pending_cleanups: dict[str, threading.Timer] = {}
        # 向后兼容别名（外部只读访问场景）
        self.cleanup_lock = self._cleanup_lock
        self.pending_cleanups = self._pending_cleanups
        
        # 多 A↔多 B 映射：运行时只接受显式配置，不从旧单根字段推导 fallback。
        a_b_mappings = getattr(config, "a_b_mappings", [])
        self.a_b_mappings: list[ABMapping] = (
            a_b_mappings if isinstance(a_b_mappings, list) else []
        )
        self.a_roots = [normalize_local_root(m.a_root) for m in self.a_b_mappings]
        self._a_to_b_map: dict[str, Path] = {
            str(normalize_local_root(m.a_root)): normalize_local_root(m.b_root)
            for m in self.a_b_mappings
            if getattr(m, "mapping_id", "") and getattr(m, "a_root", "") and getattr(m, "b_root", "")
        }
        # C 根单一全局，不建立 _a_to_c_map
        
        self.engine_configs: list[dict] = []
        self._restoring_markers: set[str] = set()
        # 代际计数器：与 _engine_internal_generation 相同模式（M1修复）
        self._restoring_generation: dict[str, int] = {}
        # B-7 删除归因：引擎内部操作（隔离/清理）删除 B 文件时标记 fingerprint，
        # handle_b_deleted 检测到此标记即跳过不可逆的云删除 + A 区删除，
        # 仅清理本地 DB 行。避免引擎隔离/僵尸清理被误判为用户删除而连累云源。
        self._engine_internal_markers: set[str] = set()
        # 代际计数器：为延迟清除提供重入安全（M1修复）
        # 每次标记递增，延迟清除时检查：若代际已变化则不清除（有新的标记发生）
        self._engine_internal_generation: dict[str, int] = {}
        self._restoring_lock = threading.Lock()
        self._lineage_log_lock = threading.Lock()
        self._lineage_log_keys: set[str] = set()
        self._webdav_scan_logged: set[str] = set()
        # 按 fingerprint 串行化 A→B 处理，避免 TOCTOU 竞争（P1-4）
        self._fingerprint_locks_lock = threading.Lock()
        self._fingerprint_locks: dict[str, threading.Lock] = {}
        # WebUI 媒体刷新锁：防止同一媒体并发刷新
        self._refresh_lock = threading.Lock()
        self.sync_service = SyncService(self)
        self.subtitle_handler = SubtitleHandler(self)
        self._mapping_version = mapping_version(self.a_b_mappings, self.c_root)

    def _current_mapping_ids(self) -> list[str]:
        """返回去重非空的 mapping_id 集合，供 generation 推进使用。"""
        return list({
            str(m.mapping_id).strip()
            for m in self.a_b_mappings
            if str(getattr(m, "mapping_id", "")).strip()
        })

    # b_root 不作为生产同步、清理、迁移或血统推导的 fallback。
    # 保留只读属性以兼容外部旧调用，但调用方必须先解析唯一 mapping。
    @property
    def b_root(self) -> Path:
        return next(iter(self._a_to_b_map.values())) if self._a_to_b_map else Path()

    @property
    def c_root(self) -> Path:
        return Path(self.config.paths.c_root).resolve() if self.config.paths.c_root else Path()

    def _mark_engine_internal(self, fingerprint: str) -> None:
        """标记 fingerprint 为引擎内部删除（B-7）。

        handle_b_deleted 检测到此标记即跳过不可逆的云删除 + A 区删除。
        与 _restoring_markers 共用 _restoring_lock 串行化。
        递增代际计数器，使之前已调度的延迟清除不会误清理（M1修复）。
        """
        if fingerprint:
            with self._restoring_lock:
                self._engine_internal_markers.add(fingerprint)
                self._engine_internal_generation[fingerprint] = \
                    self._engine_internal_generation.get(fingerprint, 0) + 1

    def _clear_engine_internal(self, fingerprint: str) -> None:
        """清除引擎内部删除标记（B-7）。"""
        if fingerprint:
            with self._restoring_lock:
                self._engine_internal_markers.discard(fingerprint)

    def _clear_engine_internal_delayed(self, fingerprint: str, delay: float = 10.0) -> None:
        """延迟清除引擎内部删除标记（M1修复）。

        quarantine_file 触发 on_moved 事件后，watchdog 在新线程中异步调用 handle_b_deleted。
        如果立即清除标记，handle_b_deleted 执行时标记已不存在，会误判为用户删除并级联删除云源。

        使用代际计数器确保重入安全：
        - 调度时捕获当前代际值
        - 清除时检查：若代际已增加（新的标记发生），则跳过清除
        - 延迟从 2s 增加到 10s，覆盖 watchdog 事件处理的最坏情况延迟
        """
        with self._restoring_lock:
            gen = self._engine_internal_generation.get(fingerprint, 0)

        def _delayed_clear():
            time.sleep(delay)
            with self._restoring_lock:
                # 仅当代际未变化时才清除 — 没有新的标记覆盖此 fingerprint
                if self._engine_internal_generation.get(fingerprint, 0) == gen:
                    self._engine_internal_markers.discard(fingerprint)
                    self._engine_internal_generation.pop(fingerprint, None)
        threading.Thread(target=_delayed_clear, daemon=True).start()

    def get_path_lock(self, path: str | Path) -> threading.Lock:
        key = str(Path(path).resolve())
        with self._path_locks_lock:
            lock = self._path_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._path_locks[key] = lock
        return lock

    def get_webdav_lock(self, webdav_path: str) -> threading.Lock:
        """获取 WebDAV 路径锁。

        WebDAV 路径（如 /movies/a.mp4）不能走 get_path_lock：
        Path('/x/y').resolve() 在 Windows 上会解析为伪造的 C:\\x\\y，
        可能与真实本地路径 key 碰撞，且跨盘符不确定。
        这里用独立的 'webdav:' 前缀命名空间，key 稳定且与本地路径锁隔离。
        """
        key = "webdav:" + webdav_path
        with self._path_locks_lock:
            lock = self._path_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._path_locks[key] = lock
        return lock

    def get_fingerprint_lock(self, fingerprint: str) -> threading.Lock:
        """按 fingerprint 获取锁，用于串行化同一媒体的 A→B 处理。"""
        with self._fingerprint_locks_lock:
            lock = self._fingerprint_locks.get(fingerprint)
            if lock is None:
                lock = threading.Lock()
                self._fingerprint_locks[fingerprint] = lock
        return lock

    def is_path_under_any_root(self, path: str, roots: list[str]) -> bool:
        if not path or path == "/":
            return False
        normalized_path = path.rstrip("/") or "/"
        # Windows 下路径大小写不敏感，统一小写比较 (P2-9)
        import sys
        if sys.platform == "win32":
            normalized_path = normalized_path.lower()
        for root in roots:
            if not root or root == "/":
                continue
            normalized_root = root.rstrip("/") or "/"
            if sys.platform == "win32":
                normalized_root = normalized_root.lower()
            if normalized_path == normalized_root or normalized_path.startswith(
                    normalized_root + "/"):
                return True
        return False

    def is_valid_refresh_root(self, root_path: str) -> bool:
        if not self.config.strm_engine_paths:
            return True
        return self.is_path_under_any_root(
            root_path, self.config.strm_engine_paths)

    def _log_lineage_pass_once(self, reason: str, b_local_path: str) -> None:
        """把同类通过日志按目录前缀压缩成一行。"""
        summary_path = str(Path(b_local_path).parent)
        if not summary_path.endswith(os.sep):
            summary_path += os.sep
        log_key = f"{reason}|{summary_path}"
        with self._lineage_log_lock:
            if log_key in self._lineage_log_keys:
                return
            # 限制集合大小，防止长期运行内存泄漏
            if len(self._lineage_log_keys) > 10000:
                self._lineage_log_keys.clear()
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
            (record.root_path, record.trash_path)
            for record in self.db.get_protected_roots()
            if record.active and (valid_engine_paths is None or record.root_path in valid_engine_paths)
        ]
        self.db.save_protected_roots_snapshot(roots)

    def refresh_webdav_root(self, root_path: str, depth: int) -> None:
        root_path = root_path.rstrip("/") or "/"
        # 每次扫描开始前重置日志去重集合，避免跨调用无界增长
        self._webdav_scan_logged.clear()
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
        # 注意：不再在此处调用 cleanup_b_zombies_under_folder(root_path)
        # 冗余清理改为局部触发：WebUI 手动刷新 / B 区删除事件（trigger_delayed_cleanup）

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
        if current_depth >= max_depth:
            return True
        normalized_path = path.rstrip("/") or "/"
        # 按路径去重，同一路径的扫描日志只输出一次
        if normalized_path not in self._webdav_scan_logged:
            self._webdav_scan_logged.add(normalized_path)
            logging.debug(
                "[WebDAV刷新] 扫描 %s (深度 %s/%s)",
                normalized_path,
                current_depth,
                max_depth)
        result = self.admin_api.list_contents(normalized_path)
        if result is None:
            logging.warning("[WebDAV刷新] 路径不存在或无法列出: %s", normalized_path)
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
        t_start = time.time()
        logging.info("[启动] 准备环境并初始化数据库...")
        self.prepare_environment()
        self.db.init_db()
        config_status = self.get_config_status()
        if config_status["status"] != "ready":
            logging.warning("[启动] 配置未就绪，进入 fail-safe: %s", config_status)
            self._running = False
            return
        logging.info("[启动] 数据库初始化完成")
        # 清理字幕表中目标文件已不存在的记录
        self.db.cleanup_invalid_subtitles()
        self.update_engine_configs()
        logging.info("[启动] 引擎配置加载完成")
        # 启动等待（无论是否执行全量同步，都等待，让 OpenList 服务就绪）
        behavior_cfg = self.config.behavior
        wait_seconds = int(
            getattr(behavior_cfg, "sync_on_startup_wait", 0) or 0)
        if wait_seconds > 0:
            logging.info("[启动] 等待 %d 秒，让 OpenList 服务就绪...", wait_seconds)
            time.sleep(wait_seconds)
        
        t_phase = time.time()
        self.initial_scan_b()
        logging.info("[启动] B 区扫描耗时: %.1fs", time.time() - t_phase)
        
        t_sub = time.time()
        self.sync_protected_roots_from_config()
        logging.info("[启动] 同步保护根目录耗时: %.1fs", time.time() - t_sub)

        t_sub = time.time()
        self.scan_removed_protected_roots()
        logging.info("[启动] 扫描已移除保护根耗时: %.1fs", time.time() - t_sub)

        t_sub = time.time()
        self.persist_current_roots_snapshot()
        logging.info("[启动] 持久化根目录快照耗时: %.1fs", time.time() - t_sub)
        
        t_phase = time.time()
        self.initial_scan_a(use_bulk=True)
        logging.info("[启动] A 区扫描耗时: %.1fs", time.time() - t_phase)
        
        # 根据配置决定是否执行 A→B 全量同步（实际复制文件）
        sync_on_startup = getattr(behavior_cfg, "sync_on_startup", True)
        generation_pushed = False
        try:
            if sync_on_startup:
                t_phase = time.time()
                self.scan_a_to_b_full_sync(
                    valid_engine_paths=list(self.config.paths.strm_engine_paths),
                    use_bulk=True)
                logging.info("[启动] A→B 同步耗时: %.1fs", time.time() - t_phase)
            else:
                logging.info("[启动] 跳过 A→B 全量同步（sync_on_startup=false）")

            # 成功收口：仅当 sync_on_startup=true 时推进 generation
            if sync_on_startup:
                mapping_ids = self._current_mapping_ids()
                if mapping_ids:
                    self.db.complete_index_generation(mapping_ids)
                    generation_pushed = True
                    logging.info("[启动] 索引代次推进到 %s", 
                                 self.db.get_control("index_generation", "0"))
                    
                    # 同步 mapping 版本摘要（仅当变化时更新时间）
                    current_version = self._mapping_version
                    self.db.set_mapping_version(current_version)
        except Exception:
            generation_pushed = False
            raise  # 保持现有中止语义，不写入审计时间

        # 启动阶段已经完成一次全量 A 区审计，7 天兜底从本次启动重新计时。
        try:
            audit_now = time.time()
            self.db.set_control("last_full_audit_at", str(audit_now))
            if hasattr(self.refresh_service, "_last_full_audit_at"):
                self.refresh_service._last_full_audit_at = audit_now
        except (AttributeError, OSError):
            logging.warning("[启动] 保存全量审计时间失败")
        
        t_sub = time.time()
        self.start_watchers()
        logging.info("[启动] Watcher 启动耗时: %.1fs", time.time() - t_sub)
        # 启动后立即扫描 A 区字幕文件（补偿 initial_scan_a 不处理字幕）
        t_sub = time.time()
        self._scan_a_subtitles_on_startup()
        logging.info("[启动] A 区字幕扫描耗时: %.1fs", time.time() - t_sub)
        self.refresh_service.start()
        # start() 能走到这里说明配置 ready 且所有启动阶段已完成。
        # WebUIServer.start_main() 用该标志判断引擎是否真的起来了；
        # 缺这一行会让 ready 配置被误判为 fail-safe（引擎在跑但对外报未启动）。
        self._running = True
        logging.info("嗨嗨，应用启动成功咯！(总耗时 %.1fs)", time.time() - t_start)

    def stop(self) -> None:
        # 取消所有待执行的延迟清理定时器
        with self._cleanup_lock:
            for timer in list(self._pending_cleanups.values()):
                timer.cancel()
            self._pending_cleanups.clear()
        self.refresh_service.stop()
        if self.observer is not None and self.observer.is_alive():
            self.observer.stop()
            self.observer.join()

    def prepare_environment(self) -> None:
        for a_root in self.a_roots:
            if not a_root.exists():
                logging.warning("[A区路径不存在] %s", a_root)
        for b_root in self._a_to_b_map.values():
            b_root.mkdir(parents=True, exist_ok=True)
        self.c_root.mkdir(parents=True, exist_ok=True)

    def start_watchers(self) -> None:
        if self.get_config_status()["status"] != "ready":
            logging.warning("[监控启动] 配置未就绪，禁止启动 watcher")
            return
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
        for b_root in self._a_to_b_map.values():
            b_root.mkdir(parents=True, exist_ok=True)
            self.observer.schedule(
                BAreaEventHandler(self), str(b_root), recursive=True)
            logging.info("[监控启动] B区: %s", b_root)
        self.c_root.mkdir(parents=True, exist_ok=True)
        self.observer.schedule(
            CAreaEventHandler(self), str(self.c_root), recursive=True)
        logging.info("[监控启动] C区: %s", self.c_root)
        self.observer.start()
        if active_a == 0:
            logging.warning("[提示] 没有可用的 A 区监控目录，程序将依赖后续目录出现或主动刷新")

    def get_mapping_for_a(self, local_path: str | Path) -> tuple[str, Path, Path] | None:
        """严格解析 A 路径所属的唯一 mapping。零/多命中均 fail-closed。"""
        target = normalize_local_root(local_path)
        matches: list[tuple[str, Path, Path]] = []
        for mapping in self.a_b_mappings:
            mapping_id = str(getattr(mapping, "mapping_id", "")).strip()
            if not mapping_id:
                continue
            a_root = normalize_local_root(mapping.a_root)
            b_root = normalize_local_root(mapping.b_root)
            try:
                target.relative_to(a_root)
            except ValueError:
                continue
            matches.append((mapping_id, a_root, b_root))
        return matches[0] if len(matches) == 1 else None

    def get_mapping_for_b(self, local_path: str | Path) -> tuple[str, Path, Path] | None:
        """严格解析 B 路径所属的唯一 mapping。零/多命中均 fail-closed。"""
        target = normalize_local_root(local_path)
        matches: list[tuple[str, Path, Path]] = []
        for mapping in self.a_b_mappings:
            mapping_id = str(getattr(mapping, "mapping_id", "")).strip()
            if not mapping_id:
                continue
            a_root = normalize_local_root(mapping.a_root)
            b_root = normalize_local_root(mapping.b_root)
            try:
                target.relative_to(b_root)
            except ValueError:
                continue
            matches.append((mapping_id, b_root, a_root))
        return matches[0] if len(matches) == 1 else None

    def get_a_roots_for_refresh_paths(self) -> list[Path]:
        """返回 refresh_paths 命中的 A 根；空列表表示周期主动扫描全部跳过。"""
        refresh_paths = [str(p).rstrip("/") or "/" for p in (self.config.refresh_paths or [])]
        if not refresh_paths:
            return []

        storage_map = getattr(self.config, "strm_storage_map", {}) or {}
        matched: list[Path] = []
        seen: set[Path] = set()
        for mapping in self.a_b_mappings:
            a_root = normalize_local_root(mapping.a_root)
            storage_entries = [
                (entry_path, storage)
                for entry_path, storage in storage_map.items()
                if normalize_local_root(storage.local_path) == a_root
            ]
            # 无法从 storage map 唯一关联引擎时 fail-closed，不能把一个 refresh
            # path 错误地套用到所有 mapping。
            engine_paths = {storage.mount_path for _, storage in storage_entries}
            if not engine_paths:
                continue
            is_matched = any(
                rp == ep.rstrip("/") or rp.startswith(ep.rstrip("/") + "/")
                for rp in refresh_paths
                for ep in engine_paths
            )
            if is_matched and a_root not in seen:
                seen.add(a_root)
                matched.append(a_root)
        return matched

    def get_engine_paths_for_a_roots(self, a_roots: list[Path]) -> list[str]:
        """返回指定 A 根对应的 STRM engine mount paths，供周期同步过滤。"""
        roots = {normalize_local_root(root) for root in a_roots}
        result: list[str] = []
        storage_map = getattr(self.config, "strm_storage_map", {}) or {}
        for entry_path, storage in storage_map.items():
            if normalize_local_root(storage.local_path) in roots:
                mount = str(storage.mount_path).rstrip("/") or "/"
                if mount not in result:
                    result.append(mount)
        return result

    def get_config_status(self) -> dict[str, object]:
        """返回可供 WebUI 使用的配置状态，不触发任何危险操作。"""
        if not self.a_b_mappings:
            return {"status": "not_configured", "reason": "没有配置 A/B mapping"}
        seen_ids: set[str] = set()
        seen_a: list[Path] = []
        seen_b: list[Path] = []
        for mapping in self.a_b_mappings:
            mapping_id = str(getattr(mapping, "mapping_id", "")).strip()
            if (not mapping_id or mapping_id in seen_ids
                    or not str(getattr(mapping, "a_root", "")).strip()
                    or not str(getattr(mapping, "b_root", "")).strip()):
                return {"status": "fail_safe_active", "reason": "mapping 缺少唯一 ID 或根路径"}
            seen_ids.add(mapping_id)
            a_root = normalize_local_root(mapping.a_root)
            b_root = normalize_local_root(mapping.b_root)
            if any(a_root == old or b_root == old for old in seen_a + seen_b):
                return {"status": "fail_safe_active", "reason": "mapping 根路径重复"}
            seen_a.append(a_root)
            seen_b.append(b_root)
        return {"status": "ready", "reason": "mapping 配置有效"}

    def get_a_root_for_path(self, local_path: str | Path) -> Path | None:
        mapping = self.get_mapping_for_a(local_path)
        return mapping[1] if mapping else None

    def get_b_root_for_a(self, a_local_path: str | Path) -> Path:
        mapping = self.get_mapping_for_a(a_local_path)
        if mapping is None:
            raise ValueError(f"文件无法唯一解析 A/B mapping: {a_local_path}")
        return mapping[2]

    def get_b_root_for_path(self, b_local_path: str | Path) -> Path | None:
        mapping = self.get_mapping_for_b(b_local_path)
        return mapping[1] if mapping else None

    def _mapping_id_for_b(self, b_local_path: str | Path) -> str | None:
        mapping = self.get_mapping_for_b(b_local_path)
        return mapping[0] if mapping else None

    def build_b_path_from_a(self, a_local_path: str | Path,
                            webdav_path: str | None = None) -> Path:
        a_local = Path(a_local_path).resolve()
        a_root = self.get_a_root_for_path(a_local)
        if a_root is None:
            raise ValueError(f"文件不属于任何A根目录: {a_local}")
        rel = a_local.relative_to(a_root)
        is_movie = self._should_treat_as_movie(a_local, webdav_path)
        b_root = self.get_b_root_for_a(a_local)
        if is_movie:
            return b_root / rel
        suggested_name = suggest_rename(a_local)
        if suggested_name and webdav_path:
            # season 只来自 A 区本地路径/文件名，不从 WebDAV 路径推导
            season = extract_season_from_path(a_local)
            if season is None:
                season, _ = _extract_season_episode(a_local.name)
            _, episode = _extract_season_episode(a_local.name)
            if season is not None and episode is not None:
                # 目标文件名保留 WebDAV 源文件 stem 的原始 padding，避免
                # S04E01 与 S4E01 被标准化成同一个 B 区路径。
                webdav_name = PurePosixPath(str(webdav_path).replace("\\", "/")).name
                webdav_stem = Path(webdav_name).stem
                standard_name = (
                    f"{webdav_stem}{Path(a_local).suffix}"
                    if webdav_stem
                    else suggested_name or f"S{season:02d}E{episode:02d}{Path(a_local).suffix}"
                )
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
                return b_root / new_rel
        return b_root / rel

    def update_engine_configs(self):
        logging.info("[引擎配置] 正在向服务器请求 STRM 存储配置...")
        content = self.admin_api.get_strm_storages_full_info()
        self.engine_configs = []
        if not content:
            logging.warning("[引擎配置] 无法获取 STRM 存储完整信息！")
            return
        logging.info("[引擎配置] 获取到 %d 个 STRM 存储", len(content))

        # 严格只加载用户在 WebUI 中显式配置的 STRM 引擎。
        # 首次运行（engines_initialized=False）时 config.strm_engine_paths 已为空，
        # 因此 configured_engines 也为空 → 不会扫描任何引擎到 B 区，符合用户意图。
        configured_engines = set(
            p.rstrip("/") for p in self.config.strm_engine_paths if p.strip()
        )
        if configured_engines:
            logging.info(
                "[引擎配置] 仅加载用户配置的 %d 个引擎: %s",
                len(configured_engines), configured_engines)
        else:
            logging.info(
                "[引擎配置] 用户尚未配置任何 STRM 引擎（engines_initialized=%s），"
                "本次启动不加载任何引擎映射",
                getattr(self.config, "engines_initialized", False))

        for s in content:
            mount_path = s.get("mount_path", "unknown")
            # 跳过未配置的引擎（configured_engines 为空时全部跳过）
            if mount_path.rstrip("/") not in configured_engines:
                logging.debug("[引擎配置] 跳过未配置的引擎: %s", mount_path)
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
                if resolved_save_path not in {str(p) for p in self.a_roots}:
                    logging.warning(
                        "[引擎配置] SaveStrmLocalPath 未匹配本地 a_folders 配置: %s (mount=%s)",
                        resolved_save_path,
                        mount_path,
                    )
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
                fingerprint, cloud_show_name, physical_media_folder_name, b_local_path)
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
            if identity and identity.source_a_path:
                a_local_path = Path(identity.source_a_path)
                if a_local_path.exists():
                    a_record = ARecord(str(a_local_path), webdav_path, "", 0)
        if not a_record:
            logging.debug("[血统校验失败] 无A区源记录: %s", b_local_path)
            return None
        a_local_path = Path(a_record.local_path).resolve()
        if not a_local_path.exists():
            logging.debug("[血统校验失败] A区源文件不存在: %s", a_local_path)
            return None
        a_root = self.get_a_root_for_path(a_local_path)
        if not a_root:
            logging.debug("[血统校验失败] A区源不在任何根目录下: %s", a_local_path)
            return None
        b_root = self.get_b_root_for_path(b_local_path)
        if b_root is None:
            logging.debug("[血统校验失败] B区路径不在任何B根目录下: %s", b_local_path)
            return None
        try:
            a_rel = a_local_path.relative_to(a_root)
            b_rel = b_local.relative_to(b_root)
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
        mapping = self.get_mapping_for_b(b_local_path)
        if mapping is None:
            logging.warning("[边界映射] 无法解析 mapping，跳过媒体名匹配: %s", b_local_path)
            return False
        boundary = self.db.get_media_boundary_by_source_name_only(mapping[0], a_media_name)
        if boundary:
            if b_media_name in (boundary.source_media_name, boundary.current_media_name):
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
        """检查越界文件，返回 True 表示放行，False 表示拒绝。"""
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
        mapping = self.get_mapping_for_b(b_local_path)
        mapping_id = mapping[0] if mapping else None
        if fingerprint and mapping_id:
            boundary = self.db.get_media_boundary_by_fingerprint(mapping_id, fingerprint)
            if boundary:
                source_media_name = boundary.source_media_name
                current_media_name = boundary.current_media_name
                if physical_media_folder_name == current_media_name:
                    self._log_lineage_pass_once("边界映射匹配", b_local_path)
                    return True
                if physical_media_folder_name == source_media_name:
                    self._log_lineage_pass_once("回到源边界", b_local_path)
                    return True

        mapping = self.get_mapping_for_b(b_local_path)
        if mapping is None:
            logging.warning("[边界映射] 无法解析 mapping，跳过边界匹配: %s", b_local_path)
            return False
        mapping_id, b_root, _ = mapping
        if cloud_show_name and physical_media_folder_name:
            boundary_by_source = self.db.get_media_boundary_by_source_name_only(
                mapping_id, physical_media_folder_name)
            if boundary_by_source:
                mapped_source = boundary_by_source.source_media_name
                mapped_current = boundary_by_source.current_media_name
                if cloud_show_name == mapped_source or cloud_show_name == mapped_current:
                    self._log_lineage_pass_once("交叉边界映射匹配(源->当前)", b_local_path)
                    return True

            boundary_by_current = self.db.get_media_boundary_by_current_name(
                mapping_id, physical_media_folder_name, str(b_root))
            if boundary_by_current:
                mapped_source = boundary_by_current.source_media_name
                mapped_current = boundary_by_current.current_media_name
                if cloud_show_name == mapped_source or cloud_show_name == mapped_current:
                    self._log_lineage_pass_once("交叉边界映射匹配(当前->源)", b_local_path)
                    return True

            boundary_by_cloud = self.db.get_media_boundary_by_source_name_only(
                mapping_id, cloud_show_name)
            if boundary_by_cloud:
                mapped_source = boundary_by_cloud.source_media_name
                mapped_current = boundary_by_cloud.current_media_name
                if physical_media_folder_name in (mapped_source, mapped_current):
                    self._log_lineage_pass_once("交叉边界映射匹配(云端)", b_local_path)
                    return True

        return False

    def _handle_sync_phase_boundary(
            self, fingerprint: str, cloud_show_name: str,
            physical_media_folder_name: str, b_local_path: str) -> None:
        """处理同步阶段的边界映射记录。"""
        mapping = self.get_mapping_for_b(b_local_path)
        if mapping is None:
            logging.warning("[边界映射] 无法解析 mapping，跳过记录: %s", b_local_path)
            return
        mapping_id, b_root, _ = mapping
        existing = self.db.get_media_boundary_by_fingerprint(mapping_id, fingerprint)
        if not existing:
            self.db.upsert_media_boundary(
                mapping_id=mapping_id,
                fingerprint=fingerprint,
                source_media_name=cloud_show_name,
                current_media_name=physical_media_folder_name,
                engine_entry_path=str(b_root))
            logging.info(
                "[边界映射] 记录新映射: %s -> %s",
                cloud_show_name,
                physical_media_folder_name)
        elif existing.current_media_name != physical_media_folder_name:
            self.db.upsert_media_boundary(
                mapping_id=mapping_id,
                fingerprint=fingerprint,
                source_media_name=existing.source_media_name,
                current_media_name=physical_media_folder_name,
                engine_entry_path=str(b_root))
            logging.info(
                "[边界映射] 更新映射: %s -> %s",
                existing.source_media_name,
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

            mapping = self.get_mapping_for_b(b_local_path)
            if mapping is None:
                logging.warning("[单兵检查] 无法唯一解析 B 区 mapping，安全跳过: %s", b_local_path)
                return True
            _, b_root, _ = mapping
            physical_media_root_dir = b_root
            for i, part in enumerate(b_parts):
                if part == physical_media_folder_name:
                    physical_media_root_dir = b_root / Path(*b_parts[:i + 1])
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
        with self._cleanup_lock:
            old_timer = self._pending_cleanups.pop(physical_dir, None)
            if old_timer:
                old_timer.cancel()
            timer = threading.Timer(
                30, self._execute_solo_judgment_safe, args=(
                    physical_dir, cloud_media_root))
            timer.daemon = True
            self._pending_cleanups[physical_dir] = timer
            timer.start()

    def _execute_solo_judgment_safe(self, physical_dir: str, cloud_media_root: str):
        """安全执行单兵审判，完成后自动清理定时器引用"""
        try:
            self.execute_solo_judgment(physical_dir, cloud_media_root)
        finally:
            with self._cleanup_lock:
                self._pending_cleanups.pop(physical_dir, None)

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

    def initial_scan_b(self, *, force_full: bool = False) -> None:
        """初始化扫描 B 区现有文件，与数据库记录进行同步。

        ``force_full`` 只禁用 snapshot 快速路径；配置 fail-safe 时仍拒绝扫描。
        
        拆分为多个子函数以提高可读性：
        1. _scan_b_disk: 扫描磁盘文件
        2. _load_b_db_records: 加载数据库记录
        3. _reconcile_b_historical_records: 对比历史 DB 记录与磁盘数据
        4. _insert_new_b_records: 插入磁盘上新的 B 区记录
        """
        t_total = time.time()
        if self.get_config_status()["status"] != "ready":
            logging.warning("[初始化] 配置处于 fail-safe，拒绝 B 区扫描 (force_full=%s)", force_full)
            return
        logging.info("[初始化] B 区逆向自同步开始 (force_full=%s)...", force_full)
        disk_data = self._scan_b_disk()
        if disk_data is None:
            return
        
        db_records = self._load_b_db_records()
        if db_records is None:
            return
        
        processed = set()
        self._reconcile_b_historical_records(disk_data, db_records, processed, force_full=force_full)
        self._insert_new_b_records(disk_data, processed)
        logging.info("[初始化] B 区逆向自同步完成 (%.1fs)", time.time() - t_total)

    def _snapshot_reuses_valid_lineage(
            self, local_path: str, fingerprint: str | None) -> bool:
        mapping = self.get_mapping_for_b(local_path)
        if mapping is None or not fingerprint:
            return False
        try:
            stat_before = Path(local_path).stat()
            snapshot = self.db.get_b_lineage_snapshot(mapping[0], local_path)
            if snapshot is None:
                return False
            return (
                snapshot.validation_state == "valid"
                and snapshot.mapping_version == self._mapping_version
                and snapshot.lineage_version == LINEAGE_VERSION
                and snapshot.fingerprint == fingerprint
                and snapshot.file_size == stat_before.st_size
                and snapshot.mtime_ns == stat_before.st_mtime_ns
            )
        except (OSError, AttributeError, TypeError, ValueError) as exc:
            logging.warning("[B区快照] 读取/比较失败，回退完整核对: %s (%s)", local_path, exc)
            return False

    def _store_valid_lineage_snapshot(self, local_path: str, fingerprint: str | None) -> None:
        mapping = self.get_mapping_for_b(local_path)
        if mapping is None or not fingerprint:
            return
        try:
            stat_after = Path(local_path).stat()
            self.db.upsert_b_lineage_snapshot(
                mapping[0], local_path, stat_after.st_size, stat_after.st_mtime_ns,
                fingerprint, self._mapping_version, LINEAGE_VERSION, "valid")
        except (OSError, AttributeError, TypeError, ValueError) as exc:
            logging.warning("[B区快照] 写入失败，记录保留但不复用: %s (%s)", local_path, exc)

    def _scan_b_disk(self) -> tuple[dict, dict] | None:
        """扫描 B 区磁盘文件，返回 (fingerprint_to_paths, path_to_data)"""
        logging.info("[初始化] B 区磁盘扫描开始...")
        all_fingerprint_to_paths: dict[str, set[str]] = {}
        all_path_to_data: dict[str, dict] = {}
        
        for b_root in self._a_to_b_map.values():
            if not b_root.exists():
                logging.info("[初始化] B 区根目录不存在，跳过: %s", b_root)
                continue
            
            t0 = time.time()
            scanned_count = 0
            
            for strm_file in b_root.rglob("*.strm"):
                try:
                    scanned_count += 1
                    webdav_path = read_strm_webdav_path(strm_file)
                    if webdav_path:
                        fingerprint = make_strm_fingerprint(webdav_path)
                        path_str = str(strm_file)
                        if fingerprint not in all_fingerprint_to_paths:
                            all_fingerprint_to_paths[fingerprint] = set()
                        all_fingerprint_to_paths[fingerprint].add(path_str)
                        all_path_to_data[path_str] = {
                            "webdav": webdav_path, "fp": fingerprint}
                    
                    # 每 500 个文件输出进度
                    if scanned_count % 500 == 0:
                        logging.info("[初始化] B 区扫描进度: %d 个文件 (%.1fs)", 
                                   scanned_count, time.time() - t0)
                except Exception as e:
                    logging.warning("[初始化] 读取 B 区文件失败: %s (%s)", strm_file, e)
            
            logging.info("[初始化] B 区根目录 %s 扫描完毕，共发现 %d 个 STRM 文件 (%.1fs)", 
                        b_root, scanned_count, time.time() - t0)
        
        total_scanned = sum(len(v) for v in all_fingerprint_to_paths.values())
        logging.info("[初始化] B 区磁盘扫描完毕，共发现 %d 个 STRM 文件", total_scanned)
        return all_fingerprint_to_paths, all_path_to_data

    def _load_b_db_records(self) -> list | None:
        """加载 B 区数据库记录，失败返回 None"""
        logging.info("[初始化] B 区数据库记录加载开始...")
        try:
            all_b_records = self.db.get_all_b_records()
            logging.info("[初始化] 成功读取 B 区历史数据库记录: %d 条", len(all_b_records))
            return all_b_records
        except Exception as e:
            logging.error("[初始化] 查询历史记录失败 (通常是因为表不存在): %s", e)
            return None

    def _reconcile_b_historical_records(
         self,
         disk_data: tuple[dict, dict],
         db_records: list,
         processed: set,
         force_full: bool = False,
     ) -> None:

        """对比历史 DB 记录与磁盘数据，处理越界/迁移/删除"""
        t_start = time.time()
        logging.info("[初始化] B 区历史记录核对开始 (%d 条)", len(db_records))
        disk_fingerprint_to_paths, disk_path_to_data = disk_data

        total_records = len(db_records)
        last_log_time = time.time()
        processed_count = 0

        for row in db_records:
            processed_count += 1
            db_local_path = row.local_path
            db_fingerprint = row.fingerprint

            # 进度日志：每 100 条或每 2 秒
            current_time = time.time()
            if processed_count % 100 == 0 or (current_time - last_log_time) >= 2.0:
                logging.info(
                    "[初始化] B 区历史记录对比进度: %d/%d 条",
                    processed_count, total_records
                )
                last_log_time = current_time
            
            if not db_fingerprint:
                logging.debug("[B区历史核对] 删除无指纹记录: %s", db_local_path)
                self.db.delete_b_by_local(db_local_path)
                continue
            
            # 历史 DB 记录在磁盘上存在且指纹匹配
            if db_local_path in disk_path_to_data and disk_path_to_data[db_local_path]["fp"] == db_fingerprint:
                webdav_path = disk_path_to_data[db_local_path]["webdav"]
                row_mapping_id = getattr(row, "mapping_id", "") or self._mapping_id_for_b(db_local_path)
                if not force_full and row_mapping_id and self._snapshot_reuses_valid_lineage(db_local_path, db_fingerprint):
                    processed.add(db_local_path)
                    logging.debug("[B区历史核对] 复用有效 lineage snapshot: %s", db_local_path)
                    continue
                logging.debug("[B区历史核对] lineage 校验: %s", db_local_path)
                t_op = time.time()
                if not self._verify_b_path_lineage(db_local_path, webdav_path):
                    op_elapsed = time.time() - t_op
                    if op_elapsed > B_SCAN_SLOW_OPERATION_SECONDS:
                        logging.warning("[B区历史核对] lineage 校验耗时 %.1fs: %s", op_elapsed, db_local_path)
                    logging.warning("[B区历史越界清理] 物理删除历史遗留越界文件: %s", db_local_path)
                    logging.debug("[B区历史核对] 物理删除: %s", db_local_path)
                    safe_remove_file(db_local_path)
                    logging.debug("[B区历史核对] DB删除: %s", db_local_path)
                    self.db.delete_b_by_local(db_local_path)
                    logging.debug("[B区历史核对] 身份刷新 fp=%s", db_fingerprint)
                    if row_mapping_id:
                        self.refresh_identity_current_b_path(db_fingerprint, row_mapping_id)
                    else:
                        logging.warning("[B区历史核对] 无法解析 mapping，跳过 projection 刷新: %s", db_local_path)
                    processed.add(db_local_path)
                    continue
                op_elapsed = time.time() - t_op
                if op_elapsed > B_SCAN_SLOW_OPERATION_SECONDS:
                    logging.warning("[B区历史核对] lineage 校验耗时 %.1fs: %s", op_elapsed, db_local_path)
                processed.add(db_local_path)
                if row_mapping_id:
                    self._store_valid_lineage_snapshot(db_local_path, db_fingerprint)
                continue
            
            # 历史 DB 记录的指纹在磁盘上存在，但路径不同（可能是重命名）
            disk_paths_for_fp = disk_fingerprint_to_paths.get(db_fingerprint, set())
            available_paths = [p for p in disk_paths_for_fp if p not in processed]
            valid_new_path = None
            
            for candidate_path in available_paths:
                candidate_webdav = disk_path_to_data[candidate_path]["webdav"]
                logging.debug("[B区历史核对] 候选路径 lineage 校验: %s", db_local_path)
                t_op = time.time()
                if self._verify_b_path_lineage(candidate_path, candidate_webdav):
                    valid_new_path = candidate_path
                    op_elapsed = time.time() - t_op
                    if op_elapsed > B_SCAN_SLOW_OPERATION_SECONDS:
                        logging.warning("[B区历史核对] lineage 校验耗时 %.1fs: %s", op_elapsed, candidate_path)
                    break
                else:
                    op_elapsed = time.time() - t_op
                    if op_elapsed > B_SCAN_SLOW_OPERATION_SECONDS:
                        logging.warning("[B区历史核对] lineage 校验耗时 %.1fs: %s", op_elapsed, candidate_path)
                    logging.warning("[B区越界清理] 发现非法跨目录移动，物理删除: %s", candidate_path)
                    safe_remove_file(candidate_path)
                    processed.add(candidate_path)
            
            if valid_new_path:
                logging.debug("[B区历史核对] 路径迁移: %s -> %s", db_local_path, valid_new_path)
                self._handle_b_record_migration(db_local_path, valid_new_path, db_fingerprint)
                self._store_valid_lineage_snapshot(valid_new_path, db_fingerprint)
                processed.add(valid_new_path)

            else:
                logging.debug("[B区历史核对] 无匹配磁盘路径，删除并刷新: %s", db_local_path)
                self.db.delete_b_by_local(db_local_path)
                mapping_id = getattr(row, "mapping_id", "") or self._mapping_id_for_b(db_local_path)
                if mapping_id:
                    self.refresh_identity_current_b_path(db_fingerprint, mapping_id)
                logging.debug("[B区自同步] 删除失效数据库记录: %s", db_local_path)

        logging.info("[初始化] B 区历史记录核对完成 (%d/%d 条, %.1fs)",
                     processed_count, total_records, time.time() - t_start)

    def _handle_b_record_migration(self, old_path: str, new_path: str, fingerprint: str) -> None:
        """处理 B 区记录的路径迁移"""
        mapping_id = self._mapping_id_for_b(new_path) or self._mapping_id_for_b(old_path)
        if not mapping_id:
            logging.warning("[B区自同步] 无法解析 mapping，跳过路径迁移: %s -> %s", old_path, new_path)
            return
        self.db.move_b_record(old_path, new_path)
        identity = self.db.get_identity_by_fingerprint(fingerprint)
        if identity and identity.current_b_path == old_path:
            self.db.update_identity_b_path(fingerprint, new_path)
        
        try:
            old_path_obj = Path(old_path)
            if old_path_obj.exists() and str(old_path_obj.resolve()) != str(Path(new_path).resolve()):
                safe_remove_file(old_path_obj)
                logging.debug("[B区自同步] 删除旧路径物理文件: %s", old_path)
        except Exception as e:
            logging.warning("[B区自同步] 删除旧路径物理文件失败: %s (%s)", old_path, e)
        
        if fingerprint:
            # 【已核对，勿再作为 bug 上报】
            # `mapping_id` 复用本函数上方 line 1429 已解析的变量，
            # 非二次调用 `self._mapping_id_for_b(...)`，勿"优化"改写。
            self.ensure_single_visible_instance(fingerprint, new_path, mapping_id=mapping_id)
        
        logging.info("[B区自同步] 更新路径(合法重命名): %s -> %s", old_path, new_path)

    def _insert_new_b_records(self, disk_data: tuple[dict, dict], processed: set) -> None:
        """插入磁盘上新的 B 区记录"""
        t_start = time.time()
        logging.info("[初始化] B 区新增记录处理开始...")
        _, disk_path_to_data = disk_data
        new_insert_count = 0
        
        for disk_path, data in disk_path_to_data.items():
            if disk_path not in processed:
                webdav_path = data["webdav"]
                fingerprint = data["fp"]
                
                if not self._verify_b_path_lineage(disk_path, webdav_path):
                    logging.warning("[B区越界清理] 发现非法新增跨区复制文件，物理删除: %s", disk_path)
                    safe_remove_file(disk_path)
                    continue
                
                mapping_id = self._mapping_id_for_b(disk_path)
                if not mapping_id:
                    logging.warning("[初始化] 无法解析 B mapping，跳过记录: %s", disk_path)
                    continue
                self.db.upsert_b(
                    disk_path,
                    webdav_path,
                    webdav_parent(webdav_path),
                    None,  # source_a_path 在初始化扫描阶段未知，后续由 A→B 同步补全
                    fingerprint=fingerprint,
                    mapping_id=mapping_id,
                    status="valid",
                )
                self.refresh_identity_current_b_path(fingerprint, mapping_id)
                self._store_valid_lineage_snapshot(disk_path, fingerprint)
                if fingerprint:
                    self.ensure_single_visible_instance(fingerprint, disk_path, mapping_id=self._mapping_id_for_b(disk_path))

                new_insert_count += 1
                
                # 每 200 条输出进度
                if new_insert_count % 200 == 0:
                    logging.info("[初始化] B 区新增记录进度: %d 条", new_insert_count)
        
        logging.info("[初始化] B 区新增记录处理完成: %d 条 (%.1fs)", new_insert_count, time.time() - t_start)

    def initial_scan_a(
            self, use_bulk: bool = False,
            a_roots: list[Path] | None = None):
        return self.sync_service.initial_scan_a(
            use_bulk=use_bulk, a_roots=a_roots)

    def cleanup_a_redundant_using_api(self) -> None:
        """使用 OpenList API 批量清理 A 区冗余文件。

        性能优化策略（混合方案）：
        1. 基于本地记录优化遍历范围：只遍历本地 A 区记录的父目录
        2. 并发分页：使用线程池并发请求多个页面（5 个并发）
        3. 客户端过滤：只保留 .strm 文件，忽略字幕、nfo、图片等

        fail-closed：若某父目录的云端列表不可信（返回 None），
        该父目录下的本地 A 记录整组不参与冗余差集。

        性能对比：
        - 旧方案：5万次 check_exists × 150ms = 7500秒（2小时）
        - 新方案：500次 /api/fs/list × 100ms / 5并发 = 10秒
        - 提升750倍
        """
        logging.info("[初始化] 使用 OpenList API 清理 A 区冗余文件...")
        t0 = time.time()

        a_records = self.db.get_all_a_records()
        if not a_records:
            logging.info("[初始化] A 区无记录，跳过冗余清理")
            return

        parent_dirs = {rec.parent_webdav_path for rec in a_records}
        logging.info("[初始化] 需要检查 %d 个云端目录", len(parent_dirs))

        # 按父目录分组 A 记录
        parent_to_records: dict[str, list] = {}
        for rec in a_records:
            parent_to_records.setdefault(rec.parent_webdav_path, []).append(rec)

        # 收集可信父目录的云端文件路径；不可信父目录整组跳过
        cloud_webdav_paths: set[str] = set()
        trusted_parents: set[str] = set()
        for parent_dir in parent_dirs:
            try:
                result = self._collect_cloud_files_concurrent(parent_dir)
                if result is not None:
                    cloud_webdav_paths.update(result)
                    trusted_parents.add(parent_dir)
                else:
                    logging.warning(
                        "[初始化] 云端目录 %s 不可信，该目录下本地记录整组排除",
                        parent_dir)
            except Exception as e:
                logging.warning(
                    "[初始化] 获取云端文件列表失败: %s, 错误: %s",
                    parent_dir, e)

        # 只把可信父目录下的本地记录纳入冗余差集
        trusted_a_records = [
            rec for rec in a_records
            if rec.parent_webdav_path in trusted_parents
        ]
        local_webdav_paths = {rec.webdav_path for rec in trusted_a_records}
        redundant_paths = local_webdav_paths - cloud_webdav_paths

        if not redundant_paths:
            logging.info("[初始化] A 区无冗余文件")
            return

        logging.info("[初始化] 发现 %d 个冗余文件，开始清理...", len(redundant_paths))

        cleaned = 0
        for rec in trusted_a_records:
            if rec.webdav_path in redundant_paths:
                try:
                    safe_remove_file(rec.local_path)
                    self.db.delete_a_by_local(rec.local_path)
                    self.db.set_ghost_protection(
                        rec.webdav_path,
                        self.config.behavior.ghost_protect_seconds,
                        reason="cloud_deleted",
                    )
                    cleaned += 1
                    if cleaned % 100 == 0:
                        logging.info(
                            "[初始化] A 区冗余清理进度: %d/%d (%.1fs)",
                            cleaned, len(redundant_paths), time.time() - t0)
                except Exception as e:
                    logging.warning(
                        "[初始化] 删除冗余文件失败: %s, 错误: %s",
                        rec.local_path, e)

        logging.info(
            "[初始化] A 区冗余清理完成，清理 %d 个文件 (%.1fs)",
            cleaned, time.time() - t0)

    def _parse_fs_list_content(self, res) -> tuple[list, int] | None:
        """解析 /api/fs/list 单页响应，按项目级契约校验（fail-closed）。

        仅当响应满足"权威成功"（code ∈ {0,200}、data 为 dict、
        data.content 为 list、data.total 为 int ≥ 0）时返回 (content, total)；
        否则返回 None 表示不可信，调用方必须对该父目录 fail-closed。

        参考 docs/openlist_api_fs_list_contract.md §2-§3。
        """
        if not res or not isinstance(res, dict):
            return None
        code = res.get("code")
        if code not in (0, 200):
            return None
        data = res.get("data")
        if not isinstance(data, dict):
            return None
        content = data.get("content")
        if not isinstance(content, list):
            return None
        total = data.get("total")
        # bool 是 int 的子类，JSON 中 total 不应为 bool；显式排除避免 True/False 被当作 1/0
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            return None
        # content=[] 但 total>0：自相矛盾，视为响应被截断/畸形
        if not content and total > 0:
            return None
        return content, total

    def _collect_cloud_files_concurrent(
            self, cloud_path: str) -> set[str] | None:
        """使用并发请求收集云端 .strm 文件。

        返回权威完整的 .strm 文件路径集合；若响应不可信则返回 None
        （fail-closed），调用方必须整组排除该父目录的本地记录。

        优化策略：
        1. 先获取第一页，获取 total
        2. 计算需要的页数
        3. 并发请求所有页面（5 个并发，带重试机制）
        4. 客户端过滤：只保留 .strm 文件

        参考 docs/openlist_api_fs_list_contract.md §4.1（per_page=100）。
        """
        file_set: set[str] = set()
        first_page = self.admin_api.list_directory(
            path=cloud_path, page=1, per_page=100)

        if not first_page:
            logging.warning("[初始化] 获取云端目录首页失败: %s", cloud_path)
            return None

        parsed = self._parse_fs_list_content(first_page)
        if parsed is None:
            logging.warning("[初始化] 云端目录首页响应不可信: %s", cloud_path)
            return None
        content, total = parsed

        for item in content:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            if (not item.get("is_dir")
                    and name.lower().endswith(".strm")):
                # API 返回的 "path" 是存储系统原始路径（如 D:\files\xxx），
                # 而非 WebDAV 虚拟路径；应从 cloud_path + name 重构路径
                file_set.add(cloud_path + "/" + name)

        total_pages = (total + 99) // 100
        if total_pages <= 1:
            return file_set

        def fetch_page_with_retry(page_num: int, max_retries: int = 3):
            """带重试的页面获取"""
            for attempt in range(max_retries):
                try:
                    result = self.admin_api.list_directory(
                        path=cloud_path, page=page_num, per_page=100)
                    if result:
                        return result
                except Exception as e:
                    if attempt < max_retries - 1:
                        logging.debug(
                            "[初始化] 获取页面 %d 失败（尝试 %d/%d）: %s",
                            page_num, attempt + 1, max_retries, e)
                        time.sleep(0.5 * (attempt + 1))
                    else:
                        logging.warning(
                            "[初始化] 获取页面 %d 失败（已重试 %d 次）: %s",
                            page_num, max_retries, e)
            return None

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(fetch_page_with_retry, page): page
                for page in range(2, total_pages + 1)
            }

            failed_pages: list[int] = []
            for future in as_completed(futures):
                page_num = futures[future]
                try:
                    result = future.result()
                    if not result:
                        failed_pages.append(page_num)
                        continue
                    parsed = self._parse_fs_list_content(result)
                    if parsed is None:
                        logging.warning(
                            "[初始化] 云端目录 %s 第 %d 页响应不可信，整组排除",
                            cloud_path, page_num)
                        return None
                    content, _total = parsed
                    for item in content:
                        # 与首页一致：非 dict 元素跳过，避免 AttributeError
                        # 把可恢复脏数据升级为整目录 fail-closed。
                        if not isinstance(item, dict):
                            continue
                        name = item.get("name")
                        if not name:
                            continue
                        if (not item.get("is_dir")
                                and name.lower().endswith(".strm")):
                            file_set.add(cloud_path + "/" + name)
                except Exception as e:
                    logging.warning(
                        "[初始化] 处理页面 %d 失败: %s", page_num, e)
                    failed_pages.append(page_num)

            if failed_pages:
                logging.warning(
                    "[初始化] 云端目录 %s 有 %d 个页面获取失败，整组排除: %s",
                    cloud_path, len(failed_pages), failed_pages)
                return None

        return file_set

    def _scan_a_subtitles_on_startup(self) -> None:
        """启动后扫描 A 区字幕文件（补偿 initial_scan_a 不处理字幕）"""
        logging.info("[初始化] 扫描 A 区字幕文件...")
        t0 = time.time()
        count = 0
        for a_root in self.a_roots:
            if not a_root.exists():
                continue
            for root, _dirs, files in os.walk(a_root):
                for name in files:
                    if is_subtitle_file(Path(root) / name):
                        self.process_subtitle_file(Path(root) / name)
                        count += 1
        logging.info(
            "[初始化] A 区字幕扫描完成，处理 %d 个文件 (%.1fs)",
            count, time.time() - t0)

    def scan_a_to_b_full_sync(
            self, valid_engine_paths: list[str] | None = None,
            use_bulk: bool = False) -> None:
        return self.sync_service.scan_a_to_b_full_sync(valid_engine_paths, use_bulk)

    def get_c_path_for_b(
            self, mapping_id: str, b_path: str | Path, b_root: str | Path) -> Path:
        """按 mapping 生成并校验 B→C 的隔离路径。"""
        mapping_id = str(mapping_id).strip()
        if not mapping_id:
            raise ValueError("mapping_id must be non-empty")
        b_path_resolved = Path(b_path).resolve()
        b_root_resolved = Path(b_root).resolve()
        try:
            relative = b_path_resolved.relative_to(b_root_resolved)
        except ValueError as exc:
            raise ValueError("B path escapes mapping root") from exc
        if ".." in relative.parts:
            raise ValueError("B path escapes mapping root")
        return self.c_root / mapping_id / relative

    @staticmethod
    def _original_strm_candidate(path: str | Path) -> Path:
        """将隔离后缀路径还原为可能的原始 STRM 路径。"""
        candidate = Path(path)
        name = candidate.name
        for marker in (".duplicate", ".quarantined", ".invalid"):
            index = name.find(marker)
            if index >= 0:
                return candidate.with_name(name[:index])
        return candidate

    def cleanup_b_redundant(self) -> None:
        # 后缀文件不是凭文件名即可删除：先解析自身/原始路径身份，并要求同源证明。
        for b_root in self._a_to_b_map.values():
            if not b_root.exists():
                continue
            redundant_keywords = ["duplicate", "quarantined", "invalid"]
            suffix_paths: set[Path] = set()
            for keyword in redundant_keywords:
                suffix_paths.update(b_root.rglob(f"*.{keyword}"))
                suffix_paths.update(b_root.rglob(f"*.{keyword}.*"))
            for file_path in suffix_paths:
                mapping = self.get_mapping_for_b(file_path)
                if mapping is None:
                    logging.warning("[冗余清理] 后缀文件 mapping 不明确，保留: %s", file_path)
                    continue
                candidate = self._original_strm_candidate(file_path)
                own_record = self.db.get_b_by_local_full(str(file_path))
                original_record = self.db.get_b_by_local_full(str(candidate))
                source = read_strm_webdav_path(file_path)
                if not source and own_record:
                    source = own_record.webdav_path
                if not source and original_record:
                    source = original_record.webdav_path
                if not source:
                    logging.warning("[冗余清理] 后缀文件身份未知，保留: %s", file_path)
                    continue
                if candidate.exists():
                    candidate_source = read_strm_webdav_path(candidate)
                    if candidate_source != source:
                        logging.warning("[冗余清理] 后缀文件与原始文件异源，保留: %s", file_path)
                        continue
                if not safe_remove_file(file_path):
                    logging.warning("[冗余清理] 后缀文件删除失败，保留: %s", file_path)
                    continue
                self.db.delete_b_by_local(str(file_path))
                if original_record and isinstance(original_record.fingerprint, str) and original_record.fingerprint:
                    self.refresh_identity_current_b_path(
                        original_record.fingerprint, mapping[0])
                logging.info("[冗余清理] 已清理已证明同源的后缀文件: %s", file_path)
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
            local_path = row.local_path
            webdav_path = row.webdav_path
            source_a_path = row.source_a_path
            fingerprint = row.fingerprint
            mapping_id = getattr(row, "mapping_id", "")
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
                exists = self.admin_api.check_exists(webdav_path)
                if exists is True:
                    logging.debug(
                        "[冗余清理跳过] A区源文件暂不可用但WebDAV存在，跳过清理: %s", webdav_path)
                    continue
                if exists is None:
                    logging.warning(
                        "[冗余清理跳过] WebDAV 存在性不可信，fail-closed 跳过: %s",
                        webdav_path)
                    continue
            # 【已核对，勿再作为 bug 上报】
            # None（不可信）已在上方 continue 拦截，此处 `source_exists` 只能是 False；
            # 该重复判断是有意冗余，确保 fail-closed 语义明确。
            if not source_exists:
                local = Path(local_path)
                if not local.exists():
                    self.db.delete_b_by_local(local_path)
                    if fingerprint:
                        self.refresh_identity_current_b_path(fingerprint, mapping_id)
                    continue
                mapping = self.get_mapping_for_b(local)
                if mapping is None or not mapping_id or mapping[0] != mapping_id:
                    logging.warning(
                        "[冗余清理→C区] 无法唯一解析 mapping，保留来源: %s", local)
                    continue
                try:
                    target = self.get_c_path_for_b(mapping_id, local, mapping[1])
                except ValueError as exc:
                    logging.warning(
                        "[冗余清理→C区] 无法生成安全目标，保留来源: %s (%s)", local, exc)
                    continue
                if target.exists():
                    target_webdav = read_strm_webdav_path(target)
                    source_webdav = read_strm_webdav_path(local)
                    if not source_webdav or not target_webdav or source_webdav != target_webdav:
                        logging.warning(
                            "[冗余清理→C区] C目标身份未知或异源，保留来源: %s -> %s",
                            local, target)
                        continue
                    if not safe_remove_file(local):
                        logging.warning("[冗余清理→C区] 同源来源清理失败，保留 DB: %s", local)
                        continue
                    self.db.delete_b_by_local(local_path)
                    if fingerprint:
                        self.refresh_identity_current_b_path(fingerprint, mapping_id)
                    migrated_count += 1
                    continue
                try:
                    move_file(local, target)
                except OSError as exc:
                    logging.warning(
                        "[冗余清理→C区] 迁移失败，保留来源: %s -> %s (%s)", local, target, exc)
                    continue
                try:
                    self.db.upsert_c(
                        str(target),
                        webdav_path,
                        local_path,
                        webdav_parent(webdav_path))
                except Exception as exc:
                    logging.error(
                        "[冗余清理→C区] C记录写入失败，保留已迁移文件待恢复: %s (%s)",
                        target, exc)
                    continue
                self.db.delete_b_by_local(local_path)
                if fingerprint:
                    self.refresh_identity_current_b_path(fingerprint, mapping_id)
                migrated_count += 1
                logging.info(
                    "[冗余清理→C区] A区源文件已不存在，迁移至C区: %s -> %s",
                    local_path,
                    webdav_path)
                continue
            exists = self.admin_api.check_exists(webdav_path)
            if exists is not False:
                # True=仍存在；None=不可信 —— 均不得删除
                if exists is None:
                    logging.warning(
                        "[冗余清理跳过] WebDAV 存在性不可信，fail-closed 跳过: %s",
                        webdav_path)
                continue
            safe_remove_file(local_path)
            self.db.delete_b_by_local(local_path)
            if fingerprint:
                self.refresh_identity_current_b_path(fingerprint, mapping_id)
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
        for b_root in self._a_to_b_map.values():
            remove_empty_dirs(b_root)
        remove_empty_dirs(self.c_root)

    def cleanup_a_deleted_on_cloud(self, engine_path: str) -> None:
        """在 update 模式下，清理 A 区中云端已删除的文件"""
        if not engine_path:
            return
        # 规范化路径前缀，避免 /movies 误匹配 /movies_extra (P2-8)
        prefix = engine_path.rstrip("/") + "/"
        # 遍历 A 区，找出指向该引擎路径下但云端已不存在的 STRM 文件
        a_records = self.db.get_all_a_records()
        for record in a_records:
            local_path = record.local_path
            webdav_path = record.webdav_path
            # 只处理属于当前 engine_path 范围的记录
            if not webdav_path.startswith(prefix) and webdav_path != engine_path:
                continue
            exists = self.admin_api.check_exists(webdav_path)
            if exists is None:
                logging.warning(
                    "[A区清理] WebDAV 存在性不可信，fail-closed 跳过: %s",
                    webdav_path)
                continue
            if exists is False:
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
            storages = self.admin_api.list_storages()
            data = storages.get("data", {}) if isinstance(storages, dict) else {}
            content = data.get("content", []) if isinstance(data, dict) else []
            # 防御 content: null —— data.get("content", []) 在 content 为 None 时
            # 返回 None（key 存在但值为 None，dict.get 不返回 default），len(None) 会
            # 抛 TypeError。与 list_contents 同模式守卫。
            if content is None:
                content = []
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
        mapping = self.get_mapping_for_a(local)
        if mapping is None:
            logging.debug("[A区跳过] 无法唯一解析 mapping: %s", local)
            return
        mapping_id = mapping[0]
        if is_subtitle_file(local):
            self.process_subtitle_file(local)
            return
        if local.suffix.lower() != ".strm":
            logging.debug("[A区跳过] 非 STRM 文件: %s", local)
            return
        webdav_path = read_strm_webdav_path(local)
        if not webdav_path:
            logging.warning("[A区] 无法解析STRM: %s", local)
            return
        parent = webdav_parent(webdav_path)
        self.db.upsert_a(str(local), webdav_path, parent)
        self.db.save_known_folder(parent, source="a")
        fingerprint = make_strm_fingerprint(webdav_path)
        # 按 fingerprint 串行化，避免并发创建 B 实例的 TOCTOU 竞争（P1-4）
        fp_lock = self.get_fingerprint_lock(fingerprint)
        with fp_lock:
            exists = self.admin_api.check_exists(webdav_path)
            if exists is None:
                logging.warning(
                    "[A区即时清理] WebDAV 存在性不可信，fail-closed 跳过删除: %s",
                    local)
                return
            if exists is False:
                logging.warning("[A区即时清理] WebDAV 已不存在，删除本地冗余 STRM: %s", local)
                safe_remove_file(str(local))
                self.db.delete_a_by_local(str(local))
                self.db.set_ghost_protection(
                    webdav_path,
                    self.config.behavior.ghost_protect_seconds,
                    reason="webdav_not_exists")
                return
            old_identity = self.db.get_identity_by_fingerprint(fingerprint)
            current_b_path = old_identity.current_b_path if old_identity else None
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
                fingerprint, mapping_id)
            if valid_b_instance:
                existing_main_path = valid_b_instance.local_path
                # 检查磁盘文件是否实际存在，避免基于已删除文件的评分比较（P1-2）
                if not Path(existing_main_path).exists():
                    self.db.mark_b_instance_status(existing_main_path, "stale")
                    logging.info(
                        "[A->B] 旧 B 实例文件已不存在，标记为 stale: %s",
                        existing_main_path)
                    valid_b_instance = None
                elif existing_main_path != str(b_local):
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
                        mapping_id=mapping_id,
                        status="valid")
                    self.db.upsert_identity(
                        fingerprint=fingerprint,
                        webdav_path=webdav_path,
                        source_a_path=str(local),
                        current_b_path=str(b_local))
                    self.ensure_single_visible_instance(fingerprint, str(b_local), mapping_id=mapping_id)
                    return
            if old_identity and current_b_path is None:
                exists = self.admin_api.check_exists(webdav_path)
                if exists is None:
                    logging.warning(
                        "[A->B跳过] WebDAV 存在性不可信，fail-closed 不清理: %s",
                        webdav_path)
                    return
                if exists is False:
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
            webdav_path = row.webdav_path
            parent_webdav_path = row.parent_webdav_path
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
        if re.search(r"s\d{1,2}e\d{1,4}(?!\d)", name):
            return True
        if re.search(r"\d{1,2}x\d{1,4}(?!\d)", name):
            return True
        if re.search(r".*- s\d{1,2}e\d{1,4}(?!\d) -", name):
            return True
        if re.search(r"season \d{1,2}/episode \d{1,4}(?!\d)", name):
            return True
        return False

    def _b_file_score(self, path: str) -> tuple:
        p = Path(path)
        name = p.name.lower()
        is_standard = self._is_standard_media_name(name)
        mapping = self.get_mapping_for_b(p)
        if mapping is None:
            logging.warning("[文件评分] 无法解析映射，使用路径自身降级评分: path=%s", path)
            b_rel_parts = p.parts
        else:
            _, b_root, _ = mapping
            try:
                b_rel_parts = p.relative_to(b_root).parts
            except ValueError:
                logging.warning("[文件评分] B路径不在对应根内: path=%s", path)
                b_rel_parts = p.parts
        webdav_parts = []
        try:
            row = self.db.get_b_by_local_full(path)
            if row:
                webdav_path = row.webdav_path
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
            self, fingerprint: str, trigger_path: str,
            prefer_path: str | None = None, mapping_id: str | None = None) -> None:
        """确保同一 fingerprint 只有一个 visible 实例。
        
        Args:
            fingerprint: 文件指纹
            trigger_path: 触发检查的路径
            prefer_path: 可选，评分相同时优先保留的路径（P2-10）
        """
        if not mapping_id:
            resolved = self.get_mapping_for_b(trigger_path)
            if resolved is None:
                logging.warning("[B区重复] 无法解析 mapping，跳过去重: %s", trigger_path)
                return
            mapping_id = resolved[0]
        all_instances = self.db.get_all_b_by_fingerprint(fingerprint, mapping_id)
        if not isinstance(all_instances, (list, tuple)):
            logging.warning("[B区重复] DB 返回不可迭代记录，跳过去重: %s", trigger_path)
            return
        if not all_instances:
            return
        valid_files = [row.local_path for row in all_instances if row.status
                       == "valid" and Path(row.local_path).exists()]
        if not valid_files:
            return
        # 排序，评分相同且 prefer_path 存在时让 prefer_path 排在前面
        prefer_path = prefer_path or trigger_path
        def _sort_key(path: str) -> tuple:
            score = self._b_file_score(path)
            # 评分相同时 prefer_path 优先（更低排序值）
            return (score, 0 if path == prefer_path else 1)
        valid_files.sort(key=_sort_key)
        keep = valid_files[0]
        duplicate_paths = self.db.mark_other_b_instances_duplicate(
            fingerprint, keep, mapping_id)
        for dup_path in duplicate_paths:
            dup = Path(dup_path)
            if not dup.exists():
                continue
            # B-7 删除归因：在物理隔离前标记，防止 quarantine_file 的改名事件
            # 触发 handle_b_deleted 连带删除云源/A区源。
            self._mark_engine_internal(fingerprint)
            try:
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
                        # B-8: DB 迁移失败（目标被占/冲突）— 回滚物理改名，
                        # 保持 DB local_path 与文件系统一致，避免两者分叉。
                        try:
                            Path(quarantined).rename(dup)
                            # B3-A: mark_other 已把 status 标为 duplicate，
                            # 物理已回滚到原 .strm → 恢复 valid，避免假 duplicate 死锁。
                            self.db.mark_b_instance_status(str(dup), "valid")
                            logging.warning(
                                "[B区重复] DB迁移失败，已回滚物理改名: %s", dup)
                        except OSError as revert_err:
                            # B3-B: 物理已在 quarantined，回滚失败 → 把 DB
                            # local_path 对齐到磁盘实际路径，避免「DB 指旧路径 /
                            # 磁盘在 .duplicate」分叉。
                            try:
                                aligned = self.db.move_b_record(
                                    str(dup), str(quarantined))
                                if aligned:
                                    self.db.mark_b_instance_status(
                                        str(quarantined), "duplicate")
                                    logging.error(
                                        "[B区重复] 回滚失败，已将 DB 对齐到隔离路径: %s -> %s",
                                        dup, quarantined)
                                else:
                                    logging.error(
                                        "[B区重复] 回滚失败且 DB 对齐隔离路径也失败: %s -> %s",
                                        dup, quarantined)
                            except Exception as align_err:  # noqa: BLE001
                                logging.error(
                                    "[B区重复] 回滚失败后 DB 对齐异常: %s -> %s: %s",
                                    dup, quarantined, align_err)
                            logging.error(
                                "[B区重复] DB迁移失败且回滚物理改名失败: %s -> %s: %s",
                                dup, quarantined, revert_err)
                            raise
                else:
                    # B3-A: 物理隔离失败时撤销 mark_other 留下的假 duplicate，
                    # 恢复 status=valid，避免「DB=duplicate / 磁盘仍为 .strm」
                    # 导致 ensure 永不重试的死锁。
                    self.db.mark_b_instance_status(str(dup), "valid")
                    logging.warning("[B区重复] 重复实例隔离失败: %s", dup)
            finally:
                # 延迟清除标记，确保 watchdog 事件已被处理
                self._clear_engine_internal_delayed(fingerprint)

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
        mapping_id = self._mapping_id_for_b(target)
        if not mapping_id:
            logging.warning("[B区修复失败] 无法解析 mapping: %s", target)
            return False
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
                mapping_id=mapping_id,
                status="valid")
            self.db.upsert_identity(
                fingerprint=fingerprint,
                webdav_path=webdav_path,
                source_a_path=source,
                current_b_path=str(target))
            self.ensure_single_visible_instance(fingerprint, str(target), mapping_id=self._mapping_id_for_b(target))
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
            historical_b_path = identity.current_b_path
            source_a_path = identity.source_a_path
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
                if identity and identity.current_b_path:
                    correct_b_path = identity.current_b_path
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
                        _restore_gen = self._restoring_generation.get(fingerprint, 0) + 1
                        self._restoring_generation[fingerprint] = _restore_gen
                    try:
                        shutil.copyfile(source_a_path, correct_b)
                        logging.info(
                            "[B区越界恢复] 已从 A 区恢复到正确位置: %s -> %s",
                            source_a_path,
                            correct_b_path)
                    finally:
                        def _remove_marker():
                            time.sleep(10)
                            with self._restoring_lock:
                                # 代际未变化才清除（M1修复）
                                if self._restoring_generation.get(fingerprint, 0) == _restore_gen:
                                    self._restoring_markers.discard(fingerprint)
                                    self._restoring_generation.pop(fingerprint, None)
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
            mapping_id = self._mapping_id_for_b(correct_b_path)
            if not mapping_id:
                logging.warning(
                    "[B区越界恢复] 无法解析目标 mapping，跳过 DB 恢复: %s", correct_b_path)
                return
            parent = webdav_parent(webdav_path)
            final_source_a = source_a_path or (
                identity.source_a_path if identity else self.find_a_source_by_webdav(webdav_path))
            if final_source_a is None:
                logging.warning(
                    "[B区] webdav_path=%s 无对应 A 区源文件，source_a_path 写入 NULL",
                    webdav_path)
            self.db.upsert_b(
                correct_b_path,
                webdav_path,
                parent,
                final_source_a,
                mapping_id=mapping_id,
                fingerprint=fingerprint,
                status="valid")
            self.db.upsert_identity(
                fingerprint=fingerprint,
                webdav_path=webdav_path,
                source_a_path=final_source_a,
                current_b_path=correct_b_path)
            self.ensure_single_visible_instance(fingerprint, correct_b_path, mapping_id=mapping_id)

    def _verify_a_source_exists(
            self, b_local_path: str, webdav_path: str, fingerprint: str) -> bool:
        identity = self.db.get_identity_by_fingerprint(fingerprint)
        if identity and identity.source_a_path:
            if Path(identity.source_a_path).exists():
                return True
        a_source = self.find_a_source_by_webdav(webdav_path)
        if a_source and Path(a_source).exists():
            return True
        mapping = self.get_mapping_for_b(b_local_path)
        if mapping is None:
            logging.warning("[A区源校验] 无法解析 B mapping，拒绝放行: %s", b_local_path)
            return False
        boundary = self.db.get_media_boundary_by_fingerprint(mapping[0], fingerprint)
        if boundary:
            logging.debug("[A区源校验] mapping boundary 存在，放宽检查: %s (指纹: %s...)",
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

    def _handle_unparseable_strm(self, local: Path, row: BRecord | None) -> None:
        if row:
            old_webdav_path = row.webdav_path
            parent = row.parent_webdav_path
            source_a_path = row.source_a_path
            if self.restore_b_file_from_a(
                    str(local), old_webdav_path, parent, source_a_path):
                logging.warning("[B区修复] 已从A区恢复异常STRM: %s", local)
                return
        # B-7 删除归因：物理隔离前标记 fingerprint 为引擎内部操作，
        # 使 quarantine_file 重命名触发的 on_moved→handle_b_deleted 不级联删除云源。
        fp_marker = row.fingerprint if row else None
        if fp_marker:
            self._mark_engine_internal(fp_marker)
        try:
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
        finally:
            if fp_marker:
                # 延迟清除标记，确保 watchdog 事件已被处理
                self._clear_engine_internal_delayed(fp_marker)

    def _handle_existing_b_file(
            self, local: Path, webdav_path: str, parent: str, fingerprint: str, row: BRecord) -> None:
        old_webdav_path = row.webdav_path
        old_parent = row.parent_webdav_path
        source_a_path = row.source_a_path
        old_fingerprint = row.fingerprint
        status = row.status
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
        self._quarantine_modified_b_file(local, old_fingerprint)

    def _refresh_b_record(self, local: Path, webdav_path: str, parent: str,
                          source_a_path: str | None, fingerprint: str, status: str | None) -> None:
        normalized_status = status or "valid"
        mapping_id = self._mapping_id_for_b(local)
        if not mapping_id:
            logging.warning("[B区记录] 无法解析 mapping，跳过更新: %s", local)
            return
        self.db.upsert_b(
            str(local),
            webdav_path,
            parent,
            source_a_path,
            fingerprint=fingerprint,
            mapping_id=mapping_id,
            status=normalized_status)
        self.db.upsert_identity(
            fingerprint=fingerprint,
            webdav_path=webdav_path,
            source_a_path=source_a_path,
            current_b_path=str(local) if normalized_status == "valid" else None)
        if normalized_status == "valid":
            self.ensure_single_visible_instance(fingerprint, str(local), mapping_id=self._mapping_id_for_b(local))

    def _quarantine_modified_b_file(self, local: Path, fingerprint: str | None = None) -> None:
        # B-7 删除归因：物理隔离前标记 fingerprint 为引擎内部操作，
        # 防止 quarantine_file 重命名触发的 handle_b_deleted 级联删除云源。
        if fingerprint:
            self._mark_engine_internal(fingerprint)
        try:
            quarantined = quarantine_file(local, suffix=".invalid")
            if quarantined:
                self.db.move_b_record(str(local), str(quarantined))
                self.db.mark_b_instance_status(str(quarantined), "quarantined")
                logging.warning("[B区隔离] 内容身份变化且恢复失败: %s -> %s", local, quarantined)
        finally:
            if fingerprint:
                # 延迟清除标记，确保 watchdog 事件已被处理
                self._clear_engine_internal_delayed(fingerprint)

    def _handle_new_b_file(self, local: Path, webdav_path: str,
                           parent: str, fingerprint: str) -> None:
        mapping_id = self._mapping_id_for_b(local)
        if not mapping_id:
            logging.warning("[B区] 无法解析 mapping，跳过新增文件: %s", local)
            return
        identity = self.db.get_identity_by_fingerprint(fingerprint)
        source_a_path = identity.source_a_path if identity else self.find_a_source_by_webdav(
            webdav_path)
        if source_a_path is None:
            logging.warning(
                "[B区] webdav_path=%s 无对应 A 区源文件，source_a_path 写入 NULL",
                webdav_path)
        self._maybe_record_boundary_mapping(local, webdav_path, fingerprint)
        self.db.upsert_b(
            str(local),
            webdav_path,
            parent,
            source_a_path,
            fingerprint=fingerprint,
            mapping_id=mapping_id,
            status="valid")
        self.db.upsert_identity(
            fingerprint=fingerprint,
            webdav_path=webdav_path,
            source_a_path=source_a_path,
            current_b_path=str(local))
        self.ensure_single_visible_instance(fingerprint, str(local), mapping_id=mapping_id)

    def _cloud_path_to_engine_paths(self, cloud_path: str) -> list[str]:
        result = []
        for entry_path, mapping in self.config.strm_storage_map.items():
            for mp in mapping.paths:
                if cloud_path.startswith(mp):
                    relative = cloud_path[len(mp.rstrip("/")):].lstrip("/")
                    engine_path = f"{entry_path.rstrip('/')}/{relative}" if relative else entry_path
                    result.append(engine_path)
                    break
        return result

    def request_openlist_index_update(
            self, _webdav_path: str, parent_webdav_path: str) -> None:
        engine_paths = self._cloud_path_to_engine_paths(parent_webdav_path)
        if not engine_paths:
            logging.debug(
                "[OpenListAdmin] 无法映射引擎路径，跳过索引更新: %s",
                parent_webdav_path)
            return
        if not self.admin_api.token:
            if not self.admin_api.login():
                error_msg = self.admin_api.last_error_message or "未知错误"
                logging.warning("[OpenListAdmin] 登录失败: %s，跳过索引更新", error_msg)
                return
        ok = self.admin_api.trigger_refresh_via_fs_list(engine_paths)
        if ok:
            logging.info("[OpenListAdmin] 已请求更新strm索引: %s", engine_paths)
        else:
            logging.warning("[OpenListAdmin] 索引更新触发失败: %s", engine_paths)

    def handle_b_renamed_to_non_strm(self, local_path: str) -> None:
        local = Path(local_path).resolve()
        lock = self.get_path_lock(local)
        with lock:
            row = self.db.get_b_by_local_full(str(local))
            if not row:
                return
            self.db.delete_b_by_local(str(local))
            logging.info("[B区重命名] .strm 重命名为非 .strm，已从数据库移除记录: %s", local_path)

    def handle_b_deleted(self, local_path: str) -> None:
        local = Path(local_path).resolve()
        lock = self.get_path_lock(local)
        with lock:
            row = self.db.get_b_by_local_full(str(local))
            if not row:
                return
            webdav_path = row.webdav_path
            parent_webdav_path = row.parent_webdav_path
            fingerprint = row.fingerprint
            with self._restoring_lock:
                # 恢复操作标记：程序自身正在恢复此指纹的文件
                if fingerprint in self._restoring_markers:
                    logging.info("[B区删除] 检测到程序恢复操作，跳过追删: %s", local_path)
                    return
                # 引擎内部删除标记（B-7）：隔离/去重/迁移等程序自身操作触发的
                # 物理删除，不应级联到不可逆的 WebDAV 源文件 + A 区源文件删除。
                if fingerprint in self._engine_internal_markers:
                    logging.info(
                        "[B区删除] 检测到程序内部删除（隔离/去重/迁移），跳过云删除与A区删除: %s",
                        local_path)
                    self.db.delete_b_by_local(str(local))
                    return
            mapping_id = row.mapping_id
            if not mapping_id:
                logging.warning("[B区删除] 记录缺少 mapping_id，跳过云端和 A 区删除: %s", local_path)
                self.db.delete_b_by_local(str(local))
                return
            if fingerprint and self.db.has_other_b_instance(mapping_id, fingerprint, str(local)):
                logging.info("[B区删除联动] B区中仍存在同指纹文件，跳过WebDAV删除: %s", local_path)
                self.db.delete_b_by_local(str(local))
                return
            if fingerprint and self._check_fingerprint_exists_in_b(
                    fingerprint,
                    exclude_path=str(local), mapping_id=mapping_id):
                logging.info(
                    "[B区删除联动] B区文件系统中仍存在同指纹文件，跳过WebDAV删除: %s",
                    local_path)
                self.db.delete_b_by_local(str(local))
                return
            if webdav_path:
                self._execute_webdav_deletion(webdav_path, parent_webdav_path)
                self._delete_a_file_by_webdav(webdav_path)
            self.db.delete_b_by_local(str(local))
            if fingerprint:
                self.refresh_identity_current_b_path(fingerprint, mapping_id)
            # 异步触发局部冗余检查：清理该父目录下的 B 区僵尸文件
            # 与 A 区删除保持一致的异步处理模式，避免阻塞 watchdog 事件处理线程
            if parent_webdav_path:
                self.trigger_delayed_cleanup(parent_webdav_path)

    def _check_fingerprint_exists_in_b(
            self, fingerprint: str, exclude_path: str | None = None,
            mapping_id: str | None = None) -> bool:
        if not mapping_id:
            return False
        b_instances = self.db.get_b_instances_by_fingerprint(fingerprint, mapping_id)
        for instance in b_instances:
            instance_path = instance.local_path
            if exclude_path and instance_path == exclude_path:
                continue
            if Path(instance_path).exists():
                return True
        return False

    def handle_b_moved(self, src_path: str, dest_path: str) -> None:
        """处理 B 区 .strm 重命名为 .strm 的事件（异步调用）。

        B-2：原 on_moved 在 watchdog 事件线程内同步调用 db.move_b_record，
        既不取路径锁也不经 _run_async，与同路径的 created/modified/deleted
        异步处理线程竞争，导致 move_b_record 的 SELECT→INSERT/DELETE 序列
        与并发插入/删除产生丢失更新（复活已删行 / 删掉刚插入的新行）。

        现统一异步化，并按规范化全序获取 src+dst 双路径锁（src_key<=dst_key），
        消除交叉重命名（X→Y 与 Y→X）的 AB-BA 死锁；src 与 dst 解析后相同时
        退化为单锁。
        """
        src = Path(src_path).resolve()
        dst = Path(dest_path).resolve()
        # 规范化全序取锁：按 key 字典序先取小者，避免交叉重命名死锁
        src_key = str(src)
        dst_key = str(dst)
        locks = [self.get_path_lock(src)]
        if dst_key != src_key:
            locks.append(self.get_path_lock(dst))
            # 保证获取顺序：小 key 在前
            if dst_key < src_key:
                locks.reverse()
        first = locks[0]
        second = locks[1] if len(locks) > 1 else None
        with first:
            ctx = (second if second is not None else _nullcontext())
            with ctx:
                moved = self.db.move_b_record(str(src), str(dst))
                if moved:
                    logging.info(
                        "[B区重命名] 已更新路径: %s -> %s",
                        src.name, dst.name)
                    webdav = read_strm_webdav_path(dst)
                    if webdav:
                        fp = make_strm_fingerprint(webdav)
                        mid = self._mapping_id_for_b(dst)
                        if mid:
                            self.refresh_identity_current_b_path(fp, mid)
                        else:
                            logging.warning("[B区重命名] 无法解析目标 mapping，跳过 projection 刷新: %s", dst)

    def _execute_webdav_deletion(
            self, webdav_path: str, parent_webdav_path: str) -> bool:
        logging.debug("[WebDAV删除] 进入，路径=%s, 父目录=%s", webdav_path, parent_webdav_path)
        # B-2: webdav 路径使用独立命名空间的锁（get_webdav_lock），
        # 避免与本地路径锁在 Windows 上因 Path().resolve() 碰撞。
        lock = self.get_webdav_lock(webdav_path)
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
            a_path = a_record.local_path
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

    def migrate_b_under_root_to_c(self, root_path: str) -> None:
        root_path = root_path.rstrip("/") or "/"
        logging.warning("[B区迁移→C区] 开始迁移根路径下的 B 区文件: %s", root_path)
        records = self.db.get_b_under_root(root_path)
        migrated_count = 0
        for record in records:
            local_path = record.local_path
            webdav_path = record.webdav_path
            source_a_path = record.source_a_path
            mapping_id = getattr(record, "mapping_id", "") or self._mapping_id_for_b(local_path)
            if not mapping_id:
                logging.warning("[B区迁移→C区] 无法解析 mapping，保留来源: %s", local_path)
                continue
            local = Path(local_path)
            if not local.exists():
                self.db.delete_b_by_local(local_path)
                continue
            mapping = self.get_mapping_for_b(local)
            if mapping is None or mapping[0] != mapping_id:
                logging.warning("[B区迁移→C区] B路径 mapping 不一致，保留来源: %s", local_path)
                continue
            try:
                target = self.get_c_path_for_b(mapping_id, local, mapping[1])
            except ValueError as exc:
                logging.warning("[B区迁移→C区] 无法生成安全 C 目标，保留来源: %s (%s)", local_path, exc)
                continue
            if target.exists():
                source_identity = read_strm_webdav_path(local)
                target_identity = read_strm_webdav_path(target)
                if not source_identity or not target_identity or source_identity != target_identity:
                    logging.warning("[B区迁移→C区] C目标身份未知或异源，保留来源: %s", local_path)
                    continue
                if not safe_remove_file(local):
                    logging.warning("[B区迁移→C区] 同源来源清理失败，保留来源: %s", local_path)
                    continue
                self.db.delete_b_by_local(local_path)
                migrated_count += 1
                continue
            try:
                move_file(local, target)
                self.db.upsert_c(
                    str(target),
                    webdav_path,
                    local_path,
                    webdav_parent(webdav_path),
                )
                self.db.delete_b_by_local(local_path)
                if fingerprint := make_strm_fingerprint(webdav_path):
                    self.refresh_identity_current_b_path(fingerprint, mapping_id)
                migrated_count += 1
                logging.info("[B区迁移→C区] %s -> %s", local_path, target)
            except OSError as exc:
                logging.warning(
                    "[B区迁移→C区] 迁移失败，保留来源: %s -> %s (%s)",
                    local_path,
                    target,
                    exc,
                )
        if migrated_count:
            logging.warning("[B区迁移→C区] 完成迁移，共处理 %s 个文件", migrated_count)

    def cleanup_b_zombies_under_folder(self, root_path: str) -> None:
        """清理指定目录下的 B 区僵尸文件（云端已删除但本地残留的文件）
        
        优化策略：按父目录分组，使用 list_directory() 批量获取云端文件列表，
        在内存中进行集合比对，避免逐条 check_exists() 调用。
        
        性能对比：
        - 原方案：N 条记录 × 1 次 check_exists() = N 次 API 调用
        - 新方案：M 个父目录 × 1 次 list_directory() = M 次 API 调用
        - 优化效果：当 N >> M 时（如 1000 条记录在 10 个目录下），API 调用从 1000 次降至 10 次
        """
        import posixpath
        root_path = root_path.rstrip("/") or "/"
        logging.info("[B区僵尸清理] 开始扫描: %s", root_path)
        records = self.db.get_b_under_root(root_path)
        if not isinstance(records, (list, tuple)):
            logging.warning("[B区僵尸清理] DB 返回不可迭代记录，跳过: %s", root_path)
            return
        if not records:
            logging.info("[B区僵尸清理] 目录下无记录，跳过: %s", root_path)
            return
        
        # 按父目录分组
        parent_to_records = {}
        for record in records:
            if not record.webdav_path:
                continue
            parent = webdav_parent(record.webdav_path)
            if parent not in parent_to_records:
                parent_to_records[parent] = []
            parent_to_records[parent].append(record)
        
        # 批量检查每个父目录
        removed_count = 0
        for parent, parent_records in parent_to_records.items():
            # 一次性获取该目录下的所有云端文件
            cloud_files = self._collect_cloud_files_in_directory(parent)
            if cloud_files is None:
                # API 调用失败，跳过该目录
                logging.warning("[B区僵尸清理] 无法获取云端文件列表: %s", parent)
                continue
            
            # 在内存中比对
            for record in parent_records:
                if record.webdav_path in cloud_files:
                    continue
                # 云端不存在，处理僵尸文件
                full_row = self.db.get_b_by_local_full(record.local_path)
                fingerprint = full_row.fingerprint if full_row else None
                self._handle_b_zombie(record.local_path, record.webdav_path, fingerprint)
                removed_count += 1
        
        if removed_count:
            logging.warning("[B区僵尸清理] 完成清理，共处理 %s 个文件", removed_count)
    
    def _collect_cloud_files_in_directory(self, directory_path: str) -> set[str] | None:
        """获取指定目录下的所有文件的完整 WebDAV 路径集合。

        返回权威完整集合；若响应不可信则返回 None（fail-closed）。

        参考 docs/openlist_api_fs_list_contract.md §4（per_page=100）。

        Args:
            directory_path: 目录的 WebDAV 路径

        Returns:
            set[str] | None: 文件路径集合或 None（不可信）
        """
        import posixpath
        result = set()
        page = 1
        per_page = 100  # 对齐 docs maximum:100

        while page <= 100:  # 安全阀
            res = self.admin_api.list_directory(
                directory_path, page=page, per_page=per_page)
            parsed = self._parse_fs_list_content(res)
            if parsed is None:
                return None
            content, _total = parsed

            for item in content:
                if isinstance(item, dict) and not item.get("is_dir", False):
                    file_name = item.get("name", "")
                    if file_name:
                        full_path = posixpath.join(directory_path, file_name)
                        result.add(full_path)

            if len(content) < per_page:
                break
            page += 1

        # 安全阀耗尽：fail-closed（不返回部分集）
        if page > 100:
            logging.warning(
                "[B区僵尸清理] 安全阀耗尽(%s)，视为不可信", directory_path)
            return None

        return result

    def refresh_identity_current_b_path(self, fingerprint: str, mapping_id: str | None = None) -> None:
        if not fingerprint:
            return
        if not mapping_id:
            logging.warning("[身份投影] 缺少 mapping_id，跳过刷新: %s", fingerprint)
            return
        identity = self.db.get_identity_by_fingerprint(fingerprint)
        b_instances = self.db.get_all_b_by_fingerprint(fingerprint, mapping_id)
        valid_instances = [
            row for row in b_instances
            if row.status == "valid" and Path(row.local_path).exists()
        ]
        if not valid_instances:
            self.db.delete_identity_projection(fingerprint, mapping_id)
            return
        valid_instances.sort(key=lambda row: self._b_file_score(row.local_path))
        best = valid_instances[0]
        self.db.upsert_identity_projection(
            fingerprint, mapping_id, best.local_path, "visible")
        if identity:
            self.db.update_identity_b_path(fingerprint, best.local_path)
        else:
            self.db.upsert_identity(
                fingerprint=fingerprint,
                webdav_path=best.webdav_path,
                source_a_path=best.source_a_path,
                current_b_path=best.local_path,
            )

    def _maybe_record_boundary_mapping(
            self, local: Path, webdav_path: str, fingerprint: str) -> None:
        if not webdav_path or not fingerprint or not local.exists():
            return
        b_root = self.get_b_root_for_path(local)
        if b_root is None:
            return
        try:
            local_rel = local.resolve().relative_to(b_root)
            physical_media_folder_name = None
            for i, part in enumerate(local_rel.parts):
                if re.match(r"(?i)^season\s*\d+$", part):
                    if i > 0:
                        physical_media_folder_name = local_rel.parts[i - 1]
                    break
            if physical_media_folder_name is None and local_rel.parts:
                physical_media_folder_name = local_rel.parts[-1]
        except Exception:
            return

        cloud_parts = [p for p in webdav_path.rstrip("/").split("/") if p]
        cloud_show_name = None
        for i, part in enumerate(cloud_parts):
            if re.match(r"(?i)^season\s*\d+$", part):
                if i > 0:
                    cloud_show_name = cloud_parts[i - 1]
                break
        if cloud_show_name is None and len(cloud_parts) >= 2:
            cloud_show_name = cloud_parts[-2]

        if not cloud_show_name or not physical_media_folder_name:
            return
        if cloud_show_name == physical_media_folder_name:
            return
        mapping = self.get_mapping_for_b(local)
        if mapping is None:
            logging.warning("[边界映射] 无法解析 mapping，跳过记录: %s", local)
            return
        mapping_id = mapping[0]
        self.db.upsert_media_boundary(
            mapping_id=mapping_id,
            fingerprint=fingerprint,
            source_media_name=cloud_show_name,
            current_media_name=physical_media_folder_name,
            engine_entry_path=str(b_root),
        )
        logging.info(
            "[边界映射] 记录媒体映射: %s -> %s",
            cloud_show_name,
            physical_media_folder_name,
        )

    def _handle_b_zombie(
        self,
        local_path: str,
        webdav_path: str | None = None,
        fingerprint: str | None = None,
    ) -> None:
        """处理 B 区僵尸文件（本地文件已删除但 B 区仍存在）。
        
        Args:
            local_path: 本地文件路径
            webdav_path: WebDAV 路径（可选，用于设置幽灵保护）
            fingerprint: 文件指纹（可选，用于刷新身份记录）
        """
        if not local_path:
            return
        local = Path(local_path)
        # B-7 删除归因：先删 DB 记录，再删物理文件。
        # 反序原顺序以消除竞态窗口：若先 safe_remove_file，其触发的 on_deleted
        # 事件会让 handle_b_deleted 在 DB 行仍存在时找到记录并误判为用户删除，
        # 连带触发不可逆的 WebDAV 源文件 + A 区源文件删除。先删 DB 行后，
        # handle_b_deleted 的 get_b_by_local_full 返回 None → 提前返回，不级联。
        mapping_id = self._mapping_id_for_b(local)
        self.db.delete_b_by_local(str(local))
        if local.exists():
            safe_remove_file(local)
            if fingerprint and mapping_id:
                self.refresh_identity_current_b_path(fingerprint, mapping_id)
        if webdav_path:
            self.db.set_ghost_protection(
                webdav_path,
                self.config.behavior.ghost_protect_seconds,
                reason="b_zombie",
            )

    def trigger_delayed_cleanup(self, parent_webdav_path: str) -> None:
        if not parent_webdav_path:
            return
        with self._cleanup_lock:
            old_timer = self._pending_cleanups.pop(parent_webdav_path, None)
            if old_timer:
                old_timer.cancel()
            timer = threading.Timer(
                self.config.behavior.a_to_b_restore_delay_seconds,
                self._cleanup_b_zombies_under_folder_safe,
                args=(parent_webdav_path,),
            )
            timer.daemon = True
            self._pending_cleanups[parent_webdav_path] = timer
            timer.start()

    def _cleanup_b_zombies_under_folder_safe(self, parent_webdav_path: str) -> None:
        """安全执行 B 区僵尸清理，完成后自动清理定时器引用"""
        try:
            self.cleanup_b_zombies_under_folder(parent_webdav_path)
        finally:
            with self._cleanup_lock:
                self._pending_cleanups.pop(parent_webdav_path, None)

    def _build_trash_path(self, cloud_path: str) -> str | None:
        return build_webdav_trash_path(
            cloud_path, self.config.behavior.trash_dir_name)

    def _ensure_trash_dirs(self, trash_path: str) -> bool:
        """确保 WebDAV 回收站目录存在（递归逐层创建远程目录）。

        trash_path 示例:
            /天翼云盘家庭云30GB/strm_回收站_测试/番剧/[1998] 头文字D/Season 1/S01E01.mkv

        需要依次创建:
            /天翼云盘家庭云30GB/strm_回收站_测试
            /天翼云盘家庭云30GB/strm_回收站_测试/番剧
            ...
            /天翼云盘家庭云30GB/strm_回收站_测试/番剧/[1998] 头文字D/Season 1
        """
        try:
            parts = [p for p in trash_path.rstrip("/").split("/") if p]
            if len(parts) < 3:
                logging.warning("[回收站] 路径层级不足，跳过目录创建: %s", trash_path)
                return True

            # 从根目录开始逐层创建，跳过第一级（根挂载点，通常已存在）
            # 例如 parts = ["天翼云盘家庭云30GB", "strm_回收站_测试", "番剧", ..., "Season 1", "S01E01.mkv"]
            # 文件名最后一级不需要创建目录
            dir_parts = parts[:-1]  # 去掉文件名

            for depth in range(2, len(dir_parts) + 1):
                sub_path = "/" + "/".join(dir_parts[:depth])
                logging.debug("[回收站] 逐层创建目录: %s", sub_path)
                ok = self.admin_api.mkdir(sub_path)
                if not ok:
                    logging.warning("[回收站] 目录创建失败: %s (将尝试继续)", sub_path)
                    # mkdir 在目录已存在时仍返回 True（见 webdav_client.py）
                    # 如果真的创建失败，继续尝试下一层，最坏情况由 move API 报错

            return True
        except Exception as e:
            logging.error("[回收站] 递归创建目录异常: %s", e)
            return False
