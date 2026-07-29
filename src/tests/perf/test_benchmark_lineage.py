"""性能基准正确性门禁测试。

这些测试验证 ``benchmark_lineage.py`` 中的核心函数（``compute_digest``、
``build_fixture``、``baseline``、``optimized``）在**小规模数据**上的正确性，
而非性能阈值。性能基准（耗时、加速比）仅由显式 CLI 执行，不纳入 pytest 门禁，
避免因机器差异导致不稳定。

运行方式::

    python -m pytest src/tests/perf -v
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

# 确保 src/ 在 sys.path 中（conftest.py 也会处理，此处冗余保护）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import benchmark_lineage as bl


# ============================================================
# compute_digest
# ============================================================

class TestComputeDigest:
    """测试 compute_digest 的稳定性和确定性。"""

    def test_empty_rows(self):
        assert bl.compute_digest([]) == hashlib.sha256().hexdigest()

    def test_deterministic_same_input(self):
        rows = [(1, "valid"), (2, "invalid"), (3, "missing")]
        assert bl.compute_digest(rows) == bl.compute_digest(rows)

    def test_order_independent(self):
        """digest 按 id 排序，因此行顺序不影响结果。"""
        a = [(3, "missing"), (1, "valid"), (2, "invalid")]
        b = [(1, "valid"), (2, "invalid"), (3, "missing")]
        assert bl.compute_digest(a) == bl.compute_digest(b)

    def test_different_state_different_digest(self):
        a = [(1, "valid")]
        b = [(1, "invalid")]
        assert bl.compute_digest(a) != bl.compute_digest(b)

    def test_different_id_different_digest(self):
        a = [(1, "valid")]
        b = [(2, "valid")]
        assert bl.compute_digest(a) != bl.compute_digest(b)


# ============================================================
# build_fixture
# ============================================================

class TestBuildFixture:
    """测试 build_fixture 构造的测试环境结构正确。"""

    def test_creates_expected_files(self, tmp_path):
        a_root, b_root, db_path, n, delta = bl.build_fixture(tmp_path, 10)
        assert n == 10
        assert delta == max(1, int(10 * 0.1))  # delta_pct 默认 0.1
        assert a_root.exists()
        assert b_root.exists()
        assert db_path.exists()

    def test_creates_strm_files(self, tmp_path):
        a_root, b_root, db_path, n, _ = bl.build_fixture(tmp_path, 5)
        a_files = list(a_root.rglob("*.strm"))
        b_files = list(b_root.rglob("*.strm"))
        assert len(a_files) == 5
        assert len(b_files) == 5

    def test_db_has_records(self, tmp_path):
        _, _, db_path, n, _ = bl.build_fixture(tmp_path, 8)
        import sqlite3
        con = sqlite3.connect(str(db_path))
        a_count = con.execute("SELECT COUNT(*) FROM a_strm_files").fetchone()[0]
        b_count = con.execute("SELECT COUNT(*) FROM b_strm_files").fetchone()[0]
        snap_count = con.execute(
            "SELECT COUNT(*) FROM b_lineage_snapshot").fetchone()[0]
        con.close()
        assert a_count == 8
        assert b_count == 8
        assert snap_count == 8

    def test_delta_count(self, tmp_path):
        _, _, _, n, delta = bl.build_fixture(tmp_path, 20, delta_pct=0.2)
        assert delta == 4

    def test_minimum_one_delta(self, tmp_path):
        """即使 delta_pct 很小，也至少有 1 条变化。"""
        _, _, _, n, delta = bl.build_fixture(tmp_path, 5, delta_pct=0.01)
        assert delta == 1


# ============================================================
# baseline vs optimized 等价性（正确性门禁，不含性能阈值）
# ============================================================

class TestBaselineOptimizedEquivalence:
    """验证 baseline 和 optimized 产生相同的 digest（正确性等价）。

    这是性能优化的核心正确性保证：增量校验的结果必须与全量校验一致。
    """

    @pytest.mark.parametrize("n", [5, 20, 50])
    def test_digests_match_no_delta(self, tmp_path, n):
        """无变化时 baseline 和 optimized 的 digest 必须一致。"""
        a_root, b_root, db_path, _, _ = bl.build_fixture(tmp_path, n)
        base = bl.baseline(a_root, b_root, db_path)
        opt = bl.optimized(a_root, b_root, db_path)
        assert base.digest == opt.digest
        assert base.records == opt.records == n

    @pytest.mark.parametrize("n", [10, 30])
    def test_digests_match_with_delta(self, tmp_path, n):
        """有变化时 baseline 和 optimized 的 digest 仍必须一致。"""
        a_root, b_root, db_path, _, delta = bl.build_fixture(
            tmp_path, n, delta_pct=0.3)
        bl.apply_delta(a_root, b_root, db_path, delta)
        base = bl.baseline(a_root, b_root, db_path)
        opt = bl.optimized(a_root, b_root, db_path)
        assert base.digest == opt.digest

    def test_optimized_skips_unchanged(self, tmp_path):
        """无变化时 optimized 应跳过大部分记录（skipped > 0）。"""
        a_root, b_root, db_path, _, _ = bl.build_fixture(tmp_path, 20)
        opt = bl.optimized(a_root, b_root, db_path)
        assert opt.skipped > 0
        assert opt.verified < opt.records

    def test_optimized_verifies_changed(self, tmp_path):
        """有变化时 optimized 应验证变化的记录（verified > 0）。"""
        a_root, b_root, db_path, _, delta = bl.build_fixture(
            tmp_path, 20, delta_pct=0.3)
        bl.apply_delta(a_root, b_root, db_path, delta)
        opt = bl.optimized(a_root, b_root, db_path)
        assert opt.verified > 0

    def test_result_counts_consistent(self, tmp_path):
        """baseline 和 optimized 的 valid/invalid/missing 计数一致。"""
        a_root, b_root, db_path, _, delta = bl.build_fixture(
            tmp_path, 15, delta_pct=0.2)
        bl.apply_delta(a_root, b_root, db_path, delta)
        base = bl.baseline(a_root, b_root, db_path)
        opt = bl.optimized(a_root, b_root, db_path)
        assert base.valid == opt.valid
        assert base.invalid == opt.invalid
        assert base.missing == opt.missing


# ============================================================
# apply_delta
# ============================================================

class TestApplyDelta:
    """测试 apply_delta 的文件修改行为。"""

    def test_deletes_and_modifies_files(self, tmp_path):
        a_root, b_root, db_path, n, delta = bl.build_fixture(
            tmp_path, 20, delta_pct=0.2)
        changes = bl.apply_delta(a_root, b_root, db_path, delta)
        assert len(changes) > 0
        deleted = [c for c in changes if c[1] == "deleted"]
        modified = [c for c in changes if c[1] == "modified"]
        assert len(deleted) > 0
        assert len(modified) > 0

    def test_deleted_files_removed_from_disk(self, tmp_path):
        a_root, b_root, db_path, n, delta = bl.build_fixture(
            tmp_path, 10, delta_pct=0.2)
        changes = bl.apply_delta(a_root, b_root, db_path, delta)
        for path, action in changes:
            if action == "deleted":
                assert not Path(path).exists()
            elif action == "modified":
                assert Path(path).exists()
