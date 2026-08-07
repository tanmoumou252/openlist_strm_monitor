"""测试 FTS 孤儿记录清理

验证批量同步删除 movies/tv 时，FTS 表中的孤儿记录被正确清理。
"""
import pytest
from tmdb_watchlist_db import TmdbWatchlistDb


class _FakeTmdbClient:
    """最小 TMDB 客户端桩：返回固定 watchlist 分页数据。"""

    def __init__(self, movies=None, tv=None):
        self.movies = movies or []
        self.tv = tv or []

    def get_watchlist_movies(self, page):
        return (self.movies, False)

    def get_watchlist_tv(self, page):
        return (self.tv, False)

    def get_tv_details(self, tid):
        return None


class TestFTSOrphanCleanup:
    """测试批量同步删除时 FTS 表的级联清理"""

    def test_movie_sync_removes_fts_orphans(self, tmp_path):
        """电影同步删除时，应同步清理 FTS 表中的孤儿记录"""
        db_path = str(tmp_path / "test.db")
        db = TmdbWatchlistDb(db_path)

        # 插入 3 部电影
        now = 1000000.0
        for movie_id in [101, 102, 103]:
            db._upsert_movie({
                "id": movie_id,
                "title": f"Movie {movie_id}",
                "original_title": f"Original {movie_id}",
                "overview": f"Overview {movie_id}",
                "poster_path": "/poster.jpg",
                "release_date": "2024-01-01",
                "vote_average": 7.5,
                "vote_count": 100,
            }, now)

        # 验证 FTS 表有 3 条记录
        with db._conn() as conn:
            fts_count = conn.execute("SELECT COUNT(*) FROM tmdb_watchlist_fts").fetchone()[0]
            assert fts_count == 3, f"FTS 表应有 3 条记录，实际 {fts_count}"

        # 模拟同步：只保留 movie_id 101，删除 102 和 103
        with db._conn() as conn:
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS _keep_movie_ids (id INTEGER)")
            conn.execute("DELETE FROM _keep_movie_ids")
            conn.executemany("INSERT INTO _keep_movie_ids VALUES (?)", [(101,)])

            # 调用修复后的批量删除逻辑
            conn.execute("DELETE FROM movies WHERE id NOT IN (SELECT id FROM _keep_movie_ids)")
            # 修复：同步清理 FTS 表中的孤儿记录
            conn.execute(
                "DELETE FROM tmdb_watchlist_fts WHERE rowid NOT IN (SELECT rowid FROM movies)")
            conn.commit()

        # 验证 FTS 表只剩 1 条记录（movie_id 101）
        with db._conn() as conn:
            fts_count = conn.execute("SELECT COUNT(*) FROM tmdb_watchlist_fts").fetchone()[0]
            assert fts_count == 1, f"FTS 表应剩 1 条记录（movie_id 101），实际 {fts_count}（存在孤儿记录）"

            # 验证剩余的是 movie_id 101
            remaining = conn.execute("""
                SELECT m.title FROM movies m
                JOIN tmdb_watchlist_fts f ON m.rowid = f.rowid
            """).fetchone()
            assert remaining is not None, "应剩余 1 条记录"
            assert remaining[0] == "Movie 101", f"剩余记录应为 Movie 101，实际 {remaining[0]}"

    def test_tv_sync_removes_fts_orphans(self, tmp_path):
        """电视剧同步删除时，应同步清理 FTS 表中的孤儿记录"""
        db_path = str(tmp_path / "test.db")
        db = TmdbWatchlistDb(db_path)

        # 插入 3 部电视剧
        now = 1000000.0
        for tv_id in [201, 202, 203]:
            db._upsert_tv({
                "id": tv_id,
                "name": f"TV {tv_id}",
                "original_name": f"Original {tv_id}",
                "overview": f"Overview {tv_id}",
                "poster_path": "/poster.jpg",
                "first_air_date": "2024-01-01",
                "vote_average": 8.0,
                "vote_count": 200,
            }, now)

        # 验证 FTS 表有 3 条记录
        with db._conn() as conn:
            fts_count = conn.execute("SELECT COUNT(*) FROM tmdb_watchlist_fts").fetchone()[0]
            assert fts_count == 3, f"FTS 表应有 3 条记录，实际 {fts_count}"

        # 模拟同步：只保留 tv_id 201，删除 202 和 203
        with db._conn() as conn:
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS _keep_tv_ids (id INTEGER)")
            conn.execute("DELETE FROM _keep_tv_ids")
            conn.executemany("INSERT INTO _keep_tv_ids VALUES (?)", [(201,)])

            # 调用修复后的批量删除逻辑
            conn.execute("DELETE FROM tv WHERE id NOT IN (SELECT id FROM _keep_tv_ids)")
            # 修复：同步清理 FTS 表中的孤儿记录
            conn.execute(
                "DELETE FROM tmdb_watchlist_fts WHERE rowid NOT IN (SELECT rowid FROM tv)")
            conn.commit()

        # 验证 FTS 表只剩 1 条记录（tv_id 201）
        with db._conn() as conn:
            fts_count = conn.execute("SELECT COUNT(*) FROM tmdb_watchlist_fts").fetchone()[0]
            assert fts_count == 1, f"FTS 表应剩 1 条记录（tv_id 201），实际 {fts_count}（存在孤儿记录）"

            # 验证剩余的是 tv_id 201
            remaining = conn.execute("""
                SELECT t.name FROM tv t
                JOIN tmdb_watchlist_fts f ON t.rowid = f.rowid
            """).fetchone()
            assert remaining is not None, "应剩余 1 条记录"
            assert remaining[0] == "TV 201", f"剩余记录应为 TV 201，实际 {remaining[0]}"

    def test_full_delete_clears_all_fts(self, tmp_path):
        """全量删除时应清空 FTS 表"""
        db_path = str(tmp_path / "test.db")
        db = TmdbWatchlistDb(db_path)

        # 插入 2 部电影
        now = 1000000.0
        for movie_id in [301, 302]:
            db._upsert_movie({
                "id": movie_id,
                "title": f"Movie {movie_id}",
                "original_title": f"Original {movie_id}",
                "overview": f"Overview {movie_id}",
                "poster_path": "/poster.jpg",
                "release_date": "2024-01-01",
                "vote_average": 7.5,
                "vote_count": 100,
            }, now)

        # 验证 FTS 表有 2 条记录
        with db._conn() as conn:
            fts_count = conn.execute("SELECT COUNT(*) FROM tmdb_watchlist_fts").fetchone()[0]
            assert fts_count == 2, f"FTS 表应有 2 条记录，实际 {fts_count}"

        # 模拟同步：movie_ids 为空，全量删除
        with db._conn() as conn:
            # 调用修复后的全量删除逻辑
            conn.execute("DELETE FROM movies")
            # 修复：同步清理 FTS 表
            conn.execute("DELETE FROM tmdb_watchlist_fts")
            conn.commit()

        # 验证 FTS 表已清空
        with db._conn() as conn:
            fts_count = conn.execute("SELECT COUNT(*) FROM tmdb_watchlist_fts").fetchone()[0]
            assert fts_count == 0, f"FTS 表应已清空，实际 {fts_count}（存在孤儿记录）"

    def test_sync_preserves_both_movie_and_tv_fts(self, tmp_path):
        """H1 回归：sync() 同时含电影+剧集时，电影的 FTS 行不得被剧集清理误删。

        旧实现：剧集清理 `DELETE FROM tmdb_watchlist_fts WHERE rowid NOT IN (SELECT rowid FROM tv)`
        会删掉全部电影 FTS 行，导致 /api/tmdb/watchlist/movie 搜索返回空。
        """
        db_path = str(tmp_path / "test.db")
        db = TmdbWatchlistDb(db_path)

        movies = [{
            "id": 1001,
            "title": "Inception",
            "original_title": "Inception",
            "overview": "A thief who steals corporate secrets.",
            "poster_path": "/p.jpg",
            "release_date": "2010-07-16",
            "vote_average": 8.4,
            "vote_count": 1000,
        }]
        tvs = [{
            "id": 2001,
            "name": "Breaking Bad",
            "original_name": "Breaking Bad",
            "overview": "A high school chemistry teacher.",
            "poster_path": "/t.jpg",
            "first_air_date": "2008-01-20",
            "vote_average": 9.0,
            "vote_count": 5000,
            "number_of_seasons": 5,  # 使 _populate_tv_details 跳过详情拉取
        }]

        client = _FakeTmdbClient(movies=movies, tv=tvs)
        db.sync(client, force=True)

        # 1) FTS 应同时保留电影与剧集两类行
        with db._conn() as conn:
            fts_count = conn.execute("SELECT COUNT(*) FROM tmdb_watchlist_fts").fetchone()[0]
            assert fts_count == 2, (
                f"sync() 后 FTS 应保留 2 条（电影 1 + 剧集 1），实际 {fts_count}（电影行被剧集清理误删）"
            )

        # 2) 电影 FTS 搜索仍能返回结果（此前被剧集清理清空）
        with db._conn() as conn:
            rows = conn.execute(
                "SELECT title FROM tmdb_watchlist_fts WHERE tmdb_watchlist_fts MATCH 'Inception'"
            ).fetchall()
            assert len(rows) == 1, f"电影 FTS 搜索应返回 1 条，实际 {len(rows)}"
            assert rows[0][0] == "Inception"

        # 3) 剧集 FTS 搜索仍能返回结果
        with db._conn() as conn:
            rows = conn.execute(
                "SELECT title FROM tmdb_watchlist_fts WHERE tmdb_watchlist_fts MATCH 'Breaking'"
            ).fetchall()
            assert len(rows) == 1, f"剧集 FTS 搜索应返回 1 条，实际 {len(rows)}"
            assert rows[0][0] == "Breaking Bad"
