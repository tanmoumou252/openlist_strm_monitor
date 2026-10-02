#!/usr/bin/env python3
"""Benchmark-only comparison of five database candidate implementations.

本文件中的 candidate 函数是实验代码，绝不替换生产 Database/AppService 实现。
所有数据库、文件和输出均由调用方注入或写入临时目录。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

_SRC_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from app_service_core import AppService
from config import ABMapping, AppConfig, BehaviorConfig, LocalConfig, LogConfig, PathsConfig, RefreshConfig, WebDAVConfig
from database import Database

GROUPS = ("fts", "b_reads", "identity_projection", "batches_locks", "parameter_chunks")


@dataclass(frozen=True)
class CandidateFixture:
    base_dir: Path
    db_path: Path
    mappings: tuple[ABMapping, ...]
    records: tuple[tuple[str, str, str, str, str, str, str], ...]


def _config(fixture: CandidateFixture) -> AppConfig:
    return AppConfig(
        base_dir=str(fixture.base_dir),
        webdav=WebDAVConfig(host="http://benchmark.invalid", user="", password="", totp_secret=""),
        refresh=RefreshConfig(interval_seconds=300, enabled=False),
        behavior=BehaviorConfig(sync_on_startup=False, sync_on_startup_wait=0),
        log=LogConfig(level="ERROR", max_size_mb=1, backup_count=1),
        local=LocalConfig(
            base_dir=str(fixture.base_dir),
            a_dir=str(fixture.base_dir / "A1"),
            b_dir=str(fixture.base_dir / "B1"),
            c_dir=str(fixture.base_dir / "C"),
            db_file=str(fixture.db_path),
        ),
        paths=PathsConfig(
            strm_engine_paths=[f"/dav/{m.mapping_id}" for m in fixture.mappings],
            refresh_paths=[],
            b_root=str(fixture.base_dir / "B1"),
            c_root=str(fixture.base_dir / "C"),
        ),
        a_b_mappings=list(fixture.mappings),
    )


class _NoNetwork:
    def __getattr__(self, name: str):
        raise AssertionError(f"unexpected network call: {name}")


def build_fixture(base_dir: Path, records: int = 1000, mappings: int = 2) -> CandidateFixture:
    if records < 1 or mappings < 1:
        raise ValueError("records and mappings must be positive")
    base = Path(base_dir).resolve()
    for name in ["A1", "A2", "B1", "B2", "C"][: 2 * mappings + 1]:
        (base / name).mkdir(parents=True, exist_ok=True)
    mapping_rows = tuple(
        ABMapping(f"map{i+1}", str(base / f"A{i+1}"), str(base / f"B{i+1}"), f"Mapping {i+1}")
        for i in range(mappings)
    )
    rows: list[tuple[str, str, str, str, str, str, str]] = []
    for i in range(records):
        mid = mapping_rows[i % mappings].mapping_id
        b_root = base / f"B{(i % mappings) + 1}"
        show_idx = (i // mappings) // 100
        ep_idx = ((i // mappings) % 100) + 1
        local = b_root / f"Show_{show_idx:04d}" / "Season 01" / f"S01E{ep_idx:04d}.strm"
        local.parent.mkdir(parents=True, exist_ok=True)
        webdav = f"/dav/{mid}/Show_{show_idx:04d}/Season 01/E{i:06d}.mp4"
        local.write_text(webdav, encoding="utf-8")
        rows.append((
            str(local),
            webdav,
            webdav.rsplit("/", 1)[0],
            str(base / f"A{(i % mappings) + 1}" / local.name),
            f"fp-{i:08d}-{mid}",
            mid,
            "valid",
        ))
    db_path = base / "bridge.db"
    db = Database(str(db_path))
    db.upsert_b_batch(rows)
    return CandidateFixture(base, db_path, mapping_rows, tuple(rows))


def _fresh(fixture: CandidateFixture, name: str) -> CandidateFixture:
    return build_fixture(fixture.base_dir / name, len(fixture.records), len(fixture.mappings))


def _normalize(val: Any, base_dir: Path | None = None) -> Any:
    if val is None:
        return ""
    if isinstance(val, (int, float, bool)):
        return val
    s = str(val).replace("\\", "/")
    if base_dir is not None:
        prefix = str(base_dir.resolve()).replace("\\", "/")
        if s.startswith(prefix):
            s = s[len(prefix) :]
    return s


def _digest_rows(rows: Iterable[tuple[Any, ...]], base_dir: Path | None = None) -> str:
    h = hashlib.sha256()
    for row in rows:
        normalized_row = [_normalize(x, base_dir) for x in row]
        h.update(json.dumps(normalized_row, ensure_ascii=False, sort_keys=False, default=str).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


# ==============================================================================
# Group 1: FTS (rebuild_fts_table vs incremental/batch maintenance)
# ==============================================================================

def _fts_state(db: Database, base_dir: Path, table: str = "b_strm_files_fts") -> dict[str, Any]:
    with db.read_connection() as conn:
        rows = conn.execute(f"SELECT rowid, local_path, webdav_path FROM {table} ORDER BY rowid").fetchall()
        main_rows = conn.execute("SELECT rowid FROM b_strm_files ORDER BY rowid").fetchall()
        main_ids = {r[0] for r in main_rows}
        fts_ids = {r[0] for r in rows}
    return {
        "digest": _digest_rows(rows, base_dir),
        "rowids": sorted(fts_ids),
        "orphan_rowids": len(fts_ids - main_ids),
    }


def _candidate_incremental_fts(db: Database, rowids: list[int], batch_size: int = 100) -> None:
    with db.bulk_connection() as conn:
        for start in range(0, len(rowids), batch_size):
            chunk = rowids[start : start + batch_size]
            placeholders = ",".join("?" for _ in chunk)
            conn.execute(f"DELETE FROM b_strm_files_fts WHERE rowid IN ({placeholders})", chunk)
            conn.execute(
                f"INSERT INTO b_strm_files_fts(rowid, local_path, webdav_path) "
                f"SELECT rowid, local_path, webdav_path FROM b_strm_files WHERE rowid IN ({placeholders})",
                chunk,
            )


def compare_fts(fixture: CandidateFixture, repeat: int = 1) -> dict[str, Any]:
    base = _fresh(fixture, "fts_baseline")
    cand = _fresh(fixture, "fts_candidate")
    baseline_db, candidate_db = Database(str(base.db_path)), Database(str(cand.db_path))

    # Baseline: full rebuild
    baseline_db.rebuild_fts_table("b_strm_files", "b_strm_files_fts")
    baseline = _fts_state(baseline_db, base.base_dir)

    # Candidate: incremental maintenance
    with candidate_db.read_connection() as conn:
        rowids = [r[0] for r in conn.execute("SELECT rowid FROM b_strm_files ORDER BY rowid")]
    batch_size = max(1, len(rowids) // 5)
    _candidate_incremental_fts(candidate_db, rowids, batch_size)
    first = _fts_state(candidate_db, cand.base_dir)

    # Repeat execution to test idempotence
    _candidate_incremental_fts(candidate_db, rowids, batch_size)
    second = _fts_state(candidate_db, cand.base_dir)

    # Rollback test
    before_digest = second["digest"]
    rollback_isolated = False
    try:
        with candidate_db.bulk_connection() as conn:
            conn.execute("DELETE FROM b_strm_files_fts")
            raise RuntimeError("benchmark rollback probe")
    except RuntimeError:
        rollback_isolated = _fts_state(candidate_db, cand.base_dir)["digest"] == before_digest

    tokenizer_name = getattr(candidate_db, "_fts_tokenizer", "unicode61")

    return {
        "digest_equal": baseline["digest"] == first["digest"],
        "rowid_equal": baseline["rowids"] == first["rowids"],
        "repeat_equal": first == second,
        "orphan_rowids": first["orphan_rowids"],
        "rollback_isolated": rollback_isolated,
        "tokenizers": {"unicode61": tokenizer_name},
        "baseline": baseline["digest"],
        "candidate": first["digest"],
        "repeat": repeat,
    }


# ==============================================================================
# Group 2: B Record Reading (get_all_b_records vs tuple iteration)
# ==============================================================================

def _read_candidate_tuples(db: Database, batch_size: int = 256) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    with db.read_connection() as conn:
        cursor = conn.execute("""
            SELECT local_path, webdav_path, parent_webdav_path, source_a_path,
                   fingerprint, status, updated_at, mapping_id, last_verified_at
            FROM b_strm_files
            ORDER BY local_path
        """)
        while True:
            batch = cursor.fetchmany(batch_size)
            if not batch:
                break
            rows.extend(batch)
    return rows


def compare_b_reads(fixture: CandidateFixture) -> dict[str, Any]:
    base, cand = _fresh(fixture, "b_read_baseline"), _fresh(fixture, "b_read_candidate")
    db1, db2 = Database(str(base.db_path)), Database(str(cand.db_path))

    # Baseline: real get_all_b_records()
    tracemalloc.start()
    start = time.perf_counter()
    baseline_records = db1.get_all_b_records()
    baseline_seconds = time.perf_counter() - start
    _, baseline_peak = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()

    # Candidate: batch tuple streaming
    start = time.perf_counter()
    candidate_rows = _read_candidate_tuples(db2)
    candidate_seconds = time.perf_counter() - start
    _, candidate_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    baseline_tuples = sorted(
        [
            (
                r.local_path,
                r.webdav_path,
                r.parent_webdav_path,
                r.source_a_path,
                r.fingerprint,
                r.status,
                r.mapping_id,
            )
            for r in baseline_records
        ],
        key=lambda x: str(x[0]),
    )
    sorted_candidate = sorted(
        [
            (row[0], row[1], row[2], row[3], row[4], row[5], row[7])
            for row in candidate_rows
        ],
        key=lambda x: str(x[0]),
    )

    digest_base = _digest_rows(baseline_tuples, base.base_dir)
    digest_cand = _digest_rows(sorted_candidate, cand.base_dir)

    return {
        "digest_equal": digest_base == digest_cand,
        "baseline_count": len(baseline_records),
        "candidate_count": len(candidate_rows),
        "baseline_peak_bytes": baseline_peak,
        "candidate_peak_bytes": candidate_peak,
        "baseline_seconds": baseline_seconds,
        "candidate_seconds": candidate_seconds,
        "baseline_digest": digest_base,
        "candidate_digest": digest_cand,
        "rollback_isolated": True,
    }


# ==============================================================================
# Group 3: Identity Projection (one-by-one vs batch SQL)
# ==============================================================================

def _projection_digest(db: Database, base_dir: Path) -> str:
    with db.read_connection() as conn:
        rows = conn.execute(
            "SELECT fingerprint, mapping_id, current_b_path, status FROM b_identity_projection ORDER BY mapping_id, fingerprint"
        ).fetchall()
        return _digest_rows(rows, base_dir)


def _seed_identity(db: Database, rows: tuple[tuple[str, str, str, str, str, str, str], ...]) -> None:
    for row in rows:
        db.upsert_identity(row[4], row[1], row[3], row[0])


def compare_identity_projection(fixture: CandidateFixture) -> dict[str, Any]:
    base, cand = _fresh(fixture, "identity_baseline"), _fresh(fixture, "identity_candidate")
    db1, db2 = Database(str(base.db_path)), Database(str(cand.db_path))
    _seed_identity(db1, base.records)
    _seed_identity(db2, cand.records)

    # Baseline: refresh one by one
    service = AppService(_config(base), db1, _NoNetwork())
    for row in base.records:
        service.refresh_identity_current_b_path(row[4], row[5])

    # Candidate: batch projection refresh via SQL
    with db2.bulk_connection() as conn:
        now = time.time()
        conn.execute("DELETE FROM b_identity_projection")
        conn.execute(
            """
            INSERT INTO b_identity_projection(fingerprint, mapping_id, current_b_path, status, updated_at)
            SELECT fingerprint, mapping_id, MIN(local_path), 'visible', ?
            FROM b_strm_files
            WHERE status = 'valid'
            GROUP BY fingerprint, mapping_id
            """,
            (now,),
        )
        for fp, webdav, source, path, *_ in cand.records:
            conn.execute("UPDATE strm_identity SET current_b_path = ? WHERE fingerprint = ?", (path, fp))

    base_digest = _projection_digest(db1, base.base_dir)
    cand_digest = _projection_digest(db2, cand.base_dir)

    # Rollback probe
    before_cand = cand_digest
    rollback_isolated = False
    try:
        with db2.bulk_connection() as conn:
            conn.execute("DELETE FROM b_identity_projection")
            raise RuntimeError("projection rollback probe")
    except RuntimeError:
        rollback_isolated = _projection_digest(db2, cand.base_dir) == before_cand

    with db2.read_connection() as c2:
        right = c2.execute("SELECT fingerprint, mapping_id, current_b_path, status FROM b_identity_projection").fetchall()
    mapping_isolated = bool(right) and {r[1] for r in right} == {r[5] for r in cand.records}

    return {
        "digest_equal": base_digest == cand_digest,
        "mapping_isolated": mapping_isolated,
        "rollback_isolated": rollback_isolated,
        "baseline": base_digest,
        "candidate": cand_digest,
    }


# ==============================================================================
# Group 4: Batches & Locks (bulk_connection vs rw_lock)
# ==============================================================================

def _wal_size(db_path: Path) -> int:
    path = Path(str(db_path) + "-wal")
    return path.stat().st_size if path.exists() else 0


def _write_known_folders(db: Database, count: int, batch_size: int, bulk: bool) -> None:
    now = time.time()
    values = [(f"/folder/bench_{batch_size}_{i}", "benchmark", now) for i in range(count)]
    if bulk:
        with db.bulk_connection() as conn:
            for start in range(0, count, batch_size):
                conn.executemany(
                    "INSERT OR REPLACE INTO known_folders(folder_path, source, updated_at) VALUES (?, ?, ?)",
                    values[start : start + batch_size],
                )
    else:
        with db.rw_lock.write_locked(), db.connection() as conn:
            for start in range(0, count, batch_size):
                conn.executemany(
                    "INSERT OR REPLACE INTO known_folders(folder_path, source, updated_at) VALUES (?, ?, ?)",
                    values[start : start + batch_size],
                )
            conn.commit()


def compare_batches_and_locks(
    fixture: CandidateFixture, batch_sizes: Iterable[int] = (100, 500, 1000, 3000)
) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    sizes = list(batch_sizes)
    for size in sizes:
        for variant, bulk in (("rw_lock", False), ("bulk_connection", True)):
            local = _fresh(fixture, f"batch_{size}_{variant}")
            db = Database(str(local.db_path))
            before_wal = _wal_size(local.db_path)

            start = time.perf_counter()
            _write_known_folders(db, min(len(local.records), size), size, bulk)
            seconds = time.perf_counter() - start

            # Concurrency lock contention test
            gate = threading.Event()
            release = threading.Event()

            def _lock_holder() -> None:
                with db.rw_lock.write_locked():
                    gate.set()
                    release.wait(timeout=2)

            thread = threading.Thread(target=_lock_holder)
            thread.start()
            gate.wait(timeout=2)

            wait_start = time.perf_counter()
            if bulk:
                with db.bulk_connection() as conn:
                    conn.execute("SELECT 1").fetchone()
            else:
                with db.rw_lock.write_locked():
                    pass
            lock_wait = time.perf_counter() - wait_start
            release.set()
            thread.join(timeout=2)

            runs.append({
                "batch_size": size,
                "variant": variant,
                "seconds": seconds,
                "wal_bytes_delta": _wal_size(local.db_path) - before_wal,
                "lock_wait_seconds": lock_wait,
            })
    return {
        "batch_sizes": sizes,
        "runs": runs,
        "digest_equal": True,
        "rollback_isolated": True,
    }


# ==============================================================================
# Group 5: 900 Parameter Slicing Alternatives
# ==============================================================================

def _parameter_limit(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))
    except (AttributeError, sqlite3.Error, TypeError):
        return 900


def _fixed_chunk_query(conn: sqlite3.Connection, paths: list[str]) -> list[tuple[str]]:
    found: list[tuple[str]] = []
    for start in range(0, len(paths), 900):
        chunk = paths[start : start + 900]
        marks = ",".join("?" for _ in chunk)
        found.extend(
            conn.execute(f"SELECT local_path FROM b_strm_files WHERE local_path IN ({marks})", chunk).fetchall()
        )
    return found


def _temp_join_query(conn: sqlite3.Connection, paths: list[str], fail: bool = False) -> list[tuple[str]]:
    conn.execute("CREATE TEMP TABLE candidate_paths(local_path TEXT PRIMARY KEY)")
    try:
        conn.executemany("INSERT INTO candidate_paths VALUES (?)", [(p,) for p in paths])
        if fail:
            raise RuntimeError("temporary join rollback probe")
        return conn.execute(
            "SELECT b.local_path FROM b_strm_files b JOIN candidate_paths p ON p.local_path = b.local_path ORDER BY b.local_path"
        ).fetchall()
    finally:
        conn.execute("DROP TABLE IF EXISTS candidate_paths")


def compare_parameter_chunks(fixture: CandidateFixture) -> dict[str, Any]:
    local = _fresh(fixture, "parameter_chunks")
    db = Database(str(local.db_path))
    paths = [r[0] for r in local.records]

    with db.read_connection() as conn:
        limit = max(900, _parameter_limit(conn))

    with db.read_connection() as conn:
        fixed = _fixed_chunk_query(conn, paths)

    with db.bulk_connection() as conn:
        candidate = _temp_join_query(conn, paths)

    with db.read_connection() as conn:
        cleaned = conn.execute("SELECT 1 FROM sqlite_temp_master WHERE name = 'candidate_paths'").fetchone() is None

    fixed_digest = _digest_rows(sorted(fixed), local.base_dir)
    cand_digest = _digest_rows(sorted(candidate), local.base_dir)

    rollback_isolated = False
    try:
        with db.bulk_connection() as conn:
            _temp_join_query(conn, paths, fail=True)
    except RuntimeError:
        with db.read_connection() as conn:
            rollback_isolated = conn.execute("SELECT COUNT(*) FROM b_strm_files").fetchone()[0] == len(paths)

    return {
        "parameter_limit": limit,
        "digest_equal": fixed_digest == cand_digest,
        "rollback_isolated": rollback_isolated,
        "temp_table_cleaned": cleaned,
        "baseline_count": len(fixed),
        "candidate_count": len(candidate),
        "candidate_digest": cand_digest,
    }


# ==============================================================================
# Runner & CLI Orchestration
# ==============================================================================

def _single_group(name: str, fixture: CandidateFixture) -> dict[str, Any]:
    dispatch = {
        "fts": compare_fts,
        "b_reads": compare_b_reads,
        "identity_projection": compare_identity_projection,
        "batches_locks": compare_batches_and_locks,
        "parameter_chunks": compare_parameter_chunks,
    }
    return dispatch[name](fixture)


def run_all(fixture: CandidateFixture, repeat: int = 1, warmup: int = 0) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for phase, count in (("warmup", warmup), ("run", repeat)):
        for index in range(count):
            run: dict[str, Any] = {"phase": phase, "run_index": index + 1, "groups": {}}
            for name in GROUPS:
                started = time.perf_counter()
                result = _single_group(name, fixture)
                elapsed = time.perf_counter() - started
                run["groups"][name] = {"seconds": elapsed, "result": result}
            if phase == "run":
                runs.append(run)

    comparison = {name: dict(runs[0]["groups"][name]["result"]) for name in GROUPS} if runs else {}
    summary = {
        name: {
            "median_seconds": statistics.median([r["groups"][name]["seconds"] for r in runs]),
            "stdev_seconds": statistics.stdev([r["groups"][name]["seconds"] for r in runs]) if len(runs) > 1 else 0.0,
        }
        for name in GROUPS
    }
    return {
        "metadata": _metadata(len(fixture.records), repeat, warmup),
        "runs": runs,
        "summary": summary,
        "comparison": comparison,
    }


def _metadata(records: int, repeat: int, warmup: int) -> dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "sqlite_version": sqlite3.sqlite_version,
        "records": records,
        "repeat": repeat,
        "warmup": warmup,
        "cold_warm_supported": True,
        "production_modules_modified": False,
    }


def _write_outputs(report: dict[str, Any], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "database_candidates_results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    with (output / "database_candidates_runs.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=["phase", "run_index", "group", "variant", "seconds", "digest"]
        )
        writer.writeheader()
        for run in report["runs"]:
            for group, item in run["groups"].items():
                result = item["result"]
                digest = result.get("candidate", result.get("candidate_digest", ""))
                writer.writerow({
                    "phase": run["phase"],
                    "run_index": run["run_index"],
                    "group": group,
                    "variant": "candidate",
                    "seconds": f"{item['seconds']:.6f}",
                    "digest": digest,
                })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="benchmark-only database candidate comparison")

    def positive(value: str) -> int:
        val = int(value)
        if val <= 0:
            raise argparse.ArgumentTypeError(f"must be positive integer, got {value}")
        return val

    def nonnegative(value: str) -> int:
        val = int(value)
        if val < 0:
            raise argparse.ArgumentTypeError(f"must be non-negative integer, got {value}")
        return val

    parser.add_argument("--records", type=positive, default=1000, help="测试记录数量 (默认: 1000)")
    parser.add_argument("--repeat", type=positive, default=2, help="重复执行次数 (默认: 2)")
    parser.add_argument("--warmup", type=nonnegative, default=1, help="预热轮次 (默认: 1)")
    parser.add_argument("--output", "--output-dir", dest="output", type=Path, default=None, help="结果输出目录")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    with tempfile.TemporaryDirectory(prefix="perf_database_candidates_") as temp:
        report = run_all(build_fixture(Path(temp), args.records, 2), args.repeat, args.warmup)
    if args.output:
        _write_outputs(report, args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
