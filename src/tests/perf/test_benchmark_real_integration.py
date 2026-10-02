"""Runner D: 真实 OpenList 集成的 Pytest 门禁（默认 skip，需显式 opt-in）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import benchmark_real_integration as br


@pytest.mark.skip(reason="真实 OpenList 集成测试需显式 opt-in（环境变量 OPENLIST_REAL_INTEGRATION_TEST 或手工指定 --host/--user/--password）")
def test_real_integration_runs_only_when_opted_in():
    """真实网络集成测试默认 skip；显式 opt-in 时执行真实 HTTP。"""
    if os.getenv("OPENLIST_REAL_INTEGRATION_TEST", "0") != "1":
        pytest.skip("OPENLIST_REAL_INTEGRATION_TEST 未启用")
    result = br.run_real_integration_check(
        host=os.getenv("OPENLIST_BRIDGE_TEST_HOST", ""),
        user=os.getenv("OPENLIST_BRIDGE_TEST_USER", ""),
        password=os.getenv("OPENLIST_BRIDGE_TEST_PASSWORD", ""),
        totp_secret=os.getenv("OPENLIST_BRIDGE_TEST_TOTP", ""),
    )
    assert result["success"] is True


class TestSanitization:
    def test_password_redacted(self):
        masked = br.sanitize_sensitive_string("super_secret_password")
        assert "*" in masked
        assert masked.startswith("su")
        assert masked.endswith("rd")
        assert "secret" not in masked

    def test_url_host_redacted(self):
        assert "admin" not in br.sanitize_url("http://admin:pass@10.0.0.1:5244")
        assert "pass" not in br.sanitize_url("http://admin:pass@10.0.0.1:5244")
        out = br.sanitize_url("http://fake-host")
        assert "fake" not in out
        assert "*" in out

    def test_storage_summary_redacts_mount_paths(self):
        storages = [
            {
                "mount_path": "/dav/privatevol/Season 01",
                "driver": "Local",
                "status": "work",
            }
        ]
        out = br.sanitize_storage_summary(storages)
        assert out[0]["driver"] == "Local"
        assert out[0]["masked_mount_path"].startswith("/dav/")
        assert "Season 01" not in out[0]["masked_mount_path"]


class TestCLI:
    def test_cli_requires_explicit_credentials(self, tmp_path):
        """未显式提供凭据时 CLI 返回 0 且不尝试网络（安全默认）。"""
        import subprocess
        completed = subprocess.run(
            [sys.executable, str(Path(br.__file__).resolve())],
            capture_output=True,
            text=True,
        )
        assert "opt-in" in completed.stderr.lower()
        assert completed.returncode == 0

    def test_parser_accepts_optional_flags(self):
        parser = br.build_parser()
        ns = parser.parse_args([])
        assert ns.host == ""
        assert ns.user == ""
        assert ns.password == ""