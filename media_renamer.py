"""媒体文件智能重命名模块"""

import re
import logging
from pathlib import Path

log = logging.getLogger(__name__)


# 季集提取正则
SEASON_EPISODE_PATTERNS = [
    # S01E01, s01e01, S1E1
    (re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})"), "S{season:02d}E{episode:02d}"),
    # 1x01, 01x21
    (re.compile(r"(\d{1,2})[xX](\d{1,2})"), "S{season:02d}E{episode:02d}"),
    # 第1季第1集, 第一季第一集, 第01集
    (re.compile(r"第[一二三四五六七八九十\d]+季"), None),  # 仅标记有季信息
    (re.compile(r"第[一二三四五六七八九十\d]+集"), None),  # 仅标记有集信息
]

# 中文数字映射
CN_NUMBERS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12,
}


def _cn_to_int(s: str) -> int | None:
    """将中文数字转换为整数"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    # 简单处理 "十" 开头的情况
    if s.startswith("十"):
        if len(s) == 1:
            return 10
        rest = s[1:]
        return 10 + (_cn_to_int(rest) or 0)
    # 处理 "X十" 的情况
    if "十" in s:
        parts = s.split("十")
        if len(parts) == 2:
            left = _cn_to_int(parts[0]) or 0
            right = _cn_to_int(parts[1]) or 0
            return left * 10 + right
    return CN_NUMBERS.get(s)


def _extract_season_episode(filename: str) -> tuple[int | None, int | None]:
    """从文件名中提取季和集数"""
    # 优先匹配 S01E01 格式
    for pattern, _ in SEASON_EPISODE_PATTERNS[:2]:
        match = pattern.search(filename)
        if match:
            season = int(match.group(1))
            episode = int(match.group(2))
            return season, episode

    # 匹配中文格式：第X季第Y集
    season_match = re.search(r"第([一二三四五六七八九十\d]+)季", filename)
    episode_match = re.search(r"第([一二三四五六七八九十\d]+)集", filename)

    season = None
    episode = None

    if season_match:
        season = _cn_to_int(season_match.group(1))
    if episode_match:
        episode = _cn_to_int(episode_match.group(1))

    # 如果只匹配到集，没有季，默认第一季
    if episode is not None and season is None:
        season = 1

    return season, episode


def _build_standard_name(season: int, episode: int) -> str:
    """构建标准命名 S01E01"""
    return f"S{season:02d}E{episode:02d}"


def suggest_rename(src_path: str | Path) -> str | None:
    """
    建议新的标准文件名。如果文件名已经是标准格式或无法解析，返回 None。

    返回建议的新文件名（不含路径），不需要重命名时返回 None。
    """
    path = Path(src_path)
    filename = path.stem  # 不含扩展名
    ext = path.suffix  # 包含点，如 .strm

    # 已经是标准格式？返回原文件名，用于触发 Season 路径构建
    if re.match(r"^[Ss]\d{2}[Ee]\d{2}$", filename):
        return filename + ext

    season, episode = _extract_season_episode(filename)
    if season is not None and episode is not None:
        # ===== 修复：只返回集信息，不包含季信息 =====
        # 文件名如 "第01集.mp4" 不包含季信息，_extract_season_episode 会默认 season=1
        # 但实际季信息在 webdav_path 的目录结构中（如 "第二季"）
        # 所以这里只返回 E01 格式，让调用方决定季信息
        new_name = f"E{episode:02d}{ext}"
        return new_name

    return None


def build_season_path(
    base_dir: str | Path,
    show_name: str,
    season: int,
    filename: str,
) -> Path:
    """构建 Season XX 路径"""
    base = Path(base_dir)
    season_dir = f"Season {season:02d}"
    return base / show_name / season_dir / filename
