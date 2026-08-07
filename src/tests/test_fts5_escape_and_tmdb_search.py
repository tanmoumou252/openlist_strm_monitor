"""
FTS5 查询转义与 TMDB 综合搜索 API 测试。

覆盖需求 27 的两个未测试组件：
- `_escape_fts5_query`：FTS5 特殊字符转义（routes.py 中的纯函数）
- `/api/tmdb/search`：TMDB 综合搜索路由（同时搜索电影和电视剧）

测试策略：
- `_escape_fts5_query` 为纯函数，直接导入并验证转义结果
- `/api/tmdb/search` 通过真实 HTTPServer + mock TmdbClient 验证响应契约
- 不依赖真实 TMDB 网络服务
"""

from __future__ import annotations

import json
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
from webui.routes import _escape_fts5_query  # noqa: E402


# ============================================================
# _escape_fts5_query 纯函数测试
# ============================================================

class TestEscapeFts5Query:
    """测试 FTS5 查询清理函数。"""

    def test_escape_star(self):
        """星号应被移除"""
        result = _escape_fts5_query("test*")
        assert "*" not in result
        assert "test" in result

    def test_escape_minus(self):
        """连字符在词中间应保留，首尾连字符应移除"""
        result = _escape_fts5_query("test-name")
        assert "test-name" in result  # 词中间保留
        result2 = _escape_fts5_query("-test")
        assert not result2.startswith("-")  # 首连字符移除
        result3 = _escape_fts5_query("test-")
        assert not result3.endswith("-")  # 尾连字符移除

    def test_escape_plus(self):
        """加号应被移除"""
        result = _escape_fts5_query("test+name")
        assert "+" not in result
        assert "test" in result
        assert "name" in result

    def test_escape_double_quote(self):
        """双引号应被移除，结果用引号包裹（FTS5 短语精确匹配，提交 1ab6826）"""
        result = _escape_fts5_query('test"name')
        # 内部引号被移除，外部包裹引号
        assert result == '"testname"'
        assert "test" in result
        assert "name" in result

    def test_escape_parentheses(self):
        """圆括号应被替换为空格"""
        result = _escape_fts5_query("test(name)")
        assert "(" not in result
        assert ")" not in result
        assert "test" in result
        assert "name" in result

    def test_escape_braces(self):
        """花括号应被替换为空格"""
        result = _escape_fts5_query("test{name}")
        assert "{" not in result
        assert "}" not in result
        assert "test" in result
        assert "name" in result

    def test_escape_brackets(self):
        """方括号应被替换为空格"""
        result = _escape_fts5_query("test[name]")
        assert "[" not in result
        assert "]" not in result
        assert "test" in result
        assert "name" in result

    def test_escape_caret(self):
        """脱字符应被移除"""
        result = _escape_fts5_query("test^name")
        assert "^" not in result
        assert "test" in result
        assert "name" in result

    def test_escape_tilde(self):
        """波浪号应被移除"""
        result = _escape_fts5_query("test~name")
        assert "~" not in result
        assert "test" in result
        assert "name" in result

    def test_escape_colon(self):
        """冒号应被移除"""
        result = _escape_fts5_query("test:name")
        assert ":" not in result
        assert "test" in result
        assert "name" in result

    def test_escape_backslash(self):
        """反斜杠应被替换为空格"""
        result = _escape_fts5_query("test\\name")
        assert "\\" not in result
        assert "test" in result
        assert "name" in result

    def test_escape_all_special_chars(self):
        """所有特殊字符同时出现应全部被处理，结果用引号包裹"""
        query = '*-+"(){}[]^~:\\'
        escaped = _escape_fts5_query(query)
        # 剥离外层包裹引号后验证内部无运算符字符
        inner = escaped[1:-1] if escaped.startswith('"') and escaped.endswith('"') else escaped
        # 验证所有运算符字符都被移除或替换
        for char in ['*', '+', '"', '(', ')', '{', '}', '[', ']', '^', '~', ':', '\\']:
            assert char not in inner, f"字符 {char} 不应在清理结果中"
        # 结果应以引号包裹
        assert escaped.startswith('"') and escaped.endswith('"')

    def test_plain_text_unchanged(self):
        """普通文本（无特殊字符）应保持不变，但被引号包裹（FTS5 短语精确匹配）"""
        assert _escape_fts5_query("普通电影名") == '"普通电影名"'
        assert _escape_fts5_query("Movie Title 2024") == '"Movie Title 2024"'

    def test_empty_string(self):
        """空字符串应返回空引号对"""
        assert _escape_fts5_query("") == '""'

    def test_chinese_with_special(self):
        """中文混合特殊字符应正确清理"""
        result = _escape_fts5_query("电影:测试*")
        # 冒号和星号都应被移除
        assert ":" not in result
        assert "*" not in result
        assert "电影" in result
        assert "测试" in result

    def test_escape_bracketed_anime(self):
        """真实番剧名含 [限制级] 等方括号：(){}[] 替换为空格，主名仍可命中。"""
        result = _escape_fts5_query("进击的巨人[限制级]")
        # 方括号被替换为空格
        assert "[" not in result and "]" not in result
        # 主名保留，且方括号处变为空格分隔（结果被引号包裹）
        assert result == '"进击的巨人 限制级"', f"期望 '\"进击的巨人 限制级\"'，实际 {result!r}"
        # 主名可独立作为搜索词命中（列表搜索场景下转义后能搜到主名）
        assert _escape_fts5_query("进击的巨人") == '"进击的巨人"'

    def test_escape_colon_star(self):
        """'电影：测试*'：全角冒号不在移除集内（保留），星号移除。

        实测：_escape_fts5_query 仅移除半角 *+"^~: 与 (){}[]（→空格），
        全角冒号 U+FF1A 保留，故输出为 '电影：测试'。
        """
        result = _escape_fts5_query("电影：测试*")
        assert "*" not in result
        assert "：" in result, f"全角冒号应保留，实际 {result!r}"
        assert "电影" in result and "测试" in result

    def test_escape_fullwidth(self):
        """'Spy×Family'：连字符/乘号处理——× 非 FTS5 运算符应保留为词内字符。"""
        result = _escape_fts5_query("Spy×Family")
        # ×（U+00D7 乘号）不在移除/空格化集合内，应原样保留（结果被引号包裹）
        assert result == '"Spy×Family"', f"期望 '\"Spy×Family\"'，实际 {result!r}"
        # 对照：普通半角连字符在词内保留（已有 test_hyphen_in_middle_preserved 覆盖）
        assert "Family" in result

    def test_no_special_chars_returns_same(self):
        """不含特殊字符的输入应原样返回（但被引号包裹）"""
        assert _escape_fts5_query("hello world") == '"hello world"'
        assert _escape_fts5_query("12345") == '"12345"'

    def test_hyphen_in_middle_preserved(self):
        """词中间的连字符应保留（如 test-123）"""
        result = _escape_fts5_query("test-123")
        assert "test-123" in result

    def test_multiple_spaces_collapsed(self):
        """多个连续空白应被合并为单个空格（结果被引号包裹）"""
        result = _escape_fts5_query("test   name")
        assert result == '"test name"'


# ============================================================
# /api/tmdb/search 路由测试
# ============================================================

def _free_port() -> int:
    """获取一个空闲端口。"""
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
    db.get_b_status_counts.return_value = {"valid": 0, "orphan": 0, "unknown": 0}
    db.get_db_file_size.return_value = 0
    db.get_subtitle_by_local.return_value = None
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
        test_password = "test_password_123"
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


def _http_get(base_url: str, path: str, session_token: str | None = None, timeout: float = 3.0):
    """发送 GET 请求并返回 (status, headers, body_dict_or_bytes)。"""
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


def _http_post(base_url: str, path: str, data, session_token: str | None = None,
               timeout: float = 3.0):
    """发送 POST 请求并返回 (status, headers, body_dict_or_bytes)。"""
    url = f"{base_url}{path}"
    body = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
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


class TestTmdbSearchRoute:
    """测试 /api/tmdb/search 综合搜索路由。"""

    def test_search_missing_query_returns_400(self, webui_server):
        """缺少 query 参数应返回 400"""
        server, base, session_token = webui_server
        # 设置 mock tmdb_client（即使有 client，空 query 也应 400）
        server._tmdb_client = MagicMock()
        status, _, resp = _http_get(base, "/api/tmdb/search", session_token)
        assert status == 400
        assert "query" in resp.get("error", "").lower()

    def test_search_empty_query_returns_400(self, webui_server):
        """空 query（仅空白）应返回 400"""
        server, base, session_token = webui_server
        server._tmdb_client = MagicMock()
        status, _, resp = _http_get(base, "/api/tmdb/search?query=%20%20", session_token)
        assert status == 400
        assert "query" in resp.get("error", "").lower()

    def test_search_returns_movies_and_tv(self, webui_server):
        """正常搜索应同时返回电影和电视剧结果"""
        server, base, session_token = webui_server

        mock_tmdb = MagicMock()
        mock_tmdb.search_movie.return_value = [
            {"id": 1, "title": "Test Movie", "release_date": "2024-01-01"},
            {"id": 2, "title": "Test Movie 2", "release_date": "2023-01-01"},
        ]
        mock_tmdb.search_tv.return_value = [
            {"id": 100, "name": "Test Show", "first_air_date": "2024-01-01"},
        ]
        server._tmdb_client = mock_tmdb

        status, _, resp = _http_get(base, "/api/tmdb/search?query=test", session_token)
        assert status == 200
        assert resp.get("query") == "test"
        assert isinstance(resp.get("movies"), list)
        assert isinstance(resp.get("tv_shows"), list)
        assert len(resp["movies"]) == 2
        assert len(resp["tv_shows"]) == 1
        # 验证调用了 search_movie 和 search_tv
        mock_tmdb.search_movie.assert_called_once_with("test", page=1)
        mock_tmdb.search_tv.assert_called_once_with("test", page=1)

    def test_search_limits_to_10_results(self, webui_server):
        """搜索结果应限制为前 10 条"""
        server, base, session_token = webui_server

        mock_tmdb = MagicMock()
        mock_tmdb.search_movie.return_value = [
            {"id": i, "title": f"Movie {i}"} for i in range(15)
        ]
        mock_tmdb.search_tv.return_value = [
            {"id": i, "name": f"Show {i}"} for i in range(15)
        ]
        server._tmdb_client = mock_tmdb

        status, _, resp = _http_get(base, "/api/tmdb/search?query=test", session_token)
        assert status == 200
        assert len(resp["movies"]) == 10
        assert len(resp["tv_shows"]) == 10

    def test_search_empty_results(self, webui_server):
        """无匹配结果时应返回空列表"""
        server, base, session_token = webui_server

        mock_tmdb = MagicMock()
        mock_tmdb.search_movie.return_value = []
        mock_tmdb.search_tv.return_value = []
        server._tmdb_client = mock_tmdb

        status, _, resp = _http_get(base, "/api/tmdb/search?query=nonexistent", session_token)
        assert status == 200
        assert resp["movies"] == []
        assert resp["tv_shows"] == []

    def test_search_url_decodes_query(self, webui_server):
        """query 参数应正确 URL 解码"""
        server, base, session_token = webui_server

        mock_tmdb = MagicMock()
        mock_tmdb.search_movie.return_value = []
        mock_tmdb.search_tv.return_value = []
        server._tmdb_client = mock_tmdb

        # 中文搜索词需要 URL 编码
        status, _, resp = _http_get(
            base, "/api/tmdb/search?query=" + urllib.parse.quote("电影名"),
            session_token)
        assert status == 200
        assert resp.get("query") == "电影名"
        mock_tmdb.search_movie.assert_called_once_with("电影名", page=1)

    def test_search_strips_whitespace(self, webui_server):
        """query 前后空白应被去除"""
        server, base, session_token = webui_server

        mock_tmdb = MagicMock()
        mock_tmdb.search_movie.return_value = []
        mock_tmdb.search_tv.return_value = []
        server._tmdb_client = mock_tmdb

        status, _, resp = _http_get(
            base, "/api/tmdb/search?query=%20test%20", session_token)
        assert status == 200
        assert resp.get("query") == "test"
        mock_tmdb.search_movie.assert_called_once_with("test", page=1)

    def test_search_with_special_chars(self, webui_server):
        """包含特殊字符的 query 应正常传递给 TMDB 客户端"""
        server, base, session_token = webui_server

        mock_tmdb = MagicMock()
        mock_tmdb.search_movie.return_value = []
        mock_tmdb.search_tv.return_value = []
        server._tmdb_client = mock_tmdb

        # 注意：/api/tmdb/search 路由本身不做 FTS5 转义（那是给本地 FTS 搜索用的）
        # TMDB 在线搜索直接传原始 query
        status, _, resp = _http_get(
            base, "/api/tmdb/search?query=" + urllib.parse.quote("S01E01"),
            session_token)
        assert status == 200
        mock_tmdb.search_movie.assert_called_once_with("S01E01", page=1)

    def test_search_tmdb_error_returns_500(self, webui_server):
        """TMDB 客户端抛异常时应返回 500"""
        server, base, session_token = webui_server

        mock_tmdb = MagicMock()
        mock_tmdb.search_movie.side_effect = Exception("TMDB API error")
        server._tmdb_client = mock_tmdb

        status, _, resp = _http_get(base, "/api/tmdb/search?query=test", session_token)
        assert status == 500
        assert "error" in resp


# 需要导入 urllib.parse 用于 URL 编码
import urllib.parse  # noqa: E402
