"""WebUI 测试共享夹具（纯新增沉淀模块）。

从 test_webui_http.py 抽取的可复用 helper：空闲端口、最小 AppConfig mock、
最小 Database mock 与真实 threading 服务器起停。既有测试文件不回改，
新回归测试统一从这里 import，避免三处重复定义漂移。

用法（pytest fixture 见 conftest.py 的 webui_server_shared）：

    from webui_fixtures import start_webui_server, http_get, http_post
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

from webui.server import WebUIServer

TEST_PASSWORD = "1111"


def free_port() -> int:
    """获取一个空闲端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def make_mock_config(tmp_path: Path) -> MagicMock:
    """构造最小化 AppConfig mock，满足 WebUIServer 初始化需求。"""
    cfg = MagicMock()
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
    cfg.webdav.host = "http://openlist:5244"
    cfg.webdav.user = ""
    cfg.webdav.password = ""
    cfg.webdav.totp_secret = ""
    cfg.paths.b_root = str(tmp_path / "b")
    cfg.paths.c_root = str(tmp_path / "c")
    cfg.behavior.ghost_protect_seconds = 300
    cfg.strm_storage_map = {}
    cfg.strm_engine_paths = []
    cfg.update_from_db = MagicMock()
    cfg.base_dir = str(tmp_path)
    return cfg


def make_mock_db(tmp_path: Path) -> MagicMock:
    """构造最小化 Database mock。"""
    db = MagicMock(spec=["db_path", "get_table_counts", "get_b_status_counts",
                         "get_db_file_size", "get_subtitle_by_local",
                         "read_connection", "get_index_metadata", "get_all_config"])
    db.db_path = str(tmp_path / "bridge.db")
    db.get_table_counts.return_value = {
        "a_strm_files": 0, "b_strm_files": 0, "c_ghost_files": 0,
    }
    db.get_b_status_counts.return_value = {
        "valid": 0, "orphan": 0, "unknown": 0,
    }
    db.get_db_file_size.return_value = 0
    db.get_subtitle_by_local.return_value = None
    db.get_index_metadata.return_value = {"mapping_index_generation": 1, "mapping_index_generation_at": 1000.0}
    db.get_all_config.return_value = {}
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = (0,)
    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn_ctx.__exit__ = MagicMock(return_value=False)
    db.read_connection.return_value = mock_conn_ctx
    return db


def _restore_env(snapshot: dict) -> None:
    """把环境变量还原到快照记录的原始状态。

    必须区分「原本不存在」与「原本有值」：前者用 pop 删除，后者写回原值。
    """
    for key, value in snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def start_webui_server(tmp_path: Path):
    """启动真实 WebUIServer，登录并返回 (server, base_url, session_token)。

    PROJECT_ROOT / STATIC_DIR 被 patch 到 tmp_path，且 patch 在**服务器存活期内
    持续有效**——server.py 的 STATIC_DIR 是模块级全局，_send_static_file、静态
    资源路径守卫与 favicon/logo 处理器都在**请求时**读取它，因此 patch 必须等到
    调用方 server.stop() 才撤销，不能随本函数 return 结束。

    环境变量（WEBUI_TEST_MODE / WEBUI_ADMIN_PASSWORD_FOR_TEST）同样在
    server.stop() 时还原，避免泄漏到同一进程内的后续用例。

    调用方负责在结束时 server.stop()，还原由包装后的 stop 自动完成。
    """
    from webui.routes import _login_attempts
    _login_attempts.clear()

    cfg = make_mock_config(tmp_path)
    db = make_mock_db(tmp_path)
    port = free_port()
    cfg.webui.port = port

    env_snapshot = {
        key: os.environ.get(key)
        for key in ("WEBUI_TEST_MODE", "WEBUI_ADMIN_PASSWORD_FOR_TEST")
    }

    project_root_patch = patch("webui.server.PROJECT_ROOT", tmp_path)
    static_dir_patch = patch("webui.server.STATIC_DIR", tmp_path / "static")
    torn_down = False

    def _teardown():
        # 幂等闩：登录失败路径与外层 except 兜底可能重复触发清理
        nonlocal torn_down
        if torn_down:
            return
        torn_down = True
        static_dir_patch.stop()
        project_root_patch.stop()
        _restore_env(env_snapshot)

    project_root_patch.start()
    try:
        static_dir_patch.start()
        (tmp_path / "static").mkdir(exist_ok=True)
        (tmp_path / "static" / "index.html").write_text(
            "<html><body>test</body></html>", encoding="utf-8")
        (tmp_path / "static" / "assets").mkdir(exist_ok=True)
        (tmp_path / "static" / "assets" / "favicon.ico").write_bytes(b"\x00")

        server = WebUIServer(cfg.webui, db, app_config=cfg)
        os.environ["WEBUI_TEST_MODE"] = "1"
        os.environ["WEBUI_ADMIN_PASSWORD_FOR_TEST"] = TEST_PASSWORD

        original_stop = server.stop

        def stop_and_restore():
            try:
                original_stop()
            finally:
                _teardown()

        server.stop = stop_and_restore

        server.start()
        deadline = time.time() + 2.0
        while not server._server and time.time() < deadline:
            time.sleep(0.05)

        base_url = f"http://127.0.0.1:{port}"
        status, _, body = http_post(base_url, "/api/login", {"password": TEST_PASSWORD})
        if status != 200 or not body.get("token"):
            server.stop()
            raise RuntimeError(f"test server login failed: {status} {body}")
        return server, base_url, body["token"]
    except BaseException:
        _teardown()
        raise


def http_get(base_url: str, path: str, session_token: str | None = None, timeout: float = 3.0):
    """发送 GET 请求并返回 (status, headers, body_dict_or_bytes)。"""
    url = f"{base_url}{path}"
    req = urllib.request.Request(url, method="GET", headers={"X-Session-Token": session_token}) if session_token else urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype:
                return resp.status, resp.headers, json.loads(body)
            return resp.status, resp.headers, body
    except urllib.error.HTTPError as e:
        body = e.read()
        ctype = e.headers.get("Content-Type", "")
        if "application/json" in ctype:
            return e.code, e.headers, json.loads(body)
        return e.code, e.headers, body


def http_post(base_url: str, path: str, data: dict | bytes, session_token: str | None = None,
              timeout: float = 3.0):
    """发送 POST 请求并返回 (status, headers, body_dict_or_bytes)。"""
    url = f"{base_url}{path}"
    body = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Session-Token": session_token} if session_token else {"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype:
                return resp.status, resp.headers, json.loads(raw)
            return resp.status, resp.headers, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        ctype = e.headers.get("Content-Type", "")
        if "application/json" in ctype:
            return e.code, e.headers, json.loads(raw)
        return e.code, e.headers, raw
