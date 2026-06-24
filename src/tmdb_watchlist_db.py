"""
TMDB 待看列表独立 SQLite 数据库模块。

职责：
  - 管理 tmdb_watchlist.db 的建表、CRUD、全量同步
  - 不持有持久连接（每次操作新建 sqlite3.connect，WAL 模式）
  - 不依赖 webui.py / test_webui.py 内部状态
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tmdb_client import TmdbClient

# ============================================================
# Schema SQL
# ============================================================

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS movies (
    id              INTEGER PRIMARY KEY,
    title           TEXT DEFAULT '',
    original_title  TEXT DEFAULT '',
    overview        TEXT DEFAULT '',
    poster_path     TEXT DEFAULT '',
    backdrop_path   TEXT DEFAULT '',
    release_date    TEXT DEFAULT '',
    vote_average    REAL DEFAULT 0.0,
    vote_count      INTEGER DEFAULT 0,
    genre_ids       TEXT DEFAULT '[]',
    popularity      REAL DEFAULT 0.0,
    original_language TEXT DEFAULT '',
    video           INTEGER DEFAULT 0,
    adult           INTEGER DEFAULT 0,
    _media_type     TEXT DEFAULT 'movie',
    _synced_at      REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tv (
    id              INTEGER PRIMARY KEY,
    name            TEXT DEFAULT '',
    original_name   TEXT DEFAULT '',
    overview        TEXT DEFAULT '',
    poster_path     TEXT DEFAULT '',
    backdrop_path   TEXT DEFAULT '',
    first_air_date  TEXT DEFAULT '',
    vote_average    REAL DEFAULT 0.0,
    vote_count      INTEGER DEFAULT 0,
    genre_ids       TEXT DEFAULT '[]',
    popularity      REAL DEFAULT 0.0,
    origin_country  TEXT DEFAULT '[]',
    original_language TEXT DEFAULT '',
    _season_count   INTEGER DEFAULT 0,
    _media_type     TEXT DEFAULT 'tv',
    _synced_at      REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class TmdbWatchlistDb:
    """TMDB 待看列表 SQLite 数据库管理器。"""

    def __init__(self, db_path: str | Path, ttl: float = 604800) -> None:
        self._db_path = str(db_path)
        self._ttl = ttl
        self._init_schema()

    # ----------------------------------------------------------
    # 连接管理（每次调用新建连接，WAL 模式）
    # ----------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA_SQL)
        logging.debug("[TMDB-DB] 数据库初始化完成: %s", self._db_path)

    # ----------------------------------------------------------
    # 元数据
    # ----------------------------------------------------------

    def _get_meta(self, key: str, default: str = "") -> str:
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key=?", (key,)
                ).fetchone()
                return row[0] if row else default
        except Exception:
            return default

    def _set_meta(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()

    # ----------------------------------------------------------
    # 查询
    # ----------------------------------------------------------

    def get_all(self, media_type: str | None = None) -> list[dict]:
        """返回所有条目，可选按 _media_type 过滤。"""
        rows: list[sqlite3.Row] = []
        with self._conn() as conn:
            if not media_type or media_type == "movie":
                rows.extend(conn.execute("SELECT * FROM movies").fetchall())
            if not media_type or media_type == "tv":
                rows.extend(conn.execute("SELECT * FROM tv").fetchall())

        result: list[dict] = []
        for row in rows:
            d = dict(row)
            # genre_ids / origin_country 反序列化（存储为 JSON 字符串）
            for json_field in ("genre_ids", "origin_country"):
                if isinstance(d.get(json_field), str):
                    try:
                        d[json_field] = json.loads(d[json_field])
                    except (json.JSONDecodeError, TypeError):
                        d[json_field] = []
            result.append(d)
        return result

    # ----------------------------------------------------------
    # 全量同步
    # ----------------------------------------------------------

    def sync(self, tmdb_client: TmdbClient, force: bool = False) -> list[dict]:
        """全量同步：拉取所有分页 → upsert → 物理删除已移除条目。

        force=True 时忽略 TTL，强制重新拉取。
        movies 和 tv 各自独立事务。API 失败时各自回滚，
        成功一方数据保留。

        返回最新完整列表。
        """
        if not force:
            last_sync = self._get_meta("last_sync", "0")
            try:
                last_sync_time = float(last_sync) if last_sync else 0.0
                if last_sync_time > 0 and (time.time() - last_sync_time) < self._ttl:
                    logging.debug("[TMDB-DB] 缓存未过期,跳过同步")
                    return self.get_all()
            except (ValueError, TypeError):
                logging.warning("[TMDB-DB] 无效的 last_sync 值,强制同步")
                pass

        now = time.time()
        logging.info("[TMDB] 待看列表缓存过期，正在从 API 同步...")

        # ---- Movies ----
        movie_ids: set[int] = set()
        movie_sync_ok = False
        try:
            page = 1
            while True:
                result = tmdb_client.get_watchlist_movies(page)
                if not result or not isinstance(result, tuple) or len(result) != 2:
                    logging.warning("[TMDB] 电影 API 返回格式异常: %s", result)
                    break
                items, has_next = result
                if not items:
                    break
                for item in items:
                    if not isinstance(item, dict) or "id" not in item:
                        continue
                    movie_ids.add(item["id"])
                    self._upsert_movie(item, now)
                if not has_next:
                    break
                page += 1
                time.sleep(0.3)

            # 删除已移除的电影
            if movie_ids:
                with self._conn() as conn:
                    conn.execute(
                        "DELETE FROM movies WHERE id NOT IN ({})".format(
                            ",".join("?" * len(movie_ids))
                        ),
                        tuple(movie_ids),
                    )
                    conn.commit()
            else:
                with self._conn() as conn:
                    conn.execute("DELETE FROM movies")
                    conn.commit()

            logging.info("[TMDB] 电影同步完成 (%d 项)", len(movie_ids))
            movie_sync_ok = True
        except Exception as e:
            logging.warning("[TMDB] 电影同步失败: %s", e)

        # ---- TV ----
        tv_ids: set[int] = set()
        tv_sync_ok = False
        try:
            page = 1
            while True:
                result = tmdb_client.get_watchlist_tv(page)
                if not result or not isinstance(result, tuple) or len(result) != 2:
                    logging.warning("[TMDB] 剧集 API 返回格式异常: %s", result)
                    break
                items, has_next = result
                if not items:
                    break
                for item in items:
                    if not isinstance(item, dict) or "id" not in item:
                        continue
                    tv_ids.add(item["id"])
                    self._upsert_tv(item, now)
                if not has_next:
                    break
                page += 1
                time.sleep(0.3)

            if tv_ids:
                with self._conn() as conn:
                    conn.execute(
                        "DELETE FROM tv WHERE id NOT IN ({})".format(
                            ",".join("?" * len(tv_ids))
                        ),
                        tuple(tv_ids),
                    )
                    conn.commit()
            else:
                with self._conn() as conn:
                    conn.execute("DELETE FROM tv")
                    conn.commit()

            logging.info("[TMDB] 剧集同步完成 (%d 项)", len(tv_ids))
            tv_sync_ok = True
        except Exception as e:
            logging.warning("[TMDB] 剧集同步失败: %s", e)

        # ---- 批量补齐季数 ----
        if tv_sync_ok:
            try:
                self._populate_season_counts(tmdb_client)
            except Exception as e:
                logging.warning("[TMDB-DB] 批量获取季数失败: %s", e)

        # 只有至少一个成功才更新 last_sync
        if movie_sync_ok or tv_sync_ok:
            self._set_meta("last_sync", str(now))
        else:
            logging.error("[TMDB] 电影和剧集同步都失败,不更新 last_sync")

        all_items = self.get_all()
        logging.info(
            "[TMDB] 待看列表已同步: %d 项 (电影 %d, 剧集 %d)",
            len(all_items),
            sum(1 for i in all_items if i.get("_media_type") == "movie"),
            sum(1 for i in all_items if i.get("_media_type") == "tv"),
        )
        return all_items

    # ----------------------------------------------------------
    # 内部 upsert
    # ----------------------------------------------------------

    @staticmethod
    def _val(d: dict, key: str, default=""):
        v = d.get(key)
        return v if v is not None else default

    def _upsert_movie(self, item: dict, synced_at: float) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO movies
                (id, title, original_title, overview, poster_path, backdrop_path,
                 release_date, vote_average, vote_count, genre_ids, popularity,
                 original_language, video, adult, _media_type, _synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item["id"],
                    self._val(item, "title"),
                    self._val(item, "original_title"),
                    self._val(item, "overview"),
                    self._val(item, "poster_path"),
                    self._val(item, "backdrop_path"),
                    self._val(item, "release_date"),
                    float(self._val(item, "vote_average", 0)),
                    int(self._val(item, "vote_count", 0)),
                    json.dumps(self._val(item, "genre_ids", []), ensure_ascii=False),
                    float(self._val(item, "popularity", 0)),
                    self._val(item, "original_language"),
                    1 if item.get("video") else 0,
                    1 if item.get("adult") else 0,
                    "movie",
                    synced_at,
                ),
            )
            conn.commit()

    def _upsert_tv(self, item: dict, synced_at: float) -> None:
        with self._conn() as conn:
            # 先查已有 _season_count，保留旧值
            existing = conn.execute(
                "SELECT _season_count FROM tv WHERE id=?", (item["id"],)
            ).fetchone()
            season_count = existing[0] if existing else 0

            conn.execute(
                """INSERT OR REPLACE INTO tv
                (id, name, original_name, overview, poster_path, backdrop_path,
                 first_air_date, vote_average, vote_count, genre_ids, popularity,
                 origin_country, original_language, _season_count, _media_type, _synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item["id"],
                    self._val(item, "name"),
                    self._val(item, "original_name"),
                    self._val(item, "overview"),
                    self._val(item, "poster_path"),
                    self._val(item, "backdrop_path"),
                    self._val(item, "first_air_date"),
                    float(self._val(item, "vote_average", 0)),
                    int(self._val(item, "vote_count", 0)),
                    json.dumps(self._val(item, "genre_ids", []), ensure_ascii=False),
                    float(self._val(item, "popularity", 0)),
                    json.dumps(self._val(item, "origin_country", []), ensure_ascii=False),
                    self._val(item, "original_language"),
                    season_count,
                    "tv",
                    synced_at,
                ),
            )
            conn.commit()

    # ----------------------------------------------------------
    # 批量获取季数（并发 + 限速）
    # ----------------------------------------------------------

    def _populate_season_counts(self, tmdb_client: TmdbClient) -> None:
        """批量补齐 TV 表的 _season_count。

        同步后调用，查询所有 _season_count = 0 的剧集，
        用线程池并发调用 TMDB API，每批 10 个请求后等待 2s 避免限速。
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id FROM tv WHERE _season_count IS NULL OR _season_count <= 0"
            ).fetchall()
        ids_to_fetch = [r[0] for r in rows]
        if not ids_to_fetch:
            return

        logging.info(
            "[TMDB-DB] 正在批量获取 %d 部剧集的季数...",
            len(ids_to_fetch))

        BATCH_SIZE = 10
        BATCH_SLEEP = 2.0  # 每批间隔秒数

        def _fetch(tid: int) -> tuple[int, int] | None:
            try:
                count = tmdb_client.get_tv_seasons_info(tid)
                return (tid, count) if count > 0 else None
            except Exception:
                return None

        fetched = 0
        for start in range(0, len(ids_to_fetch), BATCH_SIZE):
            batch = ids_to_fetch[start:start + BATCH_SIZE]
            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = {pool.submit(_fetch, tid): tid for tid in batch}
                for future in as_completed(futures):
                    result = future.result()
                    if result:
                        tid, count = result
                        try:
                            with self._conn() as conn:
                                conn.execute(
                                    "UPDATE tv SET _season_count=? WHERE id=?",
                                    (count, tid))
                                conn.commit()
                            fetched += 1
                        except Exception:
                            pass
            if start + BATCH_SIZE < len(ids_to_fetch):
                time.sleep(BATCH_SLEEP)

        if fetched:
            logging.info(
                "[TMDB-DB] 季数批量获取完成: %d/%d 部",
                fetched,
                len(ids_to_fetch))
