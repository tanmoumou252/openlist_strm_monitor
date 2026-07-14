"""
TMDB 待看列表独立 SQLite 数据库模块。

职责：
  - 管理 tmdb_watchlist.db 的建表、CRUD、全量同步
  - 不持有持久连接（每次操作新建 sqlite3.connect，WAL 模式）
- 不依赖 webui.py / standalone_webui.py 内部状态    
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

import secret_manager

if TYPE_CHECKING:
    from tmdb_client import TmdbClient

# ============================================================
# 敏感配置键（需要加密存储）
# ============================================================

_SENSITIVE_KEYS: frozenset[tuple[str, str]] = frozenset({
    ("openlist", "webdav_password"),
    ("openlist", "webdav_totp_secret"),
    ("tmdb", "access_token"),
    ("tmdb", "api_key"),
})

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
    _synced_at      REAL NOT NULL DEFAULT 0,
    match_status    TEXT DEFAULT 'uncomputed',
    match_reason    TEXT DEFAULT '',
    match_updated_at REAL DEFAULT 0,
    manual_override_at REAL DEFAULT 0,
    manual_override_by TEXT DEFAULT ''
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
    _episode_count         INTEGER DEFAULT 0,
    _last_ep_season        INTEGER DEFAULT 0,
    _last_ep_episode       INTEGER DEFAULT 0,
    _media_type     TEXT DEFAULT 'tv',
    _synced_at      REAL NOT NULL DEFAULT 0,
    match_status    TEXT DEFAULT 'uncomputed',
    match_reason    TEXT DEFAULT '',
    match_updated_at REAL DEFAULT 0,
    manual_override_at REAL DEFAULT 0,
    manual_override_by TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS webui_config (
    scope      TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, key)
);
"""


class TmdbWatchlistDb:
    """TMDB 待看列表 SQLite 数据库管理器。"""

    def __init__(self, db_path: str | Path, ttl: float = 604800, tmdb_log_max_rows: int = 1000) -> None:
        self._db_path = str(db_path)
        self._ttl = ttl
        self._tmdb_log_max_rows = tmdb_log_max_rows
        self._init_schema()

    # ----------------------------------------------------------
    # 连接管理（每次调用新建连接，WAL 模式）
    # ----------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        
        # 尝试加载 simple 分词器（与 database.py 保持一致）
        try:
            conn.enable_load_extension(True)
            simple_dll = Path(__file__).parent.parent / "simple.dll"
            if simple_dll.exists():
                conn.load_extension(str(simple_dll))
        except Exception:
            pass  # 静默失败，使用 unicode61 降级
        
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA_SQL)
            self._ensure_column(
                conn,
                "movies",
                "match_status",
                "TEXT DEFAULT 'uncomputed'")
            self._ensure_column(
                conn,
                "movies",
                "match_reason",
                "TEXT DEFAULT ''")
            self._ensure_column(
                conn,
                "movies",
                "match_updated_at",
                "REAL DEFAULT 0")
            self._ensure_column(
                conn,
                "movies",
                "manual_override_at",
                "REAL DEFAULT 0")
            self._ensure_column(
                conn,
                "movies",
                "manual_override_by",
                "TEXT DEFAULT ''")
            self._ensure_column(conn, "tv", "match_status",
                                "TEXT DEFAULT 'uncomputed'")
            self._ensure_column(conn, "tv", "match_reason", "TEXT DEFAULT ''")
            self._ensure_column(
                conn,
                "tv",
                "match_updated_at",
                "REAL DEFAULT 0")
            self._ensure_column(
                conn,
                "tv",
                "manual_override_at",
                "REAL DEFAULT 0")
            self._ensure_column(
                conn,
                "tv",
                "manual_override_by",
                "TEXT DEFAULT ''")
            self._ensure_column(
                conn,
                "tv",
                "_episode_count",
                "INTEGER DEFAULT 0")
            self._ensure_column(
                conn,
                "tv",
                "_last_ep_season",
                "INTEGER DEFAULT 0")
            self._ensure_column(
                conn,
                "tv",
                "_last_ep_episode",
                "INTEGER DEFAULT 0")
            # TMDB 操作日志表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tmdb_operation_log (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts       REAL    NOT NULL,
                    op       TEXT    NOT NULL,
                    level    TEXT    NOT NULL DEFAULT 'info',
                    msg      TEXT    NOT NULL,
                    detail   TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_tmdb_log_ts ON tmdb_operation_log(ts DESC)
            """)
            
            # FTS5 全文搜索表（用于 TMDB 待看列表搜索）
            # 尝试使用 simple 分词器，失败则降级到 unicode61
            tokenizer = 'unicode61'
            try:
                conn.enable_load_extension(True)
                simple_dll = Path(__file__).parent.parent / "simple.dll"
                if simple_dll.exists():
                    conn.load_extension(str(simple_dll))
                    tokenizer = 'simple'
            except Exception:
                pass
            
            conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS tmdb_watchlist_fts USING fts5(
                    title,
                    original_title,
                    overview,
                    tokenize='{tokenizer}'
                )
            """)
            
            conn.commit()
        logging.debug("[TMDB-DB] 数据库初始化完成: %s", self._db_path)

    # ----------------------------------------------------------
    # 元数据
    # ----------------------------------------------------------

    def get_cache_status(self) -> dict:
        """返回缓存状态（不触发同步，不加载全量数据）。"""
        last_sync = self._get_meta("last_sync", "0")
        try:
            last_sync_time = float(last_sync) if last_sync else 0.0
        except (ValueError, TypeError):
            last_sync_time = 0.0
        stale = not (
            last_sync_time > 0 and (
                time.time() -
                last_sync_time) < self._ttl)
        with self._conn() as conn:
            mc = conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
            tc = conn.execute("SELECT COUNT(*) FROM tv").fetchone()[0]
        return {
            "cache_stale": stale,
            "cache_last_sync": last_sync_time,
            "cache_item_count": mc + tc,
        }

    def get_match_statistics(self) -> dict:
        """获取匹配统计信息（uncomputed 与 total）。

        将原 routes.py 中直接访问 `_conn()` 的查询封装为公共方法，
        避免外部绕过数据库访问层，并在异常时返回安全默认值。
        """
        result = {"uncomputed": 0, "total": 0}
        try:
            with self._conn() as conn:
                uncomputed = conn.execute(
                    "SELECT COUNT(*) FROM ("
                    "  SELECT id FROM movies WHERE match_status='uncomputed'"
                    "  UNION ALL"
                    "  SELECT id FROM tv WHERE match_status='uncomputed'"
                    ")"
                ).fetchone()[0]
                total = conn.execute(
                    "SELECT (SELECT COUNT(*) FROM movies)"
                    " + (SELECT COUNT(*) FROM tv)"
                ).fetchone()[0]
                result["uncomputed"] = uncomputed or 0
                result["total"] = total or 0
        except Exception as e:
            logging.warning("[TMDB] 获取匹配统计失败: %s", e)
        return result


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

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        column_name: str,
        column_def: str,
    ) -> None:
        cur = conn.execute(f"PRAGMA table_info({table_name})")
        columns = {row[1] for row in cur.fetchall()}
        if column_name not in columns:
            conn.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")
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

    def replace_match_state(
        self,
        media_type: str,
        states: Iterable[tuple[int, str, str, float, float, str]],
    ) -> None:
        """批量写入收录状态（自动刷新用）。

        对每个条目先尝试 UPDATE；若行不存在（sync 尚未写入该 ID），
        则回退 INSERT（仅写入 match 列，其余列使用 DEFAULT）。

        关键：保留手动覆盖。UPDATE 语句带 ``manual_override_at=0`` 条件，
        仅更新尚未被手动覆盖的条目；已被用户手动设置状态（manual_override_at>0）
        的条目跳过，避免自动刷新覆盖用户决策。
        """
        table = "movies" if media_type == "movie" else "tv"
        # 仅更新未被手动覆盖的条目（manual_override_at=0 表示无手动覆盖）
        update_sql = f"""UPDATE {table}
            SET match_status=?, match_reason=?, match_updated_at=?,
                manual_override_at=?, manual_override_by=?
            WHERE id=? AND manual_override_at=0"""
        insert_sql = f"""INSERT OR IGNORE INTO {table}
            (id, match_status, match_reason, match_updated_at,
             manual_override_at, manual_override_by)
            VALUES (?, ?, ?, ?, ?, ?)"""
        with self._conn() as conn:
            for item_id, status, reason, updated_at, override_at, override_by in states:
                cursor = conn.execute(
                    update_sql,
                    (status, reason, updated_at, override_at, override_by, item_id),
                )
                if cursor.rowcount == 0:
                    # 行不存在 → INSERT 新行；行存在但已被手动覆盖 → INSERT OR IGNORE 跳过
                    cursor2 = conn.execute(
                        insert_sql,
                        (item_id, status, reason, updated_at,
                         override_at, override_by),
                    )
                    if cursor2.rowcount == 0:
                        logging.debug(
                            "[TMDB] 跳过手动覆盖行: %s/%d (manual_override_at>0)",
                            media_type, item_id
                        )
            conn.commit()

    def set_match_state(
        self,
        media_type: str,
        item_id: int,
        status: str,
        reason: str,
        updated_at: float | None = None,
        manual_override_at: float | None = None,
        manual_override_by: str = "",
    ) -> None:
        table = "movies" if media_type == "movie" else "tv"
        now = time.time() if updated_at is None else updated_at
        override_at = manual_override_at or 0.0
        with self._conn() as conn:
            conn.execute(
                f"""UPDATE {table}
                SET match_status=?, match_reason=?, match_updated_at=?,
                    manual_override_at=?, manual_override_by=?
                WHERE id=?""",
                (status, reason, now, override_at, manual_override_by, item_id),
            )
            conn.commit()

    def override_match_state(
        self,
        media_type: str,
        item_id: int,
        status: str,
        reason: str,
        manual_override_by: str = "manual",
    ) -> None:
        now = time.time()
        self.set_match_state(
            media_type,
            item_id,
            status,
            reason,
            updated_at=now,
            manual_override_at=now,
            manual_override_by=manual_override_by,
        )

    def get_match_state(self, media_type: str, item_id: int) -> dict | None:
        table = "movies" if media_type == "movie" else "tv"
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT match_status, match_reason, match_updated_at, manual_override_at, manual_override_by FROM {table} WHERE id=?",
                (item_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_season_count(self, tmdb_id: int) -> int:
        """获取指定 TV 剧集的季数缓存。返回 int，0 表示未找到或无效。"""
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT _season_count FROM tv WHERE id=?", (tmdb_id,)
                ).fetchone()
                return row[0] if row and row[0] > 0 else 0
        except Exception:
            return 0

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
                if last_sync_time > 0 and (
                        time.time() - last_sync_time) < self._ttl:
                    logging.debug("[TMDB-DB] 缓存未过期,跳过同步")
                    return self.get_all()
            except (ValueError, TypeError):
                logging.warning("[TMDB-DB] 无效的 last_sync 值,强制同步")
                pass

        now = time.time()
        logging.info("[TMDB] 待看列表缓存过期，正在从 API 同步...")
        self.log_tmdb_operation("sync_cache_expired", "info", "待看列表缓存过期，正在从 API 同步...")

        # ---- Movies ----
        movie_ids: set[int] = set()
        movie_sync_ok = False
        try:
            page = 1
            while True:
                result = tmdb_client.get_watchlist_movies(page)
                if not result or not isinstance(
                        result, tuple) or len(result) != 2:
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

            # 删除已移除的电影（使用临时表避免 SQLite 参数上限 P2-3）
            if movie_ids:
                with self._conn() as conn:
                    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _keep_movie_ids (id INTEGER)")
                    conn.execute("DELETE FROM _keep_movie_ids")
                    conn.executemany(
                        "INSERT INTO _keep_movie_ids VALUES (?)",
                        [(i,) for i in movie_ids],
                    )
                    conn.execute(
                        "DELETE FROM movies WHERE id NOT IN (SELECT id FROM _keep_movie_ids)")
                    # 同步清理 FTS 表中的孤儿记录
                    conn.execute(
                        "DELETE FROM tmdb_watchlist_fts WHERE rowid NOT IN (SELECT rowid FROM movies)")
                    conn.commit()
            else:
                with self._conn() as conn:
                    conn.execute("DELETE FROM movies")
                    # 同步清理 FTS 表
                    conn.execute("DELETE FROM tmdb_watchlist_fts")
                    conn.commit()

            logging.info("[TMDB] 电影同步完成 (%d 项)", len(movie_ids))
            self.log_tmdb_operation("sync_movies_done", "info", f"电影同步完成 ({len(movie_ids)} 项)")
            movie_sync_ok = True
        except Exception as e:
            logging.warning("[TMDB] 电影同步失败: %s", e)
            self.log_tmdb_operation("sync_movies_error", "error", f"电影同步失败: {e}")

        # ---- TV ----
        tv_ids: set[int] = set()
        tv_sync_ok = False
        try:
            page = 1
            while True:
                result = tmdb_client.get_watchlist_tv(page)
                if not result or not isinstance(
                        result, tuple) or len(result) != 2:
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
                    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _keep_tv_ids (id INTEGER)")
                    conn.execute("DELETE FROM _keep_tv_ids")
                    conn.executemany(
                        "INSERT INTO _keep_tv_ids VALUES (?)",
                        [(i,) for i in tv_ids],
                    )
                    conn.execute(
                        "DELETE FROM tv WHERE id NOT IN (SELECT id FROM _keep_tv_ids)")
                    # 同步清理 FTS 表中的孤儿记录
                    conn.execute(
                        "DELETE FROM tmdb_watchlist_fts WHERE rowid NOT IN (SELECT rowid FROM tv)")
                    conn.commit()
            else:
                with self._conn() as conn:
                    conn.execute("DELETE FROM tv")
                    # 同步清理 FTS 表
                    conn.execute("DELETE FROM tmdb_watchlist_fts")
                    conn.commit()

            logging.info("[TMDB] 剧集同步完成 (%d 项)", len(tv_ids))
            self.log_tmdb_operation("sync_tv_done", "info", f"剧集同步完成 ({len(tv_ids)} 项)")
            tv_sync_ok = True
        except Exception as e:
            logging.warning("[TMDB] 剧集同步失败: %s", e)
            self.log_tmdb_operation("sync_tv_error", "error", f"剧集同步失败: {e}")

        # ---- 批量补齐季数 ----
        if tv_sync_ok:
            try:
                self.log_tmdb_operation("sync_tv_details_start", "info", f"正在批量获取 {len(tv_ids)} 部剧集的详情...")
                self._populate_tv_details(tmdb_client)
                self.log_tmdb_operation("sync_tv_details_done", "info", f"剧集详情批量获取完成: {len(tv_ids)} 部")
            except Exception as e:
                logging.warning("[TMDB-DB] 批量获取剧集详情失败: %s", e)
                self.log_tmdb_operation("sync_tv_details_error", "error", f"批量获取剧集详情失败: {e}")

        # 只有至少一个成功才更新 last_sync
        if movie_sync_ok or tv_sync_ok:
            self._set_meta("last_sync", str(now))
        else:
            logging.error("[TMDB] 电影和剧集同步都失败,不更新 last_sync")

        all_items = self.get_all()
        movie_count = sum(1 for i in all_items if i.get("_media_type") == "movie")
        tv_count = sum(1 for i in all_items if i.get("_media_type") == "tv")
        logging.info(
            "[TMDB] 待看列表已同步: %d 项 (电影 %d, 剧集 %d)",
            len(all_items),
            movie_count,
            tv_count,
        )
        self.log_tmdb_operation("sync_summary", "success", f"待看列表已同步: {len(all_items)} 项 (电影 {movie_count}, 剧集 {tv_count})")
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
            existing = conn.execute(
                "SELECT match_status, match_reason, match_updated_at, manual_override_at, manual_override_by FROM movies WHERE id=?",
                (item["id"],),
            ).fetchone()
            match_status = existing[0] if existing else "uncomputed"
            match_reason = existing[1] if existing else ""
            match_updated_at = existing[2] if existing else 0.0
            manual_override_at = existing[3] if existing else 0.0
            manual_override_by = existing[4] if existing else ""
            
            # 删除旧的 FTS 记录
            conn.execute(
                "DELETE FROM tmdb_watchlist_fts WHERE rowid = (SELECT rowid FROM movies WHERE id=?)",
                (item["id"],),
            )
            
            conn.execute(
                """INSERT OR REPLACE INTO movies
                (id, title, original_title, overview, poster_path, backdrop_path,
                 release_date, vote_average, vote_count, genre_ids, popularity,
                 original_language, video, adult, _media_type, _synced_at,
                 match_status, match_reason, match_updated_at, manual_override_at, manual_override_by)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    json.dumps(
                        self._val(
                            item,
                            "genre_ids",
                            []),
                        ensure_ascii=False),
                    float(self._val(item, "popularity", 0)),
                    self._val(item, "original_language"),
                    1 if item.get("video") else 0,
                    1 if item.get("adult") else 0,
                    "movie",
                    synced_at,
                    match_status,
                    match_reason,
                    match_updated_at,
                    manual_override_at,
                    manual_override_by,
                ),
            )
            
            # 插入新的 FTS 记录
            conn.execute(
                """INSERT INTO tmdb_watchlist_fts(rowid, title, original_title, overview)
                VALUES((SELECT rowid FROM movies WHERE id=?), ?, ?, ?)""",
                (
                    item["id"],
                    self._val(item, "title"),
                    self._val(item, "original_title"),
                    self._val(item, "overview"),
                ),
            )
            
            conn.commit()

    def _upsert_tv(self, item: dict, synced_at: float) -> None:
        with self._conn() as conn:
            # 先查已有 _season_count, _episode_count 和匹配状态，保留旧值
            existing = conn.execute(
                "SELECT _season_count, _episode_count, match_status, match_reason, match_updated_at, manual_override_at, manual_override_by FROM tv WHERE id=?",
                (item["id"],),
            ).fetchone()
            season_count = existing[0] if existing else 0
            episode_count = existing[1] if existing else 0
            match_status = existing[2] if existing else "uncomputed"
            match_reason = existing[3] if existing else ""
            match_updated_at = existing[4] if existing else 0.0
            manual_override_at = existing[5] if existing else 0.0
            manual_override_by = existing[6] if existing else ""

            # 如果 item 有 number_of_seasons/number_of_episodes（watchlist API），始终更新计数（P2-2）
            if item.get("number_of_seasons") is not None:
                season_count = int(item["number_of_seasons"])
            if item.get("number_of_episodes") is not None:
                episode_count = int(item["number_of_episodes"])

            # 删除旧的 FTS 记录
            conn.execute(
                "DELETE FROM tmdb_watchlist_fts WHERE rowid = (SELECT rowid FROM tv WHERE id=?)",
                (item["id"],),
            )

            conn.execute(
                """INSERT OR REPLACE INTO tv
                (id, name, original_name, overview, poster_path, backdrop_path,
                 first_air_date, vote_average, vote_count, genre_ids, popularity,
                 origin_country, original_language, _season_count, _episode_count,
                 _last_ep_season, _last_ep_episode, _media_type, _synced_at,
                 match_status, match_reason, match_updated_at, manual_override_at, manual_override_by)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    json.dumps(
                        self._val(
                            item,
                            "genre_ids",
                            []),
                        ensure_ascii=False),
                    float(self._val(item, "popularity", 0)),
                    json.dumps(
                        self._val(
                            item,
                            "origin_country",
                            []),
                        ensure_ascii=False),
                    self._val(item, "original_language"),
                    season_count,
                    episode_count,
                    0,  # _last_ep_season — 由 _populate_tv_details 填充
                    0,  # _last_ep_episode — 由 _populate_tv_details 填充
                    "tv",
                    synced_at,
                    match_status,
                    match_reason,
                    match_updated_at,
                    manual_override_at,
                    manual_override_by,
                ),
            )
            
            # 插入新的 FTS 记录（使用 name 作为 title）
            conn.execute(
                """INSERT INTO tmdb_watchlist_fts(rowid, title, original_title, overview)
                VALUES((SELECT rowid FROM tv WHERE id=?), ?, ?, ?)""",
                (
                    item["id"],
                    self._val(item, "name"),
                    self._val(item, "original_name"),
                    self._val(item, "overview"),
                ),
            )
            
            conn.commit()

    # ----------------------------------------------------------
    # 批量获取季数（并发 + 限速）
    # ----------------------------------------------------------

    def _populate_tv_details(self, tmdb_client: TmdbClient) -> None:
        """批量补齐 TV 表的 _season_count, _episode_count, _last_ep 信息。

        同步后调用，查询所有 _season_count = 0 的剧集，
        用线程池并发调用 TMDB API（get_tv_details），每批 10 个请求后等待 2s 避免限速。
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id FROM tv WHERE _season_count IS NULL OR _season_count <= 0"
            ).fetchall()
        ids_to_fetch = [r[0] for r in rows]
        if not ids_to_fetch:
            return

        logging.info(
            "[TMDB-DB] 正在批量获取 %d 部剧集的详情...",
            len(ids_to_fetch))

        BATCH_SIZE = 10
        BATCH_SLEEP = 2.0  # 每批间隔秒数

        def _fetch(tid: int) -> dict | None:
            try:
                details = tmdb_client.get_tv_details(tid)
                if details and isinstance(details, dict):
                    return details
                return None
            except Exception:
                return None

        fetched = 0
        for start in range(0, len(ids_to_fetch), BATCH_SIZE):
            batch = ids_to_fetch[start:start + BATCH_SIZE]
            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = {pool.submit(_fetch, tid): tid for tid in batch}
                for future in as_completed(futures):
                    details = future.result()
                    if details:
                        tid = details.get("id")
                        if not tid:
                            continue
                        season_count = int(
                            details.get("number_of_seasons") or 0)
                        episode_count = int(
                            details.get("number_of_episodes") or 0)
                        last_ep = details.get("last_episode_to_air")
                        last_ep_season = 0
                        last_ep_episode = 0
                        if last_ep and isinstance(last_ep, dict):
                            last_ep_season = int(
                                last_ep.get("season_number") or 0)
                            last_ep_episode = int(
                                last_ep.get("episode_number") or 0)
                        try:
                            with self._conn() as conn:
                                conn.execute(
                                    """UPDATE tv SET
                                        _season_count=?,
                                        _episode_count=?,
                                        _last_ep_season=?,
                                        _last_ep_episode=?
                                    WHERE id=?""",
                                    (season_count, episode_count,
                                     last_ep_season, last_ep_episode, tid))
                                conn.commit()
                            fetched += 1
                        except Exception:
                            pass
            if start + BATCH_SIZE < len(ids_to_fetch):
                time.sleep(BATCH_SLEEP)

        if fetched:
            logging.info(
                "[TMDB-DB] 剧集详情批量获取完成: %d/%d 部",
                fetched,
                len(ids_to_fetch))

    # ----------------------------------------------------------
    # TMDB 操作日志
    # ----------------------------------------------------------

    def log_tmdb_operation(self, op: str, level: str,
                           msg: str, detail: str | None = None) -> None:
        """写入一条 TMDB 操作日志"""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO tmdb_operation_log (ts, op, level, msg, detail) VALUES (?, ?, ?, ?, ?)",
                (time.time(), op, level, msg, detail),
            )
            conn.commit()

    def get_tmdb_logs(self, limit: int = 100) -> list[dict]:
        """获取最近的 TMDB 操作日志（按时间倒序），超过 7 天或超过行数限制的自动清理"""
        seven_days_ago = time.time() - 7 * 86400
        with self._conn() as conn:
            # 清理 7 天前的日志
            conn.execute(
                "DELETE FROM tmdb_operation_log WHERE ts < ?", (seven_days_ago,))
            
            # 清理超过行数限制的日志（保留最新的 N 条）
            conn.execute("""
                DELETE FROM tmdb_operation_log 
                WHERE id NOT IN (
                    SELECT id FROM tmdb_operation_log 
                    ORDER BY ts DESC 
                    LIMIT ?
                )
            """, (self._tmdb_log_max_rows,))
            
            cur = conn.execute(
                "SELECT id, ts, op, level, msg, detail FROM tmdb_operation_log ORDER BY ts DESC LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
        return [
            {"id": r["id"], "ts": r["ts"], "op": r["op"], "level": r["level"],
             "msg": r["msg"], "detail": r["detail"]}
            for r in rows
        ]

    # ----------------------------------------------------------
    # webui_config CRUD
    # ----------------------------------------------------------

    def get_config(self, scope: str, key: str, default: str = "") -> str:
        """读取单个配置值。敏感键自动解密。"""
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT value FROM webui_config WHERE scope=? AND key=?",
                    (scope, key),
                ).fetchone()
                value = row[0] if row else default
                # 敏感键自动解密
                if (scope, key) in _SENSITIVE_KEYS and secret_manager.is_encrypted(value):
                    return secret_manager.decrypt(value)
                return value
        except Exception:
            return default

    def set_config(self, scope: str, key: str, value: str) -> None:
        """写入单个配置值（UPSERT）。敏感键自动加密。"""
        # 敏感键自动加密
        if (scope, key) in _SENSITIVE_KEYS and value:
            value = secret_manager.encrypt(value)
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO webui_config (scope, key, value, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(scope, key) DO UPDATE SET
                       value=excluded.value, updated_at=excluded.updated_at""",
                (scope, key, value, time.time()),
            )
            conn.commit()

    def get_all_config(self, scope: str) -> dict[str, str]:
        """读取某个 scope 下所有配置，返回 {key: value} 字典。敏感键自动解密。"""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT key, value FROM webui_config WHERE scope=?",
                    (scope,),
                ).fetchall()
                result = {}
                for r in rows:
                    key = r["key"]
                    value = r["value"]
                    # 敏感键自动解密
                    if (scope, key) in _SENSITIVE_KEYS and secret_manager.is_encrypted(value):
                        value = secret_manager.decrypt(value)
                    result[key] = value
                return result
        except Exception:
            return {}

    def delete_config(self, scope: str, key: str) -> None:
        """删除单个配置。"""
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM webui_config WHERE scope=? AND key=?",
                (scope, key),
            )
            conn.commit()

    def migrate_plaintext_to_encrypted(self) -> None:
        """将历史明文凭据加密（幂等操作）。

        遍历 _SENSITIVE_KEYS，对每个键：
        1. 直接读 DB 原始值（绕过 get_config 的解密层）
        2. 若原始值非空且不以 "ENC:" 开头 → 加密后写回
        3. 若已是 "ENC:" 开头 → 跳过（幂等）
        4. 若为空 → 跳过（避免无意义的加密）

        此方法应在 WebUIServer._reinit_watchlist_db() 中调用，
        作为旧版本 DB 残留明文凭据的兜底加密机会。
        """
        for scope, key in _SENSITIVE_KEYS:
            try:
                # 直接读 DB 原始值（绕过 get_config 的解密层）
                with self._conn() as conn:
                    row = conn.execute(
                        "SELECT value FROM webui_config WHERE scope=? AND key=?",
                        (scope, key),
                    ).fetchone()
                    if not row:
                        continue
                    raw_value = row[0]
                    # 空值或已加密 → 跳过
                    if not raw_value or secret_manager.is_encrypted(raw_value):
                        continue
                    # 明文 → 加密后写回
                    encrypted = secret_manager.encrypt(raw_value)
                    conn.execute(
                        """UPDATE webui_config
                           SET value=?, updated_at=?
                           WHERE scope=? AND key=?""",
                        (encrypted, time.time(), scope, key),
                    )
                    conn.commit()
                    logging.info(
                        "[SecretManager] 已加密敏感配置: %s.%s", scope, key
                    )
            except Exception as e:
                logging.warning(
                    "[SecretManager] 加密迁移失败 (%s.%s): %s", scope, key, e
                )
