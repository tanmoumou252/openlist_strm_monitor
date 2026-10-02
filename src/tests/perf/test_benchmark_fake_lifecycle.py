"""Runner B: Fake 生命周期与启动协议冻结契约的 Pytest 门禁。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import benchmark_fake_lifecycle as bl


class TestFakeOpenListAdminClient:
    def test_fake_traps_unwhitelisted_method_calls(self):
        fake = bl.FakeOpenListAdminClient("http://fake", "u", "p")
        with pytest.raises(bl.UnexpectedExternalCallError):
            fake.check_exists("/test")
        assert fake.unexpected_external_calls == 1
        assert fake.fake_contract_calls == 0
        assert fake.real_http_calls == 0

    def test_fake_whitelisted_calls_counted_correctly(self):
        fake = bl.FakeOpenListAdminClient(
            "http://fake", "u", "p", login_return=True, storages_sequence=[[]]
        )
        assert fake.login(force=True, source="startup") is True
        assert fake.get_strm_storages_full_info() == []
        assert fake.fake_contract_calls == 2
        assert fake.unexpected_external_calls == 0
        assert fake.real_http_calls == 0


class TestRunnerBScenarios:
    def test_happy_non_empty_storage_contract(self, tmp_path):
        """Happy Path：login×1, get_strm×1, 终态 ready。"""
        res = bl.run_single_lifecycle_scenario(
            "happy_non_empty_storage",
            base_dir=tmp_path,
            sync_on_startup=True,
        )
        assert res["contract_passed"] is True
        assert res["actual_phase"] == "ready"
        assert res["login_calls"] == 1
        assert res["storage_calls"] == 1
        assert res["unexpected_external_calls"] == 0
        assert res["real_http_calls"] == 0
        assert res["stop_success"] is True
        assert res["worker_thread_alive_after_stop"] is False
        assert "syncing_a_to_b" in res["observed_phases"]

    def test_happy_without_sync_on_startup(self, tmp_path):
        """sync_on_startup=False：合法跳过 syncing_a_to_b，直接进入 ready。"""
        res = bl.run_single_lifecycle_scenario(
            "happy_non_empty_storage",
            base_dir=tmp_path,
            sync_on_startup=False,
        )
        assert res["contract_passed"] is True
        assert res["actual_phase"] == "ready"
        assert "syncing_a_to_b" not in res["observed_phases"]

    def test_empty_storage_continues_contract(self, tmp_path):
        """存储为空断言恰好 2 次 get_strm，终态 ready。"""
        res = bl.run_single_lifecycle_scenario(
            "empty_storage_continues",
            base_dir=tmp_path,
            sync_on_startup=True,
        )
        assert res["contract_passed"] is True
        assert res["actual_phase"] == "ready"
        assert res["login_calls"] == 1
        assert res["storage_calls"] == 2
        assert res["unexpected_external_calls"] == 0

    def test_startup_login_failure_contract(self, tmp_path):
        """登录失败：login×1, get_strm×0, 终态 fail_safe。"""
        res = bl.run_single_lifecycle_scenario(
            "startup_login_failure",
            base_dir=tmp_path,
            sync_on_startup=True,
        )
        assert res["contract_passed"] is True
        assert res["actual_phase"] == "fail_safe"
        assert res["login_calls"] == 1
        assert res["storage_calls"] == 0
        assert res["final_state"]["running"] is False

    def test_storage_load_exception_contract(self, tmp_path):
        """存储异常：login×1, get_strm×2 (load 内部吞异常 → update_engine_configs 抛出), 终态 fail_safe。"""
        res = bl.run_single_lifecycle_scenario(
            "storage_load_exception",
            base_dir=tmp_path,
            sync_on_startup=True,
        )
        assert res["contract_passed"] is True
        assert res["actual_phase"] == "fail_safe"
        assert res["login_calls"] == 1
        assert res["storage_calls"] == 2
        assert res["final_state"]["running"] is False

    def test_workspace_zero_side_effects(self, tmp_path):
        """断言测试后仓库工作区根目录下未被 Fake 生命周期写入任何新数据或修改 Token 文件。"""
        repo_root = Path(__file__).resolve().parent.parent.parent.parent
        token_file = repo_root / "src" / ".admin_token.json"
        token_mtime_before = token_file.stat().st_mtime_ns if token_file.exists() else None

        res = bl.run_single_lifecycle_scenario(
            "happy_non_empty_storage",
            base_dir=tmp_path,
            sync_on_startup=True,
        )
        assert res["contract_passed"] is True

        token_mtime_after = token_file.stat().st_mtime_ns if token_file.exists() else None
        assert token_mtime_before == token_mtime_after
        # 断言 Fake 零真实 HTTP 且未在仓库根产生 .admin_token.json
        assert not (repo_root / ".admin_token.json").exists()

    def test_cli_runner_b(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(bl.__file__).resolve()),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        # 从显式标记后提取 JSON（stdout 混有日志噪声）
        marker = "===PERF_JSON_START==="
        json_start = completed.stdout.find(marker)
        assert json_start != -1
        payload = json.loads(completed.stdout[json_start + len(marker):])
        assert payload["all_contracts_passed"] is True
        assert len(payload["scenarios"]) == 5
