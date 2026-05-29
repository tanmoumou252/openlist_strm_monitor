from __future__ import annotations
import os
import sys
from pathlib import Path

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def ensure_base_dir_first():
    normalized_base_dir = os.path.normcase(os.path.abspath(BASE_DIR))
    sys.path[:] = [p for p in sys.path if os.path.normcase(os.path.abspath(p or os.getcwd())) != normalized_base_dir]
    sys.path.insert(0, BASE_DIR)


def load_local_module(module_name: str, filename: str, base_dir: str | None = None):
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


ensure_base_dir_first()

try:
    import tomllib
except ImportError:
    import tomli as tomllib

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional
import threading

# ==================== PathAnalysis 定义 ====================


@dataclass
class PathAnalysis:
    valid_refresh_paths: list[str]
    only_refresh: set[str]
    only_engine: set[str]
    engine_set: set[str]


# ==================== 内联 STRM 存储管理类 ====================


@dataclass(slots=True, frozen=True)
class _StrmStorageInfo:
    """STRM 存储信息（内部使用，避免循环导入)"""

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
        return self.save_local_mode.lower() == "sync"


class _StrmStorageManager:
    """STRM 存储管理器（内部使用，避免循环导入)"""

    def __init__(self, client) -> None:
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

    def get_strm_storages(self) -> list[_StrmStorageInfo]:
        """获取所有 STRM 存储"""
        storages = self.client.list_storages()
        if not storages:
            return []

        data = storages.get("data", {})
        content = data.get("content", []) if isinstance(data, dict) else []

        result: list[_StrmStorageInfo] = []
        for storage in content:
            if storage.get("driver", "").lower() != "strm":
                continue

            addition = storage.get("addition", "")
            result.append(
                _StrmStorageInfo(
                    id=storage.get("id", 0),
                    mount_path=storage.get("mount_path", ""),
                    status=storage.get("status", "unknown"),
                    paths=self._extract_paths_from_addition(addition),
                    save_local_mode=self._extract_save_local_mode(addition),
                )
            )

        return result

    def get_working_sync_storages(self) -> list[_StrmStorageInfo]:
        """获取有效的同步模式存储"""
        return [s for s in self.get_strm_storages() if s.is_working and s.is_sync_mode]


# =========================================================


class RefreshService:
    def __init__(self, app) -> None:
        self.app = app
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.app.config.refresh.enabled:
            logging.info("[主动刷新] 已关闭")
            return

        if not self.app.config.refresh_paths:
            logging.info("[主动刷新] 未配置 refresh_paths.txt 或内容为空，已关闭")
            return

        self._running = True
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _worker(self) -> None:
        self.execute_refresh_cycle()

        interval = self.app.config.refresh.interval_seconds
        while self._running:
            time.sleep(interval)
            if not self._running:
                break
            self.execute_refresh_cycle()

    def execute_refresh_cycle(self) -> None:
        """执行完整的主动刷新周期。"""
        logging.info("[主动刷新] 开始执行")

        self._sync_and_scan_protected_roots()

        path_analysis = self._analyze_paths()
        self._log_path_analysis(path_analysis)

        accessible_engines = self._check_engine_accessibility(path_analysis.engine_set)

        safe_refresh_paths = self._calculate_safe_refresh_paths(path_analysis, accessible_engines)

        # 执行 WebDAV 刷新
        self._execute_webdav_refreshes(safe_refresh_paths, path_analysis.only_refresh)

        # 等待同步落地
        self._wait_for_sync()

        # 扫描和同步
        self._scan_and_sync(accessible_engines)

        # 保存快照
        self._persist_snapshot(accessible_engines, path_analysis.engine_set)

        logging.info("[主动刷新] 完成")

    def _sync_and_scan_protected_roots(self) -> None:
        """同步保护根目录并扫描已移除的根目录。"""
        self.app.sync_protected_roots_from_config()
        self.app.scan_removed_protected_roots()

    def _analyze_paths(self) -> PathAnalysis:
        """分析 refresh_paths 和 strm_engine_paths 的关系。"""
        refresh_set = set(self.app.config.refresh_paths)
        engine_set = set(self.app.config.strm_engine_paths)

        if not engine_set:
            return PathAnalysis(
                valid_refresh_paths=list(refresh_set),
                only_refresh=set(),
                only_engine=set(),
                engine_set=engine_set,
            )

        return PathAnalysis(
            valid_refresh_paths=sorted(refresh_set & engine_set),
            only_refresh=refresh_set - engine_set,
            only_engine=engine_set - refresh_set,
            engine_set=engine_set,
        )

    def _log_path_analysis(self, analysis: PathAnalysis) -> None:
        """记录路径分析结果日志。"""
        if analysis.only_refresh:
            logging.warning(
                "[主动刷新保护] 以下路径仅程序监控，不在 STRM 引擎监控列表，" "将只执行 WebDAV 刷新，不清理 B 区: %s",
                analysis.only_refresh,
            )

        if analysis.only_engine:
            logging.info(
                "[主动刷新提示] 以下 STRM 引擎监控路径未被 refresh_paths 覆盖，"
                "建议添加到 refresh_paths.txt 以启用主动刷新: %s",
                analysis.only_engine,
            )

    def _check_engine_accessibility(self, engine_set: set[str]) -> set[str]:
        """检查引擎路径的可访问性，返回可访问的引擎路径集合。"""
        if not engine_set:
            return set()

        # 通过 Admin API 验证
        api_accessible = self._validate_strm_storages_via_api(engine_set)
        if api_accessible is not None:
            return api_accessible

        # API 验证失败，返回空集合
        logging.warning("[STRM引擎路径检查] Admin API 验证失败，无法确定可访问路径")
        return set()

    def _validate_strm_storages_via_api(self, engine_set: set[str]) -> set[str] | None:
        """
        通过 Admin API 验证 STRM 存储状态。

        返回可访问的引擎路径集合，如果验证失败返回 None。
        """
        try:
            # 从已加载的模块中获取 OpenListAdminClient
            webdav_module = sys.modules.get("webdav_client")
            if webdav_module is None:
                logging.warning("[STRM存储API验证] webdav_client 模块未加载，回退到 WebDAV 检查")
                return None

            OpenListAdminClient = webdav_module.OpenListAdminClient

            admin_client = OpenListAdminClient(
                host=self.app.config.webdav.host,
                user=self.app.config.webdav.user,
                password=self.app.config.webdav.password,
                totp_secret=self.app.config.webdav.totp_secret,
            )

            if not admin_client.login():
                logging.warning("[STRM存储API验证] Admin API 登录失败，回退到 WebDAV 检查")
                return None

            # 使用内联的 _StrmStorageManager
            manager = _StrmStorageManager(admin_client)
            all_storages = manager.get_strm_storages()

            # 只选择状态为 work 且是 sync 模式的存储
            valid_storages = [s for s in all_storages if s.is_working and s.is_sync_mode]
            valid_paths = {s.mount_path for s in valid_storages}

            # 检查请求的 engine_set 是否在有效路径中
            result = set()
            for engine_path in engine_set:
                if engine_path in valid_paths:
                    result.add(engine_path)
                else:
                    # 检查是否是子路径
                    for valid_path in valid_paths:
                        if engine_path == valid_path or engine_path.startswith(valid_path + "/"):
                            result.add(engine_path)
                            break

            # 记录状态异常的存储
            for storage in all_storages:
                if storage.mount_path in engine_set or any(
                    storage.mount_path == ep or ep.startswith(storage.mount_path + "/") for ep in engine_set
                ):
                    if not storage.is_working:
                        logging.warning(
                            "[STRM存储API验证] 存储状态异常: %s (status=%s)",
                            storage.mount_path,
                            storage.status,
                        )
                    elif not storage.is_sync_mode:
                        logging.warning(
                            "[STRM存储API验证] 存储非同步模式: %s (mode=%s)",
                            storage.mount_path,
                            storage.save_local_mode,
                        )

            return result

        except Exception as exc:
            logging.warning("[STRM存储API验证] 验证异常，回退到 WebDAV 检查: %s", exc)
            return None

    def _calculate_safe_refresh_paths(
        self,
        analysis: PathAnalysis,
        accessible_engines: set[str],
    ) -> list[str]:
        """计算可安全执行完整刷新的路径。"""
        if not analysis.engine_set:
            return analysis.valid_refresh_paths
        return [p for p in analysis.valid_refresh_paths if p in accessible_engines]

    def _execute_webdav_refreshes(
        self,
        safe_refresh_paths: list[str],
        only_refresh: set[str],
    ) -> None:
        for root_path in safe_refresh_paths:
            # root_path 已经是引擎入口路径（如 /测试a），直接使用
            self.app.refresh_webdav_root(root_path, self.app.config.refresh.depth)

        for root_path in sorted(only_refresh):
            logging.info("[WebDAV刷新] 仅刷新目录结构，不清理B区: %s", root_path)
            self.app.refresh_webdav_root_readonly(root_path, self.app.config.refresh.depth)

    def _wait_for_sync(self) -> None:
        """等待 OpenList / 外部同步落地。"""
        logging.info("[主动刷新] 等待 openlist / 外部同步落地...")
        time.sleep(self.app.config.behavior.a_to_b_restore_delay_seconds)

    def _scan_and_sync(self, accessible_engines: set[str]) -> None:
        """执行 A 区扫描和 A→B 同步。"""
        self.app.initial_scan_a()

        # 同步是本地文件复制，不涉及 WebDAV 网络请求
        # 引擎路径可访问性只影响清理/迁移等涉及 WebDAV 的操作
        self.app.scan_a_to_b_full_sync(valid_engine_paths=None)

        self.app.cleanup_local_empty_dirs()

    def _persist_snapshot(self, accessible_engines: set[str], engine_set: set[str]) -> None:
        """保存保护根目录快照。"""
        snapshot_paths = sorted(accessible_engines) if engine_set else None
        self.app.persist_current_roots_snapshot(valid_engine_paths=snapshot_paths)
