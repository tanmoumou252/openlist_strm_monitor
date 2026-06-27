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
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from math import ceil
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse
import urllib.request

from tmdb_watchlist_db import TmdbWatchlistDb
from watchlist_match import (
    _media_info, _compute_media_root, _extract_season_from_local_path,
    _category_filter_value,
    refresh_watchlist_match_state,
)
from webui_routes import (
    _tmdb_routes, _resolve_tmdb_proxy, _build_img_opener, _safe_int,
    _is_lan_ip, _human_size, _h,
)

if TYPE_CHECKING:
    from tmdb_client import TmdbClient
    from config import WebUIConfig
    from database import Database


# ============================================================
# 常量
# ============================================================

PAGE_SIZE = 50
STATIC_DIR = Path(__file__).resolve().parent / "webui_static"

# ============================================================
# 已迁移到 webui_routes.py:
#   _tmdb_routes, TMDB_GENRE_NAMES, _safe_int,
#   _bg_sync_refresh, _build_img_opener, _resolve_tmdb_proxy
#   _is_lan_ip, _human_size, _h
# 已迁移到 watchlist_match.py:
#   _media_info, _compute_media_root, _extract_season_from_local_path,
#   _SEASON_RE, _EPISODE_RE, _MOVIE_HINT_RE, _CATEGORY_DIRS,
#   _cn_to_int, _extract_season_int, _path_parts,
#   _is_category_dir, _category_filter_value
# ============================================================


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
            logos = sorted(STATIC_DIR.glob("logo.*.png"))
            self._send_static_file(
                random.choice(logos).name if logos else "logo.01.png")
        elif path.startswith("/wallpaper/"):
            fname = path[len("/wallpaper/"):]
            if ".." in fname or "/" in fname or "\\" in fname:
                self._send_json({"error": "invalid path"}, 400)
            else:
                self._send_static_file(f"wallpaper/{fname}")
        elif path == "/api/dashboard":
            self._handle_dashboard()
        elif path == "/api/local-wallpapers":
            self._handle_local_wallpapers()
        elif path == "/api/logs":
            self._handle_logs_api(params)
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
                tmdb_cfg.proxy_http = data["proxy_http"] or ""
                tmdb_cfg.proxy.http = data["proxy_http"] or ""  # 兼容嵌套
                changed = True
            if "proxy_enabled" in data:
                tmdb_cfg.proxy_enabled = bool(data["proxy_enabled"])
                tmdb_cfg.proxy.enabled = bool(data["proxy_enabled"])  # 兼容嵌套
                changed = True
            if changed:
                # 重新初始化 TMDB 客户端
                self.webui._init_tmdb_client()
                # 重建 watchlist DB（路径解析逻辑与 __init__ 一致）
                self.webui._reinit_watchlist_db()
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
            logging.error("[WebUI] 保存 TMDB 配置异常: %s", e, exc_info=True)
            db = getattr(self.webui, '_db', None)
            if db:
                try:
                    db.log_tmdb_operation("config_update", "error", f"TMDB 配置保存失败: {e}")
                except Exception:
                    pass
            self._send_json({"success": False, "error": f"保存失败: {e}"}, 500)

    def _save_tmdb_overrides(self, changes: dict) -> None:
        """保存 TMDB 配置覆盖到 .tmdb_webui_config.json（不写 config.toml）。"""
        overrides_file = Path(__file__).resolve(
        ).parent.parent / ".tmdb_webui_config.json"
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
        db = getattr(self.webui, '_db', None)
        if db:
            try:
                db.log_tmdb_operation("restart", "info", "WebUI 重启")
            except Exception:
                pass
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

    def _handle_tmdb_watchlist_match_refresh(self) -> None:
        """触发后台刷新 TMDB 待看列表的 B 区收录状态（不阻塞 HTTP 线程）。"""
        if not getattr(self.webui, '_watchlist_db', None):
            self._send_json(
                {"success": False, "message": "TMDB 待看数据库未启用"}, 400)
            return
        if not getattr(self.webui, '_db', None):
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

    def _do_match_refresh(self) -> None:
        db = getattr(self.webui, '_db', None)
        try:
            tmdb_cfg = getattr(self.webui._config, "tmdb", None)
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
            counts = refresh_watchlist_match_state(self.webui, fuzzy, min_ep)
            with self.webui._match_refresh_lock:
                self.webui._match_refresh_result = counts
            logging.info("[TMDB] 收录状态刷新完成: %s", counts)
            if db:
                try:
                    db.log_tmdb_operation("match_refresh", "success", f"收录状态刷新完成: {counts}")
                except Exception:
                    pass
        except Exception as e:
            logging.error("[TMDB] 收录状态刷新失败: %s", e, exc_info=True)
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

    def _handle_tmdb_watchlist_match_override(self, body: bytes) -> None:
        """手动覆盖 TMDB 待看条目的收录状态。"""
        if not getattr(self.webui, '_watchlist_db', None):
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
            logging.error("[TMDB] 手动覆盖收录状态失败: %s", e, exc_info=True)
            self._send_json({"success": False, "message": f"覆盖失败: {e}"}, 500)

    def _handle_tmdb_watchlist_bg_sync(self) -> None:
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

    def _do_bg_sync(self) -> None:
        try:
            self.webui._watchlist_db.sync(self.webui._tmdb_client)
            logging.info("[TMDB] 后台同步完成")
            db = getattr(self.webui, '_db', None)
            if db:
                try:
                    db.log_tmdb_operation("sync", "success", "TMDB 同步完成")
                except Exception:
                    pass
        except Exception as e:
            logging.warning("[TMDB] 后台同步失败: %s", e)
            db = getattr(self.webui, '_db', None)
            if db:
                try:
                    db.log_tmdb_operation("sync", "error", f"TMDB 同步失败: {e}")
                except Exception:
                    pass
        finally:
            with self.webui._sync_lock:
                self.webui._sync_running = False

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

    def _handle_local_wallpapers(self) -> None:
        wallpaper_dir = STATIC_DIR / "wallpaper"
        wallpapers = []
        if wallpaper_dir.is_dir():
            for f in sorted(wallpaper_dir.iterdir()):
                if f.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp'):
                    wallpapers.append({"url": f"/wallpaper/{f.name}"})
        self._send_json(wallpapers)

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
        kind_label_map = {
            "anime": "番剧",
            "movie": "电影",
            "other": "其他",
            "all": "全部"}
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
            items = [
                it for it in items if _category_filter_value(
                    it["kind"]) == kind_filter]
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
            webdav_root = _compute_media_root(
                records[0].get("webdav_path", ""))
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
        token_preview = (token[:16] +
                         "...") if len(token) > 16 else (token or "")
        self._send_json({
            # 新字段
            "db_file": cfg.local.db_file,
            "db_exists": os.path.isfile(cfg.local.db_file),
            "webui_port": cfg.webui.port,
            "webui_bind": cfg.webui.bind,
            "tmdb_configured": bool(self.webui._tmdb_client),
            "tmdb_token_preview": token_preview,
            "tmdb_token": token,
            "tmdb_language": cfg.tmdb.language,
            "tmdb_host": cfg.tmdb.host or "",
            "tmdb_api_key": cfg.tmdb.api_key or "",
            "tmdb_api_key_configured": bool(cfg.tmdb.api_key),
            "tmdb_proxy": _resolve_tmdb_proxy(cfg),
            "tmdb_proxy_http": cfg.tmdb.proxy_http,
            "tmdb_proxy_enabled": cfg.tmdb.proxy_enabled,
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
        self._sync_lock = threading.Lock()
        self._sync_running = False
        self._match_refresh_lock = threading.Lock()
        self._match_refresh_running = False
        self._match_refresh_result: dict | None = None

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
        overrides_file = Path(__file__).resolve(
        ).parent.parent / ".tmdb_webui_config.json"
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
                        cfg_tmdb.proxy_http = val or ""
                        cfg_tmdb.proxy.http = val or ""  # 兼容嵌套
                        proxy_changed = True
                        continue
                    if key == "proxy_enabled":
                        cfg_tmdb.proxy_enabled = bool(val)
                        cfg_tmdb.proxy.enabled = bool(val)  # 兼容嵌套
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
                        access_token=getattr(
                            tmdb_cfg, "access_token", "") or "",
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
        """获取待看列表。缓存过期时直接返回旧数据，不自动同步。"""
        if not self._tmdb_client or not self._watchlist_db:
            return []
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
