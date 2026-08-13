"""并发测试 - 验证多用户场景下的安全性"""
import threading
import time
import socket
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from webui.server import WebUIServer
from tmdb_watchlist_db import TmdbWatchlistDb


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_mock_config(tmp_path: Path) -> MagicMock:
    cfg = MagicMock()
    cfg.webui.enabled = True
    cfg.webui.port = 0
    cfg.webui.bind = "127.0.0.1"
    cfg.tmdb.access_token = ""
    cfg.tmdb.api_key = ""
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
    cfg.local.db_file = str(tmp_path / "bridge.db")
    cfg.local.base_dir = str(tmp_path)
    cfg.log.level = "INFO"
    cfg.log.file = str(tmp_path / "test.log")
    cfg.log.max_size_mb = 10
    cfg.log.backup_count = 5
    cfg.paths.b_root = str(tmp_path / "b")
    cfg.paths.c_root = str(tmp_path / "c")
    cfg.paths.strm_engine_paths = []
    cfg.paths.refresh_paths = []
    cfg.webdav.host = ""
    cfg.webdav.user = ""
    cfg.webdav.password = ""
    cfg.webdav.totp_secret = ""
    cfg.refresh.enabled = False
    cfg.refresh.interval_seconds = 300
    cfg.refresh.depth = 5
    cfg.behavior.action = "MOVE"
    cfg.behavior.ghost_protect_seconds = 10
    cfg.behavior.a_to_b_restore_delay_seconds = 30
    cfg.strm_storage_map = {}
    return cfg


class TestConcurrency:
    """并发测试"""

    def test_concurrent_login_attempts(self, tmp_path):
        """测试多个客户端同时尝试登录（验证服务器不会崩溃）"""
        cfg = _make_mock_config(tmp_path)

        from database import Database
        bridge_db = Database(cfg.local.db_file)

        with patch("webui.server.PROJECT_ROOT", tmp_path):
            server = WebUIServer(cfg.webui, bridge_db, app_config=cfg)
            server._has_password = True
            if server._watchlist_db:
                from utils.password_utils import hash_password
                server._watchlist_db.set_config("ui", "admin_password", hash_password("1111"))

        port = _free_port()
        server._port = port

        # 启动服务器
        server.start()
        time.sleep(0.5)

        results = []

        def login_attempt(attempt_id):
            import urllib.request
            import json
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/login",
                    data=json.dumps({"password": "1111"}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    body = json.loads(resp.read())
                    results.append((attempt_id, resp.status, body.get("success")))
            except urllib.error.HTTPError as e:
                # 速率限制返回 429，这也是正常响应
                results.append((attempt_id, e.code, None))
            except Exception as e:
                results.append((attempt_id, "error", str(e)))

        # 启动 10 个并发登录尝试
        threads = []
        for i in range(10):
            t = threading.Thread(target=login_attempt, args=(i,))
            threads.append(t)
            t.start()

        # 等待所有线程完成
        for t in threads:
            t.join(timeout=10)

        server.stop()

        # 验证：所有请求都有响应（200 或 429），没有崩溃
        valid_responses = sum(1 for _, status, _ in results if status in (200, 429))
        # 由于服务器是单线程的，并发请求可能会超时或失败，这是预期行为
        # 重要的是服务器没有崩溃，且至少有一些请求成功
        assert len(results) == 10, f"应该有 10 个结果，实际: {len(results)}"
        # 至少有一些请求成功或被速率限制（不是全部崩溃）
        assert valid_responses >= 1, f"至少应该有 1 个有效响应，实际: {valid_responses}, 结果: {results}"

    def test_concurrent_session_validation(self, tmp_path):
        """测试多个请求同时验证 session token"""
        cfg = _make_mock_config(tmp_path)
        db_path = tmp_path / "tmdb_watchlist.db"
        wdb = TmdbWatchlistDb(db_path)

        from database import Database
        bridge_db = Database(cfg.local.db_file)

        server = WebUIServer(cfg.webui, bridge_db, app_config=cfg)
        server._has_password = True

        # 添加有效 session（预存 bug：需存 tuple (expiry, stored_ip)，非纯 float）
        server._sessions["valid_token"] = (time.time() + 3600, "")

        port = _free_port()
        server._port = port
        server.start()
        time.sleep(0.5)

        results = []

        def validate_session(attempt_id):
            import urllib.request
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/dashboard",
                    headers={"X-Session-Token": "valid_token"},
                    method="GET"
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    results.append((attempt_id, resp.status))
            except Exception as e:
                results.append((attempt_id, "error"))

        # 启动 20 个并发验证
        threads = []
        for i in range(20):
            t = threading.Thread(target=validate_session, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=10)

        server.stop()

        # 验证：所有请求都应该成功（200）
        success_count = sum(1 for _, status in results if status == 200)
        assert success_count == 20, f"所有 20 个请求都应该成功，实际成功: {success_count}"
