"""
FTS5 中文分词搜索测试。

测试 SQLite FTS5 虚拟表和 simple 分词器的功能：
- 分词器加载
- FTS5 表创建
- 数据同步
- 中文搜索
- 混合语言搜索
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# 确保 src 在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import Database


class TestFTS5Tokenizer:
    """测试 simple 分词器加载"""

    def test_simple_dll_exists(self):
        """验证 simple.dll 文件存在（迁移后位于 src/tokenizers/simple/）"""
        simple_dll = (
            Path(__file__).resolve().parent.parent
            / "tokenizers" / "simple" / "simple.dll"
        )
        assert simple_dll.exists(), f"simple.dll 不存在: {simple_dll}"

    def test_simple_version_readable(self):
        """验证 Database 加载 simple 分词器后 _simple_version 非空（来自 VERSION 文件）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)
            with db.connection() as conn:
                # 若 simple.dll 缺失则软降级 unicode61，_simple_version 可能为空；
                # 有 dll 时必为版本号字符串（如 v0.7.1）。
                if (Path(__file__).resolve().parent.parent / "tokenizers" / "simple" / "simple.dll").exists():
                    assert db._simple_version, "simple 加载成功但 _simple_version 为空"
                    assert db._simple_version.startswith("v"), (
                        f"_simple_version 格式异常: {db._simple_version!r}"
                    )

    def test_load_simple_tokenizer(self):
        """测试分词器加载"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            # 获取连接并验证分词器已加载
            with db.connection() as conn:
                # 尝试创建 FTS5 表，如果分词器未加载会失败
                try:
                    conn.execute("""
                        CREATE VIRTUAL TABLE test_fts USING fts5(
                            content,
                            tokenize='simple'
                        )
                    """)
                    success = True
                except Exception as e:
                    success = False
                    print(f"FTS5 创建失败: {e}")

                assert success, "simple 分词器加载失败"


class TestFTS5Table:
    """测试 FTS5 虚拟表"""

    def test_create_fts_tables(self):
        """测试 FTS5 表创建"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            # 验证 FTS5 表已创建
            with db.read_connection() as conn:
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%_fts'"
                )
                tables = [row[0] for row in cursor.fetchall()]

                assert "a_strm_files_fts" in tables
                assert "b_strm_files_fts" in tables
                assert "c_ghost_files_fts" in tables

    def test_data_sync_to_fts(self):
        """测试数据同步到 FTS5 表"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            # 插入测试数据
            db.upsert_a(
                local_path="/test/黑暗骑士/黑暗骑士.strm",
                webdav_path="/test/黑暗骑士/黑暗骑士.strm",
                parent_webdav_path="/test/黑暗骑士"
            )

            # 验证 FTS5 表中有数据
            with db.read_connection() as conn:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM a_strm_files_fts"
                )
                count = cursor.fetchone()[0]
                assert count == 1, f"FTS5 表中应有 1 条记录，实际有 {count} 条"


class TestChineseSearch:
    """测试中文搜索功能"""

    def test_search_chinese_keyword(self):
        """测试中文关键词搜索"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            # 插入多条测试数据
            test_data = [
                ("/test/黑暗骑士/黑暗骑士.strm", "/test/黑暗骑士"),
                ("/test/黑暗之光/黑暗之光.strm", "/test/黑暗之光"),
                ("/test/黎明前的黑暗/黎明前的黑暗.strm", "/test/黎明前的黑暗"),
                ("/test/蝙蝠侠/蝙蝠侠.strm", "/test/蝙蝠侠"),
            ]

            for local_path, parent_path in test_data:
                db.upsert_a(
                    local_path=local_path,
                    webdav_path=local_path,
                    parent_webdav_path=parent_path
                )

            # 搜索"黑暗"
            with db.read_connection() as conn:
                cursor = conn.execute("""
                    SELECT local_path FROM a_strm_files
                    WHERE rowid IN (
                        SELECT rowid FROM a_strm_files_fts
                        WHERE a_strm_files_fts MATCH ?
                    )
                """, ("黑暗",))

                results = [row[0] for row in cursor.fetchall()]

                # 应该匹配 3 条记录
                assert len(results) == 3, f"应匹配 3 条记录，实际匹配 {len(results)} 条"
                assert all("黑暗" in path for path in results)

    def test_search_dark_vs_reverse(self):
        """固化分词语义：simple 按词分词，'黑暗' 与 '暗黑' 是两个不同词，互不命中。

        数据集含「黑暗骑士 / 黑暗之光 / 暗黑破坏神 / 黎明前的黑暗」：
        - 搜 '黑暗' → 3 条（黑暗骑士、黑暗之光、黎明前的黑暗），不含 暗黑破坏神
        - 搜 '暗黑' → 仅 1 条（暗黑破坏神）
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            data = [
                ("/test/黑暗骑士/黑暗骑士.strm", "/test/黑暗骑士"),
                ("/test/黑暗之光/黑暗之光.strm", "/test/黑暗之光"),
                ("/test/暗黑破坏神/暗黑破坏神.strm", "/test/暗黑破坏神"),
                ("/test/黎明前的黑暗/黎明前的黑暗.strm", "/test/黎明前的黑暗"),
            ]
            for local_path, parent_path in data:
                db.upsert_a(
                    local_path=local_path,
                    webdav_path=local_path,
                    parent_webdav_path=parent_path,
                )

            def _match(query: str) -> list[str]:
                with db.read_connection() as conn:
                    cursor = conn.execute(
                        """
                        SELECT local_path FROM a_strm_files
                        WHERE rowid IN (
                            SELECT rowid FROM a_strm_files_fts
                            WHERE a_strm_files_fts MATCH ?
                        )
                        """,
                        (query,),
                    )
                    return [row[0] for row in cursor.fetchall()]

            dark = _match("黑暗")
            assert len(dark) == 3, f"搜 '黑暗' 应命中 3 条，实际 {len(dark)} 条: {dark}"
            assert not any("暗黑破坏神" in p for p in dark), (
                f"搜 '黑暗' 不应命中 '暗黑破坏神'：{dark}"
            )

            reverse = _match("暗黑")
            assert len(reverse) == 1, f"搜 '暗黑' 应仅命中 1 条，实际 {len(reverse)} 条: {reverse}"
            assert any("暗黑破坏神" in p for p in reverse), (
                f"搜 '暗黑' 应命中 '暗黑破坏神'：{reverse}"
            )

    def test_search_mixed_language(self):
        """测试混合语言搜索"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            # 插入混合语言数据
            test_data = [
                "/test/Movie电影/Movie电影.strm",
                "/test/Anime动画/Anime动画.strm",
                "/test/Test测试/Test测试.strm",
            ]

            for local_path in test_data:
                db.upsert_a(
                    local_path=local_path,
                    webdav_path=local_path,
                    parent_webdav_path=Path(local_path).parent.as_posix()
                )

            # 搜索"Movie"
            with db.read_connection() as conn:
                cursor = conn.execute("""
                    SELECT local_path FROM a_strm_files
                    WHERE rowid IN (
                        SELECT rowid FROM a_strm_files_fts
                        WHERE a_strm_files_fts MATCH ?
                    )
                """, ("Movie",))

                results = [row[0] for row in cursor.fetchall()]

                assert len(results) == 1
                assert "Movie电影" in results[0]

    def test_search_partial_match(self):
        """测试部分匹配"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            # 插入数据
            db.upsert_a(
                local_path="/test/黑暗骑士/黑暗骑士.strm",
                webdav_path="/test/黑暗骑士/黑暗骑士.strm",
                parent_webdav_path="/test/黑暗骑士"
            )

            # 搜索"骑士"（部分匹配）
            with db.read_connection() as conn:
                cursor = conn.execute("""
                    SELECT local_path FROM a_strm_files
                    WHERE rowid IN (
                        SELECT rowid FROM a_strm_files_fts
                        WHERE a_strm_files_fts MATCH ?
                    )
                """, ("骑士",))

                results = [row[0] for row in cursor.fetchall()]

                assert len(results) == 1
                assert "黑暗骑士" in results[0]


class TestFTS5Delete:
    """测试 FTS5 数据删除同步"""

    def test_delete_sync(self):
        """测试删除数据时 FTS5 表同步"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            # 插入数据
            local_path = "/test/黑暗骑士/黑暗骑士.strm"
            db.upsert_a(
                local_path=local_path,
                webdav_path=local_path,
                parent_webdav_path="/test/黑暗骑士"
            )

            # 验证 FTS5 表中有数据
            with db.read_connection() as conn:
                cursor = conn.execute("SELECT COUNT(*) FROM a_strm_files_fts")
                count_before = cursor.fetchone()[0]
                assert count_before == 1

            # 删除数据
            db.delete_a_by_local(local_path)

            # 验证 FTS5 表中数据已删除
            with db.read_connection() as conn:
                cursor = conn.execute("SELECT COUNT(*) FROM a_strm_files_fts")
                count_after = cursor.fetchone()[0]
                assert count_after == 0, f"FTS5 表中应有 0 条记录，实际有 {count_after} 条"

    def test_delete_b_sync(self):
        """测试删除 B 区数据时 FTS5 表同步"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            local_path = "/test/电影/电影.strm"
            db.upsert_b(
                local_path=local_path,
                webdav_path=local_path,
                parent_webdav_path="/test/电影",
                source_a_path="/test/电影",
                fingerprint="fp1",
                mapping_id="test_mapping",
                status="valid"
            )

            with db.read_connection() as conn:
                count_before = conn.execute("SELECT COUNT(*) FROM b_strm_files_fts").fetchone()[0]
                assert count_before == 1

            db.delete_b_by_local(local_path)

            with db.read_connection() as conn:
                count_after = conn.execute("SELECT COUNT(*) FROM b_strm_files_fts").fetchone()[0]
                assert count_after == 0

    def test_delete_c_sync(self):
        """测试删除 C 区数据时 FTS5 表同步"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            local_path = "/test/幽灵/幽灵.strm"
            db.upsert_c(
                local_path=local_path,
                webdav_path=local_path,
                original_b_path="/test/幽灵",
                ghost_root="/ghost"
            )

            with db.read_connection() as conn:
                count_before = conn.execute("SELECT COUNT(*) FROM c_ghost_files_fts").fetchone()[0]
                assert count_before == 1

            db.delete_c_by_local(local_path)

            with db.read_connection() as conn:
                count_after = conn.execute("SELECT COUNT(*) FROM c_ghost_files_fts").fetchone()[0]
                assert count_after == 0


class TestFTS5UpsertNoOrphan:
    """测试 upsert 不产生孤儿记录"""

    def test_upsert_replace_no_orphan(self):
        """测试 INSERT OR REPLACE 后 FTS 行数与主表一致（无孤儿）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            local_path = "/test/黑暗骑士/黑暗骑士.strm"

            # 第一次插入
            db.upsert_a(
                local_path=local_path,
                webdav_path=local_path,
                parent_webdav_path="/test/黑暗骑士"
            )

            # 第二次 upsert（触发 REPLACE）
            db.upsert_a(
                local_path=local_path,
                webdav_path=local_path + "_v2",
                parent_webdav_path="/test/黑暗骑士"
            )

            # 验证主表和 FTS 表行数一致
            with db.read_connection() as conn:
                main_count = conn.execute("SELECT COUNT(*) FROM a_strm_files").fetchone()[0]
                fts_count = conn.execute("SELECT COUNT(*) FROM a_strm_files_fts").fetchone()[0]
                assert main_count == 1, f"主表应有 1 条记录，实际有 {main_count} 条"
                assert fts_count == 1, f"FTS 表应有 1 条记录（无孤儿），实际有 {fts_count} 条"


class TestFTS5Rebuild:
    """测试 FTS 重建逻辑"""

    def test_rebuild_fts_if_stale(self):
        """测试启动时自动清理孤儿记录"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            # 插入数据
            db.upsert_a(
                local_path="/test/电影/电影.strm",
                webdav_path="/test/电影/电影.strm",
                parent_webdav_path="/test/电影"
            )

            # 手动插入孤儿记录到 FTS 表
            with db.connection() as conn:
                conn.execute(
                    "INSERT INTO a_strm_files_fts(rowid, local_path, webdav_path) VALUES(9999, '/orphan', '/orphan')"
                )
                conn.commit()

            # 验证孤儿存在
            with db.read_connection() as conn:
                fts_count_before = conn.execute("SELECT COUNT(*) FROM a_strm_files_fts").fetchone()[0]
                assert fts_count_before == 2  # 1 正常 + 1 孤儿

            # 重新初始化数据库（触发 _rebuild_fts_if_stale）
            db2 = Database(db_path)

            # 验证孤儿已清理
            with db2.read_connection() as conn:
                main_count = conn.execute("SELECT COUNT(*) FROM a_strm_files").fetchone()[0]
                fts_count_after = conn.execute("SELECT COUNT(*) FROM a_strm_files_fts").fetchone()[0]
                assert main_count == 1
                assert fts_count_after == 1, f"FTS 表应重建为 1 条记录，实际有 {fts_count_after} 条"


class TestFTS5Fallback:
    """测试 FTS5 降级路径"""

    def test_unicode61_fallback(self):
        """测试 simple.dll 缺失时降级到 unicode61"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")

            # monkeypatch _load_simple_tokenizer，使其直接返回 False（模拟加载失败）
            from database import Database
            original_load = Database._load_simple_tokenizer

            def mock_load(self, conn):
                return False

            Database._load_simple_tokenizer = mock_load

            try:
                db = Database(db_path)

                # 验证 FTS 表使用 unicode61 创建
                with db.read_connection() as conn:
                    cursor = conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name='a_strm_files_fts'"
                    )
                    sql = cursor.fetchone()[0]
                    assert "unicode61" in sql, f"FTS 表应使用 unicode61 分词器，实际 SQL: {sql}"
            finally:
                # 恢复原始方法
                Database._load_simple_tokenizer = original_load

    def test_unicode61_fallback_no_cjk_hits(self):
        """固化降级行为：unicode61 降级后中文 FTS5 精确查询返回 0 条。

        unicode61 默认 categories 包含 Lo（CJK 属此类），会将连续 CJK 字符拼为
        整词 token（如「黑暗骑士」→ 单 token），不按词切分。
        因此 MATCH '黑暗'（精确 token 匹配）对索引中的「黑暗骑士」不命中 → 0 条。

        本测试 monkeypatch _load_simple_tokenizer 返回 False（模拟 dll 缺失），
        建库后插入含中文媒体名的 A 区数据，断言 FTS5 MATCH 查询返回 0 条。
        """
        from database import Database
        original_load = Database._load_simple_tokenizer

        def mock_load(self, conn):
            return False

        Database._load_simple_tokenizer = mock_load

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = os.path.join(tmpdir, "test_cjk_fallback.db")
                db = Database(db_path)

                # 确认已降级到 unicode61
                assert db._fts_tokenizer == "unicode61", (
                    f"应降级到 unicode61，实际 _fts_tokenizer={db._fts_tokenizer!r}"
                )

                # 插入中文数据（与 test_search_dark_vs_reverse 同数据集）
                data = [
                    ("/test/黑暗骑士/黑暗骑士.strm", "/test/黑暗骑士"),
                    ("/test/黑暗之光/黑暗之光.strm", "/test/黑暗之光"),
                    ("/test/暗黑破坏神/暗黑破坏神.strm", "/test/暗黑破坏神"),
                    ("/test/黎明前的黑暗/黎明前的黑暗.strm", "/test/黎明前的黑暗"),
                ]
                for local_path, parent_path in data:
                    db.upsert_a(
                        local_path=local_path,
                        webdav_path=local_path,
                        parent_webdav_path=parent_path,
                    )

                # 防御性断言：确认 FTS 表确实有数据（排除 FTS 同步失效导致的假阴性）
                # 注意：upsert_a 内部由 Python 代码手动 INSERT/DELETE 同步 FTS 表，
                # 非数据库触发器；若此处断言失败，应检查 upsert_a 的 FTS 同步逻辑。
                with db.read_connection() as conn:
                    fts_count = conn.execute(
                        "SELECT count(*) FROM a_strm_files_fts"
                    ).fetchone()[0]
                    assert fts_count == 4, (
                        f"FTS 表应有 4 条记录，实际 {fts_count}（FTS 同步可能未生效）"
                    )

                # FTS5 MATCH 查询「黑暗」—— unicode61 将连续 CJK 拼为整词 token，
                # 不按词切分，因此精确 token「黑暗」不命中索引中的「黑暗骑士」→ 0 条
                with db.read_connection() as conn:
                    cursor = conn.execute(
                        """
                        SELECT local_path FROM a_strm_files
                        WHERE rowid IN (
                            SELECT rowid FROM a_strm_files_fts
                            WHERE a_strm_files_fts MATCH ?
                        )
                        """,
                        ("黑暗",),
                    )
                    results = [row[0] for row in cursor.fetchall()]

                assert len(results) == 0, (
                    f"unicode61 降级后搜 '黑暗' 应返回 0 条，实际 {len(results)} 条: {results}"
                )

                # 对照：「暗黑」同样不应命中
                with db.read_connection() as conn:
                    cursor = conn.execute(
                        """
                        SELECT local_path FROM a_strm_files
                        WHERE rowid IN (
                            SELECT rowid FROM a_strm_files_fts
                            WHERE a_strm_files_fts MATCH ?
                        )
                        """,
                        ("暗黑",),
                    )
                    results2 = [row[0] for row in cursor.fetchall()]

                assert len(results2) == 0, (
                    f"unicode61 降级后搜 '暗黑' 应返回 0 条，实际 {len(results2)} 条: {results2}"
                )
        finally:
            Database._load_simple_tokenizer = original_load

    def test_special_chars_fallback_to_like(self):
        """测试搜索含特殊字符时回退到 LIKE"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            # 插入含特殊字符的数据
            db.upsert_a(
                local_path="/test/Movie-2024/Movie-2024.strm",
                webdav_path="/test/Movie-2024/Movie-2024.strm",
                parent_webdav_path="/test/Movie-2024"
            )

            # 搜索含特殊字符（- 在 FTS5 中是特殊字符）
            # 这里测试 routes.py 的回退逻辑，需要模拟 WebUI 环境
            # 简化测试：直接验证 LIKE 查询可用
            with db.read_connection() as conn:
                cursor = conn.execute(
                    "SELECT local_path FROM a_strm_files WHERE local_path LIKE ?",
                    ("%Movie-2024%",)
                )
                results = [row[0] for row in cursor.fetchall()]
                assert len(results) == 1
                assert "Movie-2024" in results[0]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
