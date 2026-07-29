"""真实 AppService 的 mapping-scoped lineage snapshot 验收测试。"""
from __future__ import annotations

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
from database import Database
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


def _scan(case: _AppCase, monkeypatch, *, force_full: bool = False, valid: bool = True):
    calls: list[tuple[str, str]] = []

    def verify(path: str, webdav: str, is_sync_phase: bool = False) -> bool:
        calls.append((path, webdav))
        return valid

    monkeypatch.setattr(case.app, "_verify_b_path_lineage", verify)
    case.app.initial_scan_b(force_full=force_full)
    return calls


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
    calls = _scan(case, monkeypatch, valid=False)
    assert len(calls) >= 1
    assert case.db.get_b_by_local(str(new)) is None


def test_cross_directory_invalid_move_is_fail_closed(case, monkeypatch):
    old = _seed_b(case)
    _scan(case, monkeypatch, force_full=True)
    new = case.b1 / "unrelated" / old.name
    new.parent.mkdir()
    old.rename(new)
    calls = _scan(case, monkeypatch, valid=False)
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
    monkeypatch.setattr(
        case.db, "upsert_b_lineage_snapshot",
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
