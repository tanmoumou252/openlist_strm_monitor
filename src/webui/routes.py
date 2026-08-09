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

import html as html_module
import ipaddress
import json
import logging
import datetime as _dt
import re

import os
import secrets
import socket
import sqlite3
import sys
import threading
import time
import urllib.request
from math import ceil
from pathlib import Path
from typing import TYPE_CHECKING

from watchlist_match import (
    _compute_media_root, _extract_season_from_local_path,
)
from config import normalize_local_root
from utils import escape_like
from utils.password_utils import hash_password

if TYPE_CHECKING:
    from tmdb_client import TmdbClient
    from database import Database

# ============================================================
# 工具函数
# ============================================================

def _is_lan_ip(ip: str) -> bool:
    """判断 IP 是否为局域网地址（含 localhost 与局域网 IPv6）。

    用显式局域网范围替代 is_private。is_private 对 TEST-NET
    （203.0.113.0/24，RFC 5737 文档保留地址）也返回 True，导致公网文档地址被
    误判为局域网而放行。显式列出 RFC1918 与 IPv6 ULA（fc00::/7）+ loopback +
    link-local，避免 is_private 的宽泛判定，同时正确识别局域网 IPv6。
    """
    if ip in ("127.0.0.1", "::1", "localhost"):
        return True
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if addr.is_loopback or addr.is_link_local:
        return True
    # 兼容 IPv4-mapped IPv6（::ffff:192.168.1.5）
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if addr.version == 4:
        return (addr in ipaddress.ip_network('10.0.0.0/8')
                or addr in ipaddress.ip_network('172.16.0.0/12')
                or addr in ipaddress.ip_network('192.168.0.0/16'))
    # IPv6 局域网：唯一本地地址 ULA (fc00::/7)
    return addr in ipaddress.ip_network('fc00::/7')

def _human_size(size: int) -> str:
    """人类可读的文件大小"""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size //= 1024
    return f"{size:.1f} TB"

# CSV 单元格安全——防止公式注入
# 以 =, +, -, @ 开头的文本在电子表格中会被解释为公式，添加前缀使其作为文本处理
_CSV_FORMULA_PREFIXES = frozenset("=+-@")


def _csv_safe_text(val: str) -> str:
    """对 CSV 文本单元格进行公式注入防护。"""
    if val and isinstance(val, str) and val[0] in _CSV_FORMULA_PREFIXES:
        return "\t" + val
    return val

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

    Windows 上使用 SO_EXCLUSIVEADDRUSE 防止多进程绑定同一端口。
    Unix 上使用 SO_REUSEADDR 支持重启时 TIME_WAIT 状态复用。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if sys.platform == 'win32':
                # Windows: 独占地址，防止多进程绑定
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                # Unix: 允许 TIME_WAIT 复用
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
    """后台执行待看列表同步，带完整的错误处理"""
    _wdb = getattr(server, '_watchlist_db', None)
    try:
        if not _wdb:
            raise RuntimeError("watchlist_db 未初始化")
        if not server._tmdb_client:
            raise RuntimeError("tmdb_client 未初始化")

        server._watchlist_db.sync(server._tmdb_client, force=True)
        logging.info("[TMDB] 后台同步完成")

        if _wdb:
            _wdb.log_tmdb_operation("sync", "success", "后台同步完成")
    except Exception as e:
        error_msg = f"后台同步失败: {e}"
        logging.error("[TMDB] %s", error_msg, exc_info=True)
        if _wdb:
            try:
                _wdb.log_tmdb_operation("sync", "error", error_msg)
            except Exception as log_err:
                logging.error("[TMDB] 日志记录失败: %s", log_err)
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
                    stats = _wdb.get_match_statistics()
                    result["match_uncomputed"] = stats.get("uncomputed", 0)
                    result["match_total"] = stats.get("total", 0)
                except Exception as e:
                    logging.warning("[TMDB] 获取匹配统计失败: %s", e)
                    result["match_uncomputed"] = 0
                    result["match_total"] = 0
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
    # 免鉴权（登录前需显示头像），但需输入校验防止路径注入
    if path == "/api/tmdb/avatar":
        avatar_hash = params.get("hash", [""])[0]
        if not avatar_hash:
            handler._send_json({"error": "missing hash"}, 400)
            return True
        # avatar_hash 应为十六进制字符串（MD5 或 SHA 哈希）- 防止路径注入
        if not all(c in "0123456789abcdefABCDEF" for c in avatar_hash):
            handler._send_json({"error": "invalid hash format (must be hex)"}, 400)
            return True
        _host = handler.webui._config.tmdb.host
        if _host:
            avatar_url = f"{_host.rstrip('/')}/avatar/{avatar_hash}"
        else:
            avatar_url = f"https://www.gravatar.com/avatar/{avatar_hash}?d=identicon&s=80"
        try:
            # 添加大小限制（10MB），防止内存耗尽 DoS
            MAX_IMG_SIZE = 10 * 1024 * 1024  # 10MB
            ava_req = urllib.request.Request(
                avatar_url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
            )
            opener = _build_img_opener(handler, use_proxy=not bool(_host))
            # with 确保 resp 在 413 提前返回/正常返回/异常三路径下均 close，防 FD 泄漏
            with opener.open(ava_req, timeout=10) as resp:
                # 检查 Content-Length（如果服务端返回）
                content_length = int(resp.headers.get("Content-Length", 0))
                if content_length > MAX_IMG_SIZE:
                    handler._send_json({"error": "Image too large"}, 413)
                    return True

                # 分块读取，防止无界 read() 导致内存耗尽 DoS
                img_data = resp.read(MAX_IMG_SIZE + 1)
                if len(img_data) > MAX_IMG_SIZE:
                    handler._send_json({"error": "Image too large"}, 413)
                    return True

                handler.send_response(200)
                handler.send_header("Content-Type", "image/png")
                handler.send_header("Content-Length", str(len(img_data)))
                handler.send_header("Cache-Control", "public, max-age=86400")
                handler.send_header("X-Content-Type-Options", "nosniff")
                handler.send_header("X-Frame-Options", "DENY")
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
        # poster_path 应为 TMDB 路径格式（/t/p/xxx 或类似），限制字符集防注入
        # 追加 '..' 检查：字符类允许连续点，需显式拒绝路径穿越
        if not re.match(r'^/[A-Za-z0-9._/\-]+$', poster_path) or '..' in poster_path:
            handler._send_json({"error": "invalid poster path format"}, 400)
            return True
        if width not in ("92", "154", "185", "342", "500", "780"):
            width = "342"
        img_base = (tmdb_client.image_base() if tmdb_client
                    else "https://image.tmdb.org/t/p")
        poster_url = f"{img_base}/w{width}{poster_path}"
        logging.debug("[TMDB] Poster Request - path: %s, url: %s", poster_path, poster_url)
        try:
            # 添加大小限制（10MB），防止内存耗尽 DoS
            MAX_IMG_SIZE = 10 * 1024 * 1024  # 10MB
            poster_req = urllib.request.Request(
                poster_url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
            )
            opener = _build_img_opener(handler, use_proxy=True)
            # with 确保 resp 在 413 提前返回/正常返回/异常三路径下均 close，防 FD 泄漏
            with opener.open(poster_req, timeout=15) as resp:
                # 检查 Content-Length（如果服务端返回）
                content_length = int(resp.headers.get("Content-Length", 0))
                if content_length > MAX_IMG_SIZE:
                    handler._send_json({"error": "Image too large"}, 413)
                    return True

                # 分块读取，防止无界 read() 导致内存耗尽 DoS
                img_data = resp.read(MAX_IMG_SIZE + 1)
                if len(img_data) > MAX_IMG_SIZE:
                    handler._send_json({"error": "Image too large"}, 413)
                    return True

                content_type = resp.headers.get("Content-Type", "image/jpeg")
                # 白名单 Content-Type，防止通过 Content-Type 头注入恶意内容
                allowed_types = {"image/jpeg", "image/png", "image/webp"}
                if content_type not in allowed_types:
                    content_type = "image/jpeg"

                handler.send_response(200)
                handler.send_header("Content-Type", content_type)
                handler.send_header("Content-Length", str(len(img_data)))
                handler.send_header("Cache-Control", "public, max-age=604800")
                handler.send_header("X-Content-Type-Options", "nosniff")
                handler.send_header("X-Frame-Options", "DENY")
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

    # TMDB 操作日志下载
    if path == "/api/tmdb/logs/download":
        _wdb = getattr(webui_server, '_watchlist_db', None)
        if webui_server and _wdb:
            try:
                all_logs = _wdb.get_tmdb_logs(limit=100000)
                lines = []
                for log in all_logs:
                    ts = log.get('ts', '')
                    op = log.get('op', '')
                    level = log.get('level', '')
                    msg = log.get('msg', '')
                    if ts:
                        ts_str = _dt.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
                    else:
                        ts_str = '-'
                    lines.append(f"[{ts_str}] [{level.upper()}] [{op}] {msg}")
                content = '\n'.join(lines).encode('utf-8')
                handler.send_response(200)
                handler.send_header('Content-Type', 'text/plain; charset=utf-8')
                handler.send_header('Content-Disposition', 'attachment; filename="tmdb_operations.log"')
                handler.end_headers()
                handler.wfile.write(content)
            except Exception as e:
                logging.exception("[TMDB] 下载操作日志失败: %s", e)
                handler._send_json({"error": "internal_error"}, 500)
        else:
            handler._send_json({"error": "TMDB 日志不可用"}, 404)
        return True

    # CSV 导出 — 即使 TMDB 客户端未初始化，也可从 DB 缓存导出
    if path == "/api/tmdb/watchlist/export.csv":
        import csv
        import io
        all_items = (webui_server.get_watchlist_cached()
                     if webui_server and hasattr(webui_server, 'get_watchlist_cached')
                     else [])
# CSV 使用的 items 没有经过 _STATUS_MAP 映射（watchlist/movies?all=1 路由才有）
        # 在此补上映射；_status 不存在时通过 match_status 回退
        # 在 CSV 写入循环中内联计算，避免原地修改共享缓存
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["状态", "TMDB ID", "类型", "标题", "原标题", "发布日期", "评分"])
        for item in all_items:
            status = _STATUS_MAP.get(
                item.get("match_status", "uncomputed"), "out")
            media_type = item.get("_media_type", "movie")
            title = item.get("title") or item.get("name") or ""
            orig = item.get("original_title") or item.get(
                "original_name") or ""
            date = item.get("release_date") or item.get("first_air_date") or ""
            # 处理 vote_average=None，避免 f"{None:.1f}" 抛 TypeError
            # 强制转 float，防止 vote_average 为字符串（如 "8.5"）
            # 时 f"{rating:.1f}" 抛 TypeError 导致整次 CSV 导出崩溃。
            try:
                rating = float(item.get("vote_average")) \
                    if item.get("vote_average") is not None else 0.0
            except (TypeError, ValueError):
                rating = 0.0
            status_label = {
                "in": "已收录",
                "out": "待看",
                "que": "有疑问"}.get(
                status,
                "待看")
            writer.writerow([_csv_safe_text(status_label), item.get("id", ""), media_type,
                             _csv_safe_text(title), _csv_safe_text(orig), _csv_safe_text(date), f"{rating:.1f}"])
        csv_data = buf.getvalue().encode("utf-8-sig")

        # 直接返回 CSV 数据作为浏览器下载
        handler.send_response(200)
        handler.send_header("Content-Type", "text/csv; charset=utf-8")
        handler.send_header("Content-Disposition", "attachment; filename=watchlist.csv")
        handler.send_header("Content-Length", str(len(csv_data)))
        handler.end_headers()
        handler.wfile.write(csv_data)
        logging.info("[TMDB] CSV 导出 %d 项", len(all_items))
        return True

    if not tmdb_client:
        auth_hint = "TMDB 未配置 access_token 或 api_key，请在 config.toml 的 [tmdb] 段配置"
        handler._send_json({"error": auth_hint}, 503)
        return True

    if path == "/api/tmdb/watchlist/movies":
        search_query = params.get("q", [""])[0].strip()
        if params.get("all", ["0"])[0] == "1":
            if webui_server and hasattr(webui_server, 'get_watchlist_cached'):
                all_items = webui_server.get_watchlist_cached()
                items = [i for i in all_items if i.get(
                    "_media_type") == "movie"]
            else:
                items = tmdb_client.fetch_all_watchlist_movies()

            # 如果有搜索查询，使用 FTS5 过滤
            if search_query:
                _wdb = getattr(webui_server, '_watchlist_db', None)
                if _wdb:
                    try:
                        escaped_query = _escape_fts5_query(search_query)
                        with _wdb._conn() as conn:
                            fts_ids = conn.execute(
                                "SELECT rowid FROM movies_fts WHERE movies_fts MATCH ?",
                                (escaped_query,)
                            ).fetchall()
                            fts_id_set = {row[0] for row in fts_ids}
                            items = [i for i in items if i.get("id") in fts_id_set]
                    except Exception as fts_err:
                        logging.warning("[TMDB] FTS5 搜索失败，回退到内存过滤: %s", fts_err)
                        # 回退到内存过滤
                        search_lower = search_query.lower()
                        items = [i for i in items if
                                search_lower in (i.get("title") or "").lower() or
                                search_lower in (i.get("original_title") or "").lower() or
                                search_lower in (i.get("overview") or "").lower()]

            # 附加 _status 映射字段和 _is_manual 标记
            for item in items:
                item["_status"] = _STATUS_MAP.get(
                    item.get("match_status", "uncomputed"), "out")
                item["_is_manual"] = bool(item.get("manual_override_at", 0) > 0)
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
                logging.exception("[TMDB] 获取电影待看列表失败: %s", e)
                handler._send_json({"error": "获取待看列表失败"}, 500)
                return True

            # 如果有搜索查询，使用 FTS5 过滤
            if search_query:
                _wdb = getattr(webui_server, '_watchlist_db', None)
                if _wdb:
                    try:
                        escaped_query = _escape_fts5_query(search_query)
                        with _wdb._conn() as conn:
                            fts_ids = conn.execute(
                                "SELECT rowid FROM movies_fts WHERE movies_fts MATCH ?",
                                (escaped_query,)
                            ).fetchall()
                            fts_id_set = {row[0] for row in fts_ids}
                            items = [i for i in items if i.get("id") in fts_id_set]
                    except Exception as fts_err:
                        logging.warning("[TMDB] FTS5 搜索失败，回退到内存过滤: %s", fts_err)
                        search_lower = search_query.lower()
                        items = [i for i in items if
                                search_lower in (i.get("title") or "").lower() or
                                search_lower in (i.get("original_title") or "").lower() or
                                search_lower in (i.get("overview") or "").lower()]

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
        search_query = params.get("q", [""])[0].strip()
        if params.get("all", ["0"])[0] == "1":
            if webui_server and hasattr(webui_server, 'get_watchlist_cached'):
                all_items = webui_server.get_watchlist_cached()
                items = [i for i in all_items if i.get("_media_type") == "tv"]
            else:
                items = tmdb_client.fetch_all_watchlist_tv()

            # 如果有搜索查询，使用 FTS5 过滤
            if search_query:
                _wdb = getattr(webui_server, '_watchlist_db', None)
                if _wdb:
                    try:
                        escaped_query = _escape_fts5_query(search_query)
                        with _wdb._conn() as conn:
                            fts_ids = conn.execute(
                                "SELECT rowid FROM tv_fts WHERE tv_fts MATCH ?",
                                (escaped_query,)
                            ).fetchall()
                            fts_id_set = {row[0] for row in fts_ids}
                            items = [i for i in items if i.get("id") in fts_id_set]
                    except Exception as fts_err:
                        logging.warning("[TMDB] FTS5 搜索失败，回退到内存过滤: %s", fts_err)
                        search_lower = search_query.lower()
                        items = [i for i in items if
                                search_lower in (i.get("name") or "").lower() or
                                search_lower in (i.get("original_name") or "").lower() or
                                search_lower in (i.get("overview") or "").lower()]

            # 附加 _status 映射字段和 _is_manual 标记
            for item in items:
                item["_status"] = _STATUS_MAP.get(
                    item.get("match_status", "uncomputed"), "out")
                item["_is_manual"] = bool(item.get("manual_override_at", 0) > 0)
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
                logging.exception("[TMDB] 获取剧集待看列表失败: %s", e)
                handler._send_json({"error": "获取待看列表失败"}, 500)
                return True

            # 如果有搜索查询，使用 FTS5 过滤
            if search_query:
                _wdb = getattr(webui_server, '_watchlist_db', None)
                if _wdb:
                    try:
                        escaped_query = _escape_fts5_query(search_query)
                        with _wdb._conn() as conn:
                            fts_ids = conn.execute(
                                "SELECT rowid FROM tv_fts WHERE tv_fts MATCH ?",
                                (escaped_query,)
                            ).fetchall()
                            fts_id_set = {row[0] for row in fts_ids}
                            items = [i for i in items if i.get("id") in fts_id_set]
                    except Exception as fts_err:
                        logging.warning("[TMDB] FTS5 搜索失败，回退到内存过滤: %s", fts_err)
                        search_lower = search_query.lower()
                        items = [i for i in items if
                                search_lower in (i.get("name") or "").lower() or
                                search_lower in (i.get("original_name") or "").lower() or
                                search_lower in (i.get("overview") or "").lower()]

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
                    count = webui_server._watchlist_db.get_season_count(tmdb_id)
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
            credits = data.get("credits") or {}
            cast_raw = credits.get("cast", []) if isinstance(credits, dict) else []
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

    # 综合搜索：同时搜索电影和电视剧
    if path == "/api/tmdb/search":
        query = params.get("query", [""])[0].strip()
        if not query:
            handler._send_json({"error": "query is required"}, 400)
            return True

        try:
            # 同时搜索电影和电视剧
            movies = tmdb_client.search_movie(query, page=1)
            tv_shows = tmdb_client.search_tv(query, page=1)

            handler._send_json({
                "query": query,
                "movies": movies[:10],  # 限制返回数量
                "tv_shows": tv_shows[:10],
            })
        except Exception as e:
            logging.exception("[TMDB] 搜索失败: %s", e)
            handler._send_json({"error": "internal_error"}, 500)
        return True

    return False

# ============================================================
# 共享 TMDB POST 处理器
# ============================================================

from tmdb_watchlist_db import TmdbWatchlistDb  # noqa: E402
from watchlist_match import refresh_watchlist_match_state  # noqa: E402

def _handle_tmdb_configure(handler, webui_server, body: bytes) -> None:
    """处理 TMDB 配置更新请求。
    
    注意：本函数只持久化「实际生效的值」（applied 字典），不是原始请求体。
    调用方负责过滤与归一化，_save_tmdb_to_db 负责写入 DB。
    """
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
        applied = {}  # 只记录实际生效的值
        # watchlist_db 已从允许字段中移除，请求体含该键时返回 400
        if "watchlist_db" in data:
            handler._send_json(
                {"success": False, "error": "该路径配置已移除，请使用固定项目根路径"},
                400,
            )
            return
        for key in ("access_token", "api_key", "language", "host",
                    "csv_watchlist_file",
                    "fuzzy_threshold", "anime_min_ep_ratio",
                    "anime_max_season_diff", "watchlist_cache_ttl", "anime_min_season_ratio"):
            if key in data and data[key] is not None:
                val = data[key]
                # 空值守卫：access_token/api_key 已配置时跳过，避免空串覆盖
                if key in ("access_token", "api_key") and not val:
                    if getattr(tmdb_cfg, key, ""):
                        continue
                setattr(tmdb_cfg, key, val)
                applied[key] = val  # 记录实际生效的值
                changed = True
        # Proxy settings — 前端发送扁平字段
        if "proxy_http" in data:
            tmdb_cfg.proxy_http = data["proxy_http"] or ""
            # 兼容旧嵌套结构
            proxy_cfg = getattr(tmdb_cfg, "proxy", None)
            if proxy_cfg:
                proxy_cfg.http = data["proxy_http"] or ""
            applied["proxy_http"] = tmdb_cfg.proxy_http  # 归一化后的值
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
            applied["proxy_enabled"] = "true" if proxy_enabled else "false"  # 归一化
            changed = True
        # Watchlist enabled setting
        if "watchlist_enabled" in data:
            watchlist_enabled = str(data["watchlist_enabled"]).lower() in ("true", "1", "yes")
            # 只记录归一化后的值，由统一的 _save_tmdb_to_db 落库
            applied["watchlist_enabled"] = "true" if watchlist_enabled else "false"
            changed = True
        if changed:
            # 重新初始化 TMDB 客户端
            _handler_reinit_tmdb(webui_server, tmdb_cfg)
            # 保存到 DB（webui_config 表）——传实际生效值，不是原始请求体
            _save_tmdb_to_db(webui_server, applied)
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
        # M-13: 不回传原始异常信息
        handler._send_json({"success": False, "error": "保存失败"}, 500)

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

    # 获取项目根目录（用于 config.toml 兜底）
    project_root = (getattr(webui_server, '_project_root', None)
                    or Path(__file__).resolve().parent.parent.parent)

    # 获取 api_key，为空时从 config.toml 兜底
    api_key = getattr(tmdb_cfg, "api_key", "") or ""

    try:
        webui_server._tmdb_client = create_tmdb_client(
            access_token=getattr(tmdb_cfg, "access_token", "") or "",
            language=getattr(tmdb_cfg, "language", "zh-CN"),
            proxy=proxy,
            host=host,
            api_key=api_key,
        )
    except Exception as e:
        logging.warning("[TMDB] 重新初始化客户端失败: %s", e)
        webui_server._tmdb_client = None
    # 数据库路径固定在项目根，watchlist_db 字段已移除
    # 仅测试注入，生产固定项目根
    db_path = str(project_root / "tmdb_watchlist.db")
    ttl = float(getattr(tmdb_cfg, "watchlist_cache_ttl", 604800))
    try:
        webui_server._watchlist_db = TmdbWatchlistDb(db_path, ttl)
    except Exception as e:
        logging.warning("[TMDB] 待看列表数据库重建失败: %s", e)
        webui_server._watchlist_db = None

def _save_tmdb_to_db(webui_server, changes: dict) -> None:
    """保存 TMDB 配置到 DB webui_config 表（scope="tmdb"）。
    
    入参是「实际生效值」，不是原始请求体；调用方负责过滤与归一化。
    """
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
        # 统一脱敏，与 handle_config_api 对齐。所有敏感凭据
        # 只返回布尔值（已配置/未配置），不返回明文。
        _SENSITIVE_KEYS = {
            "ui": {"admin_password"},
            "tmdb": {"access_token", "api_key"},
            "openlist": {"webdav_password", "webdav_totp_secret"},
        }
        if isinstance(cfg, dict):
            sensitive = _SENSITIVE_KEYS.get(scope, set())
            for key in sensitive:
                if key in cfg:
                    cfg[key] = bool(cfg[key])
        handler._send_json({"success": True, "scope": scope, "config": cfg})
    except Exception as e:
        logging.exception("[WebUI] 读取配置失败 (scope=%s): %s", scope, e)
        handler._send_json({"success": False, "error": "internal_error"}, 500)

def _validate_strm_engines(value: str) -> bool:
    """校验 openlist.strm_engines 写入值。

    合法形态：JSON 数组，元素为 {"engine": str(非空),
    "monitored_paths": [str, ...]}。空数组 [] 合法。
    用于在写入 DB 前拦截异常载荷（如误把全部引擎/坏结构塞入）。
    """
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(parsed, list):
        return False
    for eng in parsed:
        if not isinstance(eng, dict):
            return False
        engine = eng.get("engine")
        monitored = eng.get("monitored_paths")
        if not isinstance(engine, str) or not engine:
            return False  # 拒绝空引擎条目
        if not isinstance(monitored, list):
            return False
        if not all(isinstance(p, str) for p in monitored):
            return False
    return True

def _validate_a_b_mappings(value: str) -> bool:
    """校验 openlist.a_b_mappings 写入值。

    合法形态：JSON 数组，元素为 {"a_root": str(非空), "b_root": str(非空), "label": str(可选)}。
    空数组 [] 合法（表示无引擎配置）。
    """
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(parsed, list):
        return False
    for m in parsed:
        if not isinstance(m, dict):
            return False
        a_root = m.get("a_root")
        b_root = m.get("b_root")
        if not isinstance(a_root, str) or not a_root:
            return False
        if not isinstance(b_root, str) or not b_root:
            return False
        label = m.get("label")
        if label is not None and not isinstance(label, str):
            return False
    return True

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
        # openlist.strm_engines 写入前护栏：拒绝非法形态（非用户显式选择的
        # 引擎子集、坏结构），防止未来再次误注入全部引擎或脏结构。
        # 若非法，整次拒绝（不部分写入，避免半写脏状态）。
        if scope == "openlist" and "strm_engines" in data:
            # 前端正常以 JSON 字符串形式发送 strm_engines；但若其它客户端以
            # 原生 JSON 对象/数组发送，避免 str() 产生 Python repr 误拒，
            # 这里统一规整为 JSON 字符串再交校验器（None/空串等交由校验器拒绝）。
            _raw_se = data["strm_engines"]
            if isinstance(_raw_se, str):
                _se_value = _raw_se
            elif _raw_se is None:
                _se_value = ""
            else:
                _se_value = json.dumps(_raw_se, ensure_ascii=False)
            if not _validate_strm_engines(_se_value):
                handler._send_json(
                    {"success": False,
                     "error": "STRM 引擎配置(strm_engines)格式不正确：请先在「STRM 引擎配置」区选择引擎入口，或清空未使用的引擎行后再保存。"},
                    400)
                return
            # 校验通过：回写规整后的合法 JSON 字符串，确保后续通用写循环
            # （str(val)）存入 DB 的是合法 JSON，而非原生对象被 str() 出来的 repr。
            data["strm_engines"] = _se_value

        # a_b_mappings 写入前护栏
        if scope == "openlist" and "a_b_mappings" in data:
            _raw_abm = data["a_b_mappings"]
            if isinstance(_raw_abm, str):
                _abm_value = _raw_abm
            elif _raw_abm is None:
                _abm_value = ""
            else:
                _abm_value = json.dumps(_raw_abm, ensure_ascii=False)
            if not _validate_a_b_mappings(_abm_value):
                handler._send_json(
                    {"success": False,
                     "error": "A↔B 映射配置(a_b_mappings)格式不正确：每个映射必须包含非空的 a_root 和 b_root 字段。"},
                    400)
                return
            data["a_b_mappings"] = _abm_value

        # ui scope 白名单过滤：拒绝未声明的 key，避免 LAN 内任意 key 污染配置表
        if scope == "ui":
            rejected = [k for k in data if k not in _UI_CONFIG_ALLOWED_KEYS]
            if rejected:
                logging.warning("[WebUI] ui scope 配置拒绝未声明 key: %s", rejected)
                handler._send_json(
                    {"success": False, "error": f"不允许的配置项: {rejected}"}, 403)
                return
        # admin_password 必须以 salt$iterations$hash 格式存储；
        # 若以明文写入（str(val)），登录端 split("$", 2) 会失败 → 永久锁死。
        # 在写入循环前统一处理，避免循环内重复哈希。
        # 密码长度校验（≥4）：防止管理员把 password 设为空串导致认证失效
        # 整数等非字符串类型必须拒绝，避免 str(val) 写入字面量导致永久锁死
        # 密码类型/长度校验，防空密码旁路认证
        if scope == "ui" and "admin_password" in data:
            _pw = data["admin_password"]
            if not isinstance(_pw, str):
                handler._send_json({"success": False, "error": "密码必须为字符串"}, 400)
                return
            # 用严格哈希正则判断"已哈希"，而非简单 "$" in _pw
            # 密码如 My$ecret 含 $ 但不符合哈希格式，必须重新哈希，否则登录永久锁死
            if re.match(r'^[0-9a-f]{32}\$[0-9]+\$[0-9a-f]{64}$', _pw):
                pass  # 已哈希，原样写入
            else:
                if len(_pw) < 4:
                    handler._send_json(
                        {"success": False, "error": "密码长度至少 4 个字符"}, 400)
                    return
                data["admin_password"] = hash_password(_pw)
        # tmdb scope 收到 watchlist_db 键时宽容剥离，避免写入 DB 孤儿键
        if scope == "tmdb" and "watchlist_db" in data:
            logging.warning("[WebUI] tmdb scope 配置收到已移除的 watchlist_db 键，已剥离")
            data.pop("watchlist_db", None)
        for key, val in data.items():
            _wdb.set_config(scope, str(key), str(val) if val is not None else "")

        # OpenList 配置保存后：
        # 1. 标记 engines_initialized=True（区分首次运行 vs 用户已保存配置）
        # 2. 触发热更新
        if scope == "openlist":
            if "strm_engines" in data:
                _wdb.set_config("openlist", "engines_initialized", "true")
            _hot_reload_openlist_config(webui_server)
            # 记录 OpenList 配置保存日志
            try:
                _wdb.log_tmdb_operation(
                    "openlist_config_save", "success",
                    f"OpenList 配置已保存 ({len(data)} 项配置)",
                    detail=json.dumps({"keys": list(data.keys())})
                )
            except Exception:
                pass

        # tmdb scope 写入后重新初始化 TMDB 客户端 + 重载 DB 配置
        if scope == "tmdb":
            try:
                webui_server._load_db_config()
                _handler_reinit_tmdb(webui_server, webui_server._config.tmdb)
            except Exception as e:
                logging.warning("[WebUI] tmdb scope 热更新失败: %s", e)

        # 更新 _has_password 缓存（ui scope 管理密码变更时）
        # 改密后清空全部会话，使旧 token 立即失效（旧 token 最长 7 天）
        if scope == "ui" and "admin_password" in data:
            webui_server._has_password = bool(data.get("admin_password"))
            with webui_server._sessions_lock:
                webui_server._sessions.clear()
        handler._send_json(
            {"success": True, "scope": scope, "saved": len(data)})
    except json.JSONDecodeError:
        handler._send_json({"success": False, "error": "无效的 JSON"}, 400)
    except Exception as e:
        logging.exception("[WebUI] 写入配置失败 (scope=%s): %s", scope, e)
        handler._send_json({"success": False, "error": "internal_error"}, 500)

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

        # 【新增】保存旧的日志配置（必须在 update_from_db 之前，否则新旧值会被覆盖）
        old_log_level = cfg.log.level
        old_log_max_size = cfg.log.max_size_mb
        old_log_backup_count = cfg.log.backup_count
        old_log_file = cfg.log.file

        # 从 DB 重新加载配置
        cfg.update_from_db(_wdb)

        # 【新增】检查日志配置是否变更，如果变更则重新初始化日志系统
        log_changed = (
            cfg.log.level != old_log_level
            or cfg.log.max_size_mb != old_log_max_size
            or cfg.log.backup_count != old_log_backup_count
            or cfg.log.file != old_log_file
        )
        if log_changed:
            try:
                from logger_setup import setup_logging
                setup_logging(
                    level=cfg.log.level,
                    log_file=cfg.log.file,
                    max_size_mb=cfg.log.max_size_mb,
                    backup_count=cfg.log.backup_count,
                )
                logging.info(
                    "[HotReload] 日志配置已重新初始化: level=%s, max_size_mb=%s, backup_count=%s, file=%s",
                    cfg.log.level, cfg.log.max_size_mb, cfg.log.backup_count, cfg.log.file,
                )
            except Exception as log_exc:
                logging.warning("[HotReload] 日志配置重新初始化失败: %s", log_exc)

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

        # 无论 WebDAV 配置是否变更，只要有 strm_engines 变化就重加载存储映射
        new_client = getattr(webui_server, '_admin_client', None)
        try:
            cfg.load_strm_storage_from_api(admin_client=new_client)
            logging.info("[HotReload] STRM 存储映射已重新加载")
        except Exception as exc:
            logging.warning("[HotReload] 重新加载 STRM 存储映射失败: %s", exc)

        app_service = getattr(webui_server, "_app_service", None)
        # 热更新后同步刷新 AppService 内存中的 mapping 快照
        # (a_b_mappings/a_roots/_a_to_b_map/_mapping_version)，否则引擎血统
        # 校验、清理、迁移仍用旧路径/旧 mapping_version。c_root 为属性实时读取
        # config.paths，无需额外刷新。
        if app_service is not None and hasattr(app_service, "_refresh_mapping_snapshot"):
            try:
                app_service._refresh_mapping_snapshot()
            except Exception as exc:
                logging.warning("[HotReload] mapping 快照刷新失败: %s", exc)
        refresh_service = getattr(app_service, "refresh_service", None)
        if refresh_service is not None:
            refresh_service.reconfigure()
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
        if new_client.login(force=True):
            logging.info("[HotReload] 新的 OpenListAdminClient 登录成功")
            # 仅当登录成功后才替换 client 引用，避免用无效客户端冲掉正常工作实例
            webui_server._admin_client = new_client
            app_service = getattr(webui_server, '_app_service', None)
            if app_service:
                app_service.admin_api = new_client
                logging.info("[HotReload] AppService.admin_api 已更新")
        else:
            logging.warning("[HotReload] 新的 OpenListAdminClient 登录失败: %s — 保留旧客户端继续运行", new_client.last_error_message or "未知错误")
    except Exception as e:
        logging.warning("[HotReload] 重新初始化 OpenListAdminClient 失败: %s", e)

def _handle_openlist_test_connection(handler, webui_server, body: bytes) -> None:
    """处理 POST /api/openlist/test-connection — 验证 API 连接。"""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, Exception):
        data = {}

    cfg = webui_server._config
    user = data.get("user", cfg.webdav.user)
    password = data.get("password", cfg.webdav.password)
    totp_secret = data.get("totp_secret", cfg.webdav.totp_secret)

    # 限制 host 参数，仅允许测试当前配置的 host，防止 SSRF
    # 忽略请求体中的 host，始终使用当前配置的 host
    host = cfg.webdav.host

    if not host:
        handler._send_json({"success": False, "error": "WebDAV 地址未配置"}, 400)
        return

    # 验证 host 格式：必须是 http(s) URL
    if not re.match(r'^https?://', host):
        handler._send_json({"success": False, "error": "WebDAV 地址格式无效（必须是 http:// 或 https://）"}, 400)
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

            # 根据错误类型返回不同的错误消息
            error_messages = {
                "wrong_password": "密码错误，请检查用户名和密码",
                "wrong_2fa": "2FA 验证码错误，请检查 2FA 密钥或时间同步",
                "account_not_found": "账号不存在，请检查用户名",
                "network_error": "无法连接到 OpenList 服务器，请检查地址和网络",
                "not_configured": "OpenList host 未配置或格式无效，请在配置页面填写正确的 WebDAV 地址",
                "invalid_totp": "TOTP Secret 无效或格式错误，请检查 2FA 密钥",
                "unknown": "登录失败，请检查配置",
            }
            display_message = error_messages.get(error_type, error_messages["unknown"])

            # M-13: display_message 已按 error_type 映射为用户友好文本，
            # 不回传 client.last_error_message（可能含内部路径/凭据）
            handler._send_json({
                "success": False,
                "error": display_message,
                "error_type": error_type,
            })
    except Exception as e:
        # M-13: 不回传原始异常信息
        logging.error("[OpenList] 连接测试异常: %s", e, exc_info=True)
        handler._send_json({
            "success": False,
            "error": "连接失败",
            "error_type": "exception",
        })

def _fetch_strm_storages(cfg) -> list[dict]:
    """确保 STRM 存储映射已加载，并返回按 entry_path 展开的存储列表。

    单一数据源：始终从 cfg.strm_storage_map 派生，避免与 load_strm_storage_from_api()
    重复解析 API、重复登录，并保证分组粒度（entry_path = mount_path/last_dir）一致。
    若内存映射为空，则先调用 load_strm_storage_from_api() 加载（有副作用：会填充缓存）。

    返回格式（每个 entry_path 一条，未按 mount_path 聚合）:
    [
        {"entry_path": "/strm/movies", "mount_path": "/strm", "paths": [...], "local_path": "/local"},
        ...
    ]
    失败返回空列表。
    """
    if not getattr(cfg, "strm_storage_map", None):
        try:
            cfg.load_strm_storage_from_api()
        except Exception as exc:
            logging.warning("[OpenList] 动态加载 STRM 存储映射失败: %s", exc)
            return []

    result: list[dict] = []
    for entry_path, mapping in cfg.strm_storage_map.items():
        result.append({
            "entry_path": entry_path,
            "mount_path": mapping.mount_path,
            "paths": list(mapping.paths),
            "local_path": mapping.local_path,
        })
    return result

def _handle_openlist_strm_engines(handler, webui_server) -> None:
    """处理 GET /api/openlist/strm-engines — 获取 STRM 引擎列表。

    统一从 cfg.strm_storage_map 派生（必要时由 _fetch_strm_storages 触发加载）。
    结果按 mount_path 聚合，确保下拉框不重复。
    """
    cfg = webui_server._config
    # 统一数据源：若内存映射为空，先由 _fetch_strm_storages 触发 load_strm_storage_from_api
    strm_storages = _fetch_strm_storages(cfg)
    raw_engines = []
    for storage in strm_storages:
        raw_engines.append({
            "entry_path": storage["entry_path"],
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

    # 统一数据源：若内存映射为空，先由 _fetch_strm_storages 触发 load_strm_storage_from_api
    if not strm_map:
        _fetch_strm_storages(cfg)
        strm_map = cfg.strm_storage_map

    # 查找以 engine 为 mount_path 前缀的所有条目
    for entry_path, mapping in strm_map.items():
        if mapping.mount_path == engine or entry_path == engine or entry_path.startswith(engine.rstrip("/") + "/"):
            paths.extend(mapping.paths)
    logging.debug("[OpenList] 读取到 %d 个监控目录 (engine=%s)", len(paths), engine)

    handler._send_json({"success": True, "engine": engine, "paths": paths})

def _openlist_merged_webdav_cfg(webui_server):
    """合并 DB/config.toml 的 WebDAV 配置，返回 (host, user, password, totp_secret)。"""
    cfg = webui_server._config
    _wdb = getattr(webui_server, '_watchlist_db', None)
    db_cfg = {}
    if _wdb:
        try:
            db_cfg = _wdb.get_all_config("openlist") or {}
        except Exception:
            pass
    host = db_cfg.get("webdav_host", "") or cfg.webdav.host
    user = db_cfg.get("webdav_user", "") or cfg.webdav.user
    password = db_cfg.get("webdav_password", "") or cfg.webdav.password
    totp_secret = db_cfg.get("webdav_totp_secret", "") or cfg.webdav.totp_secret
    return host, user, password, totp_secret

def _handle_openlist_status(handler, webui_server) -> None:
    """处理 GET /api/openlist/status — 仅判断是否已配置（不解耦在线性）。

    根据 DB/配置中 host 是否非空返回 configured / unconfigured，
    不再调用 client.login()，以解耦"是否配置"与"是否在线"。
    在线探测请使用 GET /api/openlist/ping。
    """
    host, _user, _password, _totp = _openlist_merged_webdav_cfg(webui_server)
    if not host:
        handler._send_json({"success": True, "status": "unconfigured"})
        return
    handler._send_json({"success": True, "status": "configured"})

def _handle_openlist_ping(handler, webui_server) -> None:
    """处理 GET /api/openlist/ping — 探测 OpenList 服务在线状态。

    仅用于在线性检测，不影响"是否已配置"状态。
    返回 status: online / auth_failed_password / auth_failed_2fa / auth_failed / offline。

    该端点为白名单免 Token，但每次调用都会用存储凭据对 OpenList
    发起真实登录。加 IP 级 10 次/分钟速率限制，防止 LAN 客户端无限制调用触发
    OpenList 反暴力破解账户锁定。
    """
    # ---- IP 级速率限制 ----
    client_ip = handler.client_address[0] if handler.client_address else "unknown"
    now = time.time()
    _PING_LIMIT = 10
    _PING_WINDOW = 60
    with _ping_attempts_lock:
        ping_attempts = _ping_attempts  # 模块级 dict，见文件底部初始化
        ip_attempts = ping_attempts.get(client_ip, [])
        ip_attempts = [t for t in ip_attempts if now - t < _PING_WINDOW]
        if len(ip_attempts) >= _PING_LIMIT:
            retry_after = int(_PING_WINDOW - (now - ip_attempts[0]))
            handler._send_json(
                {"success": False, "status": "rate_limited",
                 "message": f"请求过于频繁，请在 {retry_after} 秒后重试"},
                429)
            return
        ip_attempts.append(now)
        ping_attempts[client_ip] = ip_attempts
    # ---- 速率限制结束 ----
    host, user, password, totp_secret = _openlist_merged_webdav_cfg(webui_server)
    if not host:
        handler._send_json({"success": False, "status": "unconfigured"})
        return
    try:
        from webdav_client import OpenListAdminClient
        client = OpenListAdminClient(host, user, password, totp_secret=totp_secret)
        if client.login(force=True):
            # 不返回 host（与 /api/openlist/status 一致，避免白名单端点泄露配置）
            handler._send_json({"success": True, "status": "online"})
        else:
            error_type = client.last_error_type or "unknown"
            status_map = {
                "wrong_password": "auth_failed_password",
                "wrong_2fa": "auth_failed_2fa",
                "account_not_found": "auth_failed",
                "network_error": "offline",
                "not_configured": "offline",  # host 为空或无效，视为离线
                "invalid_totp": "auth_failed_2fa",  # TOTP 密钥无效，归类为 2FA 错误
                "unknown": "auth_failed",
            }
            status = status_map.get(error_type, "auth_failed")
            handler._send_json({"success": False, "status": status})
    except Exception as e:
        logging.exception("[OpenList] 状态检查失败: %s", e)
        handler._send_json({"success": False, "status": "offline", "error": "internal_error"})

def _handle_openlist_paths(handler, webui_server) -> None:
    """处理 GET /api/openlist/paths — 路径自动获取。

    只返回用户配置的 STRM 引擎对应的 a_folders，不返回所有可用引擎。
    """
    cfg = webui_server._config
    _wdb = getattr(webui_server, '_watchlist_db', None)
    strm_map = cfg.strm_storage_map

    # 从 DB 读取用户配置的 strm_engines
    a_folders = []
    a_b_mappings = []
    try:
        db_openlist_cfg = _wdb.get_all_config("openlist") if _wdb else {}
        strm_engines_json = db_openlist_cfg.get("strm_engines", "[]")
        strm_engines = json.loads(strm_engines_json) if strm_engines_json else []

        # 从用户配置的引擎中提取 local_path
        for eng in strm_engines:
            if eng.get("engine"):
                mount_path = eng["engine"]
                if mount_path in strm_map:
                    local_path = strm_map[mount_path].local_path
                    if local_path and local_path not in a_folders:
                        a_folders.append(local_path)

        # 读取 a_b_mappings
        a_b_mappings_json = db_openlist_cfg.get("a_b_mappings", "[]")
        a_b_mappings = json.loads(a_b_mappings_json) if a_b_mappings_json else []
    except Exception as e:
        logging.debug("[OpenList] 从用户配置获取 a_folders 失败: %s", e)

    handler._send_json({
        "success": True,
        "a_folders": a_folders,
        "a_b_mappings": a_b_mappings,
        "b_root": cfg.paths.b_root,
        "c_root": cfg.paths.c_root,
    })

def _handle_tmdb_watchlist_match_refresh(handler, webui_server) -> None:
    """触发后台刷新 TMDB 待看列表的收录状态。"""
    if not getattr(webui_server, '_watchlist_db', None):
        handler._send_json(
            {"success": False, "message": "TMDB 待看数据库未启用"}, 400)
        return
    # 检查 watchlist_enabled 开关（只有明确设为 "false" 才禁用，未设置/空字符串默认启用）
    _wdb_enabled_check = getattr(webui_server, '_watchlist_db', None)
    if _wdb_enabled_check:
        enabled_raw = _wdb_enabled_check.get_config("tmdb", "watchlist_enabled")
        if str(enabled_raw).lower() == "false":
            handler._send_json(
                {"success": False, "message": "TMDB 待看列表已禁用"}, 400)
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
            webui_server._match_refresh_result = {"error": "internal_error"}
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
    # 检查 watchlist_enabled 开关（只有明确设为 "false" 才禁用）
    _wdb_enabled_check = getattr(webui_server, '_watchlist_db', None)
    if _wdb_enabled_check:
        enabled_raw = _wdb_enabled_check.get_config("tmdb", "watchlist_enabled")
        if str(enabled_raw).lower() == "false":
            handler._send_json(
                {"success": False, "message": "TMDB 待看列表已禁用"}, 400)
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
    if item_id <= 0:
        handler._send_json(
            {"success": False, "message": "id 必须大于 0"}, 400)
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
        # M-13: 不回传原始异常信息
        handler._send_json({"success": False, "message": "覆盖失败"}, 500)

def _handle_tmdb_watchlist_match_clear(
        handler, webui_server, body: bytes) -> None:
    """清除 TMDB 待看条目的人工覆盖，恢复为 uncomputed。

    请求体: {media_type: str, id: int}
    - 非法 media_type 或 id<=0 → 400
    - get_match_state() 返回 None → 404
    - 成功清除 → 200 {success: true}
    """
    if not getattr(webui_server, '_watchlist_db', None):
        handler._send_json(
            {"success": False, "message": "TMDB 待看数据库未启用"}, 400)
        return
    # 检查 watchlist_enabled 开关（只有明确设为 "false" 才禁用）
    _wdb_enabled_check = getattr(webui_server, '_watchlist_db', None)
    if _wdb_enabled_check:
        enabled_raw = _wdb_enabled_check.get_config("tmdb", "watchlist_enabled")
        if str(enabled_raw).lower() == "false":
            handler._send_json(
                {"success": False, "message": "TMDB 待看列表已禁用"}, 400)
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
    if item_id <= 0:
        handler._send_json(
            {"success": False, "message": "id 必须大于 0"}, 400)
        return
    # 先检查条目是否存在
    existing = webui_server._watchlist_db.get_match_state(media_type, item_id)
    if existing is None:
        handler._send_json(
            {"success": False, "message": "条目不存在"}, 404)
        return
    try:
        webui_server._watchlist_db.clear_match_override(media_type, item_id)
        handler._send_json({"success": True, "message": "人工覆盖已清除"})
        _wdb = getattr(webui_server, '_watchlist_db', None)
        if _wdb:
            try:
                _wdb.log_tmdb_operation(
                    "match_clear", "info",
                    f"清除人工覆盖 {media_type}/{item_id}",
                    detail=json.dumps({
                        "media_type": media_type, "id": item_id,
                    }),
                )
            except Exception:
                pass
    except Exception as e:
        logging.error("[TMDB] 清除人工覆盖失败: %s", e, exc_info=True)
        # M-13: 不回传原始异常信息
        handler._send_json({"success": False, "message": "清除失败"}, 500)

def _handle_tmdb_watchlist_bg_sync(handler, webui_server) -> None:
    """触发后台 TMDB 待看列表同步。"""
    if not getattr(webui_server, '_tmdb_client', None) or not getattr(
            webui_server, '_watchlist_db', None):
        handler._send_json({"success": False, "message": "TMDB 未配置"}, 400)
        return
    # 检查 watchlist_enabled 开关（只有明确设为 "false" 才禁用，未设置/空字符串默认启用）
    _wdb_enabled_check = getattr(webui_server, '_watchlist_db', None)
    if _wdb_enabled_check:
        enabled_raw = _wdb_enabled_check.get_config("tmdb", "watchlist_enabled")
        if str(enabled_raw).lower() == "false":
            handler._send_json(
                {"success": False, "message": "TMDB 待看列表已禁用"}, 400)
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
        # 显式检查 TMDB 客户端引用，防止未来重构移除 try/except 兜底
        tmdb_client = getattr(webui_server, '_tmdb_client', None)
        if tmdb_client is None:
            logging.warning("[TMDB] 后台同步中止：_tmdb_client 未初始化")
            return
        # force=True 确保同步执行，因为 TTL 检查已移至 sync 方法内部
        webui_server._watchlist_db.sync(tmdb_client, force=True)
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
    """重启主程序（AppService）和 WebUI HTTP 服务。"""
    logging.info("[WebUI] 正在重启主程序和 HTTP 服务...")
    _wdb = getattr(webui_server, '_watchlist_db', None)
    if _wdb:
        try:
            _wdb.log_tmdb_operation("restart", "info", "主程序重启")
        except Exception:
            pass
    handler._send_json({"success": True, "message": "正在重启主程序..."})

    def _do_restart():
        time.sleep(0.5)
        try:
            # 1. 停止主程序（如果在运行）
            if webui_server._app_running:
                logging.info("[Restart] 正在停止主程序...")
                webui_server.stop_main()

            # 2. 重新启动主程序
            #    start_main() 内部会调用 load_strm_storage_from_api() 加载 STRM 存储映射，
            #    此处不再重复加载，避免二次登录 + 二次拉取。
            logging.info("[Restart] 正在启动主程序...")
            result = webui_server.start_main()
            if result.get("success"):
                logging.info("[Restart] 主程序已重启")
            else:
                logging.error("[Restart] 主程序启动失败: %s", result.get("message"))

            # 3. 重启 HTTP 服务
            webui_server.stop()
            # start() 对端口占用/绑定失败只记录日志并返回（_server 仍为
            # None），此前 _do_restart 不校验导致 WebUI 静默永久离线。重试绑定并高声告警。
            for attempt in range(1, 4):
                webui_server.start()
                if webui_server._server is not None:
                    break
                logging.error(
                    "[Restart] HTTP 服务第 %d 次绑定失败（端口可能被占用），"
                    "2 秒后重试...", attempt)
                time.sleep(2)
            if webui_server._server is not None:
                logging.info("[Restart] HTTP 服务重启完成")
            else:
                logging.error(
                    "[Restart] ⚠ HTTP 服务重启失败：端口 %s 无法绑定，WebUI 当前离线。"
                    "请手动关闭占用进程或修改端口后运行 server.py 恢复。",
                    getattr(webui_server, "_port", "?"))
        except Exception as e:
            logging.error("[Restart] 重启失败: %s", e)
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
            duplicate = conn.execute(
                "SELECT COUNT(*) FROM b_strm_files WHERE status='duplicate'").fetchone()[0]
            quarantined = conn.execute(
                "SELECT COUNT(*) FROM b_strm_files WHERE status='quarantined'").fetchone()[0]
        return {"valid": valid, "duplicate": duplicate, "quarantined": quarantined}
    except Exception:
        return {"valid": 0, "duplicate": 0, "quarantined": 0}

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
                like = f"%{escape_like(search)}%"
                count_sql += " WHERE local_path LIKE ? ESCAPE '\\' OR webdav_path LIKE ? ESCAPE '\\'"
                query_sql += " WHERE local_path LIKE ? ESCAPE '\\' OR webdav_path LIKE ? ESCAPE '\\'"
                search_params = (like, like)
            else:
                search_params = ()
            query_sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"

        elif area == "b":
            count_sql = "SELECT COUNT(*) FROM b_strm_files"
            query_sql = "SELECT local_path, webdav_path, parent_webdav_path, source_a_path, fingerprint, status, updated_at FROM b_strm_files"
            if search:
                like = f"%{escape_like(search)}%"
                count_sql += " WHERE local_path LIKE ? ESCAPE '\\' OR webdav_path LIKE ? ESCAPE '\\'"
                query_sql += " WHERE local_path LIKE ? ESCAPE '\\' OR webdav_path LIKE ? ESCAPE '\\'"
                search_params = (like, like)
            else:
                search_params = ()
            query_sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"

        elif area == "c":
            count_sql = "SELECT COUNT(*) FROM c_ghost_files"
            query_sql = "SELECT local_path, webdav_path, original_b_path, ghost_root, moved_at FROM c_ghost_files"
            if search:
                like = f"%{escape_like(search)}%"
                count_sql += " WHERE local_path LIKE ? ESCAPE '\\' OR webdav_path LIKE ? ESCAPE '\\'"
                query_sql += " WHERE local_path LIKE ? ESCAPE '\\' OR webdav_path LIKE ? ESCAPE '\\'"
                search_params = (like, like)
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

def _get_mapping_metadata_list(handler) -> list[dict]:
    """获取所有 mapping 的元数据列表（供 Dashboard 展示）。"""
    app_service = getattr(handler.webui, '_app_service', None)
    if not app_service:
        return []
    
    db = handler.webui._db
    a_b_mappings = getattr(app_service, 'a_b_mappings', [])
    metadata_list = []
    
    for mapping in a_b_mappings:
        mapping_id = str(getattr(mapping, 'mapping_id', '')).strip()
        if not mapping_id:
            continue
        
        meta = db.get_index_metadata(mapping_id)
        metadata_list.append({
            "mapping_id": mapping_id,
            "label": getattr(mapping, 'label', ''),
            "a_root": getattr(mapping, 'a_root', ''),
            "b_root": getattr(mapping, 'b_root', ''),
            "index_generation": meta.get("mapping_index_generation", 0),
            "index_generation_at": meta.get("mapping_index_generation_at", 0),
        })
    
    return metadata_list

def handle_dashboard(handler) -> None:
    """处理 GET /api/dashboard"""
    db = handler.webui._db
    try:
        counts = _db_get_table_counts(db)
        b_status = _db_get_b_status_counts(db)
        db_size = _db_get_db_file_size(db)
        
        # 获取索引元数据（Task 2）
        index_metadata = db.get_index_metadata()
        mapping_metadata = _get_mapping_metadata_list(handler)
        
        # 从 app_service 获取 watchdog 健康状态
        app_service = handler.webui._app_service
        watchers_healthy = getattr(app_service, '_watchers_healthy', True) if app_service else True
        
        handler._send_json({
            "a_count": counts.get("a_strm_files", 0),
            "b_count": counts.get("b_strm_files", 0),
            "c_count": counts.get("c_ghost_files", 0),
            "b_valid": b_status.get("valid", 0),
            "b_duplicate": b_status.get("duplicate", 0),
            "b_quarantined": b_status.get("quarantined", 0),
            "tmdb_configured": bool(handler.webui._tmdb_client),
            # Watchdog 健康状态 - 前端据此显示降级指示
            "watchers_healthy": watchers_healthy,
            # 遗留字段（保持向后兼容）
            "table_counts": counts,
            "b_status_counts": b_status,
            "db_file_size": db_size,
            "db_file_size_human": _human_size(db_size),
            "uptime": time.time() - handler.webui._start_time,
            # Task 2: 索引元数据
            "index_metadata": {
                "index_generation": index_metadata.get("index_generation", 0),
                "index_generation_at": index_metadata.get("index_generation_at", 0),
                "last_full_index_at": index_metadata.get("last_full_index_at", 0),
                "mapping_version": index_metadata.get("mapping_version", ""),
                "mapping_version_generated_at": index_metadata.get("mapping_version_generated_at", 0),
            },
            "mappings": mapping_metadata,
        })
    except Exception as e:
        logging.exception("[Dashboard] 获取索引元数据失败: %s", e)
        handler._send_json({"error": "internal_error"}, 500)

def handle_records_api(handler, params) -> None:
    """处理 GET /api/records?area=a&page=1&page_size=100&search=xxx

    SQL 级分页的记录查询接口。
    返回 {total, page, page_size, records}
    """
    area = params.get("area", ["a"])[0]
    if area not in ("a", "b", "c"):
        handler._send_json({"error": "无效区域"}, 400)
        return
    page = max(1, _safe_int(params.get("page", ["1"])[0], 1))
    page_size = max(1, min(_safe_int(params.get("page_size", ["100"])[0], 100), 500))
    search = params.get("search", [""])[0].strip()

    result = _get_records_paginated(handler, area, page=page,
                                     page_size=page_size, search=search)
    handler._send_json(result)

# SQL 提取 kind 的逻辑（与 Python _media_info 一致）
# 根据路径中的分类目录判断（番剧/电影/其他）
# 使用模块级常量避免重复定义
_KIND_SQL = """
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

# 提取媒体名称：找到分类目录后的第一段目录名
# 覆盖 _KIND_SQL 中归为 电影/番剧 的全部别名目录（/movies/ /movie/ /anime/ /动漫/ /动画/），
# 否则别名目录下的标题会坍缩进 '未分类'。偏移量 = 匹配串长度（含首尾斜杠）。
_MEDIA_NAME_SQL = f"""
    CASE
        WHEN {_KIND_SQL} = '番剧' THEN
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
                WHEN INSTR(REPLACE(webdav_path, '\\', '/'), '/anime/') > 0 THEN
                    SUBSTR(
                        SUBSTR(REPLACE(webdav_path, '\\', '/'), INSTR(REPLACE(webdav_path, '\\', '/'), '/anime/') + 7),
                        1,
                        INSTR(SUBSTR(REPLACE(webdav_path, '\\', '/'), INSTR(REPLACE(webdav_path, '\\', '/'), '/anime/') + 7) || '/', '/') - 1
                    )
                WHEN INSTR(REPLACE(local_path, '\\', '/'), '/anime/') > 0 THEN
                    SUBSTR(
                        SUBSTR(REPLACE(local_path, '\\', '/'), INSTR(REPLACE(local_path, '\\', '/'), '/anime/') + 7),
                        1,
                        INSTR(SUBSTR(REPLACE(local_path, '\\', '/'), INSTR(REPLACE(local_path, '\\', '/'), '/anime/') + 7) || '/', '/') - 1
                    )
                WHEN INSTR(REPLACE(webdav_path, '\\', '/'), '/动漫/') > 0 THEN
                    SUBSTR(
                        SUBSTR(REPLACE(webdav_path, '\\', '/'), INSTR(REPLACE(webdav_path, '\\', '/'), '/动漫/') + 4),
                        1,
                        INSTR(SUBSTR(REPLACE(webdav_path, '\\', '/'), INSTR(REPLACE(webdav_path, '\\', '/'), '/动漫/') + 4) || '/', '/') - 1
                    )
                WHEN INSTR(REPLACE(local_path, '\\', '/'), '/动漫/') > 0 THEN
                    SUBSTR(
                        SUBSTR(REPLACE(local_path, '\\', '/'), INSTR(REPLACE(local_path, '\\', '/'), '/动漫/') + 4),
                        1,
                        INSTR(SUBSTR(REPLACE(local_path, '\\', '/'), INSTR(REPLACE(local_path, '\\', '/'), '/动漫/') + 4) || '/', '/') - 1
                    )
                WHEN INSTR(REPLACE(webdav_path, '\\', '/'), '/动画/') > 0 THEN
                    SUBSTR(
                        SUBSTR(REPLACE(webdav_path, '\\', '/'), INSTR(REPLACE(webdav_path, '\\', '/'), '/动画/') + 4),
                        1,
                        INSTR(SUBSTR(REPLACE(webdav_path, '\\', '/'), INSTR(REPLACE(webdav_path, '\\', '/'), '/动画/') + 4) || '/', '/') - 1
                    )
                WHEN INSTR(REPLACE(local_path, '\\', '/'), '/动画/') > 0 THEN
                    SUBSTR(
                        SUBSTR(REPLACE(local_path, '\\', '/'), INSTR(REPLACE(local_path, '\\', '/'), '/动画/') + 4),
                        1,
                        INSTR(SUBSTR(REPLACE(local_path, '\\', '/'), INSTR(REPLACE(local_path, '\\', '/'), '/动画/') + 4) || '/', '/') - 1
                    )
                ELSE '未分类'
            END
        WHEN {_KIND_SQL} = '电影' THEN
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
                WHEN INSTR(REPLACE(webdav_path, '\\', '/'), '/movies/') > 0 THEN
                    SUBSTR(
                        SUBSTR(REPLACE(webdav_path, '\\', '/'), INSTR(REPLACE(webdav_path, '\\', '/'), '/movies/') + 8),
                        1,
                        INSTR(SUBSTR(REPLACE(webdav_path, '\\', '/'), INSTR(REPLACE(webdav_path, '\\', '/'), '/movies/') + 8) || '/', '/') - 1
                    )
                WHEN INSTR(REPLACE(local_path, '\\', '/'), '/movies/') > 0 THEN
                    SUBSTR(
                        SUBSTR(REPLACE(local_path, '\\', '/'), INSTR(REPLACE(local_path, '\\', '/'), '/movies/') + 8),
                        1,
                        INSTR(SUBSTR(REPLACE(local_path, '\\', '/'), INSTR(REPLACE(local_path, '\\', '/'), '/movies/') + 8) || '/', '/') - 1
                    )
                WHEN INSTR(REPLACE(webdav_path, '\\', '/'), '/movie/') > 0 THEN
                    SUBSTR(
                        SUBSTR(REPLACE(webdav_path, '\\', '/'), INSTR(REPLACE(webdav_path, '\\', '/'), '/movie/') + 7),
                        1,
                        INSTR(SUBSTR(REPLACE(webdav_path, '\\', '/'), INSTR(REPLACE(webdav_path, '\\', '/'), '/movie/') + 7) || '/', '/') - 1
                    )
                WHEN INSTR(REPLACE(local_path, '\\', '/'), '/movie/') > 0 THEN
                    SUBSTR(
                        SUBSTR(REPLACE(local_path, '\\', '/'), INSTR(REPLACE(local_path, '\\', '/'), '/movie/') + 7),
                        1,
                        INSTR(SUBSTR(REPLACE(local_path, '\\', '/'), INSTR(REPLACE(local_path, '\\', '/'), '/movie/') + 7) || '/', '/') - 1
                    )
                ELSE '未分类'
            END
        ELSE
            CASE
                WHEN INSTR(SUBSTR(REPLACE(webdav_path, '\\', '/'), 2), '/') > 0 THEN
                    SUBSTR(REPLACE(webdav_path, '\\', '/'), 2, INSTR(SUBSTR(REPLACE(webdav_path, '\\', '/'), 2), '/') - 1)
                ELSE REPLACE(webdav_path, '\\', '/')
            END
    END
"""

# 表名和时间字段映射（白名单验证）
_AREA_TABLE_MAP = {
    "a": ("a_strm_files", "updated_at"),
    "b": ("b_strm_files", "updated_at"),
    "c": ("c_ghost_files", "moved_at"),
}

# kind 筛选值映射（白名单验证）
_KIND_FILTER_MAP = {
    "anime": "番剧",
    "movie": "电影",
    "other": "其他",
}

# UI scope 写入白名单：仅允许这些 key 通过 POST /api/webui/config/ui 写入
_UI_CONFIG_ALLOWED_KEYS = {"tmdb_cache_never_remind", "tmdb_match_toast_disabled", "admin_password", "onboarding_completed", "onboarding_skipped"}

# 登录速率限制
_login_attempts: dict[str, list[float]] = {}
_login_attempts_lock = threading.Lock()
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCKOUT_SECONDS = 300

# /api/openlist/ping 的 IP 级速率限制（10 次/分钟），
# 防止白名单端点被无限制调用触发 OpenList 账户锁定。
_ping_attempts: dict[str, list[float]] = {}
_ping_attempts_lock = threading.Lock()

def _handle_login(handler, webui_server, body: bytes) -> None:
    """处理 POST /api/login — 密码登录验证。"""
    # 客户端 IP 速率限制（原子化读写，防止并发绕过）
    client_ip = handler.client_address[0]
    now = time.time()
    with _login_attempts_lock:
        # 定期清理 _login_attempts（每 1000 个 IP 时清理一次，防止内存泄漏）
        if len(_login_attempts) > 1000:
            cutoff = now - _LOGIN_LOCKOUT_SECONDS
            stale_ips = [ip for ip, times in _login_attempts.items()
                         if all(t < cutoff for t in times)]
            for ip in stale_ips:
                del _login_attempts[ip]

        attempts = _login_attempts.get(client_ip, [])
        # 清理过期记录
        attempts = [t for t in attempts if now - t < _LOGIN_LOCKOUT_SECONDS]
        if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
            retry_after = int(_LOGIN_LOCKOUT_SECONDS - (now - attempts[0]))
            handler._send_json(
                {"error": f"登录尝试过于频繁，请在 {retry_after} 秒后重试"},
                429)
            return
        # 【已核对，勿再作为 bug 上报】
        # 写回操作仍在 `with _login_attempts_lock:` 块内（从 line 2125 到此行），
        # 读-清理-判限-写回全程原子，勿据缩进误判为竞态。
        _login_attempts[client_ip] = attempts
    try:
        data = json.loads(body)
        # 校验 JSON body 必须为对象，防止非对象体导致 AttributeError
        # 安全权衡：畸形请求不计入 _login_attempts（攻击者无法通过批量畸形请求触发锁定）
        if not isinstance(data, dict):
            handler._send_json({"error": "请求体须为 JSON 对象"}, 400)
            return
        password = data.get("password", "")
        if not password:
            handler._send_json({"error": "密码不能为空"}, 400)
            return
        if not webui_server._watchlist_db:
            handler._send_json({"error": "DB 未初始化"}, 500)
            return
        stored = webui_server._watchlist_db.get_config("ui", "admin_password", "")
        if not stored:
            handler._send_json({"error": "未设置管理员密码"}, 400)
            return
        # 检测密码哈希格式是否损坏（salt$iterations$hash）
        if "$" not in stored or len(stored.split("$", 2)) != 3:
            handler._send_json({
                "error": "密码格式损坏，请运行 reset_admin.py 重置管理员密码"
            }, 500)
            return
        # M-2: 使用统一的密码工具模块验证密码
        from utils.password_utils import verify_password
        password_ok = verify_password(password, stored)
        if not password_ok:
            with _login_attempts_lock:
                # 双重检查锁定。初始限流检查（上方）释放锁后、
                # 密码哈希（慢 ~100ms）执行期间，N 个并发请求都可能通过初始检查；
                # 此处重新获锁后再次校验计数，已达上限则直接 429，不再追加，
                # 突破 5 次锁定上限。
                current = _login_attempts.get(client_ip, [])
                current = [t for t in current if now - t < _LOGIN_LOCKOUT_SECONDS]
                if len(current) >= _LOGIN_MAX_ATTEMPTS:
                    retry_after = int(
                        _LOGIN_LOCKOUT_SECONDS - (now - current[0]))
                    handler._send_json(
                        {"error": f"登录尝试过于频繁，请在 {retry_after} 秒后重试"},
                        429)
                    return
                # 读-过滤-追加-写回全程原子，避免覆盖并发失败记录
                current.append(now)
                _login_attempts[client_ip] = current
            handler._send_json({"error": "密码错误"}, 401)
            return
        # 登录成功，清除失败记录
        with _login_attempts_lock:
            _login_attempts.pop(client_ip, None)
        # 生成 session token
        token = secrets.token_hex(32)
        # M-4: Session 绑定客户端 IP（防止被盗 token 跨 IP 使用）
        with webui_server._sessions_lock:
            webui_server._sessions[token] = (time.time() + 604800, client_ip)  # 7天, IP
        handler._send_json({"success": True, "token": token})
    except json.JSONDecodeError:
        with _login_attempts_lock:
            _login_attempts.setdefault(client_ip, []).append(now)
        handler._send_json({"error": "无效的 JSON"}, 400)
    except Exception as e:
        with _login_attempts_lock:
            _login_attempts.setdefault(client_ip, []).append(now)
        logging.warning("[Login] 登录失败: %s", e)
        handler._send_json({"error": "服务器内部错误"}, 500)

def _get_media_groups_paginated(handler, area: str, kind_filter: str,
                                 q: str, sort_key: str, sort_order: str,
                                 page: int, page_size: int) -> dict:
    """SQL 级分页的媒体分组查询。

    返回 {total, page, page_size, media_items, kind_counts}
    """
    db = handler.webui._db

    # 确定表名和时间字段（使用白名单映射）
    if area not in _AREA_TABLE_MAP:
        return {"total": 0, "page": page, "page_size": page_size,
                "media_items": [], "kind_counts": {}}
    table, time_field = _AREA_TABLE_MAP[area]

    # 构建基础查询条件
    base_where = ""
    params_list = []

    if q:
        # 列表页搜索：使用 FTS5 全文搜索（simple 分词器支持中文），通过 rowid 关联主表。
        # 这里是用户主动输入关键词的模糊搜索场景，数据量大，适合 FTS5。
        # 注意：与 handle_area_detail 不同——详情页用 LIKE 子串精确取「某部媒体的全部剧集」，
        # 那里 media 是从列表页点进来的过滤条件而非搜索词，且必须避免 FTS5 静默返回 0 行。
        fts_table_map = {"a": "a_strm_files_fts", "b": "b_strm_files_fts", "c": "c_ghost_files_fts"}
        fts_table = fts_table_map.get(area, "a_strm_files_fts")
        escaped_query = _escape_fts5_query(q)
        base_where = f" AND rowid IN (SELECT rowid FROM {fts_table} WHERE {fts_table} MATCH ?)"
        params_list.append(escaped_query)

    # kind_counts 使用独立参数（只含搜索 q，不含 kind 筛选），
    # 确保统计始终显示所有类型的真实数量不受当前筛选影响
    kind_params = list(params_list)  # 仅复制搜索参数

    # 查询 kind_counts（分类统计）— 必须放在 kind 筛选之前，排除 kind_where
    kind_counts_sql = f"""
        SELECT
            CASE
                WHEN {_KIND_SQL} = '番剧' THEN 'anime'
                WHEN {_KIND_SQL} = '电影' THEN 'movie'
                ELSE 'other'
            END AS kind_category,
            COUNT(DISTINCT ({_MEDIA_NAME_SQL})) AS count
        FROM {table}
        WHERE 1=1 {base_where}
        GROUP BY kind_category
    """

    # 筛选 kind（仅作用于分页列表，不影响 kind_counts）
    kind_where = ""
    if kind_filter != "all" and kind_filter in _KIND_FILTER_MAP:
        kind_value = _KIND_FILTER_MAP[kind_filter]
        kind_where = f" AND {_KIND_SQL} = ?"
        params_list.append(kind_value)

    # 查询媒体分组（分页）
    offset = (page - 1) * page_size

    # 排序校验
    if sort_order not in _AREA_SORT_ORDERS:
        sort_order = "asc"

    order_clause = "ORDER BY "
    if sort_key == "count":
        order_clause += "file_count " + ("DESC" if sort_order == "desc" else "ASC")
    elif sort_key == "time":
        order_clause += "latest_ts " + ("DESC" if sort_order == "desc" else "ASC")
    elif sort_key == "kind":
        order_clause += "kind " + ("DESC" if sort_order == "desc" else "ASC")
    else:  # name
        order_clause += "media_name " + ("DESC" if sort_order == "desc" else "ASC")

    media_groups_sql = f"""
        SELECT
            {_KIND_SQL} AS kind,
            {_MEDIA_NAME_SQL} AS media_name,
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
        SELECT COUNT(DISTINCT ({_KIND_SQL} || '|' || {_MEDIA_NAME_SQL})) AS total
        FROM {table}
        WHERE 1=1 {base_where} {kind_where}
    """

    try:
        with db.read_connection() as conn:
            conn.row_factory = sqlite3.Row
            # 查询 kind_counts
            kind_counts_rows = conn.execute(kind_counts_sql, kind_params).fetchall()
            kind_counts = {}
            for row in kind_counts_rows:
                kind_counts[row[0]] = row[1]

            # 查询总数
            total = conn.execute(total_sql, params_list).fetchone()[0]

            # 查询媒体分组（先查询所有符合条件的记录，再在 Python 中自然排序）
            # 为了支持自然排序，我们需要先获取完整结果集
            if sort_key == "name":
                # 查询所有记录（不分页），然后在 Python 中自然排序
                all_media_sql = f"""
                    SELECT
                        {_KIND_SQL} AS kind,
                        {_MEDIA_NAME_SQL} AS media_name,
                        COUNT(*) AS file_count,
                        MAX({time_field}) AS latest_ts
                    FROM {table}
                    WHERE 1=1 {base_where} {kind_where}
                    GROUP BY kind, media_name
                """
                all_media_rows = conn.execute(all_media_sql, params_list).fetchall()

                media_items = []
                for row in all_media_rows:
                    media_items.append({
                        "name": row["media_name"] or "未分类",
                        "kind": row["kind"],
                        "count": row["file_count"],
                        "season": "",
                        "latest_ts": row["latest_ts"] or 0,
                    })

                # 自然排序
                media_items.sort(
                    key=lambda item: _natural_sort_key(item["name"]),
                    reverse=(sort_order == "desc")
                )

                # 分页
                total = len(media_items)
                media_items = media_items[offset:offset + page_size]
            else:
                # 其他排序键使用 SQL 排序
                media_rows = conn.execute(media_groups_sql, params_list + [page_size, offset]).fetchall()

                media_items = []
                for row in media_rows:
                    media_items.append({
                        "name": row["media_name"] or "未分类",
                        "kind": row["kind"],
                        "count": row["file_count"],
                        "season": "",
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
        # FTS5 搜索失败时回退到 LIKE 子串匹配，避免返回空结果
        # 注意：必须重新构建 SQL 语句，因为 f-string 在构建时已求值
        if q:
            try:
                like = f"%{escape_like(q)}%"
                like_base_where = f" AND (local_path LIKE ? ESCAPE '\\' OR webdav_path LIKE ? ESCAPE '\\')"
                like_params = [like, like]
                like_kind_params = list(like_params)
                like_kind_where = ""
                if kind_filter != "all" and kind_filter in _KIND_FILTER_MAP:
                    kind_value = _KIND_FILTER_MAP[kind_filter]
                    like_kind_where = f" AND {_KIND_SQL} = ?"
                    like_params.append(kind_value)
                    like_kind_params.append(kind_value)

                # 重新构建 SQL 语句（使用 LIKE 条件）
                like_kind_counts_sql = f"""
                    SELECT
                        CASE
                            WHEN {_KIND_SQL} = '番剧' THEN 'anime'
                            WHEN {_KIND_SQL} = '电影' THEN 'movie'
                            ELSE 'other'
                        END AS kind_category,
                        COUNT(DISTINCT ({_MEDIA_NAME_SQL})) AS count
                    FROM {table}
                    WHERE 1=1 {like_base_where}
                    GROUP BY kind_category
                """
                like_total_sql = f"""
                    SELECT COUNT(DISTINCT ({_KIND_SQL} || '|' || {_MEDIA_NAME_SQL})) AS total
                    FROM {table}
                    WHERE 1=1 {like_base_where} {like_kind_where}
                """
                like_media_groups_sql = f"""
                    SELECT
                        {_KIND_SQL} AS kind,
                        {_MEDIA_NAME_SQL} AS media_name,
                        COUNT(*) AS file_count,
                        MAX({time_field}) AS latest_ts
                    FROM {table}
                    WHERE 1=1 {like_base_where} {like_kind_where}
                    GROUP BY kind, media_name
                    {order_clause}
                    LIMIT ? OFFSET ?
                """

                with db.read_connection() as conn:
                    conn.row_factory = sqlite3.Row
                    kind_counts_rows = conn.execute(like_kind_counts_sql, like_kind_params).fetchall()
                    kind_counts = {row[0]: row[1] for row in kind_counts_rows}
                    total = conn.execute(like_total_sql, like_params).fetchone()[0]
                    media_rows = conn.execute(
                        like_media_groups_sql, like_params + [page_size, offset]).fetchall()
                    media_items = [
                        {
                            "name": row["media_name"] or "未分类",
                            "kind": row["kind"],
                            "count": row["file_count"],
                            "season": "",
                            "latest_ts": row["latest_ts"] or 0,
                        }
                        for row in media_rows
                    ]
                return {
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "media_items": media_items,
                    "kind_counts": kind_counts,
                }
            except Exception as e2:
                logging.error("SQL 分页查询 LIKE 回退失败: %s", e2)
        return {"total": 0, "page": page, "page_size": page_size,
                "media_items": [], "kind_counts": {}}

def handle_area(handler, area, params) -> None:
    """处理 GET /api/area/{area} — 区域列表，返回按媒体分组的统计摘要"""
    if area not in ("a", "b", "c"):
        handler._send_json({"error": "无效区域"}, 400)
        return

    kind_filter = params.get("kind", ["anime"])[0].lower()
    q = params.get("q", [""])[0].strip().lower()
    sort_key = params.get("sort", ["name"])[0]
    sort_order = params.get("order", ["asc"])[0]
    page = max(1, _safe_int(params.get("page", ["1"])[0], 1))
    page_size = max(1, min(_safe_int(params.get("page_size", ["50"])[0], 50), 500))

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
                        f"SELECT local_path FROM {table} WHERE local_path LIKE ? ESCAPE '\\' LIMIT 1",
                        (f"%{escape_like(media_name)}%",)
                    ).fetchone()
                    if row:
                        # 电影/other/all: is_anime=False，防止路径中的 S01/Season 目录被误提取为季分组
                        season = _extract_season_from_local_path(row[0], allow_filename_fallback=(kind_filter == "anime"), is_anime=(kind_filter == "anime"))
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

# 各区可排序字段白名单
_AREA_SORT_FIELDS: dict[str, set[str]] = {
    "a": {"local_path", "webdav_path", "updated_at", "last_verified_at"},
    "b": {"local_path", "webdav_path", "updated_at", "last_verified_at", "status", "fingerprint"},
    "c": {"local_path", "webdav_path", "moved_at"},
}
_AREA_SORT_ORDERS = {"asc", "desc"}

def _natural_sort_key(path: str) -> tuple:
    """自然排序键：对 basename 的连续数字按整数比较，避免字典序导致的
    `1, 10, 2, 21` 错乱（缺前导零时）。最终以 `local_path` 作为 tiebreaker。

    示例：
        E1.strm  -> (1, ...)
        E2.strm  -> (2, ...)
        E10.strm -> (10, ...)
        E21.strm -> (21, ...)
    """
    basename = Path(path).name if path else ""
    # 切分 basename 为 (非数字, 数字) 段；非数字段一并参与字典序比较。
    # 例如 "Show - S01E10.strm" → ('Show - S', 1, 'E', 10, '.strm')
    parts: list = []
    for i, tok in enumerate(re.split(r"(\d+)", basename)):
        if i % 2 == 1:  # 数字段
            try:
                parts.append((0, int(tok)))  # 标记 0 表示数字，先于字符串段
            except ValueError:
                parts.append((1, tok))
        else:
            parts.append((1, tok))
    parts.append((1, path))  # tiebreaker：完整路径
    return tuple(parts)

def _compute_common_local_root(local_paths: list[str]) -> str:
    """计算多个本地路径的公共目录前缀。

    用于路径归属校验，确保删除操作只影响同一媒体目录下的文件。
    返回带分隔符结尾的目录路径，便于 startswith 检查。
    """
    if not local_paths:
        return ""

    # 使用 os.path.commonpath 计算公共路径
    try:
        import os
        common = os.path.commonpath(local_paths)
        # 如果公共路径是文件（不是目录），取其父目录
        if common and not common.endswith(('/', '\\')):
            common = os.path.dirname(common)
        # 确保以分隔符结尾
        if common and not common.endswith(('/', '\\')):
            common += os.sep
        return common
    except (ValueError, TypeError):
        # 如果路径无法计算公共路径（如不同驱动器），返回空
        return ""

def _escape_fts5_query(query: str) -> str:
    """清理 FTS5 查询字符串，移除可能被解释为运算符的字符。

    策略：移除 FTS5 特殊运算符字符（* - + " ^ ~），保留括号等可能出现在
    文件名中的字符（替换为空格）。避免逐个反斜杠转义在不同上下文的行为不一致问题。
    """
    # 移除 FTS5 运算符字符（包括冒号，因为冒号在 FTS5 中用于列过滤）
    query = re.sub(r'[*+"^~:]', '', query)
    # 将括号替换为空格（避免嵌套查询语法问题）
    query = re.sub(r'[(){}[\]]', ' ', query)
    # 保留连字符（文件名中常见，如 test-123），但移除首尾连字符
    query = query.strip('-')
    # 移除多余空白
    query = ' '.join(query.split())
    # 移除反斜杠（Windows 路径分隔符在 FTS5 中无意义）
    query = query.replace('\\', ' ')
    return f'"{query}"'

def handle_area_detail(handler, area, params) -> None:
    """处理 GET /api/area/{area}/detail — 区域详情，返回指定媒体的所有记录
    
    Task 2: 支持多 mapping 分区
    - 列表页：按 kind + media_name 合并（不拆分）
    - 详情页：按 mapping_id 分区，每个 mapping 独立根路径/季分组/分页
    - 单一 mapping：保持向后兼容扁平响应
    - 多 mapping：返回 mappings 数组
    
    Task 4: 支持 kind 参数控制季提取行为
    - kind ∈ {anime, movie, other, all}，非法值降级为 all
    - 仅 kind == 'anime' 允许文件名 SxxExx fallback
    - movie/other/all 只认目录显式季标识，否则归入「默认」
    """
    if area not in ("a", "b", "c"):
        handler._send_json({"error": "无效区域"}, 400)
        return

    media_name = params.get("media", [""])[0]
    sort_field = params.get("sort", ["local_path"])[0]
    sort_order = params.get("order", ["asc"])[0]
    page = _safe_int(params.get("page", ["1"])[0], 1)
    
    # Task 4: 读取并校验 kind 参数
    kind = params.get("kind", [""])[0].strip().lower()
    valid_kinds = {"anime", "movie", "other", "all"}
    if kind not in valid_kinds:
        kind = "all"  # 非法值降级为 all（安全行为）
    # 仅 anime 允许文件名 fallback
    allow_filename_fallback = (kind == "anime")

    # 排序白名单校验
    allowed_fields = _AREA_SORT_FIELDS.get(area, {"local_path"})
    if sort_field not in allowed_fields:
        sort_field = "local_path"
    if sort_order not in _AREA_SORT_ORDERS:
        sort_order = "asc"

    db = handler.webui._db
    all_records: list[dict] = []
    total = 0
    search_params: tuple[str, ...] = ()
    try:
        # 构建列列表和 COUNT（Task 2: B 区添加 mapping_id 列）
        if area == "a":
            columns = "local_path, webdav_path, parent_webdav_path, updated_at, last_verified_at"
            table = "a_strm_files"
        elif area == "b":
            columns = "local_path, webdav_path, parent_webdav_path, source_a_path, fingerprint, status, updated_at, last_verified_at, mapping_id"
            table = "b_strm_files"
        else:  # area == "c"
            columns = "local_path, webdav_path, original_b_path, ghost_root, moved_at"
            table = "c_ghost_files"

        where_clause = ""
        if media_name:
            # 使用 LIKE 而非 FTS5：详情页数据量小，LIKE 更可靠（FTS5 对中文媒体名可能静默返回空结果）
            # 转义 media_name 中的 LIKE 通配符（% _ \），避免下划线等合法字符被当作通配符过度匹配
            like = f"%{escape_like(media_name)}%"
            where_clause = " WHERE local_path LIKE ? ESCAPE '\\' OR webdav_path LIKE ? ESCAPE '\\'"
            search_params = (like, like)
        count_sql = f"SELECT COUNT(*) FROM {table}{where_clause}"
        with db.read_connection() as conn:
            total = conn.execute(count_sql, search_params).fetchone()[0]

        # 查询所有记录（不做分页，由 mapping 分区独立分页）
        query_sql = f"SELECT {columns} FROM {table}{where_clause}"
        with db.read_connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query_sql, search_params).fetchall()
        all_records = [dict(r) for r in rows]
    except Exception as e:
        logging.error("查询 %s 区详情失败: %s", area, e)

    # Task 2: 按 mapping 分区
    app_service = getattr(handler.webui, '_app_service', None)
    current_mapping_ids = set()
    if app_service:
        current_mapping_ids = {
            str(m.mapping_id).strip()
            for m in getattr(app_service, 'a_b_mappings', [])
            if str(getattr(m, 'mapping_id', '')).strip()
        }
    
    # B 区和 A 区按 mapping 分区，C 区不分区
    if area in ("a", "b"):
        # 按 mapping_id 分组（B 区直接用 mapping_id 列，A 区通过 get_mapping_for_a 解析）
        mapping_groups: dict[str, list[dict]] = {}
        for rec in all_records:
            if area == "b":
                # B 区：直接用 mapping_id 列
                mid = rec.get("mapping_id", "")
            else:
                # A 区：通过 get_mapping_for_a 解析
                local_path = rec.get("local_path", "")
                mapping_result = app_service.get_mapping_for_a(local_path) if app_service else None
                mid = mapping_result[0] if mapping_result else ""
            
            # 未知 mapping 归入 unknown
            if mid and mid not in current_mapping_ids:
                mid = "unknown"
            elif not mid:
                mid = "unknown"
            
            mapping_groups.setdefault(mid, []).append(rec)
        
        # 处理 A 区无 mapping_id 但有记录的情况（fallback）
        if not mapping_groups and all_records:
            mapping_groups["unknown"] = all_records
        
        # 对每个 mapping 分区独立计算分页和排序
        mappings_result = []
        for mid, records in mapping_groups.items():
            # 电影/other/all: is_anime=False，防止路径中的 S01/Season 目录被误提取为季分组
            mapping_meta = _process_mapping_partition(
                db, app_service, area, records, mid,
                sort_field, sort_order, page, handler,
                allow_filename_fallback, is_anime=(kind == "anime")
            )
            mappings_result.append(mapping_meta)
        
        # 单一 mapping 向后兼容（扁平响应）
        # 使用 mappings_result[0]["mapping_id"] 而非循环残留变量 mid
        if len(mappings_result) == 1:
            result = mappings_result[0]
            result["area"] = area
            result["media"] = media_name
            result["mapping_id"] = result["mapping_id"]  # 使用分区处理返回的真实 mapping_id
            result["index_metadata"] = db.get_index_metadata(result["mapping_id"])
            handler._send_json(result)
        elif not mappings_result and not all_records:
            # 无记录时返回空 seasons，保持响应契约一致
            handler._send_json({
                "area": area,
                "media": media_name,
                "total": 0,
                "page": 1,
                "total_pages": 1,
                "seasons": [],
            })
        else:
            # 多 mapping 返回 mappings 数组
            total_pages = max(1, ceil(total / PAGE_SIZE)) if total else 1
            page = max(1, min(page, total_pages))
            
            handler._send_json({
                "area": area,
                "media": media_name,
                "total": total,
                "page": page,
                "total_pages": total_pages,
                "mappings": mappings_result,
            })
    else:
        # C 区：不按 mapping 分区，但复用分页切片逻辑
        local_root = ""
        webdav_root = ""
        strm_engine_root = ""
        if all_records:
            local_root = _compute_media_root(all_records[0].get("local_path", ""))
            webdav_root = _compute_media_root(all_records[0].get("webdav_path", ""))
            if app_service and webdav_root:
                engine_paths = app_service._cloud_path_to_engine_paths(webdav_root)
                if engine_paths:
                    strm_engine_root = engine_paths[0]

        total_pages = max(1, ceil(total / PAGE_SIZE)) if total else 1
        page = max(1, min(page, total_pages))
        offset = (page - 1) * PAGE_SIZE
        paged_records = all_records[offset:offset + PAGE_SIZE]

        # 按季分组（使用 allow_filename_fallback + is_anime）
        seasons_map: dict[str, list[dict]] = {}
        for rec in paged_records:
            # 电影/other/all: is_anime=False，防止路径中的 S01/Season 目录被误提取为季分组
            label = _extract_season_from_local_path(rec.get("local_path", ""), allow_filename_fallback, is_anime=(kind == "anime")) or "默认"
            seasons_map.setdefault(label, []).append(rec)

        # 排序
        rev = sort_order.upper() == "DESC"
        if sort_field == "local_path":
            for recs in seasons_map.values():
                recs.sort(key=lambda r: _natural_sort_key(r.get("local_path", "") or ""), reverse=rev)
        elif sort_field in ("updated_at", "moved_at"):
            for recs in seasons_map.values():
                recs.sort(key=lambda r: r.get(sort_field, 0) or 0, reverse=rev)

        seasons = [{"label": lbl, "records": recs} for lbl, recs in seasons_map.items()]

        handler._send_json({
            "area": area,
            "media": media_name,
            "local_root": local_root,
            "webdav_root": webdav_root,
            "strm_engine_root": strm_engine_root,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "seasons": seasons,
        })

def _process_mapping_partition(
    db,
    app_service,
    area: str,
    records: list[dict],
    mapping_id: str,
    sort_field: str,
    sort_order: str,
    page: int,
    handler,
    allow_filename_fallback: bool = True,
    is_anime: bool = True,
) -> dict:
    """处理单个 mapping 分区的数据：独立分页、排序、计算根路径和 index_metadata。"""
    # 计算根路径
    local_root = ""
    webdav_root = ""
    strm_engine_root = ""
    if records:
        local_root = _compute_media_root(records[0].get("local_path", ""))
        webdav_root = _compute_media_root(records[0].get("webdav_path", ""))
        if app_service and webdav_root:
            engine_paths = app_service._cloud_path_to_engine_paths(webdav_root)
            if engine_paths:
                strm_engine_root = engine_paths[0]

    # 独立分页
    total = len(records)
    total_pages = max(1, ceil(total / PAGE_SIZE)) if total else 1
    # 记录请求页码是否被 clamp（多 mapping 时各分区记录数不同，静默截断会让用户看到错误页）
    requested_page = page
    page = max(1, min(page, total_pages))
    clamped = page != requested_page
    offset = (page - 1) * PAGE_SIZE
    paged_records = records[offset:offset + PAGE_SIZE]

    # 按季分组（使用 allow_filename_fallback + is_anime）
    seasons_map: dict[str, list[dict]] = {}
    for rec in paged_records:
        label = _extract_season_from_local_path(rec.get("local_path", ""), allow_filename_fallback, is_anime) or "默认"
        seasons_map.setdefault(label, []).append(rec)

    # 排序
    rev = sort_order.upper() == "DESC"
    if sort_field == "local_path":
        for recs in seasons_map.values():
            recs.sort(key=lambda r: _natural_sort_key(r.get("local_path", "") or ""), reverse=rev)
    elif sort_field == "updated_at":
        for recs in seasons_map.values():
            recs.sort(key=lambda r: r.get("updated_at", 0) or 0, reverse=rev)
    elif sort_field == "last_verified_at":
        for recs in seasons_map.values():
            recs.sort(key=lambda r: r.get("last_verified_at", 0) or 0, reverse=rev)

    seasons = [{"label": lbl, "records": recs} for lbl, recs in seasons_map.items()]

    return {
        "mapping_id": mapping_id,
        "local_root": local_root,
        "webdav_root": webdav_root,
        "strm_engine_root": strm_engine_root,
        "index_metadata": db.get_index_metadata(mapping_id) if mapping_id != "unknown" else None,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "clamped": clamped,
        "seasons": seasons,
    }

def handle_area_refresh(handler, area, body: bytes) -> None:
    """处理 POST /api/area/{area}/refresh — 通过 STRM 入口路径触发引擎刷新并同步到 B 区
    
    Task 2: 支持 mapping_id 参数，按 mapping 过滤 A 区记录
    """
    if area not in ("a", "b"):
        handler._send_json({"error": "无效区域，仅支持 'a' 或 'b'"}, 400)
        return

    # 解析请求体
    try:
        data = json.loads(body) if body else {}
    except (ValueError, json.JSONDecodeError):
        data = {}

    media_name = (data.get("media") or "").strip()
    if not media_name:
        handler._send_json({"error": "缺少 media 参数"}, 400)
        return

    # Task 2: 可选的 mapping_id 参数
    mapping_id = (data.get("mapping_id") or "").strip() or None

    # 路径穿越校验：防止恶意构造路径
    # 检查长度
    if len(media_name) > 255:
        handler._send_json({"error": "媒体名长度超限"}, 400)
        return

    # 检查危险字符和路径分隔符
    dangerous_chars = ['..', '/', '\\', '\x00', ':', '*', '?', '"', '<', '>', '|']
    if any(c in media_name for c in dangerous_chars):
        handler._send_json({"error": "媒体名包含非法字符"}, 400)
        return

    # 检查是否为绝对路径
    try:
        if Path(media_name).is_absolute():
            handler._send_json({"error": "媒体名不能是绝对路径"}, 400)
            return
    except Exception:
        handler._send_json({"error": "无效的媒体名"}, 400)
        return

    # 获取 AppService
    app_service = getattr(handler.webui, '_app_service', None)
    if not app_service:
        handler._send_json({"error": "主程序未运行", "status": "not_running"}, 503)
        return

    # 获取 refresh_lock，防止同一媒体并发刷新
    # 锁在 WebUIServer.__init__ 预建，避免懒初始化非原子导致 409 互斥被绕过
    refresh_lock = handler.webui._refresh_lock

    if not refresh_lock.acquire(blocking=False):
        handler._send_json({"error": "刷新进行中，请稍后再试"}, 409)
        return

    try:
        result = _do_media_refresh(app_service, area, media_name, mapping_id=mapping_id)
        # 业务失败返回 400 而非 200。前端 api.js 正确提取 err.error，
        # 具体错误信息不丢失；但 HTTP 状态码此前恒为 200，掩盖了刷新失败。
        handler._send_json(result, 200 if result.get("ok") else 400)
    except Exception as e:
        logging.exception("[Refresh] 刷新媒体 %s 失败: %s", media_name, e)
        handler._send_json({"error": "internal_error", "status": "error"}, 500)
    finally:
        refresh_lock.release()

def _do_media_refresh(app_service, area: str, media_name: str, mapping_id: str | None = None) -> dict:
    """执行媒体刷新逻辑：通过 STRM 入口路径触发引擎重新生成，然后同步到 B 区。
    
    Task 2: 支持 mapping_id 参数，按 mapping 过滤 A 区记录
    """
    db = app_service.db
    admin_api = app_service.admin_api
    now_verified = time.time()  # D'.3: 默认时间戳，成功路径会在 step 5.1 更新

    # 读取刷新日志级别
    app_config = getattr(app_service, 'config', None)
    log_level_name = "INFO"
    if app_config and hasattr(app_config, 'refresh'):
        log_level_name = getattr(app_config.refresh, 'log_level', "INFO").upper()
    _refresh_log = _make_refresh_logger(log_level_name)

    _refresh_log("info", "[Refresh] 开始刷新 媒体=%s 区=%s mapping_id=%s", media_name, area, mapping_id)

    # 1. 从 A 区 DB 查询该媒体的所有记录（Task 2: 支持 mapping_id 过滤）
    # 注意：LIKE '%media_name%' 是子串匹配，理论上当两部媒体名互为子串时会误匹配
    # （如 '巨人' 会命中 '进击的巨人'）。此处依赖后续 _compute_common_parent_path
    # 计算公共父目录 + '/' 根目录保护来收敛范围；若误匹配导致跨目录，公共父目录会退化为
    # '/' 并被上面的边界保护拒绝，因此不会触发全盘刷新。media_name 已在调用方做过
    # 路径分隔符/危险字符校验。
    phase_start = time.monotonic()
    a_records = []
    try:
        with db.read_connection() as conn:
            conn.row_factory = sqlite3.Row
            # 转义 media_name 中的 LIKE 通配符（% _ \），配合 ESCAPE '\' 子句。
            # 下划线在媒体名中极常见（如 S01_E01、The_Movie），不转义会被当作单字符通配符过度匹配。
            like = f"%{escape_like(media_name)}%"
            
            # Task 2: 如果指定了 mapping_id，通过 app_service 获取对应的 A 区路径进行过滤
            if mapping_id and app_service:
                # 通过 mapping_id 找到对应的 a_root
                a_root = None
                for m in getattr(app_service, 'a_b_mappings', []):
                    if str(getattr(m, 'mapping_id', '')).strip() == mapping_id:
                        a_root = str(normalize_local_root(getattr(m, 'a_root', '')))
                        break
                
                if a_root:
                    # 按 a_root 和 media_name 过滤 A 区记录
                    like_root = f"%{escape_like(a_root)}%"
                    rows = conn.execute(
                        "SELECT local_path, webdav_path, parent_webdav_path FROM a_strm_files "
                        "WHERE (local_path LIKE ? ESCAPE '\\' OR webdav_path LIKE ? ESCAPE '\\') "
                        "AND (local_path LIKE ? ESCAPE '\\' OR webdav_path LIKE ? ESCAPE '\\')",
                        (like_root, like_root, like, like)
                    ).fetchall()
                else:
                    # mapping_id 未找到匹配的 a_root，使用默认查询
                    rows = conn.execute(
                        "SELECT local_path, webdav_path, parent_webdav_path FROM a_strm_files "
                        "WHERE local_path LIKE ? ESCAPE '\\' OR webdav_path LIKE ? ESCAPE '\\'",
                        (like, like)
                    ).fetchall()
            else:
                # 无 mapping_id，使用默认查询（向后兼容）
                rows = conn.execute(
                    "SELECT local_path, webdav_path, parent_webdav_path FROM a_strm_files "
                    "WHERE local_path LIKE ? ESCAPE '\\' OR webdav_path LIKE ? ESCAPE '\\'",
                    (like, like)
                ).fetchall()
            
            a_records = [dict(r) for r in rows]
    except Exception as e:
        logging.error("[Refresh] 查询 A 区记录失败: %s", e)
        # M-13: 不回传原始异常信息
        return {"ok": False, "error": "query_failed"}

    _refresh_log("debug", "[Refresh] 查询完成 耗时=%.2fs 记录数=%d",
                 time.monotonic() - phase_start, len(a_records))

    if not a_records:
        return {"ok": True, "message": "未找到相关记录"}

    # 2. 计算媒体目录的公共父目录（云盘路径）
    parent_paths = [r.get("parent_webdav_path", "") for r in a_records if r.get("parent_webdav_path")]
    if not parent_paths:
        return {"ok": True, "message": "无父目录信息"}

    common_parent = _compute_common_parent_path(parent_paths)
    if not common_parent:
        return {"ok": True, "message": "无法确定公共父目录"}
    # 边界保护：不同根目录时 _compute_common_parent_path 返回 "/"，
    # 若用 "/" 触发刷新会波及整个云盘根，代价高且不安全，拒绝执行。
    if common_parent == "/":
        _refresh_log("warning",
                     "[Refresh] 公共父目录退化为根目录 '/'，拒绝全盘刷新: media=%s", media_name)
        return {"ok": False, "error": "拒绝全盘刷新", "message": "公共父目录退化为根目录，已拒绝全盘刷新"}

    # 3. 映射到 STRM 引擎入口路径
    engine_parent = ""
    if app_service:
        engine_paths = app_service._cloud_path_to_engine_paths(common_parent)
        if engine_paths:
            engine_parent = engine_paths[0]
    refresh_dir = engine_parent or common_parent  # 降级到云盘路径

    # 4. 调用 OpenList /api/fs/list 带 refresh=true 触发 STRM 引擎重新生成
    _refresh_log("info", "[Refresh] 调用引擎 API 云盘目录=%s (引擎入口=%s)",
                 common_parent, refresh_dir)
    phase_start = time.monotonic()
    list_result = admin_api.list_directory(refresh_dir, refresh=True)
    _refresh_log("debug", "[Refresh] 引擎 API 完成 耗时=%.2fs", time.monotonic() - phase_start)

    if list_result is None or list_result.get("code") not in (0, 200):
        # M-13: 不回传原始 API 响应（可能含内部路径/服务端详情）
        logging.error("[Refresh] OpenList API 返回异常: code=%s", list_result.get("code") if isinstance(list_result, dict) else None)
        return {"ok": False, "error": "OpenList API 返回错误"}

    # 检查 API 返回内容是否为空（可能是云盘临时不可达或目录不存在）
    _data = list_result.get("data", {})
    _content = _data.get("content", []) if isinstance(_data, dict) else []
    if not _content:
        _refresh_log("warning", "[Refresh] API 返回目录为空: dir=%s (可能是云盘不可达或目录不存在)", refresh_dir)

    # 5. 仅同步当前媒体的 A→B（只处理步骤 1 已查出的记录，避免全库全量同步）
    #    直接复用步骤 1 内存中的 a_records，逐条调用 copy_a_record_to_b_if_needed，
    #    不读全库、不触发 [初始化] 全量扫描。数据库规模无论多大，此处只处理该媒体的记录。
    phase_start = time.monotonic()
    synced = 0
    skipped = 0
    failed = 0
    for rec in a_records:
        a_local = rec.get("local_path", "")
        a_webdav = rec.get("webdav_path", "")
        a_parent = rec.get("parent_webdav_path", "")
        if not a_local or not a_webdav:
            continue
        if not Path(a_local).exists():
            _refresh_log("debug", "[Refresh] A→B跳过 源文件不存在: %s", a_local)
            skipped += 1
            continue
        try:
            # ghost 保护 / 指纹去重由 copy_a_record_to_b_if_needed 内部处理
            copy_result = app_service.copy_a_record_to_b_if_needed(a_local, a_webdav, a_parent)
            if copy_result is True:
                synced += 1
            elif copy_result is None:
                skipped += 1
            else:
                failed += 1
        except Exception as e:
            logging.warning("[Refresh] A→B 单条同步失败 %s: %s", a_local, e)
            failed += 1

    _refresh_log("info",
                 "[Refresh] 同步到 B 区完成 耗时=%.2fs 成功=%d 跳过=%d 失败=%d",
                 time.monotonic() - phase_start, synced, skipped, failed)

    # 5.1 标记 last_verified_at（单剧目刷新后推进核对时间）
    now_verified = time.time()
    try:
        a_local_paths = [r.get("local_path", "") for r in a_records if r.get("local_path")]
        source_a_paths = [r.get("local_path", "") for r in a_records if r.get("local_path")]
        if a_local_paths:
            db.touch_verified_a(a_local_paths, now_verified)
        if source_a_paths:
            db.touch_verified_b(source_a_paths, now_verified)
    except Exception as e:
        logging.warning("[Refresh] 更新 last_verified_at 失败: %s", e)

    # 6. 局部冗余检查：清理该媒体目录下的 B 区僵尸文件（云端已删除但本地残留）
    # 设计原则：冗余清理永远只在局部触发，不做全盘扫描
    if app_service and common_parent:
        try:
            app_service.cleanup_b_zombies_under_folder(common_parent)
        except Exception as e:
            logging.warning("[Refresh] 局部冗余清理失败 %s: %s", common_parent, e)

    _refresh_log("info", "[Refresh] 刷新完成 目录=%s", refresh_dir)

    return {
        "ok": True,
        "message": f"刷新完成：同步 {synced}，跳过 {skipped}，失败 {failed}",
        "refresh_dir": refresh_dir,
        "synced": synced,
        "skipped": skipped,
        "failed": failed,
        "verified_at": now_verified,
    }

def _make_refresh_logger(level_name: str):
    """根据日志级别名称创建刷新日志辅助函数。

    返回一个函数 log(level, msg, *args)，其中 level 是单次调用的级别，
    只有 >= level_name 的日志才会实际输出。
    """
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
    }
    threshold = level_map.get(level_name, logging.INFO)

    def _log(level: str, msg: str, *args) -> None:
        numeric = level_map.get(level.upper(), logging.INFO)
        if numeric >= threshold:
            logging.log(numeric, msg, *args)

    return _log

def _compute_common_parent_path(paths: list[str]) -> str:
    """计算路径列表的最长公共父目录"""
    if not paths:
        return ""
    if len(paths) == 1:
        return paths[0]

    # 分割路径为组件
    split_paths = [p.strip("/").split("/") for p in paths if p]
    if not split_paths:
        return ""

    # 找出公共前缀
    common = []
    for parts in zip(*split_paths):
        if len(set(parts)) == 1:
            common.append(parts[0])
        else:
            break

    if not common:
        return "/"
    return "/" + "/".join(common)

def _parse_api_files(list_result: dict, parent_path: str) -> list[dict]:
    """解析 OpenList API 返回的文件列表，只保留 .strm 和字幕文件"""
    from media_renamer import is_subtitle_file

    data = list_result.get("data", {})
    content = data.get("content", []) if isinstance(data, dict) else []
    if content is None:
        content = []

    files = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("is_dir", False):
            continue
        name = item.get("name", "")
        if not name:
            continue
        # 只保留 .strm 和字幕文件
        if name.endswith(".strm") or is_subtitle_file(name):
            webdav_path = f"{parent_path.rstrip('/')}/{name}"
            files.append({
                "name": name,
                "webdav_path": webdav_path,
            })
    return files

def _read_log_file_tail(log_file: Path | str, lines_req: int) -> list[str]:
    """读取日志文件的最后 N 行。

    优化：仅读取文件末尾的字节，避免大文件全量读取。
    估算每行平均 300 字节（考虑多字节中文字符），读取 lines_req * 300 字节。
    """
    log_file = Path(log_file)
    if not log_file.exists():
        return []

    try:
        # 优化：仅读取文件末尾的字节，避免大文件全量读取阻塞 HTTP 线程
        # 估算每行平均 300 字节（考虑多字节中文字符），读取 lines_req * 300 字节足够
        read_size = lines_req * 300
        file_size = log_file.stat().st_size
        with log_file.open("rb") as f:
            if file_size > read_size:
                f.seek(file_size - read_size)
                # 跳过可能读取到的不完整行
                f.readline()
                tail_bytes = f.read()
                tail = tail_bytes.decode("utf-8", errors="replace").splitlines()
            else:
                tail = f.read().decode("utf-8", errors="replace").splitlines()
        # 仅返回最后 lines_req 行
        return tail[-lines_req:]
    except Exception:
        return []

def handle_logs_api(handler, params: dict) -> None:
    """处理 GET /api/logs"""
    # 限制 lines 参数范围，防止 DoS（内存耗尽）
    lines_req = max(1, min(_safe_int(params.get("lines", ["200"])[0], 200), 5000))

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
        tail = _read_log_file_tail(log_file, lines_req)
        tail = tail[::-1]  # 反转为倒序（最新在上），与 TMDB 操作日志保持一致
        handler._send_json({"lines": tail, "count": len(tail)})
    except Exception as e:
        logging.exception("[WebUI] 读取日志尾部失败: %s", e)
        handler._send_json({"error": "internal_error"}, 500)

def handle_download_log_api(handler, params: dict) -> None:
    """处理 GET /api/logs/download - 下载完整的日志文件"""
    log_file_path = None
    cfg = handler.webui._config
    cfg_log = getattr(cfg, 'log', None)
    if cfg_log and hasattr(cfg_log, 'file'):
        log_file_path = Path(cfg_log.file)
    if log_file_path is None or not log_file_path.exists():
        fallback = getattr(handler.webui, '_log_file', None)
        if fallback:
            log_file_path = Path(fallback)

    if not log_file_path or not log_file_path.exists():
        handler._send_json({"error": "Log file not found"}, 404)
        return

    try:
        # 分块流式写 + Content-Length，避免整文件读入内存
        file_size = log_file_path.stat().st_size
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/octet-stream')
        safe_name = re.sub(r'[^\w.\-]', '_', log_file_path.name)
        handler.send_header('Content-Disposition', f'attachment; filename="{safe_name}"')
        handler.send_header('Content-Length', str(file_size))
        handler.end_headers()
        with open(log_file_path, 'rb') as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                handler.wfile.write(chunk)
    except Exception as e:
        # 流式写阶段 headers 已发送后不得再调 _send_json（会抛
        # "headers already sent" 二次异常）。仅记录日志并关闭连接，客户端收到
        # 截断的 body 即可判断失败。
        logging.exception("[WebUI] 下载日志文件失败: %s", e)
        try:
            handler.wfile.close()
        except Exception:
            pass
        try:
            handler.connection.close()
        except Exception:
            pass

def handle_config_api(handler) -> None:
    """处理 GET /api/config — 归一化配置字段，兼容 WebUIConfig 和 AppConfig

    TMDB 配置优先从 DB (webui_config scope=tmdb) 读取，
    如果 DB 无数据则回退到内存 TmdbConfig 对象。
    OpenList 配置优先从 DB (webui_config scope=openlist) 读取。
    
    M-3: 未认证时只返回状态 booleans（configured/not_configured），
         完整配置信息需认证后获取，防止局域网信息泄露。
    """
    # b_root/a_folders）有意在登录前可读，以便 SPA/onboarding 在鉴权前渲染（服务绑定局域网）。
    cfg = handler.webui._config
    tmdb_client = handler.webui._tmdb_client
    tmdb_cfg = getattr(cfg, "tmdb", None)

    # 尝试从 DB 读取配置
    _wdb = getattr(handler.webui, '_watchlist_db', None)
    db_tmdb_cfg = _wdb.get_all_config("tmdb") if _wdb else {}
    db_openlist_cfg = _wdb.get_all_config("openlist") if _wdb else {}

    # TMDB token 相关 — DB 优先
    token = db_tmdb_cfg.get("access_token", "") or (
        getattr(tmdb_cfg, "access_token", "") if tmdb_cfg else "")
    token_configured = bool(token)

    # 统一调用 handler._validate_session_token 做会话校验，
    # 与 server._check_auth 的常规路径一致：含滑动过期续期（7 天）与
    # stored_ip=="" 兼容。原自定义实现既不滑动续期、又要求 stored_ip 严格
    # 相等，导致客户端只轮询 /api/config 时 7 天过期不滑动、空 IP 会话被误拒。
    token_header = getattr(handler.headers, 'get', lambda k, d=None: d)('X-Session-Token', '')
    is_authenticated = False
    if token_header and handler.webui._has_password:
        client_ip = handler.client_address[0] if handler.client_address else ""
        is_authenticated = handler._validate_session_token(token_header, client_ip)

    if not is_authenticated:
        # M-3: 未认证时只返回最基本的状态 booleans，防止信息泄露
        handler._send_json({
            # 只返回是否已配置的状态，不返回具体值
            "tmdb_configured": bool(tmdb_client),
            "tmdb_token_configured": token_configured,
            "tmdb_api_key_configured": bool(db_tmdb_cfg.get("api_key", "") or (
                getattr(tmdb_cfg, "api_key", "") if tmdb_cfg else "")),
            "tmdb_proxy_configured": bool(db_tmdb_cfg.get("proxy_http", "") or (
                getattr(tmdb_cfg, "proxy_http", "") if tmdb_cfg else "")),
            "webdav_configured": bool(db_openlist_cfg.get("webdav_host", "") or (
                getattr(getattr(cfg, "webdav", None), "host", "") if getattr(cfg, "webdav", None) else "")),
            # 认证状态
            "_authenticated": False,
            "_message": "未认证，仅返回配置状态。请登录后获取完整配置。",
        })
        return

    # 认证通过，返回完整配置
    token_configured = bool(token)

    # db_file 固定在项目根，仅返回固定路径 + 存在状态，只读
    project_root = (getattr(handler.webui, '_project_root', None)
                    or Path(__file__).resolve().parent.parent.parent)
    db_file = os.path.normpath(str(project_root / "bridge.db"))

    # 日志文件路径 — 兼容两种配置结构
    # AppConfig: cfg.log.file
    log_file = ""
    log_cfg = getattr(cfg, "log", None)
    if log_cfg and hasattr(log_cfg, "file"):
        log_file = log_cfg.file
    elif hasattr(cfg, "log_file"):
        log_file = cfg.log_file

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

    # WebDAV - 优先从 DB 读取
    webdav_cfg = getattr(cfg, "webdav", None)
    db_openlist_cfg = _wdb.get_all_config("openlist") if _wdb else {}
    webdav_host = db_openlist_cfg.get("webdav_host", "") or (
        getattr(webdav_cfg, "host", "") if webdav_cfg else "")
    webdav_user = db_openlist_cfg.get("webdav_user", "") or (
        getattr(webdav_cfg, "user", "") if webdav_cfg else "")
    webdav_password = bool(
        db_openlist_cfg.get("webdav_password", "") or (
            getattr(webdav_cfg, "password", "") if webdav_cfg else ""))
    webdav_totp_secret = bool(
        db_openlist_cfg.get("webdav_totp_secret", "") or (
            getattr(webdav_cfg, "totp_secret", "") if webdav_cfg else ""))

    # a_folders — 只从用户配置的 STRM 引擎获取，不自动加载所有引擎
    # 从 DB 读取用户配置的 strm_engines
    a_folders = []
    strm_map = getattr(cfg, "strm_storage_map", {})
    try:
        strm_engines_json = db_openlist_cfg.get("strm_engines", "[]")
        strm_engines = json.loads(strm_engines_json) if strm_engines_json else []

        # 从用户配置的引擎中提取 local_path
        for eng in strm_engines:
            if eng.get("engine"):
                # 从 strm_storage_map 获取对应的 local_path
                mount_path = eng["engine"]
                if mount_path in strm_map:
                    local_path = strm_map[mount_path].local_path
                    if local_path and local_path not in a_folders:
                        a_folders.append(local_path)
    except Exception as e:
        logging.debug("[Config] 从用户配置获取 a_folders 失败: %s", e)

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
    refresh_full_audit_interval_days = getattr(
        refresh_cfg, "full_audit_interval_days", 7) if refresh_cfg else 7

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
    # tmdb_watchlist_db 固定在项目根，只读，不从 webui_config 读取
    tmdb_watchlist_db = str(project_root / "tmdb_watchlist.db")
    # TMDB Watchlist 启用/禁用开关 — DB 优先，默认启用
    tmdb_watchlist_enabled_raw = db_tmdb_cfg.get("watchlist_enabled", "")
    if tmdb_watchlist_enabled_raw != "":
        tmdb_watchlist_enabled = str(
            tmdb_watchlist_enabled_raw).lower() in ("true", "1", "yes")
    else:
        tmdb_watchlist_enabled = True

    # TMDB 匹配参数 — DB 优先，回退到内存 TmdbConfig
    tmdb_fuzzy_threshold = db_tmdb_cfg.get("fuzzy_threshold", "") or (
        str(getattr(tmdb_cfg, "fuzzy_threshold", "0.60")) if tmdb_cfg else "0.60")
    tmdb_anime_min_ep_ratio = db_tmdb_cfg.get("anime_min_ep_ratio", "") or (
        str(getattr(tmdb_cfg, "anime_min_ep_ratio", "0.30")) if tmdb_cfg else "0.30")
    # 回退默认值统一为 0.3，与 config.py 的
    # anime_max_season_diff: float = 0.3 一致（原为 "1"）。
    tmdb_anime_max_season_diff = db_tmdb_cfg.get("anime_max_season_diff", "") or (
        str(getattr(tmdb_cfg, "anime_max_season_diff", "0.3")) if tmdb_cfg else "0.3")
    tmdb_anime_min_season_ratio = db_tmdb_cfg.get("anime_min_season_ratio", "") or (
        str(getattr(tmdb_cfg, "anime_min_season_ratio", "0.3")) if tmdb_cfg else "0.3")
    tmdb_cache_ttl = db_tmdb_cfg.get("watchlist_cache_ttl", "") or (
        str(getattr(tmdb_cfg, "watchlist_cache_ttl", "604800")) if tmdb_cfg else "604800")

    handler._send_json({
        # 数据库 & WebUI
        "db_file": db_file,
        "db_exists": os.path.isfile(db_file) if db_file else False,
        "webui_port": webui_port,
        "webui_bind": webui_bind,
        # TMDB 认证
        "tmdb_configured": bool(tmdb_client),
        "tmdb_token_configured": token_configured,
        "tmdb_language": tmdb_language,
        "tmdb_host": tmdb_host,
        # tmdb_api_key 不返回明文（B-3）：仅返回是否已配置的布尔值，
        # 防止未认证客户端通过白名单接口窃取完整 API key。
        "tmdb_api_key": bool(tmdb_api_key),
        "tmdb_api_key_configured": bool(tmdb_api_key),
        # tmdb_proxy_configured: 是否配置了代理（脱敏，不返回完整 URL）
        # /api/config 是白名单端点（无需认证），返回完整代理 URL 会泄露内网代理地址
        "tmdb_proxy_configured": bool(tmdb_proxy_http),
        # tmdb_proxy_enabled: DB 中存储的代理启用开关
        "tmdb_proxy_enabled": tmdb_proxy_enabled,
        "tmdb_account_id": tmdb_client.account_id if tmdb_client else None,
        "tmdb_watchlist_db": tmdb_watchlist_db,
        "tmdb_watchlist_enabled": tmdb_watchlist_enabled,
        "tmdb_fuzzy_threshold": tmdb_fuzzy_threshold,
        "tmdb_anime_min_ep_ratio": tmdb_anime_min_ep_ratio,
        "tmdb_anime_max_season_diff": tmdb_anime_max_season_diff,
        "tmdb_anime_min_season_ratio": tmdb_anime_min_season_ratio,
        "tmdb_cache_ttl": tmdb_cache_ttl,
        # 路径 & 文件夹
        "b_root": b_root,
        "c_root": c_root,
        "a_folders": a_folders,
        # a_b_mappings 补充 mapping_id，与 dashboard 一致
        "a_b_mappings": [
            {"a_root": m.a_root, "b_root": m.b_root, "label": m.label,
             "mapping_id": m.mapping_id}
            for m in getattr(cfg, "a_b_mappings", [])
        ],
        "strm_engine_paths": strm_engine_paths,
        "refresh_paths": refresh_paths_val,
        # WebDAV（M-3: 认证后才返回敏感信息）
        "webdav_host": webdav_host,
        "webdav_user": webdav_user,
        "webdav_password": webdav_password,
        "webdav_totp_secret": webdav_totp_secret,
        # Refresh
        "refresh_enabled": refresh_enabled,
        "refresh_interval": refresh_interval,
        "refresh_full_audit_interval_days": refresh_full_audit_interval_days,
        # Behavior
        "behavior_action": behavior_action,
        "ghost_protect_seconds": ghost_protect_seconds,
        # M-3: 认证状态标记
        "_authenticated": True,
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
    """POST /api/main/start — 启动主程序
    
    业务失败（未配置/fail-safe/登录失败等）返回 200 + success:false，
    与 _handle_openlist_test_connection 的约定一致；
    仅服务层未预期异常返回 500 + error_type: "exception"。
    """
    if not webui_server:
        handler._send_json({"success": False, "message": "WebUI 服务器未初始化"}, 500)
        return True

    result = webui_server.start_main()
    status_code = 500 if result.get("error_type") == "exception" else 200
    handler._send_json(result, status_code)
    return True

def _handle_main_stop(handler, webui_server) -> bool:
    """POST /api/main/stop — 停止主程序
    
    同 _handle_main_start：业务失败 200，内部异常 500。
    """
    if not webui_server:
        handler._send_json({"success": False, "message": "WebUI 服务器未初始化"}, 500)
        return True

    result = webui_server.stop_main()
    status_code = 500 if result.get("error_type") == "exception" else 200
    handler._send_json(result, status_code)
    return True

# ============================================================
# 配置状态 & 启动预检 API
# ============================================================

def _handle_config_status(handler, webui_server) -> None:
    """GET /api/config/status — 返回配置完成状态，供首次引导使用。

    返回各配置步骤的完成状态：
    - password_set: 管理员密码是否已设置（始终为 True，首次启动自动生成）
    - tmdb_configured: TMDB 是否已配置
    - openlist_configured: OpenList WebDAV 是否已配置
    - main_running: 主程序是否在运行
    - onboarding_completed: 引导是否已完成或已跳过
    """
    if not webui_server:
        handler._send_json({
            "password_set": False,
            "tmdb_configured": False,
            "openlist_configured": False,
            "main_running": False,
            "onboarding_completed": False,
        })
        return

    _wdb = getattr(webui_server, '_watchlist_db', None)

    # password_set: 始终为 True（server.py 启动时自动生成）
    password_set = bool(getattr(webui_server, '_has_password', False))

    # tmdb_configured: 仅查 DB，不查 client（client 为 None 说明未配置）
    tmdb_configured = False
    if _wdb:
        try:
            db_tmdb = _wdb.get_all_config("tmdb") or {}
            token = db_tmdb.get("access_token", "")
            api_key = db_tmdb.get("api_key", "")
            tmdb_configured = bool(token or api_key)
        except Exception:
            pass

    # openlist_configured: 检查 host 是否非空
    host, _user, _password, _totp = _openlist_merged_webdav_cfg(webui_server)
    openlist_configured = bool(host)

    # main_running
    main_running = bool(getattr(webui_server, '_app_running', False))

    # onboarding_completed: 检查 DB 中的标记
    onboarding_completed = False
    if _wdb:
        try:
            val = _wdb.get_config("ui", "onboarding_completed", "")
            onboarding_completed = val == "1"
        except Exception:
            pass

    # 新增引导步骤完成状态
    view_ab_completed = False
    tmdb_refresh_completed = False
    tmdb_match_completed = False
    if _wdb:
        try:
            view_ab_completed = _wdb.get_config("ui", "onboarding_view_ab_completed", "") == "1"
            tmdb_refresh_completed = _wdb.get_config("ui", "onboarding_tmdb_refresh_completed", "") == "1"
            tmdb_match_completed = _wdb.get_config("ui", "onboarding_tmdb_match_completed", "") == "1"
        except Exception:
            pass

    handler._send_json({
        "password_set": password_set,
        "tmdb_configured": tmdb_configured,
        "openlist_configured": openlist_configured,
        "main_running": main_running,
        "onboarding_completed": onboarding_completed,
        "view_ab_completed": view_ab_completed,
        "tmdb_refresh_completed": tmdb_refresh_completed,
        "tmdb_match_completed": tmdb_match_completed,
    })

def _handle_config_validate(handler, webui_server) -> None:
    """POST /api/config/validate — 启动主程序前的预检。

    检查项：
    1. OpenList 配置是否已保存（host 非空）
    2. OpenList 服务器是否可达（ping）
    3. TMDB 是否已配置（警告级别，非阻塞）

    返回：
    - ok: 是否所有必要检查通过
    - checks: 各项检查结果列表
    """
    if not webui_server:
        handler._send_json({"ok": False, "error": "WebUI 服务器未初始化"}, 500)
        return

    _wdb = getattr(webui_server, '_watchlist_db', None)
    checks = []

    # --- 检查项 1: OpenList 配置 ---
    host, user, password, totp_secret = _openlist_merged_webdav_cfg(webui_server)
    if not host:
        checks.append({
            "name": "openlist_config",
            "label": "OpenList 配置",
            "status": "error",
            "message": "OpenList WebDAV 地址未配置",
            "suggestion": "请前往「OpenList 配置」页面填写 WebDAV 地址",
        })
        # 未配置则跳过 ping 检查
        checks.append({
            "name": "openlist_online",
            "label": "OpenList 连接",
            "status": "skipped",
            "message": "配置未完成，跳过连接检测",
        })
    else:
        checks.append({
            "name": "openlist_config",
            "label": "OpenList 配置",
            "status": "ok",
            "message": f"已配置: {html_module.escape(host)}",
        })
        # --- 检查项 2: OpenList 可达性 ---
        try:
            from webdav_client import OpenListAdminClient
            client = OpenListAdminClient(host, user, password, totp_secret=totp_secret)
            if client.login(force=True):
                checks.append({
                    "name": "openlist_online",
                    "label": "OpenList 连接",
                    "status": "ok",
                    "message": "连接成功",
                })
            else:
                error_type = client.last_error_type or "unknown"
                error_messages = {
                    "wrong_password": "密码错误，请检查用户名和密码",
                    "wrong_2fa": "2FA 验证码错误，请检查 2FA 密钥",
                    "account_not_found": "账号不存在，请检查用户名",
                    "network_error": "无法连接到 OpenList 服务器，请检查地址和网络",
                    "not_configured": "OpenList 地址无效",
                    "invalid_totp": "TOTP 密钥无效",
                    "unknown": "登录失败，请检查配置",
                }
                checks.append({
                    "name": "openlist_online",
                    "label": "OpenList 连接",
                    "status": "error",
                    "message": error_messages.get(error_type, "连接失败"),
                    "suggestion": "请检查 OpenList 配置或网络连通性",
                })
        except Exception as e:
            # M-13: 通用消息，不向前端泄露异常详情（HTML 转义仅防 XSS，不防信息泄露）
            logging.debug("[仪表盘] OpenList 连接检查异常: %s", e, exc_info=True)
            checks.append({
                "name": "openlist_online",
                "label": "OpenList 连接",
                "status": "warning",
                "message": "连接异常",
                "suggestion": "请检查网络或 OpenList 服务状态",
            })

    # --- 检查项 3: TMDB（警告级别，非阻塞） ---
    tmdb_configured = False
    if _wdb:
        try:
            db_tmdb = _wdb.get_all_config("tmdb") or {}
            tmdb_configured = bool(db_tmdb.get("access_token", "") or db_tmdb.get("api_key", ""))
        except Exception:
            pass
    if not tmdb_configured:
        tmdb_client = getattr(webui_server, '_tmdb_client', None)
        tmdb_configured = bool(tmdb_client)

    if tmdb_configured:
        checks.append({
            "name": "tmdb_config",
            "label": "TMDB 配置",
            "status": "ok",
            "message": "已配置",
        })
    else:
        checks.append({
            "name": "tmdb_config",
            "label": "TMDB 配置",
            "status": "warning",
            "message": "TMDB 未配置（不影响主程序启动）",
            "suggestion": "可选：前往「TMDB 配置」页面填写 API Token 以启用待看列表功能",
        })

    # 汇总：仅 openlist_config 为 error 时阻塞（openlist_online 降级为警告）
    has_blocker = any(
        c["status"] == "error" and c["name"] == "openlist_config"
        for c in checks
    )

    handler._send_json({
        "ok": not has_blocker,
        "checks": checks,
    })

def _handle_onboarding_complete_step(handler, webui_server, body: bytes) -> None:
    """POST /api/onboarding/complete-step — 手动标记引导步骤完成"""
    try:
        data = json.loads(body) if body else {}
    except (ValueError, json.JSONDecodeError):
        data = {}

    step = data.get("step", "")

    if step not in ("view_ab", "tmdb_refresh", "tmdb_match"):
        handler._send_json({"error": "invalid step"}, 400)
        return

    _wdb = getattr(webui_server, '_watchlist_db', None)
    if _wdb:
        try:
            _wdb.set_config("ui", f"onboarding_{step}_completed", "1")
        except Exception as e:
            logging.exception("[Onboarding] 标记步骤完成失败: %s", e)
            handler._send_json({"error": "internal_error"}, 500)
            return

    handler._send_json({"ok": True})

# ============================================================
# Task A: 手动全量审计端点
# ============================================================

def handle_index_audit(handler, body: bytes) -> None:
    """POST /api/index/audit — 触发手动全量审计（异步）"""
    webui_server = handler.webui

    # 检查主程序是否在运行
    app_service = getattr(webui_server, '_app_service', None)
    if app_service is None:
        handler._send_json({
            "ok": False,
            "status": "not_configured",
            "message": "主程序未运行，无法执行审计"
        }, 400)
        return

    # 检查引擎是否 ready
    if not getattr(app_service, '_running', False):
        handler._send_json({
            "ok": False,
            "status": "not_configured",
            "message": "引擎未就绪，无法执行审计"
        }, 400)
        return

    # 检查并发互斥
    with webui_server._index_audit_lock:
        if webui_server._index_audit_running:
            handler._send_json({
                "ok": True,
                "status": "already_running",
                "message": "审计已在进行中"
            })
            return
        # 设置进行中标记
        webui_server._index_audit_running = True
        webui_server._index_audit_result = None

    # 后台线程执行审计
    def _do_audit():
        try:
            refresh_service = getattr(app_service, 'refresh_service', None)
            if refresh_service is None:
                with webui_server._index_audit_lock:
                    webui_server._index_audit_result = {"error": "刷新服务未初始化"}
                return

            # A'.1: 调用 RefreshService.run_full_audit_now()，不再内联审计逻辑
            result = refresh_service.run_full_audit_now()

            with webui_server._index_audit_lock:
                webui_server._index_audit_result = result

        except Exception as e:
            logging.error("[IndexAudit] 审计失败: %s", e, exc_info=True)
            with webui_server._index_audit_lock:
                webui_server._index_audit_result = {"error": "internal_error"}

        finally:
            # 清除进行中标记
            with webui_server._index_audit_lock:
                webui_server._index_audit_running = False

    # 启动后台线程
    import threading
    audit_thread = threading.Thread(target=_do_audit, daemon=True)
    audit_thread.start()

    handler._send_json({
        "ok": True,
        "status": "started",
        "message": "审计已启动"
    })

def handle_index_audit_status(handler) -> None:
    """GET /api/index/audit/status — 查询审计进度"""
    webui_server = handler.webui

    with webui_server._index_audit_lock:
        running = webui_server._index_audit_running
        result = webui_server._index_audit_result

    handler._send_json({
        "running": running,
        "result": result
    })

