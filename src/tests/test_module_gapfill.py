"""后端模块覆盖缺口补测。

覆盖此前零直接断言的面：
- reset_admin.py：任意未知 - 前缀标志拒绝 / DB 不存在退出 / 无参随机密码路径
- logger_setup.EncodingSafeStreamHandler.emit：GBK 控制台降级替代符而非崩溃
- secret_manager.check_decryption_health：健康结构与损坏密文降级语义
- watchlist_match.collect_b_media_snapshot：B 区快照聚合直接契约
- tmdb_watchlist_db 批量与单条 upsert 终态等价 + 匹配状态保留
- reset_admin 内联 DDL 与 tmdb_watchlist_db 建表语句漂移检测

期望值全部由各函数源码契约推导（引用见各用例注释）。
"""
from __future__ import annotations

import importlib.util
import logging
import re
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC_DIR))

import secret_manager as sm  # noqa: E402
from database import Database  # noqa: E402
from logger_setup import EncodingSafeStreamHandler  # noqa: E402
from utils.password_utils import verify_password  # noqa: E402

PROJECT_ROOT = SRC_DIR.parent
RESET_ADMIN_PATH = PROJECT_ROOT / "reset_admin.py"


def _load_reset_admin():
    spec = importlib.util.spec_from_file_location("reset_admin",
                                                  RESET_ADMIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── reset_admin.py ───────────────────────────────────────────────────

class TestResetAdmin:
    def test_unknown_dash_flag_is_rejected_before_db_access(self):
        reset_admin = _load_reset_admin()
        with patch("sys.argv", ["reset_admin.py", "-x"]):
            with pytest.raises(SystemExit) as ei:
                reset_admin.main()
        assert ei.value.code == 1

    def test_multiple_unknown_flags_are_rejected(self):
        reset_admin = _load_reset_admin()
        with patch("sys.argv", ["reset_admin.py", "--db", "/tmp/x.db", "-y"]):
            with pytest.raises(SystemExit) as ei:
                reset_admin.main()
        assert ei.value.code == 1

    def test_missing_db_exits_one(self, tmp_path):
        reset_admin = _load_reset_admin()
        with patch.object(reset_admin, "find_db_path",
                          lambda: str(tmp_path / "absent.db")):
            with patch("sys.argv", ["reset_admin.py", "goodpass"]):
                with pytest.raises(SystemExit) as ei:
                    reset_admin.main()
        assert ei.value.code == 1

    def test_no_args_generates_random_usable_password(self, tmp_path, capsys):
        reset_admin = _load_reset_admin()
        db_path = tmp_path / "fresh.db"
        db_path.write_bytes(b"")  # 仅需存在；webui_config 表由脚本自建
        with patch.object(reset_admin, "find_db_path", lambda: str(db_path)):
            with patch("sys.argv", ["reset_admin.py"]):
                reset_admin.main()
        out = capsys.readouterr().out
        m = re.search(r"新密码:\s*(\S+)", out)
        assert m is not None, f"输出缺新密码行: {out}"
        password = m.group(1)
        assert len(password) >= 16, "secrets.token_urlsafe(12) 应产生 16 字符随机密码"
        conn = sqlite3.connect(str(db_path))
        try:
            stored = conn.execute(
                "SELECT value FROM webui_config"
                " WHERE scope='ui' AND key='admin_password'").fetchone()[0]
        finally:
            conn.close()
        assert verify_password(password, stored), "随机密码须能与写入的哈希互验"


# ── logger_setup.EncodingSafeStreamHandler ───────────────────────────

class _FakeStream:
    def __init__(self, encoding):
        self.encoding = encoding
        self.written = []
        self.flushed = False

    def write(self, message):
        self.written.append(message)

    def flush(self):
        self.flushed = True


def _emit(message, encoding):
    stream = _FakeStream(encoding)
    handler = EncodingSafeStreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord("t", logging.INFO, __file__, 1, message,
                               None, None)
    handler.emit(record)
    return stream


class TestEncodingSafeStreamHandler:
    def test_gbk_console_downgrades_unencodable_to_replacement(self):
        stream = _emit("启动 🚀 完成", "gbk")
        assert stream.written == ["启动 ? 完成\n"], (
            "GBK 无法表示的字符应降级为 ? 且整条日志不消失")
        assert stream.flushed is True

    def test_utf8_console_keeps_emoji_intact(self):
        stream = _emit("启动 🚀 完成", "utf-8")
        assert stream.written == ["启动 🚀 完成\n"]


# ── secret_manager.check_decryption_health ───────────────────────────

@pytest.mark.skipif(not sm._check_cryptography_available(),
                    reason="cryptography 未安装，secret_manager 降级为明文"
                           "存储，加密路径不可测")
class TestDecryptionHealth:
    def test_healthy_structure_when_no_failures(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sm, "_KEY_FILE", tmp_path / "k1")
        sm.reset_master_key_for_testing()
        monkeypatch.setattr(sm, "_decryption_failed", False)
        monkeypatch.setattr(sm, "_decryption_failure_count", 0)
        assert sm.check_decryption_health() == {
            "healthy": True,
            "decryption_failed": False,
            "failure_count": 0,
            "message": "解密功能正常",
        }

    def test_corrupted_key_downgrades_health(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sm, "_KEY_FILE", tmp_path / "k1")
        sm.reset_master_key_for_testing()
        token = sm.encrypt("secret-token")
        assert sm.is_encrypted(token)
        # 换一把新主密钥（模拟 .secret_key 丢失后重建）→ 旧密文解密失败
        monkeypatch.setattr(sm, "_KEY_FILE", tmp_path / "k2")
        sm.reset_master_key_for_testing()
        assert sm.decrypt(token) == "", "损坏密文应降级为空串（凭据未配置）而非抛异常"
        health = sm.check_decryption_health()
        assert health["healthy"] is False
        assert health["decryption_failed"] is True
        assert health["failure_count"] >= 1
        assert "解密失败" in health["message"]


# ── watchlist_match.collect_b_media_snapshot ─────────────────────────

def _seed_b(db, rows):
    with db.rw_lock.write_locked(), db.connection() as conn:
        for row in rows:
            conn.execute(
                """INSERT INTO b_strm_files(
                    local_path, webdav_path, parent_webdav_path,
                    source_a_path, fingerprint, status, updated_at, mapping_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", row)
        conn.commit()


def _row(local, webdav, fingerprint, mapping_id):
    # parent_webdav_path 为 NOT NULL（database.py 建表约束），按 webdav
    # 路径取父目录；source_a_path 可空。计划夹具遗漏该约束，就地纠偏。
    return (local, webdav, webdav.rsplit("/", 1)[0], None, fingerprint,
            "valid", 100.0, mapping_id)


class TestCollectBMediaSnapshot:
    def test_tv_episodes_aggregate_with_max_season_and_unique_episode_count(
            self, tmp_path):
        from watchlist_match import collect_b_media_snapshot
        db = Database(str(tmp_path / "bridge.db"))
        fp = "fp-tv-1"
        _seed_b(db, [
            _row("X:\\B\\番剧\\凡人修仙传\\Season 01\\凡人修仙传 - S01E01.strm",
                 "/dav/m1/番剧/凡人修仙传/Season 01/凡人修仙传 - S01E01.strm",
                 fp, "m1"),
            _row("X:\\B\\番剧\\凡人修仙传\\Season 02\\凡人修仙传 - S02E03.strm",
                 "/dav/m1/番剧/凡人修仙传/Season 02/凡人修仙传 - S02E03.strm",
                 fp, "m1"),
        ])
        snap = collect_b_media_snapshot(db)
        assert len(snap["tv"]) == 1
        entry = snap["tv"][0]
        assert entry["name"] == "凡人修仙传"
        assert entry["season_num"] == 2
        assert entry["season"] == "第2季"
        assert entry["episode_count"] == 2
        assert entry["episode_hint"] is True

    def test_same_name_across_mappings_is_not_merged(self, tmp_path):
        from watchlist_match import collect_b_media_snapshot
        db = Database(str(tmp_path / "bridge.db"))
        _seed_b(db, [
            _row("X:\\B\\电影\\沙丘 2\\dune2.strm",
                 "/dav/m1/电影/沙丘 2/dune2.strm", "fp-a", "m1"),
            _row("X:\\B2\\电影\\沙丘 2\\dune2.strm",
                 "/dav/m2/电影/沙丘 2/dune2.strm", "fp-b", "m2"),
        ])
        snap = collect_b_media_snapshot(db)
        assert len(snap["movie"]) == 2, "聚合键含 mapping_id，跨映射同名媒体不得合并"
        assert {e["name"] for e in snap["movie"]} == {"沙丘 2"}


# ── tmdb_watchlist_db 批量与单条 upsert 等价 ─────────────────────────

def _movie_item():
    return {
        "id": 313369, "title": "海王", "original_title": "Aquaman",
        "overview": "o", "poster_path": "/p.jpg", "backdrop_path": "/b.jpg",
        "release_date": "2018-12-07", "vote_average": 7.4,
        "vote_count": 1200, "genre_ids": [28, 878], "popularity": 88.1,
        "original_language": "en", "video": False, "adult": False,
    }


def _tv_item():
    return {
        "id": 1399, "name": "权力的游戏",
        "original_name": "Game of Thrones", "overview": "o",
        "poster_path": "/p.jpg", "backdrop_path": "/b.jpg",
        "first_air_date": "2011-04-17", "vote_average": 8.4,
        "vote_count": 900, "genre_ids": [18], "popularity": 77.7,
        "origin_country": ["US"], "original_language": "en",
    }


def _read_movies(db, movie_id):
    cols = ("title, original_title, overview, poster_path, backdrop_path, "
            "release_date, vote_average, vote_count, genre_ids, popularity, "
            "original_language, video, adult, _media_type, _synced_at, "
            "match_status, match_reason, match_updated_at, "
            "manual_override_at, manual_override_by")
    with db._conn() as conn:
        movie = conn.execute(
            f"SELECT {cols} FROM movies WHERE id=?", (movie_id,)).fetchone()
        fts = conn.execute(
            "SELECT title, original_title, overview FROM movies_fts"
            " WHERE rowid = (SELECT rowid FROM movies WHERE id=?)",
            (movie_id,)).fetchone()
    return movie, fts


def _read_tvs(db, tv_id):
    cols = ("name, original_name, overview, poster_path, backdrop_path, "
            "first_air_date, vote_average, vote_count, genre_ids, popularity, "
            "origin_country, original_language, _season_count, _episode_count, "
            "_media_type, _synced_at")
    with db._conn() as conn:
        return conn.execute(
            f"SELECT {cols} FROM tv WHERE id=?", (tv_id,)).fetchone()


class TestBatchUpsertEquivalence:
    def test_movie_batch_and_single_upsert_produce_identical_state(
            self, tmp_path):
        from tmdb_watchlist_db import TmdbWatchlistDb
        item = _movie_item()
        db_single = TmdbWatchlistDb(str(tmp_path / "single.db"))
        db_batch = TmdbWatchlistDb(str(tmp_path / "batch.db"))
        db_single._upsert_movie(item, 100.0)
        with db_batch._conn() as conn:
            db_batch._upsert_movies_batch(conn, [item], 100.0)
        assert _read_movies(db_single, item["id"]) == \
            _read_movies(db_batch, item["id"])

    def test_tv_batch_and_single_upsert_produce_identical_state(
            self, tmp_path):
        from tmdb_watchlist_db import TmdbWatchlistDb
        item = _tv_item()
        db_single = TmdbWatchlistDb(str(tmp_path / "single.db"))
        db_batch = TmdbWatchlistDb(str(tmp_path / "batch.db"))
        db_single._upsert_tv(item, 100.0)
        with db_batch._conn() as conn:
            db_batch._upsert_tvs_batch(conn, [item], 100.0)
        assert _read_tvs(db_single, item["id"]) == \
            _read_tvs(db_batch, item["id"])

    def test_reupsert_preserves_user_match_state(self, tmp_path):
        from tmdb_watchlist_db import TmdbWatchlistDb
        db = TmdbWatchlistDb(str(tmp_path / "w.db"))
        item = _movie_item()
        db._upsert_movie(item, 100.0)
        with db._conn() as conn:
            conn.execute(
                "UPDATE movies SET match_status='matched', match_reason='r',"
                " match_updated_at=55.0, manual_override_at=7.0,"
                " manual_override_by='human' WHERE id=?", (item["id"],))
            conn.commit()
        with db._conn() as conn:
            db._upsert_movies_batch(conn, [item], 200.0)
        with db._conn() as conn:
            row = conn.execute(
                "SELECT match_status, manual_override_at, _synced_at"
                " FROM movies WHERE id=?", (item["id"],)).fetchone()
        assert row[0] == "matched"
        assert row[1] == 7.0
        assert row[2] == 200.0, "重同步应推进 _synced_at 同时保留匹配状态"

    def test_tv_batch_preserves_counts_when_item_lacks_counters(
            self, tmp_path):
        from tmdb_watchlist_db import TmdbWatchlistDb
        db = TmdbWatchlistDb(str(tmp_path / "w.db"))
        item = _tv_item()
        db._upsert_tv(item, 100.0)
        with db._conn() as conn:
            conn.execute(
                "UPDATE tv SET _season_count=9, _episode_count=99"
                " WHERE id=?", (item["id"],))
            conn.commit()
        with db._conn() as conn:
            db._upsert_tvs_batch(conn, [item], 200.0)
        with db._conn() as conn:
            row = conn.execute(
                "SELECT _season_count, _episode_count FROM tv WHERE id=?",
                (item["id"],)).fetchone()
        assert tuple(row) == (9, 99), (
            "item 无 number_of_* 键时批量 upsert 不得清零既有计数")


# ── webui_config DDL 漂移契约 ────────────────────────────────────────

_WEBUI_DDL_RE = re.compile(
    r"CREATE TABLE IF NOT EXISTS webui_config\s*\((.*?)\)\s*(?:;|\"\"\")",
    re.S)


def _webui_config_schema(create_sql):
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(create_sql)
        return conn.execute("PRAGMA table_info(webui_config)").fetchall()
    finally:
        conn.close()


class TestWebuiConfigDdlDrift:
    def test_reset_admin_inline_ddl_matches_tmdb_watchlist_db(self):
        reset_src = RESET_ADMIN_PATH.read_text(encoding="utf-8")
        db_src = (SRC_DIR / "tmdb_watchlist_db.py").read_text(encoding="utf-8")
        m_reset = _WEBUI_DDL_RE.search(reset_src)
        m_db = _WEBUI_DDL_RE.search(db_src)
        assert m_reset and m_db, "两处建表语句至少一处缺失或形态变更"
        schema_reset = _webui_config_schema(
            "CREATE TABLE IF NOT EXISTS webui_config ("
            + m_reset.group(1) + ")")
        schema_db = _webui_config_schema(
            "CREATE TABLE IF NOT EXISTS webui_config (" + m_db.group(1) + ")")
        assert schema_reset == schema_db, (
            "reset_admin.py 内联 DDL 与 tmdb_watchlist_db.py 建表语句"
            "列集/PK 漂移")
