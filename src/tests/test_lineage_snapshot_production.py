"""真实 AppService 的 mapping-scoped lineage snapshot 验收测试。"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from app_service_core import AppService
from config import (
    ABMapping,
    AppConfig,
    BehaviorConfig,
    LocalConfig,
    LogConfig,
    PathsConfig,
    RefreshConfig,
    WebDAVConfig,
)
from database import Database, BLineageSnapshotRecord
from utils.strm_utils import make_strm_fingerprint


class _AppCase:
    def __init__(self, root: Path):
        self.root = root
        self.c_root = root / "c"
        self.a1 = root / "a1"
        self.b1 = root / "b1"
        self.a2 = root / "a2"
        self.b2 = root / "b2"
        for path in (self.c_root, self.a1, self.b1, self.a2, self.b2):
            path.mkdir(parents=True, exist_ok=True)

        self.m1 = ABMapping("m1", str(self.a1), str(self.b1), "one")
        self.m2 = ABMapping("m2", str(self.a2), str(self.b2), "two")
        config = AppConfig(
            base_dir=str(root),
            webdav=WebDAVConfig("", "", "", ""),
            refresh=RefreshConfig(interval_seconds=300, enabled=False),
            behavior=BehaviorConfig(sync_on_startup=False, sync_on_startup_wait=0),
            log=LogConfig(level="WARNING", max_size_mb=1, backup_count=1),
            local=LocalConfig(str(root), str(self.a1), str(self.b1), str(self.c_root)),
            paths=PathsConfig([], [], c_root=str(self.c_root)),
            a_b_mappings=[self.m1, self.m2],
        )
        self.db = Database(str(root / "bridge.db"))
        self.admin = Mock()
        self.admin.check_exists.return_value = True
        self.app = AppService(config, self.db, self.admin)

    def close(self) -> None:
        self.app.stop()


def _write_strm(path: Path, webdav: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(webdav, encoding="utf-8")
    return path


def _seed_a(case: _AppCase, *, mapping: str = "m1", name: str = "show.strm") -> Path:
    root = case.a1 if mapping == "m1" else case.a2
    return _write_strm(root / name, f"/cloud/{mapping}/{name}")


def _seed_b(case: _AppCase, *, mapping: str = "m1", name: str = "show.strm") -> Path:
    root = case.b1 if mapping == "m1" else case.b2
    return _write_strm(root / name, f"/cloud/{mapping}/{name}")


def _scan(case: _AppCase, monkeypatch, *, force_full: bool = False, valid: bool = True,
          preflight: bool | None = None):
    """驱动 initial_scan_b 并记录 lineage 判定调用（T2b 双相口径，C9）。

    - preflight=None（默认）：wave1 走真实 _b_lineage_preflight 自然判定
      （本测试组不播种 A 源，预检必然未放行 → 完整校验回退，与历史口径一致）；
    - preflight=True/False：monkeypatch _b_lineage_preflight 强制返回，
      显式控制新增文件（insert 路径 wave1）的预检判定。
    返回完整校验（_verify_b_path_lineage，reconcile/回退路径）与强制预检
    （preflight_calls）的合并调用列表——语义等价：新增文件经历的 lineage
    判定总次数不变；verify_calls 单独记录完整校验相位。
    """
    verify_calls: list[tuple[str, str]] = []
    preflight_calls: list[tuple[str, str]] = []

    def verify(path: str, webdav: str, is_sync_phase: bool = False) -> bool:
        verify_calls.append((path, webdav))
        return valid

    monkeypatch.setattr(case.app, "_verify_b_path_lineage", verify)
    if preflight is not None:
        def fake_preflight(path: str, webdav: str, b_local=None) -> bool:
            preflight_calls.append((path, webdav))
            return preflight
        monkeypatch.setattr(case.app, "_b_lineage_preflight", fake_preflight)
    case.app.initial_scan_b(force_full=force_full)
    return verify_calls + preflight_calls


def _records(db: Database) -> set[tuple[str, str, str]]:
    return {
        (row.local_path, row.fingerprint or "", row.mapping_id)
        for row in db.get_all_b_records()
    }


@pytest.fixture
def case(tmp_path: Path):
    value = _AppCase(tmp_path)
    yield value
    value.close()


def test_unchanged_incremental_reuses_snapshot(case, monkeypatch):
    path = _seed_b(case)
    first = _scan(case, monkeypatch, force_full=True)
    second = _scan(case, monkeypatch)
    assert len(first) == 1
    assert second == []
    assert case.db.get_b_lineage_snapshot("m1", str(path)).validation_state == "valid"


def test_content_modified_rechecks_lineage(case, monkeypatch):
    path = _seed_b(case)
    _scan(case, monkeypatch, force_full=True)
    path.write_text("/cloud/m1/changed.strm", encoding="utf-8")
    calls = _scan(case, monkeypatch)
    assert len(calls) == 1


def test_deleted_file_removes_b_record(case, monkeypatch):
    path = _seed_b(case)
    _scan(case, monkeypatch, force_full=True)
    path.unlink()
    _scan(case, monkeypatch)
    assert case.db.get_b_by_local(str(path)) is None


def test_same_mapping_rename_migrates_record_and_snapshot(case, monkeypatch):
    old = _seed_b(case)
    _scan(case, monkeypatch, force_full=True)
    new = old.with_name("renamed.strm")
    old.rename(new)
    calls = _scan(case, monkeypatch)
    assert len(calls) == 1
    assert case.db.get_b_by_local(str(old)) is None
    assert case.db.get_b_by_local(str(new)) is not None
    assert case.db.get_b_lineage_snapshot("m1", str(new)) is not None


def test_cross_mapping_move_does_not_reuse_snapshot(case, monkeypatch):
    old = _seed_b(case)
    _scan(case, monkeypatch, force_full=True)
    new = case.b2 / old.name
    old.rename(new)
    # C9：fail-closed 意图经 preflight=False 显式强制（insert 路径 wave1
    # 预检 + reconcile/回退完整校验均拒绝）
    calls = _scan(case, monkeypatch, valid=False, preflight=False)
    assert len(calls) >= 1
    assert case.db.get_b_by_local(str(new)) is None


def test_cross_directory_invalid_move_is_fail_closed(case, monkeypatch):
    old = _seed_b(case)
    _scan(case, monkeypatch, force_full=True)
    new = case.b1 / "unrelated" / old.name
    new.parent.mkdir()
    old.rename(new)
    calls = _scan(case, monkeypatch, valid=False, preflight=False)
    assert calls
    assert not new.exists()


@pytest.mark.parametrize("force_full", [True, False])
def test_missing_fingerprint_is_not_inserted(case, monkeypatch, force_full):
    path = case.b1 / "invalid.strm"
    path.write_text("not a webdav path", encoding="utf-8")
    _scan(case, monkeypatch, force_full=force_full)
    assert case.db.get_b_by_local(str(path)) is None


@pytest.mark.parametrize("force_full", [True, False])
def test_duplicate_same_mapping_is_scoped(case, monkeypatch, force_full):
    _write_strm(case.b1 / "one.strm", "/same/fingerprint.strm")
    _write_strm(case.b1 / "two.strm", "/same/fingerprint.strm")
    _scan(case, monkeypatch, force_full=force_full)
    rows = [r for r in case.db.get_all_b_records() if r.mapping_id == "m1"]
    assert len(rows) == 2
    assert {r.fingerprint for r in rows} == {make_strm_fingerprint("/same/fingerprint.strm")}


@pytest.mark.parametrize("force_full", [True, False])
def test_duplicate_different_mapping_is_preserved(case, monkeypatch, force_full):
    _seed_b(case, mapping="m1")
    _seed_b(case, mapping="m2")
    _scan(case, monkeypatch, force_full=force_full)
    assert {row.mapping_id for row in case.db.get_all_b_records()} == {"m1", "m2"}


def test_missing_a_source_can_use_mapping_boundary(case, monkeypatch):
    path = _seed_b(case)
    fp = make_strm_fingerprint("/cloud/m1/show.strm")
    case.db.upsert_media_boundary("m1", fp, "show", "renamed", "/engine")
    monkeypatch.setattr(case.app, "find_a_source_by_webdav", lambda _: None)
    assert case.app._verify_a_source_exists(str(path), "/cloud/m1/show.strm", fp)


@pytest.mark.parametrize("force_full", [True, False])
def test_same_relative_name_under_two_roots_isolated(case, monkeypatch, force_full):
    one = _seed_b(case, mapping="m1", name="same.strm")
    two = _seed_b(case, mapping="m2", name="same.strm")
    _scan(case, monkeypatch, force_full=force_full)
    assert case.db.get_b_by_local(str(one)).mapping_id == "m1"
    assert case.db.get_b_by_local(str(two)).mapping_id == "m2"


def test_mapping_version_change_disables_reuse(case, monkeypatch):
    _seed_b(case)
    _scan(case, monkeypatch, force_full=True)
    case.app._mapping_version = "changed"
    calls = _scan(case, monkeypatch)
    assert calls


def test_lineage_version_mismatch_disables_reuse(case, monkeypatch):
    path = _seed_b(case)
    _scan(case, monkeypatch, force_full=True)
    case.db.upsert_b_lineage_snapshot(
        "m1", str(path), path.stat().st_size, path.stat().st_mtime_ns,
        make_strm_fingerprint("/cloud/m1/show.strm"), case.app._mapping_version,
        999, "valid")
    calls = _scan(case, monkeypatch)
    assert calls


def test_missing_or_corrupt_snapshot_falls_back(case, monkeypatch):
    path = _seed_b(case)
    _scan(case, monkeypatch, force_full=True)
    case.db.invalidate_b_lineage_snapshots("m1", str(path))
    calls = _scan(case, monkeypatch)
    assert calls


def test_stat_or_snapshot_write_error_does_not_claim_unchanged(case, monkeypatch):
    path = _seed_b(case)
    _scan(case, monkeypatch, force_full=True)
    original = case.db.get_b_lineage_snapshot
    monkeypatch.setattr(case.db, "get_b_lineage_snapshot", Mock(side_effect=OSError("db unavailable")))
    assert not case.app._snapshot_reuses_valid_lineage(
        str(path), make_strm_fingerprint("/cloud/m1/show.strm"))
    monkeypatch.setattr(case.db, "get_b_lineage_snapshot", original)


def test_stat_oserror_does_not_claim_unchanged(case, monkeypatch):
    """真实 Path.stat OSError：读取侧回退完整核对，写入侧不留下半状态。"""
    path = _seed_b(case)
    _scan(case, monkeypatch, force_full=True)
    fp = make_strm_fingerprint("/cloud/m1/show.strm")
    real_stat = Path.stat

    def flaky_stat(self, *args, **kwargs):
        if self == path:
            raise OSError("simulated stat failure")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    # 读取侧：stat 失败不得被解释为 unchanged
    assert not case.app._snapshot_reuses_valid_lineage(str(path), fp)
    # 写入侧：stat 失败不得抛出，且不得写入 snapshot
    case.db.invalidate_b_lineage_snapshots("m1", str(path))
    case.app._store_valid_lineage_snapshot(str(path), fp)
    assert case.db.get_b_lineage_snapshot("m1", str(path)) is None


def test_snapshot_write_db_error_does_not_abort_scan(case, monkeypatch):
    """snapshot DB 写异常：扫描不得中断，记录保留，且走完整核对而非误判 unchanged。"""
    path = _seed_b(case)
    _scan(case, monkeypatch, force_full=True)
    case.db.invalidate_b_lineage_snapshots("m1", str(path))
    # 核对路径的快照写入是批量缓冲的，patch 批量写入方法模拟 DB 写失败
    monkeypatch.setattr(
        case.db, "upsert_b_lineage_snapshots_batch",
        Mock(side_effect=OSError("disk io error")))
    calls = _scan(case, monkeypatch)
    assert len(calls) == 1
    assert case.db.get_b_by_local(str(path)) is not None
    assert case.db.get_b_lineage_snapshot("m1", str(path)) is None


def test_scan_concurrent_file_modification(case, monkeypatch):
    """核对执行期间文件被并发修改：不得留下可复用的陈旧 valid snapshot。"""
    path = _seed_b(case)
    _scan(case, monkeypatch, force_full=True)
    case.db.invalidate_b_lineage_snapshots("m1", str(path))

    def verify_and_modify(p: str, w: str, is_sync_phase: bool = False) -> bool:
        # 模拟 lineage 校验进行期间文件内容被改写
        path.write_text("/cloud/m1/modified-during-scan.strm", encoding="utf-8")
        return True

    monkeypatch.setattr(case.app, "_verify_b_path_lineage", verify_and_modify)
    case.app.initial_scan_b(force_full=True)
    # 被修改的文件指纹已变化，下一轮扫描必须重新核对而非复用 snapshot
    calls = _scan(case, monkeypatch)
    assert calls
    # 第三轮：snapshot 已对齐新内容，可安全复用
    assert _scan(case, monkeypatch) == []


# ============================================================
# 缓存与批量写入语义（Task 1 补充保护测试）
# ============================================================


def test_cache_a_record_duplicate_key_first_row_wins(case):
    """_build_reconcile_cache 中 a_by_webdav 重复键使用首行优先（setdefault），
    与 get_a_by_webdav(...).fetchone() 语义一致。"""
    case.db.upsert_a("a1/first.strm", "/cloud/m1/dup", "/cloud/m1")
    case.db.upsert_a("a1/second.strm", "/cloud/m1/dup", "/cloud/m1")
    case.app._build_reconcile_cache()
    cache = case.app._reconcile_cache
    assert cache is not None
    a_record = cache["a_by_webdav"].get("/cloud/m1/dup")
    assert a_record is not None
    assert a_record.local_path == "a1/first.strm"


def test_cache_boundary_updated_at_latest_wins(case):
    """_build_reconcile_cache 中 boundary 三种索引均保留 updated_at 最大的一条，
    与 ORDER BY updated_at DESC LIMIT 1 语义一致。"""
    fp = "fp_show"
    # 插入旧记录（updated_at 较小）
    case.db.upsert_media_boundary("m1", fp, "src_old", "cur_old", "engine_entry")
    old_time = time.time() - 100
    # 不能直接 set updated_at，需通过 SQL 修改
    with case.db.rw_lock.write_locked(), case.db.connection() as conn:
        conn.execute(
            "UPDATE strm_media_boundary SET updated_at = ? WHERE fingerprint = ? AND mapping_id = ?",
            (old_time, fp, "m1"))
        conn.commit()
    # 插入新记录（不同 fingerprint 但同 source_media_name，用于测试 by_source_name_only）
    fp2 = "fp_show_v2"
    case.db.upsert_media_boundary("m1", fp2, "src_old", "cur_new", "engine_entry")
    # 构建缓存
    case.app._build_reconcile_cache()
    cache = case.app._reconcile_cache
    assert cache is not None
    # by_fingerprint: 同 fingerprint 保留最新 updated_at
    b_fp = cache["boundaries"]["by_fingerprint"].get(("m1", fp))
    assert b_fp is not None
    assert b_fp.fingerprint == fp
    assert b_fp.current_media_name == "cur_old"  # 旧记录保留
    # by_source_name_only: 同 source_media_name 保留最新 updated_at
    b_src = cache["boundaries"]["by_source_name_only"].get(("m1", "src_old"))
    assert b_src is not None
    assert b_src.fingerprint == fp2  # 较新记录
    # by_current_name: 同 current_media_name 保留最新 updated_at
    b_cur = cache["boundaries"]["by_current_name"].get(("m1", "cur_new", "engine_entry"))
    assert b_cur is not None
    assert b_cur.fingerprint == fp2


def test_cache_batch_snapshot_row_failure_isolated(case):
    """upsert_b_lineage_snapshots_batch 单行失败不阻断其余行。"""
    valid_row = (
        "m1", "/b1/valid.strm", 100, 200,
        "fp_valid", case.app._mapping_version, 1, "valid")
    invalid_row = (
        "m1", "/b1/bad.strm", 100, 200,
        "fp_bad", "", 1, "invalid")  # mapping_version="" 触发 ValueError
    case.db.upsert_b_lineage_snapshots_batch([invalid_row, valid_row])
    # 验证有效行仍写入
    snapshot = case.db.get_b_lineage_snapshot("m1", "/b1/valid.strm")
    assert snapshot is not None
    assert snapshot.fingerprint == "fp_valid"
    assert snapshot.validation_state == "valid"
    # 无效行未写入
    bad = case.db.get_b_lineage_snapshot("m1", "/b1/bad.strm")
    assert bad is None


def test_cache_new_records_not_buffered(case, monkeypatch):
    """_insert_new_b_records 调用 _store_valid_lineage_snapshot 时使用默认
    buffered=False，快照直接写入 DB 而不留在 pending 缓冲。"""
    path = _seed_b(case, name="new_show.strm")
    _scan(case, monkeypatch, force_full=True)
    # 核对完成时 _reconcile_cache 已被清理
    assert case.app._reconcile_cache is None
    # 新记录的快照直接写入 DB 而非缓冲
    snapshot = case.db.get_b_lineage_snapshot("m1", str(path))
    assert snapshot is not None
    assert snapshot.validation_state == "valid"


def test_cache_mapping_id_isolation_in_boundaries(case):
    """_build_reconcile_cache 中 boundary 按 mapping_id 隔离；
    m1 的缓存不应包含 m2 的记录。"""
    case.db.upsert_media_boundary("m1", "fp_m1", "src_m1", "cur_m1", "engine")
    case.db.upsert_media_boundary("m2", "fp_m2", "src_m2", "cur_m2", "engine")
    case.app._build_reconcile_cache()
    cache = case.app._reconcile_cache
    assert cache is not None
    # m1 的缓存只应包含 m1 的记录
    assert ("m1", "fp_m1") in cache["boundaries"]["by_fingerprint"]
    assert ("m1", "src_m1") in cache["boundaries"]["by_source_name_only"]
    assert ("m1", "cur_m1", "engine") in cache["boundaries"]["by_current_name"]
    # m2 的记录不应出现在 m1 的索引中
    assert ("m1", "fp_m2") not in cache["boundaries"]["by_fingerprint"]
    assert ("m1", "src_m2") not in cache["boundaries"]["by_source_name_only"]


def test_cache_mapping_id_isolation_in_snapshots(case):
    """_build_reconcile_cache 中 snapshot 按 mapping_id 隔离；
    m1 的 snapshot 缓存不应包含 m2 的记录。"""
    # 直接写入 snapshot 表
    case.db.upsert_b_lineage_snapshot(
        "m1", "/b1/m1_show.strm", 100, 200, "fp_m1",
        case.app._mapping_version, 1, "valid")
    case.db.upsert_b_lineage_snapshot(
        "m2", "/b2/m2_show.strm", 100, 200, "fp_m2",
        case.app._mapping_version, 1, "valid")
    case.app._build_reconcile_cache()
    cache = case.app._reconcile_cache
    assert cache is not None
    assert ("m1", "/b1/m1_show.strm") in cache["snapshots"]
    assert ("m1", "/b2/m2_show.strm") not in cache["snapshots"]


def test_db_get_all_lineage_snapshots_mapping_id_filter(case):
    """get_all_lineage_snapshots 按 mapping_id 过滤，不泄漏跨 mapping 记录。"""
    case.db.upsert_b_lineage_snapshot(
        "m1", "/b1/m1.strm", 100, 200, "fp1",
        case.app._mapping_version, 1, "valid")
    case.db.upsert_b_lineage_snapshot(
        "m2", "/b2/m2.strm", 100, 200, "fp2",
        case.app._mapping_version, 1, "valid")
    m1_snapshots = case.db.get_all_lineage_snapshots("m1")
    assert len(m1_snapshots) == 1
    assert m1_snapshots[0].local_path == "/b1/m1.strm"
    m2_snapshots = case.db.get_all_lineage_snapshots("m2")
    assert len(m2_snapshots) == 1
    assert m2_snapshots[0].local_path == "/b2/m2.strm"


def test_db_get_all_media_boundaries_mapping_id_filter(case):
    """get_all_media_boundaries 按 mapping_id 过滤，不泄漏跨 mapping 记录。"""
    case.db.upsert_media_boundary("m1", "fp1", "src1", "cur1", "engine")
    case.db.upsert_media_boundary("m2", "fp2", "src2", "cur2", "engine")
    m1_boundaries = case.db.get_all_media_boundaries("m1")
    assert len(m1_boundaries) == 1
    assert m1_boundaries[0].fingerprint == "fp1"
    m2_boundaries = case.db.get_all_media_boundaries("m2")
    assert len(m2_boundaries) == 1
    assert m2_boundaries[0].fingerprint == "fp2"
