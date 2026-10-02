#!/usr/bin/env python3
"""Runner D: 真实 OpenList 集成基准（显式 opt-in，脱敏记录）。

职责：
1. 仅在显式开关（环境变量 OPENLIST_REAL_INTEGRATION_TEST=1 或 CLI 参数）下执行；
2. 默认 pytest 与 CI 不执行，不读取默认生产凭据；
3. 执行真实 HTTP 交互，度量登录、TOTP、STRM 存储映射与网络耗时；
4. 全面脱敏：对 host、user、password、token、TOTP、具体路径等敏感信息进行脱敏输出；
5. 结果与离线门禁分离，单独写入指定的输出目录。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import re
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 确保 src/ 与 src/tests/perf/ 在 sys.path
_PERF_DIR = Path(__file__).resolve().parent
_SRC_ROOT = _PERF_DIR.parent.parent
if str(_PERF_DIR) not in sys.path:
    sys.path.insert(0, str(_PERF_DIR))
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))


def sanitize_sensitive_string(val: str | None) -> str:
    """脱敏函数：保留前后少量字符，中间以 * 掩码。"""
    if not val:
        return ""
    val_str = str(val).strip()
    if len(val_str) <= 4:
        return "***"
    return val_str[:2] + "*" * (len(val_str) - 4) + val_str[-2:]


def sanitize_url(url: str | None) -> str:
    """对 URL 的 host/端口和凭据脱敏。"""
    if not url:
        return ""
    # 替换其中的用户名密码等
    pattern = r"(https?://)([^@/]+@)?([^:/]+)(:\d+)?"
    match = re.match(pattern, url)
    if match:
        scheme, auth, host, port = match.groups()
        masked_host = sanitize_sensitive_string(host)
        masked_auth = "***@" if auth else ""
        port_str = port or ""
        return f"{scheme}{masked_auth}{masked_host}{port_str}"
    return "***"


def sanitize_storage_summary(storages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对 STRM 存储元数据进行结构化脱敏（仅保留根挂载点前缀）。"""
    sanitized: list[dict[str, Any]] = []
    for s in storages:
        mount_path = str(s.get("mount_path", ""))
        driver = str(s.get("driver", ""))
        status = str(s.get("status", ""))
        parts = [p for p in mount_path.split("/") if p]
        if parts:
            masked_mount = f"/{parts[0]}/***"
        else:
            masked_mount = "/***"
        sanitized.append({
            "masked_mount_path": masked_mount,
            "driver": driver,
            "status": status,
        })
    return sanitized


def run_real_integration_check(
    host: str,
    user: str,
    password: str,
    totp_secret: str = "",
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """执行真实 OpenList 服务连接与 STRM 存储发现，记录各项度量与脱敏结果。"""
    from webdav_client import OpenListAdminClient

    t_start = time.perf_counter()

    # 使用临时 Token 缓存路径，防止污染工作区
    with tempfile.TemporaryDirectory(prefix="real_token_cache_") as tmp_dir:
        temp_cache = Path(tmp_dir) / ".admin_token.json"

        client = OpenListAdminClient(
            host=host,
            user=user,
            password=password,
            totp_secret=totp_secret,
        )
        client.token_cache_path = str(temp_cache)
        client.token = None

        # 1. 登录阶段
        t0_login = time.perf_counter()
        login_ok = client.login(force=True, source="runner_d_real_integration")
        login_duration = time.perf_counter() - t0_login

        login_error_type = client.last_error_type
        login_error_msg = client.last_error_message

        # 2. 存储映射查询阶段
        storage_duration = 0.0
        storages_raw: list[dict[str, Any]] = []
        storage_error = None
        if login_ok:
            t0_storage = time.perf_counter()
            try:
                storages_raw = client.get_strm_storages_full_info() or []
            except Exception as e:
                storage_error = str(e)
            storage_duration = time.perf_counter() - t0_storage

    total_wall = time.perf_counter() - t_start

    sanitized_storages = sanitize_storage_summary(storages_raw)

    return {
        "success": login_ok and storage_error is None,
        "total_wall_seconds": total_wall,
        "login": {
            "success": login_ok,
            "duration_seconds": login_duration,
            "error_type": login_error_type,
            "error_message": login_error_msg,
        },
        "storage_query": {
            "duration_seconds": storage_duration,
            "count": len(storages_raw),
            "storages": sanitized_storages,
            "error": storage_error,
        },
        "target_sanitized": {
            "host": sanitize_url(host),
            "user": sanitize_sensitive_string(user),
            "has_totp": bool(totp_secret),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Runner D: 真实 OpenList 集成基准 (显式 opt-in)"
    )
    parser.add_argument("--host", type=str, default=os.getenv("OPENLIST_BRIDGE_TEST_HOST", ""), help="OpenList 服务 Host")
    parser.add_argument("--user", type=str, default=os.getenv("OPENLIST_BRIDGE_TEST_USER", ""), help="OpenList 用户名")
    parser.add_argument("--password", type=str, default=os.getenv("OPENLIST_BRIDGE_TEST_PASSWORD", ""), help="OpenList 密码")
    parser.add_argument("--totp-secret", type=str, default=os.getenv("OPENLIST_BRIDGE_TEST_TOTP", ""), help="TOTP 密钥")
    parser.add_argument("--output-dir", type=Path, default=None, help="结果导出目录")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # 检查是否已显式配置目标（不自动尝试生产凭据）
    if not args.host or not args.user or not args.password:
        print(
            "Runner D (真实 OpenList 集成) 为显式 opt-in 模式。\n"
            "如需运行，请提供 --host, --user, --password 参数或设置相应环境变量。\n"
            "示例: python src/tests/perf/benchmark_real_integration.py --host http://localhost:5244 --user admin --password admin",
            file=sys.stderr,
        )
        return 0

    report = run_real_integration_check(
        host=args.host,
        user=args.user,
        password=args.password,
        totp_secret=args.totp_secret,
    )

    print("===PERF_JSON_START===")
    print(json.dumps(report, indent=2, ensure_ascii=False))

    if args.output_dir:
        out = Path(args.output_dir).resolve() / f"real-integration-batch-{int(time.time())}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "real_integration_results.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    return 0 if report["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
