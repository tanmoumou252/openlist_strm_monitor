from __future__ import annotations

import time
from pathlib import Path

from watchlist_match import score_watchlist_item as _score_watchlist_item, refresh_watchlist_match_state as _refresh_watchlist_match_state
from tmdb_watchlist_db import TmdbWatchlistDb


# ============================================================
# Stubs
# ============================================================

class _StubDb:
    def __init__(self, rows):
        self._rows = rows

    class _Conn:
        def __init__(self, rows):
            self._rows = rows

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, _sql):
            class _Cur:
                def __init__(self, rows):
                    self._rows = rows

                def fetchall(self):
                    return self._rows
            return _Cur(self._rows)

    def read_connection(self):
        return self._Conn(self._rows)


class _StubWebUI:
    def __init__(self, watchlist_db, db):
        self._watchlist_db = watchlist_db
        self._db = db


class _StubStateDb:
    def __init__(self, items):
        self._items = items
        self.calls = []

    def get_all(self):
        return list(self._items)

    def replace_match_state(self, media_type, states):
        self.calls.append((media_type, list(states)))


def _movie(name, **kw):
    d = {"title": name, "original_title": name, "_media_type": "movie"}
    d.update(kw)
    return d


def _tv(name, **kw):
    d = {"name": name, "original_name": name, "_media_type": "tv"}
    d.update(kw)
    return d


def _cand(name, season="", season_num=0, episode_hint=False, episode_count=0):
    return {"name": name, "season": season,
            "season_num": season_num, "episode_hint": episode_hint,
            "episode_count": episode_count}


# ============================================================
# DB persistence
# ============================================================

def test_watchlist_db_persists_match_state(tmp_path: Path):
    db = TmdbWatchlistDb(tmp_path / "wl.db")
    db._upsert_movie({"id": 1, "title": "A", "original_title": "A"}, 0.0)
    db._upsert_tv({"id": 2, "name": "B", "original_name": "B"}, 0.0)
    db.set_match_state("movie", 1, "matched", "movie_exact:A")
    db.set_match_state("tv", 2, "fuzzy", "tv_name_conflict:B,C")
    assert db.get_match_state("movie", 1)["match_status"] == "matched"
    assert db.get_match_state(
        "tv", 2)["match_reason"] == "tv_name_conflict:B,C"


# ============================================================
# 电影精确命中
# ============================================================

def test_movie_exact_hit():
    item = _movie("Test Movie")
    cands = [_cand("Test Movie")]
    status, reason = _score_watchlist_item(item, cands, "movie")
    assert status == "matched"
    assert "movie_exact" in reason


# ============================================================
# 电影同名冲突 → fuzzy
# ============================================================

def test_movie_name_conflict():
    item = _movie("Shared Title")
    cands = [_cand("Shared Title A"), _cand("Shared Title B")]
    status, reason = _score_watchlist_item(item, cands, "movie")
    assert status == "fuzzy"
    assert "movie_ambiguous" in reason or "movie_name_conflict" in reason


# ============================================================
# 电影子串命中 → matched（TMDB 名是 B 区名的子串）
# ============================================================

def test_movie_loose_substring_match():
    item = _movie("Test")
    cands = [_cand("Test Movie")]
    status, reason = _score_watchlist_item(item, cands, "movie")
    assert status == "matched"
    assert "movie_loose" in reason


# ============================================================
# 电影无候选 → unmatched
# ============================================================

def test_movie_no_candidate():
    item = _movie("Nonexistent")
    cands = [_cand("Other Movie")]
    status, reason = _score_watchlist_item(item, cands, "movie")
    assert status == "unmatched"
    assert "no_movie_candidate" in reason


# ============================================================
# 番剧结构命中（路径有季+集文件）
# ============================================================

def test_tv_structure_hit():
    item = _tv("Test Show", number_of_seasons=2, number_of_episodes=24,
               last_episode_to_air={"season_number": 2, "episode_number": 12})
    cands = [_cand("Test Show", season="第1季", season_num=1, episode_hint=True)]
    status, reason = _score_watchlist_item(item, cands, "tv")
    assert status == "matched"
    assert "tv_exact" in reason


# ============================================================
# 番剧结构冲突（路径第5季但TMDB只有2季）→ fuzzy
# ============================================================

def test_tv_season_mismatch():
    item = _tv("Test Show", number_of_seasons=2)
    cands = [_cand("Test Show", season="第5季", season_num=5)]
    status, reason = _score_watchlist_item(item, cands, "tv")
    assert status == "fuzzy"
    assert "tv_season_mismatch" in reason


# ============================================================
# 残局番剧：路径无季/集，依赖 last_episode_to_air
# last_episode_to_air 逻辑已移植到 score_watchlist_item，仅在有结构证据时触发
# ============================================================

def test_tv_last_episode_hit():
    """T4: last_episode_to_air 逻辑已移植，仅在有结构证据时触发；
    名字精确匹配 + 无季/集结构 → matched (no_structure)"""
    item = _tv("Endgame Show", number_of_seasons=3,
               last_episode_to_air={"season_number": 3, "episode_number": 10})
    cands = [_cand("Endgame Show")]
    status, reason = _score_watchlist_item(item, cands, "tv")
    assert status == "matched"
    assert "no_structure" in reason


# ============================================================
# 残局番剧 last_season > total_seasons → fuzzy
# ============================================================

def test_tv_last_season_mismatch():
    """T4: last_episode_to_air 季数不匹配逻辑已移植（仅在有结构证据时触发）；
    无结构证据时名字精确 → matched (no_structure)"""
    item = _tv("Mismatch Show", number_of_seasons=1,
               last_episode_to_air={"season_number": 3, "episode_number": 1})
    cands = [_cand("Mismatch Show")]
    status, reason = _score_watchlist_item(item, cands, "tv")
    assert status == "matched"
    assert "no_structure" in reason


# ============================================================
# 番剧完全无结构证据 → matched (精确匹配名命中，无季/集结构)
# ============================================================

def test_tv_missing_structure():
    """T4: 名字精确 + 无季/集结构 → matched (no_structure)"""
    item = _tv("Bare Show")
    cands = [_cand("Bare Show")]
    status, reason = _score_watchlist_item(item, cands, "tv")
    assert status == "matched"
    assert "no_structure" in reason


# ============================================================
# 番剧同名冲突 → fuzzy
# ============================================================

def test_tv_name_conflict():
    item = _tv("Same Name")
    cands = [_cand("Same Name A", season_num=1, episode_hint=True),
             _cand("Same Name B", season_num=1, episode_hint=True)]
    status, reason = _score_watchlist_item(item, cands, "tv")
    assert status == "fuzzy"
    assert "tv_ambiguous" in reason or "tv_name_conflict" in reason


# ============================================================
# Schema 迁移 + 批量回写
# ============================================================

def test_refresh_writes_both_types():
    wdb = _StubStateDb([
        _movie("Movie A", id=1),
        _tv("Show B", id=2, number_of_seasons=1, number_of_episodes=6,
            last_episode_to_air={"season_number": 1, "episode_number": 6}),
    ])
    bdb = _StubDb([
        ("/root/电影/Movie A/test.strm",
         "/root/电影/Movie A/test.strm", "", "", "", "valid", 0.0),
        ("/root/番剧/Show B/第1季/E01.strm",
         "/root/番剧/Show B/第1季/E01.strm", "", "", "", "valid", 0.0),
    ])
    counts = _refresh_watchlist_match_state(_StubWebUI(wdb, bdb))
    assert counts["total"] == 2
    assert counts["matched"] >= 1
    assert {c[0] for c in wdb.calls} == {"movie", "tv"}


def test_override_match_state_sets_manual_tracking(tmp_path: Path):
    db = TmdbWatchlistDb(tmp_path / "wl.db")
    db._upsert_movie({"id": 1, "title": "A", "original_title": "A"}, 0.0)
    db.override_match_state("movie", 1, "matched", "manual_override", "tester")
    state = db.get_match_state("movie", 1)
    assert state["match_status"] == "matched"
    assert state["match_reason"] == "manual_override"
    assert state["manual_override_by"] == "tester"
    assert state["manual_override_at"] > 0


def test_batch_update_skips_manually_overridden(tmp_path: Path):
    """replace_match_state 跳过已被手动覆盖的条目。

    覆盖 tmdb_watchlist_db.py:321-327：UPDATE 语句带 manual_override_at=0 条件，
    仅更新尚未被手动覆盖的条目；已被用户手动设置状态的条目跳过。
    """
    db = TmdbWatchlistDb(tmp_path / "batch.db")

    # 插入 movie id=1，初始状态 uncomputed
    db._upsert_movie({"id": 1, "title": "Movie1", "original_title": "Movie1"}, 0.0)
    # 手动覆盖 id=1
    db.override_match_state("movie", 1, "matched", "manual_override", "tester")

    # 插入 movie id=2，无手动覆盖
    db._upsert_movie({"id": 2, "title": "Movie2", "original_title": "Movie2"}, 0.0)

    # 批量更新：尝试将 id=1 和 id=2 都更新为 "out"
    now = time.time()
    db.replace_match_state("movie", [
        (1, "out", "auto_reason", now, 0, ""),
        (2, "out", "auto_reason", now, 0, ""),
    ])

    # id=1 应保持手动覆盖的 "matched"（未被 replace_match_state 覆盖）
    state1 = db.get_match_state("movie", 1)
    assert state1["match_status"] == "matched", \
        f"手动覆盖的 id=1 应保持 matched，实际: {state1['match_status']}"
    assert state1["match_reason"] == "manual_override", \
        f"手动覆盖的 id=1 应保持 manual_override，实际: {state1['match_reason']}"

    # id=2 应被更新为 "out"
    state2 = db.get_match_state("movie", 2)
    assert state2["match_status"] == "out", \
        f"未手动覆盖的 id=2 应被更新为 out，实际: {state2['match_status']}"


# ============================================================
# 旧库兼容迁移
# ============================================================

def test_old_schema_migrates_match_columns(tmp_path: Path):
    import sqlite3

    db_path = tmp_path / "compat.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE movies (
                id INTEGER PRIMARY KEY,
                title TEXT DEFAULT '',
                _media_type TEXT DEFAULT 'movie',
                _synced_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE tv (
                id INTEGER PRIMARY KEY,
                name TEXT DEFAULT '',
                _media_type TEXT DEFAULT 'tv',
                _synced_at REAL NOT NULL DEFAULT 0
            );
            """
        )
        conn.execute(
            "INSERT INTO movies (id, title) VALUES (?, ?)",
            (1, "Legacy Movie"),
        )
        conn.execute(
            "INSERT INTO tv (id, name) VALUES (?, ?)",
            (2, "Legacy Show"),
        )
        conn.commit()

    db = TmdbWatchlistDb(db_path)
    items = db.get_all()
    assert {i["id"] for i in items} == {1, 2}

    assert db.get_match_state("movie", 1)["match_status"] == "uncomputed"
    assert db.get_match_state("tv", 2)["match_status"] == "uncomputed"

    db.set_match_state("movie", 1, "matched", "movie_exact:Legacy Movie")
    db.set_match_state("tv", 2, "fuzzy", "tv_name_conflict:Legacy Show,Other")

    assert db.get_match_state("movie", 1)["match_status"] == "matched"
    assert db.get_match_state(
        "movie", 1)["match_reason"] == "movie_exact:Legacy Movie"
    assert db.get_match_state("tv", 2)["match_status"] == "fuzzy"
    assert db.get_match_state(
        "tv", 2)["match_reason"] == "tv_name_conflict:Legacy Show,Other"


# ============================================================
# _episode_count 存储
# ============================================================

def test_upsert_tv_stores_episode_count(tmp_path: Path):
    """_upsert_tv 从 API 响应提取 number_of_episodes"""
    db = TmdbWatchlistDb(tmp_path / "wl.db")
    db._upsert_tv({"id": 1, "name": "Test", "original_name": "Test",
                   "number_of_episodes": 87}, 0.0)
    items = db.get_all()
    tv_items = [i for i in items if i.get("_media_type") == "tv"]
    assert len(tv_items) == 1
    assert tv_items[0]["_episode_count"] == 87


def test_upsert_tv_preserves_episode_count(tmp_path: Path):
    """_upsert_tv 保留已有的 _episode_count 值"""
    db = TmdbWatchlistDb(tmp_path / "wl.db")
    # 第一次 upsert 写入 episode_count
    db._upsert_tv({"id": 1, "name": "Test", "original_name": "Test",
                   "number_of_episodes": 87}, 0.0)
    # 第二次 upsert 不带 number_of_episodes，应保留旧值
    db._upsert_tv({"id": 1, "name": "Test", "original_name": "Test"}, 0.0)
    items = db.get_all()
    tv_items = [i for i in items if i.get("_media_type") == "tv"]
    assert tv_items[0]["_episode_count"] == 87
