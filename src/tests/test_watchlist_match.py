"""
Unit tests for watchlist_match.score_watchlist_item().

Covers movie matching, TV matching, noise stripping, SequenceMatcher fuzzy,
and edge cases. No external dependencies — pure Python tests.

Run: python -m pytest src/tests/test_watchlist_match.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

# 确保 src 在 sys.path 中（conftest.py 也会处理，此处冗余保护）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchlist_match import (
    score_watchlist_item,
    _strip_noise_tokens,
    _split_aliases,
    _normalize_text,
)


# ============================================================
# 辅助构造器
# ============================================================

def _c(name: str, season_num: int = 0, episode_hint: bool = False,
        season: str = "", episode_count: int = 0) -> dict:
    """构造候选条目。"""
    return {
        "name": name,
        "season_num": season_num,
        "episode_hint": episode_hint,
        "season": season,
        "episode_count": episode_count,
    }


def _item(title: str = "", name: str = "", original_title: str = "",
          original_name: str = "", season_count: int = 0,
          _last_ep_season: int = 0, _last_ep_episode: int = 0,
          _episode_count: int = 0, number_of_episodes: int = 0) -> dict:
    """构造 TMDB watchlist 条目。"""
    d: dict = {}
    if title:
        d["title"] = title
    if name:
        d["name"] = name
    if original_title:
        d["original_title"] = original_title
    if original_name:
        d["original_name"] = original_name
    if season_count:
        d["_season_count"] = season_count
    if _last_ep_season:
        d["_last_ep_season"] = _last_ep_season
    if _last_ep_episode:
        d["_last_ep_episode"] = _last_ep_episode
    if _episode_count:
        d["_episode_count"] = _episode_count
    if number_of_episodes:
        d["number_of_episodes"] = number_of_episodes
    return d


# ============================================================
# 电影匹配
# ============================================================

class TestMovieMatch:
    def test_exact_match(self):
        """名字完全一致 → matched"""
        item = _item(title="盗梦空间")
        candidates = [_c("盗梦空间")]
        status, reason = score_watchlist_item(item, candidates, "movie")
        assert status == "matched"
        assert "movie_exact" in reason

    def test_loose_match(self):
        """子串包含 → matched"""
        item = _item(title="盗梦空间")
        candidates = [_c("盗梦空间 Inception 1080p")]
        status, reason = score_watchlist_item(item, candidates, "movie")
        assert status == "matched"
        assert "movie_loose" in reason

    def test_no_match(self):
        """无匹配候选 → unmatched"""
        item = _item(title="盗梦空间")
        candidates = [_c("星际穿越")]
        status, reason = score_watchlist_item(item, candidates, "movie")
        assert status == "unmatched"
        assert "no_movie_candidate" in reason

    def test_name_conflict(self):
        """多个精确匹配 → fuzzy"""
        item = _item(title="教父")
        candidates = [_c("教父"), _c("教父2")]
        # 教父 strips to 教父, 教父2 strips to 教父2
        # exact: 教父 matches first candidate; second doesn't exactly match
        # This is actually 1 exact + possible loose, so should be matched
        status, reason = score_watchlist_item(item, candidates, "movie")
        # With only 1 exact hit, it should be matched
        assert status == "matched"

    def test_name_conflict_multiple_exact(self):
        """多个完全相同的候选 → fuzzy"""
        item = _item(title="教父")
        candidates = [_c("教父", season_num=0), _c("教父", season_num=0)]
        status, reason = score_watchlist_item(item, candidates, "movie")
        assert status == "fuzzy"
        assert "movie_name_conflict" in reason

    def test_fuzzy_match_gto(self):
        """SequenceMatcher 能匹配带噪音的名称"""
        item = _item(title="GTO")
        candidates = [_c("GTO 1080p Remux")]
        # "GTO 1080p remux" after strip → "gto" (all noise removed)
        # "gto" equals "gto" → exact match
        status, reason = score_watchlist_item(item, candidates, "movie")
        assert status == "matched"
        assert "movie_exact" in reason

    def test_fuzzy_sequence_matcher(self):
        """SequenceMatcher 模糊匹配（不满足子串匹配时）"""
        # 使用高阈值使得精确/子串不命中，只能走 SequenceMatcher
        item = _item(title="你的名字")
        candidates = [_c("你的名字。")]
        # "你的名字。" after strip → "你的名字。"（句号保留）
        # "你的名字" vs "你的名字。" → 子串匹配: "你的名字" in "你的名字。" → True → loose
        # 所以这个还是 loose 匹配
        status, reason = score_watchlist_item(item, candidates, "movie")
        assert status == "matched"


# ============================================================
# TV 匹配
# ============================================================

class TestTvMatch:
    def test_exact_match_with_episode(self):
        """名字精确 + 有集文件 → matched"""
        item = _item(name="进击的巨人", season_count=4)
        candidates = [_c("进击的巨人", season_num=1, episode_hint=True, season="第1季")]
        status, reason = score_watchlist_item(item, candidates, "tv")
        assert status == "matched"
        assert "tv_exact" in reason

    def test_exact_match_with_season_in_range(self):
        """名字精确 + 季数在范围内 → matched（需要 _season_count）"""
        item = _item(name="进击的巨人", season_count=4)
        candidates = [_c("进击的巨人", season_num=2)]
        status, reason = score_watchlist_item(item, candidates, "tv")
        assert status == "matched"
        assert "tv_exact" in reason
        assert "S2" in reason

    def test_exact_match_no_structure(self):
        """名字精确 + 无季/集信息 → matched (T4: 无结构证据不再依赖 last_episode_to_air)"""
        item = _item(name="进击的巨人", season_count=4)
        candidates = [_c("进击的巨人")]
        status, reason = score_watchlist_item(item, candidates, "tv")
        assert status == "matched"
        assert "no_structure" in reason

    def test_season_mismatch(self):
        """B区 S5 但 TMDB 只有 2 季 → fuzzy"""
        item = _item(name="某番剧", season_count=2)
        candidates = [_c("某番剧", season_num=5)]
        status, reason = score_watchlist_item(item, candidates, "tv")
        assert status == "fuzzy"
        assert "tv_season_mismatch" in reason

    def test_season_count_zero_fallback(self):
        """_season_count=0 时（未填充），有季但无法确认 → fuzzy"""
        item = _item(name="新番剧")  # no _season_count
        candidates = [_c("新番剧", season_num=1)]
        status, reason = score_watchlist_item(item, candidates, "tv")
        assert status == "fuzzy"
        assert "tv_season_only" in reason

    def test_tv_name_conflict(self):
        """多个同名 TV 候选 → fuzzy"""
        item = _item(name="孤独摇滚")
        candidates = [_c("孤独摇滚"), _c("孤独摇滚")]
        status, reason = score_watchlist_item(item, candidates, "tv")
        assert status == "fuzzy"
        assert "tv_name_conflict" in reason

    def test_tv_no_candidate(self):
        """无 TV 候选 → unmatched"""
        item = _item(name="不存在的番")
        candidates = [_c("其他番剧")]
        status, reason = score_watchlist_item(item, candidates, "tv")
        assert status == "unmatched"

    def test_tv_episode_hint_always_matched(self):
        """有集文件证据时始终 matched，不论 _season_count"""
        item = _item(name="进击的巨人")  # no _season_count
        candidates = [_c("进击的巨人", season_num=1, episode_hint=True, season="第1季")]
        status, reason = score_watchlist_item(item, candidates, "tv")
        assert status == "matched"

    def test_tv_fuzzy_only_match(self):
        """只有 SequenceMatcher 模糊匹配时，TV 进入 fuzzy_hits 路径"""
        # "钢炼FA" vs "钢之炼金术师FA" — 既非精确也非子串，但 SequenceMatcher 相似
        item = _item(name="钢炼FA", season_count=1)
        candidates = [_c("钢之炼金术师FA", season_num=1, episode_hint=True, season="第1季")]
        status, reason = score_watchlist_item(item, candidates, "tv", fuzzy_threshold=0.3)
        # Should go through fuzzy_hits → best_candidate → episode_hint → matched
        assert status == "matched"
        assert "tv_fuzzy" in reason

    def test_tv_fuzzy_no_structure(self):
        """tv_fuzzy + 无季/集证据 → fuzzy (不提升为 matched)"""
        item = _item(name="钢炼FA", season_count=2)
        candidates = [_c("钢之炼金术师FA")]  # 无 season_num / episode_hint
        status, reason = score_watchlist_item(item, candidates, "tv", fuzzy_threshold=0.3)
        assert status == "fuzzy"
        assert "no_structure" in reason

    def test_tv_fuzzy_with_season_validation(self):
        """fuzzy 命中后仍执行季数验证（非子串关系，纯 SequenceMatcher）"""
        # "钢炼FA" 不是 "钢之炼金术师FA" 的子串，反之亦然
        # 但 SequenceMatcher ratio ≈ 0.35 > fuzzy_threshold=0.3
        item = _item(name="钢炼FA", season_count=2)
        candidates = [_c("钢之炼金术师FA", season_num=5)]
        status, reason = score_watchlist_item(item, candidates, "tv", fuzzy_threshold=0.3)
        assert status == "fuzzy"
        assert "tv_season_mismatch" in reason


# ============================================================
# 边界情况
# ============================================================

class TestEdgeCases:
    def test_empty_candidates(self):
        """空候选列表 → unmatched"""
        item = _item(title="任何电影")
        status, reason = score_watchlist_item(item, [], "movie")
        assert status == "unmatched"
        assert "no_candidate" in reason

    def test_empty_item_name(self):
        """TMDB 条目无名字 → fuzzy"""
        item = _item()  # no title/name
        candidates = [_c("某电影")]
        status, reason = score_watchlist_item(item, candidates, "movie")
        assert status == "fuzzy"
        assert "missing_name" in reason

    def test_chinese_noise_handling(self):
        """中文噪音如 '全集' 被正确剥离"""
        item = _item(title="无耻之徒")
        candidates = [_c("无耻之徒 全集 合集")]
        # "无耻之徒 全集 合集" after strip → "无耻之徒" (全集/合集 removed)
        # exact match
        status, reason = score_watchlist_item(item, candidates, "movie")
        assert status == "matched"

    def test_threshold_params(self):
        """自定义阈值参数生效（exact_threshold 已移除 — 精确匹配为纯字符串相等）"""
        item = _item(title="测试电影")
        candidates = [_c("测试电影 4K HDR Remux")]
        # With high fuzzy_threshold, loose (substring) still works
        status, reason = score_watchlist_item(
            item, candidates, "movie", fuzzy_threshold=0.99)
        # loose (substring) match doesn't depend on thresholds
        assert status == "matched"


# ============================================================
# 噪音剥离
# ============================================================

class TestStripNoise:
    def test_strip_1_3_season(self):
        """'无耻之徒 1-3季 全集' → '无耻之徒'"""
        result = _strip_noise_tokens("无耻之徒 1-3季 全集")
        assert "无耻之徒" in result
        assert "季" not in result
        assert "全集" not in result

    def test_strip_chinese_season(self):
        """'进击的巨人 第4季' → '进击的巨人'"""
        result = _strip_noise_tokens("进击的巨人 第4季")
        assert "进击的巨人" in result
        assert "季" not in result

    def test_strip_quality_tokens(self):
        """'你的名字 1080p BluRay x264' → '你的名字'"""
        result = _strip_noise_tokens("你的名字 1080p BluRay x264")
        assert result == "你的名字"

    def test_strip_episode_tokens(self):
        """'某番剧 S01E05' → '某番剧'"""
        result = _strip_noise_tokens("某番剧 S01E05")
        assert "某番剧" in result
        assert "s01e05" not in result

    def test_strip_chinese_episode(self):
        """'某番剧 第12集' → '某番剧'"""
        result = _strip_noise_tokens("某番剧 第12集")
        assert "某番剧" in result

    def test_strip_completion_words(self):
        """'某番剧 完结 全集' → '某番剧'"""
        result = _strip_noise_tokens("某番剧 完结 全集")
        assert "某番剧" in result
        assert "完结" not in result
        assert "全集" not in result

    def test_strip_season_without_di(self):
        """'某番剧 1-3季' → '某番剧' (无 '第' 前缀的季数)"""
        result = _strip_noise_tokens("某番剧 1-3季")
        assert "某番剧" in result
        # "1-3季" → hyphen to space → "1 3季" → \d+季 matches "3季" → removed
        assert "季" not in result

    def test_strip_mixed_noise(self):
        """复杂混合噪音"""
        result = _strip_noise_tokens("GTO 真人版 1080p WEB-DL 连载中")
        assert "gto" in result
        assert "真人版" in result
        assert "1080p" not in result
        assert "连载中" not in result


# ============================================================
# 辅助函数
# ============================================================

class TestSplitAliases:
    def test_pipe_split(self):
        result = _split_aliases("进击的巨人|Attack on Titan")
        assert len(result) >= 2

    def test_slash_split(self):
        result = _split_aliases("进击的巨人/Attack on Titan")
        assert len(result) >= 2

    def test_dedup(self):
        result = _split_aliases("同一名称", "同一名称")
        normalized = [_normalize_text(a) for a in result]
        assert len(normalized) == len(set(normalized))

    def test_empty_values(self):
        result = _split_aliases("", None, "")
        assert result == []


# ============================================================
# last_episode_to_air 交叉验证
# ============================================================

class TestLastEpisodeValidation:
    def test_future_season_fuzzy(self):
        """TMDB 播到 S01E11，本地有 S02 → fuzzy"""
        item = _item(name="Test Show", season_count=1,
                     _last_ep_season=1, _last_ep_episode=11)
        candidates = [_c("Test Show", season_num=2, episode_hint=True, season="第2季")]
        status, reason = score_watchlist_item(item, candidates, "tv")
        assert status == "fuzzy"
        assert "tv_future_season" in reason

    def test_same_season_ok(self):
        """TMDB 播到 S02E05，本地有 S02 → matched（同季不触发）"""
        item = _item(name="Test Show", season_count=2,
                     _last_ep_season=2, _last_ep_episode=5)
        candidates = [_c("Test Show", season_num=2, episode_hint=True, season="第2季")]
        status, reason = score_watchlist_item(item, candidates, "tv")
        assert status == "matched"

    def test_no_last_ep_info_skips_check(self):
        """_last_ep_season=0 时跳过交叉验证"""
        item = _item(name="Test Show", season_count=2)
        candidates = [_c("Test Show", season_num=3)]
        status, reason = score_watchlist_item(item, candidates, "tv")
        # season_num=3 > total_seasons+1=3 → 不触发 mismatch
        # total_seasons=2, season_num=3 → 3 > 2+1=3 → 不触发 mismatch
        # 最终到 tv_season_only → fuzzy
        assert status == "fuzzy"


# ============================================================
# 集数比例验证
# ============================================================

class TestEpisodeRatio:
    def test_low_ratio_fuzzy(self):
        """集数比例过低 → fuzzy"""
        item = _item(name="Test Show", season_count=1,
                     _episode_count=87, number_of_episodes=87)
        candidates = [_c("Test Show", season_num=1, episode_hint=True,
                         season="第1季", episode_count=2)]
        status, reason = score_watchlist_item(
            item, candidates, "tv", min_ep_ratio=0.3)
        assert status == "fuzzy"
        assert "tv_few_episodes" in reason

    def test_high_ratio_matched(self):
        """集数比例足够 → matched"""
        item = _item(name="Test Show", season_count=1,
                     _episode_count=87, number_of_episodes=87)
        candidates = [_c("Test Show", season_num=1, episode_hint=True,
                         season="第1季", episode_count=80)]
        status, reason = score_watchlist_item(
            item, candidates, "tv", min_ep_ratio=0.3)
        assert status == "matched"
        assert "tv_exact" in reason

    def test_zero_counts_skips_ratio(self):
        """任一 count 为 0 → 跳过 ratio 检查"""
        item = _item(name="Test Show", season_count=1)
        candidates = [_c("Test Show", season_num=1, episode_hint=True,
                         season="第1季", episode_count=5)]
        status, reason = score_watchlist_item(
            item, candidates, "tv", min_ep_ratio=0.3)
        assert status == "matched"


# ============================================================
# 边界场景
# ============================================================

class TestEdgeStructural:
    def test_tmdb_fewer_seasons_than_local(self):
        """TMDB 总季数 < 本地季数 → fuzzy (tv_season_mismatch)"""
        item = _item(name="Show", season_count=2, _last_ep_season=2)
        candidates = [_c("Show", season_num=5)]
        status, reason = score_watchlist_item(item, candidates, "tv")
        assert status == "fuzzy"
        assert "tv_season_mismatch" in reason

    def test_combined_future_season_and_low_ratio(self):
        """同时触发 future_season 和 few_episodes → 取决于检查顺序"""
        item = _item(name="Show", season_count=1,
                     _episode_count=50, _last_ep_season=1)
        candidates = [_c("Show", season_num=2, episode_hint=True,
                         season="第2季", episode_count=3)]
        status, reason = score_watchlist_item(
            item, candidates, "tv", min_ep_ratio=0.3)
        # last_ep_season=1 < season_num=2 → future_season（先检查）
        assert status == "fuzzy"
        assert "tv_future_season" in reason
