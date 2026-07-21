from __future__ import annotations

import threading
from dataclasses import dataclass
import time
import logging
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app_service_core import AppService

# PROJECT_ROOT = 项目根目录（配置文件目录）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Bootstrap: 使用公共模块避免重复
from utils.bootstrap import ensure_base_dir_first

ensure_base_dir_first()

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]


# ==================== PathAnalysis 定义 ====================


@dataclass
class PathAnalysis:
    valid_refresh_paths: list[str]
    only_refresh: set[str]
    only_engine: set[str]
    engine_set: set[str]


# =========================================================


class RefreshService:
    # 连续失败熔断：前 N 次打全栈，之后降级为单行 WARNING
    _CIRCUIT_BREAKER_THRESHOLD: int = 3

    def __init__(self, app: AppService) -> None:
        self.app = app
        self._running = False
        self._thread: threading.Thread | None = None
        self._consecutive_failures: int = 0
        self._last_error_summary: str = ""

    def start(self) -> None:
        if not self.app.config.refresh.enabled:
            logging.info("[主动刷新] 已关闭")
            return

        if not self.app.config.refresh_paths:
            logging.info("[主动刷新] 未配置刷新路径或内容为空，已关闭")
            return

        self._running = True
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _worker(self) -> None:
        # 兜底：任何未捕获异常不得杀死刷新线程，记录后继续下一轮。
        # SQLite 瞬时 readonly（Windows 杀毒锁文件）等属于可恢复错误。
        # 熔断器：前 _CIRCUIT_BREAKER_THRESHOLD 次连续失败打全栈，
        # 之后降级为单行 WARNING 避免日志洪泛。
        self._run_cycle_with_breaker()

        while self._running:
            # 在循环内读取间隔，使热重载后的新间隔即时生效；
            # 用 1 秒粒度睡眠兼顾快速停止（_running）与间隔变更感知。
            interval = self.app.config.refresh.interval_seconds
            waited = 0
            while self._running and waited < interval:
                time.sleep(1)
                waited += 1
            if not self._running:
                break
            self._run_cycle_with_breaker()

    def _run_cycle_with_breaker(self) -> None:
        """执行一次刷新周期，带熔断器。"""
        try:
            self.execute_refresh_cycle()
            if self._consecutive_failures > 0:
                logging.info(
                    "[主动刷新] 恢复正常（此前连续失败 %d 次）",
                    self._consecutive_failures)
            self._consecutive_failures = 0
            self._last_error_summary = ""
        except Exception as exc:
            self._consecutive_failures += 1
            self._last_error_summary = f"{type(exc).__name__}: {exc}"
            if self._consecutive_failures <= self._CIRCUIT_BREAKER_THRESHOLD:
                logging.error(
                    "[主动刷新] 执行失败 (%d/%d)",
                    self._consecutive_failures,
                    self._CIRCUIT_BREAKER_THRESHOLD,
                    exc_info=True)
            else:
                logging.warning(
                    "[主动刷新] 连续失败 %d 次，已降级为摘要: %s",
                    self._consecutive_failures, self._last_error_summary)

    def execute_refresh_cycle(self) -> None:
        """执行完整的主动刷新周期。"""
        logging.info("[主动刷新] 开始执行")

        self._sync_and_scan_protected_roots()

        path_analysis = self._analyze_paths()
        self._log_path_analysis(path_analysis)

        accessible_engines = self._check_engine_accessibility(
            path_analysis.engine_set)

        # ===== update 模式：清理 A 区过期文件 =====
        self._cleanup_a_for_update_mode(accessible_engines)
        # =========================================

        safe_refresh_paths = self._calculate_safe_refresh_paths(
            path_analysis, accessible_engines)

        # 执行 WebDAV 刷新
        self._execute_webdav_refreshes(
            safe_refresh_paths, path_analysis.only_refresh)

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
        """分析 refresh_paths 和 strm_engine_paths 的关系。

        refresh_paths 是用户配置的"引擎子路径"（如 /测试a/电影），
        strm_engine_paths 是 STRM 引擎挂载点（如 /测试a）。

        使用前缀匹配判断 refresh_path 是否属于某个引擎：
        - 匹配的 → valid_refresh_paths（可执行完整刷新 + B 区清理）
        - 不匹配的 → only_refresh（仅只读 WebDAV 刷新，不清理 B 区）
        - 引擎下没有任何 refresh_path 的 → only_engine（提示用户添加）
        """
        refresh_set = set(self.app.config.refresh_paths)
        engine_set = set(self.app.config.strm_engine_paths)

        if not engine_set:
            return PathAnalysis(
                valid_refresh_paths=list(refresh_set),
                only_refresh=set(),
                only_engine=set(),
                engine_set=engine_set,
            )

        # 前缀匹配：refresh_path 是某个 engine 的子路径时视为有效
        valid_refresh_paths = []
        only_refresh: set[str] = set()
        for rp in refresh_set:
            rp_norm = rp.rstrip("/")
            matched = any(
                rp_norm.startswith(ep.rstrip("/") + "/") or rp_norm == ep.rstrip("/")
                for ep in engine_set
            )
            if matched:
                valid_refresh_paths.append(rp)
            else:
                only_refresh.add(rp)

        # 找出没有对应 refresh_path 的引擎
        only_engine: set[str] = set()
        for ep in engine_set:
            ep_norm = ep.rstrip("/")
            has_refresh = any(
                rp.rstrip("/").startswith(ep_norm + "/") or rp.rstrip("/") == ep_norm
                for rp in refresh_set
            )
            if not has_refresh:
                only_engine.add(ep)

        return PathAnalysis(
            valid_refresh_paths=sorted(valid_refresh_paths),
            only_refresh=only_refresh,
            only_engine=only_engine,
            engine_set=engine_set,
        )

    def _log_path_analysis(self, analysis: PathAnalysis) -> None:
        """记录路径分析结果日志（问题27：增强上下文信息）。"""
        if analysis.only_refresh:
            logging.warning(
                "[主动刷新保护] 以下 refresh_paths（来源: WebUI 配置页用户手动配置）"
                "不属于任何已配置的 STRM 引擎（来源: Admin API /api/admin/storage/list "
                "返回的 STRM storage 的 mount_path），"
                "将只执行 WebDAV 只读刷新（不清理 B 区）: %s",
                analysis.only_refresh,
            )

        if analysis.only_engine:
            logging.info(
                "[主动刷新提示] 以下 STRM 引擎（来源: Admin API 返回的 mount_path）"
                "下未配置 refresh_paths，建议在 WebUI 配置页添加以启用完整刷新 + B 区清理: %s",
                analysis.only_engine,
            )

        # 记录有效匹配的详细映射关系，方便排查
        if analysis.valid_refresh_paths:
            for rp in analysis.valid_refresh_paths:
                rp_norm = rp.rstrip("/")
                matched_engines = [
                    ep for ep in analysis.engine_set
                    if rp_norm.startswith(ep.rstrip("/") + "/") or rp_norm == ep.rstrip("/")
                ]
                logging.debug(
                    "[路径分析] refresh_path '%s' 匹配到引擎: %s",
                    rp, matched_engines,
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

    def _validate_strm_storages_via_api(
            self, engine_set: set[str]) -> set[str] | None:
        """
        通过 Admin API 验证 STRM 存储状态。

        返回可访问的引擎路径集合，如果验证失败返回 None。
        """
        try:
            # 复用 app.admin_api，避免重复创建客户端和 Token 缓存不一致
            admin_client = self.app.admin_api
            if admin_client is None:
                logging.warning(
                    "[STRM存储API验证] admin_api 未初始化，回退到 WebDAV 检查")
                return None

            if not admin_client.login():
                error_msg = admin_client.last_error_message or "未知错误"
                logging.warning("[STRM存储API验证] Admin API 登录失败: %s，回退到 WebDAV 检查", error_msg)
                return None

            # 使用 app_service_core 中的 StrmStorageManager（避免重复实现）
            from app_service_core import StrmStorageManager
            manager = StrmStorageManager(admin_client)
            all_storages = manager.get_strm_storages()

            # 只选择状态为 work 且是 sync 模式的存储
            valid_storages = [
                s for s in all_storages if s.is_working and s.is_sync_mode]
            valid_paths = {s.mount_path for s in valid_storages}

            # 检查请求的 engine_set 是否在有效路径中
            result = set()
            for engine_path in engine_set:
                if engine_path in valid_paths:
                    result.add(engine_path)
                else:
                    # 检查是否是子路径
                    for valid_path in valid_paths:
                        if engine_path == valid_path or engine_path.startswith(
                                valid_path + "/"):
                            result.add(engine_path)
                            break

            # 记录状态异常的存储
            for storage in all_storages:
                if storage.mount_path in engine_set or any(
                    storage.mount_path == ep or ep.startswith(storage.mount_path + "/") for ep in engine_set
                ):
                    # 问题27：增强日志，记录每个 storage 的详细信息
                    logging.debug(
                        "[STRM存储API验证] 存储详情: mount_path=%s, "
                        "paths=%s (真实云端监控路径), "
                        "status=%s, mode=%s",
                        storage.mount_path,
                        storage.paths,
                        storage.status,
                        storage.save_local_mode,
                    )

                    if not storage.is_working:
                        logging.warning(
                            "[STRM存储API验证] 存储状态异常: %s (status=%s)",
                            storage.mount_path,
                            storage.status,
                        )
                    elif not storage.is_sync_mode:
                        logging.warning(
                            "[STRM存储API验证] 存储非更新模式: %s (mode=%s, 需要改为更新模式)",
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
        """计算可安全执行完整刷新的路径。

        valid_refresh_paths 是引擎子路径（如 /测试a/电影），
        accessible_engines 是引擎挂载点（如 /测试a）。
        使用前缀匹配：子路径所属的引擎在可访问集合中即为安全。
        """
        if not analysis.engine_set:
            return analysis.valid_refresh_paths
        result = []
        for rp in analysis.valid_refresh_paths:
            rp_norm = rp.rstrip("/")
            matched = any(
                rp_norm.startswith(ep.rstrip("/") + "/") or rp_norm == ep.rstrip("/")
                for ep in accessible_engines
            )
            if matched:
                result.append(rp)
        return result

    def _execute_webdav_refreshes(
        self,
        safe_refresh_paths: list[str],
        only_refresh: set[str],
    ) -> None:
        for root_path in safe_refresh_paths:
            # root_path 是引擎子路径（如 /测试a/电影），直接用于 WebDAV 刷新
            self.app.refresh_webdav_root(
                root_path, self.app.config.refresh.depth)

        for root_path in sorted(only_refresh):
            logging.info("[WebDAV刷新] 仅刷新目录结构，不清理B区: %s", root_path)
            self.app.refresh_webdav_root_readonly(
                root_path, self.app.config.refresh.depth)

    def _wait_for_sync(self) -> None:
        """等待 OpenList / 外部同步落地。"""
        logging.info("[主动刷新] 等待 openlist / 外部同步落地...")
        time.sleep(self.app.config.behavior.a_to_b_restore_delay_seconds)

    def _scan_and_sync(self, accessible_engines: set[str]) -> None:
        """执行 A 区扫描和 A→B 同步。"""
        self.app.initial_scan_a()

        # 同步是本地文件复制，不涉及 WebDAV 网络请求
        # 引擎路径可访问性只影响清理/迁移等涉及 WebDAV 的操作
        self.app.scan_a_to_b_full_sync(valid_engine_paths=None, use_bulk=False)

        self.app.cleanup_local_empty_dirs()

    def _persist_snapshot(
            self, accessible_engines: set[str], engine_set: set[str]) -> None:
        """保存保护根目录快照。"""
        snapshot_paths = sorted(accessible_engines) if engine_set else None
        self.app.persist_current_roots_snapshot(
            valid_engine_paths=snapshot_paths)

    def _cleanup_a_for_update_mode(self, accessible_engines: set[str]) -> None:
        """在 update 模式下，清理 A 区中云端已删除的文件"""
        for engine_path in accessible_engines:
            self.app.cleanup_a_deleted_on_cloud(engine_path)
