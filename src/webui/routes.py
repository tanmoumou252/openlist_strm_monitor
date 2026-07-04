"""
WebUI 路由与处理器模块（合并自 webui_routes.py + webui_handlers.py）。

包含：
- 工具函数（_is_lan_ip, _safe_int, _human_size, _resolve_tmdb_proxy 等）
- TMDB 路由（_tmdb_routes）
- 共享 POST 处理器（_handle_tmdb_configure, _handle_webui_config_* 等）
- OpenList 路由处理器
- Dashboard / Area / Records / Logs / Config 处理器
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import threading
import time
import urllib.request
from math import ceil
from pathlib import Path
from typing import TYPE_CHECKING

from watchlist_match import (
    _compute_media_root, _extract_season_from_local_path,
)

if TYPE_CHECKING:
    from tmdb_client import TmdbClient
    from database import Database


# ============================================================
# 工具函数
# ============================================================

def _is_lan_ip(ip: str) -> bool:
    """判断 IP 是否为局域网地址（含 localhost）"""
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
    """人类可读的文件大小"""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size //= 1024
    return f"{size:.1f} TB"


def _resolve_tmdb_proxy(app_config) -> str | None:
    """统一 TMDB 代理解析逻辑（与客户端初始化一致）"""
    tmdb_cfg = getattr(app_config, "tmdb", None)
    if not tmdb_cfg:
        return None
    if getattr(tmdb_cfg, "host", ""):
        return None  # 使用反代时不启用代理
    # 优先读扁平字段，fallback 到嵌套 proxy 对象
    proxy_enabled = getattr(tmdb_cfg, "proxy_enabled", False)
    proxy_http = getattr(tmdb_cfg, "proxy_http", "")
    if proxy_enabled and proxy_http:
        return proxy_http
    # 兼容旧配置：fallback 到嵌套 proxy
    proxy_cfg = getattr(tmdb_cfg, "proxy", None)
    if proxy_cfg and proxy_cfg.enabled and proxy_cfg.http:
        return proxy_cfg.http
    return None


def _build_img_opener(handler, use_proxy=True):
    """构建用于图片/头像请求的 opener（从配置读取代理，不依赖 tmdb_client.proxy）。"""
    cfg = handler.webui._config.tmdb
    # 优先读扁平字段，fallback 到嵌套 proxy
    proxy_enabled = getattr(cfg, "proxy_enabled", False)
    proxy_http = getattr(cfg, "proxy_http", "")
    if use_proxy and proxy_enabled and proxy_http:
        proxy_handler = urllib.request.ProxyHandler(
            {"http": proxy_http, "https": proxy_http}
        )
        return urllib.request.build_opener(proxy_handler)
    # 兼容旧配置
    proxy_cfg = getattr(cfg, "proxy", None)
    if use_proxy and proxy_cfg and proxy_cfg.enabled and proxy_cfg.http:
        proxy_handler = urllib.request.ProxyHandler(
            {"http": proxy_cfg.http, "https": proxy_cfg.http}
        )
        return urllib.request.build_opener(proxy_handler)
    return urllib.request.build_opener()


def _try_bind_port(host: str, port: int) -> bool:
    """尝试绑定端口，检测端口是否可用。

    设置 SO_REUSEADDR 以支持重启时 TIME_WAIT 状态复用。
    如果端口被其他进程占用 Windows 下同样绑成功，
    但后续 HTTPServer 创建失败会被 try-except 兜住。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
        return True
    except OSError:
        return False


def _safe_int(val: str | None, default: int = 0) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


# ============================================================
# TMDB Genre ID → 中文名映射表
# ============================================================
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


# ============================================================
# match_status → _status 映射
# ============================================================
_STATUS_MAP = {
    "matched": "in",
    "fuzzy": "que",
    "unmatched": "out",
    "uncomputed": "out",
}


# ============================================================
# 后台同步
# ============================================================

def _bg_sync_refresh(server) -> None:
    """后台执行待看列表同步（供 GET /api/tmdb/watchlist/refresh 调用）。"""
    try:
        server._watchlist_db.sync(server._tmdb_client, force=True)
        logging.info("[TMDB] 后台同步完成")
    except Exception as e:
        logging.warning("[TMDB] 后台同步失败: %s", e)
    finally:
        with server._sync_lock:
            server._sync_running = False


# ============================================================
# TMDB 路由
# ============================================================

def _tmdb_routes(handler, tmdb_client: TmdbClient | None,
                 path: str, params: dict,
                 webui_server=None) -> bool:
    """
    处理 TMDB 相关路由。
    返回 True 表示已处理，False 表示不匹配。
    """
    if path == "/api/tmdb/status":
        if tmdb_client:
            result = {
                "configured": True,
                "host": tmdb_client.host or "",
                "account_id": tmdb_client.account_id,
                "username": tmdb_client.username,
                "avatar_path": tmdb_client.avatar_path,
                "proxy_enabled": bool(tmdb_client.proxy),
                "proxy_url": tmdb_client.proxy or "",
                "auth_mode": "api_key" if tmdb_client._use_api_key_auth else "access_token",
            }
            _wdb = getattr(webui_server, '_watchlist_db', None)
            if _wdb:
                try:
                    result.update(_wdb.get_cache_status())
                except Exception:
                    pass
                try:
                    with _wdb._conn() as conn:
                        uncomputed = conn.execute(
                            "SELECT COUNT(*) FROM ("
                            "  SELECT id FROM movies WHERE match_status='uncomputed'"
                            "  UNION ALL"
                            "  SELECT id FROM tv WHERE match_status='uncomputed'"
                            ")").fetchone()[0]
                        total = conn.execute(
                            "SELECT (SELECT COUNT(*) FROM movies) + (SELECT COUNT(*) FROM tv)"
                        ).fetchone()[0]
                    result["match_uncomputed"] = uncomputed
                    result["match_total"] = total
                except Exception:
                    pass
            handler._send_json(result)
        else:
            handler._send_json({"configured": False})
        return True

    # Match refresh status polling
    if path == "/api/tmdb/watchlist/match/status":
        running = False
        refresh_result: dict | None = None
        if webui_server:
            with webui_server._match_refresh_lock:
                running = webui_server._match_refresh_running
                refresh_result = webui_server._match_refresh_result
        handler._send_json({
            "running": running,
            "result": refresh_result,
        })
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
                avatar_url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
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
                poster_url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
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

    # TMDB 操作日志路由（无需 TMDB 客户端）
    if path == "/api/tmdb/logs":
        _wdb = getattr(webui_server, '_watchlist_db', None)
        if webui_server and _wdb:
            limit_val = _safe_int(params.get("limit", ["100"])[0], 100)
            limit_val = max(1, min(limit_val, 500))
            try:
                logs = _wdb.get_tmdb_logs(limit=limit_val)
                handler._send_json({"logs": logs, "count": len(logs)})
            except Exception as e:
                logging.warning("[TMDB] 获取操作日志失败: %s", e)
                handler._send_json({"logs": [], "count": 0})
        else:
            handler._send_json({"logs": [], "count": 0})
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
            # 附加 _status 映射字段
            for item in items:
                item["_status"] = _STATUS_MAP.get(
                    item.get("match_status", "uncomputed"), "out")
            handler._send_json({
                "account_id": tmdb_client.account_id,
                "media_type": "movie",
                "count": len(items),
                "results": items,
            })
        else:
            page = _safe_int(params.get("page", ["1"])[0], 1)
            try:
                items, has_next = tmdb_client.get_watchlist_movies(page=page)
            except Exception as e:
                logging.error("[TMDB] 获取电影待看列表失败: %s", e)
                handler._send_json({"error": "获取待看列表失败", "detail": str(e)}, 500)
                return True
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
            # 附加 _status 映射字段
            for item in items:
                item["_status"] = _STATUS_MAP.get(
                    item.get("match_status", "uncomputed"), "out")
            handler._send_json({
                "account_id": tmdb_client.account_id,
                "media_type": "tv",
                "count": len(items),
                "results": items,
            })
        else:
            page = _safe_int(params.get("page", ["1"])[0], 1)
            try:
                items, has_next = tmdb_client.get_watchlist_tv(page=page)
            except Exception as e:
                logging.error("[TMDB] 获取剧集待看列表失败: %s", e)
                handler._send_json({"error": "获取待看列表失败", "detail": str(e)}, 500)
                return True
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
                # 只查 DB 缓存，不同步调 API（避免阻塞 HTTP 线程）
                # 季数/集数由 _populate_tv_details 异步批量填充
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
            handler._send_json({"id": tmdb_id, "season_count": count})
            return True

    # Refresh route — 后台触发待看列表全量同步（保持 GET 向后兼容）
    if path == "/api/tmdb/watchlist/refresh":
        if webui_server and hasattr(
            webui_server, '_watchlist_db'
        ) and webui_server._watchlist_db:
            with webui_server._sync_lock:
                if webui_server._sync_running:
                    handler._send_json({
                        "success": True, "message": "已在同步中"})
                    return True
                webui_server._sync_running = True
            threading.Thread(
                target=_bg_sync_refresh,
                args=(webui_server,),
                daemon=True,
            ).start()
            handler._send_json({
                "success": True, "message": "后台同步已启动"})
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
                items: list[dict] = []  # type: ignore[no-redef]
                if hasattr(
                        webui_server, '_watchlist_cache') and webui_server._watchlist_cache is not None:
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
        # CSV 使用的 items 没有经过 _STATUS_MAP 映射（watchlist/movies?all=1 路由才有）
        # 在此补上映射；_status 不存在时通过 match_status 回退
        for item in all_items:
            item["_status"] = _STATUS_MAP.get(
                item.get("match_status", "uncomputed"), "out")
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


# ============================================================
# 共享 TMDB POST 处理器
# ============================================================

from tmdb_watchlist_db import TmdbWatchlistDb  # noqa: E402
from watchlist_match import refresh_watchlist_match_state  # noqa: E402


def _handle_tmdb_configure(handler, webui_server, body: bytes) -> None:
    """处理 TMDB 配置更新请求。"""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        handler._send_json({"success": False, "error": "无效的 JSON"}, 400)
        return
    tmdb_cfg = getattr(webui_server._config, "tmdb", None)
    if not tmdb_cfg:
        handler._send_json({"success": False, "error": "TMDB 配置不可用"}, 500)
        return
    try:
        changed = False
        for key in ("access_token", "api_key", "language", "host",
                    "watchlist_db", "csv_watchlist_file"):
            if key in data and data[key] is not None:
                val = data[key]
                if key == "watchlist_db" and val:
                    val = str(val).strip()
                    if val:
                        p = Path(val)
                        if not p.is_absolute():
                            project_root = getattr(
                                webui_server, '_project_root', None)
                            if project_root:
                                base_dir = str(project_root)
                            else:
                                base_dir = getattr(
                                    webui_server._config, "base_dir",
                                    str(Path.cwd()))
                            val = str(Path(base_dir) / val)
                setattr(tmdb_cfg, key, val)
                changed = True
        # Proxy settings — 前端发送扁平字段
        if "proxy_http" in data:
            tmdb_cfg.proxy_http = data["proxy_http"] or ""
            # 兼容旧嵌套结构
            proxy_cfg = getattr(tmdb_cfg, "proxy", None)
            if proxy_cfg:
                proxy_cfg.http = data["proxy_http"] or ""
            changed = True
        if "proxy_enabled" in data:
            # 正确转换布尔值：支持字符串 "true"/"false"、数字 1/0、布尔值
            proxy_enabled_val = data["proxy_enabled"]
            if isinstance(proxy_enabled_val, str):
                proxy_enabled = proxy_enabled_val.lower() in ("true", "1", "yes")
            else:
                proxy_enabled = bool(proxy_enabled_val)
            tmdb_cfg.proxy_enabled = proxy_enabled
            proxy_cfg = getattr(tmdb_cfg, "proxy", None)
            if proxy_cfg:
                proxy_cfg.enabled = proxy_enabled
            changed = True
        # Watchlist enabled setting
        if "watchlist_enabled" in data:
            watchlist_enabled = str(data["watchlist_enabled"]).lower() in ("true", "1", "yes")
            # 保存到 DB
            _wdb = getattr(webui_server, '_watchlist_db', None)
            if _wdb:
                _wdb.set_config("tmdb", "watchlist_enabled", "true" if watchlist_enabled else "false")
            changed = True
        if changed:
            # 重新初始化 TMDB 客户端
            _handler_reinit_tmdb(webui_server, tmdb_cfg)
            # 保存到 DB（webui_config 表）
            _save_tmdb_to_db(webui_server, data)
            configured = bool(getattr(webui_server, '_tmdb_client', None))
            _wdb = getattr(webui_server, '_watchlist_db', None)
            if _wdb:
                try:
                    _wdb.log_tmdb_operation(
                        "config_update", "success", "TMDB 配置已保存",
                        detail=json.dumps({"tmdb_configured": configured}))
                except Exception:
                    pass
            handler._send_json({
                "success": True,
                "message": "TMDB 配置已更新",
                "tmdb_configured": configured,
            })
        else:
            handler._send_json({"success": True, "message": "无变更"})
    except Exception as e:
        logging.error("[TMDB] 保存 TMDB 配置异常: %s", e, exc_info=True)
        db = getattr(webui_server, '_watchlist_db', None)
        if db:
            try:
                db.log_tmdb_operation(
                    "config_update", "error", f"TMDB 配置保存失败: {e}")
            except Exception:
                pass
        handler._send_json({"success": False, "error": f"保存失败: {e}"}, 500)


def _handler_reinit_tmdb(webui_server, tmdb_cfg) -> None:
    """重新初始化 TMDB 客户端和 watchlist DB。"""
    from tmdb_client import create_tmdb_client  # noqa: E402
    # 解析代理
    host = getattr(tmdb_cfg, "host", "") or ""
    proxy_enabled = getattr(tmdb_cfg, "proxy_enabled", False)
    proxy_http = getattr(tmdb_cfg, "proxy_http", "") or ""
    if host:
        proxy = None  # 使用反代时不启用代理
    elif proxy_enabled and proxy_http:
        proxy = proxy_http
    else:
        proxy_cfg = getattr(tmdb_cfg, "proxy", None)
        proxy = proxy_cfg.http if proxy_cfg and proxy_cfg.enabled and proxy_cfg.http else None
    try:
        webui_server._tmdb_client = create_tmdb_client(
            access_token=getattr(tmdb_cfg, "access_token", "") or "",
            language=getattr(tmdb_cfg, "language", "zh-CN"),
            proxy=proxy,
            host=host,
            api_key=getattr(tmdb_cfg, "api_key", "") or "",
        )
    except Exception as e:
        logging.warning("[TMDB] 重新初始化客户端失败: %s", e)
        webui_server._tmdb_client = None
    # 重建 watchlist DB（固定路径，无条件创建）
    project_root = (getattr(webui_server, '_project_root', None)
                    or Path(__file__).resolve().parent.parent.parent)
    db_path = str(project_root / "tmdb_watchlist.db")
    ttl = float(getattr(tmdb_cfg, "watchlist_cache_ttl", 604800))
    try:
        webui_server._watchlist_db = TmdbWatchlistDb(db_path, ttl)
    except Exception as e:
        logging.warning("[TMDB] 待看列表数据库重建失败: %s", e)
        webui_server._watchlist_db = None


def _save_tmdb_to_db(webui_server, changes: dict) -> None:
    """保存 TMDB 配置到 DB webui_config 表（scope="tmdb"）。"""
    _wdb = getattr(webui_server, '_watchlist_db', None)
    if not _wdb:
        logging.warning("[TMDB] 无法保存配置到 DB: watchlist_db 未初始化")
        return
    try:
        for key, val in changes.items():
            if val is not None:
                _wdb.set_config("tmdb", str(key), str(val))
        logging.info("[TMDB] 配置已保存到 DB (webui_config scope=tmdb)")
    except Exception as e:
        logging.warning("[TMDB] 保存配置到 DB 失败: %s", e)


def _handle_webui_config_get(handler, webui_server, scope: str) -> None:
    """处理 GET /api/webui/config/{scope} — 返回指定 scope 的所有配置。"""
    ALLOWED_SCOPES = {"tmdb", "openlist", "ui", "migration"}
    if scope not in ALLOWED_SCOPES:
        handler._send_json({"success": False, "error": f"不允许的 scope: {scope}"}, 403)
        return
    _wdb = getattr(webui_server, '_watchlist_db', None)
    if not _wdb:
        handler._send_json({"success": False, "error": "DB 未初始化"}, 500)
        return
    try:
        cfg = _wdb.get_all_config(scope)
        handler._send_json({"success": True, "scope": scope, "config": cfg})
    except Exception as e:
        logging.warning("[WebUI] 读取配置失败 (scope=%s): %s", scope, e)
        handler._send_json({"success": False, "error": str(e)}, 500)


def _handle_webui_config_post(handler, webui_server, scope: str,
                               body: bytes) -> None:
    """处理 POST /api/webui/config/{scope} — 批量写入配置。"""
    ALLOWED_SCOPES = {"tmdb", "openlist", "ui"}
    if scope not in ALLOWED_SCOPES:
        handler._send_json({"success": False, "error": f"不允许的 scope: {scope}"}, 403)
        return
    _wdb = getattr(webui_server, '_watchlist_db', None)
    if not _wdb:
        handler._send_json({"success": False, "error": "DB 未初始化"}, 500)
        return
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            handler._send_json(
                {"success": False, "error": "JSON body 须为对象"}, 400)
            return
        for key, val in data.items():
            _wdb.set_config(scope, str(key), str(val) if val is not None else "")
        
        # OpenList 配置保存后触发热更新
        if scope == "openlist":
            _hot_reload_openlist_config(webui_server)
        
        handler._send_json(
            {"success": True, "scope": scope, "saved": len(data)})
    except json.JSONDecodeError:
        handler._send_json({"success": False, "error": "无效的 JSON"}, 400)
    except Exception as e:
        logging.warning("[WebUI] 写入配置失败 (scope=%s): %s", scope, e)
        handler._send_json({"success": False, "error": str(e)}, 500)


def _hot_reload_openlist_config(webui_server) -> None:
    """OpenList 配置保存后热更新：从 DB 重新加载配置并更新内存引用。"""
    try:
        cfg = webui_server._config
        _wdb = getattr(webui_server, '_watchlist_db', None)
        if not _wdb:
            return
        
        # 保存旧的 webdav 连接信息
        old_host = cfg.webdav.host
        old_user = cfg.webdav.user
        old_password = cfg.webdav.password
        old_totp = cfg.webdav.totp_secret
        
        # 从 DB 重新加载配置
        cfg.update_from_db(_wdb)
        
        # 如果 webdav 连接信息变更，重新初始化 OpenListAdminClient
        new_host = cfg.webdav.host
        new_user = cfg.webdav.user
        new_password = cfg.webdav.password
        new_totp = cfg.webdav.totp_secret
        
        if (old_host != new_host or old_user != new_user or 
            old_password != new_password or old_totp != new_totp):
            _reinit_admin_client(webui_server)
            logging.info("[HotReload] WebDAV 连接已更新，OpenListAdminClient 已重新初始化")
        else:
            logging.info("[HotReload] OpenList 配置已热更新（WebDAV 连接未变）")
    except Exception as e:
        logging.warning("[HotReload] OpenList 配置热更新失败: %s", e)


def _reinit_admin_client(webui_server) -> None:
    """重新初始化 OpenListAdminClient 并更新 AppService 引用。"""
    try:
        from webdav_client import OpenListAdminClient
        cfg = webui_server._config
        new_client = OpenListAdminClient(
            cfg.webdav.host,
            cfg.webdav.user,
            cfg.webdav.password,
            totp_secret=cfg.webdav.totp_secret,
        )
        if new_client.login():
            logging.info("[HotReload] 新的 OpenListAdminClient 登录成功")
        else:
            logging.warning("[HotReload] 新的 OpenListAdminClient 登录失败: %s", new_client.last_error_message or "未知错误")
        
        # 更新 WebUIServer 持有的 admin_client 引用
        webui_server._admin_client = new_client
        
        # 如果 AppService 可访问，更新其 admin_api 引用
        app_service = getattr(webui_server, '_app_service', None)
        if app_service:
            app_service.admin_api = new_client
            logging.info("[HotReload] AppService.admin_api 已更新")
    except Exception as e:
        logging.warning("[HotReload] 重新初始化 OpenListAdminClient 失败: %s", e)


def _handle_openlist_test_connection(handler, webui_server, body: bytes) -> None:
    """处理 POST /api/openlist/test-connection — 验证 API 连接。"""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, Exception):
        data = {}
    
    cfg = webui_server._config
    host = data.get("host", cfg.webdav.host)
    user = data.get("user", cfg.webdav.user)
    password = data.get("password", cfg.webdav.password)
    totp_secret = data.get("totp_secret", cfg.webdav.totp_secret)
    
    if not host:
        handler._send_json({"success": False, "error": "WebDAV 地址不能为空"}, 400)
        return
    
    try:
        from webdav_client import OpenListAdminClient
        client = OpenListAdminClient(host, user, password, totp_secret=totp_secret)
        # 强制重新登录，不使用缓存的 Token，确保测试的是当前配置的连接
        if client.login(force=True):
            handler._send_json({
                "success": True,
                "message": "连接成功",
                "host": host,
            })
        else:
            error_type = client.last_error_type or "unknown"
            error_message = client.last_error_message or ""
            
            # 根据错误类型返回不同的错误消息
            error_messages = {
                "wrong_password": "密码错误，请检查用户名和密码",
                "wrong_2fa": "2FA 验证码错误，请检查 2FA 密钥或时间同步",
                "account_not_found": "账号不存在，请检查用户名",
                "network_error": "无法连接到 OpenList 服务器，请检查地址和网络",
                "unknown": "登录失败，请检查配置",
            }
            display_message = error_messages.get(error_type, error_messages["unknown"])
            
            handler._send_json({
                "success": False,
                "error": display_message,
                "error_type": error_type,
                "error_message": error_message,
            })
    except Exception as e:
        handler._send_json({
            "success": False,
            "error": f"连接失败: {e}",
            "error_type": "exception",
        })


def _fetch_strm_storages(cfg) -> list[dict]:
    """从 OpenList API 获取 STRM 存储列表。
    
    返回格式:
    [
        {
            "mount_path": "/strm",
            "paths": ["/strm/path1", "/strm/path2"],
            "local_path": "/local/path"
        },
        ...
    ]
    
    如果获取失败，返回空列表。
    """
    from webdav_client import OpenListAdminClient
    
    result: list[dict] = []
    try:
        client = OpenListAdminClient(
            cfg.webdav.host, cfg.webdav.user, cfg.webdav.password,
            totp_secret=cfg.webdav.totp_secret,
        )
        if not client.login():
            logging.warning("[OpenList] API 登录失败，无法获取 STRM 存储列表")
            return result
        
        storages = client.list_storages()
        if not storages or not isinstance(storages, dict):
            return result
        
        data = storages.get("data", {})
        content = data.get("content", []) if isinstance(data, dict) else []
        
        for storage in content:
            if storage.get("driver", "").lower() != "strm":
                continue
            
            mount_path = storage.get("mount_path", "")
            addition_str = storage.get("addition", "{}")
            
            try:
                # 确保 addition_str 是字符串
                if not isinstance(addition_str, str):
                    addition_str = str(addition_str)
                addition = json.loads(addition_str)
                paths_val = addition.get("paths", "")
                
                # 解析 paths：可能是列表或换行分隔的字符串
                if isinstance(paths_val, list):
                    storage_paths = [str(p).strip() for p in paths_val if str(p).strip()]
                else:
                    storage_paths = [p.strip() for p in paths_val.split("\n") if p.strip()]
                
                local_path = addition.get("SaveStrmLocalPath", "")
                
                result.append({
                    "mount_path": mount_path,
                    "paths": storage_paths,
                    "local_path": local_path,
                })
            except json.JSONDecodeError:
                logging.warning("[OpenList] 解析 storage addition 失败: %s", addition_str[:200])
        
        logging.info("[OpenList] 从 API 获取到 %d 个 STRM 存储", len(result))
    except Exception as e:
        logging.warning("[OpenList] 获取 STRM 存储列表失败: %s", e)
    
    return result


def _handle_openlist_strm_engines(handler, webui_server) -> None:
    """处理 GET /api/openlist/strm-engines — 获取 STRM 引擎列表。
    
    优先从 cfg.strm_storage_map 读取，如果为空则从 OpenList API 动态获取。
    结果按 mount_path 聚合，确保下拉框不重复。
    """
    cfg = webui_server._config
    strm_map = cfg.strm_storage_map
    raw_engines = []
    
    # 优先从 strm_storage_map 读取
    if strm_map:
        for entry_path, mapping in strm_map.items():
            raw_engines.append({
                "entry_path": entry_path,
                "mount_path": mapping.mount_path,
                "paths": mapping.paths,
                "local_path": mapping.local_path,
            })
        logging.debug("[OpenList] 从 strm_storage_map 读取到 %d 个引擎", len(raw_engines))
    else:
        # strm_storage_map 为空，尝试从 OpenList API 动态获取
        logging.info("[OpenList] strm_storage_map 为空，尝试从 API 动态获取 STRM 引擎列表")
        strm_storages = _fetch_strm_storages(cfg)
        for storage in strm_storages:
            raw_engines.append({
                "entry_path": storage["mount_path"],
                "mount_path": storage["mount_path"],
                "paths": storage["paths"],
                "local_path": storage["local_path"],
            })
    
    # 按 mount_path 聚合：合并同 mount_path 的所有 paths，去重
    engines: list[dict] = []
    mount_path_map = {}  # mount_path -> index in engines
    for eng in raw_engines:
        mp = eng["mount_path"]
        if mp not in mount_path_map:
            mount_path_map[mp] = len(engines)
            engines.append({
                "entry_path": eng["entry_path"],
                "mount_path": mp,
                "paths": list(eng["paths"]),
                "local_path": eng.get("local_path", ""),
            })
        else:
            idx = mount_path_map[mp]
            # 合并 paths，去重
            existing_paths = set(engines[idx]["paths"])
            for p in eng["paths"]:
                if p not in existing_paths:
                    engines[idx]["paths"].append(p)
                    existing_paths.add(p)
    
    handler._send_json({"success": True, "engines": engines})


def _handle_openlist_monitored_paths(handler, webui_server, params) -> None:
    """处理 GET /api/openlist/monitored-paths?engine=/strm — 获取监控目录。
    
    优先从 cfg.strm_storage_map 读取，如果为空则从 OpenList API 动态获取。
    """
    engine = params.get("engine", [""])[0]
    if not engine:
        handler._send_json({"success": False, "error": "engine 参数必填"}, 400)
        return
    
    cfg = webui_server._config
    strm_map = cfg.strm_storage_map
    paths = []
    
    # 优先从 strm_storage_map 读取
    if strm_map:
        # 查找以 engine 为 mount_path 前缀的所有条目
        for entry_path, mapping in strm_map.items():
            if mapping.mount_path == engine or entry_path == engine or entry_path.startswith(engine.rstrip("/") + "/"):
                paths.extend(mapping.paths)
        logging.debug("[OpenList] 从 strm_storage_map 读取到 %d 个监控目录 (engine=%s)", len(paths), engine)
    else:
        # strm_storage_map 为空，尝试从 OpenList API 动态获取
        logging.info("[OpenList] strm_storage_map 为空，尝试从 API 动态获取监控目录 (engine=%s)", engine)
        strm_storages = _fetch_strm_storages(cfg)
        for storage in strm_storages:
            if storage["mount_path"] == engine:
                paths.extend(storage["paths"])
        logging.info("[OpenList] 从 API 动态获取到 %d 个监控目录 (engine=%s)", len(paths), engine)
    
    handler._send_json({"success": True, "engine": engine, "paths": paths})


def _handle_openlist_status(handler, webui_server) -> None:
    """处理 GET /api/openlist/status — API 状态探测。"""
    cfg = webui_server._config
    if not cfg.webdav.host:
        handler._send_json({"success": True, "status": "unconfigured"})
        return
    
    try:
        from webdav_client import OpenListAdminClient
        client = OpenListAdminClient(
            cfg.webdav.host, cfg.webdav.user, cfg.webdav.password,
            totp_secret=cfg.webdav.totp_secret,
        )
        if client.login():
            handler._send_json({"success": True, "status": "online", "host": cfg.webdav.host})
        else:
            error_type = client.last_error_type or "unknown"
            # 映射到前端状态码
            status_map = {
                "wrong_password": "auth_failed_password",
                "wrong_2fa": "auth_failed_2fa",
                "account_not_found": "auth_failed",
                "network_error": "offline",
                "unknown": "auth_failed",
            }
            status = status_map.get(error_type, "auth_failed")
            handler._send_json({"success": True, "status": status})
    except Exception as e:
        handler._send_json({"success": True, "status": "offline", "error": str(e)})


def _handle_openlist_paths(handler, webui_server) -> None:
    """处理 GET /api/openlist/paths — 路径自动获取。"""
    cfg = webui_server._config
    strm_map = cfg.strm_storage_map
    
    a_folders = []
    for mapping in strm_map.values():
        if mapping.local_path and mapping.local_path not in a_folders:
            a_folders.append(mapping.local_path)
    
    handler._send_json({
        "success": True,
        "a_folders": a_folders,
        "b_root": cfg.paths.b_root,
        "c_root": cfg.paths.c_root,
    })


def _handle_tmdb_watchlist_match_refresh(handler, webui_server) -> None:
    """触发后台刷新 TMDB 待看列表的收录状态。"""
    if not getattr(webui_server, '_watchlist_db', None):
        handler._send_json(
            {"success": False, "message": "TMDB 待看数据库未启用"}, 400)
        return
    if not getattr(webui_server, '_db', None):
        handler._send_json({"success": False, "message": "主数据库未连接"}, 400)
        return
    with webui_server._match_refresh_lock:
        if webui_server._match_refresh_running:
            handler._send_json({"success": True, "message": "已在刷新中"})
            return
        webui_server._match_refresh_running = True
        webui_server._match_refresh_result = None
    _wdb = getattr(webui_server, '_watchlist_db', None)
    if _wdb:
        try:
            _wdb.log_tmdb_operation("match_refresh_start", "info", "收录状态刷新启动")
        except Exception:
            pass
    threading.Thread(
        target=_do_match_refresh, args=(webui_server,), daemon=True).start()
    handler._send_json({"success": True, "message": "后台收录状态刷新已启动"})


def _do_match_refresh(webui_server) -> None:
    """后台执行收录状态刷新。"""
    _wdb = getattr(webui_server, '_watchlist_db', None)
    try:
        tmdb_cfg = getattr(webui_server._config, "tmdb", None)
        fuzzy = float(
            getattr(
                tmdb_cfg,
                "fuzzy_threshold",
                0.60)) if tmdb_cfg else 0.60
        min_ep = float(
            getattr(
                tmdb_cfg,
                "anime_min_ep_ratio",
                0.3)) if tmdb_cfg else 0.3
        counts = refresh_watchlist_match_state(webui_server, fuzzy, min_ep)
        with webui_server._match_refresh_lock:
            webui_server._match_refresh_result = counts
        logging.info("[TMDB] 收录状态刷新完成: %s", counts)
        if _wdb:
            try:
                _wdb.log_tmdb_operation(
                    "match_refresh", "success", f"收录状态刷新完成: {counts}")
            except Exception:
                pass
    except Exception as e:
        logging.error("[TMDB] 收录状态刷新失败: %s", e, exc_info=True)
        with webui_server._match_refresh_lock:
            webui_server._match_refresh_result = {"error": str(e)}
        if _wdb:
            try:
                _wdb.log_tmdb_operation(
                    "match_refresh", "error", f"收录状态刷新失败: {e}")
            except Exception:
                pass
    finally:
        with webui_server._match_refresh_lock:
            webui_server._match_refresh_running = False


def _handle_tmdb_watchlist_match_override(
        handler, webui_server, body: bytes) -> None:
    """手动覆盖 TMDB 待看条目收录状态。"""
    if not getattr(webui_server, '_watchlist_db', None):
        handler._send_json(
            {"success": False, "message": "TMDB 待看数据库未启用"}, 400)
        return
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        handler._send_json({"success": False, "message": "无效的 JSON"}, 400)
        return
    media_type = str(data.get("media_type") or "").strip()
    if media_type not in {"movie", "tv"}:
        handler._send_json(
            {"success": False, "message": "无效的 media_type"}, 400)
        return
    try:
        item_id = int(data.get("id") or 0)
    except (TypeError, ValueError):
        handler._send_json({"success": False, "message": "无效的 id"}, 400)
        return
    status = str(data.get("status") or "").strip()
    if status not in {"matched", "fuzzy", "unmatched", "uncomputed"}:
        handler._send_json({"success": False, "message": "无效的 status"}, 400)
        return
    reason = str(data.get("reason") or "manual_override")[:256]
    try:
        webui_server._watchlist_db.override_match_state(
            media_type, item_id, status, reason)
        handler._send_json({"success": True, "message": "收录状态已手动覆盖"})
        _wdb = getattr(webui_server, '_watchlist_db', None)
        if _wdb:
            try:
                _wdb.log_tmdb_operation(
                    "match_override", "info",
                    f"手动覆盖 {media_type}/{item_id} → {status}",
                    detail=json.dumps({
                        "media_type": media_type, "id": item_id,
                        "status": status, "reason": reason,
                    }),
                )
            except Exception:
                pass
    except Exception as e:
        logging.error("[TMDB] 手动覆盖收录状态失败: %s", e, exc_info=True)
        handler._send_json({"success": False, "message": f"覆盖失败: {e}"}, 500)


def _handle_tmdb_watchlist_bg_sync(handler, webui_server) -> None:
    """触发后台 TMDB 待看列表同步。"""
    if not getattr(webui_server, '_tmdb_client', None) or not getattr(
            webui_server, '_watchlist_db', None):
        handler._send_json({"success": False, "message": "TMDB 未配置"}, 400)
        return
    with webui_server._sync_lock:
        if webui_server._sync_running:
            handler._send_json({"success": True, "message": "已在同步中"})
            return
        webui_server._sync_running = True
    _wdb = getattr(webui_server, '_watchlist_db', None)
    if _wdb:
        try:
            _wdb.log_tmdb_operation("sync_start", "info", "TMDB 同步启动")
        except Exception:
            pass
    threading.Thread(
        target=_do_bg_sync, args=(webui_server,), daemon=True).start()
    handler._send_json({"success": True, "message": "后台同步已启动"})


def _do_bg_sync(webui_server) -> None:
    """后台执行待看列表同步。"""
    try:
        webui_server._watchlist_db.sync(webui_server._tmdb_client)
        logging.info("[TMDB] 后台同步完成")
        _wdb = getattr(webui_server, '_watchlist_db', None)
        if _wdb:
            try:
                _wdb.log_tmdb_operation("sync", "success", "TMDB 同步完成")
            except Exception:
                pass
    except Exception as e:
        logging.warning("[TMDB] 后台同步失败: %s", e)
        _wdb = getattr(webui_server, '_watchlist_db', None)
        if _wdb:
            try:
                _wdb.log_tmdb_operation("sync", "error", f"TMDB 同步失败: {e}")
            except Exception:
                pass
    finally:
        with webui_server._sync_lock:
            webui_server._sync_running = False


def _handle_restart_webui(handler, webui_server) -> None:
    """重启 WebUI HTTP 服务。"""
    logging.info("[WebUI] 正在重启 HTTP 服务...")
    _wdb = getattr(webui_server, '_watchlist_db', None)
    if _wdb:
        try:
            _wdb.log_tmdb_operation("restart", "info", "WebUI 重启")
        except Exception:
            pass
    handler._send_json({"success": True, "message": "正在重启 WebUI..."})

    def _do_restart():
        time.sleep(0.5)
        try:
            webui_server.stop()
            webui_server.start()
            logging.info("[WebUI] HTTP 服务重启完成")
        except Exception as e:
            logging.error("[WebUI] 重启失败: %s", e)
    threading.Thread(target=_do_restart, daemon=True).start()


# ============================================================
# 常量（Dashboard / Area 相关）
# ============================================================

PAGE_SIZE = 50


# ============================================================
# 工具：兼容两种 db 访问模式
# ============================================================

def _db_get_table_counts(db) -> dict[str, int]:
    """获取各表记录数。优先使用 db 方法，回退到原始 SQL。"""
    if hasattr(db, 'get_table_counts'):
        return db.get_table_counts()
    # 回退到原始 SQL
    try:
        with db.read_connection() as conn:
            a = conn.execute("SELECT COUNT(*) FROM a_strm_files").fetchone()[0]
            b = conn.execute("SELECT COUNT(*) FROM b_strm_files").fetchone()[0]
            c = conn.execute(
                "SELECT COUNT(*) FROM c_ghost_files").fetchone()[0]
        return {"a_strm_files": a, "b_strm_files": b, "c_ghost_files": c}
    except Exception:
        return {"a_strm_files": 0, "b_strm_files": 0, "c_ghost_files": 0}


def _db_get_b_status_counts(db) -> dict[str, int]:
    """获取 B 区状态统计。优先使用 db 方法，回退到原始 SQL。"""
    if hasattr(db, 'get_b_status_counts'):
        return db.get_b_status_counts()
    try:
        with db.read_connection() as conn:
            valid = conn.execute(
                "SELECT COUNT(*) FROM b_strm_files WHERE status='valid'").fetchone()[0]
            orphan = conn.execute(
                "SELECT COUNT(*) FROM b_strm_files WHERE status='orphan'").fetchone()[0]
            unknown = conn.execute(
                "SELECT COUNT(*) FROM b_strm_files WHERE status NOT IN ('valid','orphan') OR status IS NULL"
            ).fetchone()[0]
        return {"valid": valid, "orphan": orphan, "unknown": unknown}
    except Exception:
        return {"valid": 0, "orphan": 0, "unknown": 0}


def _db_get_db_file_size(db) -> int:
    """获取数据库文件大小。优先使用 db 方法，回退到 os.path.getsize。"""
    if hasattr(db, 'get_db_file_size'):
        return db.get_db_file_size()
    # 回退：尝试从 db 对象获取文件路径
    db_path = getattr(db, '_db_path', None) or getattr(db, 'db_path', None)
    if db_path and os.path.isfile(db_path):
        try:
            return os.path.getsize(db_path)
        except OSError:
            pass
    return 0


def _get_records_paginated(handler, area: str, page: int = 1, 
                           page_size: int = 100, search: str = "") -> dict:
    """获取指定区域的分页记录（SQL 级别分页）。
    
    返回 {total, page, page_size, records}
    """
    db = handler.webui._db
    offset = (page - 1) * page_size
    search_params: tuple[str, ...] = ()
    
    try:
        if area == "a":
            count_sql = "SELECT COUNT(*) FROM a_strm_files"
            query_sql = "SELECT local_path, webdav_path, parent_webdav_path, updated_at FROM a_strm_files"
            if search:
                count_sql += " WHERE local_path LIKE ? OR webdav_path LIKE ?"
                query_sql += " WHERE local_path LIKE ? OR webdav_path LIKE ?"
                search_params = (f"%{search}%", f"%{search}%")
            else:
                search_params = ()
            query_sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
            
        elif area == "b":
            count_sql = "SELECT COUNT(*) FROM b_strm_files"
            query_sql = "SELECT local_path, webdav_path, parent_webdav_path, source_a_path, fingerprint, status, updated_at FROM b_strm_files"
            if search:
                count_sql += " WHERE local_path LIKE ? OR webdav_path LIKE ?"
                query_sql += " WHERE local_path LIKE ? OR webdav_path LIKE ?"
                search_params = (f"%{search}%", f"%{search}%")
            else:
                search_params = ()
            query_sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
            
        elif area == "c":
            count_sql = "SELECT COUNT(*) FROM c_ghost_files"
            query_sql = "SELECT local_path, webdav_path, original_b_path, ghost_root, moved_at FROM c_ghost_files"
            if search:
                count_sql += " WHERE local_path LIKE ? OR webdav_path LIKE ?"
                query_sql += " WHERE local_path LIKE ? OR webdav_path LIKE ?"
                search_params = (f"%{search}%", f"%{search}%")
            else:
                search_params = ()
            query_sql += " ORDER BY moved_at DESC LIMIT ? OFFSET ?"
        else:
            return {"total": 0, "page": page, "page_size": page_size, "records": []}
        
        with db.read_connection() as conn:
            conn.row_factory = sqlite3.Row
            total = conn.execute(count_sql, search_params).fetchone()[0]
            rows = conn.execute(query_sql, search_params + (page_size, offset)).fetchall()
        
        # sqlite3.Row supports both index and named access; convert to dicts
        records = [dict(r) for r in rows]
        
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "records": records,
        }
    except Exception as e:
        logging.error("分页查询 %s 区记录失败: %s", area, e)
        return {"total": 0, "page": page, "page_size": page_size, "records": []}


# ============================================================
# Dashboard / Area / Records / Logs / Config 处理器
# ============================================================

def handle_dashboard(handler) -> None:
    """处理 GET /api/dashboard"""
    db = handler.webui._db
    try:
        counts = _db_get_table_counts(db)
        b_status = _db_get_b_status_counts(db)
        db_size = _db_get_db_file_size(db)
        handler._send_json({
            "a_count": counts.get("a_strm_files", 0),
            "b_count": counts.get("b_strm_files", 0),
            "c_count": counts.get("c_ghost_files", 0),
            "b_valid": b_status.get("valid", 0),
            "b_orphan": b_status.get("orphan", 0),
            "b_unknown": b_status.get("unknown", 0),
            "tmdb_configured": bool(handler.webui._tmdb_client),
            # 遗留字段（保持向后兼容）
            "table_counts": counts,
            "b_status_counts": b_status,
            "db_file_size": db_size,
            "db_file_size_human": _human_size(db_size),
            "uptime": time.time() - handler.webui._start_time,
        })
    except Exception as e:
        handler._send_json({"error": str(e)}, 500)


def handle_records_api(handler, params) -> None:
    """处理 GET /api/records?area=a&page=1&page_size=100&search=xxx
    
    SQL 级分页的记录查询接口。
    返回 {total, page, page_size, records}
    """
    area = params.get("area", ["a"])[0]
    if area not in ("a", "b", "c"):
        handler._send_json({"error": "invalid area"}, 400)
        return
    page = _safe_int(params.get("page", ["1"])[0], 1)
    page_size = min(_safe_int(params.get("page_size", ["100"])[0], 100), 500)
    search = params.get("search", [""])[0].strip()
    
    result = _get_records_paginated(handler, area, page=page, 
                                     page_size=page_size, search=search)
    handler._send_json(result)


def _get_media_groups_paginated(handler, area: str, kind_filter: str, 
                                 q: str, sort_key: str, sort_order: str,
                                 page: int, page_size: int) -> dict:
    """SQL 级分页的媒体分组查询。
    
    返回 {total, page, page_size, media_items, kind_counts}
    """
    db = handler.webui._db
    
    # 确定表名和时间字段
    if area == "a":
        table = "a_strm_files"
        time_field = "updated_at"
    elif area == "b":
        table = "b_strm_files"
        time_field = "updated_at"
    elif area == "c":
        table = "c_ghost_files"
        time_field = "moved_at"
    else:
        return {"total": 0, "page": page, "page_size": page_size, 
                "media_items": [], "kind_counts": {}}
    
    # SQL 提取 kind 的逻辑（与 Python _media_info 一致）
    # 根据路径中的分类目录判断（番剧/电影/其他）
    kind_sql = f"""
        CASE 
            WHEN webdav_path LIKE '%/电影/%' OR webdav_path LIKE '%/movies/%' OR webdav_path LIKE '%/movie/%' 
                 OR local_path LIKE '%/电影/%' OR local_path LIKE '%\\电影\\%' 
                 OR local_path LIKE '%/movies/%' OR local_path LIKE '%/movie/%'
            THEN '电影'
            WHEN webdav_path LIKE '%/番剧/%' OR webdav_path LIKE '%/anime/%' OR webdav_path LIKE '%/动漫/%' OR webdav_path LIKE '%/动画/%'
                 OR local_path LIKE '%/番剧/%' OR local_path LIKE '%\\番剧\\%'
                 OR local_path LIKE '%/anime/%' OR local_path LIKE '%/动漫/%' OR local_path LIKE '%/动画/%'
            THEN '番剧'
            ELSE '其他'
        END
    """
    
    # 提取媒体名称：找到分类目录后的第一级目录名
    # 例：/strm/番剧/进击的巨人/S01/ep01.strm -> 进击的巨人
    # 例：/strm/电影/合集/电影1.strm -> 合集
    # 使用 INSTR 和 SUBSTR 提取
    media_name_sql = f"""
        CASE 
            WHEN {kind_sql} = '番剧' THEN
                -- 番剧：找到 "番剧/" 后的第一级目录
                CASE 
                    WHEN INSTR(REPLACE(webdav_path, '\\', '/'), '/番剧/') > 0 THEN
                        SUBSTR(
                            SUBSTR(REPLACE(webdav_path, '\\', '/'), INSTR(REPLACE(webdav_path, '\\', '/'), '/番剧/') + 4),
                            1,
                            INSTR(SUBSTR(REPLACE(webdav_path, '\\', '/'), INSTR(REPLACE(webdav_path, '\\', '/'), '/番剧/') + 4) || '/', '/') - 1
                        )
                    WHEN INSTR(REPLACE(local_path, '\\', '/'), '/番剧/') > 0 THEN
                        SUBSTR(
                            SUBSTR(REPLACE(local_path, '\\', '/'), INSTR(REPLACE(local_path, '\\', '/'), '/番剧/') + 4),
                            1,
                            INSTR(SUBSTR(REPLACE(local_path, '\\', '/'), INSTR(REPLACE(local_path, '\\', '/'), '/番剧/') + 4) || '/', '/') - 1
                        )
                    ELSE '未分类'
                END
            WHEN {kind_sql} = '电影' THEN
                -- 电影：找到 "电影/" 后的第一级目录
                CASE 
                    WHEN INSTR(REPLACE(webdav_path, '\\', '/'), '/电影/') > 0 THEN
                        SUBSTR(
                            SUBSTR(REPLACE(webdav_path, '\\', '/'), INSTR(REPLACE(webdav_path, '\\', '/'), '/电影/') + 4),
                            1,
                            INSTR(SUBSTR(REPLACE(webdav_path, '\\', '/'), INSTR(REPLACE(webdav_path, '\\', '/'), '/电影/') + 4) || '/', '/') - 1
                        )
                    WHEN INSTR(REPLACE(local_path, '\\', '/'), '/电影/') > 0 THEN
                        SUBSTR(
                            SUBSTR(REPLACE(local_path, '\\', '/'), INSTR(REPLACE(local_path, '\\', '/'), '/电影/') + 4),
                            1,
                            INSTR(SUBSTR(REPLACE(local_path, '\\', '/'), INSTR(REPLACE(local_path, '\\', '/'), '/电影/') + 4) || '/', '/') - 1
                        )
                    ELSE '未分类'
                END
            ELSE
                -- 其他：取第一级目录
                CASE 
                    WHEN INSTR(SUBSTR(REPLACE(webdav_path, '\\', '/'), 2), '/') > 0 THEN
                        SUBSTR(REPLACE(webdav_path, '\\', '/'), 2, INSTR(SUBSTR(REPLACE(webdav_path, '\\', '/'), 2), '/') - 1)
                    ELSE REPLACE(webdav_path, '\\', '/')
                END
        END
    """
    
    # 构建基础查询条件
    base_where = ""
    params_list = []
    
    if q:
        base_where = f" AND (local_path LIKE ? OR webdav_path LIKE ?)"
        params_list.extend([f"%{q}%", f"%{q}%"])
    
    # 查询 kind_counts（分类统计）
    kind_counts_sql = f"""
        SELECT 
            CASE 
                WHEN {kind_sql} = '番剧' THEN 'anime'
                WHEN {kind_sql} = '电影' THEN 'movie'
                ELSE 'other'
            END AS kind_category,
            COUNT(DISTINCT ({media_name_sql})) AS count
        FROM {table}
        WHERE 1=1 {base_where}
        GROUP BY kind_category
    """
    
    # 查询媒体分组（分页）
    offset = (page - 1) * page_size
    
    # 排序
    order_clause = "ORDER BY "
    if sort_key == "count":
        order_clause += "file_count " + ("DESC" if sort_order == "desc" else "ASC")
    elif sort_key == "time":
        order_clause += "latest_ts " + ("DESC" if sort_order == "desc" else "ASC")
    elif sort_key == "kind":
        order_clause += "kind " + ("DESC" if sort_order == "desc" else "ASC")
    else:  # name
        order_clause += "media_name " + ("DESC" if sort_order == "desc" else "ASC")
    
    # 筛选 kind
    kind_where = ""
    if kind_filter != "all":
        if kind_filter == "anime":
            kind_where = f" AND {kind_sql} = '番剧'"
        elif kind_filter == "movie":
            kind_where = f" AND {kind_sql} = '电影'"
        elif kind_filter == "other":
            kind_where = f" AND {kind_sql} = '其他'"
    
    media_groups_sql = f"""
        SELECT 
            {kind_sql} AS kind,
            {media_name_sql} AS media_name,
            COUNT(*) AS file_count,
            MAX({time_field}) AS latest_ts
        FROM {table}
        WHERE 1=1 {base_where} {kind_where}
        GROUP BY kind, media_name
        {order_clause}
        LIMIT ? OFFSET ?
    """
    
    # 总数查询
    total_sql = f"""
        SELECT COUNT(DISTINCT ({kind_sql} || '|' || {media_name_sql})) AS total
        FROM {table}
        WHERE 1=1 {base_where} {kind_where}
    """
    
    try:
        with db.read_connection() as conn:
            conn.row_factory = sqlite3.Row
            # 查询 kind_counts
            kind_counts_rows = conn.execute(kind_counts_sql, params_list).fetchall()
            kind_counts = {}
            for row in kind_counts_rows:
                kind_counts[row[0]] = row[1]
            
            # 查询总数
            total = conn.execute(total_sql, params_list).fetchone()[0]
            
            # 查询媒体分组
            media_rows = conn.execute(media_groups_sql, params_list + [page_size, offset]).fetchall()
            
            media_items = []
            for row in media_rows:
                media_items.append({
                    "name": row["media_name"] or "未分类",
                    "kind": row["kind"],
                    "count": row["file_count"],
                    "season": "",  # 季信息需要 Python 后处理
                    "latest_ts": row["latest_ts"] or 0,
                })
            
            return {
                "total": total,
                "page": page,
                "page_size": page_size,
                "media_items": media_items,
                "kind_counts": kind_counts,
            }
    except Exception as e:
        logging.error("SQL 分页查询媒体分组失败: %s", e)
        return {"total": 0, "page": page, "page_size": page_size, 
                "media_items": [], "kind_counts": {}}


def handle_area(handler, area, params) -> None:
    """处理 GET /api/area/{area} — 区域列表，返回按媒体分组的统计摘要"""
    if area not in ("a", "b", "c"):
        handler._send_json({"error": "invalid area"}, 400)
        return

    kind_filter = params.get("kind", ["anime"])[0]
    q = params.get("q", [""])[0].strip().lower()
    sort_key = params.get("sort", ["name"])[0]
    sort_order = params.get("order", ["asc"])[0]
    page = _safe_int(params.get("page", ["1"])[0], 1)
    page_size = _safe_int(params.get("page_size", ["50"])[0], 50)

    kind_label_map = {
        "anime": "番剧",
        "movie": "电影",
        "other": "其他",
        "all": "全部"}

    # 使用 SQL 级分页查询
    result = _get_media_groups_paginated(
        handler, area, kind_filter, q, sort_key, sort_order, page, page_size
    )
    
    # 补充季信息（需要 Python 后处理）
    # 获取当前页的媒体名称列表，查询对应的季信息
    if result["media_items"]:
        db = handler.webui._db
        
        # 确定表名
        if area == "a":
            table = "a_strm_files"
        elif area == "b":
            table = "b_strm_files"
        else:
            table = "c_ghost_files"
        
        # 查询每个媒体的季信息
        for item in result["media_items"]:
            media_name = item["name"]
            # 查询该媒体的第一条记录的 local_path，用于提取季信息
            try:
                with db.read_connection() as conn:
                    row = conn.execute(
                        f"SELECT local_path FROM {table} WHERE local_path LIKE ? LIMIT 1",
                        (f"%{media_name}%",)
                    ).fetchone()
                    if row:
                        season = _extract_season_from_local_path(row[0])
                        item["season"] = season
            except Exception:
                pass
    
    total = result["total"]
    total_pages = max(1, ceil(total / page_size)) if total > 0 else 1
    
    handler._send_json({
        "area": area,
        "kind_label": kind_label_map.get(kind_filter, kind_filter),
        "kind_counts": result["kind_counts"],
        "media_items": result["media_items"],
        "total": total,
        "page": result["page"],
        "total_pages": total_pages,
        "page_size": result["page_size"],
    })


def handle_area_detail(handler, area, params) -> None:
    """处理 GET /api/area/{area}/detail — 区域详情，返回指定媒体的所有记录"""
    if area not in ("a", "b", "c"):
        handler._send_json({"error": "invalid area"}, 400)
        return

    media_name = params.get("media", [""])[0]
    sort_field = params.get("sort", ["local_path"])[0]
    sort_order = params.get("order", ["asc"])[0]
    page = _safe_int(params.get("page", ["1"])[0], 1)

    # 使用 SQL 级过滤，减少返回数据量
    db = handler.webui._db
    records = []
    search_params: tuple[str, ...] = ()
    try:
        if area == "a":
            query_sql = "SELECT local_path, webdav_path, parent_webdav_path, updated_at FROM a_strm_files"
            if media_name:
                query_sql += " WHERE local_path LIKE ? OR webdav_path LIKE ?"
                search_params = (f"%{media_name}%", f"%{media_name}%")
            else:
                search_params = ()
            with db.read_connection() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(query_sql, search_params).fetchall()
            records = [dict(r) for r in rows]
        elif area == "b":
            query_sql = "SELECT local_path, webdav_path, parent_webdav_path, source_a_path, fingerprint, status, updated_at FROM b_strm_files"
            if media_name:
                query_sql += " WHERE local_path LIKE ? OR webdav_path LIKE ?"
                search_params = (f"%{media_name}%", f"%{media_name}%")
            else:
                search_params = ()
            with db.read_connection() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(query_sql, search_params).fetchall()
            records = [dict(r) for r in rows]
        elif area == "c":
            query_sql = "SELECT local_path, webdav_path, original_b_path, ghost_root, moved_at FROM c_ghost_files"
            if media_name:
                query_sql += " WHERE local_path LIKE ? OR webdav_path LIKE ?"
                search_params = (f"%{media_name}%", f"%{media_name}%")
            else:
                search_params = ()
            with db.read_connection() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(query_sql, search_params).fetchall()
            records = [dict(r) for r in rows]
    except Exception as e:
        logging.error("查询 %s 区详情失败: %s", area, e)

    local_root = ""
    webdav_root = ""
    if records:
        local_root = _compute_media_root(records[0].get("local_path", ""))
        webdav_root = _compute_media_root(records[0].get("webdav_path", ""))

    # 排序（全量排序后再分页）
    reverse = sort_order == "desc"
    if records and sort_field in records[0]:
        records.sort(key=lambda r: r.get(sort_field, ""), reverse=reverse)

    total = len(records)
    total_pages = max(1, ceil(total / PAGE_SIZE)) if total else 1
    page = max(1, min(page, total_pages))
    start = (page - 1) * PAGE_SIZE
    page_records = records[start:start + PAGE_SIZE]

    # 按季分组
    seasons_map: dict[str, list[dict]] = {}
    for rec in page_records:
        label = _extract_season_from_local_path(
            rec.get("local_path", "")) or "默认"
        seasons_map.setdefault(label, []).append(rec)

    seasons = [{"label": lbl, "records": recs}
               for lbl, recs in seasons_map.items()]

    handler._send_json({
        "area": area,
        "media": media_name,
        "local_root": local_root,
        "webdav_root": webdav_root,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "seasons": seasons,
    })


def handle_logs_api(handler, params: dict) -> None:
    """处理 GET /api/logs"""
    lines_req = _safe_int(params.get("lines", ["200"])[0], 200)

    # 确定日志文件路径
    log_file = None
    cfg = handler.webui._config
    cfg_log = getattr(cfg, 'log', None)
    if cfg_log and hasattr(cfg_log, 'file'):
        log_file = Path(cfg_log.file)
    # 回退：webui._log_file
    if log_file is None or not log_file.exists():
        fallback = getattr(handler.webui, '_log_file', None)
        if fallback:
            log_file = Path(fallback)

    if not log_file or not log_file.exists():
        handler._send_json({"lines": [], "count": 0})
        return
    try:
        with log_file.open("r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = all_lines[-lines_req:]
        handler._send_json({"lines": [line.rstrip() for line in tail],
                            "count": len(tail)})
    except Exception as e:
        handler._send_json({"error": str(e)}, 500)


def handle_config_api(handler) -> None:
    """处理 GET /api/config — 归一化配置字段，兼容 WebUIConfig 和 AppConfig

    TMDB 配置优先从 DB (webui_config scope=tmdb) 读取，
    如果 DB 无数据则回退到内存 TmdbConfig 对象。
    """
    cfg = handler.webui._config
    tmdb_client = handler.webui._tmdb_client
    tmdb_cfg = getattr(cfg, "tmdb", None)

    # 尝试从 DB 读取 TMDB 配置
    _wdb = getattr(handler.webui, '_watchlist_db', None)
    db_tmdb_cfg = _wdb.get_all_config("tmdb") if _wdb else {}

    # TMDB token 相关 — DB 优先
    token = db_tmdb_cfg.get("access_token", "") or (
        getattr(tmdb_cfg, "access_token", "") if tmdb_cfg else "")
    token_preview = (token[:16] + "...") if len(token) > 16 else (token or "")

    # 数据库文件路径 — 兼容两种配置结构
    # AppConfig: cfg.local.db_file
    db_file = ""
    local_cfg = getattr(cfg, "local", None)
    if local_cfg and hasattr(local_cfg, "db_file"):
        db_file = local_cfg.db_file
    elif hasattr(cfg, "db_file"):
        db_file = cfg.db_file

    # 路径配置
    paths_cfg = getattr(cfg, "paths", None)
    b_root = getattr(paths_cfg, "b_root", "") if paths_cfg else ""
    c_root = getattr(paths_cfg, "c_root", "") if paths_cfg else ""
    strm_engine_paths = getattr(
        paths_cfg,
        "strm_engine_paths",
        []) if paths_cfg else []
    refresh_paths_val = getattr(
        paths_cfg,
        "refresh_paths",
        []) if paths_cfg else []

    # a_folders
    a_folders = getattr(cfg, "a_folders", [])

    # WebDAV
    webdav_cfg = getattr(cfg, "webdav", None)
    webdav_host = getattr(webdav_cfg, "host", "") if webdav_cfg else ""
    webdav_user = getattr(webdav_cfg, "user", "") if webdav_cfg else ""
    webdav_password = bool(
        getattr(
            webdav_cfg,
            "password",
            "")) if webdav_cfg else False

    # Refresh
    refresh_cfg = getattr(cfg, "refresh", None)
    refresh_enabled = getattr(
        refresh_cfg,
        "enabled",
        True) if refresh_cfg else True
    refresh_interval = getattr(
        refresh_cfg,
        "interval_seconds",
        300) if refresh_cfg else 300

    # Behavior
    behavior_cfg = getattr(cfg, "behavior", None)
    behavior_action = getattr(
        behavior_cfg,
        "action",
        "MOVE") if behavior_cfg else "MOVE"
    ghost_protect_seconds = getattr(
        behavior_cfg,
        "ghost_protect_seconds",
        300) if behavior_cfg else 300

    # WebUI 配置
    webui_cfg = getattr(cfg, "webui", None)
    webui_port = getattr(webui_cfg, "port", 8579) if webui_cfg else 8579
    webui_bind = getattr(
        webui_cfg,
        "bind",
        "0.0.0.0") if webui_cfg else "0.0.0.0"

    # TMDB 配置字段 — DB 优先，回退到内存 TmdbConfig
    tmdb_language = db_tmdb_cfg.get("language", "") or (
        getattr(tmdb_cfg, "language", "zh-CN") if tmdb_cfg else "zh-CN")
    tmdb_host = db_tmdb_cfg.get("host", "") or (
        getattr(tmdb_cfg, "host", "") if tmdb_cfg else "")
    tmdb_api_key = db_tmdb_cfg.get("api_key", "") or (
        getattr(tmdb_cfg, "api_key", "") if tmdb_cfg else "")
    tmdb_proxy_http = db_tmdb_cfg.get("proxy_http", "") or (
        getattr(tmdb_cfg, "proxy_http", "") if tmdb_cfg else "")
    tmdb_proxy_enabled_raw = db_tmdb_cfg.get("proxy_enabled", "")
    if tmdb_proxy_enabled_raw != "":
        tmdb_proxy_enabled = str(
            tmdb_proxy_enabled_raw).lower() in ("true", "1", "yes")
    else:
        tmdb_proxy_enabled = getattr(
            tmdb_cfg,
            "proxy_enabled",
            False) if tmdb_cfg else False
    tmdb_watchlist_db = db_tmdb_cfg.get("watchlist_db", "") or (
        getattr(tmdb_cfg, "watchlist_db", "") if tmdb_cfg else "")
    # TMDB Watchlist 启用/禁用开关 — DB 优先，默认启用
    tmdb_watchlist_enabled_raw = db_tmdb_cfg.get("watchlist_enabled", "")
    if tmdb_watchlist_enabled_raw != "":
        tmdb_watchlist_enabled = str(
            tmdb_watchlist_enabled_raw).lower() in ("true", "1", "yes")
    else:
        tmdb_watchlist_enabled = True

    handler._send_json({
        # 数据库 & WebUI
        "db_file": db_file,
        "db_exists": os.path.isfile(db_file) if db_file else False,
        "webui_port": webui_port,
        "webui_bind": webui_bind,
        # TMDB 认证
        "tmdb_configured": bool(tmdb_client),
        "tmdb_token_preview": token_preview,
        "tmdb_token": token,
        "tmdb_language": tmdb_language,
        "tmdb_host": tmdb_host,
        "tmdb_api_key": tmdb_api_key,
        "tmdb_api_key_configured": bool(tmdb_api_key),
        "tmdb_proxy": _resolve_tmdb_proxy(cfg),
        "tmdb_proxy_http": tmdb_proxy_http,
        "tmdb_proxy_enabled": tmdb_proxy_enabled,
        "tmdb_account_id": tmdb_client.account_id if tmdb_client else None,
        "tmdb_watchlist_db": tmdb_watchlist_db,
        "tmdb_watchlist_enabled": tmdb_watchlist_enabled,
        # 路径 & 文件夹
        "b_root": b_root,
        "c_root": c_root,
        "a_folders": a_folders,
        "strm_engine_paths": strm_engine_paths,
        "refresh_paths": refresh_paths_val,
        # WebDAV
        "webdav_host": webdav_host,
        "webdav_user": webdav_user,
        "webdav_password": webdav_password,
        # Refresh
        "refresh_enabled": refresh_enabled,
        "refresh_interval": refresh_interval,
        # Behavior
        "behavior_action": behavior_action,
        "ghost_protect_seconds": ghost_protect_seconds,
    })


# ============================================================
# 主程序控制 API
# ============================================================

def _handle_main_status(handler, webui_server) -> bool:
    """GET /api/main/status — 获取主程序运行状态"""
    if not webui_server:
        handler._send_json({"running": False, "uptime": None})
        return True
    
    status = webui_server.get_main_status()
    handler._send_json(status)
    return True


def _handle_main_start(handler, webui_server, body: bytes) -> bool:
    """POST /api/main/start — 启动主程序"""
    if not webui_server:
        handler._send_json({"success": False, "message": "WebUI 服务器未初始化"}, 500)
        return True
    
    result = webui_server.start_main()
    status_code = 200 if result.get("success") else 500
    handler._send_json(result, status_code)
    return True


def _handle_main_stop(handler, webui_server) -> bool:
    """POST /api/main/stop — 停止主程序"""
    if not webui_server:
        handler._send_json({"success": False, "message": "WebUI 服务器未初始化"}, 500)
        return True
    
    result = webui_server.stop_main()
    status_code = 200 if result.get("success") else 500
    handler._send_json(result, status_code)
    return True

