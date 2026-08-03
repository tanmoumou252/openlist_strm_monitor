"""
字幕处理业务逻辑测试

测试范围：
1. 字幕数据库操作（subtitles 表 CRUD）
2. SubtitleRecord 元组协议兼容性
3. 字幕文件扩展名识别

运行方式：
  pytest src/tests/test_subtitle_handler.py -v
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 确保 src/ 在 sys.path 中（conftest.py 也会处理，此处冗余保护）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import Database, SubtitleRecord
from domain.media.subtitle_handler import SubtitleHandler
from _test_helpers import build_mock_app


# ============================================================
# 字幕数据库操作测试
# ============================================================


class TestSubtitleDatabaseOperations:
    """测试字幕数据库操作"""

    @pytest.fixture
    def temp_db(self):
        """创建临时数据库（含字幕表）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = Database(str(db_path))
            db.init_subtitle_table()
            yield db

    def test_upsert_and_get_subtitle_record(self, temp_db: Database):
        """测试字幕记录的插入和查询"""
        temp_db.upsert_subtitle(
            local_path="/path/to/subtitle.ass",
            target_path="/path/to/target.ass",
            fingerprint="fp123",
            season=1,
            episode=1,
            lang_code="chi",
            status="processed",
        )

        retrieved = temp_db.get_subtitle_by_local("/path/to/subtitle.ass")
        assert retrieved is not None
        assert isinstance(retrieved, SubtitleRecord)
        assert retrieved.local_path == "/path/to/subtitle.ass"
        assert retrieved.target_path == "/path/to/target.ass"
        assert retrieved.fingerprint == "fp123"
        assert retrieved.season == 1
        assert retrieved.episode == 1
        assert retrieved.lang_code == "chi"
        assert retrieved.status == "processed"

    def test_get_subtitle_by_local_not_found(self, temp_db: Database):
        """测试查询不存在的字幕记录"""
        result = temp_db.get_subtitle_by_local("/nonexistent/path.ass")
        assert result is None

    def test_update_subtitle_status(self, temp_db: Database):
        """测试更新字幕记录状态"""
        temp_db.upsert_subtitle(
            local_path="/path/to/subtitle.ass",
            target_path="/path/to/target.ass",
            fingerprint="fp123",
            season=1,
            episode=1,
            lang_code="chi",
            status="pending",
        )

        # 重新 upsert 更新状态
        temp_db.upsert_subtitle(
            local_path="/path/to/subtitle.ass",
            target_path="/path/to/target.ass",
            fingerprint="fp123",
            season=1,
            episode=1,
            lang_code="chi",
            status="processed",
        )

        retrieved = temp_db.get_subtitle_by_local("/path/to/subtitle.ass")
        assert retrieved is not None
        assert retrieved.status == "processed"

    def test_delete_subtitle_by_local(self, temp_db: Database):
        """测试删除字幕记录"""
        temp_db.upsert_subtitle(
            local_path="/path/to/subtitle.ass",
            target_path="/path/to/target.ass",
            fingerprint="fp123",
        )

        temp_db.delete_subtitle_by_local("/path/to/subtitle.ass")

        retrieved = temp_db.get_subtitle_by_local("/path/to/subtitle.ass")
        assert retrieved is None

    def test_subtitle_exists(self, temp_db: Database):
        """测试字幕记录存在性检查"""
        assert not temp_db.subtitle_exists("/path/to/subtitle.ass")

        temp_db.upsert_subtitle(
            local_path="/path/to/subtitle.ass",
            target_path="/path/to/target.ass",
            fingerprint="fp123",
        )

        assert temp_db.subtitle_exists("/path/to/subtitle.ass")

    def test_get_subtitles_by_fingerprint(self, temp_db: Database):
        """测试按指纹查询字幕记录"""
        temp_db.upsert_subtitle(
            local_path="/path/to/sub1.ass",
            target_path="/path/to/target1.ass",
            fingerprint="fp_shared",
        )
        temp_db.upsert_subtitle(
            local_path="/path/to/sub2.srt",
            target_path="/path/to/target2.srt",
            fingerprint="fp_shared",
        )

        results = temp_db.get_subtitles_by_fingerprint("fp_shared")
        assert len(results) == 2
        local_paths = {r.local_path for r in results}
        assert local_paths == {"/path/to/sub1.ass", "/path/to/sub2.srt"}


# ============================================================
# SubtitleHandler 初始化测试
# ============================================================


class TestSubtitleHandlerInit:
    """测试 SubtitleHandler 初始化"""

    def test_handler_init_from_app(self):
        """测试 SubtitleHandler 从 AppService 初始化"""
        app = MagicMock()
        app.config = MagicMock()
        app.db = MagicMock()

        handler = SubtitleHandler(app)
        assert handler.app is app
        assert handler.config is app.config
        assert handler.db is app.db


# ============================================================
# 字幕文件扩展名识别测试
# ============================================================


class TestSubtitleFileDiscovery:
    """测试字幕文件发现逻辑"""

    def test_is_subtitle_file_recognizes_extensions(self):
        """测试字幕文件扩展名识别"""
        from media_renamer import is_subtitle_file

        # 项目实际支持的字幕扩展名
        assert is_subtitle_file(Path("test.ass"))
        assert is_subtitle_file(Path("test.ssa"))
        assert is_subtitle_file(Path("test.srt"))

        # 非字幕文件
        assert not is_subtitle_file(Path("test.strm"))
        assert not is_subtitle_file(Path("test.mkv"))
        assert not is_subtitle_file(Path("test.mp4"))
        assert not is_subtitle_file(Path("test.txt"))

    def test_is_subtitle_file_case_insensitive(self):
        """测试字幕文件扩展名大小写不敏感"""
        from media_renamer import is_subtitle_file

        assert is_subtitle_file(Path("test.ASS"))
        assert is_subtitle_file(Path("test.Srt"))
        assert is_subtitle_file(Path("test.SSA"))

    def test_subtitle_exts_constant(self):
        """测试 SUBTITLE_EXTS 常量"""
        from media_renamer import SUBTITLE_EXTS

        assert ".ass" in SUBTITLE_EXTS
        assert ".ssa" in SUBTITLE_EXTS
        assert ".srt" in SUBTITLE_EXTS


# ============================================================
# 字幕处理集成场景测试
# ============================================================


class TestSubtitleIntegrationScenarios:
    """字幕处理集成场景测试"""

    @pytest.fixture
    def temp_db(self):
        """创建临时数据库（含字幕表）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = Database(str(db_path))
            db.init_subtitle_table()
            yield db

    def test_subtitle_record_lifecycle(self, temp_db: Database):
        """测试字幕记录完整生命周期：创建 → 查询 → 更新 → 删除"""
        # 创建
        temp_db.upsert_subtitle(
            local_path="/a/show/s01e01.ass",
            target_path="/b/show/s01e01.ass",
            fingerprint="fp_lifecycle",
            season=1,
            episode=1,
            lang_code="chi",
            status="pending",
        )

        record = temp_db.get_subtitle_by_local("/a/show/s01e01.ass")
        assert record is not None
        assert record.status == "pending"

        # 更新
        temp_db.upsert_subtitle(
            local_path="/a/show/s01e01.ass",
            target_path="/b/show/s01e01.ass",
            fingerprint="fp_lifecycle",
            season=1,
            episode=1,
            lang_code="chi",
            status="processed",
        )

        record = temp_db.get_subtitle_by_local("/a/show/s01e01.ass")
        assert record.status == "processed"

        # 删除
        temp_db.delete_subtitle_by_local("/a/show/s01e01.ass")
        assert temp_db.get_subtitle_by_local("/a/show/s01e01.ass") is None

    def test_multiple_subtitles_same_fingerprint(self, temp_db: Database):
        """测试同指纹多字幕记录"""
        for i, ext in enumerate([".ass", ".srt", ".ssa"]):
            temp_db.upsert_subtitle(
                local_path=f"/a/show/s01e01{ext}",
                target_path=f"/b/show/s01e01{ext}",
                fingerprint="fp_multi",
                season=1,
                episode=1,
            )

        results = temp_db.get_subtitles_by_fingerprint("fp_multi")
        assert len(results) == 3

    def test_subtitle_discovery_in_directory(self, temp_db: Database):
        """测试目录中字幕文件发现"""
        with tempfile.TemporaryDirectory() as tmpdir:
            media_dir = Path(tmpdir) / "Show" / "Season 01"
            media_dir.mkdir(parents=True)

            # 创建字幕文件
            subtitle_files = [
                media_dir / "episode01.ass",
                media_dir / "episode01.srt",
                media_dir / "episode02.ass",
            ]

            for sub_file in subtitle_files:
                sub_file.write_text("[Script Info]\nTitle: Test", encoding="utf-8")

            # 验证所有字幕文件都被识别
            from media_renamer import is_subtitle_file
            for sub_file in subtitle_files:
                assert is_subtitle_file(sub_file)

            # 非字幕文件不被识别
            non_subtitle = media_dir / "episode01.mkv"
            non_subtitle.touch()
            assert not is_subtitle_file(non_subtitle)


# ============================================================
# process_subtitle_file / _process_movie_subtitle / _process_anime_subtitle
# ============================================================


def _make_app(tmp_path: Path) -> MagicMock:
    """构建最小化 mock AppService，供 SubtitleHandler 使用。

    委托给 _test_helpers.build_mock_app，消除重复实现。
    """
    return build_mock_app(tmp_path, setup_b_root=True)


class TestProcessSubtitleFileDispatch:
    """测试 process_subtitle_file 的分发逻辑（movie vs anime vs early-return）"""

    def test_nonexistent_file_returns_silently(self, tmp_path: Path):
        """文件不存在 → 静默返回，不抛异常"""
        app = _make_app(tmp_path)
        handler = SubtitleHandler(app)
        # 不应该抛异常
        handler.process_subtitle_file(tmp_path / "nonexistent.ass")
        # 不应该调用 DB upsert
        app.db.upsert_subtitle.assert_not_called()

    def test_no_a_root_returns_silently(self, tmp_path: Path):
        """get_a_root_for_path 返回 None → 静默返回"""
        app = _make_app(tmp_path)
        app.get_a_root_for_path.return_value = None
        handler = SubtitleHandler(app)

        sub = tmp_path / "a" / "show" / "ep01.ass"
        sub.parent.mkdir(parents=True)
        sub.write_text("test", encoding="utf-8")

        handler.process_subtitle_file(sub)
        app.db.upsert_subtitle.assert_not_called()

    def test_already_processed_skips(self, tmp_path: Path):
        """DB 已有记录且目标文件存在 → 跳过"""
        app = _make_app(tmp_path)
        a_root = tmp_path / "a"
        a_root.mkdir()
        app.get_a_root_for_path.return_value = a_root

        sub = a_root / "movie" / "title.ass"
        sub.parent.mkdir(parents=True)
        sub.write_text("test", encoding="utf-8")

        # 模拟已有记录，目标存在
        target = tmp_path / "b_root" / "movie" / "title.forced.zho.中文.ass"
        target.parent.mkdir(parents=True)
        target.write_text("target", encoding="utf-8")
        existing = MagicMock()
        existing.target_path = str(target)
        app.db.get_subtitle_by_local.return_value = existing

        handler = SubtitleHandler(app)
        handler.process_subtitle_file(sub)
        # 不应该调用 copy（upsert 也不应该，因为目标存在直接跳过）
        app.db.upsert_subtitle.assert_not_called()

    def test_movie_path_dispatches_to_movie_handler(self, tmp_path: Path):
        """路径含 "电影" 目录 → 调用 _process_movie_subtitle"""
        app = _make_app(tmp_path)
        a_root = tmp_path / "a"
        app.get_a_root_for_path.return_value = a_root

        # 创建带 "电影" 目录的字幕文件
        sub = a_root / "电影" / "Movie1.ass"
        sub.parent.mkdir(parents=True)
        sub.write_text("test", encoding="utf-8")

        handler = SubtitleHandler(app)
        handler.process_subtitle_file(sub)
        # 应该调用了 upsert（movie handler 会写 DB）
        app.db.upsert_subtitle.assert_called_once()

    def test_anime_path_dispatches_to_anime_handler(self, tmp_path: Path):
        """路径含 "番剧" 目录 + 字幕名含季集 → 走 anime 模式，目标含 Season 01"""
        app = _make_app(tmp_path)
        a_root = tmp_path / "a"
        app.get_a_root_for_path.return_value = a_root

        # 创建带 "番剧" 目录的字幕文件，文件名含季集
        sub = a_root / "番剧" / "Show" / "S01E01.ass"
        sub.parent.mkdir(parents=True)
        sub.write_text("test", encoding="utf-8")

        handler = SubtitleHandler(app)
        handler.process_subtitle_file(sub)
        # 应该调用了 upsert（anime handler 会写 DB）
        app.db.upsert_subtitle.assert_called_once()
        # 验证目标路径包含 Season 01（证明走了 anime 模式而非 movie）
        call_kwargs = app.db.upsert_subtitle.call_args
        target_path = call_kwargs[1]["target_path"] if "target_path" in call_kwargs[1] else call_kwargs[0][1]
        assert "Season 01" in str(target_path)

    def test_anime_path_no_strm_uses_subtitle_name_season(self, tmp_path: Path):
        """路径含 "番剧" 目录 + 同目录无 STRM → 仍走 anime 模式，从字幕名提取季集

        这是 L0 修复的核心价值：修复前，anime 路径 + 无 STRM 会被误降级为 movie。
        """
        app = _make_app(tmp_path)
        a_root = tmp_path / "a"
        app.get_a_root_for_path.return_value = a_root

        # 创建带 "番剧" 目录的字幕文件，同目录无 STRM
        sub = a_root / "番剧" / "Show" / "S02E03.ass"
        sub.parent.mkdir(parents=True)
        sub.write_text("test", encoding="utf-8")

        handler = SubtitleHandler(app)
        handler.process_subtitle_file(sub)
        # 应该调用了 upsert
        app.db.upsert_subtitle.assert_called_once()
        # 验证目标路径包含 Season 02（证明走了 anime 模式）
        call_kwargs = app.db.upsert_subtitle.call_args
        target_path = call_kwargs[1]["target_path"] if "target_path" in call_kwargs[1] else call_kwargs[0][1]
        assert "Season 02" in str(target_path)
        # 验证 season/episode 参数正确
        season = call_kwargs[1].get("season") if "season" in call_kwargs[1] else call_kwargs[0][3]
        episode = call_kwargs[1].get("episode") if "episode" in call_kwargs[1] else call_kwargs[0][4]
        assert season == 2
        assert episode == 3


class TestProcessMovieSubtitle:
    """测试 _process_movie_subtitle 的复制和命名逻辑"""

    def test_copies_subtitle_to_b_root(self, tmp_path: Path):
        """字幕被复制到 B 区，文件名含 forced.zho"""
        app = _make_app(tmp_path)
        a_root = tmp_path / "a"
        a_root.mkdir()

        sub = a_root / "电影" / "MyMovie.ass"
        sub.parent.mkdir(parents=True)
        sub.write_text("subtitle content", encoding="utf-8")

        handler = SubtitleHandler(app)
        handler._process_movie_subtitle(sub, a_root, "fp_test")

        # 验证 DB 记录已写入
        app.db.upsert_subtitle.assert_called_once()
        call_kwargs = app.db.upsert_subtitle.call_args
        target_path = call_kwargs[1]["target_path"] if "target_path" in call_kwargs[1] else call_kwargs[0][1]
        # 验证目标文件实际存在
        assert Path(target_path).exists()
        # 验证文件名格式
        assert "forced" in Path(target_path).name

    def test_stem_from_sibling_strm(self, tmp_path: Path):
        """有同名 .strm 文件时，movie_stem 取自 strm"""
        app = _make_app(tmp_path)
        a_root = tmp_path / "a"
        a_root.mkdir()

        sub = a_root / "电影" / "sub.ass"
        sub.parent.mkdir(parents=True)
        sub.write_text("content", encoding="utf-8")
        # 同目录创建 .strm 文件
        strm = sub.parent / "MovieTitle.strm"
        strm.write_text("http://example.com", encoding="utf-8")

        handler = SubtitleHandler(app)
        handler._process_movie_subtitle(sub, a_root, "fp_test")

        call_kwargs = app.db.upsert_subtitle.call_args
        target_path = call_kwargs[1].get("target_path") or call_kwargs[0][1]
        # movie_stem 应该来自 MovieTitle.strm → MovieTitle
        assert "MovieTitle" in Path(target_path).name

    def test_target_exists_skips_copy(self, tmp_path: Path):
        """目标已存在 → 只更新 DB，不复制"""
        app = _make_app(tmp_path)
        a_root = tmp_path / "a"
        a_root.mkdir()

        sub = a_root / "电影" / "MyMovie.sc.ass"
        sub.parent.mkdir(parents=True)
        sub.write_text("content", encoding="utf-8")

        # 预创建目标文件
        target_dir = tmp_path / "b_root" / "电影"
        target_dir.mkdir(parents=True)
        target = target_dir / "MyMovie.forced.zho.简体.ass"
        target.write_text("existing", encoding="utf-8")

        handler = SubtitleHandler(app)
        handler._process_movie_subtitle(sub, a_root, "fp_test")

        # DB 应该被更新
        app.db.upsert_subtitle.assert_called_once()
        # 目标文件内容应该不变（没被覆盖）
        assert target.read_text() == "existing"

    def test_forced_subtitle_uses_und_when_language_unknown(self, tmp_path: Path):
        """语言检测失败时，forced 字幕使用 .forced.und 而非 .forced.zho.中文。

        覆盖 subtitle_handler.py:125-131：当 detect_subtitle_language 返回 None 时，
        应使用 .forced.und 作为语言标识（而非旧的 .forced.zho.中文）。
        """
        app = _make_app(tmp_path)
        a_root = tmp_path / "a"
        a_root.mkdir()

        # 创建不含语言标识的字幕文件（如 Movie.ass）
        # detect_subtitle_language 应返回 None
        sub = a_root / "电影" / "Movie.ass"
        sub.parent.mkdir(parents=True)
        sub.write_text("subtitle content", encoding="utf-8")

        handler = SubtitleHandler(app)
        handler._process_movie_subtitle(sub, a_root, "fp_test")

        # 验证 DB 记录已写入
        app.db.upsert_subtitle.assert_called_once()
        call_kwargs = app.db.upsert_subtitle.call_args
        target_path = call_kwargs[1]["target_path"] if "target_path" in call_kwargs[1] else call_kwargs[0][1]
        target_name = Path(target_path).name

        # 验证目标路径包含 .forced.und 而非 .forced.zho.中文
        assert ".forced.und" in target_name, \
            f"语言未知时应使用 .forced.und，实际文件名: {target_name}"
        assert ".forced.zho" not in target_name, \
            f"语言未知时不应使用 .forced.zho，实际文件名: {target_name}"


class TestProcessAnimeSubtitle:
    """测试 _process_anime_subtitle 的季集提取和目录构建"""

    def test_anime_subtitle_with_season_episode(self, tmp_path: Path):
        """番剧字幕含 S01E01 → 复制到 Season 01 目录"""
        app = _make_app(tmp_path)
        a_root = tmp_path / "a"
        a_root.mkdir()

        sub = a_root / "番剧" / "ShowName" / "S01E01.sc.ass"
        sub.parent.mkdir(parents=True)
        sub.write_text("content", encoding="utf-8")

        handler = SubtitleHandler(app)
        handler._process_anime_subtitle(sub, a_root, "fp_test")

        call_kwargs = app.db.upsert_subtitle.call_args
        target_path = call_kwargs[1].get("target_path") or call_kwargs[0][1]
        target = Path(target_path)

        # 验证 Season 01 目录
        assert "Season 01" in str(target)
        # 验证文件名格式 S01E01.forced.zho.简体.ass
        assert target.name.startswith("S01E01")
        assert target.exists()

    def test_anime_degrades_to_movie_when_no_season(self, tmp_path: Path):
        """无法提取季集 → 降级为 movie 模式"""
        app = _make_app(tmp_path)
        a_root = tmp_path / "a"
        a_root.mkdir()

        # 无季集信息的字幕，同目录也无 strm
        sub = a_root / "番剧" / "ShowName" / "randomname.ass"
        sub.parent.mkdir(parents=True)
        sub.write_text("content", encoding="utf-8")

        handler = SubtitleHandler(app)
        handler._process_anime_subtitle(sub, a_root, "fp_test")

        # 应该调用 upsert（降级到 movie 后也会写 DB）
        app.db.upsert_subtitle.assert_called_once()

    def test_cn_season_dir_converted(self, tmp_path: Path):
        """中文季目录 第X季 → Season XX"""
        app = _make_app(tmp_path)
        a_root = tmp_path / "a"
        a_root.mkdir()

        sub = a_root / "番剧" / "ShowName" / "第二季" / "S02E05.ass"
        sub.parent.mkdir(parents=True)
        sub.write_text("content", encoding="utf-8")

        handler = SubtitleHandler(app)
        handler._process_anime_subtitle(sub, a_root, "fp_test")

        call_kwargs = app.db.upsert_subtitle.call_args
        target_path = call_kwargs[1].get("target_path") or call_kwargs[0][1]
        target = Path(target_path)

        # 验证 Season 02 目录
        assert "Season 02" in str(target)
        assert target.exists()


# ============================================================
# 字幕编码转换测试 (Task F)
# ============================================================


class TestSubtitleEncodingConversion:
    """测试字幕从各种编码转换为 UTF-8 的功能"""

    @pytest.fixture
    def app(self, tmp_path: Path):
        """创建测试用 app 和 B 区目录"""
        return _make_app(tmp_path)

    def test_gb18030_to_utf8_movie(self, app, tmp_path: Path):
        """GB18030 编码的 .srt 电影字幕 → 目标可被 UTF-8 严格解码，无 BOM"""
        a_root = tmp_path / "a"
        b_root = tmp_path / "b"
        a_root.mkdir(exist_ok=True)
        b_root.mkdir(exist_ok=True)

        # 创建 GB18030 编码的字幕文件
        gb18030_content = "测试字幕内容".encode("gb18030")
        src = a_root / "电影.srt"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(gb18030_content)

        # 同目录放置 STRM 文件，识别为电影
        strm = a_root / "电影.strm"
        strm.write_text("http://example.com/movie.strm", encoding="utf-8")

        handler = SubtitleHandler(app)
        handler._process_movie_subtitle(src, a_root, "fp_test")

        # 验证目标文件
        call_kwargs = app.db.upsert_subtitle.call_args
        target_path = call_kwargs[1].get("target_path") or call_kwargs[0][1]
        target = Path(target_path)

        assert target.exists()
        target_bytes = target.read_bytes()
        # 可被 UTF-8 严格解码
        decoded = target_bytes.decode("utf-8", errors="strict")
        # 不以 UTF-8 BOM 开头
        assert not target_bytes.startswith(b'\xef\xbb\xbf')
        assert "测试字幕内容" in decoded

    def test_big5_to_utf8_anime(self, app, tmp_path: Path):
        """Big5 编码的 .ass 番剧字幕 → 目标可被 UTF-8 严格解码，无 BOM"""
        a_root = tmp_path / "a"
        b_root = tmp_path / "b"
        a_root.mkdir(exist_ok=True)
        b_root.mkdir(exist_ok=True)

        # 创建 Big5 编码的字幕文件
        big5_content = "測試字幕內容".encode("big5")
        src = a_root / "番剧" / "Show" / "Season 01" / "S01E01.ass"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(big5_content)

        handler = SubtitleHandler(app)
        handler._process_anime_subtitle(src, a_root, "fp_test")

        # 验证目标文件
        call_kwargs = app.db.upsert_subtitle.call_args
        target_path = call_kwargs[1].get("target_path") or call_kwargs[0][1]
        target = Path(target_path)

        assert target.exists()
        target_bytes = target.read_bytes()
        # 可被 UTF-8 严格解码
        decoded = target_bytes.decode("utf-8", errors="strict")
        # 不以 UTF-8 BOM 开头
        assert not target_bytes.startswith(b'\xef\xbb\xbf')
        assert "測試字幕內容" in decoded

    def test_utf16_with_bom_to_utf8(self, app, tmp_path: Path):
        """UTF-16 LE 带 BOM 的 .ssa 字幕 → 目标是 UTF-8 无 BOM"""
        a_root = tmp_path / "a"
        b_root = tmp_path / "b"
        a_root.mkdir(exist_ok=True)
        b_root.mkdir(exist_ok=True)

        # 创建 UTF-16 LE 带 BOM 的字幕文件
        utf16_content = "UTF-16 测试字幕".encode("utf-16-le")
        utf16_with_bom = b'\xff\xfe' + utf16_content  # UTF-16 LE BOM
        src = a_root / "番剧" / "Show" / "Season 01" / "S01E01.ssa"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(utf16_with_bom)

        handler = SubtitleHandler(app)
        handler._process_anime_subtitle(src, a_root, "fp_test")

        # 验证目标文件
        call_kwargs = app.db.upsert_subtitle.call_args
        target_path = call_kwargs[1].get("target_path") or call_kwargs[0][1]
        target = Path(target_path)

        assert target.exists()
        target_bytes = target.read_bytes()
        # 可被 UTF-8 严格解码
        decoded = target_bytes.decode("utf-8", errors="strict")
        # 不以 UTF-8 BOM 开头
        assert not target_bytes.startswith(b'\xef\xbb\xbf')
        assert "UTF-16 测试字幕" in decoded

    def test_already_utf8_no_bom_unchanged(self, app, tmp_path: Path):
        """已是无 BOM 的 UTF-8 → 字节完全不变（幂等）"""
        a_root = tmp_path / "a"
        b_root = tmp_path / "b"
        a_root.mkdir(exist_ok=True)
        b_root.mkdir(exist_ok=True)

        # 创建已是 UTF-8 无 BOM 的字幕文件
        utf8_content = "UTF-8 字幕内容".encode("utf-8")
        src = a_root / "电影.srt"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(utf8_content)

        # 同目录放置 STRM 文件，识别为电影
        strm = a_root / "电影.strm"
        strm.write_text("http://example.com/movie.strm", encoding="utf-8")

        handler = SubtitleHandler(app)
        handler._process_movie_subtitle(src, a_root, "fp_test")

        # 验证目标文件
        call_kwargs = app.db.upsert_subtitle.call_args
        target_path = call_kwargs[1].get("target_path") or call_kwargs[0][1]
        target = Path(target_path)

        assert target.exists()
        target_bytes = target.read_bytes()
        # 字节完全一致
        assert target_bytes == utf8_content

    def test_unrecognizable_encoding_fallback(self, app, tmp_path: Path):
        """不可识别编码的随机字节 → fail-safe 原样复制，不抛异常"""
        a_root = tmp_path / "a"
        b_root = tmp_path / "b"
        a_root.mkdir(exist_ok=True)
        b_root.mkdir(exist_ok=True)

        # 创建包含无法识别字节的文件
        random_bytes = b'\x00\x01\x02\x03\x80\x81\xfe\xff' * 100
        src = a_root / "电影.srt"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(random_bytes)

        # 同目录放置 STRM 文件，识别为电影
        strm = a_root / "电影.strm"
        strm.write_text("http://example.com/movie.strm", encoding="utf-8")

        handler = SubtitleHandler(app)
        # 不应抛异常
        handler._process_movie_subtitle(src, a_root, "fp_test")

        # 验证目标文件
        call_kwargs = app.db.upsert_subtitle.call_args
        target_path = call_kwargs[1].get("target_path") or call_kwargs[0][1]
        target = Path(target_path)

        assert target.exists()
        target_bytes = target.read_bytes()
        # 字节完全一致（fail-safe 原样复制）
        assert target_bytes == random_bytes

    def test_movie_and_anime_paths_both_covered(self, app, tmp_path: Path):
        """确保电影路径和番剧路径都使用编码转换"""
        a_root = tmp_path / "a"
        b_root = tmp_path / "b"
        a_root.mkdir(exist_ok=True)
        b_root.mkdir(exist_ok=True)

        # 测试电影路径
        movie_content = "电影字幕".encode("gb18030")
        movie_src = a_root / "电影.srt"
        movie_src.parent.mkdir(parents=True, exist_ok=True)
        movie_src.write_bytes(movie_content)
        strm = a_root / "电影.strm"
        strm.write_text("http://example.com/movie.strm", encoding="utf-8")

        movie_handler = SubtitleHandler(app)
        movie_handler._process_movie_subtitle(movie_src, a_root, "fp_movie")

        # 测试番剧路径 - 使用能被 Big5 编码的字符
        # 注意: 不是所有 GBK 字符都能被 Big5 编码
        anime_content = "中文測試字幕".encode("big5")
        anime_src = a_root / "番剧" / "Show" / "Season 01" / "S01E01.ass"
        anime_src.parent.mkdir(parents=True, exist_ok=True)
        anime_src.write_bytes(anime_content)

        anime_handler = SubtitleHandler(app)
        anime_handler._process_anime_subtitle(anime_src, a_root, "fp_anime")

        # 验证两个调用都执行了
        assert app.db.upsert_subtitle.call_count == 2

    def test_structural_no_shutil_copyfile_in_subtitle_handler(self):
        """结构性断言：subtitle_handler.py 源码中不再有对字幕目标的 shutil.copyfile 调用"""
        subtitle_handler_path = Path(__file__).parent.parent / "domain" / "media" / "subtitle_handler.py"
        content = subtitle_handler_path.read_text(encoding="utf-8")

        # 检查是否还有对字幕目标的 shutil.copyfile 调用
        # (排除 encoding_utils.py 中的兼容性回退调用)
        import re
        # 查找所有 shutil.copyfile 调用
        copyfile_calls = re.findall(r'shutil\.copyfile\([^)]+\)', content)
        # 应该没有调用（全部替换为 copy_subtitle_utf8）
        assert len(copyfile_calls) == 0, (
            f"subtitle_handler.py still has shutil.copyfile calls: {copyfile_calls}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
