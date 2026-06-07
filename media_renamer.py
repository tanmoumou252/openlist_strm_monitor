"""媒体文件智能重命名模块"""

import re
import logging
from pathlib import Path
from typing import Generator

log = logging.getLogger(__name__)


# ========== 字幕文件扩展名 ==========
SUBTITLE_EXTS = {".ass", ".ssa", ".srt"}


# ========== 季集提取正则（增强版） ==========

SEASON_EPISODE_PATTERNS = [
    # S01E01, s01e01, S1E1 (最标准)
    (re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})"), "S{season:02d}E{episode:02d}"),
    # 1x01, 01x21
    (re.compile(r"(\d{1,2})[xX](\d{1,2})"), "S{season:02d}E{episode:02d}"),
]

# 中文数字映射
CN_NUMBERS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
}


def _cn_to_int(s: str) -> int | None:
    """将中文数字转换为整数"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    if s.startswith("十"):
        if len(s) == 1:
            return 10
        rest = s[1:]
        return 10 + (_cn_to_int(rest) or 0)
    if "十" in s:
        parts = s.split("十")
        if len(parts) == 2:
            left = _cn_to_int(parts[0]) or 0
            right = _cn_to_int(parts[1]) or 0
            return left * 10 + right
    return CN_NUMBERS.get(s)


def _extract_season_episode(filename: str) -> tuple[int | None, int | None]:
    """从文件名中提取季和集数（增强版）"""
    # 1. 优先匹配 S01E01 格式
    for pattern, _ in SEASON_EPISODE_PATTERNS[:2]:
        match = pattern.search(filename)
        if match:
            season = int(match.group(1))
            episode = int(match.group(2))
            return season, episode

    # 2. 匹配 [S1][01] 或 Season 1 [01] 或 S1-01 等格式
    # 例如: [DBD-Raws][Megami no Cafe Terrace S1][01]...
    s_season_match = re.search(r"[Ss](\d{1,2})", filename)
    if s_season_match:
        season = int(s_season_match.group(1))
        # 找集数：中括号内的数字、第X集、或紧跟的数字
        episode_match = re.search(r"\[(\d{2,3})\](?!.*\[\d{2,3}\])", filename)
        if not episode_match:
            # 尝试匹配 "第X集" 格式
            episode_match = re.search(r"第(\d{1,3})集", filename)
        if not episode_match:
            # 尝试匹配 - 01 - 或 _01_ 格式
            episode_match = re.search(r"[-_\s](\d{2,3})[-_\s]", filename)
        if episode_match:
            episode = int(episode_match.group(1))
            return season, episode

    # 3. 匹配 Season X 后跟集数
    season_match = re.search(r"Season\s*(\d{1,2})", filename, re.IGNORECASE)
    if season_match:
        season = int(season_match.group(1))
        episode_match = re.search(
            r"\[(\d{2,3})\]|第(\d{1,3})集|[-_\s](\d{2,3})[-_\s]", filename)
        if episode_match:
            episode = int(episode_match.group(
                1) or episode_match.group(2) or episode_match.group(3))
            return season, episode

    # 4. 匹配中文格式：第X季第Y集, 第一季第一集
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

    # 5. 尝试纯数字匹配（如 01.mp4, 第01集）
    if season is None and episode is None:
        # 匹配 [01], [001] 格式（前面有S1标记或Season标记的）
        pure_ep_match = re.search(r"(?:\[|第)(\d{2,3})(?:\]|集)", filename)
        if pure_ep_match:
            season = 1
            episode = int(pure_ep_match.group(1))
            return season, episode

    return season, episode


# ========== 字幕语言识别 ==========

# 语言标识映射：(匹配模式, 语言代码, 中文标签, 优先级)
# 优先级数字越小越优先
LANGUAGE_PATTERNS: list[tuple[re.Pattern, str, str, int]] = [
    # 简中 (最高优先级)
    (re.compile(r"\.sc(?:[^a-z]|$)", re.IGNORECASE), "zho", "简体", 1),
    (re.compile(r"\.chs(?:[^a-z]|$)", re.IGNORECASE), "zho", "简体", 1),
    (re.compile(r"\.scjp(?:[^a-z]|$)", re.IGNORECASE), "zho", "简体", 1),  # 简日双语
    (re.compile(r"\.sccht(?:[^a-z]|$)", re.IGNORECASE), "zho", "简体", 1),
    # 繁中
    (re.compile(r"\.tc(?:[^a-z]|$)", re.IGNORECASE), "zho", "繁体", 2),
    (re.compile(r"\.cht(?:[^a-z]|$)", re.IGNORECASE), "zho", "繁体", 2),
    (re.compile(r"\.big5(?:[^a-z]|$)", re.IGNORECASE), "zho", "繁体", 2),
    # 其他中文变体
    (re.compile(r"\.cn(?:[^a-z]|$)", re.IGNORECASE), "zho", "中文", 3),
    (re.compile(r"\.zh(?:[^a-z]|$)", re.IGNORECASE), "zho", "中文", 3),
]

# 内容关键词映射（用于没有后缀标识时）
LANGUAGE_CONTENT_PATTERNS: list[tuple[re.Pattern, str, str, int]] = [
    # 简中关键词
    (re.compile(r"简中|简体|简体中文|简繁|简日", re.IGNORECASE), "zho", "简体", 1),
    # 繁中关键词
    (re.compile(r"繁中|繁体|繁体中文|繁體|cht|big-?5", re.IGNORECASE), "zho", "繁体", 2),
    # 双语
    (re.compile(r"中日|日中|简日|日简|中日双语|日语双字", re.IGNORECASE), "zho", "简体", 1),
    (re.compile(r"中英|英中|中英双语|中英字幕|中英特效|上中下英", re.IGNORECASE), "zho", "中文", 3),
]


def detect_subtitle_language(filename: str) -> tuple[str, str, int] | None:
    """
    检测字幕语言信息。

    返回: (语言代码, 中文标签, 优先级) 或 None
    """
    # 1. 优先检查后缀标识（如 .sc.ass, .tc.ass）
    for pattern, lang_code, cn_label, priority in LANGUAGE_PATTERNS:
        if pattern.search(filename):
            return lang_code, cn_label, priority

    # 2. 检查内容关键词
    for pattern, lang_code, cn_label, priority in LANGUAGE_CONTENT_PATTERNS:
        if pattern.search(filename):
            return lang_code, cn_label, priority

    return None


def is_subtitle_file(path: str | Path) -> bool:
    """判断是否为字幕文件"""
    return Path(path).suffix.lower() in SUBTITLE_EXTS


# ========== 电影/番剧分类 ==========

# 电影目录关键词
MOVIE_DIR_PATTERNS = [
    re.compile(r"电影|movie|film|cinema|片", re.IGNORECASE),
    re.compile(r"国语|粤语|港片|外语片|好莱坞", re.IGNORECASE),
]

# 番剧目录关键词
ANIME_DIR_PATTERNS = [
    re.compile(r"番剧|动漫|动画|anime|cartoon", re.IGNORECASE),
    re.compile(r"show|tv.?series|series|剧集|电视剧", re.IGNORECASE),
    re.compile(r"国漫|日漫|美漫|韩漫", re.IGNORECASE),
]


def detect_media_type_from_path(path: str | Path) -> str | None:
    """
    根据路径判断媒体类型。

    返回: "movie", "anime", 或 None（无法判断）
    """
    p = Path(path)
    # 检查所有父目录
    check_parts = [p.name] + \
        [parent.name for parent in p.parents if parent.name]

    for part in check_parts:
        for pattern in MOVIE_DIR_PATTERNS:
            if pattern.search(part):
                return "movie"
        for pattern in ANIME_DIR_PATTERNS:
            if pattern.search(part):
                return "anime"

    return None


# ========== 标准命名构建 ==========

def _build_standard_name(season: int, episode: int) -> str:
    """构建标准命名 S01E01"""
    return f"S{season:02d}E{episode:02d}"


def suggest_rename(src_path: str | Path) -> str | None:
    """
    建议新的标准文件名。如果文件名已经是标准格式或无法解析，返回 None。
    """
    path = Path(src_path)
    filename = path.stem
    ext = path.suffix

    # 已经是标准格式？
    if re.match(r"^[Ss]\d{2}[Ee]\d{2}$", filename):
        return filename + ext

    season, episode = _extract_season_episode(filename)
    if season is not None and episode is not None:
        # 只返回集信息，不包含季信息（季信息在路径中）
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


# ========== 字幕处理 ==========

def build_subtitle_name(
    base_name: str,  # 如 "S01E01"
    lang_code: str,
    cn_label: str,
    forced: bool = False,
) -> str:
    """构建标准字幕文件名"""
    forced_tag = ".forced" if forced else ""
    return f"{base_name}{forced_tag}.{lang_code}.{cn_label}"


def process_subtitle_group(
    subtitle_files: list[Path],
    episode_info: tuple[int, int],  # (season, episode)
    show_name: str,
) -> list[tuple[Path, str]]:
    """
    处理一组字幕文件，返回 (原文件, 新文件名) 列表。

    规则：
    - 单语种：加 forced
    - 多语种：sc/chs 优先 forced，其他加 forced 但用 zho
    """
    if not subtitle_files:
        return []

    season, episode = episode_info
    base_name = _build_standard_name(season, episode)

    # 分析每个字幕文件的语言
    detected: list[tuple[Path, tuple[str, str, int] | None]] = []
    for sub_file in subtitle_files:
        lang_info = detect_subtitle_language(sub_file.name)
        detected.append((sub_file, lang_info))

    # 统计有效语言检测数量
    valid_detections = [d for d in detected if d[1] is not None]
    has_multiple = len(valid_detections) > 1

    # 按优先级排序
    def sort_key(item: tuple[Path, tuple[str, str, int] | None]) -> tuple:
        _, lang_info = item
        if lang_info is None:
            return (99, 99, "")
        _code, _label, priority = lang_info
        # sc/chs 优先级最高
        is_sc = 0 if "简体" in _label else 1
        return (is_sc, priority, _label)

    detected.sort(key=sort_key)

    result: list[tuple[Path, str]] = []
    used_base_forced = False  # 标记是否已用掉 forced 名额

    for sub_file, lang_info in detected:
        ext = sub_file.suffix.lower()

        if lang_info is None:
            # 无法识别，保持原名但标准化
            new_name = f"{base_name}{ext}"
            result.append((sub_file, new_name))
            continue

        _code, _label, _priority = lang_info

        # 多语种情况下，sc/chs 优先用 forced
        if has_multiple and not used_base_forced and "简体" in _label:
            forced = True
            used_base_forced = True
        elif not has_multiple:
            # 单语种强制加 forced
            forced = True
        else:
            # 其他多语种情况
            forced = True

        new_name = build_subtitle_name(base_name, _code, _label, forced=forced)
        # 确保扩展名正确
        if not new_name.endswith(ext):
            new_name = f"{new_name}{ext}"

        result.append((sub_file, new_name))

    return result


# ========== 电影字幕处理 ==========

def build_movie_subtitle_name(
    movie_stem: str,  # 电影STRM文件名（不含扩展名）
    lang_info: tuple[str, str, int] | None,
) -> str:
    """构建电影字幕文件名"""
    if lang_info:
        _code, _label, _priority = lang_info
        return f"{movie_stem}.forced.{_code}.{_label}"
    return f"{movie_stem}.forced.zho.中文"


# ========== 主入口函数 ==========

def process_media_file(
    src_path: str | Path,
    webdav_path: str | None = None,
    media_type: str | None = None,
) -> dict | None:
    """
    处理媒体文件，返回处理结果。

    返回: {
        "type": "anime" | "movie",
        "season": int,
        "episode": int,
        "new_name": str,  # 建议的新文件名
        "is_subtitle": bool,
    } 或 None
    """
    path = Path(src_path)

    # 判断是否为字幕
    is_subtitle = is_subtitle_file(path)

    # 尝试提取季集信息
    season, episode = _extract_season_episode(path.name)

    # 如果无法提取，尝试从父目录名提取季信息
    if season is None and episode is None:
        # 检查父目录是否有 Season XX
        for parent in path.parents:
            season_match = re.search(r"[Ss]eason\s*(\d{1,2})", parent.name)
            if season_match:
                season = int(season_match.group(1))
                # 尝试从文件名提取集数
                ep_match = re.search(r"(\d{2,3})", path.name)
                if ep_match:
                    episode = int(ep_match.group(1))
                break

    # 自动检测媒体类型
    if media_type is None:
        media_type = detect_media_type_from_path(path)

    if season is not None and episode is not None:
        # 番剧
        new_name = _build_standard_name(season, episode)
        if is_subtitle:
            # 字幕只返回基础信息，具体命名由调用方处理
            return {
                "type": media_type or "anime",
                "season": season,
                "episode": episode,
                "new_name": new_name,
                "is_subtitle": True,
            }
        else:
            # STRM 文件
            return {
                "type": media_type or "anime",
                "season": season,
                "episode": episode,
                "new_name": f"{new_name}{path.suffix}",
                "is_subtitle": False,
            }

    # 无法识别季集信息，可能是电影
    if media_type == "movie" or (media_type is None and not is_subtitle):
        # 电影保持原名
        return {
            "type": "movie",
            "season": None,
            "episode": None,
            "new_name": path.name,
            "is_subtitle": is_subtitle,
        }

    return None
