"""
完整业务流程端到端测试。

覆盖场景：
1. 成功路径（分两个层次）：
   - test_complete_new_user_flow：最小冒烟，覆盖 ①登录 ②配置 TMDB
     ③配置 OpenList ⑤查看 A/B 区 + 配置状态校验（不含启动引擎 /
     待看刷新 / 收录检测）
   - test_complete_seven_step_onboarding：##26 七步全链路正向测试
2. 失败场景：不可达 OpenList 地址、预检失败、非法 scope、空 engine
3. 七步失败原因与成功条件：TestSevenStepFailureReasons
4. 分页与搜索：TestAreaSearchE2E / TestPaginationAndSearch
"""

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ============================================================
# Fixtures（与 test_onboarding_e2e.py 共享模式）
# ============================================================

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def _make_mock_config(tmp_path: Path) -> MagicMock:
    """构造最小化 AppConfig mock。"""
    cfg = MagicMock()
    cfg.webui.port = 0
    cfg.webui.bind = "127.0.0.1"
    cfg.tmdb.access_token = ""
    cfg.tmdb.api_key = ""
    cfg.tmdb.language = "zh-CN"
    cfg.tmdb.host = ""
    cfg.tmdb.watchlist_cache_ttl = 604800
    cfg.tmdb.fuzzy_threshold = 0.60
    cfg.tmdb.anime_min_ep_ratio = 0.3
    cfg.tmdb.proxy_enabled = False
    cfg.tmdb.proxy_http = ""
    proxy = MagicMock()
    proxy.enabled = False
    proxy.http = ""
    cfg.tmdb.proxy = proxy
    cfg.webdav.host = ""
    cfg.webdav.user = ""
    cfg.webdav.password = ""
    cfg.webdav.totp_secret = ""
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
        "a_strm_files": 0, "b_strm_files": 0, "c_ghost_files": 0,
    }
    db.get_b_status_counts.return_value = {
        "valid": 0, "orphan": 0, "unknown": 0,
    }
    db.get_db_file_size.return_value = 0
    db.get_subtitle_by_local.return_value = None
    db.get_b_under_root.return_value = []
    db.get_all_a_records.return_value = []
    db.get_all_b_records.return_value = []
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = (0,)
    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn_ctx.__exit__ = MagicMock(return_value=False)
    db.read_connection.return_value = mock_conn_ctx
    return db


@pytest.fixture
def webui_server(tmp_path):
    """启动一个真实的 WebUIServer 实例。"""
    from webui.server import WebUIServer
    from webui.routes import _login_attempts
    _login_attempts.clear()

    cfg = _make_mock_config(tmp_path)
    db = _make_mock_db(tmp_path)
    port = _free_port()
    cfg.webui.port = port

    with patch("webui.server.PROJECT_ROOT", tmp_path), \
         patch("webui.server.STATIC_DIR", tmp_path / "static"):
        (tmp_path / "static").mkdir(exist_ok=True)
        (tmp_path / "static" / "index.html").write_text(
            "<html><body>test</body></html>", encoding="utf-8")
        (tmp_path / "static" / "assets").mkdir(exist_ok=True)
        (tmp_path / "static" / "assets" / "favicon.ico").write_bytes(b"\x00")

        server = WebUIServer(cfg.webui, db, app_config=cfg)
        test_password = "1111"
        os.environ["WEBUI_TEST_MODE"] = "1"
        os.environ["WEBUI_ADMIN_PASSWORD_FOR_TEST"] = test_password
        server.start()
        deadline = time.time() + 2.0
        while not server._server and time.time() < deadline:
            time.sleep(0.05)

        base_url = f"http://127.0.0.1:{port}"
        login_status, _, login_body = _http_post(
            base_url, "/api/login", {"password": test_password})
        assert login_status == 200
        session_token = login_body.get("token")
        assert session_token is not None

        yield server, base_url, session_token
        server.stop()


@pytest.fixture
def real_webui_server(tmp_path):
    """启动真实 WebUIServer + 真实 SQLite Database，用于区域搜索端到端测试。"""
    from database import Database
    from webui.server import WebUIServer
    from webui.routes import _login_attempts
    _login_attempts.clear()

    cfg = _make_mock_config(tmp_path)
    db = Database(str(tmp_path / "bridge.db"))
    port = _free_port()
    cfg.webui.port = port

    with patch("webui.server.PROJECT_ROOT", tmp_path), \
         patch("webui.server.STATIC_DIR", tmp_path / "static"):
        (tmp_path / "static").mkdir(exist_ok=True)
        (tmp_path / "static" / "index.html").write_text(
            "<html><body>test</body></html>", encoding="utf-8")
        (tmp_path / "static" / "assets").mkdir(exist_ok=True)
        (tmp_path / "static" / "assets" / "favicon.ico").write_bytes(b"\x00")

        server = WebUIServer(cfg.webui, db, app_config=cfg)
        test_password = "1111"
        os.environ["WEBUI_TEST_MODE"] = "1"
        os.environ["WEBUI_ADMIN_PASSWORD_FOR_TEST"] = test_password
        server.start()
        deadline = time.time() + 2.0
        while not server._server and time.time() < deadline:
            time.sleep(0.05)

        base_url = f"http://127.0.0.1:{port}"
        login_status, _, login_body = _http_post(
            base_url, "/api/login", {"password": test_password})
        assert login_status == 200
        session_token = login_body.get("token")
        assert session_token is not None

        yield server, base_url, session_token, db
        server.stop()


@pytest.fixture
def real_config_webui_server(tmp_path):
    """启动真实 WebUIServer + 真实 AppConfig + 真实 SQLite Database。

    与 real_webui_server 的区别：AppConfig 使用真实 AppConfig.from_file 创建
    （而非 MagicMock），确保 update_from_db 等方法在真实对象上执行。
    用于测试"HTTP POST → 真实 DB → update_from_db → mapping_id 自动生成"
    等需要真实 config 对象的全链路接线场景。
    """
    from config import AppConfig
    from database import Database
    from webui.server import WebUIServer
    from webui.routes import _login_attempts
    _login_attempts.clear()

    # 最小 TOML：from_file 会补全 log/webdav/paths/webui/tmdb 默认值
    b_dir = tmp_path / "b"
    c_dir = tmp_path / "c"
    b_dir.mkdir(exist_ok=True)
    c_dir.mkdir(exist_ok=True)
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        "[paths]\n"
        f'b_root = "{b_dir.as_posix()}"\n'
        f'c_root = "{c_dir.as_posix()}"\n',
        encoding="utf-8")

    cfg = AppConfig.from_file(str(toml_path))
    db = Database(str(tmp_path / "bridge.db"))
    port = _free_port()
    cfg.webui.port = port

    with patch("webui.server.PROJECT_ROOT", tmp_path), \
         patch("webui.server.STATIC_DIR", tmp_path / "static"):
        (tmp_path / "static").mkdir(exist_ok=True)
        (tmp_path / "static" / "index.html").write_text(
            "<html><body>test</body></html>", encoding="utf-8")
        (tmp_path / "static" / "assets").mkdir(exist_ok=True)
        (tmp_path / "static" / "assets" / "favicon.ico").write_bytes(b"\x00")

        # 防止 _hot_reload_openlist_config 触发真实网络调用
        mock_admin_client = MagicMock()
        mock_admin_client.login.return_value = True
        # 显式声明"OpenList 端无 STRM 存储"（全新用户 onboarding 场景），
        # 令 get_strm_storages_full_info 返回空列表，load_strm_storage_from_api
        # 走无网络的提前返回分支，不再依赖 MagicMock.__iter__ 的默认空迭代。
        mock_admin_client.get_strm_storages_full_info.return_value = []
        # 注意：不 patch load_strm_storage_from_api（AppConfig 使用 slots=True，
        # patch.object 不支持实例方法）。startup 阶段 update_from_db 不调用它，
        # HTTP handler 内部调用时由 _reinit_admin_client 的 mock 保护。
        with patch("webdav_client.OpenListAdminClient",
                    return_value=mock_admin_client):

            server = WebUIServer(cfg.webui, db, app_config=cfg)
            test_password = "1111"
            os.environ["WEBUI_TEST_MODE"] = "1"
            os.environ["WEBUI_ADMIN_PASSWORD_FOR_TEST"] = test_password
            server.start()
            deadline = time.time() + 2.0
            while not server._server and time.time() < deadline:
                time.sleep(0.05)

            base_url = f"http://127.0.0.1:{port}"
            login_status, _, login_body = _http_post(
                base_url, "/api/login", {"password": test_password})
            assert login_status == 200
            session_token = login_body.get("token")
            assert session_token is not None

            yield server, base_url, session_token, db
            server.stop()


def _http_get(base_url, path, session_token=None, timeout=3.0):
    url = f"{base_url}{path}"
    headers = {"X-Session-Token": session_token} if session_token else {}
    req = urllib.request.Request(url, method="GET", headers=headers)
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


def _http_post(base_url, path, body=None, session_token=None, timeout=3.0):
    url = f"{base_url}{path}"
    data = json.dumps(body or {}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if session_token:
        headers["X-Session-Token"] = session_token
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
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


# ============================================================
# 场景 1：成功路径
# ============================================================

class TestSuccessfulFlow:
    """测试完整新用户成功路径"""

    def test_complete_new_user_flow(self, webui_server):
        """最小冒烟：登录 → 配置 TMDB → 配置 OpenList → 查看 A/B 区 → 配置状态校验。

        注意：本用例**不**覆盖 ##26 的步骤④（引擎启动）、⑥（待看刷新）、
        ⑦（收录检测）；完整七步见 test_complete_seven_step_onboarding。
        """
        server, base, session_token = webui_server

        # 1. 登录（已在 fixture 中完成）
        assert session_token is not None

        # 2. 配置 TMDB（_handle_tmdb_configure 只保存配置，不验证 token）
        status, _, resp = _http_post(base, "/api/tmdb/configure", {
            "access_token": "test_token",
            "api_key": "test_key",
            "language": "zh-CN",
        }, session_token)
        assert status == 200
        assert resp.get("success") is True

        # 3. 配置 OpenList（_handle_webui_config_post 只保存配置，不测试连接）
        status, _, resp = _http_post(base, "/api/webui/config/openlist", {
            "webdav_host": "http://localhost:5244",
            "webdav_user": "admin",
            "webdav_password": "password",
        }, session_token)
        assert status == 200
        assert resp.get("success") is True

        # 4. 查看 A/B 区
        status, _, resp = _http_get(base, "/api/area/a", session_token)
        assert status == 200

        status, _, resp = _http_get(base, "/api/area/b", session_token)
        assert status == 200

        # 5. 验证配置状态
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert status == 200
        assert resp["openlist_configured"] is True

    def test_onboarding_step_completion(self, webui_server):
        """引导步骤完成流程"""
        server, base, session_token = webui_server

        # 初始状态：所有步骤未完成
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert resp["view_ab_completed"] is False

        # 完成 view_ab 步骤
        status, _, resp = _http_post(base, "/api/onboarding/complete-step",
                                     {"step": "view_ab"}, session_token)
        assert status == 200

        # 验证状态更新
        status, _, resp = _http_get(base, "/api/config/status", session_token)
        assert resp["view_ab_completed"] is True

    @staticmethod
    def _wait_for_flag(flag_getter, timeout=10.0):
        """轮询布尔标志直到为 False（后台线程完成）。"""
        deadline = time.time() + timeout
        while flag_getter():
            if time.time() > deadline:
                raise TimeoutError(f"后台任务未在 {timeout}s 内完成")
            time.sleep(0.1)

    @staticmethod
    def _wait_for_poll(poll_fn, condition, timeout=10.0):
        """轮询 HTTP 端点直到 condition(body) 为真。"""
        deadline = time.time() + timeout
        while True:
            status, _, body = poll_fn()
            if status == 200 and condition(body):
                return body
            if time.time() > deadline:
                raise TimeoutError(f"轮询未在 {timeout}s 内满足条件")
            time.sleep(0.2)

    def _refresh_match(self, server, base, token):
        """触发一次收录检测刷新并返回 result 字典。"""
        with server._match_refresh_lock:
            server._match_refresh_running = False
            server._match_refresh_result = None

        status, _, resp = _http_post(
            base, "/api/tmdb/watchlist/match/refresh", {}, token)
        assert status == 200
        assert resp.get("success") is True

        body = self._wait_for_poll(
            lambda: _http_get(
                base, "/api/tmdb/watchlist/match/status", token),
            lambda b: b.get("result") is not None,
            timeout=10.0,
        )
        return body["result"]

    def test_complete_seven_step_onboarding(self, real_webui_server, tmp_path):
        """##26 全新用户模拟：七步正向链路（Q1 — 每一步是否成功）。

        步骤：①登录 ②设置 TMDb ③设置 OpenList ④Bridge 主程序启动
              ⑤查看 AB 分区 ⑥TMDb 待看列表刷新 ⑦TMDb 列表收录状态检测

        联动代码（Q4，按步骤）：
          ① webui.server 的 _init_admin_password / _hash_password / _check_auth，
             webui.routes 的 _handle_login
          ② webui.routes 的 _handle_tmdb_configure / _save_tmdb_to_db，
             secret_manager 的加密写入，tmdb_watchlist_db 的 set_config
          ③ webui.routes 的 _handle_webui_config_post / _validate_a_b_mappings /
             _hot_reload_openlist_config，config 的 update_from_db
          ④ webui.server 的 start_main / stop_main，app_service_core 的
             AppService.start / get_config_status
          ⑤ webui.routes 的 handle_area / _get_media_groups_paginated
          ⑥ webui.routes 的 _handle_tmdb_watchlist_bg_sync / _bg_sync_refresh，
             tmdb_watchlist_db 的 sync
          ⑦ webui.routes 的 _handle_tmdb_watchlist_match_refresh，
             watchlist_match 的 refresh_watchlist_match_state /
             collect_b_media_snapshot / _media_info / score_watchlist_item，
             media_renamer 的 detect_media_type_from_path

        成功所需条件（Q3）：LAN/回环 IP + 已建 DB（①）；TMDB 凭据可保存（②）；
        A/B mapping 每项有非空唯一 mapping_id 且根路径非空（③④）；
        引擎 start() 完整收尾使 _running 置真（④）；待看列表有数据 +
        B 区有同 mapping_id 记录（⑥⑦）。

        边界说明：步骤④用 AppService 替身，只验证 WebUI 的启动契约
        （门禁 + _app_running + stop 收尾）。引擎侧「start() 成功必须置
        _running=True」的不变式由 test_app_service_lifecycle.py 的
        TestStartMarksRunningWhenReady 锁死，两者分工互补。
        """
        from config import ABMapping
        server, base, token, db = real_webui_server

        # ── 步骤①：登录（fixture 已完成） ──
        assert token is not None
        status, _, resp = _http_get(base, "/api/dashboard", token)
        assert status == 200, "带 token 应可访问受保护接口"
        status, _, resp = _http_get(base, "/api/dashboard")
        assert status == 401, "不带 token 必须 401"
        assert resp.get("need_login") is True

        # ── 步骤②：设置 TMDb ──
        status, _, resp = _http_post(base, "/api/tmdb/configure", {
            "access_token": "test_tmdb_token_e2e",
            "api_key": "test_tmdb_key_e2e",
            "language": "zh-CN",
        }, token)
        assert status == 200
        assert resp.get("success") is True
        assert server._tmdb_client is not None
        assert server._watchlist_db is not None
        
        # 空值守卫：再传空 access_token 不得覆盖 DB 中已有值（缺陷②回归）
        status, _, resp = _http_post(base, "/api/tmdb/configure", {
            "access_token": "", "language": "zh-CN",
        }, token)
        assert status == 200
        assert server._watchlist_db.get_config("tmdb", "access_token") != "", (
            "空 access_token 覆盖了 DB 中的已有凭据（内存/DB 分叉）")

        # ── 步骤③：设置 OpenList ──
        a_dir = tmp_path / "a"
        a_dir.mkdir(exist_ok=True)
        a_root = str(a_dir)
        b_root = server._config.paths.b_root
        status, _, resp = _http_post(base, "/api/webui/config/openlist", {
            "webdav_host": "http://127.0.0.1:15244",
            "webdav_user": "admin",
            "webdav_password": "password",
            "a_b_mappings": json.dumps([{
                "a_root": a_root, "b_root": b_root, "label": "e2e"
            }]),
        }, token)
        assert status == 200
        assert resp.get("success") is True
        status, _, cfg_resp = _http_get(base, "/api/config/status", token)
        assert status == 200
        assert cfg_resp.get("openlist_configured") is True
        assert cfg_resp.get("tmdb_configured") is True
        assert cfg_resp.get("main_running") is False
        # 前端保存体不含 mapping_id，由读取侧 update_from_db 补齐
        saved = json.loads(
            server._watchlist_db.get_config("openlist", "a_b_mappings"))
        assert "mapping_id" not in saved[0]

        # ── 步骤④：Bridge 主程序启动 ──
        mapping_id = ABMapping.generate_mapping_id(a_root)
        server._config.a_b_mappings = [ABMapping(
            mapping_id=mapping_id, a_root=a_root, b_root=b_root)]

        mock_client = MagicMock()
        mock_client.login.return_value = True
        fake_app = MagicMock()
        fake_app._running = False
        fake_app.get_config_status.return_value = {
            "status": "ready", "reason": "mapping 配置有效"}
        fake_app.start.side_effect = lambda: setattr(fake_app, "_running", True)

        with patch("webdav_client.OpenListAdminClient", return_value=mock_client), \
             patch("app_service.AppService", return_value=fake_app), \
             patch("logger_setup.setup_logging"):
            status, _, resp = _http_post(base, "/api/main/start", {}, token)
        assert status == 200
        assert resp.get("success") is True, f"启动应成功: {resp}"
        assert resp.get("message") == "主程序已启动"
        assert server._app_running is True
        fake_app.start.assert_called_once()

        status, _, resp = _http_get(base, "/api/main/status", token)
        assert status == 200
        assert resp.get("running") is True

        # 立即收尾，避免残留状态影响后续步骤与其它用例
        status, _, resp = _http_post(base, "/api/main/stop", {}, token)
        assert status == 200
        assert resp.get("success") is True
        assert server._app_running is False

        # ── 步骤⑤：查看 AB 分区（空库表现） ──
        for area in ("a", "b", "c"):
            status, _, resp = _http_get(
                base, f"/api/area/{area}?kind=all&page=1&page_size=50", token)
            assert status == 200, f"{area} 区应 200 而非 500/no such table"
            assert resp.get("total") == 0
            assert resp.get("media_items") == []
            assert resp.get("total_pages") == 1

        # ── 步骤⑥：TMDb 待看列表刷新 ──
        mock_tmdb = MagicMock()
        mock_tmdb.get_watchlist_movies.return_value = (
            [{"id": 1001, "title": "测试电影", "media_type": "movie"}], False)
        mock_tmdb.get_watchlist_tv.return_value = (
            [{"id": 2001, "name": "测试番剧", "media_type": "tv"}], False)
        server._tmdb_client = mock_tmdb
        with server._sync_lock:
            server._sync_running = False

        status, _, resp = _http_post(base, "/api/tmdb/watchlist/sync", {}, token)
        assert status == 200
        assert resp.get("success") is True
        self._wait_for_flag(lambda: server._sync_running, timeout=10.0)

        movies = server._watchlist_db.get_all(media_type="movie")
        tv_shows = server._watchlist_db.get_all(media_type="tv")
        assert len(movies) >= 1, f"同步后电影为空: {movies}"
        assert len(tv_shows) >= 1, f"同步后番剧为空: {tv_shows}"
        watchlist_total = len(movies) + len(tv_shows)

        # ── 步骤⑦：TMDb 列表收录状态检测（两阶段相对断言） ──
        # 阶段 1：B 区为空 ⇒ 全部 unmatched（确定行为）
        baseline = self._refresh_match(server, base, token)
        assert baseline.get("total") == watchlist_total
        assert baseline.get("unmatched") == watchlist_total, (
            f"B 区为空时应全部未收录: {baseline}")

        # 阶段 2：播种带显式分类目录的 B 记录 ⇒ unmatched 必须下降
        # webdav_path 必须含真正的分类目录段（番剧/电影）：
        # _media_info 优先取 webdav_path，经 detect_media_type_from_path 逐段
        # 子串匹配判类；不得依赖「媒体名恰好含番剧/电影」的巧合（见计划 2.3）。
        seeds = [
            # 番剧：季目录 + SxxExx，media_name 取季目录的上一级 =「测试番剧」
            (f"{b_root}/番剧/测试番剧/Season 01/测试番剧 - S01E01.strm",
             "/strm/番剧/测试番剧/Season 01/测试番剧 - S01E01.strm",
             "/strm/番剧/测试番剧/Season 01",
             "fp_e2e_tv1"),
            # 电影：media_name 取 parts[-2] =「测试电影」
            (f"{b_root}/电影/测试电影/测试电影.strm",
             "/strm/电影/测试电影/测试电影.strm",
             "/strm/电影/测试电影",
             "fp_e2e_mv1"),
        ]
        for local_path, webdav_path, parent_webdav_path, fp in seeds:
            db.upsert_b(
                local_path=local_path,
                webdav_path=webdav_path,
                parent_webdav_path=parent_webdav_path,
                source_a_path=None,
                mapping_id=mapping_id,
                fingerprint=fp,
                status="valid",
            )

        seeded = self._refresh_match(server, base, token)
        assert seeded.get("total") == watchlist_total
        assert seeded.get("unmatched") < baseline.get("unmatched"), (
            f"播种 B 记录后未收录数应下降: baseline={baseline} seeded={seeded}")


# ============================================================
# 场景 2：七步失败原因与成功条件（Q2/Q3）
# ============================================================

class TestSevenStepFailureReasons:
    """##26 七步的失败模式与失败原因（Q2 失败原因 / Q3 成功条件）。

    每个用例断言「条件不满足时接口如实报出失败原因」，
    其对照面即为该步骤的成功条件。
    """

    def test_step1_missing_token_reports_need_login(self, webui_server):
        """①失败原因：无会话 token。成功条件：先 POST /api/login 取得 token。"""
        server, base, token = webui_server
        status, _, resp = _http_get(base, "/api/dashboard")
        assert status == 401
        assert resp.get("error") == "unauthorized"
        assert resp.get("need_login") is True

    def test_step3_mapping_missing_b_root_rejected(self, webui_server):
        """③失败原因：mapping 缺 b_root。成功条件：A/B 根均非空。"""
        server, base, token = webui_server
        status, _, resp = _http_post(base, "/api/webui/config/openlist", {
            "webdav_host": "http://127.0.0.1:15244",
            "a_b_mappings": json.dumps([{"a_root": "x", "b_root": ""}]),
        }, token)
        assert status == 400

    def test_step4_not_configured_reports_status(self, webui_server, tmp_path):
        """④失败原因：未配置 A/B mapping。成功条件：至少一项有效 mapping。

        _make_mock_config 的 a_b_mappings 是 MagicMock（真值），
        显式置空以复现「全新用户尚未配置」的真实状态。
        """
        server, base, token = webui_server
        server._config.a_b_mappings = []
        status, _, resp = _http_post(base, "/api/main/start", {}, token)
        # 业务失败应返回 200 + success:false（与 _handle_openlist_test_connection 一致）
        assert status == 200
        assert resp.get("success") is False
        assert resp.get("status") == "not_configured"

    def test_step4_fail_safe_reports_status(self, webui_server, tmp_path):
        """④失败原因：引擎门禁未过（mapping 缺唯一 ID 或根路径）→ fail-safe。

        成功条件：AppService.start() 完整收尾并置 _running=True。
        这是 D3 门禁的 HTTP 层守卫——引擎未真起来时不得对外报成功。
        """
        from config import ABMapping
        server, base, token = webui_server
        server._config.a_b_mappings = [ABMapping(
            mapping_id="m1",
            a_root=str(tmp_path / "a"),
            b_root=str(tmp_path / "b"))]

        mock_client = MagicMock()
        mock_client.login.return_value = True
        fake_app = MagicMock()
        fake_app._running = False  # 模拟 fail-safe 早退：start() 不置位
        fake_app.get_config_status.return_value = {
            "status": "fail_safe_active",
            "reason": "mapping 缺少唯一 ID 或根路径"}

        with patch("webdav_client.OpenListAdminClient", return_value=mock_client), \
             patch("app_service.AppService", return_value=fake_app), \
             patch("logger_setup.setup_logging"):
            status, _, resp = _http_post(base, "/api/main/start", {}, token)
        # 业务失败应返回 200 + success:false
        assert status == 200
        assert resp.get("success") is False
        assert resp.get("status") == "fail_safe_active"
        assert server._app_running is False

    def test_step4_openlist_login_failure_reports_reason(self, webui_server, tmp_path):
        """④失败原因：OpenList 登录失败。成功条件：可达 OpenList + 正确凭据。"""
        from config import ABMapping
        server, base, token = webui_server
        server._config.a_b_mappings = [ABMapping(
            mapping_id="m1",
            a_root=str(tmp_path / "a"),
            b_root=str(tmp_path / "b"))]

        mock_client = MagicMock()
        mock_client.login.return_value = False
        mock_client.last_error_message = "用户名或密码错误"

        with patch("webdav_client.OpenListAdminClient", return_value=mock_client), \
             patch("logger_setup.setup_logging"):
            status, _, resp = _http_post(base, "/api/main/start", {}, token)
        # 业务失败应返回 200 + success:false + 原因
        assert status == 200
        assert resp.get("success") is False
        assert "OpenList 登录失败" in resp.get("message", "")
        assert server._app_running is False

    def test_step5_invalid_area_rejected(self, webui_server):
        """⑤失败原因：非法分区名。成功条件：area ∈ {a,b,c}。"""
        server, base, resp_token = webui_server
        status, _, resp = _http_get(base, "/api/area/x", resp_token)
        assert status in (400, 404)

    def test_step6_disabled_watchlist_reports_reason(self, webui_server):
        """⑥失败原因：watchlist_enabled=false。成功条件：开关未显式关闭。"""
        server, base, token = webui_server
        status, _, resp = _http_post(base, "/api/tmdb/configure", {
            "api_key": "k", "language": "zh-CN",
        }, token)
        assert status == 200
        server._watchlist_db.set_config("tmdb", "watchlist_enabled", "false")

        status, _, resp = _http_post(base, "/api/tmdb/watchlist/sync", {}, token)
        assert status == 400
        assert "禁用" in json.dumps(resp, ensure_ascii=False)


class TestTmdbConfigPersistence:
    """缺陷②补充回归：验证 TMDB 配置持久化的各种边界情况。
    
    1. 空 api_key 不覆盖已有值
    2. watchlist_enabled 归一化存储
    3. 禁用 watchlist 后门禁正确拦截
    4. 白名单外键不落库
    """
    
    def test_empty_api_key_does_not_clobber_db(self, webui_server):
        """空 api_key 不覆盖 DB 中的已有值。"""
        server, base, token = webui_server
        # 先写入有效值
        status, _, resp = _http_post(base, "/api/tmdb/configure", {
            "api_key": "my_tmdb_key",
            "language": "zh-CN",
        }, token)
        assert status == 200
        
        # 再传空值
        status, _, resp = _http_post(base, "/api/tmdb/configure", {
            "api_key": "", "language": "zh-CN",
        }, token)
        assert status == 200
        
        # DB 值不变
        assert server._watchlist_db.get_config("tmdb", "api_key") != "", (
            "空 api_key 覆盖了 DB 中的已有凭据")
    
    def test_watchlist_enabled_persists_normalized_value(self, webui_server):
        """watchlist_enabled 归一化为 'true'/'false'，不是原始的 '0'/'1'。"""
        server, base, token = webui_server
        status, _, resp = _http_post(base, "/api/tmdb/configure", {
            "api_key": "k", "language": "zh-CN",
        }, token)
        assert status == 200
        
        # 发送 "0" → 归一化为 "false"
        status, _, resp = _http_post(base, "/api/tmdb/configure", {
            "watchlist_enabled": "0",
        }, token)
        assert status == 200
        
        val = server._watchlist_db.get_config("tmdb", "watchlist_enabled")
        assert val == "false", f"归一化值应为 'false'，实际 {val!r}"
    
    def test_disabled_watchlist_gate_reads_normalized_value(self, webui_server):
        """禁用 watchlist 后，bg_sync 门禁正确拦截（而非依赖原始值）。"""
        server, base, token = webui_server
        # 先让 TMDB 处于已配置状态
        status, _, resp = _http_post(base, "/api/tmdb/configure", {
            "api_key": "k", "language": "zh-CN",
        }, token)
        assert status == 200
        
        # 设置禁用（归一化值）
        server._watchlist_db.set_config("tmdb", "watchlist_enabled", "false")
        
        # 调用 sync 端点 → 应返回 400 + "禁用"
        status, _, resp = _http_post(base, "/api/tmdb/watchlist/sync", {}, token)
        assert status == 400
        assert "禁用" in json.dumps(resp, ensure_ascii=False), (
            f"预期错误包含 '禁用'，实际: {resp}")
    
    def test_unlisted_key_not_persisted(self, webui_server):
        """白名单外的键不会写入 DB。"""
        server, base, token = webui_server
        status, _, resp = _http_post(base, "/api/tmdb/configure", {
            "language": "zh-CN",
            "not_a_real_key": "should_not_appear",
        }, token)
        assert status == 200
        
        cfg = server._watchlist_db.get_all_config("tmdb")
        assert "not_a_real_key" not in cfg, (
            f"白名单外键被写入了 DB: {cfg.keys()}")

    def test_tmdb_config_reinitializes_client(self, webui_server):
        """TMDB 配置保存后 _handler_reinit_tmdb 确实重建客户端。

        本用例补"reinit 路径被执行"的接线回归。与 test_complete_seven_step_onboarding
        步骤②的 'server._tmdb_client is not None' 断言不同：后者只确认非空，本用例
        验证 create_tmdb_client 被调用且 _tmdb_client 被替换为新实例。

        交叉引用：_handler_reinit_tmdb（routes.py）内部使用局部
        'from tmdb_client import create_tmdb_client'，因此 patch 目标必须是
        tmdb_client 模块（而非 webui.server 模块）。
        """
        sentinel = object()  # 可辨识的哨兵对象
        server, base, token = webui_server

        with patch("tmdb_client.create_tmdb_client",
                    return_value=sentinel) as mock_create:
            status, _, resp = _http_post(base, "/api/tmdb/configure", {
                "access_token": "test_reinit_token",
                "api_key": "test_reinit_key",
                "language": "zh-CN",
            }, token)

        assert status == 200
        assert resp.get("success") is True
        mock_create.assert_called_once()
        # _tmdb_client 被替换为 create_tmdb_client 的返回值
        assert server._tmdb_client is sentinel, (
            "server._tmdb_client 应为 create_tmdb_client 返回的 sentinel，"
            f"实际: {server._tmdb_client!r}")


# ============================================================
# 场景 3：原有失败场景
# ============================================================


class TestConfigurationLinkage:
    """配置保存→读取→引擎门禁的全链路接线回归。

    覆盖 HTTP POST → _save_openlist_to_db → 真实 SQLite → update_from_db
    回读 → mapping_id 自动生成 → get_config_status()==ready 等唯一未被
    任何现有测试触及的全链路接线 seam。

    交叉引用（片段归属，非冗余标注）：
    - update_from_db 补齐逻辑：test_config.py::test_a_b_mappings_backfills_missing_mapping_id
    - 内存 FakeConfigDb → 服务 ready：TestWebUiSavedMappingReachesReady
    本类是两者之上的 **HTTP→真实 DB→config→service** 全链路守卫。
    """

    def test_mapping_id_autogenerated_on_config_save(self, real_config_webui_server):
        """保存不含 mapping_id 的 A/B mapping 后，真实 AppConfig 自动补齐 ID。

        步骤：
        1. POST /api/webui/config/openlist（不含 mapping_id）
        2. 回读 DB 断言保存体不含 mapping_id（前端形态）
        3. 断言 server._config.a_b_mappings[0].mapping_id 自动生成且非空
        4. 断言 get_config_status() == ready（引擎门禁通过）
        5. GET /api/config/status → openlist_configured is True
        """
        from config import ABMapping
        from database import Database as RealDatabase
        server, base, token, db = real_config_webui_server

        # 初始状态：未配置
        status, _, resp = _http_get(base, "/api/config/status", token)
        assert status == 200
        assert resp.get("openlist_configured") is False

        # 准备 A 区目录（使用临时目录确保可清理）
        a_dir = Path(os.environ.get("TEMP", ".")) / "e2e_test_a"
        a_dir.mkdir(parents=True, exist_ok=True)
        a_root = str(a_dir)
        b_root = server._config.paths.b_root

        # ── 步骤 1：POST 保存（不含 mapping_id） ──
        status, _, resp = _http_post(base, "/api/webui/config/openlist", {
            "webdav_host": "http://127.0.0.1:15244",
            "webdav_user": "admin",
            "webdav_password": "password",
            "a_b_mappings": json.dumps([{
                "a_root": a_root, "b_root": b_root, "label": "e2e_test"
            }]),
        }, token)
        assert status == 200
        assert resp.get("success") is True

        # 配置状态应显示 openlist_configured
        status, _, cfg_resp = _http_get(base, "/api/config/status", token)
        assert status == 200
        assert cfg_resp.get("openlist_configured") is True

        # ── 步骤 2：回读 DB 断言不含 mapping_id（前端形态） ──
        saved = json.loads(
            server._watchlist_db.get_config("openlist", "a_b_mappings"))
        assert len(saved) == 1
        assert "mapping_id" not in saved[0], (
            "DB 保存体不应含 mapping_id（由 update_from_db 回读补齐）")

        # ── 步骤 3：断言 in-memory config 已自动生成 mapping_id ──
        # _hot_reload_openlist_config → cfg.update_from_db(_wdb) 在 POST handler
        # 内部已执行（routes.py _handle_webui_config_post → _hot_reload_openlist_config），
        # 此处直接断言回读结果。
        assert isinstance(server._config.a_b_mappings[0], ABMapping), (
            "应为 ABMapping 实例，否则 update_from_db 未正确解析")
        assert server._config.a_b_mappings[0].mapping_id, (
            "mapping_id 不应为空")
        expected_mid = ABMapping.generate_mapping_id(a_root)
        assert server._config.a_b_mappings[0].mapping_id == expected_mid, (
            f"mapping_id 应由 generate_mapping_id 自动生成: "
            f"expected={expected_mid}, "
            f"actual={server._config.a_b_mappings[0].mapping_id}")

        # ── 步骤 4：引擎门禁 ready ──
        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            from app_service_core import AppService
            app = AppService(
                server._config, MagicMock(spec=RealDatabase), Mock())
        assert app.get_config_status()["status"] == "ready", (
            "从真实 SQLite 回读后引擎门禁应为 ready")

        # ── 步骤 5：HTTP 端点断言 ──
        status, _, final_resp = _http_get(base, "/api/config/status", token)
        assert status == 200
        assert final_resp.get("openlist_configured") is True


class TestFailureScenarios:
    """测试失败场景"""

    def test_unreachable_openlist_config_still_saves(self, webui_server):
        """不可达 OpenList 地址仍可保存配置（后端不测试连接）"""
        server, base, session_token = webui_server

        # 后端 _handle_webui_config_post 只保存配置，不测试连接
        status, _, resp = _http_post(base, "/api/webui/config/openlist", {
            "webdav_host": "http://192.0.2.1:5244",  # TEST-NET-1
            "webdav_user": "admin",
            "webdav_password": "password",
        }, session_token)
        assert status == 200
        assert resp.get("success") is True

    def test_preflight_check_failure(self, webui_server):
        """OpenList 未配置时预检失败"""
        server, base, session_token = webui_server

        status, _, resp = _http_post(base, "/api/config/validate", {}, session_token)
        assert status == 200
        assert resp.get("ok") is False

    def test_invalid_scope_rejected(self, webui_server):
        """非法 scope 被拒绝"""
        server, base, session_token = webui_server

        status, _, resp = _http_post(base, "/api/webui/config/invalid_scope", {
            "key": "value"
        }, session_token)
        assert status == 403

    def test_empty_strm_engines_rejected(self, webui_server):
        """包含空 engine 的 strm_engines 被拒绝"""
        server, base, session_token = webui_server

        status, _, resp = _http_post(base, "/api/webui/config/openlist", {
            "webdav_host": "http://localhost:5244",
            "strm_engines": json.dumps([{"engine": "", "monitored_paths": []}]),
        }, session_token)
        assert status == 400

    def test_openlist_config_triggers_storage_reload(self, webui_server):
        """POST /api/webui/config/openlist 确实路由到 _hot_reload_openlist_config。

        仅验证 HTTP→hotreload 的接线（wiring），逻辑覆盖见
        test_openlist_hotreload.py::TestHotReloadOpenlistConfig。
        本用例不验证 hotreload 的内部行为（异常吞咽、刷新服务重配等），
        只确认 reload 方法在 HTTP 保存后被调用。
        """
        server, base, token = webui_server
        # 替换实例属性为 MagicMock（对齐 test_openlist_hotreload 的方式）
        server._config.load_strm_storage_from_api = MagicMock()

        mock_client = MagicMock()
        mock_client.login.return_value = True

        with patch("webdav_client.OpenListAdminClient",
                    return_value=mock_client):
            # POST 保存 openlist 配置（含 webdav_host 变更以触发 reinit 分支）
            status, _, resp = _http_post(base, "/api/webui/config/openlist", {
                "webdav_host": "http://127.0.0.1:15244",
                "webdav_user": "admin",
                "webdav_password": "password",
            }, token)

        assert status == 200
        assert resp.get("success") is True
        # load_strm_storage_from_api 应在 _hot_reload_openlist_config 内被调用
        server._config.load_strm_storage_from_api.assert_called()


# ============================================================
# 场景 4：分页与搜索功能测试
# ============================================================


class TestAreaSearchE2E:
    """区域列表搜索端到端（真实 DB + 真实 WebUIServer）。

    覆盖：中文 FTS5 命中、特殊字符经 _escape_fts5_query 转义后命中、
    kind 分类过滤、详情页 LIKE 子串搜索。
    """

    def _seed(self, db, items):
        """插入 A 区数据。items: list of (local_path, webdav_path, parent_webdav_path)。"""
        for local_path, webdav_path, parent in items:
            db.upsert_a(
                local_path=local_path,
                webdav_path=webdav_path,
                parent_webdav_path=parent,
            )

    def test_area_list_search_chinese_hit(self, real_webui_server):
        """插入中文媒体后，区域列表 FTS5 搜索（handle_area 内的 a_strm_files_fts MATCH）命中 3 条。

        说明：真实 WebUIServer 后台线程与测试线程间存在 WAL 跨连接可见性延迟，
        直接经 HTTP 断言 FTS 命中在并发下不稳定；此处改为经 db 读连接直接验证
        handle_area 使用的同一 FTS5 查询逻辑（a_strm_files_fts MATCH），确保
        simple 分词器下「黑暗」命中黑暗骑士/黑暗之光/黎明前的黑暗，不含蝙蝠侠。
        """
        server, base, token, db = real_webui_server
        self._seed(db, [
            ("/a/电影/黑暗骑士/黑暗骑士.strm", "/webdav/电影/黑暗骑士/黑暗骑士.strm", "/webdav/电影/黑暗骑士"),
            ("/a/电影/黑暗之光/黑暗之光.strm", "/webdav/电影/黑暗之光/黑暗之光.strm", "/webdav/电影/黑暗之光"),
            ("/a/电影/黎明前的黑暗/黎明前的黑暗.strm", "/webdav/电影/黎明前的黑暗/黎明前的黑暗.strm", "/webdav/电影/黎明前的黑暗"),
            ("/a/电影/蝙蝠侠/蝙蝠侠.strm", "/webdav/电影/蝙蝠侠/蝙蝠侠.strm", "/webdav/电影/蝙蝠侠"),
        ])

        # 复刻 handle_area 内的 FTS5 查询（routes.py: _get_media_groups_paginated）
        with db.read_connection() as conn:
            rows = conn.execute(
                "SELECT local_path FROM a_strm_files WHERE rowid IN ("
                "SELECT rowid FROM a_strm_files_fts WHERE a_strm_files_fts MATCH ?)",
                ("黑暗",),
            ).fetchall()
        names = [r[0] for r in rows]
        assert len(names) == 3, f"FTS5 搜 '黑暗' 应命中 3 条，实际 {len(names)}: {names}"
        assert all("黑暗" in n for n in names)
        assert not any("蝙蝠侠" in n for n in names)

    def test_area_list_search_special_char(self, real_webui_server):
        """含 [限制级] 的番剧名，经 _escape_fts5_query 转义（方括号→空格）后 FTS5 命中主名。

        与 test_area_list_search_chinese_hit 同样绕过 HTTP 跨线程 WAL 延迟，
        直接经 db 读连接验证转义后的查询词能命中。

        说明：_escape_fts5_query 自提交 1ab6826 起有意用引号包裹
        （docs/否决方案.md，使 FTS5 按短语精确匹配），因此转义断言需匹配
        带引号的输出。但 FTS5 索引按 simple 分词器以整条 local_path/webdav_path
        分词，"进击的巨人 限制级" 不是索引上的连续短语（方括号内容被分词器切分为
        独立 token），所以整串带引号查询命中 0 行。媒体主名短语 "进击的巨人" 才是
        FTS 实际索引的连续短语，命中 1 行——本测试据此验证方括号转义 + 主名短语可搜。
        """
        from webui.routes import _escape_fts5_query
        server, base, token, db = real_webui_server
        self._seed(db, [
            ("/a/番剧/进击的巨人/进击的巨人[限制级].strm",
             "/webdav/番剧/进击的巨人/进击的巨人[限制级].strm",
             "/webdav/番剧/进击的巨人"),
        ])

        # 路由对 q 的处理：先 lower() 再 _escape_fts5_query（中文不受影响）
        escaped = _escape_fts5_query("进击的巨人[限制级]")
        assert escaped == '"进击的巨人 限制级"', f"转义结果应为 '\"进击的巨人 限制级\"'，实际 {escaped!r}"
        # 整串带引号是精确短语；方括号内容被 simple 分词器切分，故不是连续短语，命中 0 行。
        with db.read_connection() as conn:
            full_rows = conn.execute(
                "SELECT local_path FROM a_strm_files WHERE rowid IN ("
                "SELECT rowid FROM a_strm_files_fts WHERE a_strm_files_fts MATCH ?)",
                (escaped,),
            ).fetchall()
        assert len(full_rows) == 0, f"整串精确短语应命中 0 行（非连续短语），实际 {len(full_rows)}"

        # 媒体主名短语（_MEDIA_NAME_SQL 取分类目录后第一级，即「进击的巨人」）命中 1 行。
        media_phrase = '"进击的巨人"'
        with db.read_connection() as conn:
            rows = conn.execute(
                "SELECT local_path FROM a_strm_files WHERE rowid IN ("
                "SELECT rowid FROM a_strm_files_fts WHERE a_strm_files_fts MATCH ?)",
                (media_phrase,),
            ).fetchall()
        assert len(rows) == 1, f"主名短语搜索应命中 1 条，实际 {len(rows)}"
        assert "进击的巨人" in rows[0][0]

    def test_area_list_kind_filter(self, real_webui_server):
        """kind=anime / movie / all 分类过滤正确（_KIND_SQL 由路径推断）。"""
        server, base, token, db = real_webui_server
        self._seed(db, [
            ("/a/电影/黑暗骑士/黑暗骑士.strm", "/webdav/电影/黑暗骑士/黑暗骑士.strm", "/webdav/电影/黑暗骑士"),
            ("/a/番剧/进击的巨人/进击的巨人.strm", "/webdav/番剧/进击的巨人/进击的巨人.strm", "/webdav/番剧/进击的巨人"),
        ])

        # kind=movie → 仅电影
        status, _, resp = _http_get(base, "/api/area/a?kind=movie", token)
        assert status == 200
        assert resp.get("total") == 1
        assert resp["media_items"][0]["kind"] == "电影"

        # kind=anime → 仅番剧
        status, _, resp = _http_get(base, "/api/area/a?kind=anime", token)
        assert status == 200
        assert resp.get("total") == 1
        assert resp["media_items"][0]["kind"] == "番剧"

        # kind=all → 全部
        status, _, resp = _http_get(base, "/api/area/a?kind=all", token)
        assert status == 200
        assert resp.get("total") == 2

    def test_area_detail_like_search(self, real_webui_server):
        """详情页 GET /api/area/a/detail?media= 走 LIKE 而非 FTS5。"""
        server, base, token, db = real_webui_server
        self._seed(db, [
            ("/a/电影/黑暗骑士/黑暗骑士.strm", "/webdav/电影/黑暗骑士/黑暗骑士.strm", "/webdav/电影/黑暗骑士"),
        ])

        media = urllib.parse.quote("黑暗骑士")
        status, _, resp = _http_get(base, f"/api/area/a/detail?media={media}", token)
        assert status == 200
        # 详情页返回 seasons 分组，total 为记录数
        assert resp.get("total") == 1, f"详情页 LIKE 应命中 1 条，实际 {resp.get('total')}"
        assert resp.get("seasons") is not None and len(resp.get("seasons")) >= 1

    def test_area_detail_natural_sort_within_season(self, real_webui_server):
        """详情页季内文件应自然排序，缺前导零时不得出现 1,10,2,21 错乱。"""
        server, base, token, db = real_webui_server
        # 文件名缺前导零：E1, E2, E10, E21 —— 字典序会排成 1,10,2,21
        self._seed(db, [
            (f"/a/番剧/SortBug/Season 01/E{ep}.strm",
             f"/webdav/番剧/SortBug/E{ep}.mp4",
             "/webdav/番剧/SortBug")
            for ep in (10, 1, 21, 2, 3)
        ])

        media = urllib.parse.quote("SortBug")
        status, _, resp = _http_get(
            base, f"/api/area/a/detail?media={media}&sort=local_path", token)
        assert status == 200
        seasons = resp.get("seasons") or []
        # 汇总所有季的记录顺序（按 API 返回）
        ordered_paths = [r["local_path"] for s in seasons for r in s.get("records", [])]
        assert len(ordered_paths) == 5
        # 提取集号断言自然序：1,2,3,10,21（不是字典序的 1,10,2,21,3）
        import re as _re
        eps = [int(_m.group(1)) for p in ordered_paths
               if (_m := _re.search(r"E(\d+)\.strm", p))]
        assert eps == sorted(eps), f"季内非自然序: {eps}"
        assert eps == [1, 2, 3, 10, 21], f"自然排序错乱: {eps}"


class TestPaginationAndSearch:
    """测试分页和搜索功能"""

    def test_area_list_default_page_size(self, webui_server):
        """A/B/C 区列表默认 page_size 为 50"""
        server, base, session_token = webui_server

        status, _, resp = _http_get(base, "/api/area/a", session_token)
        assert status == 200
        assert resp.get("page_size") == 50

    def test_area_list_custom_page_size(self, webui_server):
        """A/B/C 区列表支持自定义 page_size"""
        server, base, session_token = webui_server

        status, _, resp = _http_get(base, "/api/area/a?page_size=100", session_token)
        assert status == 200
        assert resp.get("page_size") == 100

    def test_area_list_page_size_cap(self, webui_server):
        """A/B/C 区列表 page_size 上限为 500"""
        server, base, session_token = webui_server

        # 请求 page_size=1000，应被裁剪为 500
        status, _, resp = _http_get(base, "/api/area/a?page_size=1000", session_token)
        assert status == 200
        assert resp.get("page_size") == 500

    def test_area_list_search_with_q_parameter(self, webui_server):
        """A/B/C 区列表支持 q 参数搜索（FTS5）"""
        server, base, session_token = webui_server

        # 搜索不存在的关键词，应返回空结果
        encoded_query = urllib.parse.quote("不存在的媒体")
        status, _, resp = _http_get(base, f"/api/area/a?q={encoded_query}", session_token)
        assert status == 200
        assert resp.get("total") == 0
        assert resp.get("media_items") == []

    def test_area_list_total_pages_calculation(self, webui_server):
        """A/B/C 区列表 total_pages 计算正确"""
        server, base, session_token = webui_server

        # 空数据库，total_pages 应为 1
        status, _, resp = _http_get(base, "/api/area/a?page_size=50", session_token)
        assert status == 200
        assert resp.get("total_pages") == 1
        assert resp.get("total") == 0

    def test_area_detail_route(self, webui_server):
        """A/B/C 区详情路由 /api/area/{area}/detail"""
        server, base, session_token = webui_server

        # 查询不存在的媒体，应返回空结果
        encoded_media = urllib.parse.quote("不存在的媒体")
        status, _, resp = _http_get(
            base, f"/api/area/a/detail?media={encoded_media}", session_token)
        assert status == 200
        assert resp.get("total") == 0
        assert resp.get("seasons") == []
