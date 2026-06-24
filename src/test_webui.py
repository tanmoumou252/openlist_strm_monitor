"""
独立 WebUI 测试面板 - SPA 架构
可单独运行，直接读取 config.toml 和现有数据库，无需启动主程序。
集成了 TMDB 测试功能（待看列表、搜索、别名、详情）。
导航/筛选/分页通过 JS 拦截链接，fetch 获取 HTML 片段，只替换 <main> 内容。
"""
# autopep8: off
# isort: off
from __future__ import annotations

import html
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import tomllib
import urllib.request
from collections import OrderedDict, defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from math import ceil
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

# ============================================================
# 路径设置（必须在项目模块导入之前）
# ============================================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC_DIR))

from tmdb_watchlist_db import TmdbWatchlistDb

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
from webui import _compute_media_root, _build_img_opener  # noqa: E402

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
    password: str = ""


class SimpleTmdbConfig:
    """简化的 TMDB 配置"""
    access_token: str = ""
    language: str = "zh-CN"
    host: str = ""
    api_key: str = ""
    proxy_enabled: bool = False
    proxy_http: str = ""
    csv_watchlist_file: str = "./watchlist.csv"
    watchlist_cache_file: str = ""
    watchlist_cache_ttl: int = 604800  # 缓存过期时间（秒），默认 7 天
    watchlist_db: str = ""


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
    cfg.webui.password = webui_data.get("password", "")

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

    cache_file = tmdb_data.get(
        "watchlist_cache_file",
        "")
    cfg.tmdb.watchlist_cache_file = _norm(os.path.join(
        base_dir, cache_file)) if cache_file and not os.path.isabs(cache_file) else (cache_file if cache_file else "")

    db_file = tmdb_data.get("watchlist_db", "")
    cfg.tmdb.watchlist_db = _norm(os.path.join(
        base_dir, db_file)) if db_file and not os.path.isabs(db_file) else (db_file if db_file else "")

    cfg.tmdb.watchlist_cache_ttl = tmdb_data.get("watchlist_cache_ttl", 86400)

    return cfg


# ============================================================
# 常量与工具函数
# ============================================================

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
    """从记录中推断 media_kind 和 media_name"""
    parts = _path_parts(record.get("local_path") or record.get("webdav_path"))
    media_kind = "未分类"
    media_name = "未知"
    for i, p in enumerate(parts):
        if _is_category_dir(p):
            media_kind = p
            if i + 1 < len(parts):
                media_name = parts[i + 1]
            break
    if media_kind == "未分类" and parts:
        media_kind = "其他"
        media_name = parts[0]
    return media_kind, media_name


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


def _is_category_active(kind: str, filter_value: str) -> bool:
    if filter_value == "all":
        return True
    return _category_filter_value(kind) == filter_value


def _extract_season_from_local_path(lp: str) -> str:
    """从 local_path 提取季标签"""
    parts = _path_parts(lp)
    for p in reversed(parts):
        s = _extract_season_int(p)
        if s is not None:
            return f"第{s}季"
    return ""


def _safe_int(val: str | None, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


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

        # TMDB 路由（复用 webui.py 的增强版）
        if path.startswith("/api/tmdb/"):
            from webui import _tmdb_routes as _webui_tmdb_routes
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
            self._send_static_file("logo.png")
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
        elif path == "/api/bing-wallpapers":
            self._handle_bing_wallpapers()
        elif path == "/api/logs":
            self._handle_logs_api(params)
        elif path == "/api/config":
            self._handle_config_api()
        else:
            self._send_json({"error": "not found"}, 404)

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
                    db_path = os.path.abspath(db_path) if db_path else str(PROJECT_ROOT / "tmdb_watchlist.db")
                    ttl = float(getattr(tmdb_cfg, "watchlist_cache_ttl", 604800))
                    try:
                        self.webui._watchlist_db = TmdbWatchlistDb(db_path, ttl)
                    except Exception as e:
                        logger.warning("[WebUI] 待看列表数据库重建失败: %s", e)
                else:
                    self.webui._watchlist_db = None
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
            logger.error("[WebUI] 保存 TMDB 配置异常: %s", e, exc_info=True)
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

    def _handle_restart_webui(self):
        """重启 WebUI HTTP 服务。"""
        logger.info("[WebUI] 正在重启 HTTP 服务...")
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
    # 路由分发
    # ----------------------------------------------------------
    def _render_content(self, params):
        tab = params.get("tab", ["dashboard"])[0]
        if tab.startswith("area_"):
            area = tab.split("_", 1)[1]
            return self._render_area_page(
                area,
                media=params.get("media", [None])[0],
                kind=params.get("kind", [None])[0],
                page=params.get("page", ["1"])[0],
                q=params.get("q", [None])[0],
                sort=params.get("sort", [None])[0],
                order=params.get("order", [None])[0],
            )
        if tab == "tmdb":
            return self._render_tmdb(params)
        if tab == "logs":
            return self._render_logs()
        if tab == "config":
            return self._render_config()
        return self._render_dashboard()

    # ----------------------------------------------------------
    # 完整页面（壳 + 内容）
    # ----------------------------------------------------------
    def _render_page(self, params):
        nav = self._build_nav(params)
        content = self._render_content(params)
        uptime = int(time.time() - self.webui._start_time)
        return self._page_html(nav, content, uptime)

    def _build_nav(self, params):
        current_tab = params.get("tab", ["dashboard"])[0]
        links = [
            ("dashboard", "仪表盘"),
            ("area_b", "B 区"),
            ("area_a", "A 区"),
            ("area_c", "C 区"),
            ("tmdb", "TMDB"),
            ("logs", "日志"),
            ("config", "配置"),
        ]
        parts = []
        for tab_id, label in links:
            cls = ' class="active"' if tab_id == current_tab else ""
            parts.append(
                f'<a href="/?tab={tab_id}" data-tab="{tab_id}"{cls}>{label}</a>')
        return "\n".join(parts)

    # ----------------------------------------------------------
    # HTML 模板
    # ----------------------------------------------------------
    def _page_html(self, nav: str, content: str, uptime: int) -> str:
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<title>STRM Bridge - 测试面板</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  background:#0a0e1a;color:#e0e0e0;min-height:100vh;overflow-x:hidden}}

/* === 壁纸层 === */
#wallpaper-a,#wallpaper-b{{
  position:fixed;top:0;left:0;width:100%;height:100%;
  background-size:cover;background-position:center;
  z-index:-2;transition:opacity 1.5s ease-in-out;
}}
#overlay{{position:fixed;top:0;left:0;width:100%;height:100%;
  background:linear-gradient(135deg,rgba(10,14,26,.92) 0%,rgba(20,30,50,.88) 100%);
  z-index:-1}}
#wallpaper-info{{position:fixed;bottom:8px;right:16px;font-size:11px;
  color:rgba(255,255,255,.45);z-index:10;pointer-events:none}}

/* === 头部 === */
header{{position:sticky;top:0;z-index:100;
  backdrop-filter:blur(20px);background:rgba(10,14,26,.78);
  border-bottom:1px solid rgba(255,255,255,.06);
  padding:12px 24px;display:flex;align-items:center;gap:18px}}
header .brand{{font-size:16px;font-weight:700;color:#8ecdf7;
  letter-spacing:.5px;white-space:nowrap}}
header .sub{{font-size:11px;color:rgba(255,255,255,.35);margin-left:4px}}

nav{{display:flex;gap:4px;flex-wrap:wrap}}
nav a{{color:#8899aa;text-decoration:none;padding:6px 14px;border-radius:8px;
  font-size:13px;transition:all .2s}}
nav a:hover{{background:rgba(255,255,255,.06);color:#c0d8f0}}
nav a.active{{background:rgba(100,180,255,.12);color:#8ecdf7;font-weight:600}}

/* === 容器 === */
.container{{max-width:1200px;margin:0 auto;padding:24px 20px}}

/* === 卡片 === */
.stat-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));
  gap:16px;margin-bottom:24px}}
.stat-card{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);
  border-radius:12px;padding:18px;transition:all .2s}}
.stat-card:hover{{background:rgba(255,255,255,.07);transform:translateY(-2px)}}
.stat-card .label{{font-size:12px;color:#8899aa;margin-bottom:6px}}
.stat-card .value{{font-size:28px;font-weight:700;color:#8ecdf7}}
.stat-card .sub{{font-size:11px;color:#667788;margin-top:4px}}

/* === 媒体卡片 === */
.media-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
  gap:14px;margin-bottom:20px}}
.media-grid a{{text-decoration:none!important;color:inherit}}
.media-card{{text-decoration:none}}
.media-card{{display:block;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);
  border-radius:10px;padding:14px;cursor:pointer;transition:all .2s;position:relative}}
.media-card:hover{{background:rgba(255,255,255,.08);transform:translateY(-2px)}}
.media-card .title{{font-size:14px;font-weight:600;color:#e0e8f0;
  margin-bottom:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.media-card .meta{{font-size:12px;color:#8899aa}}
.media-card .badge{{position:absolute;top:8px;right:8px;font-size:10px;font-weight:600;
  padding:2px 6px;border-radius:4px;color:#fff;line-height:1.3}}
.media-card .badge.in{{background:#4caf50}}
.media-card .badge.out{{background:#42a5f5}}
.media-card .badge.que{{background:#8d6e63}}

/* === 表格 === */
.table-wrap{{overflow-x:auto;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{background:rgba(255,255,255,.06);color:#8ecdf7;font-weight:600;
  padding:10px 12px;text-align:left;position:sticky;top:0}}
td{{padding:8px 12px;border-bottom:1px solid rgba(255,255,255,.05);
  color:#c0c8d0;word-break:break-all}}
tr:hover td{{background:rgba(255,255,255,.03)}}

/* === 徽章 === */
.badge{{display:inline-block;padding:2px 8px;border-radius:6px;
  font-size:11px;font-weight:600}}
.badge-valid{{background:rgba(76,175,80,.15);color:#66bb6a}}
.badge-orphan{{background:rgba(255,193,7,.15);color:#ffc107}}
.badge-ghost{{background:rgba(156,39,176,.15);color:#ce93d8}}

/* === 分页器 === */
.pager{{display:flex;gap:8px;align-items:center;margin:16px 0}}
.pager a,.pager span{{padding:6px 12px;border-radius:8px;font-size:13px;
  text-decoration:none;transition:all .2s}}
.pager a{{background:rgba(255,255,255,.06);color:#8899aa}}
.pager a:hover{{background:rgba(255,255,255,.1);color:#c0d8f0}}
.pager .current{{background:rgba(100,180,255,.15);color:#8ecdf7;font-weight:600}}

/* === 工具栏 === */
.toolbar{{display:flex;gap:12px;align-items:center;margin-bottom:16px;flex-wrap:wrap}}
.toolbar input[type=text]{{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);
  border-radius:8px;padding:8px 14px;color:#e0e0e0;font-size:13px;outline:none;
  min-width:200px;transition:border-color .2s}}
.toolbar input:focus{{border-color:rgba(100,180,255,.4)}}
.toolbar select{{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);
  border-radius:8px;padding:8px 10px;color:#e0e0e0;font-size:13px;outline:none}}
.toolbar a,.toolbar button{{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.08);
  border-radius:8px;padding:8px 14px;color:#8899aa;font-size:13px;cursor:pointer;
  text-decoration:none;transition:all .2s}}
.toolbar a:hover,.toolbar button:hover{{background:rgba(255,255,255,.1);color:#c0d8f0}}

/* === 分类标签 === */
.category-tabs{{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}}
.category-tab{{padding:6px 14px;border-radius:8px;font-size:13px;cursor:pointer;
  background:rgba(255,255,255,.04);color:#8899aa;transition:all .2s;
  border:1px solid rgba(255,255,255,.06)}}
.category-tab:hover{{background:rgba(255,255,255,.08);color:#c0d8f0}}
.category-tab.active{{background:rgba(100,180,255,.12);color:#8ecdf7;font-weight:600;
  border-color:rgba(100,180,255,.3)}}

/* === 日志面板 === */
.log-panel{{background:rgba(0,0,0,.3);border:1px solid rgba(255,255,255,.08);
  border-radius:10px;padding:16px;max-height:500px;overflow-y:auto;
  font-family:"Cascadia Code","Fira Code",monospace;font-size:12px;line-height:1.6}}
.log-line{{color:#a0b0c0}}.log-line .ts{{color:#607080}}
.log-line .info{{color:#66bb6a}}.log-line .warn{{color:#ffa726}}
.log-line .error{{color:#ef5350}}.log-line .debug{{color:#78909c}}

/* === 配置面板 === */
.config-section{{margin-bottom:20px}}
.config-section h3{{font-size:15px;color:#8ecdf7;margin-bottom:10px;
  padding-bottom:6px;border-bottom:1px solid rgba(255,255,255,.06)}}
.config-row{{display:flex;justify-content:space-between;padding:8px 0;
  border-bottom:1px solid rgba(255,255,255,.03);font-size:13px}}
.config-row .key{{color:#8899aa}}.config-row .val{{color:#c0d8f0;max-width:60%;
  word-break:break-all;text-align:right}}

/* === 浮动标签输入框（测试面板暗色主题，覆盖式标签） === */
.floating-field{{padding:4px 0 6px;border-bottom:1px solid rgba(255,255,255,.04)}}
.floating-field:last-child{{border-bottom:none}}
.floating-field .field-control{{position:relative;min-height:48px}}
.floating-label{{position:absolute;left:12px;top:50%;transform:translateY(-50%);font-size:13px;color:#667788;pointer-events:none;transition:all .18s ease;opacity:0;padding:0 4px;z-index:1;line-height:1;background:transparent;transform-origin:left center;white-space:nowrap}}
.floating-label.is-shown{{opacity:1}}
.floating-label.is-floating{{top:0;transform:translateY(-50%) scale(.82);color:#8ecdf7}}
.floating-label.is-filled{{top:0;transform:translateY(-50%) scale(.82);color:#667788}}
.floating-field input,.floating-field select{{width:100%;border:none;outline:none;background:rgba(255,255,255,.04);color:#e0e0e0;font:inherit;padding:16px 12px 10px;border-radius:8px;border:1px solid rgba(255,255,255,.1);transition:border-color .18s ease,box-shadow .18s ease,background .18s ease}}
.floating-field input::placeholder{{color:#667788;opacity:.75;transition:opacity .18s ease}}
.floating-field input:focus::placeholder,.floating-field input.has-value::placeholder{{opacity:0}}
.floating-field input:focus,.floating-field select:focus{{border-color:rgba(100,180,255,.5);box-shadow:0 0 0 3px rgba(100,180,255,.12);background:rgba(255,255,255,.07)}}
.floating-field .field-select-wrap{{display:flex;align-items:stretch;gap:0}}
.floating-field .field-select-wrap select{{flex:0 0 160px;min-width:0;padding:16px 8px 10px;border-radius:8px 0 0 8px}}
.floating-field .field-select-wrap input[type="text"]{{flex:1;min-width:0;border-radius:0 8px 8px 0;border-left:none}}
.config-form-actions{{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}}
.config-form-actions .toolbar-btn{{min-width:128px}}

/* === TMDB 面板 === */
.tmdb-search{{margin-bottom:20px}}
.tmdb-search input{{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);
  border-radius:8px;padding:8px 14px;color:#e0e0e0;font-size:13px;outline:none;
  min-width:250px}}
.tmdb-search button{{background:rgba(100,180,255,.15);border:1px solid rgba(100,180,255,.3);
  border-radius:8px;padding:8px 18px;color:#8ecdf7;font-size:13px;cursor:pointer;
  margin-left:8px;transition:all .2s}}
.tmdb-search button:hover{{background:rgba(100,180,255,.25)}}
.tmdb-result{{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);
  border-radius:10px;padding:14px;margin-bottom:10px}}
.tmdb-result .title{{font-size:14px;font-weight:600;color:#e0e8f0}}
.tmdb-result .meta{{font-size:12px;color:#8899aa;margin-top:4px}}
.tmdb-result .overview{{font-size:12px;color:#a0b0c0;margin-top:6px;
  max-height:60px;overflow:hidden}}

/* === 多季番剧竖杠 === */
.tmdb-season-bars{{position:absolute;top:-1px;left:14px;display:flex;gap:3px;z-index:3;pointer-events:none}}
.tmdb-season-bar{{display:block;width:5px;height:16px;border-radius:0 0 2px 2px;border:1px solid rgba(255,255,255,.18);border-top:none}}
.tmdb-season-bar.bar-green{{background:#a5d6a7}}
.tmdb-season-bar.bar-blue{{background:#90caf9}}
.tmdb-season-bar.bar-purple{{background:#ce93d8}}

/* === 季分组标题 === */
.season-header{{background:rgba(255,255,255,.04);padding:10px 14px;
  border-radius:8px;margin:12px 0 8px;font-size:14px;font-weight:600;
  color:#8ecdf7;border-left:3px solid rgba(100,180,255,.4)}}

/* === 响应式 === */
@media(max-width:768px){{
  header{{flex-wrap:wrap;gap:8px;padding:10px 14px}}
  .container{{padding:16px 12px}}
  .stat-grid{{grid-template-columns:repeat(auto-fill,minmax(140px,1fr))}}
  .media-grid{{grid-template-columns:repeat(auto-fill,minmax(160px,1fr))}}
}}
</style>
</head>
<body>
<div id="wallpaper-a"></div>
<div id="wallpaper-b"></div>
<div id="overlay"></div>
<div id="wallpaper-info"></div>

<header>
  <div style="display:flex;align-items:center;gap:12px">
    <img src="/logo.png" alt="" style="height:100px;border-radius:12px;object-fit:contain;vertical-align:middle">
    <div>
      <span class="brand">STRM Bridge</span>
      <span class="sub">测试面板</span>
    </div>
  </div>
  <nav class="nav">{nav}</nav>
</header>

<main class="container" id="app-main">{content}</main>

<script>
// ===== Uptime 计时器 =====
(function(){{
  let s={uptime};
  const el=document.getElementById('uptime-val');
  if(!el)return;
  setInterval(()=>{{
    s++;
    const h=Math.floor(s/3600),m=Math.floor(s%3600/60),ss=s%60;
    el.textContent=(h?h+'h ':'')+m+'m '+ss+'s';
  }},1000);
}})();

// ===== 壁纸轮播 =====
(function(){{
  const a=document.getElementById('wallpaper-a');
  const b=document.getElementById('wallpaper-b');
  const info=document.getElementById('wallpaper-info');
  if(!a||!b)return;
  let urls=[],idx=0,showA=true;

  function pick(){{
    if(!urls.length)return;
    const url=urls[idx%urls.length];
    const el=showA?a:b;
    const other=showA?b:a;
    el.style.backgroundImage='url('+url+')';
    el.style.opacity='1';
    other.style.opacity='0';
    showA=!showA;
    idx++;
  }}

  async function load(){{
    try{{
      const r=await fetch('/api/bing-wallpapers');
      const d=await r.json();
      if(d&&d.length){{
        urls=d.map(p=>p.url);
        pick();
        setInterval(pick,45000);
      }}
    }}catch(e){{
      console.warn('壁纸加载失败',e);
    }}
  }}
  load();
}})();

// ===== SPA 导航 =====
(function(){{
  const main=document.getElementById('app-main');

  async function loadTab(url, push){{
    try{{
      const r=await fetch(url);
      const html=await r.text();
      const parser=new DOMParser();
      const doc=parser.parseFromString(html,'text/html');
      const fragment=doc.querySelector('main');
      if(fragment){{main.innerHTML=fragment.innerHTML;}}
      else{{main.innerHTML=html;}}
      // 执行加载内容中的内联脚本（如排序表单）
      main.querySelectorAll('script').forEach(s=>{{
        const ns=document.createElement('script');
        ns.textContent=s.textContent;
        s.parentNode.replaceChild(ns,s);
      }});
      bindEvents();
      if(push)history.pushState({{}},' ',url);
    }}catch(e){{
      main.innerHTML='<p style="color:#ef5350">加载失败: '+e.message+'</p>';
    }}
  }}

  function bindEvents(){{
    // 所有 SPA 链接（data-tab）
    main.querySelectorAll('a[data-tab]').forEach(a=>{{
      a.addEventListener('click',e=>{{
        e.preventDefault();
        loadTab(a.getAttribute('href'),true);
        document.querySelectorAll('nav a').forEach(n=>n.classList.remove('active'));
        const target=document.querySelector('nav a[data-tab="'+a.dataset.tab+'"]');
        if(target)target.classList.add('active');
      }});
    }});
    // 分页器和工具栏链接
    main.querySelectorAll('.pager a,.toolbar a').forEach(a=>{{
      if(!a.hasAttribute('data-tab')){{
        a.addEventListener('click',e=>{{
          e.preventDefault();
          loadTab(a.getAttribute('href'),true);
        }});
      }}
    }});
    // ABC区分类标签点击
    main.querySelectorAll('.category-tab').forEach(tab=>{{
      tab.addEventListener('click',()=>{{
        const filter=tab.dataset.filter;
        const url=new URL(window.location);
        if(filter==='all')url.searchParams.delete('kind');
        else url.searchParams.set('kind',filter);
        url.searchParams.set('page','1');
        // 保留搜索和排序参数
        const q=document.getElementById('media-search');
        if(q&&q.value)url.searchParams.set('q',q.value);
        // 从表单中读取 sort 和 order
        const sf=document.querySelector('.sort-form');
        if(sf){{
          const so=sf.querySelector('select');
          if(so)url.searchParams.set('sort',so.value);
          const hf=sf.querySelector('input[name=order]');
          if(hf)url.searchParams.set('order',hf.value);
        }}
        loadTab(url.toString(),true);
      }});
    }});
    // 排序按钮（上下箭头）
    document.querySelectorAll('.sort-btn').forEach(btn=>{{
      btn.addEventListener('click',()=>{{
        const sf=document.querySelector('.sort-form');
        if(sf){{
          const so=sf.querySelector('select');
          const hf=sf.querySelector('input[name=order]');
          if(so&&hf){{
            const url=new URL(window.location);
            url.searchParams.set('sort',so.value);
            url.searchParams.set('order',hf.value);
            // 保留当前 kind 和 q
            const activeTab=document.querySelector('.category-tab.active');
            if(activeTab&&activeTab.dataset.filter!=='all')url.searchParams.set('kind',activeTab.dataset.filter);
            const q=document.getElementById('media-search');
            if(q&&q.value)url.searchParams.set('q',q.value);
            url.searchParams.set('page','1');
            loadTab(url.toString(),true);
          }}
        }}
      }});
    }});
    // 媒体卡片点击（使用 data-href 避免嵌套 <a> 标签的下划线样式问题）
    main.querySelectorAll('.media-card[data-href]').forEach(card=>{{
      card.style.cursor='pointer';
      card.addEventListener('click',()=>{{
        const href=card.getAttribute('data-href');
        if(href)loadTab(href,true);
      }});
    }});
    // ABC区搜索按钮
    main.querySelectorAll('.search-btn').forEach(btn=>{{
      btn.addEventListener('click',()=>{{doSearch();}});
    }});
    // 搜索回车
    const mq=document.getElementById('media-search');
    if(mq){{
      mq.addEventListener('keydown',e=>{{
        if(e.key==='Enter'){{
          e.preventDefault();
          doSearch();
        }}
      }});
    }}
    // TMDB待看列表搜索回车
    const twlq=document.getElementById('tmdb-wl-search');
    if(twlq){{
      twlq.addEventListener('keydown',e=>{{
        if(e.key==='Enter'){{
          e.preventDefault();
          doTmdbWatchlistSearch();
        }}
      }});
    }}
    main.querySelectorAll('.tmdb-wl-search-btn').forEach(btn=>{{
      btn.addEventListener('click',()=>{{doTmdbWatchlistSearch();}});
    }});
    // TMDB搜索按钮和回车（API区，保留备用）
    const tq=document.getElementById('tmdb-query');
    if(tq){{
      tq.addEventListener('keydown',e=>{{
        if(e.key==='Enter'){{
          e.preventDefault();
          doTmdbSearch();
        }}
      }});
    }}
    main.querySelectorAll('.tmdb-search-btn').forEach(btn=>{{
      btn.addEventListener('click',()=>{{doTmdbSearch();}});
    }});
    // TMDB子导航标签
    main.querySelectorAll('.tmdb-sub-tab').forEach(tab=>{{
      tab.addEventListener('click',()=>{{switchSub(tab.dataset.sub);}});
    }});
  }}

  // 全局函数：ABC区搜索
  window.doSearch=function(){{
    const qEl=document.getElementById('media-search');
    if(!qEl)return;
    const q=qEl.value;
    const activeTab=document.querySelector('.category-tab.active');
    const kind=activeTab&&activeTab.dataset.filter&&activeTab.dataset.filter!=='all' ? activeTab.dataset.filter : '';
    const url=new URL(window.location);
    if(q)url.searchParams.set('q',q); else url.searchParams.delete('q');
    if(kind)url.searchParams.set('kind',kind); else url.searchParams.delete('kind');
    url.searchParams.set('page','1');
    loadTab(url.toString(),true);
  }};

  // 全局函数：TMDB子导航
  window.switchSub=function(sub){{
    const url=new URL(window.location);
    url.searchParams.set('sub',sub);
    url.searchParams.delete('q');
    url.searchParams.delete('page');
    url.searchParams.delete('search_type');
    if(sub==='watchlist')url.searchParams.set('type','movie');
    loadTab(url.toString(),true);
  }};

  // 全局函数：TMDB待看列表搜索（本地过滤）
  window.doTmdbWatchlistSearch=function(){{
    const qEl=document.getElementById('tmdb-wl-search');
    if(!qEl)return;
    const q=qEl.value;
    const url=new URL(window.location);
    url.searchParams.set('tab','tmdb');
    if(q)url.searchParams.set('q',q); else url.searchParams.delete('q');
    url.searchParams.set('page','1');
    loadTab(url.toString(),true);
  }};

  // 全局函数：TMDB搜索（API区，保留备用）
  window.doTmdbSearch=function(){{
    const qEl=document.getElementById('tmdb-query');
    const tEl=document.getElementById('tmdb-search-type');
    if(!qEl||!tEl)return;
    const q=qEl.value;
    const t=tEl.value;
    const url=new URL(window.location);
    url.searchParams.set('sub','search');
    url.searchParams.set('search_type',t);
    url.searchParams.set('q',q);
    url.searchParams.delete('page');
    loadTab(url.toString(),true);
  }};

  window.addEventListener('popstate',()=>{{
    loadTab(location.href,false);
  }});

  document.querySelectorAll('nav a').forEach(a=>{{
    a.addEventListener('click',e=>{{
      e.preventDefault();
      loadTab(a.getAttribute('href'),true);
      document.querySelectorAll('nav a').forEach(n=>n.classList.remove('active'));
      a.classList.add('active');
    }});
  }});

  bindEvents();
}})();
</script>
</body>
</html>"""

    # ----------------------------------------------------------
    # 仪表盘
    # ----------------------------------------------------------
    def _render_dashboard(self):
        db: Database = self.webui._db
        try:
            counts = db.get_table_counts()
            b_status = db.get_b_status_counts()
        except Exception:
            counts = {}
            b_status = {}

        a_count = counts.get("a_strm_files", 0)
        b_count = counts.get("b_strm_files", 0)
        c_count = counts.get("c_ghost_files", 0)

        valid_n = b_status.get("valid", 0)
        orphan_n = b_status.get("orphan", 0)
        unknown_n = b_status.get("unknown", 0)

        tmdb_status = "已配置" if self.webui._tmdb_client else "未配置"

        return f"""
<h2 style="font-size:20px;margin-bottom:20px;color:#8ecdf7">📊 仪表盘</h2>
<div class="stat-grid">
  <div class="stat-card"><div class="label">A 区 STRM</div><div class="value">{a_count}</div></div>
  <div class="stat-card"><div class="label">B 区 STRM</div><div class="value">{b_count}</div></div>
  <div class="stat-card"><div class="label">C 区幽灵</div><div class="value">{c_count}</div></div>
  <div class="stat-card"><div class="label">B - valid</div><div class="value" style="color:#66bb6a">{valid_n}</div></div>
  <div class="stat-card"><div class="label">B - orphan</div><div class="value" style="color:#ffa726">{orphan_n}</div></div>
  <div class="stat-card"><div class="label">B - unknown</div><div class="value" style="color:#ef5350">{unknown_n}</div></div>
  <div class="stat-card"><div class="label">TMDB</div><div class="value" style="font-size:18px">{tmdb_status}</div></div>
  <div class="stat-card"><div class="label">运行时间</div><div class="value" style="font-size:18px" id="uptime-val">-</div></div>
</div>
"""

    # ----------------------------------------------------------
    # A/B/C 区页面
    # ----------------------------------------------------------
    def _render_area_page(self, area, media=None, kind=None,
                          page="1", q=None, sort=None, order=None):
        """
        区页面逻辑：
        - 未指定 media → 显示媒体子类卡片列表
        - 已指定 media → 显示该子类下的 STRM 文件列表
        """
        db: Database = self.webui._db
        raw_records = self._get_records(area)
        kind_label = {"a": "A区", "b": "B区", "c": "C区"}.get(area, area)

        # 计算 media_kind 和 media_name
        for r in raw_records:
            r["media_kind"], r["media_name"] = _media_info(r)

        # 按 (kind, name) 聚合（用于分类标签计数，基于过滤前的全量数据）
        all_groups: OrderedDict[tuple[str, str], list[dict]] = OrderedDict()
        for r in raw_records:
            key = (r.get("media_kind", "未分类"), r.get("media_name", "未知"))
            all_groups.setdefault(key, []).append(r)

        # 大类过滤（在聚合之后，不影响分类标签计数）
        if kind == "anime":
            raw_records = [
                r for r in raw_records if r.get("media_kind") == "番剧"]
        elif kind == "movie":
            raw_records = [
                r for r in raw_records if r.get("media_kind") == "电影"]

        # 搜索过滤（只搜媒体名和大类名）
        if q:
            q_low = q.lower()
            raw_records = [
                r for r in raw_records
                if q_low in (r.get("media_name") or "").lower()
                or q_low in (r.get("media_kind") or "").lower()
            ]

        # 按 (kind, name) 聚合（用于卡片列表显示，基于过滤后的数据）
        groups: OrderedDict[tuple[str, str], list[dict]] = OrderedDict()
        for r in raw_records:
            key = (r.get("media_kind", "未分类"), r.get("media_name", "未知"))
            groups.setdefault(key, []).append(r)

        # 有 media 参数时：显示详情表格
        if media:
            target_name = media
            records = []
            for (mk, mn), recs in groups.items():
                if mn == target_name:
                    records = recs
                    break
            if not records:
                records = [r for r in raw_records if r.get(
                    "media_name") == target_name]
            return self._render_detail_table(
                area, records, media, kind or "", sort or "name", order or "asc", q or "")

        # 无 media 参数时：显示卡片列表
        # 按 kind 分类统计（基于过滤前的全量数据）
        kind_counts: dict[str, int] = {}
        for (mk, mn), recs in all_groups.items():
            kind_counts[mk] = kind_counts.get(mk, 0) + len(recs)

        out = []
        out.append(
            f'<h2 style="font-size:20px;margin-bottom:16px;color:#8ecdf7">{kind_label} 媒体浏览</h2>')

        # 分类标签
        out.append('<div class="category-tabs">')
        total_all = sum(kind_counts.values())
        active_cls = " active" if not kind else ""
        out.append(
            f'<span class="category-tab{active_cls}" data-filter="all">全部 ({total_all})</span>')
        for k, cnt in sorted(kind_counts.items(), key=lambda x: -x[1]):
            active_cls = " active" if kind and (
                (kind == "anime" and k == "番剧") or
                (kind == "movie" and k == "电影") or
                (not kind and True)
            ) else ""
            filter_val = "anime" if k == "番剧" else "movie" if k == "电影" else "other"
            out.append(
                f'<span class="category-tab{active_cls}" data-filter="{filter_val}">{k} ({cnt})</span>')
        out.append('</div>')

        # 搜索框
        q_val = html.escape(q or "")
        out.append(f'''
<div class="toolbar">
  <input type="text" id="media-search" placeholder="搜索媒体名..." value="{q_val}">
  <button class="search-btn">搜索</button>
</div>
''')

        # 媒体卡片
        q_param = f"&q={html.escape(q)}" if q else ""
        if not groups:
            out.append(
                '<div style="color:#8899aa;font-size:14px;padding:20px;text-align:center">没有匹配的媒体</div>')
        else:
            out.append('<div class="media-grid">')
            for (mk, mn), recs in groups.items():
                count = len(recs)
                out.append(f'''
<div class="media-card" data-kind="{mk}" data-href="/?tab=area_{area}&media={mn}&kind={kind or ''}{q_param}">
  <div class="title">{html.escape(mn)}</div>
  <div class="meta">{mk} · {count} 个文件</div>
</div>
''')
            out.append('</div>')

        out.append('''
''')
        return "\n".join(out)

    # ----------------------------------------------------------
    # STRM 文件详情表格
    # ----------------------------------------------------------
    def _render_detail_table(self, area, records, media, kind, sort, order, q):
        reverse = order == "desc"
        if sort == "time":
            records.sort(
                key=lambda r: float(
                    r.get("updated_at") or 0),
                reverse=reverse)
        elif sort == "path":
            records.sort(
                key=lambda r: (
                    r.get("webdav_path") or r.get("local_path") or "").lower(),
                reverse=reverse)
        else:
            records.sort(key=lambda r: r.get("media_name")
                         or r.get("local_path") or "", reverse=reverse)

        total = len(records)

        # 按季分组
        season_groups: OrderedDict[str, list[tuple[int, dict]]] = OrderedDict()
        for r in records:
            lp = r.get("local_path", "")
            season_label = _extract_season_from_local_path(lp)
            if not season_label:
                season_label = "其他"
            season_groups.setdefault(season_label, []).append((0, r))

        # 为每条记录添加序号
        idx = 0
        for slabel in season_groups:
            new_items = []
            for _, r in season_groups[slabel]:
                idx += 1
                new_items.append((idx, r))
            season_groups[slabel] = new_items

        back_kind = f"&kind={kind}" if kind else ""
        out = []
        out.append(f'''
<div class="toolbar">
  <a href="/?tab=area_{area}{back_kind}">← 返回列表</a>
  <span style="color:#8899aa;font-size:13px">{html.escape(media)} · {total} 个文件</span>
</div>
''')

        area_headers = {
            "a": ["#", "本地路径", "WebDAV 路径", "更新时间"],
            "b": ["#", "本地路径", "WebDAV 路径", "指纹", "状态", "更新时间"],
            "c": ["#", "本地路径", "WebDAV 路径", "原 B 路径", "幽灵根", "迁移时间"],
        }
        headers = area_headers.get(area, area_headers["b"])

        # 排序链接
        def sort_link(col_name, col_key):
            new_order = "desc" if sort == col_key and order == "asc" else "asc"
            arrow = ""
            if sort == col_key:
                arrow = " ▲" if order == "asc" else " ▼"
            from urllib.parse import quote_plus
            params = f"tab=area_{area}&media={media}"
            if kind:
                params += f"&kind={kind}"
            if q:
                params += f"&q={quote_plus(q)}"
            params += f"&sort={col_key}&order={new_order}"
            return f'<a href="/?{params}" style="color:#8ecdf7;text-decoration:none">{col_name}{arrow}</a>'

        for slabel, items in season_groups.items():
            out.append(
                f'<div class="season-header">{html.escape(slabel)} ({len(items)} 个文件)</div>')
            out.append('<div class="table-wrap"><table><thead><tr>')
            out.append(f'<th>{sort_link("#", "name")}</th>')
            if area == "a":
                out.append(
                    f'<th>{
                        sort_link(
                            "本地路径",
                            "path")}</th><th>WebDAV 路径</th><th>{
                        sort_link(
                            "时间",
                            "time")}</th>')
            elif area == "b":
                out.append(
                    f'<th>{
                        sort_link(
                            "本地路径",
                            "path")}</th><th>WebDAV 路径</th><th>指纹</th><th>状态</th><th>{
                        sort_link(
                            "时间",
                            "time")}</th>')
            elif area == "c":
                out.append(
                    f'<th>{
                        sort_link(
                            "本地路径",
                            "path")}</th><th>WebDAV 路径</th><th>原 B 路径</th><th>幽灵根</th><th>{
                        sort_link(
                            "时间",
                            "time")}</th>')
            out.append('</tr></thead><tbody>')

            for i, r in items:
                lp = html.escape(str(r.get("local_path", "")))
                wdp = html.escape(str(r.get("webdav_path", "")))
                ts = r.get("updated_at") or r.get("moved_at")
                ts_str = time.strftime(
                    "%m-%d %H:%M", time.localtime(ts)) if ts else "-"

                if area == "a":
                    out.append(
                        f'<tr><td>{i}</td><td>{lp}</td><td>{wdp}</td><td>{ts_str}</td></tr>')
                elif area == "b":
                    fp = html.escape(str(r.get("fingerprint", "")))[:16]
                    st = r.get("status", "valid")
                    badge_cls = f"badge-{st}" if st in (
                        "valid", "orphan") else "badge-orphan"
                    out.append(
                        f'<tr><td>{i}</td><td>{lp}</td><td>{wdp}</td><td style="font-family:monospace;font-size:11px">{fp}</td><td><span class="badge {badge_cls}">{st}</span></td><td>{ts_str}</td></tr>')
                elif area == "c":
                    obp = html.escape(str(r.get("original_b_path", "")))
                    gr = html.escape(str(r.get("ghost_root", "")))
                    out.append(
                        f'<tr><td>{i}</td><td>{lp}</td><td>{wdp}</td><td>{obp}</td><td>{gr}</td><td>{ts_str}</td></tr>')

            out.append('</tbody></table></div>')

        return "\n".join(out)

    # ----------------------------------------------------------
    # TMDB 面板
    # ----------------------------------------------------------
    def _render_tmdb(self, params):
        tmdb_client = self.webui._tmdb_client
        if not tmdb_client:
            return '''
<h2 style="font-size:20px;margin-bottom:20px;color:#8ecdf7">🎬 TMDB 待看列表</h2>
<div style="background:rgba(255,193,7,.1);border:1px solid rgba(255,193,7,.3);
  border-radius:10px;padding:20px;color:#ffc107;font-size:14px">
  ⚠️ 未配置 TMDB access_token，请在 config.toml 的 [tmdb] 段填入 access_token。
</div>
'''
        media_type = params.get("type", ["movie"])[0]
        page = _safe_int(params.get("page", ["1"])[0], 1)
        query = params.get("q", [""])[0]
        account_id = tmdb_client.account_id
        username = tmdb_client.username or account_id
        avatar_hash = tmdb_client.avatar_path
        avatar_url = (
            f"/api/tmdb/avatar?hash={avatar_hash}"
            if avatar_hash else ""
        )
        _web_base = self.webui._tmdb_client.web_base() if self.webui._tmdb_client else "https://www.themoviedb.org"
        profile_url = f"{_web_base}/u/{username}/watchlist"
        # 从缓存获取待看列表（避免每次页面加载都全量拉取 TMDB API）
        all_items = self.webui.get_watchlist_cached()

        # 按媒体类型筛选
        if media_type == "tv":
            items = [i for i in all_items if i.get("_media_type") == "tv"]
        else:
            media_type = "movie"
            items = [i for i in all_items if i.get("_media_type") == "movie"]

        # 构建 B 区已收录媒体名集合（用于收录状态匹配）
        collected_names: set[str] = set()
        try:
            db: Database = self.webui._db
            with db.read_connection() as conn:
                rows = conn.execute(
                    "SELECT local_path, webdav_path, parent_webdav_path, "
                    "source_a_path, fingerprint, status, updated_at FROM b_strm_files"
                ).fetchall()
            for r in rows:
                record = {"local_path": r[0], "webdav_path": r[1]}
                _kind, name = _media_info(record)
                if name and name != "未知":
                    collected_names.add(name.lower())
        except Exception as e:
            logger.warning("[TMDB] 获取 B 区记录失败: %s", e)

        def _match_status(item):
            """将 TMDB 条目与 B 区已收录媒体名匹配"""
            name = (item.get("title") or item.get("name") or "").strip()
            orig = (item.get("original_name") or item.get(
                "original_title") or "").strip()
            for n in (name, orig):
                if not n:
                    continue
                nl = n.lower()
                if nl in collected_names:
                    return "in"
                for cn in collected_names:
                    if nl in cn or cn in nl:
                        return "in"
            return "out"

        # 为每个条目标记收录状态
        for item in items:
            item["_status"] = _match_status(item)

        # 搜索过滤（只搜待看列表里的媒体名）
        if query:
            ql = query.lower()
            items = [
                it for it in items
                if ql in (it.get("title") or it.get("name") or "").lower()
                or ql in (it.get("original_name") or it.get("original_title") or "").lower()
            ]

        # 本地分页
        ps = PAGE_SIZE
        total = len(items)
        total_pages = max(1, ceil(total / ps))
        page = min(max(page, 1), total_pages)
        page_items = items[(page - 1) * ps: page * ps]

        out = []
        avatar_html = (
            f'<a href="{profile_url}" target="_blank" rel="noopener" '
            f'title="{html.escape(username)} 的 TMDB 主页" '
            f'style="display:inline-flex;align-items:center;gap:8px;'
            f'text-decoration:none;vertical-align:middle">'
            f'<img src="{avatar_url}" alt="" '
            f'style="width:28px;height:28px;border-radius:50%;'
            f'object-fit:cover;border:2px solid rgba(142,205,247,.4)">'
            f'<span style="font-size:14px;color:#8ecdf7;'
            f'font-weight:600">{html.escape(username)}</span></a>'
        ) if avatar_url else (
            f'<a href="{profile_url}" target="_blank" rel="noopener" '
            f'title="{html.escape(username)} 的 TMDB 主页" '
            f'style="text-decoration:none;vertical-align:middle">'
            f'<span style="font-size:14px;color:#8ecdf7;'
            f'font-weight:600">{html.escape(username)}</span></a>'
        )
        out.append(
            f'<h2 style="font-size:20px;margin-bottom:16px;color:#8ecdf7">'
            f'🎬 TMDB 待看列表 '
            f'<span style="font-size:13px;color:#8899aa;'
            f'margin-left:8px;vertical-align:middle">{avatar_html}</span></h2>'
        )
        out.append('<div id="tmdb-content">')

        # 搜索框
        out.append(f'''
<div class="toolbar">
  <input type="text" id="tmdb-wl-search" placeholder="搜索待看列表..."
         value="{html.escape(query)}" style="min-width:250px">
  <button class="tmdb-wl-search-btn" onclick="doTmdbWatchlistSearch()">搜索</button>
</div>
''')

        # 类型切换标签
        m_cls = 'font-weight:600;color:#8ecdf7' if media_type == "movie" else ""
        t_cls = 'font-weight:600;color:#8ecdf7' if media_type == "tv" else ""
        out.append(f'''
<div class="toolbar">
  <a href="/?tab=tmdb&type=movie" style="{m_cls}">电影</a>
  <a href="/?tab=tmdb&type=tv" style="{t_cls}">剧集</a>
  <span style="color:#8899aa;font-size:12px;margin-left:auto">
    第 {page}/{total_pages} 页 · 共 {total} 项
  </span>
</div>
''')

        # 条目网格
        state_map = {"in": "已收录", "out": "未收录", "que": "有疑问"}
        out.append('<div class="media-grid">')
        for item in page_items:
            season_count = item.get("_season_count", 0) or 0
            title = item.get("title") or item.get("name") or "N/A"
            date = item.get("release_date") or item.get("first_air_date") or ""
            rating = item.get("vote_average", 0)
            overview = (item.get("overview") or "")[:100]
            tmdb_id = item.get("id", 0)
            status = item.get("_status", "out")
            detail_url = f"{_web_base}/{media_type}/{tmdb_id}?language=zh-CN"

            # 多季番剧竖杠
            season_bars = ""
            if season_count > 1:
                palette = ["bar-green", "bar-blue", "bar-purple"]
                num_bars = 3 if season_count >= 5 else (2 if season_count >= 3 else 1)
                bars_html = "".join(
                    f'<span class="tmdb-season-bar {c}"></span>'
                    for c in palette[:num_bars]
                )
                season_bars = f'<div class="tmdb-season-bars">{bars_html}</div>'

            out.append(
                f'<a class="media-card" href="{detail_url}" target="_blank" rel="noopener">\n'
                f'{season_bars}'
                f'  <span class="badge {status}">{
                    state_map.get(
                        status, "未收录")}</span>\n'
                f'  <div class="title">{html.escape(title)}</div>\n'
                f'  <div class="meta">⭐ {rating:.1f} · {html.escape(date)} · ID: {tmdb_id}</div>\n'
                f'  <div class="overview" style="font-size:11px;color:#a0b0c0;margin-top:4px;'
                f'max-height:40px;overflow:hidden">{html.escape(overview)}</div>\n'
                f'</a>\n'
            )
        out.append('</div>')

        # 分页器
        q_param = f"&q={html.escape(query)}" if query else ""
        out.append('<div class="pager">')
        if page > 1:
            out.append(
                f'<a href="/?tab=tmdb&type={media_type}&page={
                    page - 1}{q_param}">← 上一页</a>'
            )
        out.append(f'<span class="current">第 {page} 页</span>')
        if page < total_pages:
            out.append(
                f'<a href="/?tab=tmdb&type={media_type}&page={
                    page + 1}{q_param}">下一页 →</a>'
            )
        out.append('</div>')

        # 导出按钮
        out.append('''
<div class="toolbar">
  <button id="tmdb-export-csv-btn" style="background:rgba(100,180,255,.15);border:1px solid rgba(100,180,255,.3);border-radius:8px;padding:8px 18px;color:#8ecdf7;font-size:13px;cursor:pointer">📥 导出待看列表 CSV</button>
  <span id="tmdb-export-msg" style="color:#66bb6a;font-size:13px;display:none;margin-left:12px"></span>
  <a href="/api/tmdb/watchlist/movie?all=1" target="_blank" style="margin-left:12px">导出电影 JSON</a>
  <a href="/api/tmdb/watchlist/tv?all=1" target="_blank" style="margin-left:12px">导出剧集 JSON</a>
</div>
<script>
(function(){
  var btn = document.getElementById('tmdb-export-csv-btn');
  var msg = document.getElementById('tmdb-export-msg');
  if(btn){
    btn.addEventListener('click', function(e){
      e.preventDefault();
      btn.disabled = true;
      btn.textContent = '导出中...';
      if(msg) { msg.style.display = 'inline'; msg.textContent = '正在保存...'; msg.style.color = '#8899aa'; }
      fetch('/api/tmdb/watchlist/export.csv')
        .then(function(r){ return r.json(); })
        .then(function(data){
          if(data.success){
            if(msg){ msg.textContent = data.message; msg.style.color = '#66bb6a'; }
          }else{
            if(msg){ msg.textContent = data.message; msg.style.color = '#ef5350'; }
          }
        })
        .catch(function(err){
          console.error('导出失败', err);
          if(msg){ msg.textContent = '导出失败: ' + err.message; msg.style.color = '#ef5350'; }
        })
        .finally(function(){
          btn.disabled = false;
          btn.textContent = '📥 导出待看列表 CSV';
        });
    });
  }
})();
</script>
''')

        out.append('</div>')  # tmdb-content
        return "\n".join(out)

    # ----------------------------------------------------------
    # 日志页面
    # ----------------------------------------------------------
    def _render_logs(self):
        log_file = self.webui._log_file
        lines = []
        if log_file and os.path.exists(log_file):
            try:
                with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = f.readlines()
                    lines = all_lines[-200:]
            except Exception:
                pass

        rendered = []
        for line in lines:
            line = line.rstrip()
            cls = "log-line"
            if "ERROR" in line:
                cls += " error"
            elif "WARN" in line:
                cls += " warn"
            elif "INFO" in line:
                cls += " info"
            elif "DEBUG" in line:
                cls += " debug"
            rendered.append(f'<div class="{cls}">{html.escape(line)}</div>')

        return f'''
<h2 style="font-size:20px;margin-bottom:16px;color:#8ecdf7">📋 WebUI 日志</h2>
<div class="toolbar">
  <button onclick="location.reload()">刷新</button>
  <span style="color:#8899aa;font-size:12px">最近 200 行</span>
</div>
<div class="log-panel">{"".join(rendered) or "<div style='color:#8899aa'>暂无日志</div>"}</div>
'''

    # ----------------------------------------------------------
    # 配置页面
    # ----------------------------------------------------------
    def _render_config(self):
        cfg = self.webui._config
        tmdb_client = self.webui._tmdb_client
        token = cfg.tmdb.access_token or ""
        api_key = cfg.tmdb.api_key or ""
        host = cfg.tmdb.host or ""
        proxy = cfg.tmdb.proxy_http if cfg.tmdb.proxy_enabled else ""
        watchlist_db = cfg.tmdb.watchlist_db or ""
        language = cfg.tmdb.language or "zh-CN"
        language_options = [
            ('zh-CN', '中文'),
            ('en-US', '英语'),
            ('ja-JP', '日语'),
            ('custom', '自定义'),
        ]

        def field(label: str, input_html: str) -> str:
            return (
                f'<div class="floating-field">'
                f'<div class="field-control">'
                f'<label class="floating-label is-shown is-floating is-filled" data-role="label">{html.escape(label)}</label>'
                f'{input_html}'
                f'</div>'
                f'</div>'
            )

        def text_input(field_id: str, value: str, placeholder: str, input_type: str = 'text') -> str:
            value_attr = html.escape(value)
            placeholder_attr = html.escape(placeholder)
            input_class = 'has-value' if value else ''
            return (
                f'<input type="{input_type}" id="{field_id}" value="{value_attr}" '
                f'placeholder="{placeholder_attr}" class="{input_class}">'
            )

        language_options_html = ''.join(
            f'<option value="{html.escape(val)}"{" selected" if language == val else ""}>{html.escape(label)}</option>'
            for val, label in language_options
        )

        html_parts = [
            '<h2 style="font-size:20px;margin-bottom:20px;color:#8ecdf7">⚙️ 配置</h2>',
            '<div class="config-section">',
            '<h3>💾 数据库</h3>',
            f'<div class="config-row"><span class="key">路径</span><span class="val">{html.escape(cfg.db_file)}</span></div>',
            f'<div class="config-row"><span class="key">存在</span><span class="val">{"✅" if os.path.exists(cfg.db_file) else "❌"}</span></div>',
            '</div>',
            '<div class="config-section">',
            '<h3>🌐 WebUI</h3>',
            f'<div class="config-row"><span class="key">端口</span><span class="val">{html.escape(str(cfg.webui.port))}</span></div>',
            f'<div class="config-row"><span class="key">绑定</span><span class="val">{html.escape(cfg.webui.bind)}</span></div>',
            f'<div class="config-row"><span class="key">密码</span><span class="val">{"***" if cfg.webui.password else "未设置"}</span></div>',
            '</div>',
            '<div class="config-section">',
            '<h3>🎬 TMDB <span style="font-size:calc(var(--font-base) - 1px);color:#8899aa;font-weight:400">(保存后即时生效，不需重启)</span></h3>',
            field('Access Token', text_input('cfg-tmdb-token', token, '输入 TMDB Access Token', 'password')),
            field('API Key', text_input('cfg-tmdb-apikey', api_key, '输入 TMDB API Key')),
            '<div class="floating-field" data-field="cfg-tmdb-lang">',
            '<div class="field-control">',
            '<label class="floating-label is-shown is-floating is-filled" data-role="label">语言</label>',
            '<div class="field-select-wrap">',
            f'<select id="cfg-tmdb-lang-select">{language_options_html}</select>',
            f'<input type="text" id="cfg-tmdb-lang" value="{html.escape(language)}" placeholder="输入语言代码" class="has-value">',
            '</div>',
            '</div>',
            '</div>',
            field('反代 Host', text_input('cfg-tmdb-host', host, '留空则使用官方 API')),
            field('HTTP 代理', text_input('cfg-tmdb-proxy', proxy, '例: http://127.0.0.1:7890')),
            field('Watchlist DB', text_input('cfg-tmdb-wldb', watchlist_db, '留空默认 tmdb_watchlist.db')),
            f'<div class="config-row"><span class="key">account_id</span><span class="val">{html.escape(str(tmdb_client.account_id) if tmdb_client else "未获取")}</span></div>',
            f'<div class="config-row"><span class="key">状态</span><span class="val">{"✅ 已配置" if cfg.tmdb.access_token else "❌ 未配置"}</span></div>',
            '<div class="config-form-actions">'
            '<button class="toolbar-btn primary" id="cfg-tmdb-save">✅ 保存 TMDB 配置</button>'
            '<button class="toolbar-btn" id="cfg-tmdb-refresh">🔄 刷新待看列表</button>'
            '<button class="toolbar-btn secondary" id="cfg-tmdb-restart" style="color:#e37400;border-color:#e37400">🔁 重启 WebUI</button>'
            '</div>',
            '</div>',
            '<script>',
            '(function(){',
            'function _q(id){return document.getElementById(id)}',
            'function _qa(sel){return document.querySelectorAll(sel)}',
            '',
            '// Floating label focus/blur',
            '_qa(".floating-field input, .floating-field select").forEach(function(elm){',
            '  var wrapper=elm.closest(".floating-field");',
            '  var label=wrapper&&wrapper.querySelector(".floating-label");',
            '  if(!label)return;',
            '  function sync(){',
            '    var hv=String(elm.value||"").trim()!=="";',
            '    var f=document.activeElement===elm;',
            '    label.classList.toggle("is-shown",hv||f);',
            '    label.classList.toggle("is-floating",f);',
            '    label.classList.toggle("is-filled",hv&&!f);',
            '    elm.classList.toggle("has-value",hv);',
            '  }',
            '  elm.addEventListener("focus",sync);',
            '  elm.addEventListener("blur",sync);',
            '  elm.addEventListener("input",sync);',
            '  sync();',
            '});',
            '',
            '// Language field: float label on select/input focus',
            '(function(){',
            '  var ls=_q("cfg-tmdb-lang-select");',
            '  var li=_q("cfg-tmdb-lang");',
            '  var fld=document.querySelector(\'[data-field="cfg-tmdb-lang"]\');',
            '  var lbl=fld&&fld.querySelector(".floating-label");',
            '  function syncLangLabel(){',
            '    if(!lbl)return;',
            '    var f1=document.activeElement===ls;',
            '    var f2=document.activeElement===li;',
            '    var hv=String(li&&li.value||"").trim()!=="";',
            '    lbl.classList.toggle("is-shown",hv||f1||f2);',
            '    lbl.classList.toggle("is-floating",f1||f2);',
            '    lbl.classList.toggle("is-filled",hv&&!f1&&!f2);',
            '  }',
            '  if(ls){ls.addEventListener("focus",syncLangLabel);ls.addEventListener("blur",syncLangLabel);}',
            '  if(li){li.addEventListener("focus",syncLangLabel);li.addEventListener("blur",syncLangLabel);li.addEventListener("input",syncLangLabel);}',
            '  syncLangLabel();',
            '})();',
            '',
            '// Token: show on focus, hide on blur',
            'var ti=_q("cfg-tmdb-token");',
            'if(ti){',
            '  ti.addEventListener("focus",function(){ti.type="text"});',
            '  ti.addEventListener("blur",function(){ti.type="password"});',
            '}',
            '',
            '// Language dropdown sync',
            'var ls=_q("cfg-tmdb-lang-select");',
            'var li=_q("cfg-tmdb-lang");',
            'if(ls&&li){',
            '  var pm={"zh-CN":"zh-CN","en-US":"en-US","ja-JP":"ja-JP"};',
            '  function syncLang(){',
            '    var c=ls.value;',
            '    if(pm[c]){li.value=c;li.disabled=true;}',
            '    else{li.disabled=false;if(!li.value)li.value="zh-CN";}',
            '  }',
            '  ls.addEventListener("change",syncLang);syncLang();',
            '  li.addEventListener("input",function(){',
            '    if(pm[li.value])ls.value=li.value;else ls.value="custom";',
            '  });',
            '}',
            '',
            '// Save handler',
            'var saveBtn=_q("cfg-tmdb-save");',
            'if(saveBtn){',
            '  saveBtn.addEventListener("click",async function(){',
            '    saveBtn.disabled=true;saveBtn.textContent="保存中...";',
            '    try{',
            '      var lc=_q("cfg-tmdb-lang-select").value;',
            '      var lv=lc==="custom"?(_q("cfg-tmdb-lang").value||"zh-CN"):lc;',
            '      var body=JSON.stringify({',
            '        access_token:_q("cfg-tmdb-token").value,',
            '        api_key:_q("cfg-tmdb-apikey").value,',
            '        language:lv,',
            '        host:_q("cfg-tmdb-host").value,',
            '        proxy_http:_q("cfg-tmdb-proxy").value,',
            '        proxy_enabled:_q("cfg-tmdb-proxy").value.trim()!=="",',
            '        watchlist_db:_q("cfg-tmdb-wldb").value,',
            '      });',
            '      var resp=await fetch("/api/tmdb/configure",{method:"POST",headers:{"Content-Type":"application/json"},body:body});',
            '      var text=await resp.text();',
            '      if(!resp.ok)throw new Error(text||"HTTP "+resp.status);',
            '      var data;',
            '      try{data=text?JSON.parse(text):{}}catch(pe){throw new Error(text||pe.message)}',
            '      if(data.success===false)throw new Error(data.error||data.message||"HTTP "+resp.status);',
            '      alert(data.message||"已保存");',
            '      location.reload();',
            '    }catch(e){alert("保存失败: "+e.message)}',
            '    finally{saveBtn.disabled=false;saveBtn.textContent="✅ 保存 TMDB 配置"}',
            '  });',
            '}',
            '',
            '// Refresh handler',
            'var refBtn=_q("cfg-tmdb-refresh");',
            'if(refBtn){',
            '  refBtn.addEventListener("click",async function(){',
            '    refBtn.disabled=true;refBtn.textContent="同步中...";',
            '    try{',
            '      var resp=await fetch("/api/tmdb/watchlist/refresh");',
            '      var data=await resp.json();',
            '      if(data.success)alert("同步完成: "+data.count+" 项 (电影 "+data.movies+", 剧集 "+data.tv+")");',
            '      else alert(data.error||"同步失败");',
            '    }catch(e){alert("同步失败: "+e.message)}',
            '    finally{refBtn.disabled=false;refBtn.textContent="🔄 刷新待看列表"}',
            '  });',
            '}',
            '',
            '// Restart handler',
            'var rstBtn=_q("cfg-tmdb-restart");',
            'if(rstBtn){',
            '  rstBtn.addEventListener("click",async function(){',
            '    rstBtn.disabled=true;rstBtn.textContent="重启中...";',
            '    try{',
            '      await fetch("/api/restart-webui",{method:"POST"});',
            '      alert("WebUI 正在重启...");',
            '    }catch(e){alert("重启失败: "+e.message)}',
            '    finally{rstBtn.disabled=false;rstBtn.textContent="🔁 重启 WebUI"}',
            '  });',
            '}',
            '})();',
            '</script>',
        ]

        out = ''.join(html_parts)
        return out

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
    def _handle_bing_wallpapers(self):
        try:
            url = "https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=8&mkt=zh-CN"
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            images = data.get("images", [])
            result = []
            for img in images:
                urlbase = img.get("urlbase", "")
                if urlbase:
                    result.append({
                        "url": f"https://www.bing.com{urlbase}_1920x1080.jpg",
                        "copyright": img.get("copyright", ""),
                        "date": img.get("enddate", ""),
                    })
            self._send_json(result)
        except Exception as e:
            logger.warning("Bing 壁纸获取失败: %s", e)
            self._send_json([])

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
            "webui_has_password": bool(cfg.webui.password),
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
        """获取待看列表（委托给 TmdbWatchlistDb）。"""
        if not self._tmdb_client or not self._watchlist_db:
            return []
        try:
            return self._watchlist_db.sync(self._tmdb_client)
        except Exception as e:
            logger.warning("[TMDB] 同步失败，返回已有缓存: %s", e)
            return self._watchlist_db.get_all()

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
            err = getattr(e, 'winerror', None) or getattr(e, 'errno', None) or 0
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
