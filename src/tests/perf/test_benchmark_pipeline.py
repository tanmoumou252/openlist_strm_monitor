"""真实 AppService 启动 pipeline benchmark 的正确性门禁。"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import benchmark_startup_pipeline as bp
from utils import make_strm_fingerprint


class TestFailFastAdmin:
    def test_unallowed_admin_call_fails_immediately(self):
        admin = bp.FailFastAdmin()
        with pytest.raises(bp.UnexpectedNetworkCall):
            admin.check_exists("/should-not-be-called")
        assert admin.unexpected_external_calls == 1
        assert admin.real_http_calls == 0

    def test_allowed_call_counter_stays_zero(self):
        admin = bp.FailFastAdmin()
        assert admin.fake_contract_calls == 0
        assert admin.unexpected_external_calls == 0
        assert admin.real_http_calls == 0
        assert admin.network_calls == 0
        assert admin.calls == []


class TestFixtureAndDigest:
    def test_fixture_uses_only_temporary_zone_roots(self, tmp_path):
        fixture = bp.build_fixture(tmp_path, records=8, mappings=2)
        assert fixture.a_roots and fixture.b_roots and fixture.c_root
        for path in [*fixture.a_roots, *fixture.b_roots, fixture.c_root]:
            assert Path(path).resolve().is_relative_to(tmp_path.resolve())
        assert fixture.db_path.resolve().is_relative_to(tmp_path.resolve())
        assert fixture.expected_records == 8

    def test_terminal_digest_changes_when_physical_state_changes(self, tmp_path):
        fixture = bp.build_fixture(tmp_path, records=4, mappings=1)
        before = bp.compute_terminal_digest(fixture)
        next(iter(fixture.b_roots)).joinpath("extra.strm").write_text(
            "/extra", encoding="utf-8"
        )
        after = bp.compute_terminal_digest(fixture)
        assert before["overall"] != after["overall"]

    def test_fixture_categories_are_conserved(self, tmp_path):
        """场景计数守恒：各种 scenario 之和 = records，且无旧键名。"""
        for records, mappings in [(10, 1), (32, 2), (101, 2)]:
            fixture = bp.build_fixture(tmp_path, records=records, mappings=mappings)
            cat_sum = sum(fixture.categories.values())
            assert cat_sum == records, (
                f"categories sum {cat_sum} != records {records} "
                f"(categories={fixture.categories})")
            # 精确断言 new_in_a 计数（仅 i%4==1 的场景）
            records_per_mapping = (records + mappings - 1) // mappings
            expected_new = sum(1 for m in range(mappings) for i in range(records_per_mapping)
                               if m * records_per_mapping + i < records and i % 4 == 1)
            assert fixture.categories["new_in_a"] == expected_new, (
                f"new_in_a {fixture.categories['new_in_a']} != expected {expected_new}")
            # 旧键名不应存在
            assert "unchanged" not in fixture.categories
            assert "existing_b_missing_db" not in fixture.categories
            assert "duplicate_fp" not in fixture.categories
            assert "rename_candidate" not in fixture.categories

    def test_fixture_injective_at_10k(self, tmp_path):
        """10,000 条/双 mapping 时实际 A 文件数与唯一 WebDAV 路径数严格等于 10,000。
        当前实现因取模周期 600 导致虚标——本测试正是为此设计。"""
        records = 10000
        fixture = bp.build_fixture(tmp_path, records=records, mappings=2)
        a_files = 0
        webdav_paths = set()
        for r in fixture.a_roots:
            for p in r.rglob("*.strm"):
                a_files += 1
                webdav_paths.add(p.read_text(encoding="utf-8"))
        assert a_files == records, (
            f"实际 A 文件数 {a_files} != records {records}（fixture 取模虚标）")
        assert len(webdav_paths) == records, (
            f"唯一 WebDAV 路径数 {len(webdav_paths)} != records {records}")

    def test_fixture_dup_scenario_same_fingerprint(self, tmp_path):
        """dup_b 场景产生真实同指纹双实例。"""
        fixture = bp.build_fixture(tmp_path, records=16, mappings=1)
        assert fixture.categories["dup_b"] == 4, f"dup_b={fixture.categories['dup_b']}"
        fps: dict[str, list[str]] = {}
        for b_root in fixture.b_roots:
            for p in b_root.rglob("*.strm"):
                content = p.read_text(encoding="utf-8")
                fp = make_strm_fingerprint(content)
                fps.setdefault(fp, []).append(str(p))
        dup_groups = [v for v in fps.values() if len(v) == 2]
        assert len(dup_groups) == fixture.categories["dup_b"], (
            f"同指纹双实例组数 {len(dup_groups)} != dup_b {fixture.categories['dup_b']}"
        )


class TestPipelineContract:
    def test_run_uses_real_database_and_app_service_and_is_network_free(self, tmp_path):
        result = bp.run_pipeline(tmp_path, records=12, mappings=2, repeat=1)
        assert result["network_calls"] == 0
        assert result["fake_contract_calls"] == 0
        assert result["unexpected_external_calls"] == 0
        assert result["real_http_calls"] == 0
        assert result["app_service_type"] == "AppService"
        assert result["database_type"] == "Database"
        assert result["terminal_digest"]["overall"]
        assert set(result["stages"]) == {
            "initial_scan_a",
            "initial_scan_b",
            "scan_a_to_b_full_sync",
            "catch_up_readonly",
            "boundary_catch_up",
        }
        run = result["runs"][0]
        # 冷启动复制数与 expected_copy 精确匹配
        assert run["physical_operations"]["copy"] == run["physical_operations"]["expected_copy"]
        # move == dup_b 计数（真实隔离改名）
        assert run["physical_operations"]["move"] == run["fixture_categories"]["dup_b"]
        # 正常启动无物理删除
        assert run["physical_operations"]["delete"] == 0
        assert run["physical_operations"]["db_delete"] == 0
        # 三组时间：wall_clock_total 为门禁依据
        assert run["wall_clock_total"] >= run["instrumented_stage_total"]
        assert run["stage_overhead_seconds"] == (
            run["wall_clock_total"] - run["instrumented_stage_total"]
        )
        # 外部队列调用计数为零
        assert run["fake_contract_calls"] == 0
        assert run["unexpected_external_calls"] == 0
        assert run["real_http_calls"] == 0

    def test_gate2_composite_correctness_and_time(self, tmp_path):
        """gate2_passed 同时反映正确性与耗时。"""
        # 正确性通过且时间充裕 → True
        result = bp.run_pipeline(
            tmp_path / "run_with_threshold",
            records=12,
            mappings=2,
            repeat=1,
            max_seconds=3600.0,
        )
        assert result["gate2_passed"] is True

        # 无阈值时仅反映正确性（应为 bool 而非 None）
        result_no_threshold = bp.run_pipeline(
            tmp_path / "run_no_threshold", records=6, mappings=1, repeat=1
        )
        assert isinstance(result_no_threshold["gate2_passed"], bool)
        assert result_no_threshold["gate2_passed"] is True

    def test_gate2_fails_on_copy_mismatch(self, tmp_path):
        """正确性失败（copy != expected_copy）时 gate2_passed 为 False。"""
        # 通过 monkeypatch 使 new_in_a 计数虚增，导致 expected_copy 虚高
        orig_build = bp.build_fixture

        def _corrupt_build(base_dir, records, mappings=2, dup_rate=0.25):
            fixture = orig_build(
                base_dir, records=records, mappings=mappings, dup_rate=dup_rate)
            fixture.categories["new_in_a"] += 1
            return fixture

        bp.build_fixture = _corrupt_build
        try:
            result = bp.run_pipeline(
                tmp_path, records=8, mappings=2, repeat=1, max_seconds=3600.0
            )
            assert result["gate2_passed"] is False
        finally:
            bp.build_fixture = orig_build

    def test_repeat_returns_stable_terminal_digest_and_ops(self, tmp_path):
        result = bp.run_pipeline(tmp_path, records=10, mappings=1, repeat=2)
        # 跨 run digest 一致
        digests = [run["terminal_digest"]["overall"] for run in result["runs"]]
        assert len(digests) == 2
        assert digests[0] == digests[1]
        # 跨 run physical_operations 一致
        for key in ("copy", "move", "delete", "db_delete", "expected_copy"):
            vals = [run["physical_operations"][key] for run in result["runs"]]
            assert vals[0] == vals[1], f"physical_operations.{key} 跨 run 不一致: {vals}"
        # 三计数器全零
        assert all(run["fake_contract_calls"] == 0 for run in result["runs"])
        assert all(run["unexpected_external_calls"] == 0 for run in result["runs"])
        assert all(run["real_http_calls"] == 0 for run in result["runs"])

    def test_output_dir_isolation_by_scale_mapping_batch(self, tmp_path):
        out_root = tmp_path / "results"
        bp.run_pipeline(
            tmp_path / "run_a", records=6, mappings=1, repeat=1, output_dir=out_root
        )
        # batch 目录名取 int(time.time())——同秒两次 run 会落入同一 batch 目录
        # 互相覆盖（R2-A 空壳化后流水线更快，同秒碰撞概率显著上升）。
        # 本用例意图是"不同 batch 落不同目录"，跨秒等待即恢复该隔离前提。
        time.sleep(1.05)
        bp.run_pipeline(
            tmp_path / "run_b", records=6, mappings=1, repeat=1, output_dir=out_root
        )
        batches = list((out_root / "records-6" / "mapping-1").glob("batch-*"))
        assert len(batches) >= 2
        for batch in batches:
            assert (batch / "pipeline_results.json").exists()
            assert (batch / "pipeline_runs.csv").exists()

    def test_cli_emits_json_with_requested_parameters(self, tmp_path):
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(bp.__file__).resolve()),
                "--records", "6",
                "--mappings", "2",
                "--repeat", "1",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(completed.stdout)
        assert payload["parameters"] == {
            "records": 6,
            "mappings": 2,
            "repeat": 1,
            "max_seconds": None,
            "dup_rate": 0.25,
            "t0_instrument": False,
            "track_memory": False,
        }
        # 三计数器全零
        assert payload["runs"][0]["fake_contract_calls"] == 0
        assert payload["runs"][0]["unexpected_external_calls"] == 0
        assert payload["runs"][0]["real_http_calls"] == 0
        # JSON 含三口径输出
        assert "records_per_mapping" in payload["metadata"]
        assert "actual_a_files" in payload["runs"][0]
        assert "actual_webdav_paths" in payload["runs"][0]

    def test_cli_exit_nonzero_on_correctness_failure(self, tmp_path, monkeypatch):
        """正确性失败但耗时达标时退出码非零（即使 max_seconds 缺省）。"""
        orig_build = bp.build_fixture

        def _corrupt_build(base_dir, records, mappings=2, dup_rate=0.25):
            fixture = orig_build(
                base_dir, records=records, mappings=mappings, dup_rate=dup_rate)
            fixture.categories["new_in_a"] += 1  # 虚增 expected_copy
            return fixture

        monkeypatch.setattr(bp, "build_fixture", _corrupt_build)
        monkeypatch.setattr(sys, "argv", [
            "benchmark_startup_pipeline.py",
            "--records", "8", "--mappings", "2", "--repeat", "1",
            "--max-seconds", "3600",
        ])
        exit_code = bp.main()
        assert exit_code != 0, "正确性失败时 CLI 应返回非零退出码"

    def test_invalid_cli_parameters_are_rejected(self):
        parser = bp.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--records", "0"])
        with pytest.raises(SystemExit):
            parser.parse_args(["--mappings", "3"])
        with pytest.raises(SystemExit):
            parser.parse_args(["--repeat", "0"])
        with pytest.raises(SystemExit):
            parser.parse_args(["--max-seconds", "-1"])


class TestTimingWindowsAndGate1:
    def test_timing_windows_are_documented(self):
        assert set(bp.TIMING_WINDOWS) == {
            "window_1_database_schema_creation",
            "window_2_start_main_sync_admission",
            "window_3_worker_app_service_start",
            "window_4_http_request_to_ready_total",
        }

    def test_gate1a_measurement_structure(self):
        class _FakeServer:
            def __init__(self):
                self.calls = 0

            def start_main(self):
                time.sleep(0.001)
                self.calls += 1
                return {"success": True}

            def stop_main(self):
                return {"success": True}

        res = bp.measure_gate1a_admission(_FakeServer(), iterations=5)
        assert res["gate"].startswith("Gate 1A")
        assert res["iterations"] == 5
        assert 0 < res["median_seconds"] < 1.0
        assert 0 < res["p95_seconds"] < 1.0
        assert "200ms" in res["applicable_target"]
        assert isinstance(res["passed_200ms"], bool)

    def test_gate1b_measurement_structure(self):
        calls = {"count": 0}

        def _fake_http_post() -> tuple[int, dict]:
            time.sleep(0.001)
            calls["count"] += 1
            return 200, {"ok": True}

        res = bp.measure_gate1b_http_admission(_fake_http_post, iterations=5)
        assert res["gate"].startswith("Gate 1B")
        assert res["iterations"] == 5
        assert calls["count"] == 5
        assert 0 < res["median_seconds"] < 1.0
        assert 0 < res["p95_seconds"] < 1.0
