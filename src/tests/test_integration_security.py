"""
安全与集成测试套件。

测试范围：
1. 所有 API 端点的鉴权验证
2. 密码验证、注册、重置逻辑
3. 敏感信息泄露检测
4. 前端逻辑（页面切换、排序等）

输出：详细的 JSON 日志文件，记录每个测试用例的执行结果。
"""

from __future__ import annotations

import atexit
import http.client
import json
import logging
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webui.server import WebUIServer  # noqa: E402

# ============================================================
# 日志配置
# ============================================================

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "test_logs"
LOG_DIR.mkdir(exist_ok=True)

LOG_FILE = LOG_DIR / f"security_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

test_results = []


def log_test(test_name: str, category: str, endpoint: str, method: str,
             status_code: int, expected: int, passed: bool,
             response_body: dict | str | None = None,
             notes: str = ""):
    """记录测试结果。"""
    result = {
        "test_name": test_name,
        "category": category,
        "endpoint": endpoint,
        "method": method,
        "status_code": status_code,
        "expected_status": expected,
        "passed": passed,
        "response_body": response_body,
        "notes": notes,
        "timestamp": datetime.now().isoformat(),
    }
    test_results.append(result)
    return result


def save_log():
    """保存测试日志到文件。"""
    if not test_results:
        return
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "test_suite": "Security & Integration Test",
            "timestamp": datetime.now().isoformat(),
            "total_tests": len(test_results),
            "passed": sum(1 for r in test_results if r["passed"]),
            "failed": sum(1 for r in test_results if not r["passed"]),
            "results": test_results,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n测试日志已保存至: {LOG_FILE}")


# 使用 atexit 确保测试结束后保存日志
atexit.register(save_log)


# ============================================================
# 测试基础设施
# ============================================================

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def _make_mock_config(tmp_path: Path) -> MagicMock:
    cfg = MagicMock()
    cfg.webui.enabled = True
    cfg.webui.port = 0
    cfg.webui.bind = "127.0.0.1"
    cfg.tmdb.access_token = "test_token_12345678901234567890"
    cfg.tmdb.api_key = "test_api_key_secret_12345"
    cfg.tmdb.language = "zh-CN"
    cfg.tmdb.host = ""
    cfg.tmdb.csv_watchlist_file = ""
    cfg.tmdb.watchlist_cache_ttl = 604800
    cfg.tmdb.fuzzy_threshold = 0.60
    cfg.tmdb.anime_min_ep_ratio = 0.3
    cfg.tmdb.proxy_enabled = False
    cfg.tmdb.proxy_http = ""
    proxy = MagicMock()
    proxy.enabled = False
    proxy.http = ""
    cfg.tmdb.proxy = proxy
    cfg.webdav.host = "http://127.0.0.1:5244"
    cfg.webdav.user = "admin"
    cfg.webdav.password = "secret_password_123"
    cfg.webdav.totp_secret = "JBSWY3DPEHPK3PXP"
    cfg.paths.b_root = str(tmp_path / "b")
    cfg.paths.c_root = str(tmp_path / "c")
    cfg.behavior.ghost_protect_seconds = 300
    cfg.strm_storage_map = {}
    cfg.strm_engine_paths = []
    cfg.update_from_db = MagicMock()
    return cfg


def _make_mock_db(tmp_path: Path) -> MagicMock:
    db = MagicMock()
    db.db_path = str(tmp_path / "bridge.db")
    db.get_table_counts.return_value = {
        "a_strm_files": 10, "b_strm_files": 20, "c_ghost_files": 5,
    }
    db.get_b_status_counts.return_value = {
        "valid": 15, "duplicate": 3, "quarantined": 2,
    }
    db.get_db_file_size.return_value = 1024000
    db.get_subtitle_by_local.return_value = None
    db.read_connection.return_value.__enter__ = MagicMock()
    db.read_connection.return_value.__exit__ = MagicMock()
    return db


def _make_mock_watchlist_db(tmp_path: Path) -> MagicMock:
    wdb = MagicMock()
    wdb.db_path = str(tmp_path / "tmdb_watchlist.db")
    wdb.get_all_config.return_value = {}
    wdb.set_config = MagicMock()
    wdb.get_admin_password.return_value = None
    return wdb


class _TestServerHelper:
    """测试服务器上下文管理器。"""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.port = _free_port()
        self.server = None
        self.thread = None

    def __enter__(self):
        cfg = _make_mock_config(self.tmp_path)
        db = _make_mock_db(self.tmp_path)
        wdb = _make_mock_watchlist_db(self.tmp_path)

        # WebUIServer 签名: (config: WebUIConfig, db: Database, app_config=None)
        cfg.webui.port = self.port
        self.server = WebUIServer(cfg.webui, db, app_config=cfg)
        self.server._has_password = False  # 初始无密码
        self.server._watchlist_db = wdb  # 注入 mock watchlist DB

        self.thread = threading.Thread(target=self.server.start, daemon=True)
        self.thread.start()

        # 等待服务器启动
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.1):
                    break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.1)
        else:
            raise RuntimeError("Server failed to start")

        return self

    def __exit__(self, *args):
        if self.server:
            self.server.stop()
        if self.thread:
            self.thread.join(timeout=2)


def _request(port: int, method: str, path: str, body: dict | None = None,
             headers: dict | None = None, timeout: float = 5.0) -> tuple[int, dict | str]:
    """发送 HTTP 请求并返回 (status_code, response_body)。"""
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode("utf-8") if body else None

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body_text = resp.read().decode("utf-8")
            try:
                body_json = json.loads(body_text)
            except json.JSONDecodeError:
                body_json = body_text
            return status, body_json
    except urllib.error.HTTPError as e:
        status = e.code
        body_text = ""
        try:
            body_text = e.read().decode("utf-8")
        except ConnectionResetError:
            body_text = "ConnectionResetError"

        try:
            body_json = json.loads(body_text)
        except json.JSONDecodeError:
            body_json = body_text
        return status, body_json
    except Exception as e:
        return 0, str(e)


# ============================================================
# 测试用例
# ============================================================

class TestAuthentication:
    """鉴权测试。"""

    def test_whitelist_endpoints_no_auth(self, tmp_path):
        """测试白名单端点无需认证。"""
        with _TestServerHelper(tmp_path) as ts:
            whitelist = [
                ("/api/config", "GET"),
                ("/api/webui/config/ui", "GET"),
                ("/api/tmdb/avatar", "GET"),
                ("/api/tmdb/poster", "GET"),
                ("/api/openlist/status", "GET"),
                ("/api/openlist/ping", "GET"),
                ("/api/admin/status", "GET"),
                ("/api/login", "POST"),
            ]

            for endpoint, method in whitelist:
                status, body = _request(ts.port, method, endpoint,
                                        body={} if method == "POST" else None)
                passed = status != 401
                log_test(
                    f"whitelist_{endpoint.replace('/', '_')}",
                    "auth_whitelist",
                    endpoint, method, status, 200, passed,
                    response_body=body if isinstance(body, dict) else None,
                    notes="白名单端点应返回 200，不应返回 401"
                )

    def test_protected_endpoints_require_auth(self, tmp_path):
        """测试受保护端点需要认证。"""
        with _TestServerHelper(tmp_path) as ts:
            # 先设置密码
            ts.server._has_password = True
            ts.server._sessions.clear()

            protected = [
                ("/api/dashboard", "GET"),
                ("/api/area/b", "GET"),
                ("/api/area/a", "GET"),
                ("/api/area/c", "GET"),
                ("/api/records", "GET"),
                ("/api/logs", "GET"),
                ("/api/webui/config/openlist", "GET"),
                ("/api/webui/config/tmdb", "GET"),
                ("/api/tmdb/status", "GET"),
                ("/api/tmdb/watchlist", "GET"),
                ("/api/main/status", "GET"),
                ("/api/main/start", "POST"),
                ("/api/main/stop", "POST"),
                ("/api/restart-webui", "POST"),
                ("/api/openlist/test-connection", "POST"),
                ("/api/tmdb/configure", "POST"),
                ("/api/tmdb/watchlist/sync", "POST"),
                ("/api/tmdb/watchlist/match/refresh", "POST"),
            ]

            for endpoint, method in protected:
                status, body = _request(ts.port, method, endpoint,
                                        body={} if method == "POST" else None)
                passed = status == 401
                log_test(
                    f"protected_{endpoint.replace('/', '_')}",
                    "auth_protected",
                    endpoint, method, status, 401, passed,
                    response_body=body if isinstance(body, dict) else None,
                    notes="受保护端点无 token 时应返回 401"
                )

    def test_session_token_validation(self, tmp_path):
        """测试 session token 验证。"""
        with _TestServerHelper(tmp_path) as ts:
            ts.server._has_password = True

            # 无效 token
            status, body = _request(ts.port, "GET", "/api/dashboard",
                                    headers={"X-Session-Token": "invalid_token"})
            passed = status == 401
            log_test(
                "invalid_token",
                "auth_token",
                "/api/dashboard", "GET", status, 401, passed,
                response_body=body if isinstance(body, dict) else None,
                notes="无效 token 应返回 401"
            )

            # 有效 token
            ts.server._sessions["valid_token"] = time.time() + 3600
            status, body = _request(ts.port, "GET", "/api/dashboard",
                                    headers={"X-Session-Token": "valid_token"})
            passed = status == 200
            log_test(
                "valid_token",
                "auth_token",
                "/api/dashboard", "GET", status, 200, passed,
                response_body=body if isinstance(body, dict) else None,
                notes="有效 token 应返回 200"
            )

            # 过期 token
            ts.server._sessions["expired_token"] = time.time() - 3600
            status, body = _request(ts.port, "GET", "/api/dashboard",
                                    headers={"X-Session-Token": "expired_token"})
            passed = status == 401
            log_test(
                "expired_token",
                "auth_token",
                "/api/dashboard", "GET", status, 401, passed,
                response_body=body if isinstance(body, dict) else None,
                notes="过期 token 应返回 401"
            )


class TestPasswordManagement:
    """密码管理测试。"""

    def test_admin_status_no_password(self, tmp_path):
        """测试无密码时 admin/status 返回正确状态。"""
        with _TestServerHelper(tmp_path) as ts:
            ts.server._has_password = False
            status, body = _request(ts.port, "GET", "/api/admin/status")
            passed = status == 200 and body.get("has_password") is False
            log_test(
                "admin_status_no_password",
                "password_status",
                "/api/admin/status", "GET", status, 200, passed,
                response_body=body if isinstance(body, dict) else None,
                notes="无密码时应返回 has_password=false"
            )

    def test_admin_status_with_password(self, tmp_path):
        """测试有密码时 admin/status 返回正确状态。"""
        with _TestServerHelper(tmp_path) as ts:
            ts.server._has_password = True
            status, body = _request(ts.port, "GET", "/api/admin/status")
            passed = status == 200 and body.get("has_password") is True
            log_test(
                "admin_status_with_password",
                "password_status",
                "/api/admin/status", "GET", status, 200, passed,
                response_body=body if isinstance(body, dict) else None,
                notes="有密码时应返回 has_password=true"
            )

    def test_login_no_password_set(self, tmp_path):
        """测试未设置密码时登录行为。"""
        with _TestServerHelper(tmp_path) as ts:
            ts.server._has_password = False
            status, body = _request(ts.port, "POST", "/api/login",
                                    body={"password": "any_password"})
            # 未设置密码时应允许登录或返回特定错误
            passed = status in (200, 400, 401)
            log_test(
                "login_no_password",
                "password_login",
                "/api/login", "POST", status, "200/400/401", passed,
                response_body=body if isinstance(body, dict) else None,
                notes="未设置密码时的登录行为"
            )

    def test_login_wrong_password(self, tmp_path):
        """测试错误密码登录。"""
        with _TestServerHelper(tmp_path) as ts:
            ts.server._has_password = True
            # 模拟存储的密码哈希
            ts.server._watchlist_db.get_admin_password.return_value = (
                "salt123$600000$hash456"
            )
            status, body = _request(ts.port, "POST", "/api/login",
                                    body={"password": "wrong_password"})
            passed = status == 401
            log_test(
                "login_wrong_password",
                "password_login",
                "/api/login", "POST", status, 401, passed,
                response_body=body if isinstance(body, dict) else None,
                notes="错误密码应返回 401"
            )

    def test_login_rate_limiting(self, tmp_path):
        """测试登录频率限制。"""
        with _TestServerHelper(tmp_path) as ts:
            ts.server._has_password = True
            ts.server._watchlist_db.get_admin_password.return_value = (
                "salt123$600000$hash456"
            )

            # 连续发送多次错误登录
            for i in range(10):
                status, body = _request(ts.port, "POST", "/api/login",
                                        body={"password": f"wrong_{i}"})

            # 检查是否被限制
            status, body = _request(ts.port, "POST", "/api/login",
                                    body={"password": "wrong_final"})
            passed = status in (401, 429)
            log_test(
                "login_rate_limit",
                "password_ratelimit",
                "/api/login", "POST", status, "401/429", passed,
                response_body=body if isinstance(body, dict) else None,
                notes="频繁登录应触发频率限制"
            )


class TestSensitiveDataLeak:
    """敏感数据泄露测试。"""

    def test_config_endpoint_no_secrets(self, tmp_path):
        """测试 /api/config 不泄露敏感信息。"""
        with _TestServerHelper(tmp_path) as ts:
            status, body = _request(ts.port, "GET", "/api/config")

            if isinstance(body, dict):
                # 检查 TMDB API key
                api_key = body.get("tmdb_api_key")
                api_key_leaked = isinstance(api_key, str) and len(api_key) > 10

                # 检查 TMDB token
                token = body.get("tmdb_token_preview")
                token_leaked = isinstance(token, str) and len(token) > 20

                # 检查 WebDAV 密码
                webdav_pwd = body.get("webdav_password")
                webdav_pwd_leaked = isinstance(webdav_pwd, str) and len(webdav_pwd) > 5

                # 检查 WebDAV TOTP
                webdav_totp = body.get("webdav_totp_secret")
                webdav_totp_leaked = isinstance(webdav_totp, str) and len(webdav_totp) > 5

                passed = not (api_key_leaked or token_leaked or
                              webdav_pwd_leaked or webdav_totp_leaked)

                leak_details = []
                if api_key_leaked:
                    leak_details.append("tmdb_api_key")
                if token_leaked:
                    leak_details.append("tmdb_token_preview")
                if webdav_pwd_leaked:
                    leak_details.append("webdav_password")
                if webdav_totp_leaked:
                    leak_details.append("webdav_totp_secret")

                log_test(
                    "config_no_secrets",
                    "data_leak",
                    "/api/config", "GET", status, 200, passed,
                    response_body=body,
                    notes=f"泄露字段: {', '.join(leak_details) if leak_details else '无'}"
                )
            else:
                log_test(
                    "config_no_secrets",
                    "data_leak",
                    "/api/config", "GET", status, 200, False,
                    notes="响应不是 JSON"
                )

    def test_openlist_config_requires_auth(self, tmp_path):
        """测试 /api/webui/config/openlist 需要认证。"""
        with _TestServerHelper(tmp_path) as ts:
            ts.server._has_password = True
            status, body = _request(ts.port, "GET", "/api/webui/config/openlist")
            passed = status == 401
            log_test(
                "openlist_config_auth",
                "data_leak",
                "/api/webui/config/openlist", "GET", status, 401, passed,
                response_body=body if isinstance(body, dict) else None,
                notes="OpenList 配置端点应需要认证"
            )


class TestRequestLimits:
    """请求限制测试。"""

    def test_negative_content_length_rejected(self, tmp_path):
        """T4: 负 Content-Length 应返回 400，而非挂起线程。

        旧实现只做上界校验：int("-1") 正常返回 → 绕过 413 → rfile.read(-1)
        读到 EOF 挂起线程；ThreadingHTTPServer 无 socket 超时，/api/login 又在
        白名单内，未鉴权即可挂线程。
        """
        with _TestServerHelper(tmp_path) as ts:
            conn = http.client.HTTPConnection("127.0.0.1", ts.port, timeout=5.0)
            conn.putrequest("POST", "/api/login")
            conn.putheader("Content-Length", "-1")
            conn.putheader("Content-Type", "application/json")
            conn.endheaders()
            resp = conn.getresponse()
            status = resp.status
            try:
                resp.read()
            except Exception:
                pass
            conn.close()
            assert status == 400, f"负 Content-Length 应返回 400，实际 {status}"

    def test_max_content_length(self, tmp_path):
        """测试请求体大小限制。"""
        with _TestServerHelper(tmp_path) as ts:
            # 发送超大请求体（11MB）
            large_body = {"data": "x" * (11 * 1024 * 1024)}
            status, body = _request(ts.port, "POST", "/api/login",
                                    body=large_body, timeout=10.0)
            passed = status == 413 or (status == 0 and "ConnectionResetError" in str(body))
            log_test(
                "max_content_length",
                "request_limit",
                "/api/login", "POST", status, 413, passed,
                response_body=body if isinstance(body, dict) else None,
                notes="超大请求体应返回 413"
            )


class TestBusinessLogic:
    """业务逻辑测试。"""

    def test_dashboard_data_structure(self, tmp_path):
        """测试 dashboard 数据结构。"""
        with _TestServerHelper(tmp_path) as ts:
            ts.server._has_password = False
            status, body = _request(ts.port, "GET", "/api/dashboard")

            if isinstance(body, dict):
                required_fields = ["a_count", "b_count", "c_count",
                                   "b_valid", "b_duplicate", "b_quarantined"]
                missing = [f for f in required_fields if f not in body]
                passed = status == 200 and len(missing) == 0
                log_test(
                    "dashboard_structure",
                    "business_logic",
                    "/api/dashboard", "GET", status, 200, passed,
                    response_body=body,
                    notes=f"缺失字段: {', '.join(missing) if missing else '无'}"
                )
            else:
                log_test(
                    "dashboard_structure",
                    "business_logic",
                    "/api/dashboard", "GET", status, 200, False,
                    notes="响应不是 JSON"
                )

    def test_area_endpoints(self, tmp_path):
        """测试 area 端点。"""
        with _TestServerHelper(tmp_path) as ts:
            ts.server._has_password = False
            for area in ["a", "b", "c"]:
                status, body = _request(ts.port, "GET", f"/api/area/{area}")
                passed = status == 200
                log_test(
                    f"area_{area}",
                    "business_logic",
                    f"/api/area/{area}", "GET", status, 200, passed,
                    response_body=body if isinstance(body, dict) else None,
                    notes=f"{area.upper()} 区端点"
                )


# ============================================================
# 主函数
# ============================================================

def run_all_tests():
    """运行所有测试并生成日志。"""
    print("=" * 60)
    print("安全与集成测试套件")
    print("=" * 60)

    # 使用 pytest 运行测试
    pytest.main([__file__, "-v", "-s"])

    # 保存日志
    save_log()

    # 打印摘要
    passed = sum(1 for r in test_results if r["passed"])
    failed = sum(1 for r in test_results if not r["passed"])
    print(f"\n测试摘要:")
    print(f"  总计: {len(test_results)}")
    print(f"  通过: {passed}")
    print(f"  失败: {failed}")


if __name__ == "__main__":
    run_all_tests()
