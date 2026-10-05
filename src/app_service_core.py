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
from collections import defaultdict
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
    canonicalize_webdav_path,
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

# B 区历史记录核对期间批量快照写入的缓冲行数（每满 N 条刷新一次，
# 循环结束的 finally 中做最终刷新）
B_SNAPSHOT_BATCH_SIZE = 1000

# AppService 生命周期阶段常量。状态由 get_state_summary() 对外提供，
# WebUI 仅据此判断是否正在启动、是否允许 READY 后的操作。
PHASE_NOT_CONFIGURED = "not_configured"
PHASE_STOPPED = "stopped"
PHASE_STARTING = "starting"
PHASE_AUTHENTICATING = "authenticating"
PHASE_SCANNING_A = "scanning_a"
PHASE_SCANNING_B = "scanning_b"
PHASE_SYNCING_A_TO_B = "syncing_a_to_b"
PHASE_CATCHING_UP = "catching_up"
PHASE_READY = "ready"
PHASE_STOPPING = "stopping"
PHASE_FAIL_SAFE = "fail_safe"

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
            # 远端 API 可能返回 "SaveLocalMode": null，.get 默认值不生效，
            # 需显式类型守卫，否则 is_sync_mode 调 .lower() 会抛 AttributeError
            val = addition_dict.get("SaveLocalMode")
            return val if isinstance(val, str) else ""
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
      3. _fingerprint_locks[fp]（按 fingerprint 串行化 A→B 处理 / B 删除防竞态）
      4. _dav_write_lock（WebDAV 写操作）
      5. _cleanup_lock（延迟清理定时器管理）
      6. _restoring_lock（恢复标记 / 引擎内部删除标记）
      7. _lineage_log_lock（日志记录）
     规则：只能按编号从小到大获取，释放时反向；禁止同时持有非相邻的锁。
    （注：原 _b_file_lock 已移除——B 区移动/修复改由 get_path_lock 按路径串行化。）
    """

    def __init__(self, config: AppConfig, db: Database,
                 admin_api: OpenListAdminClient) -> None:
        self.config = config
        self.db = db
        self.admin_api = admin_api
        self._observers: list[object] = []
        self._watcher_handlers: list = []  # C1/C3：已挂载的 A/B handler，stop 时统一 close()
        self.observer: Any = None
        self._running = False
        self._state_lock = threading.Lock()
        self._current_phase = PHASE_STOPPED
        self._phase_error: str | None = None
        self._progress_stats: dict[str, Any] = {
            "a_discovered": 0,
            "a_indexed": 0,
            "b_discovered": 0,
            "b_reconciled": 0,
            "synced_records": 0,
            "start_time": 0.0,
            "updated_at": 0.0,
        }
        self._startup_cancel_event = threading.Event()
        self._startup_generation = 0
        self.startup_generation: int = 0
        self._subtitle_scan_cancel_event = threading.Event()
        self._subtitle_scan_thread: threading.Thread | None = None
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
        # 代际计数器：与 _engine_internal_generation 相同模式
        self._restoring_generation: dict[str, int] = {}
        # 删除归因：引擎内部操作（隔离/清理）删除 B 文件时标记 fingerprint，
        # handle_b_deleted 检测到此标记即跳过不可逆的云删除 + A 区删除，
        # 仅清理本地 DB 行。避免引擎隔离/僵尸清理被误判为用户删除而连累云源。
        self._engine_internal_markers: set[str] = set()
        # 代际计数器：为延迟清除提供重入安全
        # 每次标记递增，延迟清除时检查：若代际已变化则不清除（有新的标记发生）
        self._engine_internal_generation: dict[str, int] = {}
        self._restoring_lock = threading.Lock()
        # 扫描阶段快照预计算根列表 [(mapping_id, norm_a_root, norm_b_root), ...]
        self._scan_mapping_roots: list[tuple[str, Path, Path]] | None = None
        self._lineage_log_lock = threading.Lock()
        self._lineage_log_keys: set[str] = set()
        self._webdav_scan_logged: set[str] = set()
        # 按 fingerprint 串行化 A→B 处理，避免 TOCTOU 竞争
        self._fingerprint_locks_lock = threading.Lock()
        self._fingerprint_locks: dict[str, threading.Lock] = {}
        # [已废弃] WebUI 媒体刷新锁已移至 WebUIServer.__init__ 的 _refresh_lock，
        # routes.py 使用 handler.webui._refresh_lock。此处保留注释以说明迁移。
        # self._refresh_lock = threading.Lock()
        # Watchdog 健康状态标志 - 由 area_watchers 设置，dashboard 读取并显示
        self._watchers_healthy = True
        self.sync_service = SyncService(self)
        self.subtitle_handler = SubtitleHandler(self)
        self._mapping_version = mapping_version(self.a_b_mappings, self.c_root)
        # B 区历史记录核对期间的只读预载缓存（仅在 _reconcile_b_historical_records
        # 生命周期内存在，循环结束清理；缓存为 None 时所有查询回退 DB）。
        # 键约定见 _build_reconcile_cache。
        self._reconcile_cache: dict | None = None
        self._catch_up_delta: list[dict[str, Any]] = []
        # b_orphan_count 的 (timestamp, count) TTL 缓存；None = 未缓存。
        # c.9.2 Task 5：__init__ 显式初始化，移除 _get_b_orphan_count 的
        # getattr 防御（样式级，行为不变）。
        self._b_orphan_cache: tuple[float, int] | None = None

    def set_startup_generation(self, generation: int) -> None:
        """注入新的启动代次，并使当前启动流程失效。"""
        generation = int(generation)
        if generation != self.startup_generation:
            self.startup_generation = generation
            self._startup_generation += 1

    def _startup_generation_valid(self, token: int, generation: int) -> bool:
        return (
            not self._startup_cancel_event.is_set()
            and self._startup_generation == token
            and self.startup_generation == generation
        )

    def _abort_stale_startup(self) -> bool:
        self._running = False
        self.set_phase(PHASE_STOPPED, error="启动代次已失效")
        return False

    def set_phase(self, phase: str, error: str | None = None) -> None:
        """线程安全地更新生命周期阶段与错误信息。"""
        with self._state_lock:
            self._current_phase = phase
            self._phase_error = error
            now = time.time()
            self._progress_stats["updated_at"] = now
            if phase in {
                    PHASE_STARTING, PHASE_AUTHENTICATING, PHASE_SCANNING_A,
                    PHASE_SCANNING_B, PHASE_SYNCING_A_TO_B, PHASE_CATCHING_UP,
            } and self._progress_stats["start_time"] <= 0.0:
                self._progress_stats["start_time"] = now
            elif phase in {PHASE_STOPPED, PHASE_FAIL_SAFE}:
                self._progress_stats["start_time"] = 0.0

    def update_progress(self, **kwargs: Any) -> None:
        """原子更新启动进度指标。"""
        with self._state_lock:
            self._progress_stats.update(kwargs)
            self._progress_stats["updated_at"] = time.time()

    # 缺陷 D（c7 §三）B 区孤儿只读观测：R-6 纪律——get_state_summary 持
    # _state_lock，孤儿 COUNT 不得放锁内；锁外计算 + 60s TTL 缓存 + 异常
    # 回退 None（防 dashboard 高频轮询把冷启动专项变成持锁慢查询）。
    _B_ORPHAN_TTL_SECONDS = 60.0

    def _get_b_orphan_count(self) -> int | None:
        """锁外查询 B 区孤儿计数（webdav_path 口径，TTL 缓存）。

        失败回退 None（不阻塞 dashboard）；常态值 = 0（X-9 的 ≈6529 空 mid
        行是另一口径，并非常态孤儿值，两数不可混用）。
        """
        now = time.monotonic()
        cached = self._b_orphan_cache
        if cached is not None and now - cached[0] < self._B_ORPHAN_TTL_SECONDS:
            return cached[1]
        try:
            count = self.db.count_b_orphans()
        except Exception:
            logging.debug("[观测] B 区孤儿计数查询失败", exc_info=True)
            return None
        self._b_orphan_cache = (now, count)
        return count

    def get_state_summary(self) -> dict[str, Any]:
        """返回生命周期状态和进度的独立快照。"""
        # R-6 纪律：孤儿 COUNT 在进入 _state_lock 之前锁外计算
        b_orphan_count = self._get_b_orphan_count()
        with self._state_lock:
            phase = self._current_phase
            is_running = phase in {
                PHASE_STARTING, PHASE_AUTHENTICATING, PHASE_SCANNING_A,
                PHASE_SCANNING_B, PHASE_SYNCING_A_TO_B, PHASE_CATCHING_UP,
                PHASE_READY,
            }
            start_time = self._progress_stats["start_time"]
            elapsed = (
                time.time() - start_time
                if is_running and start_time > 0.0 else 0.0
            )
            pending_candidates = sum(
                item.get("confidence") == "candidate"
                for item in self._catch_up_delta)
            unknown = sum(
                item.get("confidence") == "unknown"
                for item in self._catch_up_delta)
            return {
                "phase": phase,
                "is_running": is_running,
                "is_ready": phase == PHASE_READY,
                "fail_safe": phase == PHASE_FAIL_SAFE,
                "error": self._phase_error,
                "progress": {
                    "a_discovered": self._progress_stats["a_discovered"],
                    "a_indexed": self._progress_stats["a_indexed"],
                    "b_discovered": self._progress_stats["b_discovered"],
                    "b_reconciled": self._progress_stats["b_reconciled"],
                    "synced_records": self._progress_stats["synced_records"],
                    "elapsed_seconds": round(elapsed, 2),
                },
                "catch_up": {
                    "pending_candidates": pending_candidates,
                    "unknown": unknown,
                    "generation": self.startup_generation,
                },
                # 缺陷 D（c7 §三）：B 区孤儿只读观测（dashboard 可见，不动盘）；
                # 查询失败为 None（观测不可用 ≠ 孤儿为 0，消费方须区分）
                "b_orphan_count": b_orphan_count,
            }

    # [已废弃] v6 R2-A 启动期 catch-up 单扫化：本方法的 watchers 前全盘扫描
    # 已由 _reconcile_boundary_catch_up（watchers 后唯一单扫）严格覆盖——
    # 磁盘@#3 晚于磁盘@#2 且 DB@#3 口径相同，_catch_up_delta 全库唯一消费者是
    # get_state_summary 的两个 dashboard 计数（纯观测、无行动型消费）。
    # 保留空壳而非物理删除：基准（benchmark_startup_pipeline /
    # benchmark_incremental）stage 计时档继续可跑（零成本），历史可比性以
    # boundary 口径延续；引用面收敛为 start() 顺序 + 两处测试修订。
    # 见 docs/否决方案.md「启动期 catch-up 单扫化」。
    def _reconcile_catch_up(self) -> None:
        """只读扫描 B 区并记录新增/未索引候选。

        [已废弃] R2-A 后为 no-op 空壳；启动收口观测由
        `_reconcile_boundary_catch_up` 单扫完成。
        """
        logging.debug("[启动收口] Catch-up 前置扫描已废弃（R2-A 单扫化），跳过")

    def _reconcile_boundary_catch_up(self) -> None:
        """Watcher 挂载窗口后的只读边界补扫——启动期唯一的 catch-up 单扫。

        v6 R2-A：本扫描覆盖 `initial_scan_b` 以来全部启动窗口（含
        A→B 同步、启动清扫与 watcher 挂载间隙），不再存在 watchers
        前的第二遍全盘扫描。

        N1-alt（c7 §5.2）惰性差集化：先 `_load_b_db_records()` 取 DB 路径集，
        再仅 `rglob("*.strm")` 枚举（不读内容）求磁盘−DB 差集，仅对差集行
        读内容构造 `path_to_data`——boundary 二扫从"全盘内容读"降为
        "枚举 + 差集内容读"（Task 0 实测全读 6.58s，稳态差集为空时 ≈枚举成本）。
        语义护栏：
        - U-2：DB 加载失败（None）→ WARNING + 直接 return，不回退全读
          （磁盘枚举失败 ≠ DB 失败；现行先盘后 DB 会在 None 时白读全盘一次）；
        - 仅 rglob 枚举异常才整体回退现行 `_scan_b_disk` 全读；
        - `path_str` 键形态与入库 `str(rglob)` 完全一致（M-11）；
        - 契约零改动：`_build_catch_up_delta`/`_queue_catch_up_delta`/
          `get_state_summary` 不动。
        """
        try:
            # U-2：先 DB 后磁盘，DB 失败直接 return（不白读全盘）
            db_records = self._load_b_db_records()
            if db_records is None:
                logging.warning("[启动收口] 边界补扫跳过：B 区数据库记录加载失败")
                return
            db_paths = {getattr(r, "local_path", None) for r in db_records}
            disk_paths, enum_ok = self._enumerate_b_strm_paths()
            if enum_ok:
                delta_paths = [p for p in disk_paths if p not in db_paths]
                path_to_data = self._read_b_content_for_paths(delta_paths)
                # _build_catch_up_delta 只消费 path_to_data（第二个元素），
                # 指纹→路径集合对本 delta 无用，置空即可（契约零改动）。
                disk_data: tuple[dict, dict] = ({}, path_to_data)
                logging.info(
                    "[启动收口] 边界补扫(N1-alt): 枚举 %d, 差集 %d, 内容读 %d",
                    len(disk_paths), len(delta_paths), len(path_to_data))
            else:
                # 仅枚举异常才回退现行全读（磁盘枚举失败 ≠ DB 失败）
                disk_data = self._scan_b_disk()
                if disk_data is None:
                    return
            self._queue_catch_up_delta(
                self._build_catch_up_delta(disk_data, db_records), merge=True)
        except Exception as e:
            logging.warning("[启动收口] 边界补扫异常: %s", e)

    def _enumerate_b_strm_paths(self) -> tuple[list[str], bool]:
        """N1-alt：逐 b_root 枚举 .strm 路径（不读内容）。

        复刻 `_scan_b_disk` 的 `b_root.exists()` 跳过语义；`path_str` 键形态
        与入库 `str(rglob)` 完全一致（均为 str(Path)）。任一 root 枚举异常
        → WARNING + 返回 (已收集路径, False) 触发整体回退全读。
        """
        all_paths: list[str] = []
        try:
            for b_root in self._a_to_b_map.values():
                if not b_root.exists():
                    logging.info("[初始化] B 区根目录不存在，跳过: %s", b_root)
                    continue
                all_paths.extend(str(p) for p in b_root.rglob("*.strm"))
        except OSError as e:
            logging.warning("[启动收口] B 区枚举失败，回退全盘读: %s", e)
            return [], False
        return all_paths, True

    def _read_b_content_for_paths(self, paths: list[str]) -> dict[str, dict]:
        """N1-alt：仅对差集路径并发读内容，返回 path_to_data。

        读取语义与 `_scan_b_disk` 完全同构（8 线程、read_strm_webdav_path、
        make_strm_fingerprint）；解析失败（None）行按现行 `continue` 丢弃。
        """
        result: dict[str, dict] = {}
        if not paths:
            return result

        def _read_one(path_str: str) -> tuple[str, str, str] | None:
            webdav_path = read_strm_webdav_path(Path(path_str))
            if webdav_path:
                return (path_str, webdav_path, make_strm_fingerprint(webdav_path))
            return None

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(_read_one, p): p for p in paths}
            for future in as_completed(futures):
                strm_path = futures[future]
                try:
                    read_result = future.result()
                except Exception as e:
                    logging.warning(
                        "[启动收口] 读取 B 区文件失败: %s (%s)", strm_path, e)
                    continue
                if read_result is None:
                    continue
                path_str, webdav_path, fingerprint = read_result
                result[path_str] = {"webdav": webdav_path, "fp": fingerprint}
        return result

    def _cleanup_startup_duplicates(self) -> None:
        """启动级重复清扫（Task 6，闭合 R1 重试盲区）。

        在 scan_a_to_b_full_sync 之后、start_watchers() 之前执行一次全表 GROUP BY，
        对 status='valid' 且 COUNT(*) > 1 的 (mapping_id, fingerprint) 组调用
        ensure_single_visible_instance 进行隔离。覆盖本次新增重复与此前失败启动
        遗留的已提交批次重复实例。
        - 仅统计 valid 行（避免对 stuck duplicate 行空转建连）。
        - 单条隔离异常记 ERROR 并继续处理后续组（不阻断正常启动）。
        - 设计决策: 启动上下文（watchers 未启动）时复用本方法已查出的 dup_groups
          （C12-2：不二次查询）经 quarantine_session 会话批量路径等价执行（T1）；
          该清扫同时是批量隔离路径的组内重试兜底（已知取舍，登记册）。
        """
        try:
            dup_groups = self.db.get_duplicate_fingerprint_groups()
        except Exception as e:
            logging.warning("[启动清扫] 查询重复指纹组失败，跳过清扫: %s", e)
            return
        if not dup_groups:
            # U-1（c7 §三，X-8 人工门基准）：升 INFO——INFO 日志下观测须能
            # 区分"清扫 0 组"与"清扫未运行"；文案含「，跳过清扫」后缀（F-7
            # 精确文案，grep 断言须用全串）
            logging.info("[启动清扫] 无重复指纹组，跳过清扫")
            return
        logging.info("[启动清扫] 发现 %d 个重复指纹组，开始隔离...", len(dup_groups))
        if not self._watchers_live():
            # 批量路径内部对预读/建连失败回退逐条（C13-4）
            self._quarantine_duplicate_groups_batch(
                [(mapping_id, fingerprint, sample_path)
                 for mapping_id, fingerprint, sample_path in dup_groups])
            return
        for mapping_id, fingerprint, sample_path in dup_groups:
            try:
                self.ensure_single_visible_instance(
                    fingerprint, sample_path, mapping_id=mapping_id)
            except Exception as e:
                logging.error(
                    "[启动清扫] 隔离失败 mid=%s fp=%s: %s（继续处理后续组）",
                    mapping_id, fingerprint, e, exc_info=True)

    def _build_catch_up_delta(self, disk_data: tuple[dict, dict], db_records: list) -> list[dict[str, Any]]:
        """只读对比磁盘与数据库，生成 created_or_unindexed 候选。"""
        _, path_to_data = disk_data
        db_paths = {getattr(r, "local_path", None) for r in db_records}
        delta: list[dict[str, Any]] = []
        for path_str, data in path_to_data.items():
            if path_str in db_paths:
                continue
            data = data if isinstance(data, dict) else {}
            mapping = self.get_mapping_for_b(path_str)
            mapping_id = mapping[0] if mapping is not None else None
            path = Path(path_str)
            try:
                stat = path.stat()
                mtime_ns, size = stat.st_mtime_ns, stat.st_size
            except OSError:
                mtime_ns, size = None, None
            fingerprint = data.get("fingerprint", data.get("fp"))
            webdav_path = data.get("webdav_path", data.get("webdav"))
            confidence = "candidate" if mapping_id and fingerprint and webdav_path else "unknown"
            delta.append({
                "action_kind": "created_or_unindexed",
                "action": "created_or_unindexed",
                "local_path": path_str,
                "mapping_id": mapping_id,
                "fingerprint": fingerprint,
                "webdav_path": webdav_path,
                "observed_at": time.time(),
                "mtime_ns": mtime_ns,
                "size": size,
                "source_generation": self.startup_generation,
                "confidence": confidence,
                "requires_destructive_action": False,
            })
        return delta

    def _queue_catch_up_delta(self, delta: list[dict[str, Any]], *, merge: bool = False) -> None:
        """记录并按本地路径去重的只读候选列表。"""
        items = self._catch_up_delta if merge else []
        by_path = {item.get("local_path"): item for item in items}
        for item in delta:
            by_path[item.get("local_path")] = item
        self._catch_up_delta = list(by_path.values())

    def _current_mapping_ids(self) -> list[str]:
        """返回去重非空的 mapping_id 集合，供 generation 推进使用。"""
        return list({
            str(m.mapping_id).strip()
            for m in self.a_b_mappings
            if str(getattr(m, "mapping_id", "")).strip()
        })

    def _refresh_mapping_snapshot(self) -> None:
        """OpenList 热更新后从当前 config 重新推导 mapping 快照。

        热更新只刷新了 config，未同步 AppService 内存中的
        a_b_mappings/a_roots/_a_to_b_map/_mapping_version，导致引擎（血统快照、
        清理、迁移）仍用旧路径/旧 mapping_version。原子更新：全部在同一调用内
        从 config 重新推导，任一步失败不产生半更新状态。
        """
        config = self.config
        a_b_mappings = getattr(config, "a_b_mappings", [])
        new_mappings = (
            a_b_mappings if isinstance(a_b_mappings, list) else [])
        new_roots = [
            normalize_local_root(m.a_root) for m in new_mappings]
        new_a_to_b = {
            str(normalize_local_root(m.a_root)): normalize_local_root(m.b_root)
            for m in new_mappings
            if getattr(m, "mapping_id", "")
            and getattr(m, "a_root", "")
            and getattr(m, "b_root", "")
        }
        new_version = mapping_version(new_mappings, self.c_root)
        # 原子替换：先本地计算完，再一次性赋值，避免中途异常留下不一致状态。
        self.a_b_mappings = new_mappings
        self.a_roots = new_roots
        self._a_to_b_map = new_a_to_b
        self._mapping_version = new_version
        # 与启动路径一致：新 mapping_version 持久化，供血统快照校验。
        try:
            self.db.set_mapping_version(new_version)
        except Exception as e:
            logging.warning("[热更新] mapping_version 持久化失败: %s", e)
        logging.info(
            "[热更新] mapping 快照已刷新: %d 组映射, mapping_version=%s",
            len(new_mappings), new_version)

    # b_root 不作为生产同步、清理、迁移或血统推导的 fallback。
    # 保留只读属性以兼容外部旧调用，但调用方必须先解析唯一 mapping。
    @property
    def b_root(self) -> Path:
        return next(iter(self._a_to_b_map.values())) if self._a_to_b_map else Path()

    @property
    def c_root(self) -> Path:
        return Path(self.config.paths.c_root).resolve() if self.config.paths.c_root else Path()

    def _mark_engine_internal(self, fingerprint: str) -> None:
        """标记 fingerprint 为引擎内部删除。

        handle_b_deleted 检测到此标记即跳过不可逆的云删除 + A 区删除。
        与 _restoring_markers 共用 _restoring_lock 串行化。
        递增代际计数器，使之前已调度的延迟清除不会误清理。
        """
        if fingerprint:
            with self._restoring_lock:
                self._engine_internal_markers.add(fingerprint)
                self._engine_internal_generation[fingerprint] = \
                    self._engine_internal_generation.get(fingerprint, 0) + 1

    def _clear_engine_internal(self, fingerprint: str) -> None:
        """清除引擎内部删除标记。"""
        if fingerprint:
            with self._restoring_lock:
                self._engine_internal_markers.discard(fingerprint)

    def _clear_engine_internal_delayed(self, fingerprint: str, delay: float = 10.0) -> None:
        """延迟清除引擎内部删除标记。

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
        """获取路径锁。
        
        锁字典有容量上限（10000），超过时清空防止内存泄漏。
        """
        key = str(Path(path).resolve())
        with self._path_locks_lock:
            lock = self._path_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._path_locks[key] = lock
            # 容量上限 - 超过 10000 时告警（弃用 clear 防锁引用失效）
            # 锁字典容量告警（已弃用 clear 方案）
            if len(self._path_locks) > 10000:
                logging.warning("[Lock] _path_locks 容量 %d", len(self._path_locks))
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
            # 容量上限 - 超过 10000 时告警（弃用 clear 防锁引用失效）
            # 锁字典容量告警（已弃用 clear 方案）
            if len(self._path_locks) > 10000:
                logging.warning("[Lock] _path_locks 容量 %d", len(self._path_locks))
        return lock

    def get_fingerprint_lock(self, fingerprint: str) -> threading.Lock:
        """按 fingerprint 获取锁，用于串行化同一媒体的 A→B 处理。"""
        with self._fingerprint_locks_lock:
            lock = self._fingerprint_locks.get(fingerprint)
            if lock is None:
                lock = threading.Lock()
                self._fingerprint_locks[fingerprint] = lock
            # 容量上限 - 超过 10000 时告警（弃用 clear 防锁引用失效）
            # 锁字典容量告警（已弃用 clear 方案）
            if len(self._fingerprint_locks) > 10000:
                logging.warning("[Lock] _fingerprint_locks 容量 %d", len(self._fingerprint_locks))
        return lock

    def is_path_under_any_root(self, path: str, roots: list[str]) -> bool:
        if not path or path == "/":
            return False
        normalized_path = path.rstrip("/") or "/"
        # Windows 下路径大小写不敏感，统一小写比较
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
            # c.9.3 Task A（O-1）：根级可观测性恢复——旧实现在此无条件输出
            # WARNING，三态门版曾仅在 confirmed_missing 路径输出，导致
            # cleanup_allowed=False 的根列表失败时零日志。文案中性化（只陈述
            # 「列表失败/不可信」，不预判存在性）：verdict=True（根确认存在、
            # 仅列表不可信）时不得输出与三态门结论矛盾的「不存在」假日志。
            logging.warning("[WebDAV刷新] 根路径列表失败或不可信: %s", root_path)
            # R-新5（c.9.2 Task 10）：递归返回 False 只说明「列表请求失败/
            # 超时/不可信」（list_contents 把「目录不存在」与「请求失败」
            # 合并映射为 None），不等于权威不存在。迁移前经 Admin 客户端
            # 三态 check_exists 复核（非 OpenlistWebDAV 两态版）：仅权威
            # is False 才迁移；True/None fail-closed 跳过——宁可漏迁不可
            # 误迁（一次网络超时不该把整根 B 区物理迁 C 区）。
            # TTL 语义：check_exists 只缓存 True（False/None 不缓存），无
            # 陈旧 False 误迁移路径；根为 / 时永不返回 False，/ 根不会被迁移。
            confirmed_missing = False
            if cleanup_allowed:
                verdict = self.admin_api.check_exists(root_path)
                if verdict is False:
                    confirmed_missing = True
                else:
                    logging.warning(
                        "[WebDAV刷新] 根路径列表不可信，跳过 B→C 迁移"
                        "（fail-closed，下轮周期自愈）: %s (check_exists=%s)",
                        root_path, verdict)
            if confirmed_missing:
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
        self._startup_cancel_event.clear()
        self._subtitle_scan_cancel_event.clear()
        startup_token = self._startup_generation
        startup_generation = self.startup_generation
        self.set_phase(PHASE_STARTING)
        logging.info("[启动] 准备环境并初始化数据库...")
        self.prepare_environment()
        self.db.init_db()
        config_status = self.get_config_status()
        if config_status["status"] != "ready":
            logging.warning("[启动] 配置未就绪，进入 fail-safe: %s", config_status)
            self._running = False
            self.set_phase(PHASE_FAIL_SAFE, error=config_status.get("reason", "配置未就绪"))
            return
        logging.info("[启动] 数据库初始化完成")
        self.update_engine_configs()
        logging.info("[启动] 引擎配置加载完成")
        # 启动等待（无论是否执行全量同步，都等待，让 OpenList 服务就绪）
        behavior_cfg = self.config.behavior
        wait_seconds = int(
            getattr(behavior_cfg, "sync_on_startup_wait", 0) or 0)
        if wait_seconds > 0:
            logging.info(
                "[启动] 等待 %d 秒，让 OpenList 服务就绪（剩余 %d 秒）...",
                wait_seconds, wait_seconds)
            t_wait = time.time()
            remaining = wait_seconds
            while True:
                elapsed_wait = time.time() - t_wait
                remaining = max(0, wait_seconds - int(elapsed_wait))
                if remaining <= 0:
                    break
                if self._startup_cancel_event.wait(timeout=min(1, remaining)):
                    logging.info("[启动] 收到停止请求，中断启动等待")
                    self._running = False
                    self.set_phase(PHASE_STOPPED)
                    return
                if int(elapsed_wait) % 5 == 0 and remaining > 5:
                    logging.info("[启动] 等待剩余 %d 秒...", remaining)
            logging.info("[启动] 等待结束 (%.2fs)", time.time() - t_wait)
        
        t_sub = time.time()
        self.sync_protected_roots_from_config()
        if not self._startup_generation_valid(startup_token, startup_generation):
            return self._abort_stale_startup()
        logging.info("[启动] 同步保护根目录耗时: %.2fs", time.time() - t_sub)

        t_sub = time.time()
        self.scan_removed_protected_roots()
        if not self._startup_generation_valid(startup_token, startup_generation):
            return self._abort_stale_startup()
        logging.info("[启动] 扫描已移除保护根耗时: %.2fs", time.time() - t_sub)

        t_sub = time.time()
        self.persist_current_roots_snapshot()
        if not self._startup_generation_valid(startup_token, startup_generation):
            return self._abort_stale_startup()
        logging.info("[启动] 持久化根目录快照耗时: %.2fs", time.time() - t_sub)

        t_phase = time.time()
        self.set_phase(PHASE_SCANNING_A)
        self.initial_scan_a(use_bulk=True)
        if not self._startup_generation_valid(startup_token, startup_generation):
            return self._abort_stale_startup()
        logging.info("[启动] A 区扫描耗时: %.2fs", time.time() - t_phase)

        t_phase = time.time()
        self.set_phase(PHASE_SCANNING_B)
        self.initial_scan_b()
        if not self._startup_generation_valid(startup_token, startup_generation):
            return self._abort_stale_startup()
        logging.info("[启动] B 区扫描耗时: %.2fs", time.time() - t_phase)
        
        # 根据配置决定是否执行 A→B 全量同步（实际复制文件）
        sync_on_startup = getattr(behavior_cfg, "sync_on_startup", True)
        generation_pushed = False
        try:
            if sync_on_startup:
                t_phase = time.time()
                self.set_phase(PHASE_SYNCING_A_TO_B)
                self.scan_a_to_b_full_sync(
                    valid_engine_paths=self.get_engine_filter_paths(),
                    use_bulk=True)
                logging.info("[启动] A→B 同步耗时: %.2fs", time.time() - t_phase)
            else:
                logging.info("[启动] 跳过 A→B 全量同步（sync_on_startup=false）")

            # 成功收口：仅当 sync_on_startup=true 且代次仍有效时推进 generation
            if not self._startup_generation_valid(startup_token, startup_generation):
                return self._abort_stale_startup()
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
        if not self._startup_generation_valid(startup_token, startup_generation):
            return self._abort_stale_startup()
        try:
            audit_now = time.time()
            self.db.set_control("last_full_audit_at", str(audit_now))
            if hasattr(self.refresh_service, "_last_full_audit_at"):
                self.refresh_service._last_full_audit_at = audit_now
        # set_control 为 SQLite 写，Windows AV 锁/磁盘瞬时只读可抛 OperationalError
        except (AttributeError, OSError, sqlite3.OperationalError):
            logging.warning("[启动] 保存全量审计时间失败")
        
        t_sub = time.time()
        # 设计决策: v6 R2-A 启动期 catch-up 单扫化——原 watchers 前的
        # _reconcile_catch_up 全盘扫描删除，_reconcile_boundary_catch_up 成为
        # 唯一单扫（磁盘@#3 vs DB@#3 的观测窗口严格覆盖已删的 #2 双扫）。
        # _catch_up_delta 全库唯一消费者是 get_state_summary 的两个 dashboard
        # 计数（纯观测契约，结构与消费方不动）。反转 .kilo/性能专项优化.md
        # 既有「catch_up × 2 合并评估默认不动」登记，见 docs/否决方案.md。
        # 启动级重复清扫（Task 6，闭合 R1 重试盲区）：scan_a_to_b_full_sync 之后、
        # start_watchers() 之前执行一次全表 GROUP BY，隔离失败启动遗留的重复实例。
        self._cleanup_startup_duplicates()
        if not self._startup_generation_valid(startup_token, startup_generation):
            return self._abort_stale_startup()
        self.start_watchers()
        if not self._startup_generation_valid(startup_token, startup_generation):
            return self._abort_stale_startup()
        self.set_phase(PHASE_CATCHING_UP)
        self._reconcile_boundary_catch_up()
        if not self._startup_generation_valid(startup_token, startup_generation):
            return self._abort_stale_startup()
        logging.info("[启动] Watcher 挂载与边界补扫（单扫）耗时: %.2fs", time.time() - t_sub)
        # 启动后立即扫描 A 区字幕文件（补偿 initial_scan_a 不处理字幕）。
        # 字幕补偿不属于 STRM 核心就绪条件，放入受控后台线程，避免阻塞启动响应。
        self._start_subtitle_scan_background()
        # R-新6（c.9.2 Task 11）：首轮主动刷新 defer 一个 interval——启动
        # 管线刚做过同内容全量同步，立即执行构成 banner 后 2.5-3min 的
        # churn（"假性启动"感知根因）；watcher 实时覆盖增量，notify 提前
        # 唤醒语义保留。
        self.refresh_service.start(defer_first_cycle=True)
        # start() 能走到这里说明配置 ready 且所有启动阶段已完成。
        # WebUIServer.start_main() 用该标志判断引擎是否真的起来了；
        # 缺这一行会让 ready 配置被误判为 fail-safe（引擎在跑但对外报未启动）。
        self._running = True
        self.set_phase(PHASE_READY)
        # c.9.2 Task 13：banner 口径显性化——该横幅只覆盖核心五阶段，
        # 字幕扫描与主动刷新周期在后台继续，防止据 banner 误判全量 settle。
        logging.info(
            "嗨嗨，应用启动成功咯！(总耗时 %.2fs)（核心五阶段；字幕扫描与主动刷新周期后台继续）",
            time.time() - t_start)

    def stop(self) -> None:
        self._startup_cancel_event.set()
        self._subtitle_scan_cancel_event.set()
        self._startup_generation += 1
        self.startup_generation = 0
        # 取消所有待执行的延迟清理定时器
        with self._cleanup_lock:
            for timer in list(self._pending_cleanups.values()):
                timer.cancel()
            self._pending_cleanups.clear()
        self.refresh_service.stop()
        # C1 停机语义：observer.stop 同批调用 handler.close()——丢弃而非 flush
        # pending 去抖事件（flush 会在停机路径同步执行慢 handler 拖死关闭流程；
        # 丢弃事件由下次启动 boundary 单扫 + A→B 全量同步双重兜底恢复），
        # 并取消常驻去抖定时器、flush 未满窗的 C3 聚合摘要。
        for handler in getattr(self, "_watcher_handlers", []):
            close = getattr(handler, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logging.warning("[停止] watcher handler close 失败", exc_info=True)
        if self.observer is not None and self.observer.is_alive():
            self.observer.stop()
            # 有界 join：与同块字幕扫描 join(timeout=5) 对齐，防止 observer
            # 退出卡死拖垮停机路径。超时不抛错、不谎报停止完成，调用方
            # （stop_main）以存活权威复核后 fail-closed 处理。
            self.observer.join(timeout=5.0)
            if self.observer.is_alive():
                logging.warning("[停止] watcher observer 未在超时内退出，停止未完全完成")
        subtitle_thread = self._subtitle_scan_thread
        if (subtitle_thread is not None
                and subtitle_thread is not threading.current_thread()):
            subtitle_thread.join(timeout=5)
            if subtitle_thread.is_alive():
                logging.warning("[停止] A 区字幕后台扫描未在超时内退出")
            else:
                self._subtitle_scan_thread = None
        self._running = False

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
        self._watcher_handlers = []
        active_a = 0
        for a_root in self.a_roots:
            if a_root.exists():
                a_handler = AAreaEventHandler(self)
                self._watcher_handlers.append(a_handler)
                self.observer.schedule(a_handler, str(a_root), recursive=True)
                active_a += 1
                logging.info("[监控启动] A区: %s", a_root)
            else:
                logging.warning("[监控跳过] A区不存在: %s", a_root)
        for b_root in self._a_to_b_map.values():
            b_root.mkdir(parents=True, exist_ok=True)
            b_handler = BAreaEventHandler(self)
            self._watcher_handlers.append(b_handler)
            self.observer.schedule(b_handler, str(b_root), recursive=True)
            logging.info("[监控启动] B区: %s", b_root)
        self.c_root.mkdir(parents=True, exist_ok=True)
        self.observer.schedule(
            CAreaEventHandler(self), str(self.c_root), recursive=True)
        logging.info("[监控启动] C区: %s", self.c_root)
        self.observer.start()
        if active_a == 0:
            logging.warning("[提示] 没有可用的 A 区监控目录，程序将依赖后续目录出现或主动刷新")

    def _watchers_live(self) -> bool:
        """watcher 活跃派生判定（T1，C12-1：零新增状态源）。

        与 stop() 的现行 observer.is_alive() 判定同语义、单一事实源：
        - observer 构造于 __init__（None）→ False（启动窗口/未配置）；
        - start_watchers() 成功（observer.start() 后 is_alive() 即 True）→ True；
        - stop() 后 is_alive() False → False。
        会话批量隔离路径以 not _watchers_live() 门控（启动上下文专用），
        避开 Database.ReadWriteLock._writers_active 的命名陷阱。
        """
        return self.observer is not None and self.observer.is_alive()

    def is_engine_running(self) -> bool:
        """引擎存活权威（唯一单源的引擎侧 origin）：基于 observer 真实存活。

        不读 _current_phase（可被 set_phase 合法写成谎）、不读 _running
        （仅 start() 末尾置位且从不被内部读）——两者均不可信为存活事实。
        不提供 phase/progress 语义（那是 get_state_summary 的展示通道）。
        判定表达式委托 _watchers_live()（同一事实源，避免双份函数体漂移）。
        """
        return self._watchers_live()

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

    def ensure_scan_mapping_roots(self) -> None:
        """预计算各 mapping 的规范化根路径（共 2N 次 resolve），供扫描上下文 fast mapping 使用。

        由 initial_scan_b 与 scan_a_to_b_full_sync 入口各自调用、各自 finally 清理
        （两阶段作用域独立）。预计算根为 config 派生入口快照（self.a_b_mappings），
        与现行逐条读语义等价；扫描期间 watchdog/refresh 未启动，映射不会热变更。
        """
        if self._scan_mapping_roots is not None:
            return
        roots: list[tuple[str, Path, Path]] = []
        for mapping in self.a_b_mappings:
            mapping_id = str(getattr(mapping, "mapping_id", "")).strip()
            if not mapping_id:
                continue
            a_root = normalize_local_root(mapping.a_root)
            b_root = normalize_local_root(mapping.b_root)
            roots.append((mapping_id, a_root, b_root))
        self._scan_mapping_roots = roots

    def clear_scan_mapping_roots(self) -> None:
        """清理扫描阶段预计算根，恢复正常（watcher 活跃）的逐条路径解析。"""
        self._scan_mapping_roots = None

    def _get_scan_roots_for_mapping(
            self, mapping_id: str) -> tuple[Path | None, Path | None]:
        """按 mapping_id 查询扫描阶段预计算根；未初始化或未命中返回 (None, None)。"""
        roots = self._scan_mapping_roots
        if roots is None:
            return (None, None)
        for mid, a_root, b_root in roots:
            if mid == mapping_id:
                return (a_root, b_root)
        return (None, None)

    def _get_mapping_for_a_fast(
            self, local_path: str | Path,
            *, _resolved_target: Path | None = None) -> tuple[str, Path, Path] | None:
        """fast 版 get_mapping_for_a：基于扫描阶段预计算根。

        **硬性规格（N1）**：预计算根未初始化时必须回退权威实现（get_mapping_for_a），
        禁止返回 None——否则 watcher 活跃上下文中 A 区新增评分降级、B 区删除/移动后
        投影误清空、cleanup 误判。收集全部匹配仅恰好一个才返回（零/多命中 fail-closed）。

        设计决策: ``_resolved_target``（T5）供调用方传入已 resolve 的同一路径，
        跳过 normalize_local_root 的重复 resolve（Windows resolve() ~0.4ms/次，
        预检链每文件曾冗余 resolve 4-5 次）；resolve 对已 resolve 路径幂等，
        同一调用链内无文件系统变化窗口，语义与重复 resolve 完全等价。
        回退分支（roots 未预载）不受益也不受影响——权威实现自行 normalize。
        """
        roots = self._scan_mapping_roots
        if roots is None:
            return self.get_mapping_for_a(local_path)
        target = _resolved_target if _resolved_target is not None \
            else normalize_local_root(local_path)
        matches: list[tuple[str, Path, Path]] = []
        for mapping_id, a_root, b_root in roots:
            try:
                target.relative_to(a_root)
            except ValueError:
                continue
            matches.append((mapping_id, a_root, b_root))
        return matches[0] if len(matches) == 1 else None

    def _get_mapping_for_b_fast(
            self, local_path: str | Path,
            *, _resolved_target: Path | None = None) -> tuple[str, Path, Path] | None:
        """fast 版 get_mapping_for_b：基于扫描阶段预计算根（回退规格同 _get_mapping_for_a_fast）。

        ``_resolved_target`` 语义同 _get_mapping_for_a_fast（T5 冗余 resolve 消除）。
        """
        roots = self._scan_mapping_roots
        if roots is None:
            return self.get_mapping_for_b(local_path)
        target = _resolved_target if _resolved_target is not None \
            else normalize_local_root(local_path)
        matches: list[tuple[str, Path, Path]] = []
        for mapping_id, a_root, b_root in roots:
            try:
                target.relative_to(b_root)
            except ValueError:
                continue
            matches.append((mapping_id, b_root, a_root))
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
        """返回指定 A 根对应的 STRM engine **挂载路径（mount）**，供刷新闸门做可访问性交集。

        注意：返回值处于挂载命名空间（如 `/strm`、根挂载 `/`），与
        `get_engine_filter_paths` 返回的云资源过滤前缀是两个口径，勿混用。
        """
        roots = {normalize_local_root(root) for root in a_roots}
        result: list[str] = []
        storage_map = getattr(self.config, "strm_storage_map", {}) or {}
        for entry_path, storage in storage_map.items():
            if normalize_local_root(storage.local_path) in roots:
                mount = str(storage.mount_path).rstrip("/") or "/"
                if mount not in result:
                    result.append(mount)
        return result

    @staticmethod
    def _norm_mount(p: Any) -> str | None:
        """挂载命名空间规范化（mount 交集判定用）。

        空白 → 告警跳过返回 None；根挂载 `/` 经 canonicalize 原样保持 `/`，
        与 `get_engine_paths_for_a_roots` 的根挂载返回值同口径（F-A）。
        """
        if not isinstance(p, str) or not p.strip():
            logging.warning("[引擎过滤] 引擎挂载路径为空，跳过: %r", p)
            return None
        return canonicalize_webdav_path(p.strip(), case_sensitive=True)

    @staticmethod
    def _norm_prefix(p: Any) -> str | None:
        """过滤前缀命名空间规范化（valid_engine_paths 过滤域用）。

        与 `parse_strm_content` 侧同函数同口径（NFC、反斜杠归一、补前导斜杠、
        合并 /+、去尾斜杠全覆盖；空白 ValueError 由前置 guard 挡住）。
        根 `/` 转空串 `""`——现行谓词 `webdav.startswith("" + "/")` 恒真，
        即云根全域语义（计划 §三 Step 1 第 5 类）。
        """
        if not isinstance(p, str) or not p.strip():
            logging.warning("[引擎过滤] STRM 存储路径为空，跳过: %r", p)
            return None
        q = p.strip()
        if q == "/":
            return ""
        return canonicalize_webdav_path(q, case_sensitive=True)

    # 设计决策: 引擎范围过滤按云资源真实前缀（非挂载命名空间）。
    # 历史 bug：start()/_scan_and_sync 把 config.paths.strm_engine_paths（挂载，
    # 如 /strm）直接喂给 scan_a_to_b_full_sync 的过滤谓词，而 A 记录 webdav_path
    # 是云资源前缀（如 /天翼云盘X/番剧/…），异源部署下前缀匹配恒假 → 全量同步
    # 0 条复制。本 helper 是过滤输入的唯一事实源：收集配置引擎 entry.paths 的
    # 整表并集（每项过 _norm_prefix）；API 失败/无 entry/空 paths 回退该引擎
    # mount_path（自包含/基准形态兼容），告警一条。mount 匹配与过滤前缀双规范化
    # 分离（_norm_mount/_norm_prefix，F-A）；根挂载 '/' 在前缀域转 '' 全域。
    # config.paths.strm_engine_paths 的挂载全局语义不动。见 docs/否决方案.md。
    def get_engine_filter_paths(
            self, allowed_mounts: set[str] | None = None) -> list[str]:
        """返回 A→B 同步过滤用的云资源前缀并集（规范化、去重、稳定序）。

        Args:
            allowed_mounts: 刷新闸门的可访问引擎挂载集合（挂载命名空间）。
                非空时与配置引擎集合两侧均过 `_norm_mount` 后取交集；
                None/空表示不额外约束（启动路径）。

        Returns:
            过滤前缀列表（过滤命名空间）。引擎集合为空 → 空列表（保持现行
            "无引擎 → 全部过滤"语义）。每次现算，不加进程级缓存。
        """
        configured = list(getattr(self.config, "strm_engine_paths", None) or [])
        engine_set: set[str] = set()
        for ep in configured:
            norm = self._norm_mount(ep)
            if norm is not None:
                engine_set.add(norm)
        if not engine_set:
            # 无配置引擎 → 空列表：调用方谓词恒假，全部 skip_filtered（现状语义）
            return []
        if allowed_mounts:
            allowed_norm: set[str] = set()
            for am in allowed_mounts:
                norm = self._norm_mount(am)
                if norm is not None:
                    allowed_norm.add(norm)
            engine_set &= allowed_norm
            if not engine_set:
                return []

        storage_map = getattr(self.config, "strm_storage_map", None)
        if not isinstance(storage_map, dict):
            storage_map = {}

        # 按引擎挂载收集全部匹配 entry 的 paths（last_dir 分组下同 mount 多
        # entry，取整表并集；禁用 paths[0]/engine_entry_paths 便捷方法）
        mount_to_paths: dict[str, list[str]] = {m: [] for m in engine_set}
        for storage in storage_map.values():
            mount_norm = self._norm_mount(getattr(storage, "mount_path", ""))
            if mount_norm is None or mount_norm not in mount_to_paths:
                continue
            mount_to_paths[mount_norm].extend(
                getattr(storage, "paths", None) or [])

        ordered: list[str] = []
        seen: set[str] = set()
        for mount in sorted(engine_set):
            collected: list[str] = []
            for p in mount_to_paths.get(mount, []):
                prefix = self._norm_prefix(p)
                if prefix is not None:
                    collected.append(prefix)
            if not collected:
                # 兜底：某引擎无 entry / paths 空列表 → 回退挂载前缀
                # （load_strm_storage_from_api 吞异常不清旧值，map 可能陈旧/为空；
                # 自包含部署与基准形态下 mount 即云路径前缀）
                fallback = self._norm_prefix(mount)
                if fallback is not None:
                    collected.append(fallback)
                logging.warning(
                    "[引擎过滤] 引擎挂载 %s 无可用 STRM 存储路径，"
                    "回退挂载前缀过滤: %r", mount, fallback)
            for prefix in collected:
                if prefix not in seen:
                    seen.add(prefix)
                    ordered.append(prefix)
        return ordered

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
            # 嵌套根路径校验——防止 A 或 B 根互相嵌套导致路径边界模糊
            a_root_str = str(a_root)
            b_root_str = str(b_root)
            for old_a in seen_a:
                old_a_str = str(old_a)
                if (a_root_str.startswith(old_a_str + os.sep) or old_a_str.startswith(a_root_str + os.sep)
                        or b_root_str.startswith(str(old_a) + os.sep) or str(old_a).startswith(b_root_str + os.sep)):
                    return {"status": "fail_safe_active", "reason": f"mapping 根路径嵌套: {a_root} 与 {old_a}"}
            for old_b in seen_b:
                old_b_str = str(old_b)
                if (a_root_str.startswith(old_b_str + os.sep) or old_b_str.startswith(a_root_str + os.sep)
                        or b_root_str.startswith(old_b_str + os.sep) or old_b_str.startswith(b_root_str + os.sep)):
                    return {"status": "fail_safe_active", "reason": f"mapping 根路径嵌套: {b_root} 与 {old_b}"}
            seen_a.append(a_root)
            seen_b.append(b_root)
        return {"status": "ready", "reason": "mapping 配置有效"}

    def get_a_root_for_path(self, local_path: str | Path) -> Path | None:
        mapping = self._get_mapping_for_a_fast(local_path)
        return mapping[1] if mapping else None

    def get_b_root_for_a(self, a_local_path: str | Path) -> Path:
        mapping = self._get_mapping_for_a_fast(a_local_path)
        if mapping is None:
            raise ValueError(f"文件无法唯一解析 A/B mapping: {a_local_path}")
        return mapping[2]

    def get_b_root_for_path(self, b_local_path: str | Path) -> Path | None:
        mapping = self._get_mapping_for_b_fast(b_local_path)
        return mapping[1] if mapping else None

    def _mapping_id_for_b(self, b_local_path: str | Path) -> str | None:
        mapping = self._get_mapping_for_b_fast(b_local_path)
        return mapping[0] if mapping else None

    def build_b_path_from_a(self, a_local_path: str | Path,
                            webdav_path: str | None = None,
                            a_root: Path | None = None,
                            b_root: Path | None = None,
                            *,
                            _a_local_resolved: Path | None = None) -> Path:
        # N3 兑现（c.9.2 Task 6）：调用方已 resolve（如 pass1 的 T5 透传）时
        # 经 _a_local_resolved 旁路，消除非 skip 行的第二段 resolve。仅关键
        # 字形参对既有全部调用方向后兼容（未传时行为逐字节不变）。
        a_local = (
            _a_local_resolved if _a_local_resolved is not None
            else Path(a_local_path).resolve())
        if a_root is None:
            a_root = self.get_a_root_for_path(a_local)
            if a_root is None:
                raise ValueError(f"文件不属于任何A根目录: {a_local}")
        else:
            a_root = Path(a_root)
            try:
                a_local.relative_to(a_root)
            except ValueError:
                raise ValueError(f"文件不属于任何A根目录: {a_local}")
        rel = a_local.relative_to(a_root)
        is_movie = self._should_treat_as_movie(a_local, webdav_path)
        if b_root is None:
            b_root = self.get_b_root_for_a(a_local)
        else:
            b_root = Path(b_root)
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
        raw_storages = getattr(self.config, "raw_strm_storages", None)
        if raw_storages:
            logging.info("[引擎配置] 复用启动期已获取的 STRM 存储配置快照 (%d 个)", len(raw_storages))
            content = raw_storages
        else:
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

    def _b_lineage_preflight(
            self, b_local_path: str, webdav_path: str,
            b_local: Path | None = None) -> bool:
        """血统预检（第 1-4 步，纯读；T2b 提取自 _verify_b_path_lineage 的单一事实源）。

        步骤 1-4 已核实纯读：_resolve_a_source（缓存优先，DB 回退走
        read_connection，WAL 安全）、_check_basic_lineage、
        _check_season_layer_addition、_check_media_name_match（fast mapping +
        _cached_boundary_by_source_name 只读缓存）。返回 True 表示步骤 1-4
        直接放行（完整校验同样放行）；False 表示需进入完整
        _verify_b_path_lineage 的第 5-9 步串行回退（含副作用面：越界删除、
        solo-episode Timer、边界写）。
        本函数体严禁引入副作用函数与任何线程池设施（源码契约扫描，C12-3）。
        """
        fingerprint = make_strm_fingerprint(webdav_path)
        if b_local is None:
            b_local = Path(b_local_path).resolve()

        # 1. 解析 A 区源文件（b_local 已解析一次传入复用，避免重复 resolve）
        a_source = self._resolve_a_source(b_local_path, webdav_path, fingerprint, b_local=b_local)
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
        return False

    def _preflight_b_lineage_parallel(
            self, files: list[tuple[str, str]]) -> tuple[dict[str, bool], dict[str, Any], dict[str, Path]]:
        """并行血统预检 + stat（T2b；_insert_new_b_records wave1 产出 (ok, stat) 表）。

        设计决策: ThreadPoolExecutor(8) 执行 _b_lineage_preflight（步骤 1-4
        纯读，线程安全依据：只读缓存 + WAL 读连接 + _log_lineage_pass_once
        已有锁）与 Path.stat()。返回 (ok_map, stat_map, resolved_map)：
        ok_map[disk_path]=预检是否直接放行；stat_map[disk_path]=stat 结果或
        None（stat 失败）；resolved_map[disk_path]=预检内已 resolve 的规范
        路径（T5：wave2 复用，消除 fast mapping 的重复 resolve）。
        """
        ok_map: dict[str, bool] = {}
        stat_map: dict[str, Any] = {}
        resolved_map: dict[str, Path] = {}
        if not files:
            return ok_map, stat_map, resolved_map

        def _one(item: tuple[str, str]) -> tuple[str, bool, os.stat_result | None, Path]:
            disk_path, webdav_path = item
            try:
                stat = Path(disk_path).stat()
            except OSError:
                stat = None
            # T5: 每文件恰一次 resolve——经 b_local 入参传入预检（跳过其内部
            # resolve），并由 resolved_map 供 wave2 fast mapping 复用
            resolved = Path(disk_path).resolve()
            ok = self._b_lineage_preflight(
                disk_path, webdav_path, b_local=resolved)
            return disk_path, ok, stat, resolved

        with ThreadPoolExecutor(max_workers=8) as executor:
            for disk_path, ok, stat, resolved in executor.map(_one, files):
                ok_map[disk_path] = ok
                stat_map[disk_path] = stat
                resolved_map[disk_path] = resolved
        return ok_map, stat_map, resolved_map

    def _snapshot_reuse_check_parallel(
            self, candidates: list[tuple[str, str]]) -> set[str]:
        """A1（c7 §5.3）：Wave0 快照复用检查并行化——纯读复用检查段。

        池留 helper 体内（禁面契约：本函数体严禁六副作用 marker，纯读复用
        检查；`_reconcile_b_historical_records` 体仍禁 ThreadPoolExecutor
        字面量，经本 helper 引用）。executor.map 保序输出。

        输入限定：已过现行四条件门的 (local_path, fingerprint) 对——
        `not force_full` + row_mapping_id 真值 + 指纹非空 + 磁盘指纹一致。
        worker 仅调 `_snapshot_reuses_valid_lineage`（单一事实源），逐行捕获
        (OSError, AttributeError, TypeError, ValueError) -> False——与串行
        现行 catch 完全同类别、不扩大（不新增 sqlite3.Error 捕获，
        sqlite3.Error 传播语义与串行一致）。

        前置门（M-12/E3）在调用方 Wave0：仅当 `self._reconcile_cache is
        not None` 且 `_scan_mapping_roots` 已预载才进入本 helper，否则
        Wave0 整体回退串行（防 8-worker 并发 DB 回退风暴 = E-10 放大）。
        """
        reused: set[str] = set()
        if not candidates:
            return reused

        def _one(item: tuple[str, str]) -> tuple[str, bool]:
            local_path, fingerprint = item
            try:
                return local_path, self._snapshot_reuses_valid_lineage(
                    local_path, fingerprint)
            except (OSError, AttributeError, TypeError, ValueError):
                # 与串行现行 catch 同类别：异常行按 False（不复用）处理
                return local_path, False

        with ThreadPoolExecutor(max_workers=8) as executor:
            for local_path, ok in executor.map(_one, candidates):
                if ok:
                    reused.add(local_path)
        return reused

    def _verify_b_path_lineage(
            self, b_local_path: str, webdav_path: str, is_sync_phase: bool = False) -> bool:
        """验证 B 区文件路径的血统关系，确保其合法存在于 B 区。

        结构（T2b，语义与提取前逐行等价）：_b_lineage_preflight（第 1-4 步
        纯读，可并行预检）+ 未放行时的第 5-9 步串行段（含副作用：越界删除、
        solo-episode Timer、边界写——严禁并行化）。
        """
        fingerprint = make_strm_fingerprint(webdav_path)
        b_local = Path(b_local_path).resolve()

        # 第 1-4 步：纯读预检（单一事实源）
        if self._b_lineage_preflight(b_local_path, webdav_path, b_local=b_local):
            return True

        # preflight 未放行：确定性重跑第 1 步获取 a_source（步骤 1 失败即最终
        # 拒绝，与提取前语义一致；_resolve_a_source 幂等且缓存命中，开销可忽略），
        # 随后进第 5-9 步
        a_source = self._resolve_a_source(b_local_path, webdav_path, fingerprint, b_local=b_local)
        if not a_source:
            return False

        a_local_path, a_root, a_rel_dir, b_rel_dir = a_source
        b_parts = list(b_rel_dir.parts)

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
            self, b_local_path: str, webdav_path: str, fingerprint: str,
            b_local: Path | None = None) -> tuple | None:
        """解析 A 区源文件路径，返回 (a_local_path, a_root, a_rel_dir, b_rel_dir) 或 None。

        性能优化（Task 3 消除 12 次 resolve 风暴）：
        - b_local 解析一次由调用方（如 _verify_b_path_lineage）传入复用
        - a_root/b_root 经 fast mapping 查预计算根（消除每调用 1+2N 次 resolve）
        """
        if b_local is None:
            b_local = Path(b_local_path).resolve()
        # 核对期间优先查预载缓存；缓存未命中或未初始化时回退 DB（与现状等价）
        a_record = None
        cache = self._reconcile_cache
        if cache is not None:
            a_record = cache["a_by_webdav"].get(webdav_path)
        if a_record is None:
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
        # T5: 直接调 fast 变体并传入已 resolve 路径（消除 normalize_local_root
        # 的重复 resolve；roots 未预载时 fast 内部回退权威实现，语义不变）
        a_mapping = self._get_mapping_for_a_fast(
            a_local_path, _resolved_target=a_local_path)
        if a_mapping is None:
            logging.debug("[血统校验失败] A区源不在任何根目录下: %s", a_local_path)
            return None
        a_root = a_mapping[1]
        b_mapping = self._get_mapping_for_b_fast(b_local, _resolved_target=b_local)
        if b_mapping is None:
            logging.debug("[血统校验失败] B区路径不在任何B根目录下: %s", b_local_path)
            return None
        b_root = b_mapping[1]
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
        mapping = self._get_mapping_for_b_fast(b_local_path)
        if mapping is None:
            logging.warning("[边界映射] 无法解析 mapping，跳过媒体名匹配: %s", b_local_path)
            return False
        boundary = self._cached_boundary_by_source_name(mapping[0], a_media_name)
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
        mapping = self._get_mapping_for_b_fast(b_local_path)
        mapping_id = mapping[0] if mapping else None
        if fingerprint and mapping_id:
            boundary = self._cached_boundary_by_fingerprint(mapping_id, fingerprint)
            if boundary:
                source_media_name = boundary.source_media_name
                current_media_name = boundary.current_media_name
                if physical_media_folder_name == current_media_name:
                    self._log_lineage_pass_once("边界映射匹配", b_local_path)
                    return True
                if physical_media_folder_name == source_media_name:
                    self._log_lineage_pass_once("回到源边界", b_local_path)
                    return True

        mapping = self._get_mapping_for_b_fast(b_local_path)
        if mapping is None:
            logging.warning("[边界映射] 无法解析 mapping，跳过边界匹配: %s", b_local_path)
            return False
        mapping_id, b_root, _ = mapping
        if cloud_show_name and physical_media_folder_name:
            boundary_by_source = self._cached_boundary_by_source_name(
                mapping_id, physical_media_folder_name)
            if boundary_by_source:
                mapped_source = boundary_by_source.source_media_name
                mapped_current = boundary_by_source.current_media_name
                if cloud_show_name == mapped_source or cloud_show_name == mapped_current:
                    self._log_lineage_pass_once("交叉边界映射匹配(源->当前)", b_local_path)
                    return True

            boundary_by_current = self._cached_boundary_by_current_name(
                mapping_id, physical_media_folder_name, str(b_root))
            if boundary_by_current:
                mapped_source = boundary_by_current.source_media_name
                mapped_current = boundary_by_current.current_media_name
                if cloud_show_name == mapped_source or cloud_show_name == mapped_current:
                    self._log_lineage_pass_once("交叉边界映射匹配(当前->源)", b_local_path)
                    return True

            boundary_by_cloud = self._cached_boundary_by_source_name(
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

            mapping = self._get_mapping_for_b_fast(b_local_path)
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
            s_webdav = read_strm_webdav_path(bad_file)
            if not s_webdav:
                logging.warning("[单兵审判] 无法读取 STRM 的 WebDAV 路径，跳过删除: %s", bad_file)
                return
            # fail-closed 云端二次核验
            # fail-closed 云端核验：30s 观察期基于纯 DB 状态，期间云端可能被恢复，需二次确认
            # check_exists 三态：True=云端仍在→取消, False=权威缺失→删除, None=不可信→取消
            try:
                exists_check = self.admin_api.check_exists(s_webdav)
            except Exception as exc:
                logging.warning("[单兵审判] 云端核验失败（不可信），跳过删除: %s (%s)", bad_file, exc)
                return
            if exists_check is not False:
                # True=云端仍在, None=不可信 → 中止删除，记录决策
                status = "云端仍在" if exists_check is True else "不可信"
                logging.info("[单兵审判] 单兵审判取消：%s，保留文件: %s", status, bad_file)
                return
            # check_exists 返回 False，权威确认云端已删除，安全删除本地文件
            logging.warning("[B区清理] 审判结果：确认单兵脱离集体，执行物理删除: %s", bad_file)
            if safe_remove_file(bad_file):
                self.db.delete_b_by_local(str(bad_file))
            else:
                logging.warning("[B区清理] 单兵脱离审判：物理删除失败，保留 DB 记录: %s", bad_file)
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

        性能优化（Task 3）：本阶段顶层预计算 mapping 根（fast mapping）并提升
        只读预载缓存生命周期至整个 B 扫描阶段；顶层 try/finally 兜底清理，异常
        路径也不残留缓存或预计算根。
        """
        t_total = time.time()
        if self.get_config_status()["status"] != "ready":
            logging.warning("[初始化] 配置处于 fail-safe，拒绝 B 区扫描 (force_full=%s)", force_full)
            return
        logging.info("[初始化] B 区逆向自同步开始 (force_full=%s)...", force_full)
        # Task 3: 预计算 mapping 根 + 只读预载缓存覆盖整个 B 扫描阶段
        self.ensure_scan_mapping_roots()
        try:
            self._build_reconcile_cache()
            disk_data = self._scan_b_disk()
            if disk_data is None:
                return
            b_discovered = len(disk_data[1])
            self.update_progress(b_discovered=b_discovered)

            db_records = self._load_b_db_records()
            if db_records is None:
                return

            processed = set()
            self._reconcile_b_historical_records(
                disk_data, db_records, processed, force_full=force_full)
            self._insert_new_b_records(disk_data, processed)
            self.update_progress(b_reconciled=b_discovered)
            logging.info("[初始化] B 区逆向自同步完成 (%.1fs)", time.time() - t_total)
        finally:
            # 兜底清理：核对中途异常（含批写失败上抛）也必须清空缓存与预计算根，
            # 否则 _resolve_a_source/_snapshot_reuses_valid_lineage 以 cache is not None
            # 命中旧数据，属缓存污染风险（六审补充）。
            self._reconcile_cache = None
            self.clear_scan_mapping_roots()

    def _snapshot_reuses_valid_lineage(
            self, local_path: str, fingerprint: str | None) -> bool:
        mapping = self._get_mapping_for_b_fast(local_path)
        if mapping is None or not fingerprint:
            return False
        try:
            stat_before = Path(local_path).stat()
            snapshot = None
            cache = self._reconcile_cache
            if cache is not None:
                snapshot = cache["snapshots"].get((mapping[0], local_path))
            if snapshot is None:
                # A1b（c7 §5.3）：整 mapping 快照已预载且 dict miss → 确认无快照，
                # 直接 False 免 DB。安全论证：miss 方向安全——假 miss 只会让该行
                # 走 Wave1 预检/Wave2 完整校验并重写快照，绝不假命中。Task 0 实测
                # 每次 DB 回退 ≈5.45ms（12666 次 = 69s，Wave0 的 78%），本短路为
                # A1b 收益主体。cache None 或 mapping 未覆盖（预载失败）维持 DB 回退。
                if cache is not None and mapping[0] in cache.get("snapshots_loaded", ()):
                    return False
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
            # R-2：并行复用检查下逐行 WARNING 会刷屏，降级 DEBUG（红测锁定零
            # WARNING）；异常返回 False 与串行现行语义一致（回退完整校验兜底）。
            logging.debug("[B区快照] 读取/比较失败，回退完整核对: %s (%s)", local_path, exc)
            return False

    def _store_valid_lineage_snapshot(
            self, local_path: str, fingerprint: str | None,
            buffered: bool = False, *,
            stat_result=None, _resolved_target=None) -> None:
        """保存验证通过的 lineage snapshot。

        ``buffered=True`` 时（B 区历史记录核对路径）暂存到核对缓存，满
        ``B_SNAPSHOT_BATCH_SIZE`` 或循环结束统一批量写；``buffered=False``
        （新增记录路径）保持逐条直写，避免新记录路径与核对路径行为不对称。
        单行失败仅告警并继续（「写入失败，记录保留但不复用」）。

        A2（c7 §5.4，F1 门控 Task 0 实测 store 尾 0.6ms/行 ≥0.5 触发）：仅
        关键字 ``stat_result``/``_resolved_target`` 复用 Wave1 已产出的
        stat/resolve 结果（Wave2 调用点透传），缺省 None 保持原行为（重新
        stat/resolve，零行为变化）。stat 复用语义安全：Wave1 时点的
        size/mtime 若在毫秒级窗口内被改 → 快照失配 → 下次启动复用判定
        mismatch → 重写快照（假 miss 方向，绝不假命中）。迁移分支不改；
        禁 ``mapping_id`` 形参（mapping 唯一解析归属本函数内部，禁跨区共享）。
        """
        mapping = self._get_mapping_for_b_fast(
            local_path, _resolved_target=_resolved_target)
        if mapping is None or not fingerprint:
            return
        try:
            stat_after = stat_result if stat_result is not None else Path(local_path).stat()
            row = (
                mapping[0], local_path, stat_after.st_size, stat_after.st_mtime_ns,
                fingerprint, self._mapping_version, LINEAGE_VERSION, "valid")
            if buffered:
                cache = self._reconcile_cache
                if cache is None:
                    # 缓存未初始化（异常路径兜底）：保持逐条直写语义
                    self.db.upsert_b_lineage_snapshot(*row)
                    return
                cache["pending_snapshots"].append(row)
                if len(cache["pending_snapshots"]) >= B_SNAPSHOT_BATCH_SIZE:
                    self._flush_pending_snapshots()
                return
            self.db.upsert_b_lineage_snapshot(*row)
        except (OSError, AttributeError, TypeError, ValueError) as exc:
            logging.warning("[B区快照] 写入失败，记录保留但不复用: %s (%s)", local_path, exc)

    # 设计决策: 只读预载缓存（A 记录/媒体边界/lineage 快照），查询优先缓存、
    # 未命中或未初始化回退 DB 与现状等价；按 mapping_id 隔离。生命周期由
    # initial_scan_b 顶层接管（覆盖整个 B 扫描阶段：核对 + 新增记录），顶层
    # try/finally 兜底清理，异常路径不残留旧数据。勿按"多余缓存/重复查询"清理。
    def _build_reconcile_cache(self) -> None:
        """构建 B 区历史记录核对使用的只读预载缓存。

        任一子加载失败仅告警并保留空子缓存，后续查询回退 DB，
        与「缓存未初始化/未命中时行为与现状一致」的兜底语义一致。
        键约定：
        - a_by_webdav: webdav_path -> ARecord（重复键首行优先，
          与 get_a_by_webdav(...).fetchone() 语义一致）
        - boundaries.by_fingerprint: (mapping_id, fingerprint) -> BoundaryRecord
        - boundaries.by_source_name_only: (mapping_id, source_media_name) -> BoundaryRecord
          （每键保留 updated_at 最大，对齐 ORDER BY updated_at DESC LIMIT 1）
        - boundaries.by_current_name: (mapping_id, current_media_name, str(b_root)) -> BoundaryRecord
          （第三元是 B 根目录字符串，与 strm_media_boundary.engine_entry_path 承载值一致）
        - snapshots: (mapping_id, local_path) -> BLineageSnapshotRecord
        - snapshots_loaded: A1b（c7 §5.3）——快照预载完整的 mapping_id 集合
          （按 mapping 粒度：整 mapping 预载无异常才加入，E4）；命中集合且
          dict miss → 直接 False 免 DB，未覆盖 mapping 维持 DB 回退
        - pending_snapshots: 待批量写入的快照行缓冲
        """
        cache: dict = {
            "a_by_webdav": {},
            "boundaries": {
                "by_fingerprint": {},
                "by_source_name_only": {},
                "by_current_name": {},
            },
            "snapshots": {},
            "snapshots_loaded": set(),
            "pending_snapshots": [],
        }
        mapping_ids = [
            str(getattr(m, "mapping_id", "")).strip()
            for m in self.a_b_mappings
            if str(getattr(m, "mapping_id", "")).strip()
        ]
        try:
            for a in self.db.get_all_a_records():
                cache["a_by_webdav"].setdefault(a.webdav_path, a)
        except Exception as exc:
            logging.warning("[B区核对] A 区记录预载失败，回退逐条 DB 读: %s", exc)
        for mapping_id in mapping_ids:
            try:
                for b in self.db.get_all_media_boundaries(mapping_id):
                    key_fp = (b.mapping_id, b.fingerprint)
                    cur = cache["boundaries"]["by_fingerprint"].get(key_fp)
                    if cur is None or b.updated_at >= cur.updated_at:
                        cache["boundaries"]["by_fingerprint"][key_fp] = b
                    key_src = (b.mapping_id, b.source_media_name)
                    cur = cache["boundaries"]["by_source_name_only"].get(key_src)
                    if cur is None or b.updated_at >= cur.updated_at:
                        cache["boundaries"]["by_source_name_only"][key_src] = b
                    key_cur = (b.mapping_id, b.current_media_name, b.engine_entry_path)
                    cur = cache["boundaries"]["by_current_name"].get(key_cur)
                    if cur is None or b.updated_at >= cur.updated_at:
                        cache["boundaries"]["by_current_name"][key_cur] = b
            except Exception as exc:
                logging.warning("[B区核对] 边界记录预载失败，回退逐条 DB 读: %s", exc)
            try:
                for s in self.db.get_all_lineage_snapshots(mapping_id):
                    cache["snapshots"][(s.mapping_id, s.local_path)] = s
                # A1b（E4）：按 mapping 粒度——整 mapping 预载无异常才标记
                cache["snapshots_loaded"].add(mapping_id)
            except Exception as exc:
                logging.warning("[B区核对] 快照预载失败，回退逐条 DB 读: %s", exc)
        self._reconcile_cache = cache

    def _flush_pending_snapshots(self) -> None:
        """刷新核对期间缓冲的 lineage snapshot（批量单事务写入）。

        整体异常仅告警不中止扫描（与「快照写失败不中止扫描」语义一致）；
        行级容错由数据库批量方法内部按行捕获。
        """
        cache = self._reconcile_cache
        if cache is None:
            return
        pending = cache.get("pending_snapshots")
        if not pending:
            return
        cache["pending_snapshots"] = []
        try:
            self.db.upsert_b_lineage_snapshots_batch(pending)
        except Exception as exc:
            logging.warning(
                "[B区快照] 批量写入失败，记录保留但不复用 (%d 条): %s",
                len(pending), exc)

    def _cached_boundary_by_fingerprint(
            self, mapping_id: str, fingerprint: str):
        """按 (mapping_id, fingerprint) 查边界映射；缓存未命中回退 DB。"""
        cache = self._reconcile_cache
        if cache is not None:
            b = cache["boundaries"]["by_fingerprint"].get((mapping_id, fingerprint))
            if b is not None:
                return b
        return self.db.get_media_boundary_by_fingerprint(mapping_id, fingerprint)

    def _cached_boundary_by_source_name(
            self, mapping_id: str, source_media_name: str):
        """按 (mapping_id, source_media_name) 查最新边界映射；缓存未命中回退 DB。"""
        cache = self._reconcile_cache
        if cache is not None:
            b = cache["boundaries"]["by_source_name_only"].get((mapping_id, source_media_name))
            if b is not None:
                return b
        return self.db.get_media_boundary_by_source_name_only(mapping_id, source_media_name)

    def _cached_boundary_by_current_name(
            self, mapping_id: str, current_media_name: str, b_root_str: str):
        """按 (mapping_id, current_media_name, str(b_root)) 查边界映射；缓存未命中回退 DB。"""
        cache = self._reconcile_cache
        if cache is not None:
            b = cache["boundaries"]["by_current_name"].get(
                (mapping_id, current_media_name, b_root_str))
            if b is not None:
                return b
        return self.db.get_media_boundary_by_current_name(
            mapping_id, current_media_name, b_root_str)

    def _scan_b_disk(self) -> tuple[dict, dict] | None:
        """扫描 B 区磁盘文件，返回 (fingerprint_to_paths, path_to_data)

        4 线程并发读取 .strm 内容与计算指纹；主线程串行汇总结果字典，
        单文件读取/解析异常只告警跳过，不中断整个扫描。
        """
        logging.info("[初始化] B 区磁盘扫描开始...")
        t0 = time.time()

        # 1) 先收集全部 .strm 路径（rglob 本身是 C 级遍历，保持串行）
        all_strm_paths: list[Path] = []
        for b_root in self._a_to_b_map.values():
            if not b_root.exists():
                logging.info("[初始化] B 区根目录不存在，跳过: %s", b_root)
                continue
            all_strm_paths.extend(b_root.rglob("*.strm"))

        total_files = len(all_strm_paths)
        logging.info("[初始化] B 区共发现 %d 个 STRM 文件待读取", total_files)

        def _read_one(strm_file: Path) -> tuple[str, str, str] | None:
            """单文件读取（工作线程执行）：返回 (path_str, webdav, fingerprint)。"""
            webdav_path = read_strm_webdav_path(strm_file)
            if webdav_path:
                return (str(strm_file), webdav_path,
                        make_strm_fingerprint(webdav_path))
            return None

        # 2) 并发读取内容 + 计算指纹，主线程安全汇总
        # 设计决策: 8 线程（T2a，原 4 线程）——纯读 I/O 密集；rglob 收集与
        # 主线程汇总保持串行不变。若实测 I/O 饱和无收益则回 4（T0 数据裁决）。
        all_fingerprint_to_paths: dict[str, set[str]] = {}
        all_path_to_data: dict[str, dict] = {}
        completed_count = 0
        last_log_time = time.time()
        if total_files:
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = {executor.submit(_read_one, p): p
                           for p in all_strm_paths}
                for future in as_completed(futures):
                    strm_file = futures[future]
                    completed_count += 1
                    try:
                        result = future.result()
                    except Exception as e:
                        logging.warning(
                            "[初始化] 读取 B 区文件失败: %s (%s)", strm_file, e)
                        continue
                    if result is None:
                        continue
                    path_str, webdav_path, fingerprint = result
                    all_fingerprint_to_paths.setdefault(
                        fingerprint, set()).add(path_str)
                    all_path_to_data[path_str] = {
                        "webdav": webdav_path, "fp": fingerprint}
                    # 每 500 个文件或每 2 秒输出进度
                    now = time.time()
                    if completed_count % 500 == 0 or (now - last_log_time) >= 2.0:
                        logging.info(
                            "[初始化] B 区扫描进度: %d/%d 个文件 (%.1fs)",
                            completed_count, total_files, now - t0)
                        last_log_time = now

        total_scanned = len(all_path_to_data)
        elapsed = time.time() - t0
        rate = total_scanned / elapsed if elapsed > 0 else 0
        logging.info(
            "[初始化] B 区磁盘扫描完毕，共发现 %d 个 STRM 文件 (%.2fs, %.0f 文件/秒)",
            total_scanned, elapsed, rate)
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

        """对比历史 DB 记录与磁盘数据，处理越界/迁移/删除。

        v6 P1 两波结构（镜像 `_insert_new_b_records` 的 wave1 并行预检模式）：
        - Wave0 分类（主线程，按原迭代序）：分支判定与现行逐条完全同口径；
          快照复用命中者按现行语义立即 processed；B 未命中者收集预检清单；
        - Wave1 并行预检：`_preflight_b_lineage_parallel` 产出
          (ok_map, stat_map, resolved_map)——步骤 1-4 纯读并行，线程池留在
          helper 体内（契约扫描禁内联）；
        - Wave2 主线程串行（按原迭代序）：预检放行免完整校验；未放行回退
          完整 `_verify_b_path_lineage`；分支 C（迁移）/D（删除）含物理副作用
          整段保持现行串行顺序不变（X8）。
        等价性前提：`_b_lineage_preflight` True ⇒ `_verify_b_path_lineage` True
        （verify 顶部即调预检并短路返回）；wave1→wave2 时效性论证见
        docs/否决方案.md「B 区历史校核血统预检并行化」条目（F-F）。
        """
        t_start = time.time()
        logging.info("[初始化] B 区历史记录核对开始 (%d 条)", len(db_records))
        disk_fingerprint_to_paths, disk_path_to_data = disk_data

        total_records = len(db_records)
        last_log_time = time.time()
        processed_count = 0

        # 只读预载缓存已由 initial_scan_b 顶层构建并接管生命周期（覆盖整个 B 扫描
        # 阶段，含新增记录阶段）；本方法不再自建/自清缓存。真实启动顺序是 A 先 B 后
        # （start() 先 initial_scan_a 再 initial_scan_b，生命周期测试断言此序），
        # 因此首启 A 表已含记录；缓存仅作预载加速，未命中回退 DB 与现状等价。
        try:
            # ================================================================
            # Wave0：分类（主线程，两遍串行保序消费，A1 c7 §5.3）。
            # pass1 按原迭代序收集已过现行四条件门（not force_full +
            # row_mapping_id 真值 + 指纹非空 + 磁盘指纹一致）的复用检查候选；
            # 复用判定经 _snapshot_reuse_check_parallel 并行（前置门 M-12/E3：
            # 只读预载缓存已构建且 scan mapping roots 已预载；否则整体回退
            # 串行）或串行逐行；pass2 按原序填 reused/pending。判定条件与
            # 现行逐条完全相同，Wave1/Wave2 零改动。
            # ================================================================
            _t_wave0 = time.perf_counter()
            reused_paths: set[str] = set()
            pending_files: list[tuple[str, str]] = []
            candidates: list[tuple[str, str]] = []
            for row in db_records:
                db_local_path = row.local_path
                db_fingerprint = row.fingerprint
                if not db_fingerprint:
                    continue
                if (db_local_path in disk_path_to_data
                        and disk_path_to_data[db_local_path]["fp"] == db_fingerprint):
                    row_mapping_id = getattr(row, "mapping_id", "") or self._mapping_id_for_b(db_local_path)
                    if not force_full and row_mapping_id:
                        candidates.append((db_local_path, db_fingerprint))
                    else:
                        # force_full 或空 mid 行：不经复用判定，直接进预检清单
                        pending_files.append(
                            (db_local_path, disk_path_to_data[db_local_path]["webdav"]))
            use_parallel_reuse = (
                self._reconcile_cache is not None
                and self._scan_mapping_roots is not None)
            if use_parallel_reuse:
                reused_set = self._snapshot_reuse_check_parallel(candidates)
            else:
                # M-12/E3 回退：cache 未构建（异常路径/直调 reconcile）→
                # 串行逐行，判定条件与并行 worker 完全一致
                reused_set = {
                    lp for lp, fp in candidates
                    if self._snapshot_reuses_valid_lineage(lp, fp)
                }
            # pass2：按原序消费复用判定结果（candidates 按原序收集，保序等价）
            for db_local_path, _fp in candidates:
                if db_local_path in reused_set:
                    reused_paths.add(db_local_path)
                    processed.add(db_local_path)
                    logging.debug("[B区历史核对] 复用有效 lineage snapshot: %s", db_local_path)
                else:
                    pending_files.append(
                        (db_local_path, disk_path_to_data[db_local_path]["webdav"]))
            # 观测面（run2/run3 锚点）：命中 = 快照复用；待核 = 进 Wave1/2
            logging.info(
                "[B区快照复用] M1: 命中 %d / 待核 %d (并行=%s)",
                len(reused_paths), len(pending_files), use_parallel_reuse)
            t_wave0 = time.perf_counter() - _t_wave0

            # ================================================================
            # Wave1：并行血统预检（第 1-4 步纯读；与 _insert_new_b_records
            # 调用形态完全同构——复用 helper，不新建池、不内联线程池字面量）。
            # ================================================================
            ok_map: dict[str, bool] = {}
            _t_wave1 = time.perf_counter()
            if pending_files:
                ok_map, _stat_map, _resolved_map = self._preflight_b_lineage_parallel(pending_files)
            else:
                # c.9.2 Task 7 Minor（防御性赋值）：pending 为空时显式置空，
                # 消除 Wave2 引用点的隐式「仅 pending 非空可达」不变式。
                _stat_map, _resolved_map = {}, {}
            t_wave1 = time.perf_counter() - _t_wave1

            # ================================================================
            # Wave2：主线程串行（按原迭代序）。预检放行免完整校验；未放行与
            # C/D 分支的副作用（越界删除、迁移、身份刷新、快照写）现行顺序不变。
            # ================================================================
            _t_wave2 = time.perf_counter()
            for row in db_records:
                processed_count += 1
                db_local_path = row.local_path
                db_fingerprint = row.fingerprint

                # 进度日志：每 100 条或每 2 秒（Step 5a：沿用现行节奏）
                current_time = time.time()
                if processed_count % 100 == 0 or (current_time - last_log_time) >= 2.0:
                    logging.info(
                        "[初始化] B 区历史记录对比进度: %d/%d 条",
                        processed_count, total_records
                    )
                    last_log_time = current_time

                # Wave0 快照复用命中：与现行的立即 processed+continue 等价
                if db_local_path in reused_paths:
                    continue

                if not db_fingerprint:
                    logging.debug("[B区历史核对] 删除无指纹记录: %s", db_local_path)
                    self.db.delete_b_by_local(db_local_path)
                    continue

                # 历史 DB 记录在磁盘上存在且指纹匹配
                if db_local_path in disk_path_to_data and disk_path_to_data[db_local_path]["fp"] == db_fingerprint:
                    webdav_path = disk_path_to_data[db_local_path]["webdav"]
                    row_mapping_id = getattr(row, "mapping_id", "") or self._mapping_id_for_b(db_local_path)
                    if ok_map.get(db_local_path, False):
                        # Wave1 预检放行：免完整校验（preflight True ⇒ verify
                        # True），快照写入保持 row_mapping_id 门（F-C 等价性）；
                        # A2：复用 Wave1 的 stat/resolve 结果
                        processed.add(db_local_path)
                        if row_mapping_id:
                            self._store_valid_lineage_snapshot(
                                db_local_path, db_fingerprint, buffered=True,
                                stat_result=_stat_map.get(db_local_path),
                                _resolved_target=_resolved_map.get(db_local_path))
                        continue
                    logging.debug("[B区历史核对] lineage 校验: %s", db_local_path)
                    t_op = time.time()
                    if not self._verify_b_path_lineage(db_local_path, webdav_path):
                        op_elapsed = time.time() - t_op
                        if op_elapsed > B_SCAN_SLOW_OPERATION_SECONDS:
                            logging.warning("[B区历史核对] lineage 校验耗时 %.1fs: %s", op_elapsed, db_local_path)
                        logging.warning("[B区历史越界清理] 物理删除历史遗留越界文件: %s", db_local_path)
                        logging.debug("[B区历史核对] 物理删除: %s", db_local_path)
                        if safe_remove_file(db_local_path):
                            logging.debug("[B区历史核对] DB删除: %s", db_local_path)
                            self.db.delete_b_by_local(db_local_path)
                        else:
                            logging.warning("[B区历史核对] 物理删除失败，保留 DB 记录: %s", db_local_path)
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
                        # A2：完整校验通过分支同样复用 Wave1 的 stat/resolve
                        self._store_valid_lineage_snapshot(
                            db_local_path, db_fingerprint, buffered=True,
                            stat_result=_stat_map.get(db_local_path),
                            _resolved_target=_resolved_map.get(db_local_path))
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
                        if not safe_remove_file(candidate_path):
                            logging.warning("[B区越界清理] 物理删除失败: %s", candidate_path)
                        processed.add(candidate_path)

                if valid_new_path:
                    logging.debug("[B区历史核对] 路径迁移: %s -> %s", db_local_path, valid_new_path)
                    self._handle_b_record_migration(db_local_path, valid_new_path, db_fingerprint)
                    self._store_valid_lineage_snapshot(valid_new_path, db_fingerprint, buffered=True)
                    processed.add(valid_new_path)

                else:
                    logging.debug("[B区历史核对] 无匹配磁盘路径，删除并刷新: %s", db_local_path)
                    self.db.delete_b_by_local(db_local_path)
                    mapping_id = getattr(row, "mapping_id", "") or self._mapping_id_for_b(db_local_path)
                    if mapping_id:
                        self.refresh_identity_current_b_path(db_fingerprint, mapping_id)
                    logging.debug("[B区自同步] 删除失效数据库记录: %s", db_local_path)

            t_wave2 = time.perf_counter() - _t_wave2
            elapsed_reconcile = time.time() - t_start
            rate_reconcile = total_records / elapsed_reconcile if elapsed_reconcile > 0 else 0
            logging.info(
                "[初始化] B 区历史记录核对完成 (%d/%d 条, %.2fs, %.0f 条/秒)",
                processed_count, total_records, elapsed_reconcile, rate_reconcile
            )
            # M1 插桩：三波分段计时输出（Wave0 分类+复用检查 / Wave1 并行预检 /
            # Wave2 串行校验+快照写；run1 曾无法分解的 13.4s 沉默段由此归因）
            logging.info(
                "[初始化] B 区历史核对分段(M1): Wave0=%.2fs Wave1=%.2fs Wave2=%.2fs",
                t_wave0, t_wave1, t_wave2)
        finally:
            # 缓冲快照最终刷新（含异常路径）；flush 内部自吞整体异常。
            # 缓存清理由 initial_scan_b 顶层 finally 兜底完成，本方法不重复自清。
            self._flush_pending_snapshots()

    def _handle_b_record_migration(self, old_path: str, new_path: str, fingerprint: str) -> None:
        """处理 B 区记录的路径迁移
        
        物理删除失败时回滚 DB move，保持磁盘↔DB 一致性。
        检查 old_path/new_path 磁盘状态，条件回滚并记录警告日志。
        """
        mapping_id = self._mapping_id_for_b(new_path) or self._mapping_id_for_b(old_path)
        if not mapping_id:
            logging.warning("[B区自同步] 无法解析 mapping，跳过路径迁移: %s -> %s", old_path, new_path)
            return
        self.db.move_b_record(old_path, new_path)
        identity = self.db.get_identity_by_fingerprint(fingerprint)
        if identity and identity.current_b_path == old_path:
            self.db.update_identity_b_path(fingerprint, new_path)
        
        # 物理删除失败已回滚 DB move（保持磁盘↔DB 一致性）
        # 物理删除失败的孤儿处理 - 回滚 DB move 保持磁盘↔DB 一致性
        delete_success = False
        try:
            old_path_obj = Path(old_path)
            if not old_path_obj.exists():
                # 正常重命名下旧路径已不存在，视为删除成功（无残留需清理），
                # 否则 delete_success 保持 False 会被误判为"删除失败"，
                # 落入 else 打出"需手动检查"并 return，跳过 ensure_single_visible_instance
                delete_success = True
            elif str(old_path_obj.resolve()) != str(Path(new_path).resolve()):
                if safe_remove_file(old_path_obj):
                    logging.debug("[B区自同步] 删除旧路径物理文件: %s", old_path)
                    delete_success = True
                else:
                    logging.warning("[B区自同步] 删除旧路径物理文件失败，回滚 DB 记录: %s", old_path)
        except Exception as e:
            logging.warning("[B区自同步] 删除旧路径物理文件失败，回滚 DB 记录: %s (%s)", old_path, e)
        
        # 如果物理删除失败，回滚 DB move 使记录与磁盘一致
        if not delete_success:
            try:
                # 检查 old_path 磁盘上确实存在且 new_path 不存在，才回滚
                old_exists = Path(old_path).exists()
                new_exists = Path(new_path).exists()
                if old_exists and not new_exists:
                    self.db.move_b_record(new_path, old_path)
                    if identity and identity.current_b_path == new_path:
                        self.db.update_identity_b_path(fingerprint, old_path)
                    logging.info("[B区自同步] 已回滚 DB 记录以保持一致性: %s", old_path)
                else:
                    # 如果 new_path 已存在或 old_path 不存在，状态已不一致，记录警告
                    logging.warning(
                        "[B区自同步] 物理删除失败且状态不一致 (old=%s, new=%s)，需手动检查",
                        old_exists, new_exists)
            except Exception as rollback_exc:
                logging.error(
                    "[B区自同步] 回滚 DB 记录失败，可能存在磁盘/DB 不一致: %s (%s)",
                    old_path, rollback_exc)
            return  # 跳过后续的 ensure_single_visible_instance，因为回滚了
        
        if fingerprint:
            # `mapping_id` 复用本函数上方 line 1429 已解析的变量，
            # 非二次调用 `self._mapping_id_for_b(...)`，勿"优化"改写。
            self.ensure_single_visible_instance(fingerprint, new_path, mapping_id=mapping_id)
        
        logging.info("[B区自同步] 更新路径(合法重命名): %s -> %s", old_path, new_path)

    def _insert_new_b_records(self, disk_data: tuple[dict, dict], processed: set) -> None:
        """插入磁盘上新的 B 区记录（批量单事务写入 + 锁外延迟去重）。

        批写契约（设计决策，见 docs/否决方案.md 批写契约条目）：
        - B 行严格全有或全无：任一批写失败即记录批次范围后重抛原异常，
          沿本方法 → initial_scan_b → start() 传播；失败批次不刷新身份、
          不并入 dedup 队列，且批事务整体回滚无半批残留。
        - snapshot 行维持尽力而为（写失败仅告警，不阻断 B 行落库）。
        """
        t_start = time.time()
        logging.info("[初始化] B 区新增记录处理开始...")
        _, disk_path_to_data = disk_data
        new_insert_count = 0

        b_batch: list[tuple] = []
        snapshot_batch: list[tuple] = []
        batch_fps: dict[tuple[str, str], str] = {}  # (fingerprint, mapping_id) → mapping_id
        dedup_queue: list[tuple[str, str, str]] = []
        pending_dedup: list[tuple[str, str, str]] = []

        def _flush_batch() -> None:
            if not b_batch:
                return
            # 1) 单事务原子写入 B 记录与 lineage snapshots——失败整批回滚并上抛
            try:
                written = self.db.upsert_b_records_and_snapshots_batch(
                    b_batch, snapshot_batch)
            except Exception as e:
                logging.error(
                    "[初始化] B 区新增记录批写失败（本批 %d 条，已整批回滚）: %s",
                    len(b_batch), e, exc_info=True)
                raise
            if written != len(b_batch):
                raise RuntimeError(
                    f"[初始化] B 区新增记录批写数量不符: 实际持久化 {written} 行"
                    f" != 本批 {len(b_batch)} 行")
            # 2) 仅批写 commit 成功后，批量刷新身份投影（Task 4：单连接预读，消除
            #    逐条 refresh 建连风暴；DB 已存在记录，不会误删投影）
            try:
                self._refresh_identities_batch(list(batch_fps))
            except Exception as e:
                logging.warning(
                    "[初始化] 身份投影批量刷新失败（本批 %d 项）: %s",
                    len(batch_fps), e, exc_info=True)
            # 3) 本批 dedup 候选仅并入全局队列（失败批次候选已由上抛剔除）
            dedup_queue.extend(pending_dedup)
            pending_dedup.clear()
            b_batch.clear()
            snapshot_batch.clear()
            batch_fps.clear()

        # wave1（并行）：对全部未处理磁盘文件执行血统预检（第 1-4 步纯读）+ stat。
        # 设计决策: 启动血统预检并行化（T2b）——步骤 1-4 纯读可并行；第 5-9 步
        # 副作用面（越界删除 safe_remove_file / solo-episode Timer / 边界写）
        # 保持 wave2 主线程串行段，顺序与现行完全一致（disk_path_to_data 原迭代序）。
        pending_paths = [
            disk_path for disk_path in disk_path_to_data
            if disk_path not in processed]
        ok_map: dict[str, bool] = {}
        stat_map: dict[str, os.stat_result | None] = {}
        resolved_map: dict[str, Path] = {}
        if pending_paths:
            ok_map, stat_map, resolved_map = self._preflight_b_lineage_parallel(
                [(p, disk_path_to_data[p]["webdav"]) for p in pending_paths])

        for disk_path, data in disk_path_to_data.items():
            if disk_path not in processed:
                webdav_path = data["webdav"]
                fingerprint = data["fp"]

                if not ok_map.get(disk_path, False):
                    # 预检未放行（罕见）：完整 _verify_b_path_lineage 串行回退——
                    # 确定性重跑第 1-4 步后进第 5-9 步，保留越界删除与
                    # solo-episode/边界写副作用在主线程串行执行
                    if not self._verify_b_path_lineage(disk_path, webdav_path):
                        logging.warning("[B区越界清理] 发现非法新增跨区复制文件，物理删除: %s", disk_path)
                        if not safe_remove_file(disk_path):
                            logging.warning("[B区越界清理] 物理删除失败: %s", disk_path)
                        continue

                mapping = self._get_mapping_for_b_fast(
                    disk_path, _resolved_target=resolved_map.get(disk_path))
                if not mapping:
                    logging.warning("[初始化] 无法解析 B mapping，跳过记录: %s", disk_path)
                    continue
                mapping_id = mapping[0]

                b_batch.append((
                    disk_path,
                    webdav_path,
                    webdav_parent(webdav_path),
                    None,  # source_a_path 在初始化扫描阶段未知，后续由 A→B 同步补全
                    fingerprint,
                    mapping_id,
                    "valid",
                ))

                stat_info = stat_map.get(disk_path)
                if stat_info is not None:
                    snapshot_batch.append((
                        mapping_id,
                        disk_path,
                        stat_info.st_size,
                        stat_info.st_mtime_ns,
                        fingerprint,
                        self._mapping_version,
                        LINEAGE_VERSION,
                        "valid",
                    ))
                else:
                    logging.warning("[B区快照] 获取文件 stat 失败: %s", disk_path)

                if fingerprint:
                    batch_fps[(fingerprint, mapping_id)] = mapping_id
                    pending_dedup.append((fingerprint, disk_path, mapping_id))

                processed.add(disk_path)
                new_insert_count += 1

                if len(b_batch) >= 1000:
                    _flush_batch()
                    logging.info("[初始化] B 区新增记录进度: %d 条", new_insert_count)

        # 尾批 flush
        _flush_batch()

        # 3) 锁外统一执行延迟去重与隔离改名（Task 5：预筛选仅对真实存在重复的指纹执行）
        if dedup_queue:
            dup_groups = None
            try:
                dup_groups = self.db.get_duplicate_fingerprint_groups()
                dup_set = {(mid, fp) for mid, fp, _ in dup_groups}
            except Exception as e:
                logging.warning("[初始化] 去重预筛选查询失败，回退全量去重: %s", e)
                dup_set = None
                dup_groups = None  # C13-4：查询/迭代失败（含 Mock 不可迭代）不得进批量入口

            if dup_set is not None:
                filtered_queue = [
                    (fp, dpath, mid) for fp, dpath, mid in dedup_queue
                    if (mid, fp) in dup_set
                ]
            else:
                filtered_queue = dedup_queue

            if filtered_queue:
                logging.info(
                    "[初始化] B 区新增记录去重检查开始 (%d 项，预筛过滤后 %d 项)...",
                    len(dedup_queue), len(filtered_queue))
                # 已知取舍: 单条去重失败记 ERROR 后继续处理后续组（不阻断启动）。
                # 与 B3 预标/分叉恢复条目并列；杀毒锁等瞬时故障不应打挂启动。
                # 设计决策: 启动上下文（watchers 未启动）经 quarantine_session
                # 会话批量路径等价执行隔离（T1）；预筛查询失败（dup_groups 为
                # None，含 Mock 不可迭代）回退现行逐条路径（C13-4，禁止冒泡）。
                if dup_groups is not None and not self._watchers_live():
                    # C12-2: 复用上方既有 GROUP BY 结果分组（消除双入队浪费，
                    # prefer = 组内首个队列项路径，与现行第一次 ensure 触发路径一致）
                    first_path: dict[tuple[str, str], str] = {}
                    for fp, dpath, mid in filtered_queue:
                        first_path.setdefault((mid, fp), dpath)
                    batch_groups = [
                        (mid, fp, first_path[(mid, fp)])
                        for mid, fp, _ in dup_groups
                        if (mid, fp) in first_path]
                    if batch_groups:
                        self._quarantine_duplicate_groups_batch(batch_groups)
                    else:
                        logging.debug(
                            "[初始化] 去重预筛后无待隔离组，跳过批量隔离")
                else:
                    for fp, dpath, mid in filtered_queue:
                        try:
                            self.ensure_single_visible_instance(fp, dpath, mapping_id=mid)
                        except Exception as e:
                            logging.error("[初始化] 去重失败 %s: %s（继续处理后续记录）", dpath, e, exc_info=True)
            else:
                logging.debug(
                    "[初始化] B 区新增记录去重预筛选：全无重复，跳过全部 %d 项 ensure 调用",
                    len(dedup_queue))

        elapsed = time.time() - t_start
        rate = new_insert_count / elapsed if elapsed > 0 else 0
        logging.info(
            "[初始化] B 区新增记录处理完成: %d 条 (%.2fs%s)",
            new_insert_count, elapsed, f", {rate:.0f} 条/秒" if new_insert_count else "")

    def initial_scan_a(
            self, use_bulk: bool = False,
            a_roots: list[Path] | None = None,
            use_snapshot: bool = True):
        return self.sync_service.initial_scan_a(
            use_bulk=use_bulk, a_roots=a_roots, use_snapshot=use_snapshot)

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
                    if safe_remove_file(rec.local_path):
                        self.db.delete_a_by_local(rec.local_path)
                    else:
                        logging.warning("[初始化] A 区冗余清理：物理删除失败，保留 DB 记录: %s", rec.local_path)
                    # 已知取舍: 无论删除是否成功都设 ghost。若物理删除失败，A 记录仍在 DB，
                    # 但其 webdav_path 已被 ghost 屏蔽，后续 A→B 同步会跳过该仍有效文件。
                    # 该路径休眠（0 生产调用），有 WebUI 手动刷新替代，接受。
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

    def _start_subtitle_scan_background(self) -> None:
        """启动受控后台线程执行 A 区字幕补偿扫描。"""
        def _run() -> None:
            try:
                logging.info("[字幕后台] 开始异步清理失效字幕记录...")
                self.db.cleanup_invalid_subtitles(
                    cancel_event=self._subtitle_scan_cancel_event)
                logging.info("[字幕后台] 失效字幕记录清理完成")
                if self._subtitle_scan_cancel_event.is_set():
                    return
                self._scan_a_subtitles_on_startup(
                    cancel_event=self._subtitle_scan_cancel_event)
            except Exception:
                logging.exception("[初始化] A 区字幕后台扫描发生未捕获异常")

        thread = threading.Thread(
            target=_run,
            name="SubtitleScanStartup",
            daemon=True)
        self._subtitle_scan_thread = thread
        thread.start()

    def _scan_a_subtitles_on_startup(
            self, cancel_event: threading.Event | None = None) -> None:
        """扫描 A 区字幕文件（补偿 initial_scan_a 不处理字幕）。

        c.9.2 Task 12：开头单次全表预读 subtitles（local_path → target_path），
        walk 命中的字幕先判「row 存在 ∧ target 存在 → 跳过」，未命中/目标缺失
        才走原 process_subtitle_file（watcher 单文件路径零改动）。skip 判定与
        process_subtitle_file 现行逻辑逐条等价（不比对指纹）。查询键 =
        str(Path(p).resolve())——DB 存储键是 resolve 后的串，walk 原始串仅在
        a_root 已规范化时等值；键不一致不是正确性问题（miss 落回原路径），
        但会让批量收益静默落空。count 口径不变：总数含被跳过的，跳过数另行
        INFO 输出。
        """
        logging.info("[初始化] 扫描 A 区字幕文件...")
        t0 = time.time()
        count = 0
        skipped = 0
        try:
            known_targets: dict[str, str] = self.db.get_subtitle_path_targets()
            if not isinstance(known_targets, dict):
                known_targets = {}
        except Exception:
            logging.warning("[初始化] 字幕记录批量预读失败，回退逐文件处理",
                            exc_info=True)
            known_targets = {}
        for a_root in self.a_roots:
            if cancel_event is not None and cancel_event.is_set():
                logging.info("[初始化] 收到取消信号，中止 A 区字幕扫描")
                return
            if not a_root.exists():
                continue
            for root, _dirs, files in os.walk(a_root):
                if cancel_event is not None and cancel_event.is_set():
                    logging.info("[初始化] 收到取消信号，中止 A 区字幕扫描")
                    return
                for name in files:
                    if cancel_event is not None and cancel_event.is_set():
                        logging.info("[初始化] 收到取消信号，中止 A 区字幕扫描")
                        return
                    sub_path = Path(root) / name
                    if is_subtitle_file(sub_path):
                        count += 1
                        row_target = known_targets.get(str(sub_path.resolve()))
                        if row_target is not None and Path(row_target).exists():
                            skipped += 1
                            continue
                        self.process_subtitle_file(sub_path)
        logging.info(
            "[初始化] A 区字幕扫描完成，处理 %d 个文件 (%.1fs)",
            count, time.time() - t0)
        if skipped:
            logging.info(
                "[初始化] A 区字幕扫描: 其中 %d 个已处理跳过（预读命中，无落盘动作）",
                skipped)


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
            if safe_remove_file(local_path):
                self.db.delete_b_by_local(local_path)
                if fingerprint:
                    self.refresh_identity_current_b_path(fingerprint, mapping_id)
                removed_count += 1
                logging.info(
                    "[冗余清理] 已移除失效STRM(WebDAV不存在): %s -> %s",
                    local_path,
                    webdav_path)
            else:
                logging.warning("[冗余清理] 物理删除失败，跳过DB删除: %s", local_path)
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
        """死代码——原 update 模式冗余清理，现已被
        `cleanup_a_redundant_using_api` 取代，保留仅为兼容旧调用路径。"""
        if not engine_path:
            return
        # 规范化路径前缀，避免 /movies 误匹配 /movies_extra
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
                if safe_remove_file(local_path):
                    self.db.delete_a_by_local(local_path)
                    self.db.set_ghost_protection(
                        webdav_path,
                        self.config.behavior.ghost_protect_seconds,
                        reason="cloud_deleted",
                    )
                else:
                    logging.warning("[A区清理] 物理删除失败，跳过DB删除: %s", local_path)

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
            # 解析失败 = 当前无可信权威链接，旧快照行不得留给采信门复用
            self.db.delete_a_snapshot(str(local))
            return
        parent = webdav_parent(webdav_path)
        self.db.upsert_a(str(local), webdav_path, parent)
        # E2：A 区 watcher 改写后即时失效快照行，下轮扫描以当前 size/mtime 重读重建
        self.db.delete_a_snapshot(str(local))
        self.db.save_known_folder(parent, source="a")
        fingerprint = make_strm_fingerprint(webdav_path)
        # 按 fingerprint 串行化，避免并发创建 B 实例的 TOCTOU 竞争
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
                # 检查物理删除结果，避免物理/DB不一致
                if safe_remove_file(str(local)):
                    self.db.delete_a_by_local(str(local))
                    self.db.set_ghost_protection(
                        webdav_path,
                        self.config.behavior.ghost_protect_seconds,
                        reason="webdav_not_exists")
                else:
                    logging.warning("[A区清理] 物理删除失败，跳过DB删除: %s", local)
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
                # 检查磁盘文件是否实际存在，避免基于已删除文件的评分比较
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
                        # 检查物理删除结果，避免物理/DB不一致
                        if safe_remove_file(a_local_path):
                            logging.info("[A区清理] 删除冗余STRM: %s", a_local_path)
                            self.db.delete_a_by_local(a_local_path)
                            self.db.set_ghost_protection(
                                webdav_path,
                                self.config.behavior.ghost_protect_seconds,
                                reason="webdav_not_exists")
                        else:
                            logging.warning("[A区清理] 物理删除失败，跳过DB删除: %s", a_local_path)
                    return
            # 把 copy_a_record_to_b 移入 fp_lock 块内，避免 TOCTOU 竞争
            # 增加 try/except 记录 A 路径和 mapping 上下文
            try:
                self.copy_a_record_to_b(str(local), webdav_path, parent, mapping_id=mapping_id)
            except Exception:
                logging.exception(
                    "[A->B复制失败] A路径=%s, WebDAV=%s, mapping=%s",
                    local, webdav_path, mapping_id)

    def handle_a_deleted(self, local_path: str) -> None:
        if Path(local_path).exists():
            logging.debug(
                "[A区跳过] 文件仍存在，可能是openlist引擎的同步操作:删除strm又新建: %s",
                local_path)
            return
        row = self.db.get_a_by_local(local_path)
        self.db.delete_a_by_local(local_path)
        # E2：watcher 删除后同步清除快照行，消除孤儿快照
        self.db.delete_a_snapshot(local_path)
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
                           webdav_path: str, parent: str,
                           mapping_id: str = "") -> bool | None:
        return self.sync_service.copy_a_record_to_b(
            a_local_path, webdav_path, parent, mapping_id=mapping_id)

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

    def _b_file_score_pure(self, path: str, webdav_path: str | None = None) -> tuple:
        """纯内存计算 B 文件去重优选评分（免除逐条 get_b_by_local_full DB 读）。

        match_count 升序偏好少匹配=更多用户改动=优先保留（已验证设计意图）。
        返回值 (is_standard_flag, match_count, path_len, lowercase_name)
        """
        p = Path(path)
        name = p.name.lower()
        is_standard = self._is_standard_media_name(name)
        mapping = self._get_mapping_for_b_fast(p)
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
        if webdav_path:
            try:
                canonical_webdav = _canonicalize_webdav_path_for_cloud(webdav_path)
                webdav_parts = [
                    part for part in canonical_webdav.strip("/").split("/") if part]
            except Exception as e:
                logging.debug("[文件评分] webdav_parts 解析失败: %s", e)
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

    def _b_file_score(self, path: str) -> tuple:
        webdav_path = None
        try:
            row = self.db.get_b_by_local_full(path)
            if row:
                webdav_path = row.webdav_path
        except Exception as e:
            logging.debug("[文件评分] get_b_by_local_full 失败: %s", e)
        return self._b_file_score_pure(path, webdav_path)

    def ensure_single_visible_instance(
            self, fingerprint: str, trigger_path: str,
            prefer_path: str | None = None, mapping_id: str | None = None) -> None:
        """确保同一 fingerprint 只有一个 visible 实例。
        
        Args:
            fingerprint: 文件指纹
            trigger_path: 触发检查的路径
            prefer_path: 可选，评分相同时优先保留的路径
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
        # 预建 local_path -> webdav_path 映射，评分用纯内存版（免除逐条 get_b_by_local_full）
        webdav_map: dict[str, str | None] = {
            row.local_path: row.webdav_path for row in all_instances}
        # 排序，评分相同且 prefer_path 存在时让 prefer_path 排在前面
        prefer_path = prefer_path or trigger_path
        def _sort_key(path: str) -> tuple:
            score = self._b_file_score_pure(path, webdav_map.get(path))
            # 评分相同时 prefer_path 优先（更低排序值）
            return (score, 0 if path == prefer_path else 1)
        valid_files.sort(key=_sort_key)
        keep = valid_files[0]
        # 已知取舍: 预标阶段一次性把所有兄弟实例置为 duplicate（而非逐个处理时
        # 再标记），换取"物理隔离失败时逐个恢复 valid"的可重试语义。副作用：若
        # 下方循环中某次隔离异常 raise（如回滚也失败），该兄弟之后的未处理实例
        # 会保持 DB=duplicate / 磁盘=.strm 的分叉，且 valid_files 过滤器（status=
        # valid）使本函数永不重试它们。触发窗口极窄（磁盘满/杀毒锁文件叠加），
        # 权衡后接受，登记于 docs/否决方案.md B3 子注。
        duplicate_paths = self.db.mark_other_b_instances_duplicate(
            fingerprint, keep, mapping_id)
        for dup_path in duplicate_paths:
            dup = Path(dup_path)
            if not dup.exists():
                continue
            # 在物理隔离前标记，防止 quarantine_file 的改名事件
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
                        # DB 迁移失败（目标被占/冲突）— 回滚物理改名，
                        # 保持 DB local_path 与文件系统一致，避免两者分叉。
                        try:
                            Path(quarantined).rename(dup)
                            # mark_other 已把 status 标为 duplicate，
                            # 物理已回滚到原 .strm → 恢复 valid，避免假 duplicate 死锁。
                            self.db.mark_b_instance_status(str(dup), "valid")
                            logging.warning(
                                "[B区重复] DB迁移失败，已回滚物理改名: %s", dup)
                        except OSError as revert_err:
                            # 物理已在 quarantined，回滚失败 → 把 DB
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
                    # 物理隔离失败时撤销 mark_other 留下的假 duplicate，
                    # 恢复 status=valid，避免「DB=duplicate / 磁盘仍为 .strm」
                    # 导致 ensure 永不重试的死锁。
                    self.db.mark_b_instance_status(str(dup), "valid")
                    logging.warning("[B区重复] 重复实例隔离失败: %s", dup)
            finally:
                # 延迟清除标记，确保 watchdog 事件已被处理
                self._clear_engine_internal_delayed(fingerprint)

    def _ensure_groups_per_item(
            self, groups: list[tuple[str, str, str]]) -> int:
        """逐条 ensure 回退路径（C13-4：批量入口容错，禁止静默跳过去重）。

        与现行 _insert_new_b_records / _cleanup_startup_duplicates 的逐组
        ensure 循环同形：单组失败记 ERROR 后继续。返回处理的组数。
        """
        handled = 0
        for mapping_id, fingerprint, prefer_path in groups:
            try:
                self.ensure_single_visible_instance(
                    fingerprint, prefer_path, mapping_id=mapping_id)
                handled += 1
            except Exception as e:  # noqa: BLE001
                logging.error(
                    "[B区重复] 去重失败 %s: %s（继续处理后续组）",
                    prefer_path, e, exc_info=True)
        return handled

    def _quarantine_duplicate_groups_batch(
            self, groups: list[tuple[str, str, str]]) -> int:
        """启动期重复隔离批量化（T1，仅 watchers 未启动的启动上下文）。

        设计决策: 共享连接会话（quarantine_session，消除每实例建连 +
        每连接 simple 分词器词典加载 ~98ms/连接）+ 逐组事务（保失败隔离）+
        逐组 rw_lock（与现行锁语义一致）+ 4 线程并行物理改名 + 双入队消除 +
        happy 冗余写移除 + watchers 未启动免引擎标记（B3 预标路径不进入、
        延迟清除线程零 spawn）。watcher/refresh 上下文仍由
        ensure_single_visible_instance 逐条执行（字节不变）。

        Args:
            groups: (mapping_id, fingerprint, prefer_path) 组列表——入参即组
                列表（C12-2），由调用方复用各自既有 get_duplicate_fingerprint_groups
                GROUP BY 结果传入，本函数不查询重复组。

        预读复用既有 Database.get_all_b_rows_for_fp_pairs（C2：不新增方法；
                900 切片已内置；读行集≠重复组集，非双重查询）。
        预读/评分失败（含 Mock 返回不可迭代对象）→ 回退逐条 ensure（C13-4）。
        组内重试转移至 _cleanup_startup_duplicates 兜底（已知取舍，登记册）。
        """
        if not groups:
            return 0
        try:
            pairs = [(fp, mid) for mid, fp, _ in groups]
            rows = self.db.get_all_b_rows_for_fp_pairs(pairs)
            by_group: dict[tuple[str, str], list] = defaultdict(list)
            for r in rows:
                by_group[(r.mapping_id, r.fingerprint)].append(r)
            webdav_map = {r.local_path: r.webdav_path for r in rows}
        except Exception as e:  # noqa: BLE001
            logging.warning("[B区重复] 批量预读失败，回退逐条隔离: %s", e)
            return self._ensure_groups_per_item(groups)

        # 内存评分选 keep：完全复用现行规则——status='valid' 且 Path.exists()
        # 过滤（全组 valid+existing 为空则跳过整组）+ _b_file_score_pure 排序 +
        # prefer_path（组内首个队列项路径）优先。
        prefer_by_group = {(mid, fp): prefer for mid, fp, prefer in groups}
        plans: list[tuple[tuple[str, str], str, list[str]]] = []
        for gkey in prefer_by_group:
            group_rows = by_group.get(gkey)
            if not group_rows:
                continue
            valid_files = [
                r.local_path for r in group_rows
                if r.status == "valid" and Path(r.local_path).exists()]
            if not valid_files:
                continue
            prefer = prefer_by_group[gkey]
            valid_files.sort(key=lambda p: (
                self._b_file_score_pure(p, webdav_map.get(p)),
                0 if p == prefer else 1))
            keep = valid_files[0]
            dups = [p for p in valid_files if p != keep]
            if dups:
                plans.append((gkey, keep, dups))
        if not plans:
            logging.debug("[B区重复] 批量隔离：无可隔离组（%d 组入参）", len(groups))
            return 0
        logging.info(
            "[B区重复] 批量隔离开始: %d 组（入参 %d 组，会话批量化）",
            len(plans), len(groups))

        total_moved = 0
        session_cm = None
        try:
            session_cm = self.db.quarantine_session()
            session = session_cm.__enter__()
        except Exception as e:  # noqa: BLE001
            logging.warning("[B区重复] 隔离会话建连失败，回退逐条隔离: %s", e)
            return self._ensure_groups_per_item(groups)

        CHUNK = 32
        try:
            with ThreadPoolExecutor(max_workers=4) as executor:
                # 设计决策: 改名线程池必须留在本函数体内（C13-1）——契约测试
                # 以字符串扫描断言 _insert_new_b_records 体不含 ThreadPoolExecutor
                # 字面量，内联即挂契约测试。
                for chunk_start in range(0, len(plans), CHUNK):
                    chunk = plans[chunk_start:chunk_start + CHUNK]
                    futures: dict[Any, tuple[tuple[str, str], str]] = {}
                    for gkey, _keep, dups in chunk:
                        for dup in dups:
                            futures[executor.submit(
                                quarantine_file, Path(dup), ".duplicate")] = (
                                gkey, dup)
                    rename_results: dict[str, Path | None] = {}
                    for future in as_completed(futures):
                        _gkey, dup = futures[future]
                        try:
                            rename_results[dup] = future.result()
                        except Exception as e:  # noqa: BLE001
                            logging.warning(
                                "[B区重复] 并行隔离改名异常: %s (%s)", dup, e)
                            rename_results[dup] = None
                    # 块内全部改名返回后，主线程逐组 run_group（组序 = 队列序）
                    for gkey, keep, dups in chunk:
                        renamed = [
                            (dup, str(rename_results[dup])
                             if rename_results.get(dup) is not None else None)
                            for dup in dups]
                        mid, fp = gkey
                        try:
                            moved = session.run_group(fp, mid, keep, renamed)
                            total_moved += moved
                            if moved:
                                logging.warning(
                                    "[B区重复] 已批量隔离 %d 个重复实例 (保留=%s)",
                                    moved, keep)
                        except Exception as e:  # noqa: BLE001
                            # 组事务已回滚（全部行回 valid 原路径）→ 逐文件物理
                            # 回滚改名；回滚也失败的实例用独立新连接 move_b_record
                            # 对齐 DB（B3-B 镜像）→ ERROR → 继续下一组。
                            logging.error(
                                "[B区重复] 组事务失败 mid=%s fp=%s: %s"
                                "（整组回滚，继续后续组）", mid, fp, e, exc_info=True)
                            for old_path, quarantined in renamed:
                                if quarantined is None:
                                    continue
                                try:
                                    Path(quarantined).rename(old_path)
                                except OSError as revert_err:
                                    try:
                                        aligned = self.db.move_b_record(
                                            old_path, quarantined)
                                        if aligned:
                                            self.db.mark_b_instance_status(
                                                quarantined, "duplicate")
                                            logging.error(
                                                "[B区重复] 回滚失败，已将 DB 对齐到隔离路径: "
                                                "%s -> %s", old_path, quarantined)
                                        else:
                                            logging.error(
                                                "[B区重复] 回滚失败且 DB 对齐隔离路径也失败: "
                                                "%s -> %s", old_path, quarantined)
                                    except Exception as align_err:  # noqa: BLE001
                                        logging.error(
                                            "[B区重复] 回滚失败后 DB 对齐异常: %s -> %s: %s",
                                            old_path, quarantined, align_err)
                                    logging.error(
                                        "[B区重复] 组回滚中物理改名回滚失败: %s -> %s: %s",
                                        old_path, quarantined, revert_err)
        finally:
            if session_cm is not None:
                session_cm.__exit__(None, None, None)
        logging.info(
            "[B区重复] 批量隔离完成: 迁移 %d 实例（%d 组）", total_moved, len(plans))
        return total_moved

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
                # 检查物理删除结果，避免物理/DB不一致
                if safe_remove_file(local):
                    if row:
                        self.db.delete_b_by_local(str(local))
                else:
                    logging.warning("[B区拦截] 物理删除失败，保留DB记录: %s", local)
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
        # 优先使用 C 区隔离而非直接删除（保留历史追溯能力）
        try:
            # 检查是否有 mapping_id 和 root 信息可用于 C 区迁移
            mapping = self.get_mapping_for_b(local_path)
            if mapping:
                # get_mapping_for_b 返回 (mapping_id, b_root, a_root)，
                # 原解构写成 (mapping_id, a_root, b_root) 导致 b_root 实为 a_root、
                # get_c_path_for_b 的 relative_to 永远失败、C 区隔离失效、回退直接删除。
                mapping_id, b_root, _a_root = mapping
                # 尝试迁移文件到 C 区（而非直接删除）
                try:
                    c_target = self.get_c_path_for_b(mapping_id, local_path, b_root)
                    c_target.parent.mkdir(parents=True, exist_ok=True)
                    # copyfile 在恢复锁外是有意设计（避免死锁）
                    # handle_b_deleted 在锁内检查，_restoring_generation 计数器防并发恢复竞争。勿把 copyfile 移入锁内。
                    move_file(local, c_target)
                    moved = self.db.move_b_record(local_path, str(c_target))
                    if moved:
                        self.db.mark_b_instance_status(str(c_target), "quarantined")
                        logging.info("[B区越界恢复] 已将越界文件移入C区隔离: %s -> %s", local_path, c_target)
                        return  # C区迁移成功，不删除DB记录
                    # move_b_record 返回 False（目标被占/冲突）——对齐 B 区去重恢复思路：
                    # 回退物理移动或对齐 DB，避免「文件已在 C 区 / DB 行仍指向旧 B 路径」分叉
                    try:
                        Path(c_target).rename(local)
                        logging.warning("[B区越界恢复] DB迁移失败，已回滚物理移动: %s", local_path)
                    except OSError as revert_err:
                        try:
                            aligned = self.db.move_b_record(local_path, str(c_target))
                            if aligned:
                                self.db.mark_b_instance_status(str(c_target), "quarantined")
                                logging.error(
                                    "[B区越界恢复] 回滚失败，已将 DB 对齐到 C 区路径: %s -> %s",
                                    local_path, c_target)
                                return
                            logging.error(
                                "[B区越界恢复] 回滚失败且 DB 对齐 C 区路径也失败: %s -> %s",
                                local_path, c_target)
                        except Exception as align_err:
                            logging.error(
                                "[B区越界恢复] 回滚失败后 DB 对齐异常: %s -> %s: %s",
                                local_path, c_target, align_err)
                        logging.error(
                            "[B区越界恢复] DB迁移失败且回滚物理移动失败: %s -> %s: %s",
                            local_path, c_target, revert_err)
                        raise
                except Exception as e:
                    logging.warning("[B区越界恢复] C区迁移失败，回退到直接删除: %s", e)
        except Exception as e:
            logging.warning("[B区越界恢复] C区迁移失败，回退到直接删除: %s", e)
        
        # C区迁移失败时，回退到直接删除（原逻辑）
        # 注意：先确认物理删除成功后再清理 DB 记录，防止 DB/磁盘不一致
        deleted = self._force_delete_and_verify(local)
        if not deleted:
            logging.error("[B区越界恢复] 无法删除越界文件，跳过恢复: %s", local_path)
            return
        self.db.delete_b_by_local(local_path)
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
                                # 代际未变化才清除
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
        # 物理隔离前标记 fingerprint 为引擎内部操作，
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
        # 物理隔离前标记 fingerprint 为引擎内部操作，
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
                # 前缀匹配需带路径边界，避免 "/cloud/番剧" 误配
                # "/cloud/番剧2/x.strm"。与其它前缀检查（prefix + "/"）口径一致。
                mp_norm = mp.rstrip("/")
                if cloud_path == mp_norm or cloud_path.startswith(mp_norm + "/"):
                    relative = cloud_path[len(mp_norm):].lstrip("/")
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
            if not self.admin_api.login(source="index_update"):
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
            fingerprint = row.fingerprint
            with self._restoring_lock:
                # 恢复操作标记：程序自身正在恢复此指纹的文件，跳过记录清理
                if fingerprint in self._restoring_markers:
                    logging.info("[B区重命名] 检测到程序恢复操作，跳过记录清理: %s", local_path)
                    return
                # 引擎内部删除标记：隔离/去重/迁移等程序自身操作，仅清理 DB 记录
                if fingerprint in self._engine_internal_markers:
                    logging.info(
                        "[B区重命名] 检测到程序内部删除（隔离/去重/迁移），仅清理 DB 记录: %s",
                        local_path)
                    self.db.delete_b_by_local(str(local))
                    return
            self.db.delete_b_by_local(str(local))
            logging.info("[B区重命名] .strm 重命名为非 .strm，已从数据库移除记录: %s", local_path)

    def handle_b_quarantined_deleted(self, quarantined_path: str) -> None:
        """清理已物理删除的隔离 B 文件，仅更新本地投影，不联动云端/A 区。"""
        local = Path(quarantined_path).resolve()
        lock = self.get_path_lock(local)
        with lock:
            row = self.db.get_b_by_local_full(str(local))
            if not row:
                logging.debug("[B区隔离删除] DB 无对应记录，忽略: %s", local)
                return
            if row.status not in ("duplicate", "quarantined", "invalid"):
                logging.warning(
                    "[B区隔离删除] 状态不可信，fail-closed 保留记录: %s status=%s",
                    local, row.status)
                return
            self.db.delete_b_by_local(str(local))
            if row.fingerprint and row.mapping_id:
                self.refresh_identity_current_b_path(
                    row.fingerprint, row.mapping_id)
            logging.info("[B区隔离删除] 已清理本地记录及身份投影: %s", local)

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
                # 引擎内部删除标记：隔离/去重/迁移等程序自身操作触发的
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
            # 获取指纹锁，防止 handle_b_created_or_modified 在检查和删除之间
            # 创建同 fingerprint 的新 B 实例导致 TOCTOU 竞态。
            fp_lock = self.get_fingerprint_lock(fingerprint)
            with fp_lock:
                # 二次确认：在指纹锁内重新检查 B 区是否有同指纹实例
                if fingerprint and self.db.has_other_b_instance(
                        mapping_id, fingerprint, str(local)):
                    logging.info(
                        "[B区删除联动] 指纹锁内二次确认：B区中仍存在同指纹文件，跳过WebDAV删除: %s",
                        local_path)
                    self.db.delete_b_by_local(str(local))
                    return
                if fingerprint and self._check_fingerprint_exists_in_b(
                        fingerprint,
                        exclude_path=str(local), mapping_id=mapping_id):
                    logging.info(
                        "[B区删除联动] 指纹锁内二次确认：B区文件系统中仍存在同指纹文件，跳过WebDAV删除: %s",
                        local_path)
                    self.db.delete_b_by_local(str(local))
                    return
                # 云端 MOVE/DELETE 成功才联动清理；失败保留 A 区/A 记录
                if webdav_path:
                    ok = self._execute_webdav_deletion(webdav_path, parent_webdav_path)
                    if not ok:
                        logging.warning(
                            "[B区删除联动] 云端删除失败，保留 A 区记录以便重试: %s",
                            webdav_path)
                        return
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

        原 on_moved 在 watchdog 事件线程内同步调用 db.move_b_record，
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
        # webdav 路径使用独立命名空间的锁（get_webdav_lock），
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
            if safe_remove_file(a_path):
                logging.info("[A区删除] B区删除联动，清理A区: %s", a_path)
                self.db.delete_a_by_local(a_path)
            else:
                logging.warning("[A区删除] 物理删除失败，跳过DB删除: %s", a_path)

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
                # 写操作后失效 check_exists 缓存，避免陈旧 True
                self.admin_api.invalidate_check_exists_cache(cloud_path)
                self.admin_api.invalidate_check_exists_cache(trash_path)
            return ok

        # 只有显式 action == "DELETE" 才放行硬删除。
        # 原实现把所有非 MOVE 值（含小写 "move"/"delete"、拼写错误、None 等）
        # 一律落入 DELETE 分支，属 fail-open。未知/异常 action 一律 fail-closed：
        # 返回 False + 高声告警，绝不执行不可逆的云端删除。
        if action == "DELETE":
            logging.debug("[云盘操作] 执行删除: %s (action=%s)", cloud_path, action)
            ok = self.admin_api.remove(cloud_path)
            if not ok:
                logging.error("[云盘操作] 删除失败: %s", cloud_path)
            else:
                logging.info("[云盘操作] 删除成功: %s", cloud_path)
                # 写操作后失效 check_exists 缓存，避免陈旧 True
                self.admin_api.invalidate_check_exists_cache(cloud_path)
            return ok

        # 未知 action：fail-closed，软性告警，不硬删除。
        logging.error(
            "[云盘操作] ⚠ 未知/非法 action=%r，已拒绝执行。"
            "仅支持 MOVE（回收站）与 DELETE（硬删除）。路径未做任何云端变更: %s",
            action, cloud_path)
        return False

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

        # B 区僵尸清理保留 100 页安全阀。超过 10000 条时
        # 整个父目录 fail-closed 跳过，避免无界顺序请求及部分结果触发误删除。
        # A 区并发收集器按 total 获取全部页，性能模型不同，二者不强行对齐。
        while page <= 100:  # 100 页上限是有意的安全阀，勿与 A 区并发版对齐
            res = self.admin_api.list_directory(
                directory_path, page=page, per_page=per_page)
            parsed = self._parse_fs_list_content(res)
            if parsed is None:
                return None
            content, total = parsed

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
            # 无可见实例时显式清空陈旧的 current_b_path，防止 identity 指向已不存在的文件
            self.db.update_identity_b_path(fingerprint, None)
            return
        valid_instances.sort(key=lambda row: self._b_file_score_pure(
            row.local_path, row.webdav_path))
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

    def _refresh_identities_batch(self, fp_mapping_pairs: list[tuple[str, str]]) -> None:
        """批量刷新身份投影（Task 4）：单连接预读全部相关 B 行与 identity，
        内存选优后批量 upsert 投影 / identity，消除逐条 refresh 的建连风暴。

        写分支逐项等价于逐条 refresh_identity_current_b_path：
        - valid 且磁盘存在过滤；有有效实例 → upsert 投影 + 按 identity 存在性
          update_identity_b_path / upsert_identity；无有效实例 → 删该 mapping 投影
          + 置空全局 current_b_path；按 mapping 隔离。
        """
        pairs = list(dict.fromkeys(
            (fp, mid) for fp, mid in fp_mapping_pairs if fp and mid))
        if not pairs:
            return
        identities = self.db.get_identities_for_fingerprints(
            [fp for fp, _ in pairs])
        b_instances = self.db.get_all_b_rows_for_fp_pairs(pairs)

        # 分组：{fingerprint: {mapping_id: [BRecord, ...]}}
        by_fp: dict[str, dict[str, list]] = {}
        for row in b_instances:
            if not row.fingerprint or not row.mapping_id:
                continue
            by_fp.setdefault(row.fingerprint, {}).setdefault(
                row.mapping_id, []).append(row)

        # 分写分支：upsert 投影 / identity，与删除分支
        now = time.time()
        proj_upserts: list[tuple[str, str, str | None, str, float]] = []
        identity_upserts: list[tuple[str, str, str | None, str | None, float]] = []
        identity_updates: list[tuple[str | None, float, str]] = []
        proj_deletes: list[tuple[str, str]] = []

        for fp, mid in pairs:
            instances = by_fp.get(fp, {}).get(mid, [])
            valid_instances = [
                row for row in instances
                if row.status == "valid" and Path(row.local_path).exists()
            ]
            identity = identities.get(fp)
            if not valid_instances:
                proj_deletes.append((fp, mid))
                identity_updates.append((None, now, fp))
                continue
            valid_instances.sort(key=lambda row: self._b_file_score_pure(
                row.local_path, row.webdav_path))
            best = valid_instances[0]
            proj_upserts.append((fp, mid, best.local_path, "visible", now))
            if identity:
                identity_updates.append((best.local_path, now, fp))
            else:
                identity_upserts.append(
                    (fp, best.webdav_path, best.source_a_path, best.local_path, now))

        try:
            if proj_upserts:
                self.db.upsert_identity_projections_batch(proj_upserts)
            if identity_upserts:
                self.db.upsert_identities_batch(identity_upserts)
            if identity_updates:
                self.db.update_identity_b_paths_batch(identity_updates)
            if proj_deletes:
                self.db.delete_identity_projections_batch(proj_deletes)
        except Exception as e:
            logging.warning(
                "[身份投影] 批量刷新失败（%d 项）: %s", len(pairs), e)

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
                # 无 Season 层时取父目录名（与云端 cloud_parts[-2] 口径一致）
                if len(local_rel.parts) >= 2:
                    physical_media_folder_name = local_rel.parts[-2]
        except Exception as e:
            logging.debug("[边界映射] %s: %s", local, e)
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
        # 先删 DB 再删文件是设计如此（避免 watchdog 级联）
        # 物理删除为尽力而为。勿当作未守卫删除标记。
        # 删除归因：先删 DB 记录，再删物理文件。
        # 反序原顺序以消除竞态窗口：若先 safe_remove_file，其触发的 on_deleted
        # 事件会让 handle_b_deleted 在 DB 行仍存在时找到记录并误判为用户删除，
        # 连带触发不可逆的 WebDAV 源文件 + A 区源文件删除。先删 DB 行后，
        # handle_b_deleted 的 get_b_by_local_full 返回 None → 提前返回，不级联。
        mapping_id = self._mapping_id_for_b(local)
        self.db.delete_b_by_local(str(local))
        # 已知取舍: 物理删除为尽力而为。若 safe_remove_file 失败（杀毒锁/权限），
        # 磁盘残留 .strm 且无 DB 行 → 重扫前为"未跟踪文件"分叉。窄边沿，接受。
        if local.exists():
            safe_remove_file(local)
        # 无论物理文件是否仍在磁盘，只要指纹与映射存在即刷新身份投影，
        # 避免文件已被外部删除时 b_identity_projection 残留过期 B 路径。
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
                else:
                    # mkdir 成功后失效父目录缓存
                    parent_dir = "/".join(sub_path.rstrip("/").split("/")[:-1]) or "/"
                    self.admin_api.invalidate_check_exists_cache(parent_dir)

            return True
        except Exception as e:
            logging.error("[回收站] 递归创建目录异常: %s", e)
            return False
