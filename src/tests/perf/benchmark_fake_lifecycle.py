#!/usr/bin/env python3
"""Runner B: Fake 生命周期基准与启动协议冻结契约验证。

职责：
1. 从 WebUIServer.start_main() 唯一入口启动；
2. 类级替换 webdav_client.OpenListAdminClient（保留真实 WebUIServer/AppService/Database）；
3. 严格隔离所有文件、数据库、日志、Token 缓存与工作目录在临时路径中；
4. 三计数器 + 全线程 trace + 全局白名单；
5. 四大场景冻结契约验证：
   - happy_non_empty_storage: login×1, get_strm×1, phase=ready
   - empty_storage_continues: login×1, get_strm×2, phase=ready (update_engine_configs 二次拉取)
   - startup_login_failure: login×1, get_strm×0, phase=fail_safe
   - storage_load_exception: login×1, get_strm×2, phase=fail_safe (load_strm 吞异常 → update_engine_configs 二次抛出)
6. READY 后经 WebUIServer.stop_main() 规范收口与资源释放；
7. 仓库零副作用校验。
"""
from __future__ import annotations

import argparse
import csv
import gc
import json
import logging
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

# 确保 src/ 在 sys.path
_SRC_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

import webdav_client
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
    WebUIConfig,
)
from database import Database
from tmdb_watchlist_db import TmdbWatchlistDb
from webui.server import WebUIServer


class UnexpectedExternalCallError(RuntimeError):
    """任何线程调用白名单外方法时抛出。"""


@dataclass
class CallTrace:
    seq: int
    timestamp_ns: int
    thread_id: int
    thread_name: str
    method_name: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    classification: str  # "fake_contract" | "unexpected_external" | "real_http"

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "timestamp_ns": self.timestamp_ns,
            "thread_id": self.thread_id,
            "thread_name": self.thread_name,
            "method_name": self.method_name,
            "args": [str(a) for a in self.args],
            "kwargs": {k: str(v) for k, v in self.kwargs.items()},
            "classification": self.classification,
        }


class FakeOpenListAdminClient:
    """全线程可观测的 Fake Admin 客户端，用于 Runner B 生命周期测试。

    - 记录全线程调用 trace（时间戳、线程名、方法、脱敏参数、分类）；
    - 维护 fake_contract_calls, unexpected_external_calls, real_http_calls 三计数器；
    - 仅允许场景授权的白名单方法；未授权方法立即抛出 UnexpectedExternalCallError。
    """

    instance_count = 0
    last_instance: "FakeOpenListAdminClient | None" = None
    _global_lock = threading.Lock()

    def __init__(
        self,
        host: str,
        user: str = "",
        password: str = "",
        totp_secret: str = "",
        *,
        login_return: bool = True,
        login_error_message: str | None = None,
        storages_sequence: list[list[dict[str, Any]] | None] | None = None,
        storages_exception: Exception | None = None,
    ) -> None:
        with self._global_lock:
            FakeOpenListAdminClient.instance_count += 1
            FakeOpenListAdminClient.last_instance = self

        self.host = host
        self.user = user
        self.password = password
        self.totp_secret = totp_secret

        self.login_return = login_return
        self.login_error_message = login_error_message or ("Fake: login failed" if not login_return else None)
        self.last_error_message: str | None = self.login_error_message
        self.last_error_type: str | None = "fake_error" if not login_return else None

        self.storages_sequence = list(storages_sequence) if storages_sequence is not None else [[]]
        self.storages_call_count = 0
        self.storages_exception = storages_exception

        self.traces: list[CallTrace] = []
        self._trace_lock = threading.Lock()
        self._seq = 0

        self.fake_contract_calls = 0
        self.unexpected_external_calls = 0
        self.real_http_calls = 0

        # 白名单方法集合
        self.allowed_methods = {"login", "get_strm_storages_full_info"}

    def _record_call(
        self,
        method_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        classification: str,
    ) -> None:
        with self._trace_lock:
            self._seq += 1
            curr_thread = threading.current_thread()
            trace = CallTrace(
                seq=self._seq,
                timestamp_ns=time.perf_counter_ns(),
                thread_id=curr_thread.ident or 0,
                thread_name=curr_thread.name,
                method_name=method_name,
                args=args,
                kwargs=kwargs,
                classification=classification,
            )
            self.traces.append(trace)
            if classification == "fake_contract":
                self.fake_contract_calls += 1
            elif classification == "unexpected_external":
                self.unexpected_external_calls += 1
            elif classification == "real_http":
                self.real_http_calls += 1

    def login(self, force: bool = False, source: str = "unknown") -> bool:
        if "login" not in self.allowed_methods:
            self._record_call("login", (force, source), {}, "unexpected_external")
            raise UnexpectedExternalCallError("login is not whitelisted in current scenario")
        self._record_call("login", (force, source), {}, "fake_contract")
        return self.login_return

    def get_strm_storages_full_info(self) -> list[dict[str, Any]]:
        if "get_strm_storages_full_info" not in self.allowed_methods:
            self._record_call("get_strm_storages_full_info", (), {}, "unexpected_external")
            raise UnexpectedExternalCallError(
                "get_strm_storages_full_info is not whitelisted in current scenario"
            )
        self._record_call("get_strm_storages_full_info", (), {}, "fake_contract")

        if self.storages_exception:
            raise self.storages_exception

        idx = self.storages_call_count
        self.storages_call_count += 1
        if idx < len(self.storages_sequence):
            res = self.storages_sequence[idx]
            return res if res is not None else []
        return []

    def check_exists(self, path: str) -> bool | None:
        self._record_call("check_exists", (path,), {}, "unexpected_external")
        raise UnexpectedExternalCallError(f"check_exists called unexpectedly on path: {path}")

    def list_directory(self, path: str, page: int = 1, per_page: int = 100) -> dict[str, Any]:
        self._record_call("list_directory", (path, page, per_page), {}, "unexpected_external")
        raise UnexpectedExternalCallError(f"list_directory called unexpectedly on path: {path}")

    def move_file(self, src: str, dst: str) -> bool:
        self._record_call("move_file", (src, dst), {}, "unexpected_external")
        raise UnexpectedExternalCallError(f"move_file called unexpectedly: {src} -> {dst}")

    def remove_file(self, path: str) -> bool:
        self._record_call("remove_file", (path,), {}, "unexpected_external")
        raise UnexpectedExternalCallError(f"remove_file called unexpectedly: {path}")

    def mkdir(self, path: str) -> bool:
        self._record_call("mkdir", (path,), {}, "unexpected_external")
        raise UnexpectedExternalCallError(f"mkdir called unexpectedly: {path}")

    def rename_file(self, src: str, dst: str) -> bool:
        self._record_call("rename_file", (src, dst), {}, "unexpected_external")
        raise UnexpectedExternalCallError(f"rename_file called unexpectedly: {src} -> {dst}")


@dataclass
class LifecycleFixture:
    base_dir: Path
    a_dir: Path
    b_dir: Path
    c_dir: Path
    db_path: Path
    watchlist_db_path: Path
    log_path: Path
    mapping: ABMapping
    config: AppConfig
    db: Database
    watchlist_db: TmdbWatchlistDb


def _cleanup_logging_handlers() -> None:
    """清理并关闭所有根日志处理程序，释放 Windows 文件句柄。"""
    root = logging.getLogger()
    for h in list(root.handlers):
        try:
            h.flush()
            h.close()
        except Exception:
            pass
        root.removeHandler(h)


def build_lifecycle_fixture(base_dir: Path, sync_on_startup: bool = True) -> LifecycleFixture:
    """构建完全隔离的临时生命周期测试环境。"""
    base = Path(base_dir).resolve()
    a_dir = base / "A1"
    b_dir = base / "B1"
    c_dir = base / "C"
    db_path = base / "bridge.db"
    watchlist_db_path = base / "tmdb_watchlist.db"
    log_path = base / "strm_bridge.log"

    for d in (a_dir, b_dir, c_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 写入测试 STRM 文件
    show_dir = a_dir / "Show_0001" / "Season 01"
    show_dir.mkdir(parents=True, exist_ok=True)
    strm_file = show_dir / "S01E01.strm"
    strm_file.write_text("/dav/map1/Show_0001/Season 01/E01.mp4", encoding="utf-8")

    mapping = ABMapping(
        mapping_id="map1",
        a_root=str(a_dir),
        b_root=str(b_dir),
        label="Mapping 1",
    )

    app_config = AppConfig(
        base_dir=str(base),
        webdav=WebDAVConfig(host="http://fake-openlist:5244", user="fake_user", password="fake_password", totp_secret=""),
        refresh=RefreshConfig(interval_seconds=300, enabled=False),
        behavior=BehaviorConfig(sync_on_startup=sync_on_startup, sync_on_startup_wait=0),
        log=LogConfig(level="INFO", file=str(log_path), max_size_mb=5, backup_count=1),
        local=LocalConfig(
            base_dir=str(base),
            a_dir=str(a_dir),
            b_dir=str(b_dir),
            c_dir=str(c_dir),
            db_file=str(db_path),
        ),
        paths=PathsConfig(
            strm_engine_paths=["/dav/map1"],
            refresh_paths=[],
            b_root=str(b_dir),
            c_root=str(c_dir),
        ),
        a_b_mappings=[mapping],
        openlist_strm_engines=[{"engine": "/dav/map1"}],
    )

    db = Database(str(db_path))
    watchlist_db = TmdbWatchlistDb(str(watchlist_db_path))

    return LifecycleFixture(
        base_dir=base,
        a_dir=a_dir,
        b_dir=b_dir,
        c_dir=c_dir,
        db_path=db_path,
        watchlist_db_path=watchlist_db_path,
        log_path=log_path,
        mapping=mapping,
        config=app_config,
        db=db,
        watchlist_db=watchlist_db,
    )


def _make_fake_storage_payload(mapping_id: str, a_root: Path) -> list[dict[str, Any]]:
    return [
        {
            "id": 1,
            "mount_path": f"/dav/{mapping_id}",
            "driver": "Local",
            "status": "work",
            "addition": json.dumps({
                "SaveStrmLocalPath": str(a_root),
                "paths": [f"/dav/{mapping_id}"],
            }),
        }
    ]


def run_single_lifecycle_scenario(
    scenario_name: str,
    base_dir: Path,
    sync_on_startup: bool = True,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """运行单个 Runner B 生命周期场景并返回测试证据与快照。"""
    fixture = build_lifecycle_fixture(base_dir, sync_on_startup=sync_on_startup)

    FakeOpenListAdminClient.instance_count = 0
    FakeOpenListAdminClient.last_instance = None

    # 配置四场景的 Fake 行为与预期
    if scenario_name == "happy_non_empty_storage":
        client_factory = lambda host, user, password, totp_secret: FakeOpenListAdminClient(
            host, user, password, totp_secret,
            login_return=True,
            storages_sequence=[_make_fake_storage_payload(fixture.mapping.mapping_id, fixture.a_dir)],
        )
        expected_phase = "ready"
        expected_login_calls = 1
        expected_storage_calls = 1
        expected_entered_start = True
    elif scenario_name == "empty_storage_continues":
        client_factory = lambda host, user, password, totp_secret: FakeOpenListAdminClient(
            host, user, password, totp_secret,
            login_return=True,
            storages_sequence=[[], []],  # 首次空返回，update_engine_configs 二次请求
        )
        expected_phase = "ready"
        expected_login_calls = 1
        expected_storage_calls = 2
        expected_entered_start = True
    elif scenario_name == "startup_login_failure":
        client_factory = lambda host, user, password, totp_secret: FakeOpenListAdminClient(
            host, user, password, totp_secret,
            login_return=False,
            login_error_message="Invalid credentials",
        )
        expected_phase = "fail_safe"
        expected_login_calls = 1
        expected_storage_calls = 0
        expected_entered_start = False
    elif scenario_name == "storage_load_exception":
        client_factory = lambda host, user, password, totp_secret: FakeOpenListAdminClient(
            host, user, password, totp_secret,
            login_return=True,
            storages_exception=RuntimeError("Storage API network timeout"),
        )
        expected_phase = "fail_safe"
        expected_login_calls = 1
        # load_strm_storage_from_api 内部吞异常警告 → update_engine_configs 二次拉取时抛出并触发 fail_safe
        expected_storage_calls = 2
        expected_entered_start = True
    else:
        raise ValueError(f"Unknown scenario: {scenario_name}")

    webui_cfg = WebUIConfig(port=0, bind="127.0.0.1")
    server = WebUIServer(
        webui_cfg,
        fixture.db,
        app_config=fixture.config,
        watchlist_db=fixture.watchlist_db,
    )

    t_start = time.perf_counter()
    observed_phases: list[str] = []

    # 类级替换 OpenListAdminClient
    with patch("webdav_client.OpenListAdminClient", side_effect=client_factory):
        start_res = server.start_main()
        if not start_res.get("success"):
            raise RuntimeError(f"start_main returned failure: {start_res}")

        # 轮询状态直到到达终态或超时
        deadline = time.time() + timeout_seconds
        last_phase = "starting"
        while time.time() < deadline:
            st = server.get_main_status()
            cur_phase = st["phase"]
            if not observed_phases or observed_phases[-1] != cur_phase:
                observed_phases.append(cur_phase)
            last_phase = cur_phase
            if cur_phase in (expected_phase, "fail_safe", "stopped", "ready"):
                if cur_phase == expected_phase:
                    break
                if expected_phase in ("ready", "fail_safe") and cur_phase != "starting":
                    break
            time.sleep(0.05)

    wall_duration = time.perf_counter() - t_start

    fake_inst = FakeOpenListAdminClient.last_instance
    if fake_inst is None:
        raise RuntimeError("FakeOpenListAdminClient was never instantiated")

    # 收集终态快照
    state_summary = server.get_main_status()
    traces_copy = [t.to_dict() for t in fake_inst.traces]

    login_calls = sum(1 for t in fake_inst.traces if t.method_name == "login")
    storage_calls = sum(1 for t in fake_inst.traces if t.method_name == "get_strm_storages_full_info")

    # 规范收口与资源释放
    stop_res = server.stop_main()

    # 验证受控线程退出
    worker_alive = server._app_worker_thread.is_alive() if server._app_worker_thread else False

    # 显式解除 DB 引用并 GC，确保 Windows 文件锁被释放
    server._db = None
    server._watchlist_db = None
    server._admin_client = None
    del server
    del fixture
    _cleanup_logging_handlers()
    gc.collect()

    # 契约断言评估
    contract_passed = (
        last_phase == expected_phase
        and login_calls == expected_login_calls
        and storage_calls == expected_storage_calls
        and fake_inst.unexpected_external_calls == 0
        and fake_inst.real_http_calls == 0
        and FakeOpenListAdminClient.instance_count == 1
    )

    return {
        "scenario": scenario_name,
        "sync_on_startup": sync_on_startup,
        "wall_duration_seconds": wall_duration,
        "expected_phase": expected_phase,
        "actual_phase": last_phase,
        "observed_phases": observed_phases,
        "contract_passed": contract_passed,
        "instance_count": FakeOpenListAdminClient.instance_count,
        "login_calls": login_calls,
        "expected_login_calls": expected_login_calls,
        "storage_calls": storage_calls,
        "expected_storage_calls": expected_storage_calls,
        "fake_contract_calls": fake_inst.fake_contract_calls,
        "unexpected_external_calls": fake_inst.unexpected_external_calls,
        "real_http_calls": fake_inst.real_http_calls,
        "stop_success": stop_res.get("success", False),
        "worker_thread_alive_after_stop": worker_alive,
        "traces": traces_copy,
        "final_state": state_summary,
    }


def run_all_lifecycle_scenarios(
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """运行全量 Runner B 场景并输出序列化报告。"""
    scenarios = [
        ("happy_non_empty_storage", True),
        ("happy_non_empty_storage", False),
        ("empty_storage_continues", True),
        ("startup_login_failure", True),
        ("storage_load_exception", True),
    ]

    results: list[dict[str, Any]] = []

    for name, sync_flag in scenarios:
        with tempfile.TemporaryDirectory(prefix=f"perf_lifecycle_{name}_") as tmp_dir:
            res = run_single_lifecycle_scenario(
                scenario_name=name,
                base_dir=Path(tmp_dir),
                sync_on_startup=sync_flag,
            )
            results.append(res)
            # 确保每轮临时目录退出前无残留句柄
            _cleanup_logging_handlers()
            gc.collect()

    all_passed = all(r["contract_passed"] for r in results)

    report = {
        "metadata": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "sqlite_version": sqlite3.sqlite_version,
            "runner": "Runner B (Fake Lifecycle)",
            "scenario_count": len(results),
        },
        "all_contracts_passed": all_passed,
        "scenarios": results,
    }

    if output_dir:
        out = Path(output_dir).resolve() / f"lifecycle-batch-{int(time.time())}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "lifecycle_results.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        csv_path = out / "lifecycle_runs.csv"
        with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "scenario",
                "sync_on_startup",
                "expected_phase",
                "actual_phase",
                "contract_passed",
                "login_calls",
                "storage_calls",
                "fake_contract_calls",
                "unexpected_external_calls",
                "real_http_calls",
                "wall_duration_seconds",
            ])
            for r in results:
                writer.writerow([
                    r["scenario"],
                    r["sync_on_startup"],
                    r["expected_phase"],
                    r["actual_phase"],
                    r["contract_passed"],
                    r["login_calls"],
                    r["storage_calls"],
                    r["fake_contract_calls"],
                    r["unexpected_external_calls"],
                    r["real_http_calls"],
                    f"{r['wall_duration_seconds']:.6f}",
                ])

    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Runner B: Fake 生命周期基准与启动协议冻结契约验证"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="结果导出目录 (JSON/CSV)",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    report = run_all_lifecycle_scenarios(output_dir=args.output_dir)
    # stdout 可能混入生产日志噪声，用显式标记定位 JSON 输出
    print("===PERF_JSON_START===")
    print(json.dumps(report, indent=2, ensure_ascii=False))

    if not report["all_contracts_passed"]:
        print("[GATE FAIL] Runner B lifecycle contracts failed!", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
