"""
TMDB 待看列表解析与数据库媒体匹配。

支持三种数据源：
  1. HTML 模式 - 解析本地 movie.htm / tv.htm
  2. API 模式 - 通过 TMDB API 获取完整待看列表（推荐）
  3. CSV 模式 - 解析导出的 CSV 文件

匹配策略：
  - 名字弱匹配：归一化后相似度 + 别名扩展
  - 结构强匹配：番剧的季/集数量 + 电影的年份
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import TmdbConfig
    from database import Database

from tmdb_client import TmdbClient


# ============================================================
# 数据模型
# ============================================================

@dataclass
class LastEpisode:
    """last_episode_to_air 数据结构"""
    episode_number: int = 0
    season_number: int = 0
    name: str = ""
    overview: str = ""
    air_date: str = ""
    vote_average: float = 0.0
    runtime: int = 0


@dataclass
class TmdbItem:
    """TMDB 媒体条目"""
    media_type: str          # "movie" or "tv"
    tmdb_id: int = 0
    title: str = ""          # 中文标题
    original_title: str = "" # 原始语言标题
    release_date: str = ""
    poster_url: str = ""
    aliases: set[str] = field(default_factory=set)
    # TV 专用
    total_seasons: int = 0
    total_episodes: int = 0
    last_episode: LastEpisode = field(default_factory=LastEpisode)
    # 原始 TMDB 响应中的 titles
    titles: list[str] = field(default_factory=list)

    @property
    def all_names(self) -> set[str]:
        names = set()
        for n in (self.title, self.original_title):
            n = n.strip()
            if n:
                names.add(n)
        names.update(self.aliases)
        for t in self.titles:
            t = t.strip()
            if t:
                names.add(t)
        return names


@dataclass
class MatchResult:
    """匹配结果"""
    tmdb: TmdbItem
    status: str            # "已收录" / "模糊匹配" / "未收录"
    matched_media: str = "" # 匹配到的数据库媒体名
    score: float = 0.0
    detail: str = ""       # 匹配详情


# ============================================================
# TMDB API 客户端
# ============================================================

class TmdbApiClient(TmdbClient):
    """兼容旧接口的 TMDB 客户端别名。"""





def parse_tmdb_csv(csv_path: str | Path) -> list[TmdbItem]:
    """解析 TMDB 导出的 CSV 待看列表"""
    path = Path(csv_path)
    if not path.exists():
        logging.warning("[TMDB CSV] 文件不存在: %s", path)
        return []

    items: list[TmdbItem] = []
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                title = row.get("Title", "") or row.get("title", "")
                orig = row.get("Original Title", "") or row.get("original_title", "")
                type_str = (row.get("Type", "") or row.get("type", "")).lower()
                year = row.get("Year", "") or row.get("release_date", "")[:4]

                media_type = "movie" if "movie" in type_str else "tv"
                # 尝试从 CSV 中解析 TMDB ID
                tmdb_id = 0
                link = row.get("Link", "") or row.get("link", "")
                m = re.search(r"themoviedb\.org/(?:tv|movie)/(\d+)", link)
                if m:
                    tmdb_id = int(m.group(1))

                items.append(TmdbItem(
                    media_type=media_type,
                    tmdb_id=tmdb_id,
                    title=title,
                    original_title=orig,
                    release_date=year,
                ))
    except Exception:
        logging.exception("[TMDB CSV] 解析失败: %s", path)

    logging.info("[TMDB CSV] 从 %s 解析出 %d 条记录", path, len(items))
    return items


# ============================================================
# 名字匹配引擎
# ============================================================

_SEASON_RE = re.compile(r"(?:^|[\\/._\-\s])(Season\s*\d+|S\d{1,2}|第\s*\d+\s*季)(?:[\\/._\-\s]|$)", re.I)
_EPISODE_RE = re.compile(r"(?:S\d{1,2}E\d{1,3}|第\s*\d+\s*[集话]|EP?\s*\d{1,3})", re.I)


def _normalize(name: str) -> str:
    """名字归一化：去符号、小写、去空白。"""
    s = name.lower().strip()
    s = s.replace("：", ":").replace("！", "!").replace("？", "?")
    s = s.replace("（", "(").replace("）", ")").replace("～", "~")
    s = s.replace("·", " ").replace("・", " ")
    s = re.sub(r"[^\w\s\u4e00-\u9fff]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _similarity(a: str, b: str) -> float:
    """计算两个名字的相似度。"""
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.9
    return SequenceMatcher(None, na, nb).ratio()


def _extract_season_episode_count(strm_files: list[str]) -> tuple[int, int]:
    """从 STRM 文件列表中提取季数和集数。"""
    seasons: set[int] = set()
    episodes_per_season: dict[int, set[int]] = {}

    for f in strm_files:
        stem = Path(f).stem
        # 匹配 S02E05
        m = re.search(r"S(\d{1,2})E(\d{1,3})", stem, re.I)
        if m:
            s, e = int(m.group(1)), int(m.group(2))
            seasons.add(s)
            episodes_per_season.setdefault(s, set()).add(e)
            continue
        # 匹配 第02话 / EP02
        m2 = re.search(r"(?:第|EP?)(\d{1,2})(?:话|集)", stem, re.I)
        if m2:
            e = int(m2.group(1))
            # 尝试从路径推断季
            for s_num in range(1, 10):
                if f"S{s_num:02d}" in f or f"Season{s_num}" in f or f"第{s_num}季" in f:
                    seasons.add(s_num)
                    episodes_per_season.setdefault(s_num, set()).add(e)
                    break

    total_seasons = len(seasons) if seasons else 0
    total_episodes = sum(len(epis) for epis in episodes_per_season.values())
    return total_seasons, total_episodes


# ============================================================
# 核心匹配函数
# ============================================================

def _match_movie(tmdb: TmdbItem, db_movies: set[str],
                 exact_thresh: float, fuzzy_thresh: float) -> MatchResult:
    """匹配电影"""
    best_score = 0.0
    best_match = ""

    for db_name in db_movies:
        for tn in tmdb.all_names:
            score = _similarity(tn, db_name)
            if score > best_score:
                best_score = score
                best_match = db_name

    # 年份辅助验证
    if tmdb.release_date and best_score > 0.3:
        try:
            tmdb_year = int(tmdb.release_date[:4])
            # 在数据库名中搜索年份
            for db_name in db_movies:
                year_match = re.search(r"(\d{4})", db_name)
                if year_match and int(year_match.group(1)) == tmdb_year:
                    best_score = min(best_score + 0.1, 1.0)
                    best_match = db_name
        except (ValueError, IndexError):
            pass

    if best_score >= exact_thresh:
        status = "已收录"
    elif best_score >= fuzzy_thresh:
        status = "模糊匹配"
    else:
        status = "未收录"

    return MatchResult(
        tmdb=tmdb,
        status=status,
        matched_media=best_match,
        score=best_score,
        detail=f"相似度 {best_score:.2f}",
    )


def _match_tv(tmdb: TmdbItem, db_anime: dict[str, list[str]],
              exact_thresh: float, fuzzy_thresh: float,
              min_ep_ratio: float, max_season_diff: int) -> MatchResult:
    """
    匹配番剧。
    db_anime: {anime_name: [strm_file_paths, ...]}
    
    结构匹配策略：
    - 名字相似度（别名/原名/中文名扩展）
    - 季数验证：TMDB 的 total_seasons 或 last_episode.season_number 与 B 区对比
    - 集数验证：TMDB 的 total_episodes 或 last_episode.episode_number 与 B 区对比
    - 特别处理：用户可能只保留了最后几季/集，last_episode_to_air 是关键线索
    """
    best_score = 0.0
    best_match = ""
    best_detail = ""

    # 确定 TMDB 的有效季数和集数
    # 优先使用 last_episode_to_air 提供的实际播放进度
    tmdb_last_season = tmdb.last_episode.season_number if tmdb.last_episode.episode_number > 0 else 0
    tmdb_last_ep = tmdb.last_episode.episode_number if tmdb.last_episode.episode_number > 0 else 0
    
    # 如果 last_episode 有效，用它的 season_number 作为参考
    tmdb_effective_seasons = tmdb_last_season if tmdb_last_season > 0 else tmdb.total_seasons
    tmdb_effective_episodes = tmdb.total_episodes if tmdb.total_episodes > 0 else 0

    for anime_name, strm_files in db_anime.items():
        db_seasons, db_episodes = _extract_season_episode_count(strm_files)

        for tn in tmdb.all_names:
            score = _similarity(tn, anime_name)

            # 结构验证加分
            struct_bonus = 0.0
            
            # 1. 季数验证
            if db_seasons > 0 and tmdb_effective_seasons > 0:
                season_diff = abs(db_seasons - tmdb_effective_seasons)
                if season_diff <= max_season_diff:
                    struct_bonus += 0.15 * (1 - season_diff / (max_season_diff + 1))
            
            # 2. 集数验证
            if db_episodes > 0 and tmdb_effective_episodes > 0:
                ep_ratio = db_episodes / tmdb_effective_episodes
                if ep_ratio >= min_ep_ratio:
                    struct_bonus += 0.2 * min(ep_ratio, 1.0)
            
            # 3. 特殊处理：last_episode_to_air 线索
            # 如果 B 区有 S02E24，而 TMDB 的 last_episode 是 S01E11
            # 说明这部番还没播到第2季，不匹配
            if tmdb_last_season > 0 and db_seasons > 0:
                matched_seasons = [int(re.search(r"S(\d+)", Path(f).stem, re.I).group(1)) 
                                   for f in strm_files if re.search(r"S(\d+)", Path(f).stem, re.I)]
                max_db_season = max(matched_seasons) if matched_seasons else 0
                if max_db_season > 0 and tmdb_last_season < max_db_season:
                    # TMDB 还没播到这个季，不可能收录
                    struct_bonus -= 0.5  # 大幅扣分

            total_score = min(score + struct_bonus, 1.0)

            if total_score > best_score:
                best_score = total_score
                best_match = anime_name
                best_detail = (
                    f"名字相似度 {score:.2f} + 结构加分 {struct_bonus:.2f} = {total_score:.2f}, "
                    f"数据库: {db_seasons}季{db_episodes}集, TMDB: {tmdb_effective_seasons}季{tmdb_effective_episodes}集"
                    f"{f' (last_ep: S{tmdb_last_season}E{tmdb_last_ep})' if tmdb_last_season > 0 else ''}"
                )

    if best_score >= exact_thresh:
        status = "已收录"
    elif best_score >= fuzzy_thresh:
        status = "模糊匹配"
    else:
        status = "未收录"

    return MatchResult(
        tmdb=tmdb,
        status=status,
        matched_media=best_match,
        score=best_score,
        detail=best_detail,
    )


def match_tmdb_watchlist(
    tmdb_items: list[TmdbItem],
    db_records: list[tuple],
    config: TmdbConfig | None = None,
) -> list[MatchResult]:
    """
    将 TMDB 待看列表与数据库媒体集合对齐。

    Args:
        tmdb_items: TMDB 待看条目列表
        db_records: B 区数据库记录 [(local_path, webdav_path, parent_webdav_path, source_a_path, fingerprint, status, updated_at), ...]
        config: TMDB 配置

    Returns:
        MatchResult 列表
    """
    if config is None:
        config = TmdbConfig()

    exact_thresh = config.exact_threshold
    fuzzy_thresh = config.fuzzy_threshold
    min_ep_ratio = config.anime_min_ep_ratio
    max_season_diff = config.anime_max_season_diff

    # 分离数据库中的番剧和电影
    db_movies: set[str] = set()
    db_anime: dict[str, list[str]] = {}  # anime_name -> [strm_paths]

    try:
        from webui import _media_info
        for r in db_records:
            row = {
                "local_path": r[0],
                "webdav_path": r[1],
                "parent_webdav_path": r[2],
                "source_a_path": r[3],
                "fingerprint": r[4],
                "status": r[5],
                "updated_at": r[6],
            }
            kind, name = _media_info(row)
            if kind == "电影" and name:
                db_movies.add(name)
            elif kind == "番剧" and name:
                db_anime.setdefault(name, []).append(r[0])
    except Exception:
        logging.exception("[TMDB] 读取 B 区记录失败")

    results: list[MatchResult] = []

    for item in tmdb_items:
        if item.media_type == "movie":
            result = _match_movie(item, db_movies, exact_thresh, fuzzy_thresh)
        elif item.media_type == "tv":
            result = _match_tv(item, db_anime, exact_thresh, fuzzy_thresh,
                               min_ep_ratio, max_season_diff)
        else:
            result = MatchResult(tmdb=item, status="未收录", score=0.0,
                                 detail=f"未知类型: {item.media_type}")
        results.append(result)

    return results


def get_db_media_groups(db: "Database") -> dict[str, list[str]]:
    """从数据库获取 B 区所有媒体分组。返回 {kind: [media_name, ...]}。"""
    groups: dict[str, set[str]] = {"番剧": set(), "电影": set()}
    try:
        from webui import _media_info
        records = db.get_all_b_records()
        for r in records:
            row = {
                "local_path": r[0], "webdav_path": r[1],
                "parent_webdav_path": r[2], "source_a_path": r[3],
                "fingerprint": r[4], "status": r[5], "updated_at": r[6],
            }
            kind, name = _media_info(row)
            if name:
                groups.setdefault(kind, set()).add(name)
    except Exception:
        logging.exception("[TMDB] 读取 B 区记录失败")
    return {k: sorted(v) for k, v in groups.items()}


def export_watchlist_csv(results: list[MatchResult], output_path: str | Path) -> None:
    """将匹配结果导出为 CSV"""
    # 检查是否有别名数据，动态决定列头
    has_aliases = any(r.tmdb.aliases for r in results)
    header = ["状态", "TMDB ID", "类型", "标题", "原标题", "发布日期"]
    if has_aliases:
        header.append("别名")

    path = Path(output_path)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for r in results:
            row = [
                r.status,
                r.tmdb.tmdb_id,
                r.tmdb.media_type,
                r.tmdb.title,
                r.tmdb.original_title,
                r.tmdb.release_date,
            ]
            if has_aliases:
                aliases_str = "|".join(sorted(r.tmdb.aliases)) if r.tmdb.aliases else ""
                row.append(aliases_str)
            writer.writerow(row)
    logging.info("[TMDB] 导出 CSV: %s (%d 条)", path, len(results))
