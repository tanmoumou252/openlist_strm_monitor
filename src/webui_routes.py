"""
共享 TMDB 路由模块 — 从 webui.py 提取。

供 webui.py 和 test_webui.py 共同引用，避免循环依赖。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tmdb_client import TmdbClient


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
        size /= 1024
    return f"{size:.1f} TB"


def _h(value: object) -> str:
    """HTML 转义"""
    import html
    return html.escape(str(value if value is not None else ""), quote=True)


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


def _safe_int(val: str | None, default: int = 0) -> int:
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
        result = None
        if webui_server:
            with webui_server._match_refresh_lock:
                running = webui_server._match_refresh_running
                result = webui_server._match_refresh_result
        handler._send_json({
            "running": running,
            "result": result,
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

    # TMDB 操作日志路由（无需 TMDB 客户端）
    if path == "/api/tmdb/logs":
        if webui_server and hasattr(webui_server, '_db') and webui_server._db:
            limit_val = _safe_int(params.get("limit", ["100"])[0], 100)
            limit_val = max(1, min(limit_val, 500))
            try:
                logs = webui_server._db.get_tmdb_logs(limit=limit_val)
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
        if not tmdb_client:
            handler._send_json({"error": "TMDB 未配置"}, 503)
            return True
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
