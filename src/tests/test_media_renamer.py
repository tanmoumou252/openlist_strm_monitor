"""
media_renamer.py 单元测试

测试范围（纯函数，无需 mock）：
- _cn_to_int: 中文/阿拉伯数字转换
- extract_season_from_path: 从路径提取季信息
- _extract_season_episode: 从文件名提取季集
- detect_media_type_from_path: 从路径判断媒体类型
- suggest_rename: 建议标准文件名
- is_subtitle_file / detect_subtitle_language: 字幕识别
"""
from __future__ import annotations

import sys
from pathlib import Path

# 确保 src/ 在 sys.path 中（conftest.py 也会处理，此处冗余保护）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_renamer import (
    _cn_to_int,
    extract_season_from_path,
    _extract_season_episode,
    detect_media_type_from_path,
    suggest_rename,
    is_subtitle_file,
    detect_subtitle_language,
    build_season_path,
    build_subtitle_name,
    build_movie_subtitle_name,
    process_subtitle_group,
    process_media_file,
)


# ============================================================
# _cn_to_int
# ============================================================

class TestCnToInt:
    """中文数字 → 整数转换"""

    def test_arabic_digits(self):
        assert _cn_to_int("1") == 1
        assert _cn_to_int("12") == 12
        assert _cn_to_int(" 3 ") == 3

    def test_single_cn(self):
        assert _cn_to_int("一") == 1
        assert _cn_to_int("九") == 9
        assert _cn_to_int("十") == 10

    def test_teens(self):
        assert _cn_to_int("十一") == 11
        assert _cn_to_int("十五") == 15

    def test_tens(self):
        assert _cn_to_int("二十") == 20
        assert _cn_to_int("二十一") == 21
        assert _cn_to_int("三十五") == 35

    def test_unknown_returns_none(self):
        assert _cn_to_int("abc") is None
        assert _cn_to_int("") is None


# ============================================================
# extract_season_from_path
# ============================================================

class TestExtractSeasonFromPath:
    """从路径组件提取季编号"""

    def test_season_dir(self):
        assert extract_season_from_path("/strm/anime/show/Season 01/ep01.strm") == 1
        assert extract_season_from_path("/strm/anime/show/Season 2/ep01.strm") == 2

    def test_sxx_dir(self):
        assert extract_season_from_path("/strm/anime/show/S01/ep01.strm") == 1
        assert extract_season_from_path("/strm/anime/show/s03/ep01.strm") == 3

    def test_cn_season_dir(self):
        assert extract_season_from_path("/strm/anime/show/第一季/ep01.strm") == 1
        assert extract_season_from_path("/strm/anime/show/第三季/ep01.strm") == 3
        assert extract_season_from_path("/strm/anime/show/第十二季/ep01.strm") == 12

    def test_windows_path(self):
        assert extract_season_from_path("C:\\strm\\show\\Season 02\\ep01.strm") == 2

    def test_no_season_returns_none(self):
        assert extract_season_from_path("/strm/anime/show/ep01.strm") is None
        assert extract_season_from_path("/strm/anime/show/Specials/ep01.strm") is None

    def test_pathlib_object(self):
        p = Path("/strm/anime/show/Season 05/ep01.strm")
        assert extract_season_from_path(p) == 5


# ============================================================

# _extract_season_episode
# ============================================================

class TestExtractSeasonEpisode:
    """从文件名提取季和集"""

    def test_standard_sxxexx(self):
        assert _extract_season_episode("S01E01.mkv") == (1, 1)
        assert _extract_season_episode("s02e13.mkv") == (2, 13)
        assert _extract_season_episode("S1E1.mkv") == (1, 1)

    def test_large_episode_numbers_and_terminal_boundary(self):
        assert _extract_season_episode("S02E043.mkv") == (2, 43)
        assert _extract_season_episode("S02E049.mkv") == (2, 49)
        assert _extract_season_episode("S18E760.mkv") == (18, 760)
        assert _extract_season_episode("S21E1088.mp4") == (21, 1088)
        assert _extract_season_episode("S01E9999.mkv") == (1, 9999)
        assert _extract_season_episode("S01E10000.mkv") == (None, None)

    def test_large_episode_numbers_in_x_format(self):
        assert _extract_season_episode("02x43.mkv") == (2, 43)
        assert _extract_season_episode("21x1088.mp4") == (21, 1088)

    def test_large_episode_numbers_in_fallback_formats(self):
        assert _extract_season_episode("Show S01 [9999].mkv") == (1, 9999)
        assert _extract_season_episode("Show Season 01 第9999集.mkv") == (1, 9999)

    def test_x_format(self):
        assert _extract_season_episode("1x01.mkv") == (1, 1)
        assert _extract_season_episode("01x21.mkv") == (1, 21)
        assert _extract_season_episode("3x05.mkv") == (3, 5)

    def test_bracket_season_episode(self):
        """[Show S1][01] 嵌套格式"""
        s, e = _extract_season_episode("[进击的巨人 S1][01].mkv")
        assert s == 1
        assert e == 1

    def test_s_prefix_with_bracket_episode(self):
        s, e = _extract_season_episode("ShowName S01 [05].mkv")
        assert s == 1
        assert e == 5

    def test_s_prefix_with_cn_episode(self):
        s, e = _extract_season_episode("ShowName S02 第5集.mkv")
        assert s == 2
        assert e == 5

    def test_season_keyword(self):
        s, e = _extract_season_episode("MyShow Season 1 [03].mkv")
        assert s == 1
        assert e == 3

    def test_cn_season_episode(self):
        s, e = _extract_season_episode("第二季第5集.mkv")
        assert s == 2
        assert e == 5

    def test_cn_episode_only_defaults_season1(self):
        s, e = _extract_season_episode("第3集.mkv")
        assert s == 1
        assert e == 3

    def test_pure_number_bracket(self):
        s, e = _extract_season_episode("[01].mkv")
        assert s == 1
        assert e == 1

    def test_unparseable_returns_none_none(self):
        s, e = _extract_season_episode("random_video_name.mkv")
        assert s is None
        assert e is None

    def test_partial_season_only_returns_none(self):
        """只有 S01 没有集数 → season/episode 均为 None（无完整匹配）"""
        s, e = _extract_season_episode("ShowName S01.mkv")
        # S01 前缀存在但无集数模式匹配 → 函数第 4 步重置 season=None
        assert s is None
        assert e is None


class TestNoiseTagStripping:
    """噪音标签剥离测试（验证 suggest_rename 的预处理）"""

    def test_suggest_rename_with_noise_tags(self):
        """含噪音标签的文件名应返回 None（无法提取）而非错误解析"""
        # 1920x1080 噪音导致无法提取 → 返回 None
        assert suggest_rename("Penguin Drum - 01 (BD 1920x1080 x.264 FLACx2).strm") is None
        assert suggest_rename("Dynamis_One_..._01_Baha_1920x1080_AVC.strm") is None
        # 合法命名不受影响
        assert suggest_rename("ShowName S01E01.mkv") == "S01E01.mkv"
        assert suggest_rename("ShowName S18E760.mkv") == "S18E760.mkv"
        assert suggest_rename("ShowName 21x1088.mp4") == "S21E1088.mp4"

    def test_strip_noise_tags_function(self):
        """直接测试 _strip_noise_tags 函数"""
        from media_renamer import _strip_noise_tags
        # 分辨率应被剥离
        assert "1920x1080" not in _strip_noise_tags("Penguin Drum - 01 (BD 1920x1080 x.264 FLACx2)")
        assert "1080p" not in _strip_noise_tags("Show.Name.1080p.BluRay.x264")
        # 编码标签应被剥离
        assert "x264" not in _strip_noise_tags("Show.Name.1080p.BluRay.x264")
        assert "FLAC" not in _strip_noise_tags("Show.Name.FLAC.1080p")
        # 年份应被剥离
        assert "2020" not in _strip_noise_tags("Show.Name.2020.1080p")


# ============================================================
# detect_media_type_from_path
# ============================================================

class TestDetectMediaTypeFromPath:
    """从路径判断媒体类型 (movie/anime/None)"""

    def test_movie_dir(self):
        assert detect_media_type_from_path("/strm/电影/Movie1/video.mkv") == "movie"
        assert detect_media_type_from_path("/strm/movies/Film/video.mkv") == "movie"

    def test_anime_dir(self):
        assert detect_media_type_from_path("/strm/番剧/Show/ep01.mkv") == "anime"
        assert detect_media_type_from_path("/strm/anime/Show/ep01.mkv") == "anime"

    def test_no_match_returns_none(self):
        assert detect_media_type_from_path("/random/path/file.mkv") is None

    def test_movie_dir_first_checked_wins(self):
        """同一路径层级中 movie 先检查；不同层级中靠文件最近的层级先匹配。
        /strm/电影/番剧/file.mkv → 番剧（更近文件）先被检查 → anime"""
        result = detect_media_type_from_path("/strm/电影/番剧/file.mkv")
        assert result == "anime"

    def test_movie_closer_to_file(self):
        """/strm/番剧/电影/file.mkv → 电影（更近文件）先被检查 → movie"""
        result = detect_media_type_from_path("/strm/番剧/电影/file.mkv")
        assert result == "movie"

    def test_nested_anime_dir(self):
        """深层目录匹配"""
        result = detect_media_type_from_path("/strm/show/Season 01/番剧/ep01.mkv")
        assert result == "anime"

    def test_windows_path(self):
        assert detect_media_type_from_path("C:\\strm\\电影\\film.mkv") == "movie"


# ============================================================
# suggest_rename
# ============================================================

class TestSuggestRename:
    """建议标准文件名"""

    def test_already_standard(self):
        assert suggest_rename("/path/S01E01.mkv") == "S01E01.mkv"
        assert suggest_rename("/path/s02e13.mkv") == "s02e13.mkv"

    def test_from_sxxexx_stem(self):
        assert suggest_rename("/path/S1E1.mkv") == "S01E01.mkv"

    def test_preserves_standard_large_episode_names_and_boundaries(self):
        assert suggest_rename("/path/S01E01.mkv") == "S01E01.mkv"
        assert suggest_rename("/path/S02E043.mp4") == "S02E043.mp4"
        assert suggest_rename("/path/S21E1088.mkv") == "S21E1088.mkv"
        assert suggest_rename("/path/S01E0001.mkv") == "S01E0001.mkv"
        assert suggest_rename("/path/S01E10000.mkv") is None

    def test_from_x_format(self):
        assert suggest_rename("/path/1x01.mkv") == "S01E01.mkv"

    def test_unparseable_returns_none(self):
        assert suggest_rename("/path/random_name.mkv") is None

    def test_preserves_extension(self):
        assert suggest_rename("/path/2x05.mp4") == "S02E05.mp4"
        assert suggest_rename("/path/S01E12.ass") == "S01E12.ass"

    def test_double_digit_padding(self):
        """季和集都补零到两位"""
        result = suggest_rename("/path/1x1.mkv")
        assert result == "S01E01.mkv"

    def test_pathlib_input(self):
        p = Path("/path/S03E07.mkv")
        assert suggest_rename(p) == "S03E07.mkv"


# ============================================================
# is_subtitle_file
# ============================================================

class TestIsSubtitleFile:
    """字幕文件识别"""

    def test_subtitle_extensions(self):
        assert is_subtitle_file("sub.ass") is True
        assert is_subtitle_file("sub.ssa") is True
        assert is_subtitle_file("sub.srt") is True

    def test_non_subtitle(self):
        assert is_subtitle_file("video.mkv") is False
        assert is_subtitle_file("file.strm") is False
        assert is_subtitle_file("doc.txt") is False

    def test_case_insensitive(self):
        assert is_subtitle_file("sub.ASS") is True
        assert is_subtitle_file("SUB.SRT") is True


# ============================================================
# detect_subtitle_language
# ============================================================

class TestDetectSubtitleLanguage:
    """字幕语言检测"""

    def test_chinese_simplified(self):
        result = detect_subtitle_language("show.zh-Hans.srt")
        assert result is not None
        code, label, priority = result
        assert code in ("chs", "zh-Hans", "sc", "zho")

    def test_chinese_traditional(self):
        result = detect_subtitle_language("show.zh-Hant.srt")
        assert result is not None

    def test_japanese_bilingual(self):
        """简日双语 .scjp 标记"""
        result = detect_subtitle_language("show.scjp.srt")
        assert result is not None
        code, label, priority = result
        assert code == "zho"

    def test_big5_traditional(self):
        result = detect_subtitle_language("show.big5.srt")
        assert result is not None
        code, label, priority = result
        assert label == "繁体"

    def test_content_keyword_simplified(self):
        """内容关键词识别（无后缀标识）"""
        result = detect_subtitle_language("show.简体.srt")
        assert result is not None

    def test_content_keyword_bilingual(self):
        result = detect_subtitle_language("show.中日双语.srt")
        assert result is not None

    def test_no_language_returns_none(self):
        """无语言标记 → 返回 None"""
        result = detect_subtitle_language("show.srt")
        assert result is None


# ============================================================
# build_season_path
# ============================================================

class TestBuildSeasonPath:
    """构建 Season XX 路径"""

    def test_basic_season_path(self, tmp_path):
        result = build_season_path(tmp_path, "Show Name", 1, "episode.strm")
        expected = tmp_path / "Show Name" / "Season 01" / "episode.strm"
        assert result == expected

    def test_double_digit_season(self, tmp_path):
        result = build_season_path(tmp_path, "Anime", 12, "ep01.strm")
        expected = tmp_path / "Anime" / "Season 12" / "ep01.strm"
        assert result == expected

    def test_string_base_dir(self, tmp_path):
        result = build_season_path(str(tmp_path), "Series", 3, "file.mkv")
        expected = tmp_path / "Series" / "Season 03" / "file.mkv"
        assert result == expected

    def test_windows_style_path(self):
        # Windows 路径应该被正确处理
        result = build_season_path("C:\\media", "Show", 1, "ep.strm")
        assert "Season 01" in str(result)
        assert result.name == "ep.strm"


# ============================================================
# build_subtitle_name
# ============================================================

class TestBuildSubtitleName:
    """构建标准字幕文件名"""

    def test_basic_subtitle_name(self):
        result = build_subtitle_name("S01E01", "chs", "简体", forced=False)
        assert result == "S01E01.chs.简体"

    def test_forced_subtitle(self):
        result = build_subtitle_name("S01E01", "chs", "简体", forced=True)
        assert result == "S01E01.forced.chs.简体"

    def test_traditional_chinese(self):
        result = build_subtitle_name("S02E05", "cht", "繁体", forced=False)
        assert result == "S02E05.cht.繁体"

    def test_japanese_bilingual(self):
        result = build_subtitle_name("S01E10", "zho", "简日双语", forced=True)
        assert result == "S01E10.forced.zho.简日双语"


# ============================================================
# build_movie_subtitle_name
# ============================================================

class TestBuildMovieSubtitleName:
    """构建电影字幕文件名"""

    def test_with_lang_info(self):
        lang_info = ("chs", "简体", 1)
        result = build_movie_subtitle_name("Movie.2024", lang_info)
        assert result == "Movie.2024.forced.chs.简体"

    def test_without_lang_info(self):
        result = build_movie_subtitle_name("Movie.2024", None)
        assert result == "Movie.2024.forced.zho.中文"

    def test_traditional_chinese(self):
        lang_info = ("cht", "繁体", 2)
        result = build_movie_subtitle_name("Film.Name", lang_info)
        assert result == "Film.Name.forced.cht.繁体"


# ============================================================
# process_subtitle_group
# ============================================================

class TestProcessSubtitleGroup:
    """处理字幕文件组"""

    def test_empty_list(self):
        result = process_subtitle_group([], (1, 1), "Show")
        assert result == []

    def test_single_subtitle_forced(self, tmp_path):
        """单语种字幕自动加 forced"""
        sub_file = tmp_path / "show.sc.srt"
        sub_file.write_text("subtitle content", encoding="utf-8")

        result = process_subtitle_group([sub_file], (1, 1), "Show")
        assert len(result) == 1
        path, new_name = result[0]
        assert path == sub_file
        assert "forced" in new_name
        assert "S01E01" in new_name

    def test_multiple_subtitles_priority(self, tmp_path):
        """多语种字幕按优先级排序"""
        sub1 = tmp_path / "show.en.srt"
        sub1.write_text("english", encoding="utf-8")
        sub2 = tmp_path / "show.sc.srt"
        sub2.write_text("chinese", encoding="utf-8")

        result = process_subtitle_group([sub1, sub2], (2, 5), "Series")
        assert len(result) == 2
        # 简体中文应该排在前面（优先级高）
        first_path, first_name = result[0]
        assert "简体" in first_name or "chs" in first_name

    def test_unrecognized_language(self, tmp_path):
        """无法识别语言的字幕保持原名"""
        sub_file = tmp_path / "show.unknown.srt"
        sub_file.write_text("content", encoding="utf-8")

        result = process_subtitle_group([sub_file], (1, 3), "Show")
        assert len(result) == 1
        path, new_name = result[0]
        assert "S01E03" in new_name
        assert new_name.endswith(".srt")


# ============================================================
# process_media_file
# ============================================================

class TestProcessMediaFile:
    """处理媒体文件主入口"""

    def test_strm_file_with_season_episode(self, tmp_path):
        """标准 STRM 文件（含季集信息）"""
        strm_file = tmp_path / "Show.S01E05.strm"
        strm_file.write_text("content", encoding="utf-8")

        result = process_media_file(strm_file)
        assert result is not None
        assert result["season"] == 1
        assert result["episode"] == 5
        assert result["is_subtitle"] is False
        assert result["new_name"] == "S01E05.strm"

    def test_subtitle_file(self, tmp_path):
        """字幕文件"""
        sub_file = tmp_path / "Show.S02E10.sc.srt"
        sub_file.write_text("subtitle", encoding="utf-8")

        result = process_media_file(sub_file)
        assert result is not None
        assert result["season"] == 2
        assert result["episode"] == 10
        assert result["is_subtitle"] is True

    def test_movie_file_no_season(self, tmp_path):
        """电影文件（无季集信息，文件名不含数字）"""
        movie_file = tmp_path / "Inception.strm"
        movie_file.write_text("content", encoding="utf-8")

        result = process_media_file(movie_file, media_type="movie")
        assert result is not None
        assert result["type"] == "movie"
        assert result["season"] is None
        assert result["episode"] is None
        assert result["new_name"] == "Inception.strm"

    def test_unrecognizable_non_subtitle_returns_movie(self, tmp_path):
        """无季集信息且非字幕 → 自动归类为 movie"""
        unknown_file = tmp_path / "random.strm"
        unknown_file.write_text("content", encoding="utf-8")

        result = process_media_file(unknown_file)
        # media_type 自动检测为 None，非字幕 → 走 movie fallback
        assert result is not None
        assert result["type"] == "movie"
        assert result["season"] is None

    def test_season_from_parent_directory(self, tmp_path):
        """从父目录提取季信息"""
        season_dir = tmp_path / "Season 03"
        season_dir.mkdir()
        ep_file = season_dir / "episode.05.strm"
        ep_file.write_text("content", encoding="utf-8")

        result = process_media_file(ep_file)
        assert result is not None
        assert result["season"] == 3
        assert result["episode"] == 5

    def test_explicit_anime_type_without_season_returns_none(self, tmp_path):
        """显式指定 anime 类型但无季集信息 → 返回 None"""
        file = tmp_path / "content.strm"
        file.write_text("content", encoding="utf-8")

        # media_type="anime" 不等于 "movie"，且非字幕 → 走 movie fallback 条件不满足 → None
        result = process_media_file(file, media_type="anime")
        assert result is None

    def test_explicit_movie_type_returns_movie_dict(self, tmp_path):
        """显式指定 movie 类型 → 返回 movie dict"""
        file = tmp_path / "content.strm"
        file.write_text("content", encoding="utf-8")

        result = process_media_file(file, media_type="movie")
        assert result is not None
        assert result["type"] == "movie"
        assert result["season"] is None
        assert result["episode"] is None
        assert result["new_name"] == "content.strm"
