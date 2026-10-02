"""
TMDB 待看列表数据模型与 CSV 导出。

保留项：
  - TmdbItem、LastEpisode、MatchResult 三个数据类（被 test_tmdb_api.py 引用）
  - export_watchlist_csv() 输出匹配结果到 CSV 文件
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path


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
