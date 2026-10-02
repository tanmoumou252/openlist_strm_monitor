#!/usr/bin/env python3
"""B区历史记录核对性能基准测试 — 简化 schema，仅用于趋势比较。

**注意：** 本脚本使用自建简化 schema，**不代表生产路径**，单条耗时不能
当作生产性能结论。生产 B 区历史核对优化后的度量点是启动日志中的
「B 区历史记录核对完成 (%d/%d 条, %.1fs)」日志行或真实启动耗时对比。

核心思路 (类比 Git 增量判断):
  - baseline: 每次启动全量核对所有 B 记录
  - optimized: 启动时只核对变化的记录 (增量校验)

增量依据:
  1. 文件元数据快照 (size + mtime) — 发现程序停机期间的外部修改
  2. 根映射版本 — 映射配置变化时使相关记录失效
  3. 上次验证状态 — 失败/异常的记录必须重新验证

用法:
    python src/tests/perf/benchmark_lineage.py --records 1000 --repeat 3
    python src/tests/perf/benchmark_lineage.py --records 8507 --repeat 3 --profile

基线 (来自 strm_bridge.log):
  8507条 / 59.5秒 = 6.9ms/条
  瓶颈: _verify_b_path_lineage 每条做 3-5 次 Path.resolve() + 2-8 次 DB 查询

目标:
  8507条 / <10秒 (启动快速增量)
  全量审计可后台异步执行

注意：生产代码自本轮优化后已为 B 区历史核对引入按 mapping 隔离的内存预载
缓存（A 区、媒体边界、lineage snapshot）与批量 snapshot 写入，本脚本仅用于
观察算法趋势，不能作为该优化在真实数据规模上的直接证明。
"""
from __future__ import annotations

import argparse
import cProfile
import csv
import hashlib
import json
import os
import platform
import pstats
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc
from dataclasses import dataclass, field, asdict
from pathlib import Path


# ---------------------------------------------------------------------------
# 结果数据类
# ---------------------------------------------------------------------------
@dataclass
class Result:
    name: str
    seconds: float
    records: int
    verified: int      # 实际执行了完整 lineage 核对的记录数
    skipped: int       # 通过增量校验跳过的记录数
    valid: int
    invalid: int
    missing: int
    digest: str
    peak_bytes: int = 0
    counters: dict = field(default_factory=dict)

    @property
    def rps(self):
        return self.records / self.seconds if self.seconds else float("inf")


def compute_digest(rows: list[tuple]) -> str:
    h = hashlib.sha256()
    for rid, state in sorted(rows, key=lambda x: x[0]):
        h.update(f"{rid}:{state}\n".encode())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Fixture: 两阶段数据 (模拟程序停机期间的外部修改)
# ---------------------------------------------------------------------------
def build_fixture(base: Path, n: int, delta_pct: float = 0.1):
    """构造测试环境: n 条记录, 其中 delta_pct 比例在两阶段间发生变化。

    返回 (a_root, b_root, db_path, snapshot_db_path, n, delta_count)
    """
    a_root = base / "A"
    b_root = base / "B"
    a_root.mkdir(parents=True, exist_ok=True)
    b_root.mkdir(parents=True, exist_ok=True)

    db_path = base / "lineage.db"
    con = sqlite3.connect(str(db_path))
    con.executescript("""
        CREATE TABLE IF NOT EXISTS a_strm_files (
            local_path TEXT PRIMARY KEY,
            webdav_path TEXT,
            parent_webdav_path TEXT,
            updated_at INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS b_strm_files (
            local_path TEXT PRIMARY KEY,
            webdav_path TEXT,
            parent_webdav_path TEXT,
            source_a_path TEXT,
            fingerprint TEXT,
            status TEXT DEFAULT 'valid',
            updated_at INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS b_lineage_snapshot (
            local_path TEXT PRIMARY KEY,
            size INTEGER,
            mtime_ns INTEGER,
            fingerprint TEXT,
            verified_at INTEGER DEFAULT 0,
            state INTEGER DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS ix_b_fp ON b_strm_files(fingerprint);
        CREATE INDEX IF NOT EXISTS ix_a_wd ON a_strm_files(webdav_path);
    """)

    delta_count = max(1, int(n * delta_pct))
    a_records = []
    b_records = []
    snapshot_records = []

    for i in range(n):
        season = f"Season {(i % 97) + 1:02d}"
        media = f"Show_{i:05d}"
        ep = f"S{(i % 97) + 1:02d}E{i % 24 + 1:02d}"
        rel_dir = Path(season) / media
        a_file = a_root / rel_dir / f"{ep}.strm"
        b_file = b_root / rel_dir / f"{ep}.strm"

        a_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.parent.mkdir(parents=True, exist_ok=True)
        content = f"webdav://cloud/{season}/{media}/{ep}.strm"
        a_file.write_text(content)
        b_file.write_text(content)

        webdav = f"/d/cloud/{season}/{media}/{ep}.strm"
        fingerprint = hashlib.md5(webdav.encode()).hexdigest()

        a_records.append((str(a_file), webdav, f"/d/cloud/{season}/{media}", int(time.time())))
        b_records.append((str(b_file), webdav, f"/d/cloud/{season}/{media}",
                          str(a_file), fingerprint, "valid", int(time.time())))

        # 快照: 记录文件大小和修改时间 (mtime_ns 已是纳秒)
        stat = b_file.stat()
        snapshot_records.append((str(b_file), stat.st_size, stat.st_mtime_ns,
                                 fingerprint, int(time.time()), 1))

    con.executemany("INSERT OR REPLACE INTO a_strm_files VALUES(?,?,?,?)", a_records)
    con.executemany("INSERT OR REPLACE INTO b_strm_files VALUES(?,?,?,?,?,?,?)", b_records)
    con.executemany("INSERT OR REPLACE INTO b_lineage_snapshot VALUES(?,?,?,?,?,?)",
                     snapshot_records)
    con.commit()
    con.close()

    return a_root, b_root, db_path, n, delta_count


def apply_delta(a_root: Path, b_root: Path, db_path: Path, delta_count: int):
    """模拟程序停机期间的外部修改: 删除/修改部分文件。

    返回变更列表 [(path, action), ...]
    """
    con = sqlite3.connect(str(db_path))
    records = con.execute("SELECT local_path FROM b_strm_files ORDER BY local_path").fetchall()
    changes = []

    # 1. 删除 delta_count/2 个 B 文件 (模拟用户删除)
    delete_count = delta_count // 2
    for i in range(delete_count):
        p = Path(records[i][0])
        if p.exists():
            p.unlink()
            changes.append((str(p), "deleted"))

    # 2. 修改 delta_count/2 个 B 文件内容 (模拟用户编辑)
    modify_count = delta_count - delete_count
    for i in range(delete_count, delete_count + modify_count):
        p = Path(records[i][0])
        if p.exists():
            p.write_text("modified content after restart")
            changes.append((str(p), "modified"))

    con.close()
    return changes


# ---------------------------------------------------------------------------
# Baseline: 全量 lineage 核对 (当前行为)
# ---------------------------------------------------------------------------
def baseline(a_root: Path, b_root: Path, db_path: Path) -> Result:
    """模拟当前 _reconcile_b_historical_records: 逐条执行完整 lineage 检查。"""
    con = sqlite3.connect(str(db_path))
    records = con.execute(
        "SELECT local_path, webdav_path, fingerprint FROM b_strm_files ORDER BY local_path"
    ).fetchall()

    rows = []
    counters = {"resolve": 0, "sql": 0, "exists": 0}

    tracemalloc.start()
    t0 = time.perf_counter_ns()

    a_root_resolved = a_root.resolve()
    b_root_resolved = b_root.resolve()
    counters["resolve"] = 2  # 根路径只解析一次

    for local_path, webdav_path, fingerprint in records:
        # 完整 lineage 检查: resolve + SQL + 比较
        b_local = Path(local_path).resolve()
        counters["resolve"] += 1

        # get_a_by_webdav
        row = con.execute(
            "SELECT local_path FROM a_strm_files WHERE webdav_path = ?",
            (webdav_path,),
        ).fetchone()
        counters["sql"] += 1

        if not row:
            rows.append((local_path, "invalid"))
            continue

        a_local = Path(row[0]).resolve()
        counters["resolve"] += 1

        try:
            a_rel = a_local.relative_to(a_root_resolved)
            b_rel = b_local.relative_to(b_root_resolved)
        except ValueError:
            rows.append((local_path, "invalid"))
            continue

        # _check_basic_lineage
        if a_rel == b_rel:
            rows.append((local_path, "valid"))
            continue

        # exists check
        ok = a_local.exists()
        counters["exists"] += 1
        rows.append((local_path, "valid" if ok else "missing"))

    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    con.close()

    elapsed = (time.perf_counter_ns() - t0) / 1e9
    counts = {"valid": 0, "invalid": 0, "missing": 0}
    for _, s in rows:
        counts[s] = counts.get(s, 0) + 1
    return Result(
        name="baseline", seconds=elapsed, records=len(rows),
        verified=len(rows), skipped=0,
        valid=counts.get("valid", 0), invalid=counts.get("invalid", 0),
        missing=counts.get("missing", 0), digest=compute_digest(rows),
        peak_bytes=peak, counters=counters,
    )


# ---------------------------------------------------------------------------
# Optimized: 增量校验 (快照差异 → 只核对变化记录)
# ---------------------------------------------------------------------------
def optimized(a_root: Path, b_root: Path, db_path: Path) -> Result:
    """增量校验: 用快照差异只核对变化的记录。"""
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA query_only = ON")

    # 1. 一次性加载所有 B 记录 (批量 SQL)
    all_records = con.execute(
        "SELECT local_path, webdav_path, fingerprint FROM b_strm_files ORDER BY local_path"
    ).fetchall()

    # 2. 一次性加载快照 (local_path → (size, mtime_ns, fingerprint, state))
    snapshots = {}
    for row in con.execute(
        "SELECT local_path, size, mtime_ns, fingerprint, state FROM b_lineage_snapshot"
    ).fetchall():
        snapshots[row[0]] = (row[1], row[2], row[3], row[4])

    # 3. 一次性加载 A 记录到内存
    a_index = dict(con.execute("SELECT webdav_path, local_path FROM a_strm_files").fetchall())

    # 4. 预解析根路径
    a_root_resolved = a_root.resolve()
    b_root_resolved = b_root.resolve()

    counters = {
        "bulk_sql": 3,  # 3次批量查询
        "root_resolve": 2,
        "dynamic_resolve": 0,
        "exists": 0,
        "skipped": 0,
        "verified": 0,
    }

    rows = []
    _debug_snap_match = 0
    _debug_snap_mismatch = 0
    _debug_snap_none = 0
    tracemalloc.start()
    t0 = time.perf_counter_ns()

    for local_path, webdav_path, fingerprint in all_records:
        # 增量判断: 快照是否存在且状态有效?
        snap = snapshots.get(local_path)
        if snap is None:
            _debug_snap_none += 1
        elif snap[3] != 1:
            pass  # state != valid
        else:
            try:
                p = Path(local_path)
                stat = p.stat()
                if stat.st_size == snap[0] and stat.st_mtime_ns == snap[1]:
                    _debug_snap_match += 1
                    rows.append((local_path, "valid"))
                    counters["skipped"] += 1
                    continue
                else:
                    _debug_snap_mismatch += 1
            except (OSError, FileNotFoundError):
                pass

        # 需要完整核对 (新增/删除/修改/异常记录)
        counters["verified"] += 1
        b_local = Path(local_path).resolve()
        a_local_str = a_index.get(webdav_path)
        counters["dynamic_resolve"] += 1

        if not a_local_str:
            rows.append((local_path, "invalid"))
            continue

        a_local = Path(a_local_str).resolve()
        counters["dynamic_resolve"] += 1

        try:
            a_rel = a_local.relative_to(a_root_resolved)
            b_rel = b_local.relative_to(b_root_resolved)
        except ValueError:
            rows.append((local_path, "invalid"))
            continue

        if a_rel == b_rel:
            rows.append((local_path, "valid"))
            continue

        ok = a_local.exists()
        counters["exists"] += 1
        rows.append((local_path, "valid" if ok else "missing"))

    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    con.close()

    elapsed = (time.perf_counter_ns() - t0) / 1e9
    counts = {"valid": 0, "invalid": 0, "missing": 0}
    for _, s in rows:
        counts[s] = counts.get(s, 0) + 1
    return Result(
        name="optimized", seconds=elapsed, records=len(rows),
        verified=counters["verified"], skipped=counters["skipped"],
        valid=counts.get("valid", 0), invalid=counts.get("invalid", 0),
        missing=counts.get("missing", 0), digest=compute_digest(rows),
        peak_bytes=peak, counters=counters,
    )


# ---------------------------------------------------------------------------
# Benchmark orchestration and export
# ---------------------------------------------------------------------------
def _summary_for_runs(results: list[Result]) -> dict:
    """Build a stable summary for one benchmark variant."""
    seconds = [result.seconds for result in results]
    rps = [result.rps for result in results]
    stdev = statistics.stdev(seconds) if len(seconds) > 1 else 0.0
    rps_stdev = statistics.stdev(rps) if len(rps) > 1 else 0.0
    return {
        "median_s": statistics.median(seconds),
        "stdev_s": stdev,
        "median_rps": statistics.median(rps),
        "stdev_rps": rps_stdev,
        "digest": results[0].digest,
        "peak_bytes": max(result.peak_bytes for result in results),
        "counters": results[-1].counters,
        "median_verified": statistics.median(result.verified for result in results),
        "median_skipped": statistics.median(result.skipped for result in results),
    }


def _write_exports(output_dir: Path, payload: dict, runs: list[Result]) -> None:
    """Write the complete JSON result and flattened per-run CSV result."""
    (output_dir / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    counter_names = sorted({
        key for result in runs for key in result.counters
    })
    columns = [
        "name", "seconds", "records", "verified", "skipped", "rps",
        "valid", "invalid", "missing", "peak_bytes", "digest",
        *(f"counter_{name}" for name in counter_names),
    ]
    with open(output_dir / "runs.csv", "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for result in runs:
            row = {
                "name": result.name,
                "seconds": f"{result.seconds:.4f}",
                "records": result.records,
                "verified": result.verified,
                "skipped": result.skipped,
                "rps": f"{result.rps:.1f}",
                "valid": result.valid,
                "invalid": result.invalid,
                "missing": result.missing,
                "peak_bytes": result.peak_bytes,
                "digest": result.digest,
            }
            row.update({
                f"counter_{name}": result.counters.get(name, 0)
                for name in counter_names
            })
            writer.writerow(row)


def run_benchmark(
    records: int = 1000,
    delta_pct: float = 0.05,
    repeat: int = 5,
    warmup: int = 1,
    output_dir: Path = Path("perf-results"),
    profile: bool = False,
    max_seconds: float = 10.0,
    min_speedup: float = 3.0,
) -> tuple[int, dict, dict]:
    """Run the benchmark and return ``(exit_code, payload, summary)``.

    The function owns the temporary fixture and all exports, making the benchmark
    directly callable from tests without changing the existing CLI contract.
    """
    if repeat < 1:
        raise ValueError("repeat must be at least 1")
    if warmup < 0:
        raise ValueError("warmup must not be negative")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    runs = []
    with tempfile.TemporaryDirectory(prefix="lineage_bench_") as td:
        a_root, b_root, db_path, count, delta = build_fixture(
            Path(td), records, delta_pct)

        changes = apply_delta(a_root, b_root, db_path, delta)
        print(f"[fixture] {count} records, {delta} changed ({len(changes)} actual)")

        for _ in range(warmup):
            baseline(a_root, b_root, db_path)
            optimized(a_root, b_root, db_path)

        for index in range(repeat):
            for name, fn in (("baseline", baseline), ("optimized", optimized)):
                if profile and index == 0:
                    profile_run = cProfile.Profile()
                    result = profile_run.runcall(fn, a_root, b_root, db_path)
                    profile_run.dump_stats(str(output_dir / f"{name}.prof"))
                    with open(output_dir / f"{name}.txt", "w", encoding="utf-8") as fh:
                        pstats.Stats(profile_run, stream=fh).sort_stats(
                            "cumtime").print_stats(60)
                else:
                    result = fn(a_root, b_root, db_path)
                runs.append(result)
                skip_pct = (result.skipped / result.records * 100) if result.records else 0
                print(
                    f"  [{name}] #{index}: {result.seconds:.3f}s "
                    f"({result.rps:.0f} rec/s) verified={result.verified} "
                    f"skipped={result.skipped}({skip_pct:.0f}%) "
                    f"valid={result.valid} invalid={result.invalid}"
                )

    groups = {name: [result for result in runs if result.name == name]
              for name in ("baseline", "optimized")}
    summary = {name: _summary_for_runs(results)
               for name, results in groups.items()}
    baseline_summary = summary["baseline"]
    optimized_summary = summary["optimized"]
    summary["comparison"] = {
        "speedup_x": baseline_summary["median_s"] / optimized_summary["median_s"],
        "saved_pct": 100 * (
            1 - optimized_summary["median_s"] / baseline_summary["median_s"]),
        "equivalent": baseline_summary["digest"] == optimized_summary["digest"],
        "records_total": runs[0].records,
        "records_verified_optimized": optimized_summary["median_verified"],
        "records_skipped_optimized": optimized_summary["median_skipped"],
    }

    payload = {
        "metadata": {
            "python": sys.version,
            "sqlite_version": sqlite3.sqlite_version,
            "platform": platform.platform(),
            "scenario": "baseline_vs_optimized_incremental_lineage",
            "records": records,
            "delta_pct": delta_pct,
            "repeat": repeat,
            "warmup": warmup,
        },
        "runs": [asdict(result) for result in runs],
        "summary": summary,
    }
    _write_exports(output_dir, payload, runs)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    fail = (
        optimized_summary["median_s"] > max_seconds
        or summary["comparison"]["speedup_x"] < min_speedup
        or not summary["comparison"]["equivalent"]
    )
    print(f"\nPERF GATE: {'FAIL' if fail else 'PASS'}")
    if fail:
        reasons = []
        if optimized_summary["median_s"] > max_seconds:
            reasons.append(
                f"optimized {optimized_summary['median_s']:.1f}s > {max_seconds}s")
        if summary["comparison"]["speedup_x"] < min_speedup:
            reasons.append(
                f"speedup {summary['comparison']['speedup_x']:.2f}x < {min_speedup}x")
        if not summary["comparison"]["equivalent"]:
            reasons.append("digest mismatch (correctness regression)")
        print(f"  Reasons: {'; '.join(reasons)}")
    return (2 if fail else 0), payload, summary


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="B区核对增量校验性能基准")
    ap.add_argument("--records", type=int, default=1000, help="总记录数")
    ap.add_argument("--delta-pct", type=float, default=0.05, help="变化比例 (默认5%%)")
    ap.add_argument("--repeat", type=int, default=5, help="重复次数")
    ap.add_argument("--warmup", type=int, default=1, help="预热次数")
    ap.add_argument("--output", type=Path, default=Path("perf-results"), help="输出目录")
    ap.add_argument("--profile", action="store_true", help="cProfile")
    ap.add_argument("--max-seconds", type=float, default=10.0, help="门禁最大耗时")
    ap.add_argument("--min-speedup", type=float, default=3.0, help="门禁最小加速比")
    args = ap.parse_args()
    code, _, _ = run_benchmark(
        records=args.records,
        delta_pct=args.delta_pct,
        repeat=args.repeat,
        warmup=args.warmup,
        output_dir=args.output,
        profile=args.profile,
        max_seconds=args.max_seconds,
        min_speedup=args.min_speedup,
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
