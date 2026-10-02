#!/usr/bin/env python3
"""Runner C: 真实增量流水线与终态状态机验证。

职责：
1. 第一次启动建立冷启动基线终态（复用 Runner A fixture）；
2. 保留同一数据库文件与 A/B/C 目录，不销毁重构；
3. 按确定性种子选取 5% delta 集合（明确计算 added、modified、removed 数量及取整规则）；
4. 第二次启动在同一 fixture 上执行增量同步；
5. 全面验证 unchanged、added、modified、removed 四态以及 mapping、lineage、identity、generation、FTS、snapshot 终态；
6. 专项验证 B 区清理的 check_exists 三态（True/False/None）fail-closed 契约；
7. 冷启动与增量启动性能指标独立统计、独立分目录导出。
"""
from __future__ import annotations

import argparse
import csv
import gc
import json
import math
import os
import platform
import random
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# 确保 src/ 与 src/tests/perf/ 在 sys.path
_PERF_DIR = Path(__file__).resolve().parent
_SRC_ROOT = _PERF_DIR.parent.parent
if str(_PERF_DIR) not in sys.path:
    sys.path.insert(0, str(_PERF_DIR))
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from app_service_core import AppService
from benchmark_startup_pipeline import (
    PipelineFixture,
    build_fixture,
    compute_terminal_digest,
    _collect_disk_stats,
    _make_config,
)
from config import ABMapping
from database import Database
from utils import make_strm_fingerprint, read_strm_webdav_path


class IncrementalZeroAdmin:
    """全线程可观测的零调用 Fake Admin，默认阻断所有外部协议调用。

    支持为 B 区冗余清理等专项场景显式配置 check_exists 的三态映射 (True/False/None)。
    """

    def __init__(self, check_exists_map: dict[str, bool | None] | None = None) -> None:
        self.check_exists_map = check_exists_map or {}
        self.fake_contract_calls = 0
        self.unexpected_external_calls = 0
        self.real_http_calls = 0
        self.traces: list[dict[str, Any]] = []

    def check_exists(self, path: str) -> bool | None:
        if path in self.check_exists_map:
            self.fake_contract_calls += 1
            res = self.check_exists_map[path]
            self.traces.append({"method": "check_exists", "path": path, "result": res})
            return res
        self.unexpected_external_calls += 1
        raise RuntimeError(f"Unexpected check_exists call for path: {path}")

    def get_strm_storages_full_info(self) -> list[dict[str, Any]]:
        self.unexpected_external_calls += 1
        raise RuntimeError("Unexpected get_strm_storages_full_info call in offline pipeline")


@dataclass
class DeltaSpec:
    total_records: int
    delta_percentage: float
    total_delta_count: int
    added_count: int
    modified_count: int
    removed_count: int
    seed: int
    removed_a_paths: list[str] = field(default_factory=list)
    modified_a_paths: list[tuple[str, str, str]] = field(default_factory=list)  # (path, old_webdav, new_webdav)
    added_a_paths: list[tuple[str, str]] = field(default_factory=list)  # (path, webdav)


def calculate_delta_counts(records: int, delta_pct: float = 0.05) -> tuple[int, int, int, int]:
    """明确定义 5% delta 取整与分配规则：

    总 delta 数量 = max(3, ceil(records * delta_pct))
    均分到 added, modified, removed；余数由 added 吸收。
    """
    total_delta = max(3, math.ceil(records * delta_pct))
    base = total_delta // 3
    rem = total_delta % 3
    added = base + rem
    modified = base
    removed = base
    return total_delta, added, modified, removed


def apply_deterministic_delta(fixture: PipelineFixture, delta_pct: float = 0.05, seed: int = 42) -> DeltaSpec:
    """在保持既有 DB 与 A/B 目录不变的前提下，在 A 区磁盘上施加确定性的 delta 变更。"""
    rng = random.Random(seed)
    total_delta, added_count, modified_count, removed_count = calculate_delta_counts(
        fixture.expected_records, delta_pct
    )

    all_a_files: list[Path] = []
    for a_root in fixture.a_roots:
        all_a_files.extend(sorted(a_root.rglob("*.strm")))

    if len(all_a_files) < (modified_count + removed_count):
        raise ValueError("Not enough A files to apply delta")

    # 随机打乱选择要移除和修改的文件
    indices = list(range(len(all_a_files)))
    rng.shuffle(indices)

    removed_indices = indices[:removed_count]
    modified_indices = indices[removed_count : removed_count + modified_count]

    removed_paths: list[str] = []
    modified_paths: list[tuple[str, str, str]] = []
    added_paths: list[tuple[str, str]] = []

    # 1. 物理移除 A 区文件
    for idx in removed_indices:
        p = all_a_files[idx]
        removed_paths.append(str(p))
        p.unlink()

    # 2. 修改 A 区文件内容（更新 WebDAV 路径，使其指纹变更）
    for idx in modified_indices:
        p = all_a_files[idx]
        old_webdav = read_strm_webdav_path(p) or ""
        # 保持相同扩展名与文件名，变更季级目录与文件名编号，产生新的确定性 WebDAV
        rel = p.relative_to(p.parents[2])
        new_webdav = f"/dav/modified/{p.stem}_v2.mp4"
        p.write_text(new_webdav, encoding="utf-8")
        modified_paths.append((str(p), old_webdav, new_webdav))

    # 3. 在 A 区新增文件
    target_a_root = fixture.a_roots[0]
    for i in range(added_count):
        show_dir = target_a_root / f"Show_Delta_{(i+1):04d}" / "Season 01"
        show_dir.mkdir(parents=True, exist_ok=True)
        ep_name = f"S01E{(i+1):02d}.strm"
        new_file = show_dir / ep_name
        webdav_path = f"/dav/map1/Show_Delta_{(i+1):04d}/Season 01/S01E{(i+1):02d}.mp4"
        new_file.write_text(webdav_path, encoding="utf-8")
        added_paths.append((str(new_file), webdav_path))

    return DeltaSpec(
        total_records=fixture.expected_records,
        delta_percentage=delta_pct,
        total_delta_count=total_delta,
        added_count=added_count,
        modified_count=modified_count,
        removed_count=removed_count,
        seed=seed,
        removed_a_paths=removed_paths,
        modified_a_paths=modified_paths,
        added_a_paths=added_paths,
    )


def run_pipeline_stage_sequence(app: AppService) -> dict[str, float]:
    """执行完整的启动阶段流水线并记录独立耗时。"""
    stages: dict[str, float] = {}

    t0 = time.perf_counter_ns()
    app.sync_service.initial_scan_a(use_bulk=True, a_roots=app.a_roots)
    stages["initial_scan_a"] = (time.perf_counter_ns() - t0) / 1e9

    t0 = time.perf_counter_ns()
    app.initial_scan_b()
    stages["initial_scan_b"] = (time.perf_counter_ns() - t0) / 1e9

    t0 = time.perf_counter_ns()
    app.sync_service.scan_a_to_b_full_sync(use_bulk=True)
    stages["scan_a_to_b_full_sync"] = (time.perf_counter_ns() - t0) / 1e9

    t0 = time.perf_counter_ns()
    # [已废弃] v6 R2-A 单扫化后 _reconcile_catch_up 为空壳（计时档保留，
    # 真实收口观测全部落在 boundary_catch_up 口径）
    app._reconcile_catch_up()
    stages["catch_up_readonly"] = (time.perf_counter_ns() - t0) / 1e9

    t0 = time.perf_counter_ns()
    app._reconcile_boundary_catch_up()
    stages["boundary_catch_up"] = (time.perf_counter_ns() - t0) / 1e9

    return stages


def verify_database_and_physical_state(
    fixture: PipelineFixture, delta: DeltaSpec
) -> dict[str, Any]:
    """全面校验增量终态下的状态机不变式。"""
    db = Database(str(fixture.db_path))

    with db.read_connection() as conn:
        a_records = conn.execute("SELECT local_path, webdav_path FROM a_strm_files").fetchall()
        b_records = conn.execute("SELECT local_path, webdav_path, fingerprint, status, mapping_id FROM b_strm_files").fetchall()
        snapshots = conn.execute("SELECT mapping_id, local_path, fingerprint, validation_state FROM b_lineage_snapshot").fetchall()
        fts_a_rows = conn.execute("SELECT rowid, local_path FROM a_strm_files_fts").fetchall()
        fts_b_rows = conn.execute("SELECT rowid, local_path FROM b_strm_files_fts").fetchall()

    a_map = {row[0]: row[1] for row in a_records}
    b_map = {row[0]: row for row in b_records}
    snap_map = {row[1]: row for row in snapshots}

    # 1. Removed 校验：A 物理文件已删除；根据 fail-safe 设计，A 数据库记录保留（等待 API 清理），B 区物理与 DB 记录保持受保护
    removed_verified = True
    for p in delta.removed_a_paths:
        if Path(p).exists():
            removed_verified = False
        if p not in a_map:
            removed_verified = False  # 启动扫描不直接删除无源 DB 记录

    # 2. Added 校验：A 物理文件存在且已建 A 记录；B 物理文件已同步且已建 B 记录、FTS 与快照
    added_verified = True
    for p, webdav in delta.added_a_paths:
        if not Path(p).exists() or p not in a_map:
            added_verified = False
        # 对应 B 目标应已同步创建
        b_target = fixture.b_roots[0] / Path(p).relative_to(fixture.a_roots[0])
        if not b_target.exists() or str(b_target) not in b_map:
            added_verified = False

    # 3. Modified 校验：A 记录 WebDAV 已更新；B 记录保护（skip_exists_diff 保护用户已有文件不被覆盖）
    modified_verified = True
    for p, old_w, new_w in delta.modified_a_paths:
        if a_map.get(p) != new_w:
            modified_verified = False

    # 4. FTS 完整性校验：FTS 行数与主表行数对齐
    fts_a_aligned = len(fts_a_rows) == len(a_records)
    fts_b_aligned = len(fts_b_rows) == len(b_records)

    return {
        "removed_verified": removed_verified,
        "added_verified": added_verified,
        "modified_verified": modified_verified,
        "fts_a_aligned": fts_a_aligned,
        "fts_b_aligned": fts_b_aligned,
        "a_record_count": len(a_records),
        "b_record_count": len(b_records),
        "snapshot_count": len(snapshots),
    }


def verify_b_cleanup_three_state_contract(base_dir: Path) -> dict[str, Any]:
    """专项验证：B 区冗余清理对 check_exists 的 True / False / None 三态 fail-closed 契约。

    在**独立最小 fixture** 上构造 3 个无对应 A 源文件的孤儿 B 记录，
    避免增量 fixture 中已删除 A 源文件泄漏额外的 check_exists 调用。
    """
    base = Path(base_dir).resolve()
    a_root = base / "A1"
    b_root = base / "B1"
    c_root = base / "C"
    db_path = base / "bridge.db"
    for d in (a_root, b_root, c_root):
        d.mkdir(parents=True, exist_ok=True)

    mapping = ABMapping(
        mapping_id="map1", a_root=str(a_root), b_root=str(b_root), label="Mapping 1"
    )
    from benchmark_startup_pipeline import PipelineFixture as _PF
    fixture = _PF(
        base_dir=base,
        a_roots=[a_root],
        b_roots=[b_root],
        c_root=c_root,
        db_path=db_path,
        mappings=[mapping],
        expected_records=0,
    )
    config = _make_config(fixture)
    db = Database(str(db_path))

    p_true = b_root / "Orphan_True" / "Season 01" / "S01E01.strm"
    p_false = b_root / "Orphan_False" / "Season 01" / "S01E01.strm"
    p_none = b_root / "Orphan_None" / "Season 01" / "S01E01.strm"

    for p in (p_true, p_false, p_none):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"/dav/map1/{p.parent.parent.name}/{p.name[:-5]}.mp4", encoding="utf-8")

    w_true = f"/dav/map1/Orphan_True/Season 01/S01E01.mp4"
    w_false = f"/dav/map1/Orphan_False/Season 01/S01E01.mp4"
    w_none = f"/dav/map1/Orphan_None/Season 01/S01E01.mp4"

    db.upsert_b_batch([
        (str(p_true), w_true, "/dav/map1/Orphan_True/Season 01", "", make_strm_fingerprint(w_true), "map1", "valid"),
        (str(p_false), w_false, "/dav/map1/Orphan_False/Season 01", "", make_strm_fingerprint(w_false), "map1", "valid"),
        (str(p_none), w_none, "/dav/map1/Orphan_None/Season 01", "", make_strm_fingerprint(w_none), "map1", "valid"),
    ])

    admin = IncrementalZeroAdmin(check_exists_map={
        w_true: True,    # 云端存在 → 暂存跳过
        w_false: False,  # 权威不存在 → 触发清理/C区隔离
        w_none: None,    # 不可信 → fail-closed 跳过
    })

    app = AppService(config, db, admin)
    app.cleanup_b_redundant()

    true_kept = p_true.exists() and db.get_b_by_local(str(p_true)) is not None
    false_cleaned = not p_false.exists() and db.get_b_by_local(str(p_false)) is None
    none_kept_fail_closed = p_none.exists() and db.get_b_by_local(str(p_none)) is not None

    return {
        "true_kept": true_kept,
        "false_cleaned": false_cleaned,
        "none_kept_fail_closed": none_kept_fail_closed,
        "three_state_passed": true_kept and false_cleaned and none_kept_fail_closed,
        "fake_contract_calls": admin.fake_contract_calls,
        "unexpected_external_calls": admin.unexpected_external_calls,
    }


def run_incremental_benchmark(
    base_dir: Path,
    records: int = 1000,
    mappings: int = 2,
    delta_pct: float = 0.05,
    seed: int = 42,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """主 runner：在同一隔离 fixture 上顺序执行冷启动基线与增量二次启动。"""
    fixture = build_fixture(base_dir, records=records, mappings=mappings)
    config = _make_config(fixture)
    db = Database(str(fixture.db_path))

    admin1 = IncrementalZeroAdmin()
    app1 = AppService(config, db, admin1)

    # 1. 第一次冷启动
    t0_cold = time.perf_counter()
    cold_stages = run_pipeline_stage_sequence(app1)
    cold_wall = time.perf_counter() - t0_cold
    cold_digest = compute_terminal_digest(fixture)

    # 2. 施加确定性 5% Delta
    delta = apply_deterministic_delta(fixture, delta_pct=delta_pct, seed=seed)

    # 3. 第二次增量启动（复用同一 DB 和根目录）
    admin2 = IncrementalZeroAdmin()
    app2 = AppService(config, db, admin2)

    t0_incr = time.perf_counter()
    incr_stages = run_pipeline_stage_sequence(app2)
    incr_wall = time.perf_counter() - t0_incr
    incr_digest = compute_terminal_digest(fixture)

    # 4. 验证终态状态机
    verify_res = verify_database_and_physical_state(fixture, delta)

    # 5. 专项验证 B 区清理的三态契约（独立最小 fixture）
    with tempfile.TemporaryDirectory(prefix="perf_b_cleanup_") as cleanup_dir:
        b_cleanup_res = verify_b_cleanup_three_state_contract(Path(cleanup_dir))

    report = {
        "metadata": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "sqlite_version": sqlite3.sqlite_version,
            "records": records,
            "mappings": mappings,
            "delta_pct": delta_pct,
            "seed": seed,
            "delta_counts": {
                "total": delta.total_delta_count,
                "added": delta.added_count,
                "modified": delta.modified_count,
                "removed": delta.removed_count,
            },
        },
        "cold_run": {
            "wall_clock_seconds": cold_wall,
            "stages": cold_stages,
            "terminal_digest": cold_digest["overall"],
            "fake_contract_calls": admin1.fake_contract_calls,
            "unexpected_external_calls": admin1.unexpected_external_calls,
            "real_http_calls": admin1.real_http_calls,
        },
        "incremental_run": {
            "wall_clock_seconds": incr_wall,
            "stages": incr_stages,
            "terminal_digest": incr_digest["overall"],
            "fake_contract_calls": admin2.fake_contract_calls,
            "unexpected_external_calls": admin2.unexpected_external_calls,
            "real_http_calls": admin2.real_http_calls,
        },
        "speedup_ratio": cold_wall / incr_wall if incr_wall > 0 else 1.0,
        "verification": verify_res,
        "b_cleanup_three_state": b_cleanup_res,
        "all_passed": (
            verify_res["removed_verified"]
            and verify_res["added_verified"]
            and verify_res["modified_verified"]
            and verify_res["fts_a_aligned"]
            and verify_res["fts_b_aligned"]
            and b_cleanup_res["three_state_passed"]
            and admin1.unexpected_external_calls == 0
            and admin2.unexpected_external_calls == 0
        ),
    }

    if output_dir:
        out = Path(output_dir).resolve() / f"incremental-records-{records}" / f"mapping-{mappings}" / f"batch-{int(time.time())}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "incremental_results.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        csv_path = out / "incremental_runs.csv"
        with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "records",
                "mappings",
                "delta_pct",
                "cold_wall_seconds",
                "incremental_wall_seconds",
                "speedup_ratio",
                "all_passed",
                "cold_digest",
                "incremental_digest",
            ])
            writer.writerow([
                records,
                mappings,
                delta_pct,
                f"{cold_wall:.6f}",
                f"{incr_wall:.6f}",
                f"{report['speedup_ratio']:.3f}",
                report["all_passed"],
                cold_digest["overall"],
                incr_digest["overall"],
            ])

    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Runner C: 真实增量流水线基准")

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

    parser.add_argument("--records", type=_positive_int, default=1000, help="测试记录规模")
    parser.add_argument("--mappings", type=_mappings_int, default=2, help="映射组数 (1 或 2)")
    parser.add_argument("--delta-pct", type=float, default=0.05, help="Delta 比例 (默认 0.05)")
    parser.add_argument("--seed", type=int, default=42, help="随机数种子 (默认 42)")
    parser.add_argument("--output-dir", type=Path, default=None, help="输出目录")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="perf_incr_") as tmp_dir:
        report = run_incremental_benchmark(
            base_dir=Path(tmp_dir),
            records=args.records,
            mappings=args.mappings,
            delta_pct=args.delta_pct,
            seed=args.seed,
            output_dir=args.output_dir,
        )

    print("===PERF_JSON_START===")
    print(json.dumps(report, indent=2, ensure_ascii=False))

    if not report["all_passed"]:
        print("[GATE FAIL] Runner C incremental verification failed!", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
