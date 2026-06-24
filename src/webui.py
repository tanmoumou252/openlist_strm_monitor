"""
WebUI 管理面板 - SPA 架构
导航/筛选/分页通过 JS 拦截链接，fetch 获取 HTML 片段，只替换 <main> 内容。
壁纸层永不重建。
ABC 三区两级浏览：
  1. 子类列表（具体番剧名/电影名卡片）
  2. 点击进入该子类下的 STRM 文件详情
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from math import ceil
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse
import urllib.request

from tmdb_watchlist_db import TmdbWatchlistDb

if TYPE_CHECKING:
    from tmdb_client import TmdbClient
    from config import WebUIConfig
    from database import Database


PAGE_SIZE = 50
STATIC_DIR = Path(__file__).resolve().parent / "webui_static"


def _is_lan_ip(ip: str) -> bool:
    if ip in ("127.0.0.1", "::1", "localhost"):
        return True
    if ip.startswith("::ffff:"):
        ip = ip.rsplit(":", 1)[-1]
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    if a == 10:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    if a == 169 and b == 254:
        return True
    return False


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _h(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _resolve_tmdb_proxy(app_config) -> str | None:
    """统一 TMDB 代理解析逻辑（与客户端初始化一致）"""
    tmdb_cfg = getattr(app_config, "tmdb", None)
    if not tmdb_cfg:
        return None
    if getattr(tmdb_cfg, "host", ""):
        return None  # 使用反代时不启用代理
    proxy_cfg = getattr(tmdb_cfg, "proxy", None)
    if proxy_cfg and proxy_cfg.enabled and proxy_cfg.http:
        return proxy_cfg.http
    return None


_SEASON_RE = re.compile(
    r"(?:^|[\\/._\-\s])(Season\s*\d+|S\d{1,2}|第[一二三四五六七八九十\d]+季)(?:[\\/._\-\s]|$)",
    re.I)
_EPISODE_RE = re.compile(
    r"(?:S\d{1,2}E\d{1,3}|第\s*\d+\s*[集话]|EP?\s*\d{1,3})", re.I)
_MOVIE_HINT_RE = re.compile(r"电影|movie|movies|film|films|cinema", re.I)
_CATEGORY_DIRS = {"番剧", "电影", "动漫", "anime", "movie", "movies", "film", "tv"}

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
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
    # Season XX
    m = re.match(r"^season\s*(\d{1,2})$", p)
    if m:
        return int(m.group(1))
    # SXX (单独一个 S 开头的目录)
    m = re.match(r"^s(\d{1,2})$", p)
    if m:
        return int(m.group(1))
    # 第X季
    m = re.match(r"^第([一二三四五六七八九十\d]+)季$", p)
    if m:
        return _cn_to_int(m.group(1))
    return None


def _path_parts(value: object) -> list[str]:
    return [p for p in str(value or "").replace("\\", "/").split("/") if p]


def _is_category_dir(name: str) -> bool:
    """判断是否是顶级分类目录（番剧/电影等），而不是具体作品名"""
    return name.strip().lower() in {item.lower() for item in _CATEGORY_DIRS}


def _category_filter_value(kind: str) -> str:
    """将分类名映射为统一的筛选值 (anime/movie/other)"""
    k = kind.strip().lower()
    if k in {"番剧", "动漫", "anime", "动画"}:
        return "anime"
    if k in {"电影", "movie", "movies"}:
        return "movie"
    return "other"


def _media_info(row: dict) -> tuple[str, str]:
    path = row.get("webdav_path") or row.get(
        "original_b_path") or row.get("local_path") or ""
    parts = _path_parts(path)
    movie_hint = _MOVIE_HINT_RE.search(path) is not None
    has_season = _SEASON_RE.search(path) is not None
    has_episode = _EPISODE_RE.search(path) is not None
    kind = "番剧" if (has_season or has_episode) else "电影"
    if movie_hint and not (has_season or has_episode):
        kind = "电影"
    if len(parts) <= 2 and not (has_season or has_episode):
        kind = "电影"
    media_name = ""
    if kind == "番剧":
        # 寻找季目录，其前面的非分类目录就是番剧名
        for idx, part in enumerate(parts):
            if _extract_season_int(part) is not None and idx > 0:
                parent = parts[idx - 1]
                if not _is_category_dir(parent):
                    media_name = parent
                    break
                # 父目录是分类目录，说明没有番剧名目录，取祖目录或用 boundary
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
        media_name = Path(parts[-1]).stem if parts else "未分类电影"
    return kind, media_name or (Path(parts[-1]).stem if parts else "未分类")


def _extract_season_from_local_path(local_path: str) -> str:
    """从本地路径中提取季信息，返回如 'S01' 或 '第一季' 的字符串"""
    parts = _path_parts(local_path)
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
    # 从文件名提取
    stem = Path(parts[-1]).stem if parts else ""
    m = re.search(r"S(\d{1,2})E", stem, re.I)
    if m:
        return f"S{int(m.group(1)):02d}"
    return ""


# ============================================================
# 工具函数
# ============================================================

def _build_img_opener(handler, use_proxy=True):
    """构建用于图片/头像请求的 opener（从配置读取代理，不依赖 tmdb_client.proxy）。"""
    cfg = handler.webui._config.tmdb
    proxy_cfg = getattr(cfg, "proxy", None)
    if use_proxy and proxy_cfg and proxy_cfg.enabled and proxy_cfg.http:
        proxy_handler = urllib.request.ProxyHandler(
            {"http": proxy_cfg.http, "https": proxy_cfg.http}
        )
        return urllib.request.build_opener(proxy_handler)
    return urllib.request.build_opener()


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


# ============================================================
# TMDB 路由（嵌入到 handler 中）
# ============================================================

def _safe_int(val: str | None, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


# 静态 TMDB Genre ID → 中文名映射表
# TMDB genre 列表极为稳定，此表几乎不需要维护。
# 来源: https://developer.themoviedb.org/reference/genre-movie-list
#       https://developer.themoviedb.org/reference/genre-tv-list
TMDB_GENRE_NAMES: dict[int, str] = {
    # Movie genres
    28: "动作",
    12: "冒险",
    16: "动画",
    35: "喜剧",
    80: "犯罪",
    99: "纪录",
    18: "剧情",
    10751: "家庭",
    14: "奇幻",
    36: "历史",
    27: "恐怖",
    10402: "音乐",
    9648: "悬疑",
    10749: "爱情",
    878: "科幻",
    10770: "电视电影",
    53: "惊悚",
    10752: "战争",
    37: "西部",
    # TV genres (与 movie 共享部分 ID)
    10759: "动作冒险",
    10762: "儿童",
    10763: "新闻",
    10764: "真人秀",
    10765: "科幻奇幻",
    10766: "肥皂剧",
    10767: "脱口秀",
    10768: "战争政治",
}


def _tmdb_routes(handler, tmdb_client: TmdbClient | None,
                 path: str, params: dict,
                 webui_server=None) -> bool:
    """
    处理 TMDB 相关路由。
    返回 True 表示已处理，False 表示不匹配。
    """
    if path == "/api/tmdb/status":
        if tmdb_client:
            handler._send_json({
                "configured": True,
                "account_id": tmdb_client.account_id,
                "username": tmdb_client.username,
                "avatar_path": tmdb_client.avatar_path,
                "proxy_enabled": bool(tmdb_client.proxy),
                "proxy_url": tmdb_client.proxy or "",
                "auth_mode": "api_key" if tmdb_client._use_api_key_auth else "access_token",
            })
        else:
            handler._send_json({"configured": False})
        return True

    # Avatar proxy route — 支持 EdgeOne/custom host 反代
    if path == "/api/tmdb/avatar":
        avatar_hash = params.get("hash", [""])[0]
        if not avatar_hash:
            handler._send_json({"error": "missing hash"}, 400)
            return True
        _host = handler.webui._config.tmdb.host
        if _host:
            avatar_url = f"{_host.rstrip('/')}/avatar/{avatar_hash}"
        else:
            avatar_url = f"https://www.gravatar.com/avatar/{avatar_hash}?d=identicon&s=80"
        try:
            ava_req = urllib.request.Request(
                avatar_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
            )
            opener = _build_img_opener(handler, use_proxy=not bool(_host))
            resp = opener.open(ava_req, timeout=10)
            img_data = resp.read()
            handler.send_response(200)
            handler.send_header("Content-Type", "image/png")
            handler.send_header("Content-Length", str(len(img_data)))
            handler.send_header("Cache-Control", "public, max-age=86400")
            handler.end_headers()
            handler.wfile.write(img_data)
            return True
        except Exception as e:
            logging.warning("[TMDB] 头像代理失败: %s", e)
            handler._send_json({"error": "avatar fetch failed"}, 502)
            return True

    # Poster proxy route — 后端代理加载 TMDB 海报
    if path == "/api/tmdb/poster":
        poster_path = params.get("path", [""])[0]
        width = params.get("w", ["342"])[0]
        if not poster_path:
            handler._send_json({"error": "missing path"}, 400)
            return True
        if width not in ("92", "154", "185", "342", "500", "780"):
            width = "342"
        img_base = (tmdb_client.image_base() if tmdb_client
                    else "https://image.tmdb.org/t/p")
        poster_url = f"{img_base}/w{width}{poster_path}"
        try:
            poster_req = urllib.request.Request(
                poster_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
            )
            opener = _build_img_opener(handler, use_proxy=False)
            resp = opener.open(poster_req, timeout=15)
            img_data = resp.read()
            content_type = resp.headers.get("Content-Type", "image/jpeg")
            handler.send_response(200)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Content-Length", str(len(img_data)))
            handler.send_header("Cache-Control", "public, max-age=604800")
            handler.end_headers()
            handler.wfile.write(img_data)
            return True
        except Exception as e:
            logging.warning("[TMDB] 海报代理失败: %s", e)
            handler._send_json({"error": "poster fetch failed"}, 502)
            return True

    if not tmdb_client:
        auth_hint = "TMDB 未配置 access_token 或 api_key，请在 config.toml 的 [tmdb] 段配置"
        handler._send_json({"error": auth_hint}, 503)
        return True

    if path == "/api/tmdb/watchlist/movies":
        if params.get("all", ["0"])[0] == "1":
            if webui_server and hasattr(webui_server, 'get_watchlist_cached'):
                all_items = webui_server.get_watchlist_cached()
                items = [i for i in all_items if i.get(
                    "_media_type") == "movie"]
            else:
                items = tmdb_client.fetch_all_watchlist_movies()
            handler._send_json({
                "account_id": tmdb_client.account_id,
                "media_type": "movie",
                "count": len(items),
                "results": items,
            })
        else:
            page = _safe_int(params.get("page", ["1"])[0], 1)
            items, has_next = tmdb_client.get_watchlist_movies(page=page)
            handler._send_json({
                "account_id": tmdb_client.account_id,
                "media_type": "movie",
                "page": page,
                "has_next_page": has_next,
                "count": len(items),
                "results": items,
            })
        return True

    if path == "/api/tmdb/watchlist/tv":
        if params.get("all", ["0"])[0] == "1":
            if webui_server and hasattr(webui_server, 'get_watchlist_cached'):
                all_items = webui_server.get_watchlist_cached()
                items = [i for i in all_items if i.get("_media_type") == "tv"]
            else:
                items = tmdb_client.fetch_all_watchlist_tv()
            handler._send_json({
                "account_id": tmdb_client.account_id,
                "media_type": "tv",
                "count": len(items),
                "results": items,
            })
        else:
            page = _safe_int(params.get("page", ["1"])[0], 1)
            items, has_next = tmdb_client.get_watchlist_tv(page=page)
            handler._send_json({
                "account_id": tmdb_client.account_id,
                "media_type": "tv",
                "page": page,
                "has_next_page": has_next,
                "count": len(items),
                "results": items,
            })
        return True

    if path.startswith("/api/tmdb/alias/"):
        parts = path.split("/")
        if len(parts) == 6:
            media_type = parts[4]
            tmdb_id = _safe_int(parts[5])
            if media_type == "movie":
                aliases = tmdb_client.get_movie_aliases(tmdb_id)
            elif media_type == "tv":
                aliases = tmdb_client.get_tv_aliases(tmdb_id)
            else:
                handler._send_json({"error": "unsupported media_type"}, 400)
                return True
            handler._send_json(
                {"id": tmdb_id, "type": media_type, "aliases": aliases[:20]})
            return True

    if path.startswith("/api/tmdb/detail/tv/"):
        parts = path.split("/")
        if len(parts) == 6:
            tmdb_id = _safe_int(parts[5])
            data = tmdb_client.get_tv_details(tmdb_id)
            if not data:
                handler._send_json({"error": "not found"}, 404)
                return True
            last_ep = data.get("last_episode_to_air")
            handler._send_json({
                "id": data.get("id"),
                "name": data.get("name"),
                "original_name": data.get("original_name"),
                "first_air_date": data.get("first_air_date"),
                "last_air_date": data.get("last_air_date"),
                "number_of_seasons": data.get("number_of_seasons"),
                "number_of_episodes": data.get("number_of_episodes"),
                "status": data.get("status"),
                "vote_average": data.get("vote_average"),
                "last_episode_to_air": last_ep,
            })
            return True

    # Season-count route — 供前端懒加载多季标签
    if path.startswith("/api/tmdb/season-count/"):
        # /api/tmdb/season-count/tv/12345
        parts = path.split("/")
        if len(parts) == 6:
            media_type = parts[4]
            tmdb_id = _safe_int(parts[5])
            count = 0
            if media_type == "tv" and tmdb_client:
                # 先查 DB 缓存
                if webui_server and hasattr(
                    webui_server, '_watchlist_db'
                ) and webui_server._watchlist_db:
                    try:
                        db_path = webui_server._watchlist_db._db_path
                        with sqlite3.connect(db_path, timeout=5) as conn:
                            conn.execute("PRAGMA busy_timeout=5000")
                            row = conn.execute(
                                "SELECT _season_count FROM tv WHERE id=?",
                                (tmdb_id,)
                            ).fetchone()
                            if row and row[0] > 0:
                                count = row[0]
                    except Exception:
                        pass
                # DB 未命中 -> 调 API
                if count == 0:
                    try:
                        count = tmdb_client.get_tv_seasons_info(tmdb_id)
                    except Exception:
                        count = 0
                    # 结果写回 DB
                    if webui_server and hasattr(
                        webui_server, '_watchlist_db'
                    ) and webui_server._watchlist_db:
                        try:
                            db_path = webui_server._watchlist_db._db_path
                            with sqlite3.connect(db_path, timeout=5) as conn:
                                conn.execute("PRAGMA busy_timeout=5000")
                                conn.execute(
                                    "UPDATE tv SET _season_count=? WHERE id=?",
                                    (count, tmdb_id)
                                )
                                conn.commit()
                        except Exception:
                            pass
            handler._send_json({"id": tmdb_id, "season_count": count})
            return True

    # Refresh route — 手动触发待看列表全量同步
    if path == "/api/tmdb/watchlist/refresh":
        if not tmdb_client:
            handler._send_json({"error": "TMDB 未配置"}, 503)
            return True
        if webui_server and hasattr(
            webui_server, '_watchlist_db'
        ) and webui_server._watchlist_db:
            try:
                items = webui_server._watchlist_db.sync(
                    tmdb_client, force=True
                )
                handler._send_json({
                    "success": True,
                    "count": len(items),
                    "movies": sum(
                        1 for i in items if i.get("_media_type") == "movie"),
                    "tv": sum(
                        1 for i in items if i.get("_media_type") == "tv"),
                })
            except Exception as e:
                handler._send_json(
                    {"success": False, "error": str(e)}, 500)
        else:
            handler._send_json(
                {"success": False, "error": "数据库不可用"}, 500)
        return True

    # Credits route — 供卡片翻转时懒加载演员列表
    if path.startswith("/api/tmdb/credits/"):
        parts = path.split("/")
        if len(parts) == 6:
            media_type = parts[4]
            tmdb_id = _safe_int(parts[5])
            if media_type == "movie":
                data = tmdb_client.get_movie_details(tmdb_id)
            elif media_type == "tv":
                data = tmdb_client.get_tv_details(tmdb_id)
            else:
                handler._send_json({"error": "unsupported media_type"}, 400)
                return True
            if not data:
                handler._send_json({"error": "not found"}, 404)
                return True
            cast_raw = data.get("credits", {}).get("cast", [])
            actors = [c for c in cast_raw
                      if c.get("known_for_department", "Acting") == "Acting"]
            cast = [{"name": c.get("name", ""), "character": c.get("character", "")}
                    for c in actors[:4]]
            handler._send_json({
                "id": tmdb_id,
                "type": media_type,
                "cast": cast,
            })
            return True

    # Genres route — 供卡片翻转时懒加载分类
    # 从缓存 watchlist 的 genre_ids + 静态映射表反查，零 API 调用
    if path.startswith("/api/tmdb/genres/"):
        parts = path.split("/")
        if len(parts) == 6:
            media_type = parts[4]
            tmdb_id = _safe_int(parts[5])
            genres: list[str] = []
            if webui_server:
                # 优先用内存缓存（不触发全量重拉取）
                items: list[dict] = []
                if hasattr(webui_server, '_watchlist_cache') and webui_server._watchlist_cache is not None:
                    items = webui_server._watchlist_cache
                elif hasattr(webui_server, 'get_watchlist_cached'):
                    items = webui_server.get_watchlist_cached()
                for item in items:
                    if item.get("id") == tmdb_id:
                        gids = item.get("genre_ids", [])
                        genres = [TMDB_GENRE_NAMES[gid]
                                  for gid in gids if gid in TMDB_GENRE_NAMES]
                        break
            handler._send_json({
                "id": tmdb_id,
                "type": media_type,
                "genres": genres[:3],
            })
            return True

    if path == "/api/tmdb/search/movie":
        query = params.get("query", ["Chronicle"])[0]
        page = _safe_int(params.get("page", ["1"])[0], 1)
        handler._send_json({
            "query": query,
            "page": page,
            "results": tmdb_client.search_movie(query, page=page),
        })
        return True

    if path == "/api/tmdb/search/tv":
        query = params.get("query", ["Breaking Bad"])[0]
        page = _safe_int(params.get("page", ["1"])[0], 1)
        handler._send_json({
            "query": query,
            "page": page,
            "results": tmdb_client.search_tv(query, page=page),
        })
        return True

    if path == "/api/tmdb/watchlist/export.csv":
        import csv
        import io
        all_items = (webui_server.get_watchlist_cached()
                     if webui_server and hasattr(webui_server, 'get_watchlist_cached')
                     else [])
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["状态", "TMDB ID", "类型", "标题", "原标题", "发布日期", "评分"])
        for item in all_items:
            media_type = item.get("_media_type", "movie")
            title = item.get("title") or item.get("name") or ""
            orig = item.get("original_title") or item.get(
                "original_name") or ""
            date = item.get("release_date") or item.get("first_air_date") or ""
            rating = item.get("vote_average", 0)
            status = item.get("_status", "out")
            status_label = {
                "in": "已收录",
                "out": "待看",
                "que": "有疑问"}.get(
                status,
                "待看")
            writer.writerow([status_label, item.get("id", ""), media_type,
                             title, orig, date, f"{rating:.1f}"])
        csv_data = buf.getvalue().encode("utf-8-sig")
        csv_path = handler.webui._config.tmdb.csv_watchlist_file
        if csv_path:
            try:
                Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
                with open(csv_path, "wb") as f:
                    f.write(csv_data)
                logging.info("[TMDB] CSV 已保存到: %s", csv_path)
                handler._send_json({
                    "success": True,
                    "message": f"已保存到 {csv_path}",
                    "path": csv_path,
                    "count": len(all_items)
                })
            except Exception as e:
                logging.warning("[TMDB] 保存 CSV 失败: %s", e)
                handler._send_json({
                    "success": False,
                    "message": f"保存失败: {e}"
                }, 500)
        else:
            handler._send_json({
                "success": False,
                "message": "未配置 csv_watchlist_file"
            }, 400)
        return True

    return False


class _WebUIHandler(BaseHTTPRequestHandler):
    webui: WebUIServer

    def log_message(self, format, *args):
        pass

    def _is_client_allowed(self) -> bool:
        ip = self.client_address[0] if self.client_address else ""
        return _is_lan_ip(ip)

    def _guard_request(self) -> bool:
        if not self._is_client_allowed():
            self._send_json({"error": "forbidden"}, 403)
            return False
        return True

    def _send_json(self, data, status=200):
        body = json.dumps(
            data,
            ensure_ascii=False,
            default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError):
            pass  # 客户端提前断开，可忽略

    def _send_html(self, body, status=200):
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_static_file(self, filename: str = "index.html", status=200):
        file_path = STATIC_DIR / filename
        try:
            body = file_path.read_bytes()
        except OSError:
            logging.error("_send_static_file: 无法读取静态文件 %s", file_path)
            self.send_error(500, "static file not found")
            return
        self.send_response(status)
        # 根据文件扩展名设置 Content-Type
        ext = Path(filename).suffix.lower()
        ctype_map = {
            ".ico": "image/x-icon",
            ".png": "image/png",
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }
        content_type = ctype_map.get(ext, "application/octet-stream")
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError):
            pass

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)
        if not self._guard_request():
            return

        # TMDB 路由
        if path.startswith("/api/tmdb/"):
            tmdb_client = getattr(self.webui, '_tmdb_client', None)
            if _tmdb_routes(self, tmdb_client, path, params,
                            webui_server=self.webui):
                return

        # 原有路由
        if path == "/" or path == "/api/page":
            self._send_static_file()
        elif path == "/favicon.ico":
            self._send_static_file("favicon.ico")
        elif path == "/logo.png":
            self._send_static_file("logo.png")
        elif path == "/api/dashboard":
            self._handle_dashboard()
        elif path == "/api/bing-wallpapers":
            self._handle_bing_wallpapers()
        elif path == "/api/logs":
            self._handle_logs_api(params)
        elif path == "/api/records/a":
            self._handle_records("a")
        elif path == "/api/records/b":
            self._handle_records("b")
        elif path == "/api/records/c":
            self._handle_records("c")
        elif path == "/api/config":
            self._handle_config_api()
        elif path.startswith("/api/area/"):
            area = path.split("/api/area/")[1].split("/")[0].split("?")[0]
            rest = path.split("/api/area/")[1]
            sub = rest[len(area):] if len(rest) > len(area) else ""
            if sub.startswith("/detail"):
                self._handle_area_detail(area, params)
            elif area:
                self._handle_area(area, params)
            else:
                self._send_json({"error": "not found"}, 404)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if not self._guard_request():
            return
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b"{}"
        if path == "/api/tmdb/configure":
            self._handle_tmdb_configure(body)
        elif path == "/api/restart-webui":
            self._handle_restart_webui()
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_tmdb_configure(self, body: bytes) -> None:
        """处理 TMDB 配置更新请求。"""
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"success": False, "error": "无效的 JSON"}, 400)
            return
        try:
            tmdb_cfg = getattr(self.webui._config, "tmdb", None)
            if not tmdb_cfg:
                self._send_json({"success": False, "error": "TMDB 配置不可用"}, 500)
                return
            changed = False
            for key in ("access_token", "api_key", "language", "host",
                        "watchlist_db", "csv_watchlist_file"):
                if key in data and data[key] is not None:
                    val = data[key]
                    # 验证 watchlist_db 路径
                    if key == "watchlist_db" and val:
                        val = str(val).strip()
                        if val:
                            # 相对路径转绝对路径
                            p = Path(val)
                            if not p.is_absolute():
                                base_dir = getattr(
                                    self.webui._config, "base_dir", str(Path.cwd()))
                                val = str(Path(base_dir) / val)
                    setattr(tmdb_cfg, key, val)
                    changed = True
            # Proxy settings — 前端发送扁平字段 proxy_http / proxy_enabled
            # （与 index.html 的表单字段保持一致，便于跨 WebUI 实现复用）
            if "proxy_http" in data:
                tmdb_cfg.proxy.http = data["proxy_http"] or ""
                changed = True
            if "proxy_enabled" in data:
                tmdb_cfg.proxy.enabled = bool(data["proxy_enabled"])
                changed = True
            if changed:
                # 重新初始化 TMDB 客户端
                self.webui._init_tmdb_client()
                # 重建 watchlist DB（路径解析逻辑与 __init__ 一致）
                self.webui._reinit_watchlist_db()
                # 保存覆盖文件
                self._save_tmdb_overrides(data)
                self._send_json({
                    "success": True,
                    "message": "TMDB 配置已更新",
                    "tmdb_configured": bool(self.webui._tmdb_client),
                })
            else:
                self._send_json({"success": True, "message": "无变更"})
        except Exception as e:
            logging.error("[WebUI] 保存 TMDB 配置异常: %s", e, exc_info=True)
            self._send_json({"success": False, "error": f"保存失败: {e}"}, 500)

    def _save_tmdb_overrides(self, changes: dict) -> None:
        """保存 TMDB 配置覆盖到 .tmdb_webui_config.json（不写 config.toml）。"""
        overrides_file = Path(__file__).resolve().parent.parent / ".tmdb_webui_config.json"
        try:
            existing = {}
            if overrides_file.exists():
                with open(overrides_file, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            tmdb_sec = existing.get("tmdb", {})
            for key, val in changes.items():
                if val is not None:
                    tmdb_sec[key] = val
            existing["tmdb"] = tmdb_sec
            with open(overrides_file, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            logging.info("[WebUI] TMDB 配置覆盖已保存: %s", overrides_file)
        except Exception as e:
            logging.warning("[WebUI] 保存配置覆盖失败: %s", e)

    def _handle_restart_webui(self) -> None:
        """重启 WebUI HTTP 服务。"""
        logging.info("[WebUI] 正在重启 HTTP 服务...")
        self._send_json({"success": True, "message": "正在重启 WebUI..."})
        # 在新线程中延迟重启，确保响应已发送
        def _do_restart():
            time.sleep(0.5)
            try:
                self.webui.stop()
                self.webui.start()
                logging.info("[WebUI] HTTP 服务重启完成")
            except Exception as e:
                logging.error("[WebUI] 重启失败: %s", e)
        threading.Thread(target=_do_restart, daemon=True).start()

    def _handle_dashboard(self) -> None:
        db: Database = self.webui._db
        try:
            counts = db.get_table_counts()
            b_status = db.get_b_status_counts()
            db_size = db.get_db_file_size()
            self._send_json({
                "a_count": counts.get("a_strm_files", 0),
                "b_count": counts.get("b_strm_files", 0),
                "c_count": counts.get("c_ghost_files", 0),
                "b_valid": b_status.get("valid", 0),
                "b_orphan": b_status.get("orphan", 0),
                "b_unknown": b_status.get("unknown", 0),
                "tmdb_configured": bool(self.webui._tmdb_client),
                # 遗留字段（保持向后兼容）
                "table_counts": counts,
                "b_status_counts": b_status,
                "db_file_size": db_size,
                "db_file_size_human": _human_size(db_size),
                "uptime": time.time() - self.webui._start_time,
            })
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_bing_wallpapers(self) -> None:
        try:
            url = "https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=8&mkt=zh-CN"
            req = urllib.request.Request(
                url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            wallpapers = []
            for img in raw.get("images", []):
                url_path = img.get("url", "")
                if url_path:
                    wallpapers.append(
                        {"url": "https://www.bing.com" + url_path, "copyright": img.get("copyright", "")})
            self._send_json({"wallpapers": wallpapers})
        except Exception as e:
            self._send_json({"wallpapers": [], "error": str(e)})

    def _handle_records(self, area: str) -> None:
        items = self._get_records(area)
        self._send_json({"count": len(items), "items": items})

    def _handle_area(self, area, params):
        """区域列表：返回按媒体分组的统计摘要（支持分页和排序）"""
        if area not in ("a", "b", "c"):
            self._send_json({"error": "invalid area"}, 400)
            return
        kind_filter = params.get("kind", ["all"])[0]
        q = params.get("q", [""])[0].strip().lower()
        sort_key = params.get("sort", ["name"])[0]
        sort_order = params.get("order", ["asc"])[0]
        page = _safe_int(params.get("page", ["1"])[0], 1)
        page_size = _safe_int(params.get("page_size", ["50"])[0], 50)
        records = self._get_records(area)
        kind_label_map = {"anime": "番剧", "movie": "电影", "other": "其他", "all": "全部"}
        media_groups: dict[tuple[str, str], dict] = {}
        kind_counts: dict[str, int] = {}
        from collections import defaultdict
        kind_counts = defaultdict(int)
        for rec in records:
            kind, name = _media_info(rec)
            key = (kind, name)
            if key not in media_groups:
                media_groups[key] = {"name": name, "kind": kind, "count": 0,
                                     "season": "", "latest_ts": 0}
            g = media_groups[key]
            g["count"] += 1
            cat = _category_filter_value(kind)
            kind_counts[cat] += 1
            season = _extract_season_from_local_path(rec.get("local_path", ""))
            if season and not g["season"]:
                g["season"] = season
            ts = rec.get("updated_at") or rec.get("moved_at") or 0
            if ts and ts > g["latest_ts"]:
                g["latest_ts"] = ts
        items = list(media_groups.values())
        if kind_filter != "all":
            items = [it for it in items if _category_filter_value(it["kind"]) == kind_filter]
        if q:
            items = [it for it in items if q in it["name"].lower()]
        reverse = sort_order == "desc"
        if sort_key == "count":
            items.sort(key=lambda x: x["count"], reverse=reverse)
        elif sort_key == "time":
            items.sort(key=lambda x: x["latest_ts"], reverse=reverse)
        elif sort_key == "kind":
            items.sort(key=lambda x: x["kind"], reverse=reverse)
        else:
            items.sort(key=lambda x: x["name"], reverse=reverse)
        total = len(items)
        total_pages = max(1, ceil(total / page_size)) if total > 0 else 1
        page = max(1, min(page, total_pages))
        start = (page - 1) * page_size
        page_items = items[start:start + page_size]
        self._send_json({
            "area": area,
            "kind_label": kind_label_map.get(kind_filter, kind_filter),
            "kind_counts": dict(kind_counts),
            "media_items": page_items,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "page_size": page_size,
        })

    def _handle_area_detail(self, area, params):
        """区域详情：返回指定媒体的所有记录，按季分组"""
        if area not in ("a", "b", "c"):
            self._send_json({"error": "invalid area"}, 400)
            return
        media_name = params.get("media", [""])[0]
        sort_field = params.get("sort", ["local_path"])[0]
        sort_order = params.get("order", ["asc"])[0]
        page = _safe_int(params.get("page", ["1"])[0], 1)
        records = self._get_records(area)
        if media_name:
            records = [r for r in records if media_name in (
                r.get("local_path") or r.get("webdav_path", ""))]
        local_root = ""
        webdav_root = ""
        if records:
            local_root = _compute_media_root(records[0].get("local_path", ""))
            webdav_root = _compute_media_root(records[0].get("webdav_path", ""))
        total = len(records)
        # Sort full record list before pagination
        reverse = sort_order == "desc"
        if records and sort_field in records[0]:
            records.sort(key=lambda r: r.get(sort_field, ""), reverse=reverse)
        total_pages = max(1, ceil(total / PAGE_SIZE)) if total else 1
        page = max(1, min(page, total_pages))
        start = (page - 1) * PAGE_SIZE
        end = start + PAGE_SIZE
        page_records = records[start:end]
        seasons_map: dict[str, list[dict]] = {}
        for rec in page_records:
            label = _extract_season_from_local_path(rec.get("local_path", "")) or "默认"
            seasons_map.setdefault(label, []).append(rec)
        seasons = [{"label": lbl, "records": recs} for lbl, recs in seasons_map.items()]
        self._send_json({
            "area": area,
            "media": media_name,
            "local_root": local_root,
            "webdav_root": webdav_root,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "seasons": seasons,
        })

    def _handle_logs_api(self, params: dict[str, list[str]]) -> None:
        lines = _safe_int(params.get("lines", ["200"])[0], 200)
        log_file = Path(self.webui._config.log.file)
        try:
            if not log_file.exists():
                self._send_json({"lines": [], "count": 0})
                return
            with log_file.open("r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            tail = all_lines[-lines:]
            self._send_json({"lines": [line.rstrip()
                            for line in tail], "count": len(tail)})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_config_api(self) -> None:
        cfg = self.webui._config
        tmdb_client = self.webui._tmdb_client
        token = cfg.tmdb.access_token
        token_preview = (token[:16] + "...") if len(token) > 16 else (token or "")
        self._send_json({
            # 新字段
            "db_file": cfg.local.db_file,
            "db_exists": os.path.isfile(cfg.local.db_file),
            "webui_port": cfg.webui.port,
            "webui_bind": cfg.webui.bind,
            "webui_has_password": bool(cfg.webui.password),
            "tmdb_configured": bool(self.webui._tmdb_client),
            "tmdb_token_preview": token_preview,
            "tmdb_token": token,
            "tmdb_language": cfg.tmdb.language,
            "tmdb_host": cfg.tmdb.host or "",
            "tmdb_api_key": cfg.tmdb.api_key or "",
            "tmdb_api_key_configured": bool(cfg.tmdb.api_key),
            "tmdb_proxy": _resolve_tmdb_proxy(cfg),
            "tmdb_proxy_http": getattr(cfg.tmdb.proxy, "http", ""),
            "tmdb_proxy_enabled": getattr(cfg.tmdb.proxy, "enabled", False),
            "tmdb_account_id": tmdb_client.account_id if tmdb_client else None,
            "tmdb_watchlist_db": getattr(cfg.tmdb, "watchlist_db", "") or "",
            # 遗留字段（保持向后兼容）
            "b_root": cfg.paths.b_root,
            "c_root": cfg.paths.c_root,
            "a_folders": cfg.a_folders,
            "strm_engine_paths": cfg.paths.strm_engine_paths,
            "refresh_paths": cfg.paths.refresh_paths,
            "webdav_host": cfg.webdav.host,
            "webdav_user": cfg.webdav.user,
            "webdav_password": bool(cfg.webdav.password),
            "refresh_enabled": cfg.refresh.enabled,
            "refresh_interval": cfg.refresh.interval_seconds,
            "behavior_action": cfg.behavior.action,
            "ghost_protect_seconds": cfg.behavior.ghost_protect_seconds,
        })

    def _get_records(self, area: str) -> list[dict]:
        db: Database = self.webui._db
        try:
            if area == "a":
                return [{"local_path": r[0], "webdav_path": r[1], "parent_webdav_path": r[2],
                         "updated_at": r[3]} for r in db.get_all_a_records()]
            if area == "b":
                return [{"local_path": r[0], "webdav_path": r[1], "parent_webdav_path": r[2], "source_a_path": r[3],
                         "fingerprint": r[4], "status": r[5], "updated_at": r[6]} for r in db.get_all_b_records()]
            if area == "c":
                return [{"local_path": r[0], "webdav_path": r[1], "original_b_path": r[2],
                         "ghost_root": r[3], "moved_at": r[4]} for r in db.get_all_c()]
        except Exception:
            logging.exception("[WebUI] 读取 %s 区失败", area)
        return []


class WebUIServer:
    def __init__(self, config: WebUIConfig, db: Database,
                 app_config=None) -> None:
        self._config = app_config
        self._db = db
        self._port = config.port
        self._bind = config.bind
        self._enabled = config.enabled
        self._start_time = time.time()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

        # 顺序很重要：先加载覆盖文件，再据此初始化客户端与 DB，
        # 否则 .tmdb_webui_config.json 中的 token / host / watchlist_db 覆盖
        # 不会在首次启动生效。
        self._tmdb_client = None
        self._watchlist_db: TmdbWatchlistDb | None = None

        # 1) 先用 config.toml 原始值初始化客户端（覆盖不存在/失败时的兜底）
        self._init_tmdb_client()
        # 2) 再加载覆盖（内部会在覆盖生效后重新 _init_tmdb_client）
        self._load_webui_overrides()
        # 3) 最后据最终配置创建 watchlist DB（路径来自 self._config）
        self._reinit_watchlist_db()

    def _load_webui_overrides(self) -> None:
        """加载 .tmdb_webui_config.json 覆盖文件（不写回 config.toml）。

        JSON 中 proxy 采用扁平字段（proxy_http / proxy_enabled），
        与前端表单及 test_webui.py 保持一致；读回时映射到嵌套的
        TmdbConfig.proxy 对象。
        """
        overrides_file = Path(__file__).resolve().parent.parent / ".tmdb_webui_config.json"
        if not overrides_file.exists():
            return
        try:
            with open(overrides_file, "r", encoding="utf-8") as f:
                overrides = json.load(f)
            tmdb_overrides = overrides.get("tmdb", {})
            if tmdb_overrides and hasattr(self._config, "tmdb"):
                cfg_tmdb = self._config.tmdb
                proxy_changed = False
                for key, val in tmdb_overrides.items():
                    if val is None:
                        continue
                    # proxy 扁平字段单独处理
                    if key == "proxy_http":
                        cfg_tmdb.proxy.http = val or ""
                        proxy_changed = True
                        continue
                    if key == "proxy_enabled":
                        cfg_tmdb.proxy.enabled = bool(val)
                        proxy_changed = True
                        continue
                    # 其余字段需是 TmdbConfig 已声明槽位，避免 AttributeError
                    if val != "" and hasattr(cfg_tmdb, key):
                        setattr(cfg_tmdb, key, val)
                logging.info(
                    "[WebUI] 已加载 .tmdb_webui_config.json 配置覆盖"
                    + (" (含代理设置)" if proxy_changed else "")
                )
                # 重新初始化 TMDB 客户端（覆盖可能改变了 token / host / proxy 等）
                self._init_tmdb_client()
        except Exception as e:
            logging.debug("[WebUI] 加载 .tmdb_webui_config.json 失败: %s", e)

    def _init_tmdb_client(self) -> None:
        """初始化或重新初始化 TMDB 客户端。"""
        self._tmdb_client = None
        try:
            from tmdb_client import create_tmdb_client
            tmdb_cfg = getattr(self._config, "tmdb", None)
            if tmdb_cfg:
                has_token = bool(getattr(tmdb_cfg, "access_token", None))
                has_key = bool(getattr(tmdb_cfg, "api_key", None))
                if has_token or has_key:
                    proxy = _resolve_tmdb_proxy(self._config)
                    self._tmdb_client = create_tmdb_client(
                        access_token=getattr(tmdb_cfg, "access_token", "") or "",
                        language=getattr(tmdb_cfg, "language", "zh-CN"),
                        proxy=proxy,
                        host=getattr(tmdb_cfg, "host", ""),
                        api_key=getattr(tmdb_cfg, "api_key", "") or "",
                    )
        except Exception as e:
            logging.debug("[WebUI] TMDB 客户端初始化失败: %s", e)

    def _reinit_watchlist_db(self) -> None:
        """据当前配置重建 TMDB 待看列表 SQLite 数据库。

        统一的 db 路径解析逻辑，供 __init__ 与配置变更后复用。
        watchlist_db 留空时默认放在配置目录（base_dir）下。
        """
        tmdb_cfg = getattr(self._config, "tmdb", None)
        if not tmdb_cfg:
            self._watchlist_db = None
            return
        # 无凭据时不创建 DB（与原行为一致）
        if not (bool(getattr(tmdb_cfg, "access_token", None))
                or bool(getattr(tmdb_cfg, "api_key", None))):
            self._watchlist_db = None
            return
        db_path = getattr(tmdb_cfg, "watchlist_db", "") or ""
        if not db_path:
            base_dir = getattr(self._config, "base_dir", str(Path.cwd()))
            db_path = os.path.join(str(base_dir), "tmdb_watchlist.db")
        ttl = float(getattr(tmdb_cfg, "watchlist_cache_ttl", 604800))
        try:
            self._watchlist_db = TmdbWatchlistDb(db_path, ttl)
        except Exception as e:
            logging.warning("[WebUI] 待看列表数据库初始化失败: %s", e)
            self._watchlist_db = None

    def get_watchlist_cached(self) -> list[dict]:
        """获取待看列表（委托给 TmdbWatchlistDb，带 TTL 判断）。"""
        if not self._tmdb_client or not self._watchlist_db:
            return []
        try:
            return self._watchlist_db.sync(self._tmdb_client)
        except Exception as e:
            logging.warning("[TMDB] 同步失败，返回已有缓存: %s", e)
            return self._watchlist_db.get_all()

    def start(self) -> None:
        if not self._enabled:
            logging.info("[WebUI] 已禁用，跳过启动")
            return
        bind_ip = self._bind
        if bind_ip not in ("127.0.0.1", "0.0.0.0") and not _is_lan_ip(bind_ip):
            logging.warning("[WebUI] 绑定地址 %s 可能不是局域网地址", bind_ip)
        handler_cls = type("_BoundHandler", (_WebUIHandler,), {})
        handler_cls.webui = self
        handler_cls.allow_reuse_address = True
        try:
            self._server = HTTPServer((self._bind, self._port), handler_cls)
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True, name="WebUI")
            self._thread.start()
            logging.info(
                "[WebUI] 管理面板已启动: http://%s:%s",
                self._bind,
                self._port)
        except OSError as e:
            logging.error("[WebUI] 启动失败 (端口 %s): %s", self._port, e)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            logging.info("[WebUI] 已停止")
