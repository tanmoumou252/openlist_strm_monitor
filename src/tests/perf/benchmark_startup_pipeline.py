#!/usr/bin/env python3
"""真实 AppService 启动 pipeline benchmark 与阶段性能分析。

本基准测试执行真实 AppService 启动阶段：
  1. SyncService.initial_scan_a(use_bulk=True)
  2. AppService.initial_scan_b()
  3. SyncService.scan_a_to_b_full_sync(use_bulk=True)
  4. AppService._reconcile_catch_up() —— [已废弃] v6 R2-A 单扫化后为空壳
     （catch_up_readonly 计时档保留，历史可比性以 boundary 口径延续）
  5. AppService._reconcile_boundary_catch_up()

真实边界保证：
  - 使用真实 Database 和 AppService 实例；
  - OpenListAdminClient 使用 FailFastAdmin，任何未显式允许的网络调用立即 fail-fast；
  - 所有文件读写与 DB 操作完全隔离在临时 A/B/C 根；
  - 以终态物理文件与数据库多表 SHA256 digest 作为正确性判断依据。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

# 确保 src/ 在 sys.path
_SRC_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

import app_service_core as _asc
import domain.sync.sync_service as _ssvc
from app_service_core import AppService
from config import (
    ABMapping,
    AppConfig,
    BehaviorConfig,
    LINEAGE_VERSION,
    LocalConfig,
    LogConfig,
    PathsConfig,
    RefreshConfig,
    WebDAVConfig,
    WebUIConfig,
)
from database import Database
from utils import make_strm_fingerprint, quarantine_file, webdav_parent


TIMING_WINDOWS = {
    "window_1_database_schema_creation": "Database 构造与 schema 创建阶段耗时",
    "window_2_start_main_sync_admission": "WebUIServer.start_main() 同步准入返回耗时 (Gate 1A)",
    "window_3_worker_app_service_start": "Worker 线程内 AppService.start() 核心耗时",
    "window_4_http_request_to_ready_total": "HTTP POST 请求到系统到达 READY 状态全链路端到端耗时 (Gate 1B + 同步总耗时)",
}


def measure_gate1a_admission(server: Any, iterations: int = 10) -> dict[str, float]:
    """Gate 1A: 热进程直接调用 start_main() 同步准入耗时度量（中位数与 P95，适用 200ms 口径）。"""
    samples: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        res = server.start_main()
        dur = time.perf_counter() - t0
        samples.append(dur)
        if res.get("success"):
            server.stop_main()
    sorted_samples = sorted(samples)
    p95_idx = max(0, min(len(sorted_samples) - 1, int(0.95 * len(sorted_samples))))
    return {
        "gate": "Gate 1A (Direct Method Admission)",
        "iterations": len(samples),
        "median_seconds": statistics.median(samples),
        "p95_seconds": sorted_samples[p95_idx],
        "applicable_target": "WebUIServer.start_main() sync return latency (target: <200ms)",
        "passed_200ms": statistics.median(samples) < 0.200,
    }


def measure_gate1b_http_admission(
    client_func: Callable[[], tuple[int, dict[str, Any]]], iterations: int = 10
) -> dict[str, float]:
    """Gate 1B: 真实 HTTP POST /api/main/start 端到端响应耗时度量（中位数与 P95，适用 200ms 口径）。"""
    samples: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        client_func()
        dur = time.perf_counter() - t0
        samples.append(dur)
    sorted_samples = sorted(samples)
    p95_idx = max(0, min(len(sorted_samples) - 1, int(0.95 * len(sorted_samples))))
    return {
        "gate": "Gate 1B (HTTP Admission)",
        "iterations": len(samples),
        "median_seconds": statistics.median(samples),
        "p95_seconds": sorted_samples[p95_idx],
        "applicable_target": "POST /api/main/start HTTP roundtrip latency (target: <200ms)",
        "passed_200ms": statistics.median(samples) < 0.200,
    }


class UnexpectedNetworkCall(RuntimeError):
    """当 benchmark 触发未经允许的网络请求时抛出。"""


class FailFastAdmin:
    """Fail-fast fake OpenList Admin 客户端。

    任何网络相关方法被调用时立即抛出 UnexpectedNetworkCall 错误，
    确保启动 pipeline 在本地模式下不会伪造成功或向外部发起请求。
    提供三计数器契约：fake_contract_calls, unexpected_external_calls, real_http_calls。
    """

    def __init__(self) -> None:
        self.fake_contract_calls = 0
        self.unexpected_external_calls = 0
        self.real_http_calls = 0
        self.network_calls = 0
        self.calls: list[str] = []

    def _trap(self, method_name: str, *args: Any, **kwargs: Any) -> None:
        self.unexpected_external_calls += 1
        self.network_calls += 1
        self.calls.append(method_name)
        raise UnexpectedNetworkCall(
            f"FailFastAdmin: unexpected network call to '{method_name}' with args={args} kwargs={kwargs}"
        )

    def check_exists(self, path: str) -> bool:
        self._trap("check_exists", path)
        return False

    def list_directory(self, path: str, page: int = 1, per_page: int = 100) -> dict[str, Any]:
        self._trap("list_directory", path, page=page, per_page=per_page)
        return {}

    def move_file(self, src: str, dst: str) -> bool:
        self._trap("move_file", src, dst)
        return False

    def remove_file(self, path: str) -> bool:
        self._trap("remove_file", path)
        return False

    def mkdir(self, path: str) -> bool:
        self._trap("mkdir", path)
        return False

    def rename_file(self, src: str, dst: str) -> bool:
        self._trap("rename_file", src, dst)
        return False


class CountingDatabase(Database):
    """真实 Database 的计数+计时包装器：统计 B 区删除/移动等 DB 操作次数与墙钟。

    T0 基准插桩（benchmark-only）：
    - 4 个去重路径 DB 方法 + 批写/预筛方法的累计墙钟与调用次数；
    - move_b_record 逐调用时长分布（146ms 重析：区分均匀成本 vs 首调用离群值）；
    - connection / read_connection / bulk_connection 开启次数（建连风暴计量）。
    """

    def __init__(self, db_path: str) -> None:
        # 计数器先于 super().__init__ 初始化——schema 创建即经过本包装层
        self.method_stats: dict[str, dict[str, float]] = {}
        self.move_b_record_durations: list[float] = []
        self.connection_opens = 0
        self.read_connection_opens = 0
        self.bulk_connection_opens = 0
        super().__init__(db_path)
        self.delete_b_by_local_calls = 0
        self.move_b_record_calls = 0

    def _record(self, name: str, seconds: float) -> None:
        stat = self.method_stats.setdefault(name, {"seconds": 0.0, "calls": 0})
        stat["seconds"] += seconds
        stat["calls"] += 1

    def delete_b_by_local(self, local_path: str) -> None:
        self.delete_b_by_local_calls += 1
        return super().delete_b_by_local(local_path)

    # ---- T0 计时包装：去重循环 4 方法 + 批写 + 预筛 ----
    def get_all_b_by_fingerprint(self, fingerprint: str, mapping_id: str):
        t0 = time.perf_counter()
        try:
            return super().get_all_b_by_fingerprint(fingerprint, mapping_id)
        finally:
            self._record("get_all_b_by_fingerprint", time.perf_counter() - t0)

    def mark_other_b_instances_duplicate(self, fingerprint, keep_local_path, mapping_id):
        t0 = time.perf_counter()
        try:
            return super().mark_other_b_instances_duplicate(
                fingerprint, keep_local_path, mapping_id)
        finally:
            self._record("mark_other_b_instances_duplicate", time.perf_counter() - t0)

    def mark_b_instance_status(self, local_path: str, status: str) -> None:
        t0 = time.perf_counter()
        try:
            return super().mark_b_instance_status(local_path, status)
        finally:
            self._record("mark_b_instance_status", time.perf_counter() - t0)

    def move_b_record(self, old_local_path: str, new_local_path: str) -> bool:
        t0 = time.perf_counter()
        try:
            return super().move_b_record(old_local_path, new_local_path)
        finally:
            dur = time.perf_counter() - t0
            self._record("move_b_record", dur)
            self.move_b_record_calls += 1
            self.move_b_record_durations.append(dur)

    def upsert_b_records_and_snapshots_batch(self, b_records, snapshot_rows) -> int:
        t0 = time.perf_counter()
        try:
            return super().upsert_b_records_and_snapshots_batch(b_records, snapshot_rows)
        finally:
            self._record("upsert_b_records_and_snapshots_batch", time.perf_counter() - t0)

    def get_duplicate_fingerprint_groups(self):
        t0 = time.perf_counter()
        try:
            return super().get_duplicate_fingerprint_groups()
        finally:
            self._record("get_duplicate_fingerprint_groups", time.perf_counter() - t0)

    def get_all_b_rows_for_fp_pairs(self, fp_pairs):
        t0 = time.perf_counter()
        try:
            return super().get_all_b_rows_for_fp_pairs(fp_pairs)
        finally:
            self._record("get_all_b_rows_for_fp_pairs", time.perf_counter() - t0)

    # ---- 连接开启计数（建连风暴计量） ----
    @contextmanager
    def connection(self):
        self.connection_opens += 1
        with super().connection() as conn:
            yield conn

    @contextmanager
    def read_connection(self):
        self.read_connection_opens += 1
        with super().read_connection() as conn:
            yield conn

    @contextmanager
    def bulk_connection(self):
        self.bulk_connection_opens += 1
        with super().bulk_connection() as conn:
            yield conn


class _OperationInstrument:
    """包装 app_service_core 与 sync_service 的物理操作以进行真实测量。

    T0 扩展：quarantine_file / safe_remove_file 除计数外累计墙钟
    （rename_only_ms / 删除耗时归因）。
    """

    def __init__(self) -> None:
        self.quarantine_success = 0
        self.safe_remove_success = 0
        self.quarantine_seconds = 0.0
        self.safe_remove_seconds = 0.0
        self._orig_qf = _asc.quarantine_file
        self._orig_srf_asc = _asc.safe_remove_file
        self._orig_srf_ssvc = _ssvc.safe_remove_file

    def __enter__(self) -> _OperationInstrument:
        def _counting_qf(path: Any, suffix: str = ".invalid") -> Any:
            t0 = time.perf_counter()
            try:
                res = self._orig_qf(path, suffix=suffix)
                if res is not None:
                    self.quarantine_success += 1
                return res
            finally:
                self.quarantine_seconds += time.perf_counter() - t0

        def _counting_srf(path: Any) -> bool:
            t0 = time.perf_counter()
            try:
                res = self._orig_srf_asc(path)
                if res:
                    self.safe_remove_success += 1
                return res
            finally:
                self.safe_remove_seconds += time.perf_counter() - t0

        _asc.quarantine_file = _counting_qf
        _asc.safe_remove_file = _counting_srf
        _ssvc.safe_remove_file = _counting_srf
        return self

    def __exit__(self, *exc: Any) -> None:
        _asc.quarantine_file = self._orig_qf
        _asc.safe_remove_file = self._orig_srf_asc
        _ssvc.safe_remove_file = self._orig_srf_ssvc


def _dist_stats_ms(seconds: list[float]) -> dict[str, Any]:
    """把秒列表转成毫秒分布统计（中位数/P95/min/max/计数）。"""
    if not seconds:
        return {"count": 0}
    ms = sorted(s * 1000.0 for s in seconds)
    p95_idx = max(0, min(len(ms) - 1, int(0.95 * len(ms))))
    return {
        "count": len(ms),
        "median_ms": round(statistics.median(ms), 3),
        "p95_ms": round(ms[p95_idx], 3),
        "min_ms": round(ms[0], 3),
        "max_ms": round(ms[-1], 3),
        "mean_ms": round(statistics.fmean(ms), 3),
    }


class _PhaseProfiler:
    """阶段计时插桩（benchmark-only，不改生产代码）。

    包装 app 实例的 _scan_b_disk / _reconcile_b_historical_records /
    _insert_new_b_records / _verify_b_path_lineage，记录各阶段墙钟，
    并在阶段边界快照 CountingDatabase 方法计时与物理操作计时做差分，
    产出 initial_scan_b 四段归因（lineage / 批写 / 去重循环 / 残差 stat+开销）。
    """

    _DEDUP_METHODS = (
        "get_all_b_by_fingerprint",
        "mark_other_b_instances_duplicate",
        "mark_b_instance_status",
        "move_b_record",
        "get_duplicate_fingerprint_groups",
    )

    def __init__(self, app: AppService, db: CountingDatabase, ops: _OperationInstrument):
        self.app = app
        self.db = db
        self.ops = ops
        self.phases: dict[str, dict[str, Any]] = {}
        self.lineage_seconds = 0.0
        self.lineage_calls = 0
        self._orig = {
            name: getattr(app, name)
            for name in (
                "_verify_b_path_lineage",
                "_scan_b_disk",
                "_reconcile_b_historical_records",
                "_insert_new_b_records",
            )
        }
        self._had_instance_attr = {
            name: name in app.__dict__ for name in self._orig
        }

    def _snapshot(self) -> dict[str, Any]:
        return {
            "method_stats": {
                k: dict(v) for k, v in self.db.method_stats.items()},
            "connection_opens": self.db.connection_opens,
            "read_connection_opens": self.db.read_connection_opens,
            "bulk_connection_opens": self.db.bulk_connection_opens,
            "quarantine_seconds": self.ops.quarantine_seconds,
            "quarantine_success": self.ops.quarantine_success,
            "lineage_seconds": self.lineage_seconds,
            "lineage_calls": self.lineage_calls,
        }

    def _run_phase(self, name: str, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        before = self._snapshot()
        t0 = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            wall = time.perf_counter() - t0
            after = self._snapshot()
            delta_methods = {
                k: {
                    "seconds": after["method_stats"].get(k, {"seconds": 0.0})["seconds"]
                    - before["method_stats"].get(k, {"seconds": 0.0})["seconds"],
                    "calls": after["method_stats"].get(k, {"calls": 0})["calls"]
                    - before["method_stats"].get(k, {"calls": 0})["calls"],
                }
                for k in set(after["method_stats"]) | set(before["method_stats"])
            }
            self.phases[name] = {
                "wall_seconds": wall,
                "method_delta": delta_methods,
                "connection_opens_delta":
                    after["connection_opens"] - before["connection_opens"],
                "read_connection_opens_delta":
                    after["read_connection_opens"] - before["read_connection_opens"],
                "bulk_connection_opens_delta":
                    after["bulk_connection_opens"] - before["bulk_connection_opens"],
                "quarantine_seconds_delta":
                    after["quarantine_seconds"] - before["quarantine_seconds"],
                "quarantine_success_delta":
                    after["quarantine_success"] - before["quarantine_success"],
                "lineage_seconds_delta":
                    after["lineage_seconds"] - before["lineage_seconds"],
                "lineage_calls_delta":
                    after["lineage_calls"] - before["lineage_calls"],
            }

    def __enter__(self) -> _PhaseProfiler:
        profiler = self
        orig_lineage = self._orig["_verify_b_path_lineage"]

        def _timed_lineage(b_local_path, webdav_path, is_sync_phase=False):
            t0 = time.perf_counter()
            try:
                return orig_lineage(
                    b_local_path, webdav_path, is_sync_phase=is_sync_phase)
            finally:
                profiler.lineage_seconds += time.perf_counter() - t0
                profiler.lineage_calls += 1

        setattr(self.app, "_verify_b_path_lineage", _timed_lineage)
        setattr(self.app, "_scan_b_disk",
                lambda *a, **kw: profiler._run_phase(
                    "scan_b_disk", profiler._orig["_scan_b_disk"], *a, **kw))
        setattr(self.app, "_reconcile_b_historical_records",
                lambda *a, **kw: profiler._run_phase(
                    "reconcile_b_historical", profiler._orig["_reconcile_b_historical_records"], *a, **kw))
        setattr(self.app, "_insert_new_b_records",
                lambda *a, **kw: profiler._run_phase(
                    "insert_new_b_records", profiler._orig["_insert_new_b_records"], *a, **kw))
        return self

    def __exit__(self, *exc: Any) -> None:
        for name, orig in self._orig.items():
            if self._had_instance_attr[name]:
                setattr(self.app, name, orig)
            elif name in self.app.__dict__:
                del self.app.__dict__[name]

    def insert_new_attribution(self) -> dict[str, Any]:
        """_insert_new_b_records 四段归因（C3：实证 lineage/stat/批写/去重拆分）。"""
        phase = self.phases.get("insert_new_b_records")
        if not phase:
            return {}
        methods = phase["method_delta"]
        dedup_db = sum(
            methods.get(m, {"seconds": 0.0})["seconds"] for m in self._DEDUP_METHODS)
        rename = phase["quarantine_seconds_delta"]
        batch_write = methods.get(
            "upsert_b_records_and_snapshots_batch", {"seconds": 0.0})["seconds"]
        lineage = phase["lineage_seconds_delta"]
        total = phase["wall_seconds"]
        dedup_loop = dedup_db + rename
        return {
            "insert_new_total_seconds": round(total, 3),
            "lineage_est_seconds": round(lineage, 3),
            "batch_write_est_seconds": round(batch_write, 3),
            "dedup_loop_est_seconds": round(dedup_loop, 3),
            "dedup_db_only_seconds": round(dedup_db, 3),
            "dedup_rename_only_seconds": round(rename, 3),
            "insert_new_minus_dedup_seconds": round(total - dedup_loop, 3),
            "residual_stat_overhead_seconds": round(
                max(0.0, total - lineage - batch_write - dedup_loop), 3),
            "dedup_move_calls": methods.get(
                "move_b_record", {"calls": 0})["calls"],
            "lineage_calls": phase["lineage_calls_delta"],
            "method_delta": {
                k: {"seconds": round(v["seconds"], 3), "calls": v["calls"]}
                for k, v in sorted(methods.items()) if v["calls"] or v["seconds"] > 0
            },
        }


@dataclass
class PipelineFixture:
    base_dir: Path
    a_roots: list[Path]
    b_roots: list[Path]
    c_root: Path
    db_path: Path
    mappings: list[ABMapping]
    expected_records: int
    categories: dict[str, int] = field(default_factory=dict)


def build_fixture(
        base_dir: Path, records: int, mappings: int = 2,
        dup_rate: float = 0.25) -> PipelineFixture:
    """构造包含多场景测试数据的临时环境。

    覆盖场景（dup_rate=0.25 时按 i%4 确定性分配，历史 607.21s 可比性不断裂）：
      - existing_b (i%4 ∈ {0, 2}): A 与 B 同时存在，且内容一致（由 initial_scan_b 录入，无需 A->B 复制）
      - new_in_a (i%4 == 1): 仅存在于 A，待 scan_a_to_b_full_sync 复制到 B
      - dup_b (i%4 == 3): 同一指纹在 B 区存在两个可见实例（不同路径写入相同 WebDAV 内容），
        用于度量 dedup 预筛选与隔离改名

    dup_rate 语义（T0 --dup-rate）：
      - 0.25（默认）：保持现有 i%4 位精确分配——25% dup / 25% new_in_a / 50% existing_b。
      - 其他取值（0 / 0.01 等）：确定性散列分流（blake2b，种子固定），dup 占比 = dup_rate，
        剩余按 2:1 分配 existing_b / new_in_a（与 25% 档比例一致）。
        categories / expected_copy / move==dup_b 断言按 fixture.categories 自动适配。

    记录序号单射设计：
      show 由 `i // 1200 + 1` 确定（每 show 容量 50 季 × 24 集 = 1200 条）；
      season 由 `(i // 24) % 50 + 1` 确定；
      episode 由 `i % 24 + 1` 确定。
      对于单 mapping 内 i < 50000 严格单射无碰撞，彻底消除 LCM(200,50,24)=600 取模虚标。
    """
    if mappings not in (1, 2):
        raise ValueError(f"mappings must be 1 or 2, got {mappings}")
    if records < 1:
        raise ValueError(f"records must be >= 1, got {records}")
    if not (0.0 <= dup_rate <= 1.0):
        raise ValueError(f"dup_rate must be within [0, 1], got {dup_rate}")
    # 浮点 0.25 精确匹配（CLI 默认值保持 i%4 位分配可比性）
    use_exact_quarter = abs(dup_rate - 0.25) < 1e-9

    base = Path(base_dir).resolve()
    a_roots = [base / f"A{i+1}" for i in range(mappings)]
    b_roots = [base / f"B{i+1}" for i in range(mappings)]
    c_root = base / "C"
    db_path = base / "bridge.db"

    for r in (*a_roots, *b_roots, c_root):
        r.mkdir(parents=True, exist_ok=True)

    ab_mappings = [
        ABMapping(
            mapping_id=f"map{i+1}",
            a_root=str(a_roots[i]),
            b_root=str(b_roots[i]),
            label=f"Mapping {i+1}",
        )
        for i in range(mappings)
    ]

    categories = {
        "existing_b": 0,
        "new_in_a": 0,
        "dup_b": 0,
    }

    def _hash_scenario(m_id: str, i: int) -> str:
        """确定性散列分流（非 0.25 档）：返回 'dup_b' / 'new_in_a' / 'existing_b'。"""
        digest = hashlib.blake2b(
            f"dupslot:{m_id}:{i}".encode("utf-8"), digest_size=8).digest()
        frac = int.from_bytes(digest, "big") / float(1 << 64)
        new_in_a_cut = dup_rate + (1.0 - dup_rate) / 3.0
        if frac < dup_rate:
            return "dup_b"
        if frac < new_in_a_cut:
            return "new_in_a"
        return "existing_b"

    # 按 mapping 分配记录
    records_per_mapping = (records + mappings - 1) // mappings
    total_written = 0

    for m_idx in range(mappings):
        a_root = a_roots[m_idx]
        b_root = b_roots[m_idx]
        m_id = ab_mappings[m_idx].mapping_id

        for i in range(records_per_mapping):
            if total_written >= records:
                break
            total_written += 1

            # 单射映射：每 show 容纳 1200 集（50 季 × 24 集）
            season_num = ((i // 24) % 50) + 1
            ep_num = (i % 24) + 1
            media_name = f"Show_{(i // 1200) + 1:04d}"
            ep_name = f"S{season_num:02d}E{ep_num:02d}.strm"
            rel_path = Path(media_name) / f"Season {season_num:02d}" / ep_name

            a_file = a_root / rel_path
            a_file.parent.mkdir(parents=True, exist_ok=True)
            webdav_path = f"/dav/{m_id}/{media_name}/Season {season_num:02d}/{ep_name[:-5]}.mp4"
            a_file.write_text(webdav_path, encoding="utf-8")

            if use_exact_quarter:
                scenario = i % 4
                if scenario in (0, 2):
                    scenario_name = "existing_b"
                elif scenario == 1:
                    scenario_name = "new_in_a"
                else:
                    scenario_name = "dup_b"
            else:
                scenario_name = _hash_scenario(m_id, i)

            if scenario_name == "existing_b":
                # existing_b: A 与 B 同时存在
                b_file = b_root / rel_path
                b_file.parent.mkdir(parents=True, exist_ok=True)
                b_file.write_text(webdav_path, encoding="utf-8")
                categories["existing_b"] += 1
            elif scenario_name == "new_in_a":
                # new_in_a: 仅存在于 A
                categories["new_in_a"] += 1
            else:
                # dup_b: 同一 fingerprint 产生两个可见 B 实例
                b_file = b_root / rel_path
                b_file.parent.mkdir(parents=True, exist_ok=True)
                b_file.write_text(webdav_path, encoding="utf-8")

                dup_name = f"S{season_num:02d}E{ep_num:02d} (1).strm"
                b_file_dup = b_root / rel_path.parent / dup_name
                b_file_dup.write_text(webdav_path, encoding="utf-8")
                categories["dup_b"] += 1

    # 返回前守恒断言：实际 A 文件数、唯一 WebDAV 路径数与场景计数严格守恒
    actual_a_files = sum(len(list(r.rglob("*.strm"))) for r in a_roots)
    assert actual_a_files == records, (
        f"实际 A 文件总数 ({actual_a_files}) != 预期记录数 ({records})"
    )
    all_webdavs: set[str] = set()
    for r in a_roots:
        for p in r.rglob("*.strm"):
            all_webdavs.add(p.read_text(encoding="utf-8"))
    assert len(all_webdavs) == records, (
        f"唯一 WebDAV 路径数 ({len(all_webdavs)}) != 预期记录数 ({records})"
    )
    assert sum(categories.values()) == records, (
        f"场景分类之和 ({sum(categories.values())}) != 预期记录数 ({records})"
    )

    return PipelineFixture(
        base_dir=base,
        a_roots=a_roots,
        b_roots=b_roots,
        c_root=c_root,
        db_path=db_path,
        mappings=ab_mappings,
        expected_records=records,
        categories=categories,
    )


def compute_terminal_digest(fixture: PipelineFixture) -> dict[str, str]:
    """计算终态物理文件与数据库多表的稳定 SHA256 摘要。

    路径使用相对于 fixture.base_dir 的相对规范化表示，确保跨 run 目录的确定性比对。
    """
    digests: dict[str, str] = {}
    base_prefix = str(fixture.base_dir.resolve()).replace("\\", "/")

    def _normalize(val: Any) -> str:
        if val is None:
            return ""
        s = str(val).replace("\\", "/")
        if s.startswith(base_prefix):
            s = s[len(base_prefix):]
        return s

    def _hash_dir(root: Path, name: str) -> str:
        h = hashlib.sha256()
        if not root.exists():
            return h.hexdigest()
        items = []
        for p in root.rglob("*"):
            if p.is_file():
                rel = str(p.relative_to(root)).replace("\\", "/")
                content_hash = hashlib.sha256(p.read_bytes()).hexdigest()
                items.append((rel, p.stat().st_size, content_hash))
        for rel, size, chash in sorted(items, key=lambda x: x[0]):
            h.update(f"{rel}:{size}:{chash}\n".encode("utf-8"))
        return h.hexdigest()

    for idx, a_root in enumerate(fixture.a_roots):
        digests[f"a_files_{idx+1}"] = _hash_dir(a_root, f"A{idx+1}")
    for idx, b_root in enumerate(fixture.b_roots):
        digests[f"b_files_{idx+1}"] = _hash_dir(b_root, f"B{idx+1}")
    digests["c_files"] = _hash_dir(fixture.c_root, "C")

    # DB 表摘要
    db_h = hashlib.sha256()
    if fixture.db_path.exists():
        try:
            con = sqlite3.connect(str(fixture.db_path))
            # a_strm_files
            rows_a = con.execute(
                "SELECT local_path, webdav_path, parent_webdav_path FROM a_strm_files ORDER BY local_path"
            ).fetchall()
            for r in rows_a:
                norm_lp = _normalize(r[0])
                db_h.update(f"A:{norm_lp}:{r[1]}:{r[2]}\n".encode("utf-8"))

            # b_strm_files
            rows_b = con.execute(
                "SELECT local_path, webdav_path, fingerprint, status, mapping_id FROM b_strm_files ORDER BY local_path"
            ).fetchall()
            for r in rows_b:
                norm_lp = _normalize(r[0])
                db_h.update(f"B:{norm_lp}:{r[1]}:{r[2]}:{r[3]}:{r[4]}\n".encode("utf-8"))

            # b_lineage_snapshot
            rows_snap = con.execute(
                "SELECT mapping_id, local_path, fingerprint, validation_state FROM b_lineage_snapshot ORDER BY mapping_id, local_path"
            ).fetchall()
            for r in rows_snap:
                norm_lp = _normalize(r[1])
                db_h.update(f"SNAP:{r[0]}:{norm_lp}:{r[2]}:{r[3]}\n".encode("utf-8"))

            con.close()
        except Exception as exc:
            db_h.update(f"ERROR:{exc}\n".encode("utf-8"))
    digests["database"] = db_h.hexdigest()

    # Overall digest
    overall = hashlib.sha256()
    for k in sorted(digests):
        overall.update(f"{k}:{digests[k]}\n".encode("utf-8"))
    digests["overall"] = overall.hexdigest()

    return digests


def _make_config(fixture: PipelineFixture) -> AppConfig:
    return AppConfig(
        base_dir=str(fixture.base_dir),
        webdav=WebDAVConfig(host="http://fake-webdav", user="", password="", totp_secret=""),
        refresh=RefreshConfig(interval_seconds=300, enabled=False),
        behavior=BehaviorConfig(sync_on_startup=True, sync_on_startup_wait=0),
        log=LogConfig(level="INFO", max_size_mb=10, backup_count=1),
        local=LocalConfig(
            base_dir=str(fixture.base_dir),
            a_dir=str(fixture.a_roots[0]),
            b_dir=str(fixture.b_roots[0]),
            c_dir=str(fixture.c_root),
            db_file=str(fixture.db_path),
        ),
        paths=PathsConfig(
            strm_engine_paths=[f"/dav/map{i+1}" for i in range(len(fixture.mappings))],
            refresh_paths=[],
            b_root=str(fixture.b_roots[0]),
            c_root=str(fixture.c_root),
        ),
        a_b_mappings=fixture.mappings,
    )


def _collect_disk_stats(fixture: PipelineFixture) -> dict[str, int]:
    """统计当前文件系统物理操作与文件数。"""
    a_count = sum(len(list(r.rglob("*.strm"))) for r in fixture.a_roots)
    b_count = sum(len(list(r.rglob("*.strm"))) for r in fixture.b_roots)
    c_count = len(list(fixture.c_root.rglob("*.strm")))
    return {"a_strm": a_count, "b_strm": b_count, "c_strm": c_count}


def _collect_b_strm_paths(fixture: PipelineFixture) -> set[str]:
    """收集 B 区全部 .strm 文件绝对路径（用于精确复制数差分）。

    注意：去重隔离会把 `.strm` 改名为 `.duplicate` 后缀，此时 b_strm 计数会
    减少；因此复制数必须用「路径集合差」而非「计数差」计算，避免隔离改名
    造成复制数被低估。
    """
    paths: set[str] = set()
    for b_root in fixture.b_roots:
        for p in b_root.rglob("*.strm"):
            paths.add(str(p))
    return paths


# ============================================================================
# T0 取证测量（benchmark-only，不改生产代码）
# ============================================================================

def _open_instrumented_connection(db_path: Path | str) -> sqlite3.Connection:
    """打开带 PRAGMA + simple 分词器的裸连接（长连接摊销测量用，C12-5）。

    与 Database.bulk_connection 同构（无写探针——摊销测量隔离的是连接级
    PRAGMA/load_extension 开销对逐行成本的污染，探针属连接打开成本由
    三重探针单独测量）。
    """
    conn = sqlite3.connect(str(db_path), timeout=30)
    for stmt in Database._PRAGMA_STATEMENTS:
        conn.execute(stmt)
    conn.enable_load_extension(True)
    if Database._SIMPLE_DLL.exists():
        conn.load_extension(str(Database._SIMPLE_DLL))
    return conn


def _measure_fts_bidir_amortization(
        db_path: Path | str, windows: tuple[int, ...] = (1000, 5000, 10000)) -> dict[str, Any]:
    """B1：FTS DELETE+INSERT 双向逐行摊销（10k 达标决定变量，C11-2）。

    长连接（隔离 PRAGMA + load_extension 的连接级开销污染，C12-5）对真实
    (rowid, local_path, webdav_path) 行连续执行与 move_b_record 逐字相同的
    双向语句：
        DELETE FROM b_strm_files_fts WHERE rowid = ?
        INSERT INTO b_strm_files_fts(rowid, local_path, webdav_path) VALUES(?,?,?)
    同内容回写，终态 FTS 行不变。输出逐调用分布 + 各窗口中位数 + 档位判定：
    <10ms/行 → optimistic；10-50ms → transitional；>50ms → pessimistic。
    """
    conn = _open_instrumented_connection(db_path)
    try:
        total_rows = conn.execute(
            "SELECT count(*) FROM b_strm_files").fetchone()[0]
        rows = conn.execute(
            "SELECT rowid, local_path, webdav_path FROM b_strm_files "
            "ORDER BY rowid LIMIT ?",
            (max(windows) if windows else total_rows,),
        ).fetchall()
        durations: list[float] = []
        for rowid, local_path, webdav_path in rows:
            t0 = time.perf_counter()
            conn.execute(
                "DELETE FROM b_strm_files_fts WHERE rowid = ?", (rowid,))
            conn.execute(
                "INSERT INTO b_strm_files_fts(rowid, local_path, webdav_path) "
                "VALUES(?,?,?)",
                (rowid, local_path, webdav_path))
            durations.append(time.perf_counter() - t0)
        conn.commit()

        result: dict[str, Any] = {
            "table_rows": total_rows,
            "measured_rows": len(rows),
            "overall": _dist_stats_ms(durations),
        }
        for window in windows:
            if window <= len(durations):
                result[f"first_{window}_median_ms"] = round(
                    statistics.median(
                        [s * 1000.0 for s in durations[:window]]), 3)
        median_ms = result["overall"].get("median_ms")
        if median_ms is not None:
            if median_ms < 10.0:
                tier = "optimistic"
            elif median_ms <= 50.0:
                tier = "transitional"
            else:
                tier = "pessimistic"
            result["median_ms_per_row"] = median_ms
            result["tier"] = tier
        return result
    finally:
        conn.close()


def _measure_connection_level_probe(
        db: Database, iterations: int = 20) -> dict[str, Any]:
    """连接级三重探针（C6 修正 + C11-1 归因更正）。

    归因口径（C11-1）：FTS5 分词器为表级配置（tokenize='simple' 建表时固化），
    不按连接实例化词典；本探针测量的是「连接打开（load_extension dlopen +
    PRAGMA + 写探针 ≈ ~7ms/连接）」与「连接首次 FTS 语句实化（一次性）」的
    复合差异——对同一 DB 开 20 个新连接，分别计时三组语句的
    「首 FTS 访问 vs 同连接第二次」，任一差值中位数 ≥20ms 即证实
    T1 建连消除收益模型。

    (a) SELECT count(*) FROM b_strm_files_fts
    (b) SELECT rowid FROM b_strm_files_fts WHERE b_strm_files_fts MATCH '<term>'
    (c) DELETE rowid=-1 + INSERT rowid=-1（move 路径原样语句，测后清理）
    """
    # 从 FTS 表取真实媒体名作为 MATCH 词项（simple 分词器处理查询串本身）
    with db.read_connection() as conn:
        probe_row = conn.execute(
            "SELECT local_path FROM b_strm_files_fts LIMIT 1").fetchone()
    match_term = "Show"
    if probe_row and probe_row[0]:
        stem = Path(str(probe_row[0])).parts
        if len(stem) >= 2:
            match_term = stem[-2]

    probes = {
        "count": {"first_ms": [], "second_ms": []},
        "match": {"first_ms": [], "second_ms": []},
        "write": {"first_ms": [], "second_ms": []},
    }
    open_ms: list[float] = []
    probe_local = "C:\\__probe__\\媒体.strm"
    probe_dav = "/dav/__probe__/媒体.mp4"

    for _ in range(iterations):
        t0 = time.perf_counter()
        with db.connection() as conn:  # 与 connection() 完整同构（含写探针）
            open_ms.append(time.perf_counter() - t0)

            t = time.perf_counter()
            conn.execute("SELECT count(*) FROM b_strm_files_fts").fetchone()
            probes["count"]["first_ms"].append(time.perf_counter() - t)
            t = time.perf_counter()
            conn.execute("SELECT count(*) FROM b_strm_files_fts").fetchone()
            probes["count"]["second_ms"].append(time.perf_counter() - t)

            t = time.perf_counter()
            conn.execute(
                "SELECT rowid FROM b_strm_files_fts "
                "WHERE b_strm_files_fts MATCH ?",
                (match_term,)).fetchall()
            probes["match"]["first_ms"].append(time.perf_counter() - t)
            t = time.perf_counter()
            conn.execute(
                "SELECT rowid FROM b_strm_files_fts "
                "WHERE b_strm_files_fts MATCH ?",
                (match_term,)).fetchall()
            probes["match"]["second_ms"].append(time.perf_counter() - t)

            def _bidir_once() -> None:
                conn.execute("DELETE FROM b_strm_files_fts WHERE rowid = -1")
                conn.execute(
                    "INSERT INTO b_strm_files_fts(rowid, local_path, webdav_path) "
                    "VALUES(-1, ?, ?)",
                    (probe_local, probe_dav))
                conn.commit()

            t = time.perf_counter()
            _bidir_once()
            probes["write"]["first_ms"].append(time.perf_counter() - t)
            t = time.perf_counter()
            _bidir_once()
            probes["write"]["second_ms"].append(time.perf_counter() - t)
            # 清理探针行
            conn.execute("DELETE FROM b_strm_files_fts WHERE rowid = -1")
            conn.commit()

    result: dict[str, Any] = {
        "iterations": iterations,
        "match_term": match_term,
        "conn_open_ms": _dist_stats_ms(open_ms),
    }
    confirmed = False
    for name, data in probes.items():
        first_stats = _dist_stats_ms(data["first_ms"])
        second_stats = _dist_stats_ms(data["second_ms"])
        delta_median_ms = round(statistics.median(data["first_ms"]) * 1000.0
                                - statistics.median(data["second_ms"]) * 1000.0, 3)
        result[name] = {
            "first": first_stats,
            "second": second_stats,
            "delta_median_ms": delta_median_ms,
            "benefit_confirmed": delta_median_ms >= 20.0,
        }
        if delta_median_ms >= 20.0:
            confirmed = True
    result["connection_benefit_confirmed"] = confirmed
    result["note"] = (
        "归因口径（C11-1）：分词器表级配置；测量的连接级成本 = load_extension "
        "dlopen + PRAGMA + 写探针 + 连接首次 FTS 语句实化")
    return result


def _measure_wave1_read_connections(
        app: AppService, db: CountingDatabase,
        fixture: PipelineFixture, max_paths: int = 800) -> dict[str, Any]:
    """wave1 并发读连接计数（发现 5）：8 线程模拟 _resolve_a_source 的 miss 突刺。

    冷启动缓存预载（_build_reconcile_cache 载入全部 A 记录）后，
    8 线程并发调用 _resolve_a_source，实测 read_connection 开启总数。
    """
    sample: list[tuple[str, str]] = []
    for b_root in fixture.b_roots:
        for p in b_root.rglob("*.strm"):
            sample.append((str(p), p.read_text(encoding="utf-8")))
            if len(sample) >= max_paths:
                break
        if len(sample) >= max_paths:
            break

    app._build_reconcile_cache()
    before = db.read_connection_opens
    resolved = 0

    def _one(item: tuple[str, str]):
        b_path, webdav = item
        return app._resolve_a_source(b_path, webdav, make_strm_fingerprint(webdav))

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            for res in executor.map(_one, sample):
                if res is not None:
                    resolved += 1
    finally:
        app._reconcile_cache = None

    return {
        "threads": 8,
        "calls": len(sample),
        "resolved": resolved,
        "read_connection_opens": db.read_connection_opens - before,
        "note": "冷启动缓存预载后 miss 应≈0；opens>0 表示存在缓存未覆盖回退",
    }


def _build_pre_dedup_state(
        root: Path, records: int, mappings: int, dup_rate: float) -> tuple[Database, AppService, PipelineFixture]:
    """构造「批写完成、去重未执行」的隔离前状态（A/B 原型共用）。

    与 _insert_new_b_records 批写同构：全部 B .strm 以 valid 状态经
    upsert_b_records_and_snapshots_batch 单事务批写入库（含 snapshot 与 FTS）。
    """
    fixture = build_fixture(root, records=records, mappings=mappings, dup_rate=dup_rate)
    db = Database(str(fixture.db_path))
    config = _make_config(fixture)
    app = AppService(config, db, FailFastAdmin())

    b_batch: list[tuple] = []
    snapshot_batch: list[tuple] = []
    for idx, b_root in enumerate(fixture.b_roots):
        mapping_id = fixture.mappings[idx].mapping_id
        for p in sorted(b_root.rglob("*.strm")):
            webdav = p.read_text(encoding="utf-8")
            fp = make_strm_fingerprint(webdav)
            stat = p.stat()
            b_batch.append((
                str(p), webdav, webdav_parent(webdav), None, fp,
                mapping_id, "valid"))
            snapshot_batch.append((
                mapping_id, str(p), stat.st_size, stat.st_mtime_ns, fp,
                app._mapping_version, LINEAGE_VERSION, "valid"))
    for i in range(0, len(b_batch), 1000):
        db.upsert_b_records_and_snapshots_batch(
            b_batch[i:i + 1000], snapshot_batch[i:i + 1000])
    return db, app, fixture


def _proto_move_on_conn(conn: sqlite3.Connection,
                        old_local_path: str, new_local_path: str) -> bool:
    """measurement-only：与 database.move_b_record 逐字相同的 SQL 序列。

    【measurement-only，T1 落地后删除】重复实现仅作原型测墙钟，
    不含 BEGIN/COMMIT/ROLLBACK（事务控制归组层）与失败恢复。
    """
    cur = conn.execute(
        """
        SELECT webdav_path, parent_webdav_path, source_a_path, fingerprint,
               mapping_id, status, last_verified_at
        FROM b_strm_files WHERE local_path = ?
        """,
        (old_local_path,))
    row = cur.fetchone()
    if not row:
        return False
    webdav_path, parent_webdav_path, source_a_path, fingerprint, mapping_id, status, last_verified_at = row
    now = time.time()
    new_status = status or "valid"
    old_rowid_row = conn.execute(
        "SELECT rowid FROM b_strm_files WHERE local_path = ?",
        (old_local_path,)).fetchone()
    old_rowid = old_rowid_row[0] if old_rowid_row else None
    conflict = conn.execute(
        "SELECT fingerprint FROM b_strm_files WHERE local_path = ?",
        (new_local_path,)).fetchone()
    if conflict and conflict[0] != fingerprint:
        return False
    prev_rowid_row = None
    if conflict:
        prev_rowid_row = conn.execute(
            "SELECT rowid FROM b_strm_files WHERE local_path = ?",
            (new_local_path,)).fetchone()
    conn.execute(
        """
        INSERT OR REPLACE INTO b_strm_files(
            local_path, webdav_path, parent_webdav_path, source_a_path,
            fingerprint, status, updated_at, mapping_id, last_verified_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (new_local_path, webdav_path, parent_webdav_path, source_a_path,
         fingerprint, new_status, now, mapping_id, last_verified_at))
    conn.execute(
        "DELETE FROM b_strm_files WHERE local_path = ?", (old_local_path,))
    if old_rowid is not None:
        conn.execute("DELETE FROM b_strm_files_fts WHERE rowid = ?", (old_rowid,))
    if prev_rowid_row is not None:
        conn.execute(
            "DELETE FROM b_strm_files_fts WHERE rowid = ?", (prev_rowid_row[0],))
    new_rowid_row = conn.execute(
        "SELECT rowid FROM b_strm_files WHERE local_path = ?",
        (new_local_path,)).fetchone()
    if new_rowid_row:
        conn.execute(
            "DELETE FROM b_strm_files_fts WHERE rowid = ?", (new_rowid_row[0],))
        conn.execute(
            "INSERT INTO b_strm_files_fts(rowid, local_path, webdav_path) VALUES(?,?,?)",
            (new_rowid_row[0], new_local_path, webdav_path))
    return True


def _prototype_batch_dedup(db: Database, app: AppService) -> int:
    """measurement-only 原型：会话批量化隔离（T1 设计骨架，T1 落地后删除）。

    与现行逐条路径的关键差异（仅测墙钟，不做失败恢复/B3 镜像对齐）：
    - 单连接逐组 BEGIN IMMEDIATE 事务（UPDATE 兄弟 duplicate → 逐实例 move SQL）；
    - ThreadPoolExecutor(4) 分块（32 组）并行 quarantine_file；
    - 复用既有 get_duplicate_fingerprint_groups / get_all_b_rows_for_fp_pairs /
      _b_file_score_pure（keep 评选与现行 ensure 同规则）。
    返回隔离改名的实例数。
    """
    dup_groups = db.get_duplicate_fingerprint_groups()
    if not dup_groups:
        return 0
    pairs = [(fp, mid) for mid, fp, _ in dup_groups]
    rows = db.get_all_b_rows_for_fp_pairs(pairs)
    by_group: dict[tuple[str, str], list] = defaultdict(list)
    for r in rows:
        by_group[(r.mapping_id, r.fingerprint)].append(r)
    webdav_map = {r.local_path: r.webdav_path for r in rows}
    sample_by_group = {(mid, fp): sample for mid, fp, sample in dup_groups}

    group_plans: list[tuple[tuple[str, str], str, list[str]]] = []
    for gkey, group_rows in by_group.items():
        valid_files = [
            r.local_path for r in group_rows
            if r.status == "valid" and Path(r.local_path).exists()]
        if not valid_files:
            continue
        prefer = sample_by_group.get(gkey) or min(valid_files)
        valid_files.sort(key=lambda p: (
            app._b_file_score_pure(p, webdav_map.get(p)),
            0 if p == prefer else 1))
        keep = valid_files[0]
        dups = [p for p in valid_files if p != keep]
        if dups:
            group_plans.append((gkey, keep, dups))
    if not group_plans:
        return 0

    moved = 0
    conn = _open_instrumented_connection(db.db_path)
    try:
        CHUNK = 32
        with ThreadPoolExecutor(max_workers=4) as executor:
            for chunk_start in range(0, len(group_plans), CHUNK):
                chunk = group_plans[chunk_start:chunk_start + CHUNK]
                futures: dict[Any, tuple[tuple[str, str], str]] = {}
                for gkey, _keep, dups in chunk:
                    for d in dups:
                        futures[executor.submit(
                            quarantine_file, Path(d), ".duplicate")] = (gkey, d)
                renames: dict[str, Path] = {}
                for fut in as_completed(futures):
                    gkey, d = futures[fut]
                    res = fut.result()
                    if res is not None:
                        renames[str(d)] = res
                with db.rw_lock.write_locked():
                    for gkey, keep, dups in chunk:
                        mid, fp = gkey
                        conn.execute("BEGIN IMMEDIATE")
                        try:
                            now = time.time()
                            conn.execute(
                                """
                                UPDATE b_strm_files
                                SET status = 'duplicate', updated_at = ?
                                WHERE fingerprint = ? AND mapping_id = ?
                                  AND local_path != ? AND status = 'valid'
                                """,
                                (now, fp, mid, keep))
                            for d in dups:
                                q = renames.get(str(d))
                                if q is None:
                                    continue  # 改名失败 → 保持 valid（终态等价现行）
                                if _proto_move_on_conn(conn, str(d), str(q)):
                                    moved += 1
                            conn.commit()
                        except sqlite3.Error:
                            conn.rollback()
                            raise
    finally:
        conn.close()
    return moved


def _measure_dedup_ab_prototype(
        work_dir: Path, records: int = 2400, mappings: int = 2,
        dup_rate: float = 0.25) -> dict[str, Any]:
    """去重循环 A/B 原型（measurement-only，T1 落地后删除）。

    同一“批写完成、去重未执行”起始状态下对比两种隔离的墙钟：
    - A 现行逐条：与 _cleanup_startup_duplicates 同形（GROUP BY → 逐组
      ensure_single_visible_instance，含每 dup 一次 10s 延迟清除线程 spawn）；
    - B 原型批量化：单连接逐组事务 + 4 线程并行改名（见 _prototype_batch_dedup）。

    门禁：B 墙钟 ≤ A 墙钟 / 10 才允许 T1 进入生产改造。
    终态等价校验：两侧 .duplicate 计数 == dup_b 数，且 compute_terminal_digest 相等。
    """
    # A: 现行逐条路径
    db_a, app_a, fixture_a = _build_pre_dedup_state(
        work_dir / "ab_proto_A", records=records, mappings=mappings, dup_rate=dup_rate)
    try:
        expected_dup = fixture_a.categories["dup_b"]
        t0 = time.perf_counter()
        app_a._cleanup_startup_duplicates()
        wall_a = time.perf_counter() - t0
        dup_files_a = sum(
            1 for r in fixture_a.b_roots for _ in r.rglob("*.duplicate"))
        digest_a = compute_terminal_digest(fixture_a)
    finally:
        app_a.stop()

    # B: 原型批量化路径（相同起始状态、相同 fixture 结构）
    db_b, app_b, fixture_b = _build_pre_dedup_state(
        work_dir / "ab_proto_B", records=records, mappings=mappings, dup_rate=dup_rate)
    try:
        t0 = time.perf_counter()
        moved_b = _prototype_batch_dedup(db_b, app_b)
        wall_b = time.perf_counter() - t0
        dup_files_b = sum(
            1 for r in fixture_b.b_roots for _ in r.rglob("*.duplicate"))
        digest_b = compute_terminal_digest(fixture_b)
    finally:
        app_b.stop()

    speedup = wall_a / wall_b if wall_b > 0 else float("inf")
    return {
        "records": records,
        "mappings": mappings,
        "dup_rate": dup_rate,
        "dup_groups": expected_dup,
        "current_wall_seconds": round(wall_a, 3),
        "prototype_wall_seconds": round(wall_b, 3),
        "prototype_moved": moved_b,
        "current_ms_per_dup": round(wall_a * 1000.0 / expected_dup, 3) if expected_dup else None,
        "prototype_ms_per_dup": round(wall_b * 1000.0 / expected_dup, 3) if expected_dup else None,
        "speedup": round(speedup, 2),
        "gate_10x_passed": bool(speedup >= 10.0),
        "terminal_equivalent": bool(
            dup_files_a == expected_dup
            and dup_files_b == expected_dup
            and digest_a["overall"] == digest_b["overall"]),
        "dup_files_current": dup_files_a,
        "dup_files_prototype": dup_files_b,
    }


def run_single_pipeline(
    fixture: PipelineFixture,
    track_memory: bool = False,
    t0_probe: bool = False,
) -> dict[str, Any]:
    """在隔离环境中执行一次完整的真实启动 pipeline。

    t0_probe=True 时（T0 基线取证，仅 run 1）在 5 阶段与终态 digest 之后追加：
    - B1 FTS 双向摊销（长连接）；
    - 连接级三重探针（跨 20 新连接）；
    - wave1 并发读连接计数（8 线程模拟）。
    这些测量不改变 pipeline 终态（B1 的 DELETE+INSERT 同内容回写保持 FTS 行不变）。
    """
    wall_start = time.perf_counter()
    config = _make_config(fixture)
    db = CountingDatabase(str(fixture.db_path))
    admin_api = FailFastAdmin()
    app = AppService(config, db, admin_api)

    before_stats = _collect_disk_stats(fixture)
    before_b_paths = _collect_b_strm_paths(fixture)
    wal_path = Path(str(fixture.db_path) + "-wal")
    wal_size_before = wal_path.stat().st_size if wal_path.exists() else 0

    stages: dict[str, dict[str, Any]] = {}
    if track_memory:
        tracemalloc.start()

    with _OperationInstrument() as ops_instrument, \
            _PhaseProfiler(app, db, ops_instrument) as phase_profiler:
        # Stage 1: initial_scan_a (use_bulk=True)
        t0 = time.perf_counter_ns()
        app.sync_service.initial_scan_a(use_bulk=True, a_roots=app.a_roots)
        t1 = time.perf_counter_ns()
        stages["initial_scan_a"] = {
            "seconds": (t1 - t0) / 1e9,
        }

        # Stage 2: initial_scan_b
        t0 = time.perf_counter_ns()
        app.initial_scan_b()
        t1 = time.perf_counter_ns()
        stages["initial_scan_b"] = {
            "seconds": (t1 - t0) / 1e9,
        }

        # Stage 3: scan_a_to_b_full_sync (use_bulk=True)
        t0 = time.perf_counter_ns()
        app.sync_service.scan_a_to_b_full_sync(use_bulk=True)
        t1 = time.perf_counter_ns()
        stages["scan_a_to_b_full_sync"] = {
            "seconds": (t1 - t0) / 1e9,
        }

        # Stage 4: [已废弃] _reconcile_catch_up —— v6 R2-A 单扫化后为空壳
        # （计时档保留，≈0s）；真实收口观测全部落在 Stage 5 boundary 口径
        t0 = time.perf_counter_ns()
        app._reconcile_catch_up()
        t1 = time.perf_counter_ns()
        stages["catch_up_readonly"] = {
            "seconds": (t1 - t0) / 1e9,
        }

        # Stage 5: watcher 窗口边界补扫（独立计时，仍只读）
        t0 = time.perf_counter_ns()
        app._reconcile_boundary_catch_up()
        t1 = time.perf_counter_ns()
        stages["boundary_catch_up"] = {
            "seconds": (t1 - t0) / 1e9,
        }

    wall_end = time.perf_counter()
    wall_clock_total = wall_end - wall_start

    peak_mem = None
    if track_memory:
        _, peak_mem = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    after_stats = _collect_disk_stats(fixture)
    wal_size_after = wal_path.stat().st_size if wal_path.exists() else 0
    terminal_digests = compute_terminal_digest(fixture)

    # FTS 行数对齐断言（吸收 a.1 Task 8.C，降级为基准断言）：
    # pipeline 结束 b_strm_files_fts 行数必须与主表严格一致（隔离迁移/批写
    # 的 FTS 维护无孤儿、无丢失）
    try:
        with db.read_connection() as _conn:
            fts_rows = _conn.execute(
                "SELECT count(*) FROM b_strm_files_fts").fetchone()[0]
            main_rows = _conn.execute(
                "SELECT count(*) FROM b_strm_files").fetchone()[0]
        fts_aligned = (fts_rows == main_rows)
    except Exception:  # noqa: BLE001
        fts_aligned = False
        fts_rows = main_rows = None

    # T0 归因数据（零成本收集：阶段剖析 + move 逐调用分布 + 连接计数）
    phase_walls = {
        name: round(p["wall_seconds"], 3)
        for name, p in phase_profiler.phases.items()
    }
    fourway_insert = phase_profiler.insert_new_attribution()
    move_call_distribution = _dist_stats_ms(db.move_b_record_durations)
    if db.move_b_record_durations:
        first_move_ms = db.move_b_record_durations[0] * 1000.0
        rest = db.move_b_record_durations[1:]
        move_call_distribution["first_call_ms"] = round(first_move_ms, 3)
        if rest:
            move_call_distribution["rest_median_ms"] = round(
                statistics.median(rest) * 1000.0, 3)
            move_call_distribution["outlier_shaped"] = bool(
                first_move_ms >= 5.0 * statistics.median(rest) * 1000.0
                and first_move_ms >= 1000.0)
            move_call_distribution["uniform_shaped"] = bool(
                not move_call_distribution["outlier_shaped"]
                and statistics.median(rest) * 1000.0 >= 50.0)
        move_call_distribution["raw_ms"] = [
            round(s * 1000.0, 3) for s in db.move_b_record_durations]
    connections = {
        "connection_opens": db.connection_opens,
        "read_connection_opens": db.read_connection_opens,
        "bulk_connection_opens": db.bulk_connection_opens,
    }

    # T0 取证探针（不改变终态；B1 同内容回写）
    t0_probes: dict[str, Any] = {}
    if t0_probe:
        try:
            t0_probes["b1_fts_bidir"] = _measure_fts_bidir_amortization(fixture.db_path)
        except Exception as exc:  # noqa: BLE001
            t0_probes["b1_fts_bidir"] = {"error": str(exc)}
        try:
            t0_probes["first_fts_probe"] = _measure_connection_level_probe(db)
        except Exception as exc:  # noqa: BLE001
            t0_probes["first_fts_probe"] = {"error": str(exc)}
        try:
            t0_probes["wave1_read_connections"] = _measure_wave1_read_connections(
                app, db, fixture)
        except Exception as exc:  # noqa: BLE001
            t0_probes["wave1_read_connections"] = {"error": str(exc)}

    # 物理操作真实测量：
    # copy: A->B 产生的真实新增 .strm 文件（用路径集合差计算，避免隔离改名
    #       （.strm → .duplicate）导致 b_strm 计数减少而低估复制数）
    # move: 真实物理隔离改名次数 (quarantine_file)
    # delete: 真实物理删除次数 (safe_remove_file)
    # db_delete: 真实 DB 行删除次数 (delete_b_by_local)
    after_b_paths = _collect_b_strm_paths(fixture)
    copied = len(after_b_paths - before_b_paths)
    physical_ops = {
        "copy": copied,
        "expected_copy": fixture.categories["new_in_a"],
        "move": ops_instrument.quarantine_success,
        "delete": ops_instrument.safe_remove_success,
        "db_delete": db.delete_b_by_local_calls,
        "wal_bytes_delta": wal_size_after - wal_size_before,
    }

    instrumented_stage_total = sum(s["seconds"] for s in stages.values())

    # 实际物理文件与路径口径
    actual_a_count = sum(len(list(r.rglob("*.strm"))) for r in fixture.a_roots)
    all_webdavs: set[str] = set()
    for r in fixture.a_roots:
        for p in r.rglob("*.strm"):
            all_webdavs.add(p.read_text(encoding="utf-8"))

    return {
        "app_service_type": "AppService",
        "database_type": "Database",
        "wall_clock_total": wall_clock_total,
        "instrumented_stage_total": instrumented_stage_total,
        "stage_overhead_seconds": wall_clock_total - instrumented_stage_total,
        "stages": stages,
        "phase_walls": phase_walls,
        "fourway_insert": fourway_insert,
        "move_call_distribution": move_call_distribution,
        "connections": connections,
        "t0_probes": t0_probes,
        "fts_aligned": fts_aligned,
        "fts_rows": fts_rows,
        "b_rows": main_rows,
        "peak_bytes": peak_mem,
        "fake_contract_calls": admin_api.fake_contract_calls,
        "unexpected_external_calls": admin_api.unexpected_external_calls,
        "real_http_calls": admin_api.real_http_calls,
        "network_calls": admin_api.network_calls,
        "physical_operations": physical_ops,
        "fixture_categories": dict(fixture.categories),
        "actual_a_files": actual_a_count,
        "actual_webdav_paths": len(all_webdavs),
        "terminal_digest": terminal_digests,
    }


def run_pipeline(
    work_dir: Path,
    records: int = 1000,
    mappings: int = 2,
    repeat: int = 2,
    output_dir: Path | None = None,
    max_seconds: float | None = None,
    track_memory: bool = False,
    dup_rate: float = 0.25,
    t0_instrument: bool = False,
) -> dict[str, Any]:
    """主 runner：支持重复运行，输出聚合指标与序列化报告。

    - `wall_clock_total` 为门禁依据；`instrumented_stage_total` 仅用于阶段归因；
      二者差值单独记录，不要求相等。
    - `gate2_passed`: 复合门禁（正确性 ∧ 耗时），无 threshold 时直接反映正确性。
    - `dup_rate`: fixture 重复率（0.25 默认保持 i%4 位精确分配，历史可比）。
    - `t0_instrument`: T0 取证（B1 双向摊销 / 三重探针 / wave1 计数 / A/B 原型），
      仅在 run 1 上执行，不改变 pipeline 终态。
    - 输出按 规模/映射/场景 分目录写入，禁止同名覆盖（添加运行批次子目录）。
    """
    runs: list[dict[str, Any]] = []

    for run_idx in range(repeat):
        run_root = work_dir / f"run_{run_idx+1}"
        run_root.mkdir(parents=True, exist_ok=True)
        fixture = build_fixture(
            run_root, records=records, mappings=mappings, dup_rate=dup_rate)
        single_result = run_single_pipeline(
            fixture, track_memory=track_memory,
            t0_probe=(t0_instrument and run_idx == 0))
        single_result["run_index"] = run_idx + 1
        runs.append(single_result)

    # 聚合各阶段耗时统计
    stage_names = [
        "initial_scan_a",
        "initial_scan_b",
        "scan_a_to_b_full_sync",
        "catch_up_readonly",
        "boundary_catch_up",
    ]
    summary_stages: dict[str, Any] = {}
    for st in stage_names:
        times = [r["stages"][st]["seconds"] for r in runs]
        summary_stages[st] = {
            "median_seconds": statistics.median(times),
            "stdev_seconds": statistics.stdev(times) if len(times) > 1 else 0.0,
            "min_seconds": min(times),
            "max_seconds": max(times),
        }

    wall_times = [r["wall_clock_total"] for r in runs]
    instrumented_times = [r["instrumented_stage_total"] for r in runs]
    peak_memories = [r["peak_bytes"] for r in runs if r["peak_bytes"] is not None]
    fake_contract_calls_total = sum(r["fake_contract_calls"] for r in runs)
    unexpected_external_calls_total = sum(r["unexpected_external_calls"] for r in runs)
    real_http_calls_total = sum(r["real_http_calls"] for r in runs)
    network_calls_total = sum(r["network_calls"] for r in runs)

    wall_median = statistics.median(wall_times)

    # 跨 repeat 物理操作与 terminal_digest 一致性检查
    first_digest = runs[0]["terminal_digest"]["overall"]
    digests_consistent = all(
        r["terminal_digest"]["overall"] == first_digest for r in runs
    )
    first_ops = runs[0]["physical_operations"]
    ops_consistent = all(
        r["physical_operations"]["copy"] == first_ops["copy"]
        and r["physical_operations"]["move"] == first_ops["move"]
        and r["physical_operations"]["delete"] == first_ops["delete"]
        and r["physical_operations"]["db_delete"] == first_ops["db_delete"]
        for r in runs
    )

    # 正确性硬断言集合：
    # 1. 复制数 == 预期复制数 (new_in_a)
    # 2. 隔离改名数 == dup_b 数
    # 3. 物理删除数 == 0（正常启动无越界删除）
    # 4. DB 删除数 == 0
    # 5. 三计数器全为 0
    # 6. 跨 run 摘要与物理操作一致
    # 7. FTS 行数对齐（a.1 Task 8.C 降级为基准断言）
    correctness_passed = (
        all(
            r["physical_operations"]["copy"] == r["physical_operations"]["expected_copy"]
            and r["physical_operations"]["move"] == r["fixture_categories"]["dup_b"]
            and r["physical_operations"]["delete"] == 0
            and r["physical_operations"]["db_delete"] == 0
            and r.get("fts_aligned", False)
            for r in runs
        )
        and fake_contract_calls_total == 0
        and unexpected_external_calls_total == 0
        and real_http_calls_total == 0
        and digests_consistent
        and ops_consistent
    )

    # Gate 2 复合判定：正确性 ∧ (耗时达标或无耗时门限)
    time_passed = wall_median < max_seconds if max_seconds is not None else True
    gate2_passed = bool(correctness_passed and time_passed)

    records_per_mapping = (records + mappings - 1) // mappings

    report = {
        "metadata": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "sqlite_version": sqlite3.sqlite_version,
            "records": records,
            "records_per_mapping": records_per_mapping,
            "mappings": mappings,
            "repeat": repeat,
            "max_seconds": max_seconds,
            "track_memory": track_memory,
            "dup_rate": dup_rate,
            "t0_instrument": t0_instrument,
        },
        "app_service_type": "AppService",
        "database_type": "Database",
        "fake_contract_calls": fake_contract_calls_total,
        "unexpected_external_calls": unexpected_external_calls_total,
        "real_http_calls": real_http_calls_total,
        "network_calls": network_calls_total,
        "correctness_passed": correctness_passed,
        "gate2_passed": gate2_passed,
        "stages": stage_names,
        "physical_operations": runs[0]["physical_operations"],
        "terminal_digest": runs[0]["terminal_digest"],
        "t0_instrumentation": {},
        "summary": {
            "wall_clock_median_seconds": wall_median,
            "wall_clock_stdev_seconds": statistics.stdev(wall_times) if len(wall_times) > 1 else 0.0,
            "wall_clock_min_seconds": min(wall_times),
            "wall_clock_max_seconds": max(wall_times),
            "instrumented_median_seconds": statistics.median(instrumented_times),
            "stage_overhead_median_seconds": statistics.median(
                [r["stage_overhead_seconds"] for r in runs]
            ),
            "peak_bytes_max": max(peak_memories) if peak_memories else None,
            "stages": summary_stages,
        },
        "runs": runs,
    }

    # ---- T0 取证汇总（t0_instrument=True 时填充；归因数据零成本始终可用） ----
    if t0_instrument:
        first = runs[0]
        probes = first.get("t0_probes", {})
        b1 = probes.get("b1_fts_bidir", {})
        first_fts = probes.get("first_fts_probe", {})
        move_dist = first.get("move_call_distribution", {})
        ab_proto: dict[str, Any]
        try:
            ab_proto = _measure_dedup_ab_prototype(
                work_dir, records=2400, mappings=mappings, dup_rate=0.25)
        except Exception as exc:  # noqa: BLE001
            ab_proto = {"error": str(exc)}

        # 建连消除收益：任一探针 ≥20ms 或 move 分布呈首调用离群值型
        probe_confirmed = bool(first_fts.get("connection_benefit_confirmed"))
        outlier_confirmed = bool(move_dist.get("outlier_shaped"))
        b1_tier = b1.get("tier")
        proto_passed = bool(ab_proto.get("gate_10x_passed"))

        report["t0_instrumentation"] = {
            "b1_fts_bidir": b1,
            "first_fts_probe": first_fts,
            "wave1_read_connections": probes.get("wave1_read_connections"),
            "fourway_insert": first.get("fourway_insert"),
            "phase_walls": first.get("phase_walls"),
            "move_call_distribution": move_dist,
            "connections": first.get("connections"),
            "ab_dedup_proto": ab_proto,
            "gates": {
                "b1_tier": b1_tier,
                "b1_pessimistic_exit": b1_tier == "pessimistic",
                "connection_benefit_confirmed": probe_confirmed or outlier_confirmed,
                "connection_benefit_via_probe": probe_confirmed,
                "connection_benefit_via_outlier": outlier_confirmed,
                "proto_10x_passed": proto_passed,
                "t1_proceed": bool(
                    b1_tier in ("optimistic", "transitional")
                    and (probe_confirmed or outlier_confirmed)
                    and proto_passed),
            },
        }
    else:
        # 零成本归因数据仍然随 run 输出（无探针/原型）
        report["t0_instrumentation"] = {
            "fourway_insert": runs[0].get("fourway_insert"),
            "phase_walls": runs[0].get("phase_walls"),
            "move_call_distribution": runs[0].get("move_call_distribution"),
            "connections": runs[0].get("connections"),
            "note": "t0_instrument=False——未执行 B1/探针/原型（--t0-instrument 开启）",
        }

    if output_dir:
        out = Path(output_dir).resolve()
        out = out / f"records-{records}" / f"mapping-{mappings}" / f"batch-{int(time.time())}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "pipeline_results.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        csv_path = out / "pipeline_runs.csv"
        with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "run_index",
                "wall_clock_total_seconds",
                "instrumented_stage_total_seconds",
                "initial_scan_a_seconds",
                "initial_scan_b_seconds",
                "scan_a_to_b_seconds",
                "catch_up_seconds",
                "boundary_catch_up_seconds",
                "peak_bytes",
                "fake_contract_calls",
                "unexpected_external_calls",
                "real_http_calls",
                "wal_delta_bytes",
                "copy",
                "expected_copy",
                "move",
                "delete",
                "db_delete",
                "overall_digest",
            ])
            for r in runs:
                writer.writerow([
                    r["run_index"],
                    f"{r['wall_clock_total']:.6f}",
                    f"{r['instrumented_stage_total']:.6f}",
                    f"{r['stages']['initial_scan_a']['seconds']:.6f}",
                    f"{r['stages']['initial_scan_b']['seconds']:.6f}",
                    f"{r['stages']['scan_a_to_b_full_sync']['seconds']:.6f}",
                    f"{r['stages']['catch_up_readonly']['seconds']:.6f}",
                    f"{r['stages']['boundary_catch_up']['seconds']:.6f}",
                    r["peak_bytes"] if r["peak_bytes"] is not None else "",
                    r["fake_contract_calls"],
                    r["unexpected_external_calls"],
                    r["real_http_calls"],
                    r["physical_operations"]["wal_bytes_delta"],
                    r["physical_operations"]["copy"],
                    r["physical_operations"]["expected_copy"],
                    r["physical_operations"]["move"],
                    r["physical_operations"]["delete"],
                    r["physical_operations"]["db_delete"],
                    r["terminal_digest"]["overall"],
                ])

    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="真实 AppService 启动 pipeline benchmark"
    )

    def _positive_int(val: str) -> int:
        ival = int(val)
        if ival < 1:
            raise argparse.ArgumentTypeError(f"Must be positive integer, got {val}")
        return ival

    def _mappings_int(val: str) -> int:
        ival = int(val)
        if ival not in (1, 2):
            raise argparse.ArgumentTypeError(f"Mappings must be 1 or 2, got {val}")
        return ival

    def _nonnegative_float(val: str) -> float:
        fval = float(val)
        if fval < 0:
            raise argparse.ArgumentTypeError(f"Must be non-negative float, got {val}")
        return fval

    def _dup_rate_float(val: str) -> float:
        fval = float(val)
        if not (0.0 <= fval <= 1.0):
            raise argparse.ArgumentTypeError(
                f"dup-rate must be within [0, 1], got {val}")
        return fval

    parser.add_argument(
        "--records",
        type=_positive_int,
        default=1000,
        help="测试记录规模（双 mapping 时为两组映射的记录总量，默认: 1000）",
    )
    parser.add_argument(
        "--mappings",
        type=_mappings_int,
        default=2,
        help="A/B 映射组数 (1 或 2, 默认: 2)",
    )
    parser.add_argument(
        "--repeat",
        type=_positive_int,
        default=2,
        help="重复执行次数 (默认: 2)",
    )
    parser.add_argument(
        "--max-seconds",
        type=_nonnegative_float,
        default=None,
        help="wall_clock_total 中位数阈值（秒）；超阈值时 CLI 返回非零退出码 (Gate 2)",
    )
    parser.add_argument(
        "--dup-rate",
        type=_dup_rate_float,
        default=0.25,
        help="fixture 重复率 (0-1, 默认: 0.25 保持 i%%4 位精确分配；"
             "其他取值用确定性散列分流。25%% 对抗档保持主门禁不变)",
    )
    parser.add_argument(
        "--t0-instrument",
        action="store_true",
        default=False,
        help="启用 T0 取证探针（B1 FTS 双向摊销 / 连接级三重探针 / wave1 计数 / "
             "去重 A/B 原型），仅在 run 1 执行，不改变 pipeline 终态",
    )
    parser.add_argument(
        "--track-memory",
        action="store_true",
        default=False,
        help="启用 tracemalloc 内存峰值跟踪（默认关闭以消除分配追踪对墙钟计时的污染）",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="结果导出根目录（按 规模/映射/批次 分目录写入 JSON/CSV）",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="perf_pipeline_") as tmp_dir:
        report = run_pipeline(
            work_dir=Path(tmp_dir),
            records=args.records,
            mappings=args.mappings,
            repeat=args.repeat,
            output_dir=args.output_dir,
            max_seconds=args.max_seconds,
            track_memory=args.track_memory,
            dup_rate=args.dup_rate,
            t0_instrument=args.t0_instrument,
        )

    wall_median = report["summary"]["wall_clock_median_seconds"]
    correctness_passed = report["correctness_passed"]

    # CLI 标准输出包含完整摘要和 parameters
    output_payload = {
        "parameters": {
            "records": args.records,
            "mappings": args.mappings,
            "repeat": args.repeat,
            "max_seconds": args.max_seconds,
            "dup_rate": args.dup_rate,
            "t0_instrument": args.t0_instrument,
            "track_memory": args.track_memory,
        },
        "summary": report["summary"],
        "metadata": report["metadata"],
        "fake_contract_calls": report["fake_contract_calls"],
        "unexpected_external_calls": report["unexpected_external_calls"],
        "real_http_calls": report["real_http_calls"],
        "correctness_passed": correctness_passed,
        "gate2_passed": report["gate2_passed"],
        "t0_instrumentation": report["t0_instrumentation"],
        "runs": report["runs"],
    }
    print(json.dumps(output_payload, indent=2, ensure_ascii=False))

    # 正确性失败优先退出 (退出码 2)
    if not correctness_passed:
        print(
            "[GATE FAIL] 正确性校验未通过 (复制数/隔离改名数/删除数/三计数器/跨run一致性异常)",
            file=sys.stderr,
        )
        return 2

    # Gate 2 耗时阈值失败化：中位数超过 max-seconds 时非零退出 (退出码 3)
    if args.max_seconds is not None and wall_median >= args.max_seconds:
        print(
            f"[GATE FAIL] wall_clock_total median {wall_median:.3f}s >= "
            f"--max-seconds {args.max_seconds:.3f}s",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
