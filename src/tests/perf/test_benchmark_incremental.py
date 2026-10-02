"""Runner C: 真实增量流水线与终态状态机的 Pytest 门禁。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import benchmark_incremental as bi


class TestDeltaCounts:
    def test_delta_counts_have_rounding_rule(self):
        d = bi.calculate_delta_counts(100, 0.05)
        # 100 * 5% = 5 → total=5, base=1, rem=2 → added=3, mod=1, rem=1
        assert d == (5, 3, 1, 1)
        assert sum(d[1:]) == d[0]

    def test_delta_never_below_three(self):
        d = bi.calculate_delta_counts(10, 0.05)
        assert d[0] >= 3
        assert sum(d[1:]) == d[0]


class TestIncrementalBenchmark:
    def test_incremental_pipeline_passes_all_state_checks(self, tmp_path):
        report = bi.run_incremental_benchmark(
            base_dir=tmp_path,
            records=16,
            mappings=2,
            delta_pct=0.25,
            seed=42,
        )
        assert report["all_passed"] is True
        assert report["verification"]["removed_verified"] is True
        assert report["verification"]["added_verified"] is True
        assert report["verification"]["modified_verified"] is True
        assert report["verification"]["fts_a_aligned"] is True
        assert report["verification"]["fts_b_aligned"] is True
        assert report["cold_run"]["unexpected_external_calls"] == 0
        assert report["incremental_run"]["unexpected_external_calls"] == 0
        assert report["b_cleanup_three_state"]["three_state_passed"] is True

    def test_delta_counts_recorded_in_report(self, tmp_path):
        report = bi.run_incremental_benchmark(
            base_dir=tmp_path,
            records=16,
            mappings=1,
            delta_pct=0.25,
            seed=7,
        )
        counts = report["metadata"]["delta_counts"]
        assert counts["total"] >= 4
        assert counts["total"] <= 5
        assert counts["added"] + counts["modified"] + counts["removed"] == counts["total"]

    def test_zero_network_and_three_state_b_cleanup(self, tmp_path):
        """专项验证 check_exists True/False/None 三态在 B 区清理语义下正确。"""
        # 使用 build_fixture 与增量 runner 的共用路径
        report = bi.run_incremental_benchmark(
            base_dir=tmp_path,
            records=8,
            mappings=1,
            delta_pct=0.25,
            seed=1,
        )
        three = report["b_cleanup_three_state"]
        assert three["true_kept"] is True
        assert three["false_cleaned"] is True
        assert three["none_kept_fail_closed"] is True
        assert three["fake_contract_calls"] == 3
        assert three["unexpected_external_calls"] == 0


class TestIncrementalZeroAdmin:
    def test_zero_admin_traps_unconfigured_check_exists(self):
        admin = bi.IncrementalZeroAdmin()
        with pytest.raises(RuntimeError):
            admin.check_exists("/unexpected")
        assert admin.unexpected_external_calls == 1
        assert admin.fake_contract_calls == 0

    def test_zero_admin_allows_configured_three_state(self):
        admin = bi.IncrementalZeroAdmin(check_exists_map={"/a": True, "/b": False, "/c": None})
        assert admin.check_exists("/a") is True
        assert admin.check_exists("/b") is False
        assert admin.check_exists("/c") is None
        assert admin.fake_contract_calls == 3
        assert admin.unexpected_external_calls == 0