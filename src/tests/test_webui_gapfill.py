"""WebUI 回归覆盖补全测试。

覆盖此前零测试引用/仅鉴权级 smoke 的面：
- webui.server.FontProxyMixin 四方法（CDN 归一化 / 302 / CSS 重写 / 文件代理）
- webui.routes.handle_records_api 参数校验与分页透传
- webui.routes._handle_restart_webui 受理语义与后台重启序列
- webui.routes._do_match_refresh 成功/异常分支与 running 复位

期望值全部由各函数 docstring 契约与调用方（do_GET/do_POST 分派）语义推导。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webui.routes import (  # noqa: E402
    _do_match_refresh,
    _handle_restart_webui,
    handle_records_api,
)
from webui.server import FontProxyMixin  # noqa: E402


# ── FontProxyMixin._configured_cdn_host ──────────────────────────────

def _make_mixin(tmdb_host):
    mixin = FontProxyMixin()
    mixin._config = SimpleNamespace(tmdb=SimpleNamespace(host=tmdb_host))
    return mixin


class TestConfiguredCdnHost:
    @pytest.mark.parametrize("raw,expected", [
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("cdn.example.com", "https://cdn.example.com"),
        ("cdn.example.com/", "https://cdn.example.com"),
        ("  cdn.example.com/  ", "https://cdn.example.com"),
        ("https://cdn.example.com/", "https://cdn.example.com"),
        ("http://cdn.example.com", "http://cdn.example.com"),
    ])
    def test_host_normalization(self, raw, expected):
        assert _make_mixin(raw)._configured_cdn_host() == expected


# ── FontProxyMixin._redirect_to_configured_cdn ───────────────────────

def _with_send_stubs(m):
    m.headers = {"User-Agent": "ua"}
    m.send_error = MagicMock()
    m.send_response = MagicMock()
    m.send_header = MagicMock()
    m.end_headers = MagicMock()
    m.wfile = MagicMock()
    return m


class TestRedirectToConfiguredCdn:
    def test_no_host_returns_502(self):
        m = _with_send_stubs(_make_mixin(""))
        m._redirect_to_configured_cdn("/fonts/css/a")
        assert m.send_error.call_args.args[0] == 502
        m.send_response.assert_not_called()

    def test_redirects_302_with_location_and_no_store(self):
        m = _with_send_stubs(_make_mixin("https://cdn.example.com"))
        m._redirect_to_configured_cdn("/fonts/css/a", "family=x")
        m.send_response.assert_called_once_with(302)
        headers = {c.args[0]: c.args[1] for c in m.send_header.call_args_list}
        assert headers["Location"] == "https://cdn.example.com/fonts/css/a?family=x"
        assert headers["Cache-Control"] == "no-store"
        assert headers["Access-Control-Allow-Origin"] == "*"


# ── FontProxyMixin._proxy_google_font_css / _proxy_google_font_file ──

class _FakeUpstream:
    def __init__(self, body, content_type="text/css"):
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self, size=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestProxyGoogleFontCss:
    def test_rewrites_gstatic_urls_to_local_proxy(self):
        m = _with_send_stubs(_make_mixin(""))
        css = b"@font-face{src:url(https://fonts.gstatic.com/s/x.woff2)}"
        with patch("urllib.request.urlopen", return_value=_FakeUpstream(css)):
            m._proxy_google_font_css("/fonts/css/f", "family=x")
        body = m.wfile.write.call_args.args[0]
        assert b"/fonts/gstatic/s/x.woff2" in body
        assert b"https://fonts.gstatic.com" not in body
        types = {c.args[0]: c.args[1] for c in m.send_header.call_args_list}
        assert types["Content-Type"] == "text/css; charset=utf-8"

    def test_upstream_failure_returns_502(self):
        m = _with_send_stubs(_make_mixin(""))
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            m._proxy_google_font_css("/fonts/css/f", "family=x")
        assert m.send_error.call_args.args[0] == 502


class TestProxyGoogleFontFile:
    def test_forwards_bytes_and_content_type(self):
        m = _with_send_stubs(_make_mixin(""))
        with patch("urllib.request.urlopen",
                   return_value=_FakeUpstream(b"FONT", "font/woff2")):
            m._proxy_google_font_file("/fonts/gstatic/a.woff2")
        assert m.wfile.write.call_args.args[0] == b"FONT"
        types = {c.args[0]: c.args[1] for c in m.send_header.call_args_list}
        assert types["Content-Type"] == "font/woff2"

    def test_oversized_font_is_blocked_with_502(self):
        m = _with_send_stubs(_make_mixin(""))
        oversized = b"x" * (5 * 1024 * 1024 + 1)
        with patch("urllib.request.urlopen",
                   return_value=_FakeUpstream(oversized, "font/woff2")):
            m._proxy_google_font_file("/fonts/gstatic/big.woff2")
        assert m.send_error.call_args.args[0] == 502
        m.wfile.write.assert_not_called()

    def test_upstream_failure_returns_502(self):
        m = _with_send_stubs(_make_mixin(""))
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            m._proxy_google_font_file("/fonts/gstatic/a.woff2")
        assert m.send_error.call_args.args[0] == 502


# ── routes.handle_records_api ────────────────────────────────────────

class TestHandleRecordsApi:
    @staticmethod
    def _run(query):
        handler = MagicMock()
        captured = {}

        def fake_paginated(handler_, area, page=1, page_size=100, search=""):
            captured.update(area=area, page=page,
                            page_size=page_size, search=search)
            return {"total": 0, "page": page,
                    "page_size": page_size, "records": []}

        with patch("webui.routes._get_records_paginated",
                   side_effect=fake_paginated):
            handle_records_api(handler, query)
        return handler, captured

    def test_invalid_area_rejected_without_pagination_call(self):
        handler, captured = self._run({"area": ["z"]})
        handler._send_json.assert_called_once()
        args = handler._send_json.call_args.args
        assert args[0] == {"error": "无效区域"}
        assert args[1] == 400
        assert captured == {}

    def test_defaults_page_and_page_size(self):
        _, captured = self._run({"area": ["b"]})
        assert captured["area"] == "b"
        assert captured["page"] == 1
        assert captured["page_size"] == 100
        assert captured["search"] == ""

    @pytest.mark.parametrize("raw,clamped", [("0", 1), ("-5", 1), ("3", 3)])
    def test_page_clamped_to_minimum_one(self, raw, clamped):
        _, captured = self._run({"area": ["a"], "page": [raw]})
        assert captured["page"] == clamped

    @pytest.mark.parametrize("raw,clamped", [("0", 1), ("10000", 500),
                                             ("42", 42)])
    def test_page_size_clamped_to_1_500(self, raw, clamped):
        _, captured = self._run({"area": ["a"], "page_size": [raw]})
        assert captured["page_size"] == clamped

    def test_search_is_stripped_and_forwarded(self):
        _, captured = self._run({"area": ["c"], "search": ["  凡人修仙  "]})
        assert captured["search"] == "凡人修仙"


# ── routes._handle_restart_webui ─────────────────────────────────────

class TestHandleRestartWebui:
    @staticmethod
    def _wait_until(predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def _make_ws(self, app_running, watchlist_db=None):
        ws = MagicMock()
        ws._app_running = app_running
        ws._watchlist_db = watchlist_db
        events = []
        ws.stop_main.side_effect = lambda: events.append("stop")
        ws.start_main.side_effect = lambda: (events.append("start"),
                                             {"success": True})[1]
        return ws, events

    def test_accepts_immediately_with_success_payload(self):
        ws, events = self._make_ws(app_running=True)
        handler = MagicMock()
        _handle_restart_webui(handler, ws)
        handler._send_json.assert_called_once_with(
            {"success": True, "message": "正在重启主程序..."})
        assert events == []

    def test_running_state_restarts_stop_then_start(self):
        ws, events = self._make_ws(app_running=True)
        handler = MagicMock()
        _handle_restart_webui(handler, ws)
        assert self._wait_until(lambda: "start" in events)
        assert events == ["stop", "start"]

    def test_stopped_state_starts_without_stop(self):
        ws, events = self._make_ws(app_running=False)
        handler = MagicMock()
        _handle_restart_webui(handler, ws)
        assert self._wait_until(lambda: "start" in events)
        assert events == ["start"]

    def test_watchlist_db_logging_failure_does_not_block(self):
        bad_db = MagicMock()
        bad_db.log_tmdb_operation.side_effect = RuntimeError("db boom")
        ws, events = self._make_ws(app_running=False, watchlist_db=bad_db)
        handler = MagicMock()
        _handle_restart_webui(handler, ws)
        handler._send_json.assert_called_once_with(
            {"success": True, "message": "正在重启主程序..."})
        assert self._wait_until(lambda: "start" in events)


# ── routes._do_match_refresh ─────────────────────────────────────────

class TestDoMatchRefresh:
    @staticmethod
    def _ws(tmdb_cfg=None):
        return SimpleNamespace(
            _watchlist_db=None,
            _config=SimpleNamespace(tmdb=tmdb_cfg),
            _match_refresh_lock=threading.Lock(),
            _match_refresh_result=None,
            _match_refresh_running=True,
        )

    def test_success_stores_counts_and_resets_running(self):
        ws = self._ws()
        with patch("webui.routes.refresh_watchlist_match_state",
                   return_value={"matched": 2, "total": 5}) as refresh:
            _do_match_refresh(ws)
        refresh.assert_called_once_with(ws, 0.60, 0.3)
        assert ws._match_refresh_result == {"matched": 2, "total": 5}
        assert ws._match_refresh_running is False

    def test_configured_thresholds_are_forwarded(self):
        ws = self._ws(tmdb_cfg=SimpleNamespace(fuzzy_threshold="0.8",
                                               anime_min_ep_ratio=0.5))
        with patch("webui.routes.refresh_watchlist_match_state",
                   return_value={}) as refresh:
            _do_match_refresh(ws)
        refresh.assert_called_once_with(ws, 0.8, 0.5)

    def test_exception_sets_internal_error_and_resets_running(self):
        ws = self._ws()
        with patch("webui.routes.refresh_watchlist_match_state",
                   side_effect=RuntimeError("match boom")):
            _do_match_refresh(ws)
        assert ws._match_refresh_result == {"error": "internal_error"}
        assert ws._match_refresh_running is False
