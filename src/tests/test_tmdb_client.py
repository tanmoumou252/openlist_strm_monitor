"""
Unit tests for tmdb_client.py
Covers: TmdbClient init, account cache, request logic, search,
        details, watchlist, and create_tmdb_client factory.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add src/ to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tmdb_client as _module
from tmdb_client import TmdbClient, create_tmdb_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(data: dict, status: int = 200) -> MagicMock:
    """Build a mock HTTP response that behaves like urllib.request's response."""
    body = json.dumps(data).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _patch_opener(response_data: dict):
    """Context manager: patch urllib.request.build_opener to return fake response."""
    resp = _make_response(response_data)
    opener = MagicMock()
    opener.open.return_value = resp
    return patch("urllib.request.build_opener", return_value=opener)


def _http_error_429(retry_after_value: str) -> urllib.error.HTTPError:
    """构造带 Retry-After 头的 429 HTTPError。"""
    hdrs = Message()
    hdrs["Retry-After"] = retry_after_value
    return urllib.error.HTTPError(
        "http://example.invalid", 429, "Too Many Requests", hdrs, None)


# ===========================================================================
# TestTmdbClientInit
# ===========================================================================


class TestTmdbClientInit:
    def test_init_with_access_token(self, tmp_path):
        with patch.object(_module, "_CACHE_FILE", tmp_path / ".tmdb_account.json"):
            client = TmdbClient(access_token="tok123")
        assert client.access_token == "tok123"
        assert client._use_api_key_auth is False

    def test_init_with_api_key(self, tmp_path):
        with patch.object(_module, "_CACHE_FILE", tmp_path / ".tmdb_account.json"):
            client = TmdbClient(api_key="key456")
        assert client.api_key == "key456"
        assert client._use_api_key_auth is True

    def test_init_with_both_access_token_takes_priority(self, tmp_path):
        with patch.object(_module, "_CACHE_FILE", tmp_path / ".tmdb_account.json"):
            client = TmdbClient(access_token="tok123", api_key="key456")
        # When access_token present, use Bearer — not api_key auth
        assert client._use_api_key_auth is False

    def test_init_with_none_both_empty(self, tmp_path):
        with patch.object(_module, "_CACHE_FILE", tmp_path / ".tmdb_account.json"):
            client = TmdbClient()
        assert client.access_token == ""
        assert client.api_key == ""
        assert client._use_api_key_auth is False

    def test_init_loads_cache_when_access_token(self, tmp_path):
        cache_file = tmp_path / ".tmdb_account.json"
        cache_file.write_text(
            json.dumps({"account_id": "99", "username": "testuser",
                        "avatar_path": "/a.jpg", "ts": time.time()}),
            encoding="utf-8",
        )
        with patch.object(_module, "_CACHE_FILE", cache_file):
            client = TmdbClient(access_token="tok")
        assert client._account_id == "99"
        assert client._username == "testuser"

    def test_init_no_cache_load_without_access_token(self, tmp_path):
        cache_file = tmp_path / ".tmdb_account.json"
        cache_file.write_text(
            json.dumps({"account_id": "99", "username": "u", "ts": time.time()}),
            encoding="utf-8",
        )
        with patch.object(_module, "_CACHE_FILE", cache_file):
            client = TmdbClient(api_key="k")
        # cache should NOT be loaded when no access_token
        assert client._account_id == ""


# ===========================================================================
# TestTmdbClientAccountCache
# ===========================================================================


class TestTmdbClientAccountCache:
    def _client(self, tmp_path, **kwargs) -> TmdbClient:
        with patch.object(_module, "_CACHE_FILE", tmp_path / ".tmdb_account.json"):
            return TmdbClient(**kwargs)

    def test_load_cached_account_id(self, tmp_path):
        cache_file = tmp_path / ".tmdb_account.json"
        cache_file.write_text(
            json.dumps({"account_id": "42", "username": "alice",
                        "avatar_path": "", "ts": time.time()}),
            encoding="utf-8",
        )
        with patch.object(_module, "_CACHE_FILE", cache_file):
            client = TmdbClient(access_token="tok")
        assert client._account_id == "42"
        assert client._username == "alice"

    def test_cache_expired_does_not_load(self, tmp_path):
        cache_file = tmp_path / ".tmdb_account.json"
        old_ts = time.time() - (8 * 24 * 3600)  # 8 days ago
        cache_file.write_text(
            json.dumps({"account_id": "42", "username": "old", "ts": old_ts}),
            encoding="utf-8",
        )
        with patch.object(_module, "_CACHE_FILE", cache_file):
            client = TmdbClient(access_token="tok")
        # expired, so account_id should not be loaded
        assert client._account_id == ""

    def test_save_cache(self, tmp_path):
        cache_file = tmp_path / ".tmdb_account.json"
        with patch.object(_module, "_CACHE_FILE", cache_file):
            client = TmdbClient(access_token="tok")
            client._account_id = "77"
            client._username = "bob"
            client._avatar_path = "/b.jpg"
            client._save_cached_account_id()
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        assert data["account_id"] == "77"
        assert data["username"] == "bob"
        assert "ts" in data

    def test_account_id_property_auto_fetch(self, tmp_path):
        cache_file = tmp_path / ".tmdb_account.json"
        account_resp = {"id": 55, "username": "carol", "avatar": {}}
        with patch.object(_module, "_CACHE_FILE", cache_file):
            with _patch_opener(account_resp):
                client = TmdbClient(access_token="tok")
                # trigger via property
                aid = client.account_id
        assert aid == "55"

    def test_account_id_api_key_mode_returns_empty(self, tmp_path):
        with patch.object(_module, "_CACHE_FILE", tmp_path / ".tmdb_account.json"):
            client = TmdbClient(api_key="k")
        assert client.account_id == ""


# ===========================================================================
# TestTmdbClientRequest
# ===========================================================================


class TestTmdbClientRequest:
    def _client(self, tmp_path, **kwargs) -> TmdbClient:
        with patch.object(_module, "_CACHE_FILE", tmp_path / ".tmdb_account.json"):
            return TmdbClient(**kwargs)

    def test_request_with_bearer_token(self, tmp_path):
        resp_data = {"success": True}
        client = self._client(tmp_path, access_token="tok")
        with _patch_opener(resp_data) as mock_build:
            result = client.request("/3/authentication")
        assert result == resp_data
        # Verify opener was called
        mock_build.assert_called_once()

    def test_request_with_api_key_in_params(self, tmp_path):
        resp_data = {"results": []}
        client = self._client(tmp_path, api_key="mykey")
        with _patch_opener(resp_data):
            result = client.request("/3/search/movie", {"query": "test"})
        assert result is not None
        assert "results" in result

    def test_request_no_auth_returns_none(self, tmp_path):
        client = self._client(tmp_path)  # no auth
        result = client.request("/3/authentication")
        assert result is None

    def test_request_http_error_returns_none(self, tmp_path):
        client = self._client(tmp_path, access_token="tok")
        opener = MagicMock()
        opener.open.side_effect = Exception("connection refused")
        with patch("urllib.request.build_opener", return_value=opener):
            result = client.request("/3/authentication")
        assert result is None

    def test_request_injects_language_param(self, tmp_path):
        """language is auto-injected unless overridden."""
        resp_data = {"results": []}
        client = self._client(tmp_path, access_token="tok", language="en-US")
        captured_urls = []

        def fake_open(req, timeout=30):
            captured_urls.append(req.full_url)
            return _make_response(resp_data)

        opener = MagicMock()
        opener.open.side_effect = fake_open
        with patch("urllib.request.build_opener", return_value=opener):
            client.request("/3/search/movie", {"query": "test"})

        assert len(captured_urls) == 1
        assert "language=en-US" in captured_urls[0]

    def test_request_with_proxy(self, tmp_path):
        resp_data = {"success": True}
        client = self._client(tmp_path, access_token="tok", proxy="http://127.0.0.1:7890")
        proxy_handler_calls = []

        with patch("urllib.request.ProxyHandler", side_effect=lambda d: proxy_handler_calls.append(d) or MagicMock()):
            with _patch_opener(resp_data):
                client.request("/3/authentication")

        assert len(proxy_handler_calls) == 1
        assert "http" in proxy_handler_calls[0]

    def test_request_retry_after_http_date_falls_back(self, tmp_path):
        """T14: Retry-After 为 HTTP-date 时 float() 抛 ValueError，应回退默认退避而非逃逸。"""
        client = self._client(tmp_path, access_token="tok")
        opener = MagicMock()
        opener.open.side_effect = [
            _http_error_429("Wed, 21 Oct 2015 07:28:00 GMT"),
            _make_response({"success": True}),
        ]
        with patch("urllib.request.build_opener", return_value=opener), \
             patch("time.sleep") as m_sleep:
            result = client.request("/3/authentication")
        assert result == {"success": True}
        assert m_sleep.call_count >= 1, "HTTP-date Retry-After 应回退默认退避并等待"

    def test_request_retry_after_capped(self, tmp_path):
        """T14: Retry-After 过大值应被截断到上限，不无限挂起线程。"""
        client = self._client(tmp_path, access_token="tok")
        opener = MagicMock()
        opener.open.side_effect = [
            _http_error_429("999999"),
            _make_response({"success": True}),
        ]
        with patch("urllib.request.build_opener", return_value=opener), \
             patch("time.sleep") as m_sleep:
            result = client.request("/3/authentication")
        assert result == {"success": True}
        assert m_sleep.call_args[0][0] == 60.0, \
            f"等待秒数应截断到 60，实际 {m_sleep.call_args[0][0]}"

    def test_request_retry_after_negative_falls_back(self, tmp_path):
        """T14: Retry-After 为负值时回退默认退避，避免 time.sleep(负数) 异常。"""
        client = self._client(tmp_path, access_token="tok")
        opener = MagicMock()
        opener.open.side_effect = [
            _http_error_429("-5"),
            _make_response({"success": True}),
        ]
        with patch("urllib.request.build_opener", return_value=opener), \
             patch("time.sleep") as m_sleep:
            result = client.request("/3/authentication")
        assert result == {"success": True}
        assert m_sleep.call_count >= 1


# ===========================================================================
# TestTmdbClientValidateAndAliases
# ===========================================================================


class TestTmdbClientValidateAndAliases:
    def _client(self, tmp_path, **kwargs) -> TmdbClient:
        with patch.object(_module, "_CACHE_FILE", tmp_path / ".tmdb_account.json"):
            return TmdbClient(**kwargs)

    def test_validate_key_success(self, tmp_path):
        client = self._client(tmp_path, access_token="tok")
        with _patch_opener({"success": True}):
            result = client.validate_key()
        assert result is True

    def test_validate_key_failure(self, tmp_path):
        client = self._client(tmp_path, access_token="tok")
        with _patch_opener({"success": False}):
            result = client.validate_key()
        assert result is False

    def test_validate_key_no_auth(self, tmp_path):
        client = self._client(tmp_path)  # no auth
        result = client.validate_key()
        assert result is False

    def test_get_movie_aliases_success(self, tmp_path):
        aliases_data = {
            "titles": [
                {"title": "Avatar 2"},
                {"title": "Avatar: The Way of Water"},
                {"title": ""},  # empty title should be filtered
            ]
        }
        client = self._client(tmp_path, access_token="tok")
        with _patch_opener(aliases_data):
            result = client.get_movie_aliases(123)
        assert len(result) == 2
        assert "Avatar 2" in result
        assert "Avatar: The Way of Water" in result

    def test_get_movie_aliases_empty(self, tmp_path):
        client = self._client(tmp_path, access_token="tok")
        with _patch_opener({"titles": []}):
            result = client.get_movie_aliases(999)
        assert result == []

    def test_get_movie_aliases_request_failure(self, tmp_path):
        client = self._client(tmp_path, access_token="tok")
        with patch.object(client, "request", return_value=None):
            result = client.get_movie_aliases(123)
        assert result == []

    def test_get_tv_aliases_success(self, tmp_path):
        aliases_data = {
            "results": [
                {"title": "Breaking Bad 2"},
                {"title": "Breaking Bad: El Camino"},
            ]
        }
        client = self._client(tmp_path, access_token="tok")
        with _patch_opener(aliases_data):
            result = client.get_tv_aliases(456)
        assert len(result) == 2
        assert "Breaking Bad 2" in result

    def test_get_tv_aliases_empty(self, tmp_path):
        client = self._client(tmp_path, access_token="tok")
        with _patch_opener({"results": []}):
            result = client.get_tv_aliases(999)
        assert result == []

    def test_get_tv_aliases_request_failure(self, tmp_path):
        client = self._client(tmp_path, access_token="tok")
        with patch.object(client, "request", return_value=None):
            result = client.get_tv_aliases(456)
        assert result == []


# ===========================================================================
# TestTmdbClientSearch
# ===========================================================================


class TestTmdbClientSearch:
    def _client(self, tmp_path, **kwargs) -> TmdbClient:
        with patch.object(_module, "_CACHE_FILE", tmp_path / ".tmdb_account.json"):
            return TmdbClient(**kwargs)

    def test_search_movie_success(self, tmp_path):
        movies = [{"id": 1, "title": "Avatar"}]
        client = self._client(tmp_path, access_token="tok")
        with _patch_opener({"results": movies}):
            result = client.search_movie("Avatar")
        assert len(result) == 1
        assert result[0]["title"] == "Avatar"

    def test_search_movie_empty(self, tmp_path):
        client = self._client(tmp_path, access_token="tok")
        with _patch_opener({"results": []}):
            result = client.search_movie("NonExistent Movie XYZ123")
        assert result == []

    def test_search_movie_request_failure_returns_empty(self, tmp_path):
        client = self._client(tmp_path, access_token="tok")
        with patch.object(client, "request", return_value=None):
            result = client.search_movie("anything")
        assert result == []

    def test_search_tv_success(self, tmp_path):
        shows = [{"id": 10, "name": "Breaking Bad"}]
        client = self._client(tmp_path, access_token="tok")
        with _patch_opener({"results": shows}):
            result = client.search_tv("Breaking Bad")
        assert len(result) == 1
        assert result[0]["name"] == "Breaking Bad"

    def test_search_tv_empty(self, tmp_path):
        client = self._client(tmp_path, access_token="tok")
        with _patch_opener({"results": []}):
            result = client.search_tv("XYZ-unknown-show")
        assert result == []


# ===========================================================================
# TestTmdbClientDetails
# ===========================================================================


class TestTmdbClientDetails:
    def _client(self, tmp_path, **kwargs) -> TmdbClient:
        with patch.object(_module, "_CACHE_FILE", tmp_path / ".tmdb_account.json"):
            return TmdbClient(**kwargs)

    def test_get_movie_details(self, tmp_path):
        details = {"id": 123, "title": "Inception", "runtime": 148}
        client = self._client(tmp_path, access_token="tok")
        with _patch_opener(details):
            result = client.get_movie_details(123)
        assert result is not None
        assert result["title"] == "Inception"

    def test_get_tv_details(self, tmp_path):
        details = {"id": 456, "name": "Westworld", "number_of_seasons": 4}
        client = self._client(tmp_path, access_token="tok")
        with _patch_opener(details):
            result = client.get_tv_details(456)
        assert result is not None
        assert result["name"] == "Westworld"

    def test_get_tv_seasons_info(self, tmp_path):
        seasons_data = {
            "id": 1,
            "seasons": [
                {"season_number": 0, "episode_count": 3},  # specials — excluded
                {"season_number": 1, "episode_count": 10},
                {"season_number": 2, "episode_count": 10},
            ],
        }
        client = self._client(tmp_path, access_token="tok")
        with _patch_opener(seasons_data):
            count = client.get_tv_seasons_info(1)
        assert count == 2  # only season 1 and 2

    def test_get_tv_seasons_info_no_data(self, tmp_path):
        client = self._client(tmp_path, access_token="tok")
        with patch.object(client, "request", return_value=None):
            count = client.get_tv_seasons_info(999)
        assert count == 0


# ===========================================================================
# TestTmdbClientWatchlist
# ===========================================================================


class TestTmdbClientWatchlist:
    def _client(self, tmp_path, account_id="42", **kwargs) -> TmdbClient:
        with patch.object(_module, "_CACHE_FILE", tmp_path / ".tmdb_account.json"):
            client = TmdbClient(**kwargs)
            client._account_id = account_id
            return client

    def test_get_watchlist_movies(self, tmp_path):
        movies = [{"id": 1, "title": "Movie A"}]
        client = self._client(tmp_path, access_token="tok")
        with _patch_opener({"results": movies, "page": 1, "total_pages": 1}):
            items, has_next = client.get_watchlist_movies()
        assert len(items) == 1
        assert has_next is False

    def test_get_watchlist_movies_has_next_page(self, tmp_path):
        movies = [{"id": 1}]
        client = self._client(tmp_path, access_token="tok")
        with _patch_opener({"results": movies, "page": 1, "total_pages": 3}):
            items, has_next = client.get_watchlist_movies()
        assert has_next is True

    def test_get_watchlist_tv(self, tmp_path):
        shows = [{"id": 10, "name": "Show A"}]
        client = self._client(tmp_path, access_token="tok")
        with _patch_opener({"results": shows, "page": 1, "total_pages": 1}):
            items, has_next = client.get_watchlist_tv()
        assert len(items) == 1

    def test_watchlist_api_key_mode_skips(self, tmp_path):
        """T5: api_key 模式不支持 watchlist，应 raise 而非返回空清单（防误清本地表）"""
        client = self._client(tmp_path, api_key="k")
        with pytest.raises(RuntimeError):
            client.get_watchlist_movies()

    def test_watchlist_no_account_id(self, tmp_path):
        """T5: 无 account_id 时取回不可信，应 raise 而非返回空清单"""
        client = self._client(tmp_path, account_id="", access_token="tok")
        # no account_id — fetch_account_id is called by the property but returns ""
        # Mock fetch_account_id so it stays empty without making real HTTP requests
        with patch.object(client, "fetch_account_id", return_value=""):
            with pytest.raises(RuntimeError):
                client.get_watchlist_movies()

    def test_fetch_all_watchlist_movies_single_page(self, tmp_path):
        movies = [{"id": i} for i in range(5)]
        client = self._client(tmp_path, access_token="tok")
        with patch.object(client, "get_watchlist_movies",
                          return_value=(movies, False)):
            result = client.fetch_all_watchlist_movies()
        assert len(result) == 5

    def test_fetch_all_watchlist_movies_pagination(self, tmp_path):
        page1 = [{"id": i} for i in range(3)]
        page2 = [{"id": i + 10} for i in range(2)]
        client = self._client(tmp_path, access_token="tok")
        call_count = [0]

        def fake_get_watchlist(page=1):
            call_count[0] += 1
            if page == 1:
                return page1, True
            return page2, False

        with patch.object(client, "get_watchlist_movies", side_effect=fake_get_watchlist):
            with patch("time.sleep"):  # skip sleep
                result = client.fetch_all_watchlist_movies()

        assert len(result) == 5
        assert call_count[0] == 2


# ===========================================================================
# TestCreateTmdbClient
# ===========================================================================


class TestCreateTmdbClient:
    def test_create_returns_client_instance(self, tmp_path):
        with patch.object(_module, "_CACHE_FILE", tmp_path / ".tmdb_account.json"):
            client = create_tmdb_client(access_token="tok")
        assert isinstance(client, TmdbClient)

    def test_create_with_auto_validate_success(self, tmp_path):
        resp_data = {"success": True}
        with patch.object(_module, "_CACHE_FILE", tmp_path / ".tmdb_account.json"):
            with _patch_opener(resp_data):
                client = create_tmdb_client(access_token="tok", auto_validate=True)
        assert isinstance(client, TmdbClient)

    def test_create_with_auto_validate_fail_raises(self, tmp_path):
        resp_data = {"success": False}
        with patch.object(_module, "_CACHE_FILE", tmp_path / ".tmdb_account.json"):
            with _patch_opener(resp_data):
                with pytest.raises(RuntimeError):
                    create_tmdb_client(access_token="tok", auto_validate=True)

    def test_create_no_auth_no_validate(self, tmp_path):
        with patch.object(_module, "_CACHE_FILE", tmp_path / ".tmdb_account.json"):
            client = create_tmdb_client()
        assert isinstance(client, TmdbClient)
        assert client.access_token == ""
