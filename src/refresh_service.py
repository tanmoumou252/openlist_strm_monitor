from __future__ import annotations

import threading
from dataclasses import dataclass
import time
import logging
import json
import os
import sqlite3
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

# 规范化 A 根路径（a_strm_files.local_path 一律经 resolve() 写入，
# 传原始配置串会导致 touch_verified_by_mapping 匹配 0 行、last_verified_at 永不推进）
from config import normalize_local_root
# 全量审计不完整信号：失效失败（父类，四项推进全跳过）与覆盖缺口
# （子类，只跳过索引代次与核对盖章、不阻断审计节拍）
from domain.sync.sync_service import (
    AuditCoverageIncompleteError,
    AuditIncompleteError,
)


# ==================== PathAnalysis 定义 ====================


@dataclass
class PathAnalysis:
    valid_refresh_paths: list[str]
    only_refresh: set[str]
    only_engine: set[str]
    engine_set: set[str]


class PartialRefreshError(RuntimeError):
    """刷新周期内部分 root 失败的聚合异常。

    安全论证（缺陷 A，per-root 隔离与整周期中断对 root 级状态等价）：
    - 隔离语义：单 root 刷新失败不再中断后续 root 与 `_wait_for_sync`/
      `_scan_and_sync`/`_persist_snapshot`——这些段对"已成功的 root"的
      状态推进与异常路径一致（root 级幂等）。
    - 无 fail-open：`migrate_b_under_root_to_c` 仅在
      `_refresh_webdav_recursive` 返回 False 时触发，异常路径不经过它，
      不会因隔离而把失败误判为"云端目录为空"。
    - A3 因果链：启动门禁不经 `execute_refresh_cycle`（启动链路独立），
      刷新周期经此函数——本异常确保 run3 真机计时不被 readonly 单点
      中断污染（失败可观测、可归因，且不再吞掉整轮后续段）。
    """


# =========================================================


class RefreshService:
    # 连续失败熔断：前 N 次打全栈，之后降级为单行 WARNING
    _CIRCUIT_BREAKER_THRESHOLD: int = 3

    def __init__(self, app: AppService) -> None:
        self.app = app
        self._running = False
        self._thread: threading.Thread | None = None
        self._config_changed = threading.Event()
        self._lifecycle_lock = threading.RLock()
        self._consecutive_failures: int = 0
        self._last_error_summary: str = ""
        self._last_full_audit_at = self._load_last_full_audit_at()
        # 全量审计互斥锁（手动 vs 周期不能并发）
        self._full_audit_lock = threading.Lock()
        self._full_audit_in_progress = False

    def _load_last_full_audit_at(self) -> float:
        try:
            value = self.app.db.get_control("last_full_audit_at", "0")
            return float(value or 0)
        except (AttributeError, TypeError, ValueError, OSError):
            return 0.0

    # 公开只读属性：供 WebUI 状态面板读取，避免跨模块访问私有属性
    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def last_error_summary(self) -> str:
        return self._last_error_summary

    @property
    def healthy(self) -> bool:
        return self._consecutive_failures == 0

    def _full_audit_interval_seconds(self) -> float:
        try:
            days = float(getattr(self.app.config.refresh, "full_audit_interval_days", 7))
        except (TypeError, ValueError):
            return 7 * 86400
        return max(0.0, days) * 86400

    def _maybe_run_full_audit(self) -> bool:
        """按周期执行一次全 A 区审计，回收长期未触发的失活记录。"""
        interval = self._full_audit_interval_seconds()
        if interval <= 0:
            return False
        now = time.time()
        with self._full_audit_lock:
            if now - self._last_full_audit_at < interval:
                return False
            if self._full_audit_in_progress:
                return False
            self._full_audit_in_progress = True
        try:
            logging.warning("[主动刷新] 触发兜底全量审计，可能访问所有 A 区磁盘")
            # use_snapshot=False：全量审计为快照权威自愈触发源，强制全读并重建/剪枝
            audit_incomplete: AuditIncompleteError | None = None
            audit_coverage_incomplete = False
            try:
                self.app.initial_scan_a(use_bulk=False, a_roots=None, use_snapshot=False)
            except AuditCoverageIncompleteError as exc:
                # 覆盖不完整（存在未巡查 A 根）：B 区收敛照常，但下方索引代次与
                # 核对盖章跳过；审计节拍照常推进（否则故障期每周期重跑全量审计）。
                audit_coverage_incomplete = True
                logging.error(
                    "[主动刷新] 全量审计覆盖不完整，跳过索引代次与核对盖章: %s", exc)
            except AuditIncompleteError as exc:
                # 审计终态不完整（不可解析文件旧快照行失效失败）：A 索引已写入、
                # 过期链接未作废，审计权威性未建立。B 区收敛照常执行（a_strm_files
                # 不受影响），但下方索引代次与核对时间戳推进一律跳过——保留时间戳
                # 使本轮不计成功、下轮周期重新放行审计。
                audit_incomplete = exc
                logging.error("[主动刷新] 全量审计不完整，跳过核对时间戳推进: %s", exc)
            self.app.scan_a_to_b_full_sync(valid_engine_paths=None, use_bulk=False)
            if audit_incomplete is not None:
                # False 交由 execute_refresh_cycle 的非正常跳过分支更新健康状态
                return False
            # _last_full_audit_at 必须在所有 DB 写入成功后才更新，
            # 防止 DB 写失败时时间戳已推进导致后续周期静默跳过审计
            db_write_ok = True
            # 覆盖不完整（存在未巡查 A 根）：不得推进索引代次，也不得给未巡查
            # 的行盖"已核对"时间戳——两条推进均由 mapping_ids 驱动，置空即跳过；
            # 审计节拍（下方 set_control）仍推进，避免故障期每周期重跑全量审计。
            mapping_ids = ([] if audit_coverage_incomplete
                           else self.app._current_mapping_ids())
            if mapping_ids:
                try:
                    self.app.db.complete_index_generation(mapping_ids)
                except Exception:
                    logging.warning("[主动刷新] 推进索引代次失败", exc_info=True)
                    db_write_ok = False
                # 推进 last_verified_at（全量审计后标记核对时间）
                try:
                    for m in getattr(self.app, 'a_b_mappings', []):
                        mid = str(getattr(m, 'mapping_id', '')).strip()
                        a_root = getattr(m, 'a_root', '')
                        if mid and a_root:
                            # 传规范化后的 A 根（与 a_strm_files.local_path 写入口径一致）
                            try:
                                norm_a_root = str(normalize_local_root(a_root))
                            except Exception:
                                logging.warning("[主动刷新] 规范化 A 根失败，跳过该 mapping: %s", a_root)
                                continue
                            self.app.db.touch_verified_by_mapping(mid, norm_a_root, now)
                except Exception:
                    logging.warning("[主动刷新] 更新 last_verified_at 失败", exc_info=True)
                    db_write_ok = False
            try:
                self.app.db.set_control("last_full_audit_at", str(now))
            # SQLite 瞬时错误（Windows AV 锁/磁盘瞬时只读）单独覆盖
            except (AttributeError, OSError, sqlite3.OperationalError):
                logging.warning("[主动刷新] 保存全量审计时间失败")
                db_write_ok = False
            # DB 全部写入成功后，才推进内存时间戳
            if db_write_ok:
                with self._full_audit_lock:
                    self._last_full_audit_at = now
            return db_write_ok
        finally:
            with self._full_audit_lock:
                self._full_audit_in_progress = False

    def run_full_audit_now(self) -> dict:
        """手动触发全量审计的薄封装。

        完整镜像 _maybe_run_full_audit 的后置状态：
        initial_scan_a → scan_a_to_b_full_sync → complete_index_generation
        → touch_verified_by_mapping → _last_full_audit_at → set_control。
        忽略 interval/时间门槛，沿用现有异常捕获。
        与 _maybe_run_full_audit 共享 _full_audit_in_progress 互斥标志。
        """
        with self._full_audit_lock:
            if self._full_audit_in_progress:
                return {"ok": False, "status": "already_running", "message": "审计已在进行中"}
            self._full_audit_in_progress = True
        try:
            now = time.time()
            logging.warning("[手动审计] 触发全量审计，可能访问所有 A 区磁盘")
            # use_snapshot=False：全量审计为快照权威自愈触发源，强制全读并重建/剪枝
            audit_incomplete: AuditIncompleteError | None = None
            audit_coverage_incomplete = False
            try:
                self.app.initial_scan_a(use_bulk=False, a_roots=None, use_snapshot=False)
            except AuditCoverageIncompleteError as exc:
                # 覆盖不完整：B 区收敛照常，跳过索引代次与核对盖章，节拍照常推进，
                # 并在返回体中带 coverage_incomplete / warning 供 UI 与运维可见。
                audit_coverage_incomplete = True
                logging.error(
                    "[手动审计] 审计覆盖不完整，跳过索引代次与核对盖章: %s", exc)
            except AuditIncompleteError as exc:
                # 审计终态不完整：B 区收敛照常执行，但索引代次与核对时间戳推进
                # 全部跳过，并向调用方返回可区分的 incomplete 终态（error 位透出根因）。
                audit_incomplete = exc
                logging.error("[手动审计] 审计不完整，跳过核对时间戳推进: %s", exc)
            self.app.scan_a_to_b_full_sync(valid_engine_paths=None, use_bulk=False)
            if audit_incomplete is not None:
                return {"ok": False, "status": "incomplete",
                        "error": str(audit_incomplete)}
            # _last_full_audit_at 必须在所有 DB 写入成功后才更新
            db_write_ok = True
            # 覆盖不完整：同上，跳过索引代次与核对盖章，节拍照常推进。
            mapping_ids = ([] if audit_coverage_incomplete
                           else self.app._current_mapping_ids())
            if mapping_ids:
                try:
                    self.app.db.complete_index_generation(mapping_ids)
                except Exception:
                    logging.warning("[手动审计] 推进索引代次失败", exc_info=True)
                    db_write_ok = False
                # 推进 last_verified_at（审计后标记核对时间）
                try:
                    for m in getattr(self.app, 'a_b_mappings', []):
                        mid = str(getattr(m, 'mapping_id', '')).strip()
                        a_root = getattr(m, 'a_root', '')
                        if mid and a_root:
                            # 传规范化后的 A 根（与 a_strm_files.local_path 写入口径一致）
                            try:
                                norm_a_root = str(normalize_local_root(a_root))
                            except Exception:
                                logging.warning("[手动审计] 规范化 A 根失败，跳过该 mapping: %s", a_root)
                                continue
                            self.app.db.touch_verified_by_mapping(mid, norm_a_root, now)
                except Exception:
                    logging.warning("[手动审计] 更新 last_verified_at 失败", exc_info=True)
                    db_write_ok = False
            try:
                self.app.db.set_control("last_full_audit_at", str(now))
            # SQLite 瞬时错误（Windows AV 锁/磁盘瞬时只读）单独覆盖
            except (AttributeError, OSError, sqlite3.OperationalError):
                logging.warning("[手动审计] 保存全量审计时间失败")
                db_write_ok = False
            # DB 全部写入成功后，才推进内存时间戳
            # 与 _maybe_run_full_audit 保持锁同步，防止并发读/写撕裂
            if db_write_ok:
                with self._full_audit_lock:
                    self._last_full_audit_at = now
            meta = self.app.db.get_index_metadata()
            return {
                "ok": db_write_ok,
                "status": "completed" if db_write_ok else "db_write_failed",
                "index_generation": meta.get("index_generation", 0) if isinstance(meta, dict) else 0,
                "index_generation_at": meta.get("index_generation_at", 0) if isinstance(meta, dict) else 0,
                # 覆盖缺口：审计已按节拍完成，但存在未巡查 A 根——索引代次与
                # 核对盖章未推进，透出可区分的 warning 供运维判断挂载健康。
                "coverage_incomplete": audit_coverage_incomplete,
                "warning": ("存在未巡查 A 根：已跳过索引代次推进与核对时间戳盖章"
                            if audit_coverage_incomplete else None),
            }
        except Exception as e:
            logging.error("[手动审计] 审计失败: %s", e, exc_info=True)
            return {"ok": False, "status": "error", "error": str(e)}
        finally:
            with self._full_audit_lock:
                self._full_audit_in_progress = False

    def _refresh_audit_enabled(self) -> bool:
        return self._full_audit_interval_seconds() > 0

    def _has_source(self) -> bool:
        analysis = self._analyze_paths()
        return bool(analysis.valid_refresh_paths or analysis.only_refresh or self._refresh_audit_enabled())

    def notify_config_changed(self) -> None:
        self._config_changed.set()

    def reconfigure(self) -> None:
        old_thread = None
        with self._lifecycle_lock:
            has_source = self._has_source()
            enabled = self.app.config.refresh.enabled
            if not has_source:
                old_thread = self._thread
                self._running = False
                self._config_changed.set()
                self._thread = None
            elif self._running:
                self.notify_config_changed()
            elif enabled:
                if self._thread and self._thread.is_alive():
                    # 旧线程仍在运行，避免双 worker 并发——不启动新线程，
                    # 仅恢复运行标志并通知配置变更，让仍在运行的旧 worker 拾取新配置。
                    logging.warning("[主动刷新] reconfigure: 旧线程仍在运行，推迟启动新线程")
                    self._running = True
                    self.notify_config_changed()
                else:
                    self._running = True
                    self._thread = threading.Thread(target=self._worker, daemon=True)
                    self._thread.start()
        if old_thread and old_thread.is_alive():
            old_thread.join(timeout=2)

    def start(self, *, defer_first_cycle: bool = False) -> None:
        """启动刷新 worker。

        defer_first_cycle（仅关键字，c.9.2 Task 11 / R-新6）：True 时首轮
        周期先等一个 interval 再执行——启动管线（AppService.start）刚完成
        initial_scan_a + A→B 全量同步 + boundary 单扫 + watcher 挂载，首轮
        立即执行会原样重复该内容（WebDAV 全量刷新 + 30s 落地等待 + 标准模式
        重扫 + 第二次 A→B 同步 ≈2.5-3min），构成 banner 后的「假性启动」
        churn。生产调用方仅 AppService.start()（传 True）；reconfigure() 不
        传 defer——WebUI 保存配置后仍立即周期。期间 notify_config_changed
        仍可立即唤醒（用户显式操作不受 defer 影响）。
        """
        with self._lifecycle_lock:
            if self._running:
                return
            # 检查旧线程是否还在运行，避免双 worker 并发
            if self._thread and self._thread.is_alive():
                logging.warning("[主动刷新] 旧线程仍在运行，推迟启动新线程")
                return
            if not self.app.config.refresh.enabled:
                logging.info("[主动刷新] 已关闭")
                return
            if not self._has_source():
                logging.info("[主动刷新] 未配置刷新路径且全量审计已关闭，已关闭")
                return
            self._running = True
            # daemon kwarg 形态被既有用例锁定（call_args[1]["daemon"]）
            self._thread = threading.Thread(
                target=self._worker,
                kwargs={"defer_first_cycle": defer_first_cycle},
                daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._running = False
            self._config_changed.set()
            old_thread = self._thread
            self._thread = None
        if old_thread and old_thread.is_alive():
            # 增加 join 超时到 5 秒，并检查是否仍然存活
            old_thread.join(timeout=5)
            if old_thread.is_alive():
                logging.warning("[主动刷新] 旧线程 join 超时，线程仍在运行（PID: %d）", old_thread.ident or 0)

    def _worker(self, defer_first_cycle: bool = False) -> None:
        # 兜底：任何未捕获异常不得杀死刷新线程，记录后继续下一轮。
        # SQLite 瞬时 readonly（Windows 杀毒锁文件）等属于可恢复错误。
        # 熔断器：前 _CIRCUIT_BREAKER_THRESHOLD 次连续失败打全栈，
        # 之后降级为单行 WARNING 避免日志洪泛。
        # 首轮同样受 refresh.enabled 约束：start()/reconfigure() 虽在启动前检查，
        # 但存在「检查后、线程启动前」被改禁用的竞态窗口，此处二次拦截避免
        # 禁用状态下仍执行一轮完整刷新。
        if not self.app.config.refresh.enabled:
            logging.info("[主动刷新] 已关闭，首轮刷新跳过")
            return
        # R-新6（c.9.2 Task 11）：defer 首轮——先 clear 再 wait。
        # clear 必需：stop()/reconfigure() 无源路径都会预置 set 事件，而
        # WebUI 重启链路（_do_restart → stop → start）复用同一 RefreshService
        # 实例——不 clear 则重启后 defer 静默失效。已知取舍（登记 docs/
        # 否决方案.md）：①启动前已置位的 notify 被清空，该次变更延后至
        # defer 期满或下次 reconfigure；②OpenList 云端离线期变更的主动刷新
        # 触发延后一个 interval——引擎更新模式自身重写 + A 区 watcher 逐文件
        # 链路 + 下轮周期三重兜底不变。
        if defer_first_cycle:
            self._config_changed.clear()
            if not self._running:
                return
            interval = self.app.config.refresh.interval_seconds
            self._config_changed.wait(timeout=max(1, interval))
            if not self._running:
                return
            # c.9.3 Task C（R1）：defer 窗口内被禁用时不 return——旧形态把
            # 既有微秒竞态窗放大为整个 interval 的「worker 永久静默」窗口
            # （线程死亡而 _running 恒 True，reconfigure 仅 notify、无线程
            # 消费，start() 被 _running 挡住）。跳过首轮周期后落入下方主循环
            # disabled 分支挂起等 notify，语义与主循环逐字节一致；enable
            # 重开经 reconfigure notify 唤醒即恢复。
            if not self.app.config.refresh.enabled:
                logging.info("[主动刷新] 已关闭，首轮刷新跳过")
            else:
                self._run_cycle_with_breaker()
        else:
            # 非 defer 路径（reconfigure/默认 start）：首轮立即执行，行为不变。
            self._run_cycle_with_breaker()

        while self._running:
            self._config_changed.clear()
            if not self._running:
                break
            if not self.app.config.refresh.enabled:
                self._config_changed.wait()
                continue
            interval = self.app.config.refresh.interval_seconds
            self._config_changed.wait(timeout=max(1, interval))
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

        full_audit_ran = self._maybe_run_full_audit()
        # 设计决策: 全量审计失败时更新健康状态。注意：execute_refresh_cycle 正常返回后，
        # _run_cycle_with_breaker 会在同一调用栈内将 _consecutive_failures 清零，
        # 故单次审计失败本身不熔断；仅在审计失败后仍有异常抛出的周期里，该计数
        # 与 _last_error_summary 才保留到熔断判定。此为有意设计（审计失败可感知，但不因单次失败误触发熔断）。
        if not full_audit_ran and self._full_audit_interval_seconds() > 0:
            # 检查是否不是"未到周期"导致的 False（非正常跳过）
            now = time.time()
            with self._full_audit_lock:
                interval = self._full_audit_interval_seconds()
                if interval > 0 and now - self._last_full_audit_at >= interval:
                    # 审计应在本次周期执行但返回了 False → 视为审计失败
                    self._consecutive_failures += 1
                    self._last_error_summary = "全量审计执行失败"
                    logging.warning("[主动刷新] 全量审计返回失败，已更新健康状态")

        if not self.app.config.refresh_paths:
            logging.info("[主动刷新] refresh_paths 为空，本轮仅保留 watchdog 和删除联动")
            return
        self._sync_and_scan_protected_roots()

        path_analysis = self._analyze_paths()
        self._log_path_analysis(path_analysis)

        accessible_engines = self._check_engine_accessibility(
            path_analysis.engine_set)

        safe_refresh_paths = self._calculate_safe_refresh_paths(
            path_analysis, accessible_engines)

        # 执行 WebDAV 刷新（per-root 隔离，失败清单末尾聚合）
        refresh_failures = self._execute_webdav_refreshes(
            safe_refresh_paths, path_analysis.only_refresh)

        # 等待同步落地
        self._wait_for_sync()

        # 扫描和同步：7 天全量审计已完成时，不重复执行本轮局部扫描。
        if not full_audit_ran:
            refresh_a_roots = self.app.get_a_roots_for_refresh_paths()
            self._scan_and_sync(accessible_engines, a_roots=refresh_a_roots)

        # 保存快照
        self._persist_snapshot(accessible_engines, path_analysis.engine_set)

        # 缺陷 A：周期末尾聚合 raise（_persist_snapshot 之后、完成日志之前），
        # 确保失败被熔断器计数且不吞掉本轮已完成的后续段。
        if refresh_failures:
            summaries = [
                f"{root}: {type(exc).__name__}: {exc}"
                for root, exc in refresh_failures[:3]
            ]
            raise PartialRefreshError(
                f"刷新周期部分失败: {len(refresh_failures)}/"
                f"{len(safe_refresh_paths) + len(path_analysis.only_refresh)} 个 root 失败; "
                f"摘要: {'; '.join(summaries)}"
            )

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

            if not admin_client.login(source="refresh"):
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
    ) -> list[tuple[str, Exception]]:
        """逐 root 执行 WebDAV 刷新，单 root 失败隔离（缺陷 A）。

        返回失败清单 [(root, exc)]，由 execute_refresh_cycle 在周期末尾
        聚合为 PartialRefreshError；单 root 失败不中断后续 root 与周期
        后续段（安全论证见 PartialRefreshError docstring）。
        """
        failures: list[tuple[str, Exception]] = []
        for root_path in safe_refresh_paths:
            # root_path 是引擎子路径（如 /测试a/电影），直接用于 WebDAV 刷新
            try:
                self.app.refresh_webdav_root(
                    root_path, self.app.config.refresh.depth)
            except Exception as exc:
                # readonly 根因证据链：全栈入库（v7：瞬态锁，周期中途出现后消失）
                logging.warning(
                    "[WebDAV刷新] root 刷新失败，隔离继续: %s (%s: %s)",
                    root_path, type(exc).__name__, exc, exc_info=True)
                failures.append((root_path, exc))

        for root_path in sorted(only_refresh):
            logging.info("[WebDAV刷新] 仅刷新目录结构，不清理B区: %s", root_path)
            try:
                self.app.refresh_webdav_root_readonly(
                    root_path, self.app.config.refresh.depth)
            except Exception as exc:
                logging.warning(
                    "[WebDAV刷新] only_refresh root 刷新失败，隔离继续: %s (%s: %s)",
                    root_path, type(exc).__name__, exc, exc_info=True)
                failures.append((root_path, exc))
        return failures

    def _wait_for_sync(self) -> None:
        """等待 OpenList / 外部同步落地。"""
        logging.info("[主动刷新] 等待 openlist / 外部同步落地...")
        time.sleep(self.app.config.behavior.a_to_b_restore_delay_seconds)

    def _scan_and_sync(
            self, accessible_engines: set[str],
            a_roots: list[Path] | None = None) -> None:
        """仅扫描 refresh_paths 命中的 A 根，并限制 A→B 同步范围。

        v6 P0 双变量拆分：闸门交集与过滤前缀分属两个命名空间——
        - `engine_mounts`/`accessible`：挂载命名空间（get_engine_paths_for_a_roots
          与 _validate_strm_storages_via_api 回显同源），仅做可访问性闸门；
        - `filter_paths`：云资源前缀命名空间（get_engine_filter_paths 展开
          entry.paths），喂给同步过滤谓词。
        严禁把展开值直接喂进 mount∩mount 交集（全失配 → refresh 变 no-op）。
        见 docs/否决方案.md「引擎范围过滤按云资源真实前缀」条目。
        """
        roots = self.app.a_roots if a_roots is None else a_roots
        if not roots:
            logging.info("[主动刷新] 无匹配 A 区根，跳过本地扫描与 A→B 同步")
            return

        self.app.initial_scan_a(use_bulk=False, a_roots=roots)
        engine_mounts = self.app.get_engine_paths_for_a_roots(roots)
        if not accessible_engines:
            logging.warning("[主动刷新] 没有可访问引擎，跳过 A→B 同步")
            return
        accessible = {m for m in engine_mounts if m in accessible_engines}
        if not accessible:
            logging.warning("[主动刷新] A 根未能映射到可访问引擎，跳过 A→B 同步")
            return
        filter_paths = self.app.get_engine_filter_paths(allowed_mounts=accessible)
        self.app.scan_a_to_b_full_sync(
            valid_engine_paths=filter_paths, use_bulk=False)
        self.app.cleanup_local_empty_dirs()

    def _persist_snapshot(
            self, accessible_engines: set[str], engine_set: set[str]) -> None:
        """保存保护根目录快照。

        fail-closed：当 engine_set 非空但 accessible_engines 为空时
        （Admin API 验证失败），保留已有快照不被空集合覆盖。
        """
        if engine_set and not accessible_engines:
            logging.warning(
                "[主动刷新] Admin API 不可信（engine_set=%d, accessible=0），"
                "保留已有根目录快照不被空集合覆盖", len(engine_set))
            return
        snapshot_paths = sorted(accessible_engines) if engine_set else None
        self.app.persist_current_roots_snapshot(
            valid_engine_paths=snapshot_paths)

    # [已废弃] 原 update 模式冗余清理，已被 cleanup_a_redundant_using_api
    # （WebUI 手动刷新 / watchdog 触发）取代；保留仅为兼容旧调用路径，
    # 勿当作活跃清理逻辑调用。见 docs/否决方案.md 登记。
    def _cleanup_a_for_update_mode(self, accessible_engines: set[str]) -> None:
        """死代码——原 update 模式冗余清理，现已被
        `cleanup_a_redundant_using_api`（WebUI 手动刷新 / watchdog 触发）取代，
        保留仅为兼容旧调用路径，勿当作活跃清理逻辑调用。"""
        for engine_path in accessible_engines:
            self.app.cleanup_a_deleted_on_cloud(engine_path)
