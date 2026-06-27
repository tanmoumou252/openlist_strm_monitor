"""
独立 WebUI 测试面板 - SPA 架构
可单独运行，直接读取 config.toml 和现有数据库，无需启动主程序。
集成了 TMDB 测试功能（待看列表、搜索、别名、详情）。
导航/筛选/分页通过 JS 拦截链接，fetch 获取 HTML 片段，只替换 <main> 内容。
"""
# autopep8: off
# isort: off
from __future__ import annotations

import json
import logging
import os
import random
import socket
import sys
import threading
import time
import tomllib
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from math import ceil
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ============================================================
# 路径设置（必须在项目模块导入之前）
# ============================================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC_DIR))

from tmdb_watchlist_db import TmdbWatchlistDb
from watchlist_match import (
    _media_info, _category_filter_value,
    _compute_media_root, _extract_season_from_local_path,
    collect_b_media_snapshot as _collect_b_media_snapshot,
    score_watchlist_item as _score_watchlist_item,
    refresh_watchlist_match_state as _refresh_watchlist_match_state,
)

# ============================================================
# 日志
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_webui")

# ============================================================
# 导入项目模块（sys.path 已设置，可以正常导入）
# ============================================================
from tmdb_client import TmdbClient, create_tmdb_client  # noqa: E402
from database import Database  # noqa: E402
from webui_routes import _build_img_opener, _tmdb_routes as _webui_tmdb_routes, _safe_int, _is_lan_ip  # noqa: E402

# autopep8: on
# isort: on
# ============================================================
# 配置加载（简化版，无需 OpenList API）
# ============================================================


class SimpleWebUIConfig:
    """简化的 WebUI 配置"""
    enabled: bool = True
    port: int = 8579
    bind: str = "0.0.0.0"


class SimpleTmdbConfig:
    """简化的 TMDB 配置"""
    access_token: str = ""
    language: str = "zh-CN"
    host: str = ""
    api_key: str = ""
    proxy_enabled: bool = False
    proxy_http: str = ""
    csv_watchlist_file: str = "./watchlist.csv"
    watchlist_cache_ttl: int = 604800  # 缓存过期时间（秒），默认 7 天
    watchlist_db: str = ""
    fuzzy_threshold: float = 0.60
    anime_min_ep_ratio: float = 0.3


class SimpleConfig:
    """简化的应用配置"""

    def __init__(self):
        self.db_file: str = ""
        self.webui = SimpleWebUIConfig()
        self.tmdb = SimpleTmdbConfig()


def load_config(config_path: str | Path) -> SimpleConfig:
    """直接读取 config.toml，不调用任何外部 API"""
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with open(config_path, "rb") as f:
        data = tomllib.load(f)

    cfg = SimpleConfig()
    base_dir = str(config_path.parent)

    def _norm(p: str) -> str:
        return os.path.normpath(p)

    # local 配置
    local_data = data.get("local", {})
    db_file = local_data.get("db_file", "bridge.db")
    cfg.db_file = _norm(os.path.join(
        base_dir, db_file)) if not os.path.isabs(db_file) else _norm(db_file)

    # webui 配置
    webui_data = data.get("webui", {})
    cfg.webui.enabled = webui_data.get("enabled", True)
    cfg.webui.port = webui_data.get("port", 8579)
    cfg.webui.bind = webui_data.get("bind", "0.0.0.0")

    # tmdb 配置
    tmdb_data = data.get("tmdb", {})
    cfg.tmdb.access_token = tmdb_data.get("access_token", "")
    cfg.tmdb.language = tmdb_data.get("language", "zh-CN")
    cfg.tmdb.host = tmdb_data.get("host", "")
    cfg.tmdb.api_key = tmdb_data.get("api_key", "")
    proxy_data = tmdb_data.get("proxy", {})
    cfg.tmdb.proxy_enabled = proxy_data.get("enabled", False)
    cfg.tmdb.proxy_http = proxy_data.get("http", "")
    csv_file = tmdb_data.get("csv_watchlist_file", "./watchlist.csv")
    cfg.tmdb.csv_watchlist_file = _norm(os.path.join(
        base_dir, csv_file)) if not os.path.isabs(csv_file) else _norm(csv_file)

    db_file = tmdb_data.get("watchlist_db", "")
    cfg.tmdb.watchlist_db = _norm(os.path.join(
        base_dir, db_file)) if db_file and not os.path.isabs(db_file) else (db_file if db_file else "")

    cfg.tmdb.watchlist_cache_ttl = tmdb_data.get("watchlist_cache_ttl", 604800)

    cfg.tmdb.fuzzy_threshold = tmdb_data.get("fuzzy_threshold", 0.60)
    cfg.tmdb.anime_min_ep_ratio = tmdb_data.get("anime_min_ep_ratio", 0.3)

    return cfg


# ============================================================
# 常量与工具函数
# ============================================================

PAGE_SIZE = 50
STATIC_DIR = Path(__file__).resolve().parent / "webui_static"

# _is_lan_ip 从 webui_routes 导入
# _media_info, _category_filter_value,
# _normalize_text, _strip_noise_tokens, _split_aliases,
# _collect_b_media_snapshot, _score_watchlist_item, _refresh_watchlist_match_state,
# _compute_media_root, _extract_season_from_local_path
# → 已迁移到 watchlist_match.py，通过顶部 import 引入


def _load_tmdb_overrides(cfg: SimpleConfig) -> bool:
    """加载 .tmdb_webui_config.json 覆盖（启动时，config.toml 之后执行）。

    SimpleTmdbConfig 为扁平字段，与 JSON 中的 proxy_http / proxy_enabled
    天然对齐，直接 setattr 覆盖即可。返回是否实际加载了覆盖。

    注意：空字符串视为"未设置"，不覆盖 config.toml 的值（避免空值抹掉
    配置）；proxy_http 允许显式置空（用户想关闭代理时清空地址）。
    """
    overrides_file = PROJECT_ROOT / ".tmdb_webui_config.json"
    if not overrides_file.exists():
        return False
    try:
        with open(overrides_file, "r", encoding="utf-8") as f:
            overrides = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("[WebUI] 加载 .tmdb_webui_config.json 失败: %s", e)
        return False

    tmdb_overrides = overrides.get("tmdb", {})
    if not tmdb_overrides or not isinstance(tmdb_overrides, dict):
        return False

    tmdb_cfg = cfg.tmdb
    applied = 0
    for key, val in tmdb_overrides.items():
        if val is None or not hasattr(tmdb_cfg, key):
            continue
        if key in ("access_token", "api_key", "host"):
            # 这些字段空串视为未设置，不覆盖
            if val == "":
                continue
        if key == "proxy_enabled":
            setattr(tmdb_cfg, key, bool(val))
        else:
            setattr(tmdb_cfg, key, val)
        applied += 1

    if applied:
        logger.info(
            "[WebUI] 已加载 .tmdb_webui_config.json 覆盖 (%d 项)", applied)
    return applied > 0


# ============================================================
# TMDB 路由（使用 webui.py 的增强实现）
# ============================================================
# TMDB 路由（嵌入到 handler 中）
# ============================================================

# ============================================================
# Handler 类
# ============================================================

class _TestWebUIHandler(BaseHTTPRequestHandler):
    """独立 WebUI 测试面板的 HTTP 请求处理器"""

    # 由 WebUIServer.start() 动态设置
    webui: "TestWebUIServer"

    # ----------------------------------------------------------
    # 日志 & 安全
    # ----------------------------------------------------------
    def log_message(self, format, *args):
        pass  # 静默默认日志

    def _is_client_allowed(self) -> bool:
        client_ip = self.client_address[0]
        return _is_lan_ip(client_ip)

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

    def _send_html(self, body: str, status=200):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (ConnectionAbortedError, BrokenPipeError) as e:
            # 客户端提前断开连接（如刷新页面、快速切换 tab），静默忽略
            logging.debug("_send_html: 客户端已断开连接: %s", e)

    def _send_static_file(self, filename: str = "index.html", status=200):
        file_path = STATIC_DIR / filename
        try:
            body = file_path.read_bytes()
        except OSError as e:
            logging.error("_send_static_file: 无法读取静态文件 %s: %s", file_path, e)
            self.send_error(500, "static file not found")
            return
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
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError):
            pass

    # ----------------------------------------------------------
    # JSON API 处理器（SPA 架构）
    # ----------------------------------------------------------
    def _handle_dashboard(self):
        db: Database = self.webui._db
        a_count = b_count = c_count = 0
        b_valid = b_orphan = b_unknown = 0
        tmdb_configured = bool(self.webui._tmdb_client)
        try:
            with db.read_connection() as conn:
                a_count = conn.execute(
                    "SELECT COUNT(*) FROM a_strm_files").fetchone()[0]
                b_count = conn.execute(
                    "SELECT COUNT(*) FROM b_strm_files").fetchone()[0]
                c_count = conn.execute(
                    "SELECT COUNT(*) FROM c_ghost_files").fetchone()[0]
                b_valid = conn.execute(
                    "SELECT COUNT(*) FROM b_strm_files WHERE status='valid'").fetchone()[0]
                b_orphan = conn.execute(
                    "SELECT COUNT(*) FROM b_strm_files WHERE status='orphan'").fetchone()[0]
                b_unknown = conn.execute(
                    "SELECT COUNT(*) FROM b_strm_files WHERE status NOT IN ('valid','orphan') OR status IS NULL").fetchone()[0]
        except Exception as e:
            logger.error("获取仪表盘数据失败: %s", e)
        self._send_json({
            "a_count": a_count,
            "b_count": b_count,
            "c_count": c_count,
            "b_valid": b_valid,
            "b_orphan": b_orphan,
            "b_unknown": b_unknown,
            "tmdb_configured": tmdb_configured,
            "uptime": time.time() - self.webui._start_time,
        })

    def _handle_area(self, area, params):
        """区域列表：返回按媒体分组的统计摘要（支持分页和排序）"""
        if area not in ("a", "b", "c"):
            self._send_json({"error": "invalid area"}, 400)
            return

        kind_filter = params.get("kind", ["all"])[0]
        q = params.get("q", [""])[0].strip().lower()
        sort_key = params.get("sort", ["name"])[0]  # name, count, time, kind
        sort_order = params.get("order", ["asc"])[0]  # asc, desc
        page = _safe_int(params.get("page", ["1"])[0], 1)
        page_size = _safe_int(params.get("page_size", ["50"])[0], 50)

        records = self._get_records(area)
        kind_label_map = {
            "anime": "番剧",
            "movie": "电影",
            "other": "其他",
            "all": "全部"}

        # 按媒体名称聚合
        media_groups: dict[str, dict] = {}
        kind_counts: dict[str, int] = defaultdict(int)

        for rec in records:
            kind, name = _media_info(rec)
            if name not in media_groups:
                media_groups[name] = {
                    "name": name, "kind": kind, "count": 0,
                    "season": "", "latest_ts": 0,
                }
            g = media_groups[name]
            g["count"] += 1
            cat = _category_filter_value(kind)
            kind_counts[cat] += 1
            # 提取季信息
            season = _extract_season_from_local_path(rec.get("local_path", ""))
            if season and not g["season"]:
                g["season"] = season
            ts = rec.get("updated_at") or rec.get("moved_at") or 0
            if ts and ts > g["latest_ts"]:
                g["latest_ts"] = ts

        # 筛选
        items = list(media_groups.values())
        if kind_filter != "all":
            items = [
                it for it in items if _category_filter_value(
                    it["kind"]) == kind_filter]
        if q:
            items = [it for it in items if q in it["name"].lower()]

        # 排序
        reverse = sort_order == "desc"
        if sort_key == "count":
            items.sort(key=lambda x: x["count"], reverse=reverse)
        elif sort_key == "time":
            items.sort(key=lambda x: x["latest_ts"], reverse=reverse)
        elif sort_key == "kind":
            items.sort(key=lambda x: x["kind"], reverse=reverse)
        else:  # name
            items.sort(key=lambda x: x["name"], reverse=reverse)

        # 分页
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
        # 筛选匹配 media_name 的记录
        if media_name:
            records = [
                r for r in records if media_name in (
                    r.get("local_path") or r.get(
                        "webdav_path", ""))]

        # 计算 local_root / webdav_root（媒体文件夹前缀，不含尾部分隔符）
        # 用户说明：程序在将 strm 从 a 复制到 b 时做了刮削适配，
        # 本地路径会额外插入 Season 文件夹，且 WebDAV 端的媒体目录名可能
        # 与本地不同（如本地“番剧04” vs WebDAV“番剧E04”）。
        # 因此分别按“分类目录的下一级=媒体目录”的结构独立计算两侧根路径。
        local_root = ""
        webdav_root = ""
        if records:
            local_root = _compute_media_root(records[0].get("local_path", ""))
            webdav_root = _compute_media_root(
                records[0].get("webdav_path", ""))

        total = len(records)
        total_pages = max(1, ceil(total / PAGE_SIZE)) if total else 1
        page = max(1, min(page, total_pages))
        start = (page - 1) * PAGE_SIZE
        end = start + PAGE_SIZE
        page_records = records[start:end]

        # 排序
        reverse = sort_order == "desc"
        if page_records and sort_field in page_records[0]:
            page_records.sort(
                key=lambda r: r.get(
                    sort_field, ""), reverse=reverse)

        # 按季分组
        seasons_map: dict[str, list[dict]] = {}
        for rec in page_records:
            label = _extract_season_from_local_path(
                rec.get("local_path", "")) or "默认"
            seasons_map.setdefault(label, []).append(rec)

        seasons = [{"label": lbl, "records": recs}
                   for lbl, recs in seasons_map.items()]

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

    # ----------------------------------------------------------
    # 路由
    # ----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if not self._guard_request():
            return

        # TMDB 路由（复用 webui_routes.py 的增强版）
        if path.startswith("/api/tmdb/"):
            tmdb_client = self.webui._tmdb_client
            _webui_tmdb_routes(self, tmdb_client, path, params,
                               webui_server=self.webui)
            return

        # SPA 初始页面（从 webui_static/index.html 提供）
        if path == "/":
            self._send_static_file()
        elif path == "/favicon.ico":
            self._send_static_file("favicon.ico")
        elif path == "/logo.png":
            logos = sorted(STATIC_DIR.glob("logo.*.png"))
            self._send_static_file(random.choice(logos).name if logos else "logo.01.png")
        elif path == "/api/dashboard":
            self._handle_dashboard()
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
        elif path.startswith("/wallpaper/"):
            fname = path[len("/wallpaper/"):]
            if ".." in fname or "/" in fname or "\\" in fname:
                self._send_json({"error": "invalid path"}, 400)
            else:
                self._send_static_file(f"wallpaper/{fname}")
        elif path == "/api/local-wallpapers":
            self._handle_local_wallpapers()
        elif path == "/api/logs":
            self._handle_logs_api(params)
        elif path == "/api/config":
            self._handle_config_api()
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_tmdb_watchlist_match_refresh(self):
        if not self.webui._watchlist_db:
            self._send_json(
                {"success": False, "message": "TMDB 待看数据库未启用"}, 400)
            return
        if not self.webui._db:
            self._send_json({"success": False, "message": "主数据库未连接"}, 400)
            return
        with self.webui._match_refresh_lock:
            if self.webui._match_refresh_running:
                self._send_json({"success": True, "message": "已在刷新中"})
                return
            self.webui._match_refresh_running = True
            self.webui._match_refresh_result = None
        db = getattr(self.webui, '_db', None)
        if db:
            try:
                db.log_tmdb_operation("match_refresh_start", "info", "收录状态刷新启动")
            except Exception:
                pass
        threading.Thread(target=self._do_match_refresh, daemon=True).start()
        self._send_json({"success": True, "message": "后台收录状态刷新已启动"})

    def _do_match_refresh(self):
        db = getattr(self.webui, '_db', None)
        try:
            tmdb_cfg = getattr(self.webui._config, "tmdb", None)
            fuzzy = float(getattr(tmdb_cfg, "fuzzy_threshold", 0.60)) if tmdb_cfg else 0.60
            min_ep = float(getattr(tmdb_cfg, "anime_min_ep_ratio", 0.3)) if tmdb_cfg else 0.3
            counts = _refresh_watchlist_match_state(self.webui, fuzzy, min_ep)
            with self.webui._match_refresh_lock:
                self.webui._match_refresh_result = counts
            logger.info("[TMDB] 收录状态刷新完成: %s", counts)
            if db:
                try:
                    db.log_tmdb_operation("match_refresh", "success", f"收录状态刷新完成: {counts}")
                except Exception:
                    pass
        except Exception as e:
            logger.error("[TMDB] 收录状态刷新失败: %s", e, exc_info=True)
            with self.webui._match_refresh_lock:
                self.webui._match_refresh_result = {"error": str(e)}
            if db:
                try:
                    db.log_tmdb_operation("match_refresh", "error", f"收录状态刷新失败: {e}")
                except Exception:
                    pass
        finally:
            with self.webui._match_refresh_lock:
                self.webui._match_refresh_running = False

    def _handle_tmdb_watchlist_match_override(self, body: bytes):
        if not self.webui._watchlist_db:
            self._send_json(
                {"success": False, "message": "TMDB 待看数据库未启用"}, 400)
            return
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"success": False, "message": "无效的 JSON"}, 400)
            return
        media_type = str(data.get("media_type") or "").strip()
        if media_type not in {"movie", "tv"}:
            self._send_json(
                {"success": False, "message": "无效的 media_type"}, 400)
            return
        try:
            item_id = int(data.get("id") or 0)
        except (TypeError, ValueError):
            self._send_json({"success": False, "message": "无效的 id"}, 400)
            return
        status = str(data.get("status") or "").strip()
        if status not in {"matched", "fuzzy", "unmatched", "uncomputed"}:
            self._send_json({"success": False, "message": "无效的 status"}, 400)
            return
        reason = str(data.get("reason") or "manual_override")[:256]
        try:
            self.webui._watchlist_db.override_match_state(
                media_type, item_id, status, reason)
            self._send_json({"success": True, "message": "收录状态已手动覆盖"})
            db = getattr(self.webui, '_db', None)
            if db:
                try:
                    db.log_tmdb_operation(
                        "match_override", "info",
                        f"手动覆盖 {media_type}/{item_id} → {status}",
                        detail=json.dumps({"media_type": media_type, "id": item_id, "status": status, "reason": reason}),
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.error("[TMDB] 手动覆盖收录状态失败: %s", e, exc_info=True)
            self._send_json({"success": False, "message": f"覆盖失败: {e}"}, 500)

    # ----------------------------------------------------------
    # POST 路由（TMDB 配置保存、重启等）
    # ----------------------------------------------------------
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if not self._guard_request():
            return
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b"{}"
        if path == "/api/tmdb/configure":
            self._handle_tmdb_configure(body)
        elif path == "/api/tmdb/watchlist/match/refresh":
            self._handle_tmdb_watchlist_match_refresh()
        elif path == "/api/tmdb/watchlist/match/override":
            self._handle_tmdb_watchlist_match_override(body)
        elif path == "/api/tmdb/watchlist/sync":
            self._handle_tmdb_watchlist_bg_sync()
        elif path == "/api/restart-webui":
            self._handle_restart_webui()
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_tmdb_configure(self, body: bytes):
        """处理 TMDB 配置更新请求。"""
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"success": False, "error": "无效的 JSON"}, 400)
            return
        tmdb_cfg = self.webui._config.tmdb
        if not tmdb_cfg:
            self._send_json({"success": False, "error": "TMDB 配置不可用"}, 500)
            return
        changed = False
        try:
            for key in ("access_token", "api_key", "language", "host",
                        "watchlist_db", "csv_watchlist_file"):
                if key in data and data[key] is not None:
                    val = data[key]
                    # watchlist_db 相对路径转绝对（与 load_config 一致）
                    if key == "watchlist_db" and val:
                        val = str(val).strip()
                        if val and not os.path.isabs(val):
                            val = os.path.normpath(str(PROJECT_ROOT / val))
                    setattr(tmdb_cfg, key, val)
                    changed = True
            if "proxy_enabled" in data and data["proxy_enabled"] is not None:
                tmdb_cfg.proxy_enabled = bool(data["proxy_enabled"])
                changed = True
            if "proxy_http" in data and data["proxy_http"] is not None:
                tmdb_cfg.proxy_http = data["proxy_http"]
                changed = True
            if changed:
                # 重新初始化 TMDB 客户端
                from tmdb_client import create_tmdb_client
                proxy = tmdb_cfg.proxy_http if tmdb_cfg.proxy_enabled and not tmdb_cfg.host else None
                try:
                    self.webui._tmdb_client = create_tmdb_client(
                        access_token=tmdb_cfg.access_token or "",
                        language=tmdb_cfg.language,
                        proxy=proxy,
                        host=tmdb_cfg.host or "",
                        api_key=tmdb_cfg.api_key or "",
                    )
                except Exception as e:
                    logger.warning("[TMDB] 重新初始化失败: %s", e)
                # 重建 watchlist DB
                if tmdb_cfg.access_token or tmdb_cfg.api_key:
                    db_path = getattr(tmdb_cfg, "watchlist_db", "") or ""
                    if not db_path:
                        db_path = str(PROJECT_ROOT / "tmdb_watchlist.db")
                    db_path = os.path.abspath(db_path) if db_path else str(
                        PROJECT_ROOT / "tmdb_watchlist.db")
                    ttl = float(
                        getattr(
                            tmdb_cfg,
                            "watchlist_cache_ttl",
                            604800))
                    try:
                        self.webui._watchlist_db = TmdbWatchlistDb(
                            db_path, ttl)
                    except Exception as e:
                        logger.warning("[WebUI] 待看列表数据库重建失败: %s", e)
                else:
                    self.webui._watchlist_db = None
                # 保存覆盖文件
                self._save_tmdb_overrides(data)
                configured = bool(self.webui._tmdb_client)
                db = getattr(self.webui, '_db', None)
                if db:
                    try:
                        db.log_tmdb_operation("config_update", "success", "TMDB 配置已保存", detail=json.dumps({"tmdb_configured": configured}))
                    except Exception:
                        pass
                self._send_json({
                    "success": True,
                    "message": "TMDB 配置已更新",
                    "tmdb_configured": configured,
                })
            else:
                self._send_json({"success": True, "message": "无变更"})
        except Exception as e:
            logger.error("[WebUI] 保存 TMDB 配置异常: %s", e, exc_info=True)
            db = getattr(self.webui, '_db', None)
            if db:
                try:
                    db.log_tmdb_operation("config_update", "error", f"TMDB 配置保存失败: {e}")
                except Exception:
                    pass
            self._send_json({"success": False, "error": f"保存失败: {e}"}, 500)

    def _save_tmdb_overrides(self, changes: dict):
        """保存 TMDB 配置覆盖到 .tmdb_webui_config.json。"""
        overrides_file = PROJECT_ROOT / ".tmdb_webui_config.json"
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
            logger.info("[WebUI] TMDB 配置覆盖已保存: %s", overrides_file)
        except Exception as e:
            logger.warning("[WebUI] 保存配置覆盖失败: %s", e)

    def _handle_tmdb_watchlist_bg_sync(self):
        """触发后台 TMDB 待看列表同步（不阻塞 HTTP 线程）。"""
        if not self.webui._tmdb_client or not self.webui._watchlist_db:
            self._send_json({"success": False, "message": "TMDB 未配置"}, 400)
            return
        with self.webui._sync_lock:
            if self.webui._sync_running:
                self._send_json({"success": True, "message": "已在同步中"})
                return
            self.webui._sync_running = True
        db = getattr(self.webui, '_db', None)
        if db:
            try:
                db.log_tmdb_operation("sync_start", "info", "TMDB 同步启动")
            except Exception:
                pass
        threading.Thread(target=self._do_bg_sync, daemon=True).start()
        self._send_json({"success": True, "message": "后台同步已启动"})

    def _do_bg_sync(self):
        try:
            self.webui._watchlist_db.sync(self.webui._tmdb_client)
            logger.info("[TMDB] 后台同步完成")
            db = getattr(self.webui, '_db', None)
            if db:
                try:
                    db.log_tmdb_operation("sync", "success", "TMDB 同步完成")
                except Exception:
                    pass
        except Exception as e:
            logger.warning("[TMDB] 后台同步失败: %s", e)
            db = getattr(self.webui, '_db', None)
            if db:
                try:
                    db.log_tmdb_operation("sync", "error", f"TMDB 同步失败: {e}")
                except Exception:
                    pass
        finally:
            with self.webui._sync_lock:
                self.webui._sync_running = False

    def _handle_restart_webui(self):
        """重启 WebUI HTTP 服务。"""
        logger.info("[WebUI] 正在重启 HTTP 服务...")
        db = getattr(self.webui, '_db', None)
        if db:
            try:
                db.log_tmdb_operation("restart", "info", "WebUI 重启")
            except Exception:
                pass
        self._send_json({"success": True, "message": "正在重启 WebUI..."})

        def _do_restart():
            time.sleep(0.5)
            try:
                self.webui.stop()
                self.webui.start()
                logger.info("[WebUI] HTTP 服务重启完成")
            except Exception as e:
                logger.error("[WebUI] 重启失败: %s", e)
        threading.Thread(target=_do_restart, daemon=True).start()

    # ----------------------------------------------------------
    # 已移除服务端渲染死代码 (_render_content ... _render_config)
    # 前端使用 webui_static/index.html SPA 直接渲染
    # ----------------------------------------------------------
    # ----------------------------------------------------------
    # 数据获取
    # ----------------------------------------------------------
    def _get_records(self, area: str) -> list[dict]:
        db: Database = self.webui._db
        try:
            if area == "a":
                with db.read_connection() as conn:
                    rows = conn.execute(
                        "SELECT local_path, webdav_path, parent_webdav_path, updated_at FROM a_strm_files"
                    ).fetchall()
                return [{"local_path": r[0], "webdav_path": r[1],
                         "parent_webdav_path": r[2], "updated_at": r[3]} for r in rows]
            elif area == "b":
                with db.read_connection() as conn:
                    rows = conn.execute(
                        "SELECT local_path, webdav_path, parent_webdav_path, source_a_path, fingerprint, status, updated_at FROM b_strm_files"
                    ).fetchall()
                return [{"local_path": r[0], "webdav_path": r[1],
                         "parent_webdav_path": r[2], "source_a_path": r[3],
                         "fingerprint": r[4], "status": r[5], "updated_at": r[6]} for r in rows]
            elif area == "c":
                with db.read_connection() as conn:
                    rows = conn.execute(
                        "SELECT local_path, webdav_path, original_b_path, ghost_root, moved_at FROM c_ghost_files"
                    ).fetchall()
                return [{"local_path": r[0], "webdav_path": r[1],
                         "original_b_path": r[2], "ghost_root": r[3],
                         "moved_at": r[4]} for r in rows]
        except Exception as e:
            logger.error("获取 %s 区记录失败: %s", area, e)
        return []

    # ----------------------------------------------------------
    # API 端点
    # ----------------------------------------------------------
    def _handle_local_wallpapers(self):
        wallpaper_dir = STATIC_DIR / "wallpaper"
        wallpapers = []
        if wallpaper_dir.is_dir():
            for f in sorted(wallpaper_dir.iterdir()):
                if f.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp'):
                    wallpapers.append({"url": f"/wallpaper/{f.name}"})
        self._send_json(wallpapers)

    def _handle_records(self, area: str):
        records = self._get_records(area)
        self._send_json({"count": len(records), "items": records})

    def _handle_logs_api(self, params):
        log_file = self.webui._log_file
        lines_req = _safe_int(params.get("lines", ["200"])[0], 200)
        if not log_file or not os.path.exists(log_file):
            self._send_json({"lines": [], "count": 0})
            return
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
                tail = all_lines[-lines_req:]
            self._send_json({"lines": [l.rstrip()
                            for l in tail], "count": len(tail)})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_config_api(self):
        cfg = self.webui._config
        tmdb_client = self.webui._tmdb_client
        token = cfg.tmdb.access_token
        token_preview = (token[:16] +
                         '...') if len(token) > 16 else (token or '')
        import os as _os
        self._send_json({
            "db_file": cfg.db_file,
            "db_exists": _os.path.isfile(cfg.db_file),
            "webui_port": cfg.webui.port,
            "webui_bind": cfg.webui.bind,
            "tmdb_configured": bool(cfg.tmdb.access_token),
            "tmdb_token_preview": token_preview,
            "tmdb_token": token,
            "tmdb_language": cfg.tmdb.language,
            "tmdb_host": cfg.tmdb.host or "",
            "tmdb_api_key": cfg.tmdb.api_key or "",
            "tmdb_api_key_configured": bool(cfg.tmdb.api_key),
            "tmdb_proxy_http": cfg.tmdb.proxy_http if cfg.tmdb.proxy_enabled else "",
            "tmdb_proxy_enabled": cfg.tmdb.proxy_enabled,
            "tmdb_proxy": cfg.tmdb.proxy_http if cfg.tmdb.proxy_enabled else None,
            "tmdb_account_id": tmdb_client.account_id if tmdb_client else None,
            "tmdb_watchlist_db": cfg.tmdb.watchlist_db or "",
        })


# ============================================================
# 服务器
# ============================================================

class TestWebUIServer:
    """独立 WebUI 测试服务器"""

    def __init__(self, config: SimpleConfig, db: Database,
                 tmdb_client: TmdbClient | None = None):
        self._config = config
        self._db = db
        self._tmdb_client = tmdb_client
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._start_time = time.time()
        self._log_file: str | None = None

        # TMDB 待看列表 SQLite 数据库
        self._watchlist_db: TmdbWatchlistDb | None = None
        if config.tmdb.access_token:
            db_path = (
                config.tmdb.watchlist_db or
                str(PROJECT_ROOT / "tmdb_watchlist.db")
            )
            ttl = config.tmdb.watchlist_cache_ttl
            self._watchlist_db = TmdbWatchlistDb(db_path, ttl)

        # 后台同步控制
        self._sync_lock = threading.Lock()
        self._sync_running = False

        # 收录状态刷新控制（与 webui.py 的 WebUIServer 保持一致）
        self._match_refresh_lock = threading.Lock()
        self._match_refresh_running = False
        self._match_refresh_result: dict | None = None

        # 尝试查找日志文件
        log_candidates = [
            PROJECT_ROOT / "strm_bridge.log",
            PROJECT_ROOT / "logs" / "strm_bridge.log",
            SRC_DIR / "strm_bridge.log",
        ]
        for p in log_candidates:
            if p.exists():
                self._log_file = str(p)
                break

    def get_watchlist_cached(self) -> list[dict]:
        """获取待看列表。缓存过期时直接返回旧数据，不自动同步。"""
        if not self._tmdb_client or not self._watchlist_db:
            return []
        return self._watchlist_db.get_all()

    def refresh_watchlist_match_state(self) -> dict[str, int]:
        if not self._watchlist_db or not self._db:
            return {"matched": 0, "fuzzy": 0, "unmatched": 0, "total": 0}
        tmdb_cfg = getattr(self._config, "tmdb", None)
        fuzzy = float(getattr(tmdb_cfg, "fuzzy_threshold", 0.60)) if tmdb_cfg else 0.60
        min_ep = float(getattr(tmdb_cfg, "anime_min_ep_ratio", 0.3)) if tmdb_cfg else 0.3
        return _refresh_watchlist_match_state(self, fuzzy, min_ep)

    @staticmethod
    def _try_bind_port(host: str, port: int) -> bool:
        """尝试绑定端口，检测端口是否可用

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

    def start(self):
        """启动 WebUI 服务器

        失败时抛出 RuntimeError，由调用方处理（重启线程或 main 函数）。
        """
        port = self._config.webui.port
        bind = self._config.webui.bind

        # 动态绑定 handler 类
        handler_cls = type("_BoundHandler", (_TestWebUIHandler,), {})
        handler_cls.webui = self
        handler_cls.allow_reuse_address = True

        # 先测试端口是否可用
        if not self._try_bind_port(bind, port):
            raise RuntimeError(
                f"端口 {port} 已被占用，请关闭占用程序或修改 config.toml 中的端口配置")

        try:
            self._server = HTTPServer((bind, port), handler_cls)
        except OSError as e:
            err = getattr(
                e,
                'winerror',
                None) or getattr(
                e,
                'errno',
                None) or 0
            if err in (10048, 98):
                raise RuntimeError(
                    f"端口 {port} 已被占用，请关闭占用程序或修改端口配置") from e
            raise RuntimeError(f"启动 HTTP 服务器失败: {e}") from e

        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True)
        self._thread.start()

        tmdb_info = "已配置" if self._tmdb_client else "未配置"
        logger.info(
            "[WebUI] 测试面板已启动: http://%s:%d (TMDB: %s)",
            bind,
            port,
            tmdb_info)

    def stop(self):
        """停止 WebUI 服务器"""
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("[WebUI] 测试面板已停止")


# ============================================================
# 主入口
# ============================================================

def main():
    """主入口函数"""
    config_path = PROJECT_ROOT / "config.toml"

    if not config_path.exists():
        logger.error("未找到配置文件: %s", config_path)
        sys.exit(1)

    # 加载配置
    logger.info("加载配置: %s", config_path)
    cfg = load_config(config_path)

    # 加载 .tmdb_webui_config.json 覆盖（WebUI 编辑过的配置）
    # 启动顺序：config.toml → 覆盖文件，同名键覆盖
    _load_tmdb_overrides(cfg)

    # 初始化数据库
    if not os.path.exists(cfg.db_file):
        logger.error("数据库文件不存在: %s", cfg.db_file)
        sys.exit(1)

    logger.info("打开数据库: %s", cfg.db_file)
    db = Database(cfg.db_file)

    # 初始化 TMDB 客户端
    tmdb_client = None
    if cfg.tmdb.access_token:
        # 配置了自定义 host 时不再需要本地代理
        proxy = None if cfg.tmdb.host else (
            cfg.tmdb.proxy_http if cfg.tmdb.proxy_enabled else None)
        try:
            tmdb_client = create_tmdb_client(
                access_token=cfg.tmdb.access_token,
                language=cfg.tmdb.language,
                proxy=proxy,
                host=cfg.tmdb.host,
                api_key=cfg.tmdb.api_key,
                auto_validate=False,
            )
            logger.info(
                "TMDB 客户端已初始化 (account_id: %s)",
                tmdb_client.account_id)
        except Exception as e:
            logger.warning("TMDB 客户端初始化失败: %s", e)
    else:
        logger.info("未配置 TMDB access_token，跳过初始化")

    # 启动 WebUI
    server = TestWebUIServer(cfg, db, tmdb_client)
    try:
        server.start()
    except RuntimeError as e:
        logger.error("[WebUI] %s", e)
        sys.exit(1)

    port = cfg.webui.port
    logger.info("=" * 50)
    logger.info("  测试面板已就绪: http://127.0.0.1:%d", port)
    logger.info("  按 Ctrl+C 或输入 q 退出")
    logger.info("=" * 50)

    try:
        if '--daemon' not in sys.argv:
            while True:
                cmd = input().strip().lower()
                if cmd in ("q", "quit", "exit"):
                    break
        else:
            # 守护模式：挂起主线程等待服务器终止
            server._thread.join()
    except (KeyboardInterrupt, EOFError):
        pass

    server.stop()
    logger.info("已退出")


if __name__ == "__main__":
    main()
