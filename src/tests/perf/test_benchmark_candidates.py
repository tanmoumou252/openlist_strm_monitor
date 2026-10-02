from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import benchmark_database_candidates as bc


@pytest.fixture()
def fixture(tmp_path):
    return bc.build_fixture(tmp_path, records=24, mappings=2)


def test_all_candidate_groups_are_digest_equivalent_and_isolated(fixture):
    report = bc.run_all(fixture, repeat=1, warmup=0)
    assert set(report["comparison"]) == set(bc.GROUPS)
    for name, comparison in report["comparison"].items():
        assert comparison["digest_equal"], name
        assert comparison["rollback_isolated"], name
    assert report["comparison"]["identity_projection"]["mapping_isolated"]
    assert report["comparison"]["fts"]["orphan_rowids"] == 0
    assert report["comparison"]["parameter_chunks"]["temp_table_cleaned"]


def test_fts_candidate_preserves_rowids_repeat_and_tokenizer(fixture):
    result = bc.compare_fts(fixture, repeat=2)
    assert result["digest_equal"]
    assert result["rowid_equal"]
    assert result["repeat_equal"]
    assert result["orphan_rowids"] == 0
    assert result["tokenizers"]["unicode61"] in {"unicode61", "simple"}
    assert result["rollback_isolated"]


def test_b_read_candidate_matches_real_database_api(fixture):
    result = bc.compare_b_reads(fixture)
    assert result["digest_equal"]
    assert result["baseline_count"] == 24
    assert result["candidate_count"] == 24
    assert result["candidate_peak_bytes"] >= 0


def test_projection_candidate_keeps_mapping_boundary(fixture):
    result = bc.compare_identity_projection(fixture)
    assert result["digest_equal"]
    assert result["mapping_isolated"]
    assert result["rollback_isolated"]


def test_batch_lock_and_parameter_results_have_required_evidence(fixture):
    batch = bc.compare_batches_and_locks(fixture, batch_sizes=(100, 500))
    assert set(batch["batch_sizes"]) == {100, 500}
    assert all("wal_bytes_delta" in row for row in batch["runs"])
    assert all("lock_wait_seconds" in row for row in batch["runs"])
    params = bc.compare_parameter_chunks(fixture)
    assert params["digest_equal"]
    assert params["rollback_isolated"]
    assert params["parameter_limit"] >= 900


def test_cli_writes_json_and_csv(tmp_path):
    output = tmp_path / "perf-results"
    completed = subprocess.run(
        [sys.executable, str(Path(bc.__file__).resolve()), "--records", "20",
         "--repeat", "1", "--warmup", "0", "--output", str(output)],
        check=True, capture_output=True, text=True,
    )
    payload = json.loads(completed.stdout)
    assert set(payload) >= {"metadata", "runs", "summary", "comparison"}
    assert payload["metadata"]["records"] == 20
    json_path = output / "database_candidates_results.json"
    csv_path = output / "database_candidates_runs.csv"
    assert json_path.exists() and csv_path.exists()
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(saved["runs"]) == 1
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows and {"group", "variant", "seconds", "digest"} <= set(rows[0])


def test_cli_rejects_invalid_values():
    parser = bc.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--records", "0"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--repeat", "0"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--warmup", "-1"])
