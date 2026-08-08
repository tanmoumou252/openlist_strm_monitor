"""tmdb_watchlist_db.py 单元测试

测试范围：
- 匹配状态读写：``set_match_state`` / ``override_match_state`` / ``get_match_state``
  / ``replace_match_state``（保留手动覆盖）
- 季数缓存：``get_season_count``
- 全量同步 ``sync``：分页拉取、upsert、移除条目物理删除、FTS 同步清理、
  电影/剧集独立事务与失败回滚
- ``_populate_tv_details``：批量补齐季数 / 集数 / 最新集
- TMDB 操作日志：``log_tmdb_operation`` / ``get_tmdb_logs``（过期与行数清理）
- webui_config CRUD：``get_config`` / ``set_config`` / ``get_all_config``
  / ``delete_config``
- 缓存状态与匹配统计：``get_cache_status`` / ``get_match_statistics``

TMDB 客户端全部使用 mock，不发起真实网络请求；不改动 tmdb_client.py。

运行方式：
  python -m pytest src/tests/test_tmdb_watchlist_db.py -v
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tmdb_watchlist_db import TmdbWatchlistDb  # noqa: E402


# ============================================================
# 辅助
# ============================================================

@pytest.fixture
def db(tmp_path):
    """独立临时 DB，避免与仓库中的 tmdb_watchlist.db 互相污染。"""
    return TmdbWatchlistDb(tmp_path / "watchlist.db")


@pytest.fixture(autouse=True)
def _no_sleep():
    """同步逻辑内置分页限速；测试中直接跳过 sleep。"""
    with patch("tmdb_watchlist_db.time.sleep"):
        yield


def _movie(item_id: int, title: str = "电影", **extra) -> dict:
    base = {
        "id": item_id,
        "title": title,
        "original_title": f"Movie {item_id}",
        "overview": "简介",
        "release_date": "2024-01-01",
        "vote_average": 7.5,
        "vote_count": 100,
        "genre_ids": [28, 12],
        "popularity": 12.5,
        "original_language": "zh",
    }
    base.update(extra)
    return base


def _tv(item_id: int, name: str = "剧集", **extra) -> dict:
    base = {
        "id": item_id,
        "name": name,
        "original_name": f"Show {item_id}",
        "overview": "简介",
        "first_air_date": "2023-04-01",
        "vote_average": 8.0,
        "vote_count": 50,
        "genre_ids": [16],
        "origin_country": ["JP"],
        "popularity": 9.0,
        "original_language": "ja",
    }
    base.update(extra)
    return base


def _client(movie_pages=None, tv_pages=None, details=None) -> MagicMock:
    """构建 TMDB 客户端 mock。

    movie_pages / tv_pages 为 [(items, has_next), ...] 序列。
    """
    client = MagicMock()
    client.get_watchlist_movies.side_effect = list(movie_pages or [([], False)])
    client.get_watchlist_tv.side_effect = list(tv_pages or [([], False)])
    details = details or {}
    client.get_tv_details.side_effect = lambda tid: details.get(tid)
    return client


def _raw_rows(db_obj: TmdbWatchlistDb, sql: str, params=()) -> list[tuple]:
    conn = sqlite3.connect(db_obj._db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ============================================================
# Schema 初始化
# ============================================================

class TestSchemaInit:
    """建表必须幂等，并覆盖 match / FTS / 日志 / 配置四类结构。"""

    def test_core_tables_created(self, db):
        names = {r[0] for r in _raw_rows(
            db, "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        assert {"movies", "tv", "meta", "webui_config",
                "tmdb_operation_log"} <= names

    def test_fts_table_created(self, db):
        names = {r[0] for r in _raw_rows(
            db, "SELECT name FROM sqlite_master WHERE type='table'")}
        # T1: 拆分 movies_fts / tv_fts 分表，旧单表 tmdb_watchlist_fts 已移除
        assert {"movies_fts", "tv_fts"} <= names
        assert "tmdb_watchlist_fts" not in names

    def test_match_columns_present(self, db):
        cols = {r[1] for r in _raw_rows(db, "PRAGMA table_info(movies)")}
        assert {"match_status", "match_reason", "match_updated_at",
                "manual_override_at", "manual_override_by"} <= cols

    def test_tv_detail_columns_present(self, db):
        cols = {r[1] for r in _raw_rows(db, "PRAGMA table_info(tv)")}
        assert {"_season_count", "_episode_count",
                "_last_ep_season", "_last_ep_episode"} <= cols

    def test_reopen_is_idempotent(self, tmp_path):
        path = tmp_path / "again.db"
        TmdbWatchlistDb(path)
        second = TmdbWatchlistDb(path)  # 不得抛异常
        assert second.get_cache_status()["cache_item_count"] == 0

    def test_legacy_schema_gets_missing_columns(self, tmp_path):
        """旧库缺少 match 列时，_ensure_column 必须补齐而不是报错。"""
        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE movies (id INTEGER PRIMARY KEY, title TEXT,
                _media_type TEXT DEFAULT 'movie', _synced_at REAL DEFAULT 0);
            CREATE TABLE tv (id INTEGER PRIMARY KEY, name TEXT,
                _season_count INTEGER DEFAULT 0,
                _media_type TEXT DEFAULT 'tv', _synced_at REAL DEFAULT 0);
        """)
        conn.commit()
        conn.close()

        migrated = TmdbWatchlistDb(path)
        cols = {r[1] for r in _raw_rows(migrated, "PRAGMA table_info(movies)")}
        assert "match_status" in cols


# ============================================================
# 匹配状态
# ============================================================

class TestMatchState:
    """收录状态是 TMDB 页面的核心展示字段。"""

    def _seed(self, db):
        db._upsert_movie(_movie(1), time.time())
        db._upsert_tv(_tv(2), time.time())

    def test_new_item_defaults_to_uncomputed(self, db):
        self._seed(db)
        assert db.get_match_state("movie", 1)["match_status"] == "uncomputed"

    def test_get_match_state_missing_returns_none(self, db):
        assert db.get_match_state("movie", 999) is None

    def test_set_match_state_writes_status_and_reason(self, db):
        self._seed(db)
        db.set_match_state("movie", 1, "in_library", "指纹命中")
        state = db.get_match_state("movie", 1)
        assert state["match_status"] == "in_library"
        assert state["match_reason"] == "指纹命中"
        assert state["match_updated_at"] > 0

    def test_set_match_state_does_not_mark_manual(self, db):
        self._seed(db)
        db.set_match_state("movie", 1, "in_library", "自动")
        assert db.get_match_state("movie", 1)["manual_override_at"] == 0

    def test_override_match_state_marks_manual(self, db):
        self._seed(db)
        db.override_match_state("movie", 1, "in_library", "用户确认")
        state = db.get_match_state("movie", 1)
        assert state["manual_override_at"] > 0
        assert state["manual_override_by"] == "manual"

    def test_override_accepts_custom_actor(self, db):
        self._seed(db)
        db.override_match_state("tv", 2, "missing", "人工", manual_override_by="admin")
        assert db.get_match_state("tv", 2)["manual_override_by"] == "admin"

    def test_tv_state_uses_tv_table(self, db):
        self._seed(db)
        db.set_match_state("tv", 2, "missing", "未找到")
        assert db.get_match_state("tv", 2)["match_status"] == "missing"
        # 电影表不受影响
        assert db.get_match_state("movie", 1)["match_status"] == "uncomputed"


class TestReplaceMatchState:
    """批量刷新必须保留手动覆盖，并能为尚未同步的 ID 插入新行。"""

    def test_updates_existing_rows(self, db):
        db._upsert_movie(_movie(1), time.time())
        db.replace_match_state("movie", [(1, "in_library", "auto", 100.0, 0.0, "")])
        assert db.get_match_state("movie", 1)["match_status"] == "in_library"

    def test_inserts_missing_rows(self, db):
        db.replace_match_state("movie", [(42, "missing", "auto", 100.0, 0.0, "")])
        assert db.get_match_state("movie", 42)["match_status"] == "missing"

    def test_skips_manually_overridden_rows(self, db):
        db._upsert_movie(_movie(1), time.time())
        db.override_match_state("movie", 1, "in_library", "用户确认")
        db.replace_match_state("movie", [(1, "missing", "auto", 200.0, 0.0, "")])
        state = db.get_match_state("movie", 1)
        assert state["match_status"] == "in_library"
        assert state["manual_override_by"] == "manual"

    def test_empty_iterable_is_noop(self, db):
        db.replace_match_state("movie", [])
        assert db.get_match_statistics()["total"] == 0

    def test_tv_media_type_routes_to_tv_table(self, db):
        db.replace_match_state("tv", [(7, "in_library", "auto", 1.0, 0.0, "")])
        assert db.get_match_state("tv", 7) is not None
        assert db.get_match_state("movie", 7) is None


# ============================================================
# 季数缓存
# ============================================================

class TestSeasonCount:
    def test_missing_id_returns_zero(self, db):
        assert db.get_season_count(123) == 0

    def test_zero_season_count_returns_zero(self, db):
        db._upsert_tv(_tv(1), time.time())
        assert db.get_season_count(1) == 0

    def test_returns_stored_count(self, db):
        db._upsert_tv(_tv(1, number_of_seasons=4), time.time())
        assert db.get_season_count(1) == 4

    def test_upsert_updates_count_when_api_provides_it(self, db):
        now = time.time()
        db._upsert_tv(_tv(1, number_of_seasons=2), now)
        db._upsert_tv(_tv(1, number_of_seasons=3), now)
        assert db.get_season_count(1) == 3

    def test_upsert_preserves_count_when_api_omits_it(self, db):
        now = time.time()
        db._upsert_tv(_tv(1, number_of_seasons=2), now)
        db._upsert_tv(_tv(1), now)
        assert db.get_season_count(1) == 2

    def test_episode_count_preserved(self, db):
        now = time.time()
        db._upsert_tv(_tv(1, number_of_episodes=24), now)
        db._upsert_tv(_tv(1), now)
        rows = _raw_rows(db, "SELECT _episode_count FROM tv WHERE id=1")
        assert rows[0][0] == 24


# ============================================================
# upsert 与 FTS 一致性
# ============================================================

class TestUpsert:
    def test_movie_fields_stored(self, db):
        db._upsert_movie(_movie(1, title="流浪地球"), time.time())
        items = [i for i in db.get_all("movie") if i["id"] == 1]
        assert items[0]["title"] == "流浪地球"
        assert items[0]["_media_type"] == "movie"

    def test_genre_ids_round_trip_as_list(self, db):
        db._upsert_movie(_movie(1, genre_ids=[28, 12]), time.time())
        assert db.get_all("movie")[0]["genre_ids"] == [28, 12]

    def test_origin_country_round_trip_as_list(self, db):
        db._upsert_tv(_tv(1, origin_country=["JP", "CN"]), time.time())
        assert db.get_all("tv")[0]["origin_country"] == ["JP", "CN"]

    def test_none_values_become_defaults(self, db):
        db._upsert_movie(_movie(1, overview=None, poster_path=None), time.time())
        item = db.get_all("movie")[0]
        assert item["overview"] == ""
        assert item["poster_path"] == ""

    def test_upsert_preserves_match_state(self, db):
        now = time.time()
        db._upsert_movie(_movie(1), now)
        db.override_match_state("movie", 1, "in_library", "用户确认")
        db._upsert_movie(_movie(1, title="改名"), now)
        state = db.get_match_state("movie", 1)
        assert state["match_status"] == "in_library"
        assert state["manual_override_at"] > 0

    def test_repeated_upsert_does_not_duplicate_fts_rows(self, db):
        now = time.time()
        for _ in range(3):
            db._upsert_movie(_movie(1, title="重复"), now)
        rows = _raw_rows(db, "SELECT COUNT(*) FROM movies_fts")
        assert rows[0][0] == 1

    def test_get_all_filters_by_media_type(self, db):
        now = time.time()
        db._upsert_movie(_movie(1), now)
        db._upsert_tv(_tv(2), now)
        assert [i["id"] for i in db.get_all("movie")] == [1]
        assert [i["id"] for i in db.get_all("tv")] == [2]
        assert len(db.get_all()) == 2


# ============================================================
# sync
# ============================================================

class TestSync:
    def test_syncs_movies_and_tv(self, db):
        client = _client(
            movie_pages=[([_movie(1)], False)],
            tv_pages=[([_tv(2)], False)],
        )
        result = db.sync(client, force=True)
        assert {i["id"] for i in result} == {1, 2}

    def test_follows_pagination(self, db):
        client = _client(
            movie_pages=[([_movie(1)], True), ([_movie(2)], False)],
        )
        db.sync(client, force=True)
        assert {i["id"] for i in db.get_all("movie")} == {1, 2}

    def test_stops_on_empty_page(self, db):
        client = _client(movie_pages=[([], True)])
        db.sync(client, force=True)
        assert db.get_all("movie") == []

    def test_malformed_api_response_is_tolerated(self, db):
        client = MagicMock()
        client.get_watchlist_movies.return_value = "not a tuple"
        client.get_watchlist_tv.return_value = ([], False)
        client.get_tv_details.return_value = None
        db.sync(client, force=True)  # 不抛异常
        assert db.get_all("movie") == []

    def test_items_without_id_are_skipped(self, db):
        client = _client(movie_pages=[([{"title": "无 id"}, _movie(1)], False)])
        db.sync(client, force=True)
        assert {i["id"] for i in db.get_all("movie")} == {1}

    def test_removed_items_are_deleted(self, db):
        first = _client(movie_pages=[([_movie(1), _movie(2)], False)])
        db.sync(first, force=True)
        second = _client(movie_pages=[([_movie(1)], False)])
        db.sync(second, force=True)
        assert {i["id"] for i in db.get_all("movie")} == {1}

    def test_empty_watchlist_clears_table(self, db):
        db.sync(_client(movie_pages=[([_movie(1)], False)]), force=True)
        db.sync(_client(movie_pages=[([], False)]), force=True)
        assert db.get_all("movie") == []

    def test_ttl_skips_sync(self, db):
        db.sync(_client(movie_pages=[([_movie(1)], False)]), force=True)
        client = _client(movie_pages=[([_movie(2)], False)])
        db.sync(client, force=False)
        client.get_watchlist_movies.assert_not_called()

    def test_force_bypasses_ttl(self, db):
        db.sync(_client(movie_pages=[([_movie(1)], False)]), force=True)
        client = _client(movie_pages=[([_movie(2)], False)])
        db.sync(client, force=True)
        client.get_watchlist_movies.assert_called()

    def test_invalid_last_sync_forces_sync(self, db):
        db._set_meta("last_sync", "garbage")
        client = _client(movie_pages=[([_movie(1)], False)])
        db.sync(client, force=False)
        client.get_watchlist_movies.assert_called()

    def test_movie_failure_does_not_block_tv(self, db):
        """电影与剧集各自独立事务：一方 API 失败，另一方数据仍保留。"""
        client = MagicMock()
        client.get_watchlist_movies.side_effect = RuntimeError("movie api down")
        client.get_watchlist_tv.return_value = ([_tv(2)], False)
        client.get_tv_details.return_value = None
        db.sync(client, force=True)
        assert {i["id"] for i in db.get_all("tv")} == {2}

    def test_movie_failure_does_not_wipe_existing_movies(self, db):
        db.sync(_client(movie_pages=[([_movie(1)], False)]), force=True)
        client = MagicMock()
        client.get_watchlist_movies.side_effect = RuntimeError("api down")
        client.get_watchlist_tv.return_value = ([], False)
        client.get_tv_details.return_value = None
        db.sync(client, force=True)
        assert {i["id"] for i in db.get_all("movie")} == {1}

    def test_untrusted_retrieval_does_not_wipe_existing_movies(self, db):
        """T5: 取回不可信（格式异常）时跳过删除，本地行数不变且日志含原因。

        旧实现：格式异常 break 后 movie_ids 为空 → 走 DELETE FROM movies 全清。
        """
        db.sync(_client(movie_pages=[([_movie(1)], False)]), force=True)
        client = MagicMock()
        client.get_watchlist_movies.return_value = "not a tuple"
        client.get_watchlist_tv.return_value = ([], False)
        client.get_tv_details.return_value = None
        db.sync(client, force=True)
        assert {i["id"] for i in db.get_all("movie")} == {1}
        ops = db.get_tmdb_logs(limit=50)
        assert any("取回不可信" in (log.get("msg") or "") for log in ops), \
            "应记录取回不可信的 ERROR 日志"

    def test_both_failures_do_not_update_last_sync(self, db):
        client = MagicMock()
        client.get_watchlist_movies.side_effect = RuntimeError("down")
        client.get_watchlist_tv.side_effect = RuntimeError("down")
        db.sync(client, force=True)
        assert db.get_cache_status()["cache_last_sync"] == 0

    def test_partial_success_updates_last_sync(self, db):
        client = MagicMock()
        client.get_watchlist_movies.side_effect = RuntimeError("down")
        client.get_watchlist_tv.return_value = ([_tv(2)], False)
        client.get_tv_details.return_value = None
        db.sync(client, force=True)
        assert db.get_cache_status()["cache_last_sync"] > 0

    def test_sync_writes_operation_logs(self, db):
        db.sync(_client(movie_pages=[([_movie(1)], False)]), force=True)
        ops = {log["op"] for log in db.get_tmdb_logs(limit=50)}
        assert "sync_summary" in ops

    def test_sync_preserves_manual_override(self, db):
        db.sync(_client(movie_pages=[([_movie(1)], False)]), force=True)
        db.override_match_state("movie", 1, "in_library", "用户确认")
        db.sync(_client(movie_pages=[([_movie(1)], False)]), force=True)
        assert db.get_match_state("movie", 1)["match_status"] == "in_library"


# ============================================================
# _populate_tv_details
# ============================================================

class TestPopulateTvDetails:
    def test_fills_season_and_episode_counts(self, db):
        db._upsert_tv(_tv(1), time.time())
        client = _client(details={1: {
            "id": 1, "number_of_seasons": 3, "number_of_episodes": 36,
            "last_episode_to_air": {"season_number": 3, "episode_number": 12},
        }})
        db._populate_tv_details(client)
        row = _raw_rows(
            db,
            "SELECT _season_count, _episode_count, _last_ep_season,"
            " _last_ep_episode FROM tv WHERE id=1")[0]
        assert row == (3, 36, 3, 12)

    def test_skips_rows_that_already_have_counts(self, db):
        db._upsert_tv(_tv(1, number_of_seasons=2), time.time())
        client = _client(details={1: {"id": 1, "number_of_seasons": 9}})
        db._populate_tv_details(client)
        client.get_tv_details.assert_not_called()
        assert db.get_season_count(1) == 2

    def test_no_rows_returns_without_api_call(self, db):
        client = _client()
        db._populate_tv_details(client)
        client.get_tv_details.assert_not_called()

    def test_api_returning_none_leaves_row_unchanged(self, db):
        db._upsert_tv(_tv(1), time.time())
        client = _client(details={})
        db._populate_tv_details(client)
        assert db.get_season_count(1) == 0

    def test_api_exception_does_not_propagate(self, db):
        db._upsert_tv(_tv(1), time.time())
        client = MagicMock()
        client.get_tv_details.side_effect = RuntimeError("api down")
        db._populate_tv_details(client)  # 不抛异常
        assert db.get_season_count(1) == 0

    def test_missing_last_episode_defaults_to_zero(self, db):
        db._upsert_tv(_tv(1), time.time())
        client = _client(details={1: {"id": 1, "number_of_seasons": 1,
                                     "number_of_episodes": 10}})
        db._populate_tv_details(client)
        row = _raw_rows(
            db, "SELECT _last_ep_season, _last_ep_episode FROM tv WHERE id=1")[0]
        assert row == (0, 0)

    def test_detail_without_id_is_skipped(self, db):
        db._upsert_tv(_tv(1), time.time())
        client = _client(details={1: {"number_of_seasons": 5}})
        db._populate_tv_details(client)
        assert db.get_season_count(1) == 0


# ============================================================
# TMDB 操作日志
# ============================================================

class TestOperationLog:
    def test_log_and_read_back(self, db):
        db.log_tmdb_operation("test_op", "info", "消息", "细节")
        logs = db.get_tmdb_logs()
        assert logs[0]["op"] == "test_op"
        assert logs[0]["level"] == "info"
        assert logs[0]["msg"] == "消息"
        assert logs[0]["detail"] == "细节"

    def test_detail_is_optional(self, db):
        db.log_tmdb_operation("op", "info", "无细节")
        assert db.get_tmdb_logs()[0]["detail"] is None

    def test_logs_sorted_newest_first(self, db):
        db.log_tmdb_operation("first", "info", "1")
        time.sleep(0.01)
        db.log_tmdb_operation("second", "info", "2")
        assert db.get_tmdb_logs()[0]["op"] == "second"

    def test_limit_is_respected(self, db):
        for i in range(5):
            db.log_tmdb_operation(f"op{i}", "info", str(i))
        assert len(db.get_tmdb_logs(limit=2)) == 2

    def test_entries_older_than_seven_days_are_purged(self, db):
        db.log_tmdb_operation("recent", "info", "新")
        conn = sqlite3.connect(db._db_path)
        conn.execute(
            "INSERT INTO tmdb_operation_log (ts, op, level, msg, detail)"
            " VALUES (?, 'ancient', 'info', '旧', NULL)",
            (time.time() - 8 * 86400,))
        conn.commit()
        conn.close()
        # M-14: 清理在写侧（log_tmdb_operation / _prune_tmdb_logs）完成，
        # 而非读侧 get_tmdb_logs。直接插入 DB 的旧记录需显式触发清理。
        db._prune_tmdb_logs()
        ops = {log["op"] for log in db.get_tmdb_logs(limit=100)}
        assert "ancient" not in ops
        assert "recent" in ops

    def test_row_cap_is_enforced(self, tmp_path):
        capped = TmdbWatchlistDb(tmp_path / "capped.db", tmdb_log_max_rows=3)
        for i in range(6):
            capped.log_tmdb_operation(f"op{i}", "info", str(i))
        capped.get_tmdb_logs(limit=100)  # M-14: 清理已在 log_tmdb_operation 写侧完成，此处仅读取
        remaining = _raw_rows(capped, "SELECT COUNT(*) FROM tmdb_operation_log")
        assert remaining[0][0] == 3


# ============================================================
# webui_config CRUD
# ============================================================

class TestWebuiConfig:
    def test_missing_key_returns_default(self, db):
        assert db.get_config("ui", "absent", "fallback") == "fallback"

    def test_set_then_get(self, db):
        db.set_config("ui", "theme", "dark")
        assert db.get_config("ui", "theme") == "dark"

    def test_set_is_upsert(self, db):
        db.set_config("ui", "theme", "dark")
        db.set_config("ui", "theme", "light")
        assert db.get_config("ui", "theme") == "light"

    def test_get_all_config_is_scoped(self, db):
        db.set_config("ui", "a", "1")
        db.set_config("openlist", "b", "2")
        assert db.get_all_config("ui") == {"a": "1"}

    def test_get_all_config_unknown_scope_is_empty(self, db):
        assert db.get_all_config("nope") == {}

    def test_delete_config(self, db):
        db.set_config("ui", "a", "1")
        db.delete_config("ui", "a")
        assert db.get_config("ui", "a", "gone") == "gone"

    def test_sensitive_value_is_encrypted_at_rest(self, db):
        db.set_config("openlist", "webdav_password", "plain-secret")
        raw = _raw_rows(
            db,
            "SELECT value FROM webui_config WHERE scope='openlist'"
            " AND key='webdav_password'")[0][0]
        assert raw != "plain-secret"
        assert db.get_config("openlist", "webdav_password") == "plain-secret"

    def test_sensitive_value_decrypted_in_get_all(self, db):
        db.set_config("tmdb", "access_token", "token-value")
        assert db.get_all_config("tmdb")["access_token"] == "token-value"

    def test_non_sensitive_value_stored_as_plain(self, db):
        db.set_config("ui", "theme", "dark")
        raw = _raw_rows(
            db, "SELECT value FROM webui_config WHERE scope='ui' AND key='theme'")
        assert raw[0][0] == "dark"

    def test_migrate_plaintext_to_encrypted_is_idempotent(self, db):
        conn = sqlite3.connect(db._db_path)
        conn.execute(
            "INSERT INTO webui_config (scope, key, value, updated_at)"
            " VALUES ('openlist', 'webdav_password', 'legacy-plain', 0)")
        conn.commit()
        conn.close()

        db.migrate_plaintext_to_encrypted()
        first = _raw_rows(
            db,
            "SELECT value FROM webui_config WHERE scope='openlist'"
            " AND key='webdav_password'")[0][0]
        assert first != "legacy-plain"

        db.migrate_plaintext_to_encrypted()
        second = _raw_rows(
            db,
            "SELECT value FROM webui_config WHERE scope='openlist'"
            " AND key='webdav_password'")[0][0]
        assert second == first
        assert db.get_config("openlist", "webdav_password") == "legacy-plain"

    def test_migrate_skips_empty_values(self, db):
        db.set_config("openlist", "webdav_totp_secret", "")
        db.migrate_plaintext_to_encrypted()
        assert db.get_config("openlist", "webdav_totp_secret") == ""


# ============================================================
# clear_match_override + get_match_states
# ============================================================

class TestClearMatchOverride:
    """清除人工覆盖后应恢复为 uncomputed 等待下次刷新。"""

    def test_clear_resets_to_uncomputed(self, db):
        db._upsert_movie(_movie(1), time.time())
        db.override_match_state("movie", 1, "matched", "用户确认")
        db.clear_match_override("movie", 1)
        state = db.get_match_state("movie", 1)
        assert state["manual_override_at"] == 0.0
        assert state["manual_override_by"] == ""
        assert state["match_status"] == "uncomputed"

    def test_clear_tv(self, db):
        db._upsert_tv(_tv(2), time.time())
        db.override_match_state("tv", 2, "fuzzy", "存疑")
        db.clear_match_override("tv", 2)
        state = db.get_match_state("tv", 2)
        assert state["manual_override_at"] == 0.0
        assert state["manual_override_by"] == ""
        assert state["match_status"] == "uncomputed"

    def test_clear_nonexistent_is_noop(self, db):
        # 不应抛异常
        db.clear_match_override("movie", 999)


class TestGetMatchStates:
    """批量读取 match states 供统计使用。"""

    def test_returns_dict_keyed_by_id(self, db):
        now = time.time()
        db._upsert_movie(_movie(1), now)
        db._upsert_movie(_movie(2), now)
        db.set_match_state("movie", 1, "matched", "reason1")
        db.set_match_state("movie", 2, "fuzzy", "reason2")
        result = db.get_match_states("movie", [1, 2])
        assert 1 in result
        assert 2 in result
        assert result[1]["match_status"] == "matched"
        assert result[2]["match_status"] == "fuzzy"

    def test_missing_ids_not_in_result(self, db):
        now = time.time()
        db._upsert_movie(_movie(1), now)
        result = db.get_match_states("movie", [1, 999])
        assert 1 in result
        assert 999 not in result

    def test_empty_ids_returns_empty(self, db):
        result = db.get_match_states("movie", [])
        assert result == {}

    def test_tv_table(self, db):
        now = time.time()
        db._upsert_tv(_tv(3), now)
        db.set_match_state("tv", 3, "unmatched", "no candidate")
        result = db.get_match_states("tv", [3])
        assert 3 in result
        assert result[3]["match_status"] == "unmatched"


# ============================================================
# 缓存状态与统计
# ============================================================

class TestCacheStatusAndStatistics:
    def test_fresh_db_is_stale(self, db):
        status = db.get_cache_status()
        assert status["cache_stale"] is True
        assert status["cache_item_count"] == 0

    def test_after_sync_cache_is_fresh(self, db):
        db.sync(_client(movie_pages=[([_movie(1)], False)]), force=True)
        status = db.get_cache_status()
        assert status["cache_stale"] is False
        assert status["cache_item_count"] == 1

    def test_expired_ttl_marks_stale(self, tmp_path):
        short = TmdbWatchlistDb(tmp_path / "ttl.db", ttl=0)
        short.sync(_client(movie_pages=[([_movie(1)], False)]), force=True)
        assert short.get_cache_status()["cache_stale"] is True

    def test_invalid_last_sync_marks_stale(self, db):
        db._set_meta("last_sync", "not-a-number")
        assert db.get_cache_status()["cache_stale"] is True

    def test_statistics_counts_uncomputed(self, db):
        now = time.time()
        db._upsert_movie(_movie(1), now)
        db._upsert_tv(_tv(2), now)
        db.set_match_state("movie", 1, "in_library", "auto")
        stats = db.get_match_statistics()
        assert stats["total"] == 2
        assert stats["uncomputed"] == 1

    def test_statistics_safe_default_on_error(self, tmp_path):
        broken = TmdbWatchlistDb(tmp_path / "broken.db")
        conn = sqlite3.connect(broken._db_path)
        conn.execute("DROP TABLE movies")
        conn.commit()
        conn.close()
        assert broken.get_match_statistics() == {"uncomputed": 0, "total": 0}
