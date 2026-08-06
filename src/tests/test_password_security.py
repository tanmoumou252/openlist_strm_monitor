"""
密码安全专项测试。

覆盖 P1-P18 密码安全检查清单中标记"无直接测试"的项：
  P1  PBKDF2-HMAC-SHA256 600k 迭代哈希格式
  P2  密码验证正确性（正确/错误）
  P3  时序安全已修复：使用 hmac.compare_digest（M-5/H-5），=== 比较已移除
  P5  /api/webui/config/ui GET 不泄露 admin_password
  P6  明文密码写入 DB 时自动哈希
  P11 首次启动生成随机密码
  P13 reset_admin.py 密码长度 >= 4
  P14 密码仅输出到控制台（print），不写入日志文件

P4/P7-P10/P12/P15-P18 已由 test_integration_security.py / test_webui_http.py /
test_real_server.py 覆盖，此处不重复。

测试策略：
  - P1/P2/P3：直接调用 WebUIServer 静态方法，无需服务器
  - P5/P6：启动真实 WebUIServer + 真实 TmdbWatchlistDb（tmp_path 隔离）
  - P11：构造无密码 DB，调用 _init_admin_password，验证 _has_password=True
  - P13：调用 reset_admin.main()，验证短密码触发 sys.exit
  - P14：捕获 logging 输出，验证密码明文不出现在日志记录中
"""

from __future__ import annotations

import io
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 冗余保护：确保 src/ 在 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webui.server import WebUIServer  # noqa: E402
from tmdb_watchlist_db import TmdbWatchlistDb  # noqa: E402


# ============================================================
# 基础设施
# ============================================================

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def _make_mock_config(tmp_path: Path) -> MagicMock:
    """构造最小化 AppConfig mock，满足 WebUIServer 初始化需求。"""
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
    """构造最小化 Database mock。"""
    db = MagicMock(spec=["db_path", "get_table_counts", "get_b_status_counts",
                        "get_db_file_size", "get_subtitle_by_local",
                        "read_connection"])
    db.db_path = str(tmp_path / "bridge.db")
    db.get_table_counts.return_value = {
        "a_strm_files": 0, "b_strm_files": 0, "c_ghost_files": 0,
    }
    db.get_b_status_counts.return_value = {
        "valid": 0, "orphan": 0, "unknown": 0,
    }
    db.get_db_file_size.return_value = 0
    db.get_subtitle_by_local.return_value = None
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = (0,)
    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn_ctx.__exit__ = MagicMock(return_value=False)
    db.read_connection.return_value = mock_conn_ctx
    return db


def _http_get(base_url: str, path: str, session_token: str | None = None,
              timeout: float = 3.0):
    url = f"{base_url}{path}"
    req = urllib.request.Request(url, method="GET",
                                 headers={"X-Session-Token": session_token}) \
        if session_token else urllib.request.Request(url, method="GET")
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


def _http_post(base_url: str, path: str, data: dict, session_token: str | None = None,
               timeout: float = 3.0):
    url = f"{base_url}{path}"
    body = json.dumps(data).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if session_token:
        headers["X-Session-Token"] = session_token
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
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


# ============================================================
# P1/P2/P3: 哈希算法与验证（无需服务器）
# ============================================================

class TestPasswordHashing:
    """密码哈希算法测试（P1/P2/P3）。"""

    def test_p1_hash_format(self):
        """P1: PBKDF2-HMAC-SHA256 600k 迭代哈希格式验证。

        格式: salt$iterations$hash
        - salt: 32 字符 hex (16 字节)
        - iterations: "600000"
        - hash: 64 字符 hex (32 字节 SHA256)
        """
        h = WebUIServer._hash_password("test")
        parts = h.split("$", 2)
        assert len(parts) == 3, "哈希格式应为 salt$iterations$hash"
        assert len(parts[0]) == 32, f"salt 应为 32 字符 hex，实际: {len(parts[0])}"
        assert parts[1] == "600000", f"迭代次数应为 600000，实际: {parts[1]}"
        assert len(parts[2]) == 64, f"hash 应为 64 字符 hex，实际: {len(parts[2])}"

    def test_p1_hash_uses_random_salt(self):
        """P1 补充: 相同密码两次哈希应产生不同 salt（随机性）。"""
        h1 = WebUIServer._hash_password("samepassword")
        h2 = WebUIServer._hash_password("samepassword")
        salt1 = h1.split("$", 2)[0]
        salt2 = h2.split("$", 2)[0]
        assert salt1 != salt2, "salt 应随机生成，两次哈希不应相同"

    def test_p2_check_password_correct(self):
        """P2: 正确密码验证通过。"""
        h = WebUIServer._hash_password("mypassword")
        assert WebUIServer._check_password("mypassword", h) is True

    def test_p2_check_password_wrong(self):
        """P2: 错误密码验证失败。"""
        h = WebUIServer._hash_password("mypassword")
        assert WebUIServer._check_password("wrong", h) is False

    def test_p2_check_password_empty_stored(self):
        """P2 补充: 空存储值验证失败（不崩溃）。"""
        assert WebUIServer._check_password("anything", "") is False

    def test_p2_check_password_malformed_stored(self):
        """P2 补充: 格式错误的存储值验证失败（不崩溃）。"""
        assert WebUIServer._check_password("anything", "not_a_hash") is False
        assert WebUIServer._check_password("anything", "a$b$c$d") is False

    def test_p3_password_compare_not_timing_safe(self):
        """P3: 时序安全现状记录。

        当前实现使用 hmac.compare_digest（通过 password_utils.verify_password），
        防止时序攻击。这是已知安全设计，M-5 修复后统一使用恒定时间比较。
        本测试验证当前实现已使用安全比较。
        """
        import inspect
        source = inspect.getsource(WebUIServer._check_password)
        # 修复后：使用 verify_password（内部使用 hmac.compare_digest）
        assert "verify_password" in source, "当前实现应使用统一的 verify_password"
        assert "==" not in source, "当前实现不应使用 == 比较"


# ============================================================
# P5/P6: 密码泄露防护与自动哈希（需真实服务器 + 真实 DB）
# ============================================================

@pytest.fixture
def webui_server_real_db(tmp_path):
    """启动真实 WebUIServer + 真实 TmdbWatchlistDb。

    与 test_webui_http.py 的 webui_server fixture 不同，此处使用真实
    TmdbWatchlistDb（tmp_path 隔离），使密码写入/读取走真实 SQLite 路径。
    """
    # 清理全局登录速率限制状态
    from webui.routes import _login_attempts
    _login_attempts.clear()

    cfg = _make_mock_config(tmp_path)
    db = _make_mock_db(tmp_path)
    port = _free_port()
    cfg.webui.port = port

    test_password = "test_password_123"
    os.environ["WEBUI_TEST_MODE"] = "1"
    os.environ["WEBUI_ADMIN_PASSWORD_FOR_TEST"] = test_password

    with patch("webui.server.PROJECT_ROOT", tmp_path), \
         patch("webui.server.STATIC_DIR", tmp_path / "static"):
        (tmp_path / "static").mkdir(exist_ok=True)
        (tmp_path / "static" / "index.html").write_text(
            "<html><body>test</body></html>", encoding="utf-8")
        (tmp_path / "static" / "assets").mkdir(exist_ok=True)
        (tmp_path / "static" / "assets" / "favicon.ico").write_bytes(b"\x00")

        server = WebUIServer(cfg.webui, db, app_config=cfg)
        server.start()
        deadline = time.time() + 2.0
        while not server._server and time.time() < deadline:
            time.sleep(0.05)

        base_url = f"http://127.0.0.1:{port}"

        # 登录获取 session token
        login_status, _, login_body = _http_post(
            base_url, "/api/login", {"password": test_password})
        assert login_status == 200, f"登录失败: {login_status} {login_body}"
        session_token = login_body.get("token")
        assert session_token is not None, "未获取到 session token"

        yield server, base_url, session_token

        server.stop()
        # 清理环境变量
        os.environ.pop("WEBUI_TEST_MODE", None)
        os.environ.pop("WEBUI_ADMIN_PASSWORD_FOR_TEST", None)


class TestPasswordLeakPrevention:
    """密码泄露防护测试（P5/P6）。"""

    def test_p5_ui_config_get_no_password_leak(self, webui_server_real_db):
        """P5: GET /api/webui/config/ui 不泄露 admin_password。

        routes.py:863-864 在返回前 pop 掉 admin_password。
        """
        server, base, token = webui_server_real_db
        status, _, body = _http_get(base, "/api/webui/config/ui", token)
        assert status == 200, f"请求失败: {status} {body}"
        assert body.get("success") is True, f"响应异常: {body}"
        config = body.get("config", {})
        assert isinstance(config, dict), f"config 应为 dict: {config}"
        # admin_password 不应出现在响应中
        assert "admin_password" not in config, \
            f"admin_password 不应通过 GET 泄露: {config}"

    def test_p6_plaintext_password_auto_hash(self, webui_server_real_db):
        """P6: 明文密码写入 DB 时自动哈希。

        routes.py:949-952 检测明文密码（无 $）并自动哈希。
        """
        server, base, token = webui_server_real_db
        plaintext = "newplainpass"
        status, _, body = _http_post(
            base, "/api/webui/config/ui",
            {"admin_password": plaintext}, token)
        assert status == 200, f"请求失败: {status} {body}"
        assert body.get("success") is True, f"响应异常: {body}"

        # 验证 DB 中存储的是哈希格式（非明文）
        stored = server._watchlist_db.get_config("ui", "admin_password")
        assert stored, "DB 中应存储了 admin_password"
        assert "$" in stored, f"存储的密码应为哈希格式 salt$iterations$hash: {stored}"
        assert plaintext not in stored, \
            f"不应存储明文密码: {stored}"
        # 验证哈希格式正确（可被 _check_password 验证）
        parts = stored.split("$", 2)
        assert len(parts) == 3, f"哈希格式错误: {stored}"
        assert parts[1] == "600000", f"迭代次数错误: {parts[1]}"

    def test_p6_hashed_password_not_rehashed(self, webui_server_real_db):
        """P6 补充: 已哈希的密码不应被二次哈希。

        routes.py:951 检测到 $ 存在时跳过哈希。
        """
        server, base, token = webui_server_real_db
        # 先生成一个哈希
        original_hash = WebUIServer._hash_password("hashedpass")
        status, _, body = _http_post(
            base, "/api/webui/config/ui",
            {"admin_password": original_hash}, token)
        assert status == 200, f"请求失败: {status} {body}"

        stored = server._watchlist_db.get_config("ui", "admin_password")
        assert stored == original_hash, \
            f"已哈希密码应原样存储，不应二次哈希: {stored}"


# ============================================================
# P11: 首次启动生成随机密码
# ============================================================

class TestPasswordInitialization:
    """密码初始化测试（P11）。"""

    def test_p11_first_run_generates_random_password(self, tmp_path):
        """P11: 首次启动（无密码 DB）生成随机密码。

        server.py:950 使用 secrets.token_urlsafe(12) 生成随机密码。
        """
        cfg = _make_mock_config(tmp_path)
        db = _make_mock_db(tmp_path)

        # 确保不使用测试模式环境变量（验证随机生成路径）
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WEBUI_TEST_MODE", None)
            os.environ.pop("WEBUI_ADMIN_PASSWORD_FOR_TEST", None)

            with patch("webui.server.PROJECT_ROOT", tmp_path):
                server = WebUIServer(cfg.webui, db, app_config=cfg)
                # 此时 _init_admin_password 尚未调用（在 start() 中）
                # 直接调用以验证首次启动逻辑
                server._init_admin_password()

        # 验证 _has_password 被设为 True
        assert server._has_password is True, "首次启动应设置 _has_password=True"
        # 验证 DB 中存储了哈希格式的密码
        stored = server._watchlist_db.get_config("ui", "admin_password")
        assert stored, "首次启动应在 DB 中生成密码"
        assert "$" in stored, f"生成的密码应为哈希格式: {stored}"
        parts = stored.split("$", 2)
        assert len(parts) == 3, f"哈希格式错误: {stored}"
        assert parts[1] == "600000", f"迭代次数应为 600000: {parts[1]}"

    def test_p11_existing_password_not_regenerated(self, tmp_path):
        """P11 补充: 已有密码时不重新生成。

        server.py:927-928 检测到已有密码时跳过生成。
        """
        cfg = _make_mock_config(tmp_path)
        db = _make_mock_db(tmp_path)

        with patch("webui.server.PROJECT_ROOT", tmp_path):
            server = WebUIServer(cfg.webui, db, app_config=cfg)
            # 预先写入一个密码哈希
            existing_hash = WebUIServer._hash_password("existingpass")
            server._watchlist_db.set_config("ui", "admin_password", existing_hash)

            # 再次调用 _init_admin_password
            server._init_admin_password()

        stored = server._watchlist_db.get_config("ui", "admin_password")
        # 密码应保持不变（未被重新生成）
        assert stored == existing_hash, \
            f"已有密码不应被重新生成: {stored}"
        assert server._has_password is True

    def test_p11_test_mode_uses_env_password(self, tmp_path):
        """P11 补充: 测试模式使用环境变量密码。

        server.py:942-943 当 WEBUI_TEST_MODE=1 时使用
        WEBUI_ADMIN_PASSWORD_FOR_TEST 环境变量。
        """
        cfg = _make_mock_config(tmp_path)
        db = _make_mock_db(tmp_path)
        test_pw = "env_test_password"

        with patch("webui.server.PROJECT_ROOT", tmp_path), \
             patch.dict(os.environ, {
                 "WEBUI_TEST_MODE": "1",
                 "WEBUI_ADMIN_PASSWORD_FOR_TEST": test_pw,
             }, clear=False):
            server = WebUIServer(cfg.webui, db, app_config=cfg)
            server._init_admin_password()

        stored = server._watchlist_db.get_config("ui", "admin_password")
        assert stored, "测试模式应在 DB 中写入密码"
        # 验证环境变量密码可登录
        assert WebUIServer._check_password(test_pw, stored) is True, \
            "测试模式密码应可被验证"


# ============================================================
# P13: reset_admin.py 密码长度检查
# ============================================================

class TestResetAdminPassword:
    """reset_admin.py 密码重置测试（P13）。"""

    def test_p13_short_password_rejected(self, tmp_path, monkeypatch):
        """P13: reset_admin.py 拒绝长度 < 4 的密码。

        reset_admin.py:87-89 检查密码长度，短密码触发 sys.exit(1)。
        """
        # 创建测试 DB 文件
        db_path = tmp_path / "tmdb_watchlist.db"
        wdb = TmdbWatchlistDb(db_path)
        # 写入一个占位配置确保表存在
        wdb.set_config("ui", "admin_password", "placeholder")

        # 导入 reset_admin 模块
        reset_admin_path = Path(__file__).resolve().parent.parent.parent / "reset_admin.py"
        import importlib.util
        spec = importlib.util.spec_from_file_location("reset_admin", reset_admin_path)
        reset_admin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reset_admin)

        # [已修复] P1-3: patch find_db_path 到 tmp_path，移除 --db 参数
        monkeypatch.setattr(reset_admin, "find_db_path", lambda: str(db_path))

        # 模拟命令行参数：短密码（不带 --db）
        monkeypatch.setattr(sys, "argv", [
            "reset_admin.py", "ab"])

        # 短密码应触发 sys.exit(1)
        with pytest.raises(SystemExit) as exc_info:
            reset_admin.main()
        assert exc_info.value.code == 1, \
            f"短密码应触发 sys.exit(1)，实际: {exc_info.value.code}"

        # 验证 DB 中密码未被修改（仍为 placeholder）
        stored = wdb.get_config("ui", "admin_password")
        assert stored == "placeholder", \
            f"短密码被拒绝后 DB 不应被修改: {stored}"

    def test_p13_valid_password_accepted(self, tmp_path, monkeypatch):
        """P13 补充: 合法长度密码被接受并写入 DB。"""
        db_path = tmp_path / "tmdb_watchlist.db"
        wdb = TmdbWatchlistDb(db_path)

        reset_admin_path = Path(__file__).resolve().parent.parent.parent / "reset_admin.py"
        import importlib.util
        spec = importlib.util.spec_from_file_location("reset_admin", reset_admin_path)
        reset_admin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reset_admin)

        # [已修复] P1-3: patch find_db_path 到 tmp_path，移除 --db 参数
        monkeypatch.setattr(reset_admin, "find_db_path", lambda: str(db_path))

        valid_password = "validpass123"
        monkeypatch.setattr(sys, "argv", [
            "reset_admin.py", valid_password])

        # 不应抛出 SystemExit（正常完成）
        reset_admin.main()

        # 验证 DB 中写入了哈希格式的密码
        stored = wdb.get_config("ui", "admin_password")
        assert stored, "合法密码应写入 DB"
        assert "$" in stored, f"应存储哈希格式: {stored}"
        assert valid_password not in stored, "不应存储明文"
        # 验证哈希可被 WebUIServer._check_password 验证
        assert WebUIServer._check_password(valid_password, stored) is True

    def test_p13_hash_algorithm_consistent(self, tmp_path, monkeypatch):
        """P13 补充: reset_admin.py 的 hash_password 与 WebUIServer 算法一致。

        三处哈希函数（server.py / routes.py / reset_admin.py）使用相同算法，
        使登录端 _check_password 可正确验证。
        """
        reset_admin_path = Path(__file__).resolve().parent.parent.parent / "reset_admin.py"
        import importlib.util
        spec = importlib.util.spec_from_file_location("reset_admin", reset_admin_path)
        reset_admin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reset_admin)

        password = "consistency_test"
        # reset_admin 哈希
        reset_hash = reset_admin.hash_password(password)
        # WebUIServer 应能验证
        assert WebUIServer._check_password(password, reset_hash) is True, \
            "reset_admin.py 生成的哈希应可被 WebUIServer._check_password 验证"

    def test_p13_db_param_rejected(self, tmp_path, monkeypatch):
        """[已修复] P1-3: reset_admin.py 拒绝 --db 参数。"""
        reset_admin_path = Path(__file__).resolve().parent.parent.parent / "reset_admin.py"
        import importlib.util
        spec = importlib.util.spec_from_file_location("reset_admin", reset_admin_path)
        reset_admin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reset_admin)

        monkeypatch.setattr(sys, "argv", [
            "reset_admin.py", "--db=/arbitrary/path.db"])

        with pytest.raises(SystemExit) as exc_info:
            reset_admin.main()
        assert exc_info.value.code == 1, \
            f"--db 参数应被拒绝并退出，实际: {exc_info.value.code}"


# ============================================================
# P14: 密码不写入日志文件
# ============================================================

class TestPasswordLogging:
    """密码日志安全测试（P14）。"""

    def test_p14_password_not_in_log_records(self, tmp_path, capsys, caplog):
        """P14: 密码明文不写入 logging 记录。

        server.py:966 使用 print() 输出密码到控制台，
        而非 logging.info()，确保密码不进入日志文件。
        本测试从 stdout 提取实际密码明文，反向验证该明文不出现在 caplog.text 中。
        与同文件 test_p14_password_printed_to_stdout_not_log 风格一致。
        """
        cfg = _make_mock_config(tmp_path)
        db = _make_mock_db(tmp_path)

        with patch("webui.server.PROJECT_ROOT", tmp_path), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WEBUI_TEST_MODE", None)
            os.environ.pop("WEBUI_ADMIN_PASSWORD_FOR_TEST", None)

            with caplog.at_level(logging.DEBUG, logger="root"):
                server = WebUIServer(cfg.webui, db, app_config=cfg)
                server._init_admin_password()

        captured = capsys.readouterr()
        stored = server._watchlist_db.get_config("ui", "admin_password")
        assert stored, "应已生成密码"

        # 从 stdout 提取密码明文（格式: [WebUI] 管理密码: <password>）
        stdout_lines = [l for l in captured.out.splitlines() if "管理密码" in l]
        assert stdout_lines, "stdout 中应有管理密码输出"
        password_line = stdout_lines[0]
        printed_password = ""
        if ":" in password_line:
            printed_password = password_line.split(":", 1)[1].strip()

        # 核心断言：提取的密码明文不应出现在 logging 记录中
        if printed_password and len(printed_password) > 3:
            assert printed_password not in caplog.text, \
                f"密码明文不应出现在 logging 记录中: {printed_password}"

    def test_p14_password_printed_to_stdout_not_log(self, tmp_path, capsys, caplog):
        """P14 补充: 密码通过 print() 输出到 stdout，而非 logging。

        server.py:966 使用 print(f"[WebUI] 管理密码: {new_password}")。
        """
        cfg = _make_mock_config(tmp_path)
        db = _make_mock_db(tmp_path)

        with patch("webui.server.PROJECT_ROOT", tmp_path), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WEBUI_TEST_MODE", None)
            os.environ.pop("WEBUI_ADMIN_PASSWORD_FOR_TEST", None)

            with caplog.at_level(logging.DEBUG, logger="root"):
                server = WebUIServer(cfg.webui, db, app_config=cfg)
                server._init_admin_password()

        captured = capsys.readouterr()
        stored = server._watchlist_db.get_config("ui", "admin_password")

        # 密码应通过 print 输出到 stdout（包含 "管理密码:" 前缀）
        assert "管理密码" in captured.out, \
            f"密码应通过 print 输出到 stdout: {captured.out}"

        # 从 stdout 中提取实际密码（print 输出的明文）
        # 格式: [WebUI] 管理密码: <password>
        stdout_lines = [l for l in captured.out.splitlines() if "管理密码" in l]
        assert stdout_lines, "stdout 中应有管理密码输出"
        # 提取密码明文
        password_line = stdout_lines[0]
        # 密码在冒号后
        if ":" in password_line:
            printed_password = password_line.split(":", 1)[1].strip()
            if printed_password:
                # 验证提取的密码可被 _check_password 验证
                assert WebUIServer._check_password(printed_password, stored) is True, \
                    f"stdout 输出的密码应可验证: {printed_password}"

        # 关键验证：logging 记录中不应包含密码明文
        log_text = caplog.text
        # 提取的密码明文不应出现在日志中
        if printed_password and len(printed_password) > 3:
            assert printed_password not in log_text, \
                f"密码明文不应出现在 logging 记录中: {printed_password}"
