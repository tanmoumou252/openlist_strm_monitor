"""
TMDB API 测试脚本 - 测试 TMDB API 功能

支持两种运行模式：
  1. CLI 模式 - 直接在终端运行，快速测试各 API 端点
  2. Flask 模式 - 启动 HTTP 服务，通过浏览器/curl 访问测试端点

直接从 config.toml 读取 TMDB 配置，不触发 OpenList 初始化。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

sys.path.insert(0, str(Path(__file__).parent))

from tmdb_client import TmdbClient, create_tmdb_client, _CACHE_FILE

# ============================================================
# 日志配置
# ============================================================

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - [%(levelname)s] - %(message)s",
)
logger = logging.getLogger(__name__)

# ============================================================
# 配置加载（直接读 config.toml，不触发 OpenList）
# ============================================================

config_path = Path(__file__).parent.parent / "config.toml"
logger.info("[CONFIG] 加载配置文件: %s", config_path)
logger.info("[CONFIG] 配置文件存在: %s", config_path.exists())

cfg = None
try:
    with open(config_path, "rb") as f:
        data = tomllib.load(f)

    tmdb_data = data.get("tmdb", {})
    proxy_data = tmdb_data.get("proxy", {})

    access_token = tmdb_data.get("access_token", "")
    api_enabled = tmdb_data.get("api_enabled", False)
    language = tmdb_data.get("language", "zh-CN")
    proxy_enabled = proxy_data.get("enabled", False)
    proxy_http = proxy_data.get("http", "")

    logger.info("[CONFIG] ✓ TMDB 配置加载成功")
    logger.info("[CONFIG]   access_token: %s", "已配置" if access_token else "未配置")
    logger.info("[CONFIG]   api_enabled: %s", api_enabled)
    logger.info("[CONFIG]   language: %s", language)
    logger.info("[CONFIG]   proxy_enabled: %s", proxy_enabled)
    if proxy_enabled:
        logger.info("[CONFIG]   proxy_http: %s", proxy_http)

    cfg = type("TmdbCfg", (), {
        "access_token": access_token,
        "api_enabled": api_enabled,
        "language": language,
        "proxy_enabled": proxy_enabled,
        "proxy_http": proxy_http,
    })()
except Exception as exc:
    logger.error("[CONFIG] ✗ 配置加载失败: %s", exc, exc_info=True)

# ============================================================
# 客户端单例
# ============================================================

_client = None


def get_client() -> TmdbClient:
    global _client
    if _client is None:
        if not cfg or not cfg.access_token:
            raise RuntimeError("未配置 TMDB access_token，请在 config.toml [tmdb] 中填写 access_token")
        proxy = cfg.proxy_http if cfg.proxy_enabled else None
        _client = create_tmdb_client(
            access_token=cfg.access_token,
            language=cfg.language,
            proxy=proxy,
            auto_validate=False,
        )
    return _client


# ============================================================
# Flask HTTP 接口（Flask 模式）
# ============================================================

def create_flask_app():
    from flask import Flask, jsonify, request

    app = Flask(__name__)

    @app.route("/")
    def index():
        """首页：显示可用端点"""
        return jsonify({
            "service": "TMDB API 测试服务",
            "endpoints": {
                "/": "本页（端点列表）",
                "/validate": "验证 access_token",
                "/account": "获取 account_id",
                "/cache/status": "查看缓存状态",
                "/cache/clear": "清除 account_id 缓存",
                "/cache/refresh": "强制刷新 account_id 缓存",
                "/watchlist/movies": "获取待看电影 (可选 ?page=N)",
                "/watchlist/tv": "获取待看剧集 (可选 ?page=N)",
                "/watchlist/movies/all": "获取全部待看电影（自动分页）",
                "/watchlist/tv/all": "获取全部待看剧集（自动分页）",
                "/search/movie?query=xxx": "搜索电影",
                "/search/tv?query=xxx": "搜索电视剧",
                "/detail/tv/<tmdb_id>": "获取剧集详情",
                "/detail/movie/<tmdb_id>": "获取电影详情",
                "/alias/tv/<tmdb_id>": "获取剧集别名",
                "/alias/movie/<tmdb_id>": "获取电影别名",
                "/export": "导出待看列表 CSV（快速模式 ?include_aliases=true&include_details=true 可选）",
            },
            "account_id": get_client().account_id,
        })

    @app.route("/validate")
    def validate():
        """验证 access_token 是否有效"""
        client = get_client()
        ok = client.validate_key()
        return jsonify({
            "valid": ok,
            "account_id": client.account_id if ok else None,
        })

    @app.route("/account")
    def account():
        """获取 account_id"""
        client = get_client()
        return jsonify({
            "account_id": client.account_id,
            "cached": _CACHE_FILE.exists(),
        })

    @app.route("/cache/status")
    def cache_status():
        """查看缓存状态"""
        if not _CACHE_FILE.exists():
            return jsonify({
                "cached": False,
                "message": "缓存文件不存在",
            })
        try:
            import json
            with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            cached_ts = data.get("ts", 0)
            age_seconds = time.time() - cached_ts if cached_ts else 0
            return jsonify({
                "cached": True,
                "account_id": data.get("account_id", ""),
                "cached_at": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(cached_ts)
                ) if cached_ts else "unknown",
                "age_minutes": round(age_seconds / 60, 1),
                "expires_in_minutes": round((7 * 24 * 60) - (age_seconds / 60), 1),
                "cache_file": str(_CACHE_FILE),
            })
        except Exception as e:
            return jsonify({
                "cached": True,
                "error": str(e),
            })

    @app.route("/cache/clear")
    def cache_clear():
        """清除 account_id 缓存"""
        if _CACHE_FILE.exists():
            _CACHE_FILE.unlink()
            return jsonify({
                "cleared": True,
                "message": "缓存已清除",
            })
        return jsonify({
            "cleared": False,
            "message": "缓存文件不存在",
        })

    @app.route("/cache/refresh")
    def cache_refresh():
        """强制刷新 account_id 缓存"""
        client = get_client()
        old_id = client._account_id
        client._account_id = ""  # 清空以强制重新获取
        new_id = client.account_id
        return jsonify({
            "old_account_id": old_id,
            "new_account_id": new_id,
            "changed": old_id != new_id,
        })

    @app.route("/watchlist/movies")
    def watchlist_movies():
        """获取待看电影（分页）"""
        client = get_client()
        page = int(request.args.get("page", "1"))
        items, has_next = client.get_watchlist_movies(page=page)
        return jsonify({
            "account_id": client.account_id,
            "media_type": "movie",
            "page": page,
            "has_next_page": has_next,
            "count": len(items),
            "results": items,
        })

    @app.route("/watchlist/tv")
    def watchlist_tv():
        """获取待看剧集（分页）"""
        client = get_client()
        page = int(request.args.get("page", "1"))
        items, has_next = client.get_watchlist_tv(page=page)
        return jsonify({
            "account_id": client.account_id,
            "media_type": "tv",
            "page": page,
            "has_next_page": has_next,
            "count": len(items),
            "results": items,
        })

    @app.route("/watchlist/movies/all")
    def watchlist_movies_all():
        """获取全部待看电影"""
        client = get_client()
        t0 = time.time()
        items = client.fetch_all_watchlist_movies()
        elapsed = round(time.time() - t0, 2)
        return jsonify({
            "account_id": client.account_id,
            "media_type": "movie",
            "count": len(items),
            "elapsed_seconds": elapsed,
            "results": items,
        })

    @app.route("/watchlist/tv/all")
    def watchlist_tv_all():
        """获取全部待看剧集"""
        client = get_client()
        t0 = time.time()
        items = client.fetch_all_watchlist_tv()
        elapsed = round(time.time() - t0, 2)
        return jsonify({
            "account_id": client.account_id,
            "media_type": "tv",
            "count": len(items),
            "elapsed_seconds": elapsed,
            "results": items,
        })

    @app.route("/alias/<media_type>/<int:tmdb_id>")
    def alias(media_type, tmdb_id):
        """获取别名"""
        client = get_client()
        if media_type == "movie":
            aliases = client.get_movie_aliases(tmdb_id)
        elif media_type == "tv":
            aliases = client.get_tv_aliases(tmdb_id)
        else:
            return jsonify({
                "error": "unsupported media_type",
                "type": media_type,
            }), 400

        return jsonify({
            "id": tmdb_id,
            "type": media_type,
            "alias_count": len(aliases),
            "aliases": aliases[:20],
        })

    @app.route("/detail/tv/<int:tmdb_id>")
    def detail_tv(tmdb_id):
        """获取剧集详情"""
        client = get_client()
        data = client.get_tv_details(tmdb_id)
        if not data:
            return jsonify({"error": "not found"}), 404
        last_ep = data.get("last_episode_to_air")
        return jsonify({
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

    @app.route("/detail/movie/<int:tmdb_id>")
    def detail_movie(tmdb_id):
        """获取电影详情"""
        client = get_client()
        data = client.get_movie_details(tmdb_id)
        if not data:
            return jsonify({"error": "not found"}), 404
        return jsonify({
            "id": data.get("id"),
            "title": data.get("title"),
            "original_title": data.get("original_title"),
            "release_date": data.get("release_date"),
            "runtime": data.get("runtime"),
            "vote_average": data.get("vote_average"),
            "overview": data.get("overview", "")[:200],
        })

    @app.route("/search/movie")
    def search_movie():
        """搜索电影"""
        client = get_client()
        query = request.args.get("query", "Chronicle")
        page = int(request.args.get("page", "1"))
        return jsonify({
            "query": query,
            "page": page,
            "count": len(client.search_movie(query, page=page)),
            "results": client.search_movie(query, page=page),
        })

    @app.route("/search/tv")
    def search_tv():
        """搜索电视剧"""
        client = get_client()
        query = request.args.get("query", "Breaking Bad")
        page = int(request.args.get("page", "1"))
        return jsonify({
            "query": query,
            "page": page,
            "count": len(client.search_tv(query, page=page)),
            "results": client.search_tv(query, page=page),
        })

    @app.route("/export")
    def export():
        """
        导出待看列表为 CSV（快速模式，默认）

        查询参数:
          include_aliases=true   逐条拉取别名（慢，每条1次API调用）
          include_details=true   逐条拉取TV详情（慢，每条1次API调用）
        """
        client = get_client()
        include_aliases = request.args.get("include_aliases", "false").lower() == "true"
        include_details = request.args.get("include_details", "false").lower() == "true"
        from tmdb_watchlist import TmdbItem, LastEpisode, export_watchlist_csv, MatchResult

        # 1. 获取待看电影（watchlist 列表本身已包含 id/title/original_title/release_date）
        logger.info("[EXPORT] 开始获取待看电影...")
        raw_movies = client.fetch_all_watchlist_movies()
        logger.info("[EXPORT] 获取到 %d 部电影", len(raw_movies))
        movie_items = []
        for m in raw_movies:
            aliases = set()
            if include_aliases and m.get("id"):
                try:
                    alias_list = client.get_movie_aliases(m["id"])
                    aliases = set(alias_list)
                except Exception:
                    pass
            movie_items.append(TmdbItem(
                media_type="movie",
                tmdb_id=m.get("id", 0),
                title=m.get("title", ""),
                original_title=m.get("original_title", ""),
                release_date=m.get("release_date", ""),
                aliases=aliases,
            ))

        # 2. 获取待看剧集
        logger.info("[EXPORT] 开始获取待看剧集...")
        raw_tv = client.fetch_all_watchlist_tv()
        logger.info("[EXPORT] 获取到 %d 部剧集", len(raw_tv))
        tv_items = []
        for i, t in enumerate(raw_tv):
            tmdb_id = t.get("id", 0)
            aliases = set()
            if include_aliases and tmdb_id:
                try:
                    alias_list = client.get_tv_aliases(tmdb_id)
                    aliases = set(alias_list)
                except Exception:
                    pass

            last_ep = LastEpisode()
            if include_details and tmdb_id:
                try:
                    details = client.get_tv_details(tmdb_id)
                    if details:
                        lea = details.get("last_episode_to_air")
                        if lea:
                            last_ep = LastEpisode(
                                episode_number=lea.get("episode_number", 0),
                                season_number=lea.get("season_number", 0),
                                name=lea.get("name", ""),
                                overview=lea.get("overview", ""),
                                air_date=lea.get("air_date", ""),
                                vote_average=lea.get("vote_average", 0.0),
                                runtime=lea.get("runtime", 0),
                            )
                except Exception:
                    pass

            # 进度日志（每50条）
            if (i + 1) % 50 == 0:
                logger.info("[EXPORT] 剧集进度: %d/%d", i + 1, len(raw_tv))

            tv_items.append(TmdbItem(
                media_type="tv",
                tmdb_id=tmdb_id,
                title=t.get("name", ""),
                original_title=t.get("original_name", ""),
                release_date=t.get("first_air_date", ""),
                aliases=aliases,
                total_seasons=t.get("number_of_seasons", 0),
                total_episodes=t.get("number_of_episodes", 0),
                last_episode=last_ep,
            ))

        # 3. 合并并导出 CSV
        all_items = movie_items + tv_items
        output_path = Path(__file__).parent.parent / "watchlist_export.csv"
        export_watchlist_csv(
            [
                MatchResult(
                    tmdb=item,
                    status="待看",
                    matched_media="",
                    score=0.0,
                    detail="",
                )
                for item in all_items
            ],
            str(output_path),
        )
        logger.info("[EXPORT] CSV 导出完成: %d 电影 + %d 剧集 = %d 条",
                    len(movie_items), len(tv_items), len(all_items))
        return jsonify({
            "message": "导出完成",
            "movie_count": len(movie_items),
            "tv_count": len(tv_items),
            "total": len(all_items),
            "file": str(output_path),
            "include_aliases": include_aliases,
            "include_details": include_details,
        })

    return app


# ============================================================
# CLI 模式
# ============================================================

def run_cli():
    """CLI 交互式测试"""
    parser = argparse.ArgumentParser(description="TMDB API 测试工具")
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=[
            "all", "validate", "account", "cache", "search",
            "watchlist", "alias", "detail",
        ],
        help="要执行的测试命令 (默认: all)",
    )
    parser.add_argument("--query", "-q", default="", help="搜索关键词")
    parser.add_argument("--id", "-i", type=int, default=0, help="TMDB ID")
    parser.add_argument("--type", "-t", default="tv", choices=["movie", "tv"], help="媒体类型")
    parser.add_argument("--page", "-p", type=int, default=1, help="页码")
    parser.add_argument("--clear-cache", action="store_true", help="清除缓存")
    parser.add_argument("--refresh-cache", action="store_true", help="强制刷新缓存")
    args = parser.parse_args()

    client = get_client()

    def _print_header(title: str):
        print(f"\n{'='*60}")
        print(f"  {title}")
        print(f"{'='*60}")

    # 清除缓存
    if args.clear_cache:
        _print_header("清除 account_id 缓存")
        if _CACHE_FILE.exists():
            _CACHE_FILE.unlink()
            print("✓ 缓存已清除")
        else:
            print("- 缓存文件不存在")
        return

    # 强制刷新缓存
    if args.refresh_cache:
        _print_header("强制刷新 account_id 缓存")
        old_id = client._account_id
        client._account_id = ""
        new_id = client.account_id
        print(f"  旧 ID: {old_id or '(无)'}")
        print(f"  新 ID: {new_id}")
        print(f"  是否变化: {'是' if old_id != new_id else '否'}")
        return

    cmd = args.command

    # ---- account_id ----
    if cmd in ("all", "account", "validate"):
        _print_header("account_id")
        print(f"  account_id: {client.account_id}")
        print(f"  缓存文件: {_CACHE_FILE}")
        if _CACHE_FILE.exists():
            import json
            with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
            ts = cached.get("ts", 0)
            age_min = round((time.time() - ts) / 60, 1) if ts else 0
            print(f"  缓存时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts)) if ts else 'unknown'}")
            print(f"  缓存年龄: {age_min} 分钟")
        else:
            print("  缓存: 未缓存")

    # ---- validate ----
    if cmd in ("all", "validate"):
        _print_header("验证 access_token")
        ok = client.validate_key()
        print(f"  验证结果: {'✓ 有效' if ok else '✗ 无效'}")

    # ---- cache status ----
    if cmd in ("all", "cache"):
        _print_header("缓存状态")
        if _CACHE_FILE.exists():
            import json
            with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
            ts = cached.get("ts", 0)
            age_min = round((time.time() - ts) / 60, 1) if ts else 0
            ttl_min = round(7 * 24 * 60 - age_min, 1)
            print(f"  account_id: {cached.get('account_id', '')}")
            print(f"  缓存时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts)) if ts else 'unknown'}")
            print(f"  已过时间: {age_min} 分钟")
            print(f"  剩余有效: {ttl_min} 分钟")
        else:
            print("  无缓存文件")

    # ---- search ----
    if cmd in ("all", "search"):
        query = args.query or "Chronicle"
        _print_header(f"搜索电影: {query}")
        movies = client.search_movie(query, page=args.page)
        for m in movies[:5]:
            print(f"  [{m.get('id')}] {m.get('title')} ({m.get('release_date', '?')})")
        if len(movies) > 5:
            print(f"  ... 共 {len(movies)} 条")

        _print_header(f"搜索电视剧: {args.query or 'Breaking Bad'}")
        tvs = client.search_tv(args.query or "Breaking Bad", page=args.page)
        for t in tvs[:5]:
            print(f"  [{t.get('id')}] {t.get('name')} ({t.get('first_air_date', '?')})")
        if len(tvs) > 5:
            print(f"  ... 共 {len(tvs)} 条")

    # ---- watchlist ----
    if cmd in ("all", "watchlist"):
        _print_header("待看电影（第1页）")
        movies, has_next = client.get_watchlist_movies(page=args.page)
        print(f"  数量: {len(movies)}, 有下一页: {has_next}")
        for m in movies[:10]:
            print(f"  [{m.get('id')}] {m.get('title')} ({m.get('release_date', '?')})")
        if len(movies) > 10:
            print(f"  ... 共 {len(movies)} 条")

        _print_header("待看剧集（第1页）")
        tvs, has_next = client.get_watchlist_tv(page=args.page)
        print(f"  数量: {len(tvs)}, 有下一页: {has_next}")
        for t in tvs[:10]:
            print(f"  [{t.get('id')}] {t.get('name')} ({t.get('first_air_date', '?')})")
        if len(tvs) > 10:
            print(f"  ... 共 {len(tvs)} 条")

    # ---- alias ----
    if cmd in ("all", "alias"):
        if args.id:
            _print_header(f"别名 [{args.type}] ID={args.id}")
            if args.type == "movie":
                aliases = client.get_movie_aliases(args.id)
            else:
                aliases = client.get_tv_aliases(args.id)
            print(f"  别名数量: {len(aliases)}")
            for a in aliases[:15]:
                print(f"  - {a}")
        elif cmd == "alias":
            print("  请使用 --id <tmdb_id> 指定 TMDB ID")

    # ---- detail ----
    if cmd in ("all", "detail"):
        if args.id:
            _print_header(f"详情 [{args.type}] ID={args.id}")
            if args.type == "movie":
                data = client.get_movie_details(args.id)
                if data:
                    print(f"  标题: {data.get('title')}")
                    print(f"  原名: {data.get('original_title')}")
                    print(f"  上映: {data.get('release_date')}")
                    print(f"  评分: {data.get('vote_average')}")
                else:
                    print("  未找到")
            else:
                data = client.get_tv_details(args.id)
                if data:
                    print(f"  名称: {data.get('name')}")
                    print(f"  原名: {data.get('original_name')}")
                    print(f"  首播: {data.get('first_air_date')}")
                    print(f"  季数: {data.get('number_of_seasons')}")
                    print(f"  集数: {data.get('number_of_episodes')}")
                    print(f"  状态: {data.get('status')}")
                    last_ep = data.get("last_episode_to_air")
                    if last_ep:
                        print(f"  最新集: S{last_ep.get('season_number', 0):02d}E{last_ep.get('episode_number', 0):02d} - {last_ep.get('name', '')}")
                else:
                    print("  未找到")
        elif cmd == "detail":
            print("  请使用 --id <tmdb_id> 指定 TMDB ID")

    print(f"\n{'='*60}")
    print("  测试完成")
    print(f"{'='*60}\n")


# ============================================================
# Flask HTTP 模式
# ============================================================

def run_flask(host: str = "127.0.0.1", port: int = 5002):
    """启动 Flask 测试服务"""
    app = create_flask_app()
    logger.info("[STARTUP] 启动 Flask 应用，端口 %d（已关闭自动重载）", port)
    app.run(host=host, port=port, debug=True, use_reloader=False)


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    if "--flask" in sys.argv:
        # Flask 模式: python test_tmdb_api.py --flask [--port 5002]
        sys.argv.remove("--flask")
        port = 5002
        if "--port" in sys.argv:
            idx = sys.argv.index("--port")
            port = int(sys.argv[idx + 1])
            sys.argv.pop(idx + 1)
            sys.argv.pop(idx)
        run_flask(port=port)
    else:
        # CLI 模式: python test_tmdb_api.py [command] [options]
        run_cli()