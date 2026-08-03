"""
TMDB 待看列表收录状态匹配逻辑（共享模块）。

职责：
  - 收集 B 区媒体快照
  - 将 TMDB 待看条目与 B 区候选进行匹配评分
  - 执行收录状态刷新并回写 tmdb_watchlist.db

被 webui.py 和 standalone_webui.py 共同引用，避免循环依赖。
"""

from __future__ import annotations

import logging
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from database import Database
    from tmdb_watchlist_db import TmdbWatchlistDb

from media_renamer import detect_media_type_from_path


# ============================================================
# Protocol: 兼容 TestWebUIServer / WebUIServer
# ============================================================

class _MatchHost(Protocol):
    _watchlist_db: TmdbWatchlistDb | None
    _db: Database


# ============================================================
# 中文数字
# ============================================================

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
           "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15}


def _cn_to_int(s: str) -> int | None:
    s = s.strip()
    if s.isdigit():
        return int(s)
    if s.startswith("十"):
        if len(s) == 1:
            return 10
        return 10 + (_cn_to_int(s[1:]) or 0)
    if "十" in s:
        parts = s.split("十")
        if len(parts) == 2:
            return (_cn_to_int(parts[0]) or 0) * \
                10 + (_cn_to_int(parts[1]) or 0)
    return _CN_NUM.get(s)


def _extract_season_int(part: str) -> int | None:
    """从目录名/文件名中提取季数（支持中英文）"""
    p = part.lower().strip()
    m = re.match(r"^season\s*(\d{1,2})$", p)
    if m:
        return int(m.group(1))
    m = re.match(r"^s(\d{1,2})$", p)
    if m:
        return int(m.group(1))
    m = re.match(r"^第([一二三四五六七八九十\d]+)季$", p)
    if m:
        return _cn_to_int(m.group(1))
    return None


# ============================================================
# 媒体路径正则（用于 _media_info 分类判断）
# ============================================================

_SEASON_RE = re.compile(
    r"(?:^|[\\/._\-\s])(Season\s*\d+|S\d{1,2}|第[一二三四五六七八九十\d]+季)(?:[\\/._\-\s]|$)",
    re.I)
_EPISODE_RE = re.compile(
    r"(?:S\d{1,2}E\d{1,3}|第\s*\d+\s*[集话]|EP?\s*\d{1,3})", re.I)
_MOVIE_HINT_RE = re.compile(r"电影|movie|movies|film|films|cinema", re.I)


# ============================================================
# 路径 & 分类
# ============================================================

def _path_parts(value: object) -> list[str]:
    return [p for p in str(value or "").replace("\\", "/").split("/") if p]


def _is_category_dir(name: str) -> bool:
    """判断是否是顶级分类目录（番剧/电影等），而不是具体作品名"""
    return name.strip().lower() in {item.lower() for item in [
        "番剧", "动漫", "anime", "动画", "电影", "movie", "movies",
        "电视剧", "tv", "综艺", "纪录片", "documentary", "纪录片",
        "短片", "音乐", "mv",
    ]}


def _media_info(record: dict) -> tuple[str, str]:
    """从记录中推断 media_kind 和 media_name。

    分类直接读取 B 区路径中的分类目录名（电影/番剧等），
    由 media_renamer.detect_media_type_from_path() 完成。
    """
    path = record.get("webdav_path") or record.get("local_path") or ""
    parts = _path_parts(path)

    # 直接从路径分类目录读取：B 区路径已包含"电影"/"番剧"等目录
    detected = detect_media_type_from_path(path)
    if detected == "movie":
        kind = "电影"
    elif detected == "anime":
        kind = "番剧"
    else:
        # 兜底：无分类目录时按季/集正则判断
        has_season = _SEASON_RE.search(path) is not None
        has_episode = _EPISODE_RE.search(path) is not None
        kind = "番剧" if (has_season or has_episode) else "电影"

    media_name = ""
    if kind == "番剧":
        # 寻找季目录，其前面的非分类目录就是番剧名
        for idx, part in enumerate(parts):
            if _extract_season_int(part) is not None and idx > 0:
                parent = parts[idx - 1]
                if not _is_category_dir(parent):
                    media_name = parent
                    break
                if idx >= 2:
                    candidate = parts[idx - 2]
                    if not _is_category_dir(candidate):
                        media_name = candidate
                        break
        # 没有季目录，但有 S01E01 格式的文件名
        if not media_name:
            for idx, part in enumerate(parts):
                if _EPISODE_RE.search(part) and idx > 1:
                    parent = parts[idx - 1]
                    if not _is_category_dir(parent):
                        media_name = parent
                        break
        # 兜底：取文件名的上一级目录
        if not media_name and len(parts) >= 2:
            media_name = parts[-2]
    else:
        # 对于电影，取上一级目录的名称（电影文件夹名），而非 STRM 文件名
        # 例：/b区/电影/a合集/电影1.strm → media_name = "a合集"
        if len(parts) >= 2 and not _is_category_dir(parts[-2]):
            media_name = parts[-2]
        else:
            media_name = Path(parts[-1]).stem if parts else "未分类电影"
    return kind, media_name or (Path(parts[-1]).stem if parts else "未分类")


def _extract_season_from_local_path(local_path: str, allow_filename_fallback: bool = True,
                                    is_anime: bool = True) -> str:
    """从本地路径中提取季信息，返回如 'S01' 或 '第一季' 的字符串

    Args:
        local_path: 本地文件路径
        allow_filename_fallback: 是否允许从文件名提取季信息（SxxExx 格式）。
            - True (默认): 保持原有行为，允许从文件名提取
            - False: 仅从目录名提取，不从文件名提取（用于 movie/other/all kind）
        is_anime: 是否为番剧类型。
            - True (默认): 允许从目录名提取季节（S01/Season 1 等）
            - False: 跳过目录级季节提取（电影路径中的 S01 目录不应产生分组）
    """
    parts = _path_parts(local_path)
    # 目录级季节提取：仅番剧（is_anime=True）时执行
    if is_anime:
        for part in reversed(parts[:-1]):  # 不看文件名本身
            sn = _extract_season_int(part)
            if sn is not None:
                return f"S{sn:02d}"
            # 也检查中文季名
            m = re.match(r"^第([一二三四五六七八九十\d]+)季$", part.strip())
            if m:
                num = _cn_to_int(m.group(1))
                if num:
                    return f"S{num:02d}"
    # 从文件名提取（仅当 allow_filename_fallback=True 时）
    if allow_filename_fallback:
        stem = Path(parts[-1]).stem if parts else ""
        m = re.search(r"S(\d{1,2})E", stem, re.I)
        if m:
            return f"S{int(m.group(1)):02d}"
    return ""


def _compute_media_root(path: str) -> str:
    """计算媒体目录根路径（含媒体目录本身，保留原始分隔符与尾部分隔符）。

    规则：媒体目录是"分类目录（番剧/电影等）"的下一级。
    若找不到分类目录，则退化为第一级目录。
    返回的根路径保留原字符串风格（Windows 反斜杠 / POSIX 斜杠），
    并以分隔符结尾，便于前端直接做前缀剥离。
    """
    if not path:
        return ""
    sep = "\\" if "\\" in path else "/"
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if not parts:
        return ""
    media_idx = None
    for i, p in enumerate(parts):
        if _is_category_dir(p):
            media_idx = i + 1
            break
    if media_idx is None or media_idx >= len(parts):
        media_idx = 1 if not parts[0].endswith(":") else 2
    root_parts = parts[:media_idx + 1] if media_idx < len(parts) else parts
    root = sep.join(root_parts)
    if sep == "/" and path.startswith("/"):
        root = "/" + root
    return root + sep


def _is_top_level_category(kind: str) -> bool:
    return kind.strip().lower() in {"番剧", "动漫",
                                    "anime", "动画", "电影", "movie", "movies"}


def _category_filter_value(kind: str) -> str:
    k = kind.strip().lower()
    if k in {"番剧", "动漫", "anime", "动画"}:
        return "anime"
    if k in {"电影", "movie", "movies"}:
        return "movie"
    return "other"


# ============================================================
# 文本归一化
# ============================================================

def _normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _strip_noise_tokens(value: str) -> str:
    text = _normalize_text(value)
    text = re.sub(r"[._\-_/]+", " ", text)
    text = re.sub(
        r"\b(1080p|2160p|720p|4k|hdr|bd|blu-ray|bluray|web[- ]?dl|webrip|hdtv|x264|x265|hevc|aac|ddp?|atmos|remux|proper|repack)\b",
        " ",
        text)
    text = re.sub(
        r"\b(第\s*\d+\s*季|\d+季|season\s*\d+|s\d{1,2}e\d{1,3}|s\d{1,2}|ep\d{1,3}|e\d{1,3}|\d{1,3}话|\d{1,3}集)\b",
        " ",
        text)
    # 中文噪音词
    text = re.sub(r"(全集|合集|完结|连载中|更新中|剧场版)", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _split_aliases(*values: object) -> list[str]:
    aliases: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        aliases.extend([part.strip()
                       for part in re.split(r"[|/、;,，]", text) if part.strip()])
        if text not in aliases:
            aliases.append(text)
    seen: set[str] = set()
    result: list[str] = []
    for alias in aliases:
        key = _normalize_text(alias)
        if key and key not in seen:
            seen.add(key)
            result.append(alias)
    return result


# ============================================================
# B 区快照收集
# ============================================================

def collect_b_media_snapshot(db: Database) -> dict[str, list[dict]]:
    """收集 B 区媒体快照，按 movie / tv 分组，同一媒体名聚合为一条候选。

    聚合规则：同 (kind, name) 的多条记录合并为一条候选，
    season_num 取最大值，episode_hint 取 OR。
    """
    # 聚合字典: (kind, name) -> aggregated item
    agg: dict[tuple[str, str], dict] = {}
    with db.read_connection() as conn:
        rows = conn.execute(
            "SELECT local_path, webdav_path, parent_webdav_path, source_a_path, fingerprint, status, updated_at FROM b_strm_files"
        ).fetchall()
    for row in rows:
        record = {
            "local_path": row[0],
            "webdav_path": row[1],
            "parent_webdav_path": row[2],
            "source_a_path": row[3],
            "fingerprint": row[4],
            "status": row[5],
            "updated_at": row[6],
        }
        kind, name = _media_info(record)
        if not _is_top_level_category(kind):
            continue
        parts = _path_parts(record.get("local_path")
                            or record.get("webdav_path"))
        season_num = 0
        episode_hint = False
        season_episodes: dict[int, set[int]] = {}
        for part in parts:
            parsed_season = _extract_season_int(part)
            if parsed_season is not None:
                season_num = max(season_num, parsed_season)
        # 解析 S01E05 格式获取 (season, episode)
        for part in parts:
            m = re.search(r"S(\d{1,2})E(\d{1,3})", part, re.I)
            if m:
                s_num = int(m.group(1))
                e_num = int(m.group(2))
                if s_num > 0:
                    season_episodes.setdefault(s_num, set()).add(e_num)
        joined = _normalize_text(" / ".join(parts))
        if re.search(
                r"\b(?:s\d{1,2}e\d{1,3}|ep\d{1,3}|e\d{1,3}|第\d+集|第\d+话)\b", joined):
            episode_hint = True

        key = (kind, name)
        if key not in agg:
            agg[key] = {
                "kind": kind,
                "name": name,
                "season_num": 0,
                "episode_hint": False,
                "episode_count": 0,
                "_season_episodes": {},
            }
        entry = agg[key]
        entry["season_num"] = max(entry["season_num"], season_num)
        entry["episode_hint"] = entry["episode_hint"] or episode_hint
        # 合并 season_episodes
        existing_se = entry.get("_season_episodes") or {}
        for s, eps in season_episodes.items():
            if s not in existing_se:
                existing_se[s] = set()
            existing_se[s].update(eps)
        entry["_season_episodes"] = existing_se

    movie_items: list[dict] = []
    tv_items: list[dict] = []
    for entry in agg.values():
        kind = entry["kind"]
        season_num = entry["season_num"]
        entry["season"] = f"第{season_num}季" if season_num > 0 else ""
        # 计算唯一集文件总数
        se = entry.get("_season_episodes") or {}
        entry["episode_count"] = sum(len(eps) for eps in se.values())
        if _category_filter_value(kind) == "movie":
            movie_items.append(entry)
        else:
            tv_items.append(entry)
    return {"movie": movie_items, "tv": tv_items}


# ============================================================
# 匹配评分
# ============================================================

def score_watchlist_item(
        item: dict, candidates: list[dict], media_type: str,
        fuzzy_threshold: float = 0.60,
        min_ep_ratio: float = 0.3,
) -> tuple[str, str]:
    """对单条 watchlist 条目进行收录状态评分。"""
    if not candidates:
        return "unmatched", "no_candidate"

    title_candidates = _split_aliases(
        item.get("title"),
        item.get("name"),
        item.get("original_title"),
        item.get("original_name"),
        item.get("name_cn"),
        item.get("original_name_cn"),
    )
    normalized_titles = []
    for alias in title_candidates:
        cleaned = _strip_noise_tokens(alias)
        if cleaned:
            normalized_titles.append(cleaned)
    if not normalized_titles:
        return "fuzzy", "missing_name"

    exact_hits = []
    loose_hits = []
    fuzzy_hits = []
    for candidate in candidates:
        candidate_name = _strip_noise_tokens(candidate.get("name", ""))
        if not candidate_name:
            continue
        # 精确：归一化后完全相等
        if candidate_name in normalized_titles:
            exact_hits.append(candidate)
        # 模糊：任一方包含另一方（子串匹配），排除过短标题避免误命中
        elif any(
            len(title) >= 3 and len(candidate_name) >= 3
            and (title in candidate_name or candidate_name in title)
            for title in normalized_titles
        ):
            loose_hits.append(candidate)
        else:
            # 第三级：SequenceMatcher 模糊匹配
            best_ratio = max(
                SequenceMatcher(None, title, candidate_name).ratio()
                for title in normalized_titles
            )
            if best_ratio >= fuzzy_threshold:
                fuzzy_hits.append((candidate, best_ratio))

    if media_type == "movie":
        if len(exact_hits) == 1:
            return "matched", "movie_exact:" + exact_hits[0]["name"]
        if len(exact_hits) > 1:
            names = ",".join(sorted({hit["name"] for hit in exact_hits[:3]}))
            return "fuzzy", "movie_name_conflict:" + names
        if len(loose_hits) == 1:
            return "matched", "movie_loose:" + loose_hits[0]["name"]
        if loose_hits:
            names = ",".join(sorted({hit["name"] for hit in loose_hits[:3]}))
            return "fuzzy", "movie_ambiguous:" + names
        if fuzzy_hits:
            best = max(fuzzy_hits, key=lambda x: x[1])
            return "fuzzy", "movie_fuzzy:%s(%.2f)" % (best[0]["name"], best[1])
        return "unmatched", "no_movie_candidate"

    # TV
    best_candidate = None
    if len(exact_hits) == 1:
        best_candidate = exact_hits[0]
        match_kind = "tv_exact"
    elif len(exact_hits) > 1:
        names = ",".join(sorted({hit["name"] for hit in exact_hits[:3]}))
        return "fuzzy", "tv_name_conflict:" + names
    elif len(loose_hits) == 1:
        best_candidate = loose_hits[0]
        match_kind = "tv_loose"
    elif loose_hits:
        names = ",".join(sorted({hit["name"] for hit in loose_hits[:3]}))
        return "fuzzy", "tv_ambiguous:" + names
    elif fuzzy_hits:
        best_fuzzy = max(fuzzy_hits, key=lambda x: x[1])
        best_candidate = best_fuzzy[0]
        match_kind = "tv_fuzzy"
    else:
        return "unmatched", "no_tv_candidate"

    if not best_candidate:
        return "fuzzy", "tv_ambiguous"

    season_num = int(best_candidate.get("season_num") or 0)
    episode_hint = bool(best_candidate.get("episode_hint"))
    # _season_count 来自 DB 批量填充（_populate_tv_details），number_of_seasons 来自 API
    # 原始字段
    total_seasons = int(item.get("_season_count")
                        or item.get("number_of_seasons") or 0)

    # 名字匹配已命中，但无季/集结构证据
    # tv_fuzzy 仅通过低阈值模糊匹配命中，无结构证据时标记 fuzzy 而非 matched
    if not (season_num > 0 or episode_hint):
        if match_kind == "tv_fuzzy":
            return "fuzzy", "%s:%s|no_structure" % (
                match_kind, best_candidate["name"])
        return "matched", "%s:%s|no_structure" % (
            match_kind, best_candidate["name"])

    # 路径有季/集证据
    # 季数严重不匹配（路径第5季但TMDB只有2季）
    if season_num > 0 and total_seasons > 0 and season_num > total_seasons + 1:
        return "fuzzy", "tv_season_mismatch:S%d>S%d+1" % (
            season_num, total_seasons)

    # last_episode_to_air 交叉验证：TMDB 最新播出季数 < B 区最大季数
    # 说明 TMDB 还没播到这个季，本地却有，可能是同名不同作品
    last_ep_season = int(item.get("_last_ep_season") or 0)
    if last_ep_season > 0 and season_num > 0 and last_ep_season < season_num:
        return "fuzzy", "tv_future_season:L%d<B%d" % (
            last_ep_season, season_num)

    if episode_hint:
        # 集数比例验证：已下载集数 / TMDB 总集数 < min_ep_ratio
        db_ep_count = int(best_candidate.get("episode_count") or 0)
        tmdb_ep_count = int(item.get("_episode_count")
                            or item.get("number_of_episodes") or 0)
        if db_ep_count > 0 and tmdb_ep_count > 0:
            ep_ratio = db_ep_count / tmdb_ep_count
            if ep_ratio < min_ep_ratio:
                return "fuzzy", "tv_few_episodes:%.0f%%<%d%%" % (
                    ep_ratio * 100, min_ep_ratio * 100)

        s = best_candidate.get("season", "") or "ep"
        return "matched", "%s:%s|%s" % (
            match_kind, best_candidate["name"], s)
    # 有季但无集文件，需要 TMDB 侧信息辅助
    if total_seasons > 0 and season_num <= total_seasons:
        return "matched", "%s:%s|S%d" % (
            match_kind, best_candidate["name"], season_num)
    # _season_count 尚未填充（异步批量获取未完成）时，仅有季目录不能确认 matched
    return "fuzzy", "tv_season_only"


# ============================================================
# 收录状态刷新
# ============================================================

def refresh_watchlist_match_state(
    webui: _MatchHost,
    fuzzy_threshold: float = 0.60,
    min_ep_ratio: float = 0.3,
) -> dict[str, int]:
    """刷新 TMDB 待看列表的 B 区收录状态，回写到 tmdb_watchlist.db。

    统计返回 {matched, fuzzy, unmatched, uncomputed, skipped_manual, total}。
    四个状态桶按写回完成后的数据库最终状态统计，
    且 matched+fuzzy+unmatched+uncomputed == total。
    skipped_manual 是附加计数，不参与四桶求和。
    """
    if not webui._watchlist_db or not webui._db:
        return {"matched": 0, "fuzzy": 0, "unmatched": 0,
                "uncomputed": 0, "skipped_manual": 0, "total": 0}
    all_items = webui._watchlist_db.get_all()
    if not all_items:
        return {"matched": 0, "fuzzy": 0, "unmatched": 0,
                "uncomputed": 0, "skipped_manual": 0, "total": 0}
    snapshot = collect_b_media_snapshot(webui._db)
    now = time.time()
    movie_states: list[tuple] = []
    tv_states: list[tuple] = []
    movie_ids: list[int] = []
    tv_ids: list[int] = []
    for item in all_items:
        media_type = item.get("_media_type") or "movie"
        candidates = snapshot.get(media_type, [])
        status, reason = score_watchlist_item(
            item, candidates, media_type, fuzzy_threshold, min_ep_ratio)
        item_id = int(item.get("id") or 0)
        if not item_id:
            # 无效 ID 计入 uncomputed，保持四桶之和 == total
            continue
        if media_type == "movie":
            movie_states.append((item_id, status, reason, now, 0.0, ""))
            movie_ids.append(item_id)
        else:
            tv_states.append((item_id, status, reason, now, 0.0, ""))
            tv_ids.append(item_id)
    # 写回到数据库（replace_match_state 保留人工覆盖行）
    if movie_states:
        webui._watchlist_db.replace_match_state("movie", movie_states)
    if tv_states:
        webui._watchlist_db.replace_match_state("tv", tv_states)

    # ---- 写回完成后，按 DB 最终状态统计四桶 ----
    all_ids = movie_ids + tv_ids
    counts: dict[str, int] = {
        "matched": 0, "fuzzy": 0, "unmatched": 0,
        "uncomputed": 0, "skipped_manual": 0,
        "total": len(all_items),
    }
    _VALID_BUCKETS = {"matched", "fuzzy", "unmatched", "uncomputed"}
    if all_ids:
        # 分 media_type 批量读取最终状态
        movie_final: dict[int, dict] = {}
        tv_final: dict[int, dict] = {}
        if movie_ids:
            movie_final = webui._watchlist_db.get_match_states("movie", movie_ids)
        if tv_ids:
            tv_final = webui._watchlist_db.get_match_states("tv", tv_ids)

        for item in all_items:
            media_type = item.get("_media_type") or "movie"
            item_id = int(item.get("id") or 0)
            if not item_id:
                # 无效 ID 归入 uncomputed，保持四桶之和 == total
                counts["uncomputed"] += 1
                continue
            final = (movie_final if media_type == "movie" else tv_final).get(item_id)
            if final is None:
                # 缺失 ID 归入 uncomputed
                counts["uncomputed"] += 1
                continue
            ms = final.get("match_status", "uncomputed")
            moa = final.get("manual_override_at", 0) or 0
            # 人工覆盖行计入 skipped_manual
            if moa > 0:
                counts["skipped_manual"] += 1
            # 按最终 match_status 归入对应桶（显式白名单，防止 total/skipped_manual 被污染）
            if ms in _VALID_BUCKETS:
                counts[ms] += 1
            else:
                # 未知/非法状态统一按 uncomputed 计数
                logging.warning("[TMDB] 未知 match_status: %s (id=%d), 按 uncomputed 计", ms, item_id)
                counts["uncomputed"] += 1
    return counts
