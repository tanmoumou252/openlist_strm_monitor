"""
TMDB API v3 独立客户端模块

功能：
  - 使用 access_token (Bearer) 认证
  - 自动获取并缓存 account_id 到本地文件
  - 提供搜索、详情、待看列表、别名等方法
  - 支持 HTTP/HTTPS 代理

缓存：
  account_id 会缓存到 src/.tmdb_account.json，格式与 .admin_token.json 一致。
  后续请求直接复用缓存的 account_id，无需重复调用 /3/account。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
import urllib.parse
from pathlib import Path

# ============================================================
# 常量
# ============================================================

BASE_URL = "https://api.themoviedb.org"

# 缓存文件路径：与 src/.admin_token.json 同级
# 本地缓存 PII（account_id），通过 .gitignore + 0600 权限保护
_CACHE_DIR = Path(__file__).parent
_CACHE_FILE = _CACHE_DIR / ".tmdb_account.json"

# 缓存有效期（秒）：7 天
_CACHE_TTL = 7 * 24 * 3600

# ============================================================
# TMDB 客户端
# ============================================================

class TmdbClient:
    """TMDB API v3 客户端（同时支持 Bearer token / api_key 两种认证方式）

    认证优先级：
    1. access_token — Bearer Token，完整功能（含 watchlist）
    2. api_key — 查询参数认证，仅搜索/详情/别名等公开 API
    3. 两者都为空 — 所有 API 调用返回 None
    """

    def __init__(
        self,
        access_token: str = "",
        language: str = "zh-CN",
        proxy: str | None = None,
        host: str = "",
        api_key: str = "",
    ) -> None:
        self.access_token = access_token
        self.language = language
        self.proxy = proxy
        self.host = host.rstrip("/")
        self.api_key = api_key
        self._use_api_key_auth = bool(not access_token and api_key)
        # _base_url: API 和图片/详情页的基准 URL（host 为空时回退官方多域名）
        self._base_url = self.host if self.host else BASE_URL
        self._account_id: str = ""
        self._username: str = ""
        self._avatar_path: str = ""
        if self.access_token:
            self._load_cached_account_id()

    # ----------------------------------------------------------
    # account_id 缓存
    # ----------------------------------------------------------
    # 文件格式为简单 JSON，便于调试和手动检查；安全由 LAN 环境+文件权限保证。
    def _load_cached_account_id(self) -> None:
        """从本地缓存文件加载 account_id"""
        if not _CACHE_FILE.exists():
            return
        try:
            with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 检查缓存是否过期
            cached_ts = data.get("ts", 0)
            if cached_ts and (time.time() - cached_ts) > _CACHE_TTL:
                logging.info("[TMDB] 缓存 account_id 已过期，将重新获取")
                return
            self._account_id = str(data.get("account_id", ""))
            self._username = str(data.get("username", ""))
            self._avatar_path = str(data.get("avatar_path", ""))
            if self._account_id:
                logging.info(
                    "[TMDB] 从缓存加载 account_id: %s", self._account_id
                )
        except Exception as e:
            logging.warning("[TMDB] 读取缓存文件失败: %s", e)
    def _save_cached_account_id(self) -> None:
        """保存 account_id 到本地缓存文件"""
        try:
            import os
            data = {
                "account_id": self._account_id,
                "username": self._username,
                "avatar_path": self._avatar_path,
                "ts": time.time(),
            }
            # 创建文件时设置权限为 0o600（仅所有者可读写）
            fd = os.open(_CACHE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logging.info("[TMDB] account_id 已缓存到 %s", _CACHE_FILE)
        except Exception as e:
            logging.warning("[TMDB] 保存缓存文件失败: %s", e)

    @property
    def account_id(self) -> str:
        """获取 account_id，若未缓存则自动调用 API 获取。
        api_key-only 模式返回空字符串（不报错）。"""
        if not self._account_id:
            if self.access_token:
                self.fetch_account_id()
        return self._account_id

    @property
    def username(self) -> str:
        """获取用户名，若未缓存则自动调用 API 获取。
        api_key-only 模式返回空字符串。"""
        if not self._username:
            if self.access_token:
                self.fetch_account_id()
        return self._username

    @property
    def avatar_path(self) -> str:
        """获取头像路径，若未缓存则自动调用 API 获取。
        api_key-only 模式返回空字符串。"""
        if not self._avatar_path:
            if self.access_token:
                self.fetch_account_id()
        return self._avatar_path

    def fetch_account_id(self) -> str:
        """通过 /3/account 获取 account_id 并缓存到本地文件。
        api_key-only 时静默跳过（需要 Bearer Token）。"""
        if not self.access_token:
            return ""
        data = self.request("/3/account")
        if data and "id" in data:
            self._account_id = str(data["id"])
            self._username = str(data.get("username", ""))
            avatar_data = data.get("avatar", {})
            if isinstance(avatar_data, dict):
                # 优先取 TMDB 头像路径
                tmdb_avatar = avatar_data.get("tmdb", {})
                if isinstance(tmdb_avatar, dict) and tmdb_avatar.get(
                        "avatar_path"):
                    self._avatar_path = str(tmdb_avatar["avatar_path"])
                else:
                    gravatar = avatar_data.get("gravatar", {})
                    self._avatar_path = str(
                        gravatar.get("hash", "")
                    ) if isinstance(gravatar, dict) else ""
            self._save_cached_account_id()
            logging.info(
                "[TMDB] 获取 account_id 成功: %s (username: %s)",
                self._account_id, self._username,
            )
            return self._account_id
        logging.warning("[TMDB] 获取 account_id 失败: %s", data)
        return ""

    # ----------------------------------------------------------
    # URL 辅助（供 WebUI 等其他模块拼接图片/详情链接）
    # ----------------------------------------------------------

    def image_base(self) -> str:
        """
        返回图片基础 URL（如 https://image.tmdb.org/t/p）。
        配置了自定义 host 时返回 {host}/t/p，否则返回官方地址。
        """
        if self.host:
            return f"{self._base_url}/t/p"
        return "https://image.tmdb.org/t/p"

    def web_base(self) -> str:
        """
        返回 TMDB 网页基础 URL（如 https://www.themoviedb.org）。
        配置了自定义 host 时返回 host 本身，否则返回官方地址。
        """
        if self.host:
            return self._base_url
        return "https://www.themoviedb.org"

    # ----------------------------------------------------------
    # 通用请求
    # ----------------------------------------------------------

    def request(
        self, endpoint: str, params: dict | None = None,
        retries: int = 3, backoff: float = 1.0
    ) -> dict | None:
        """发送 GET 请求，支持自动重试和指数退避。

        认证方式（按优先级）：
        1. access_token — Bearer Token
        2. api_key — 查询参数 ?api_key=xxx
        3. 两者都为空 — 返回 None

        重试策略：
        - 429 速率限制：使用 Retry-After 头或指数退避
        - 网络错误：指数退避重试
        - 其他 HTTP 错误：不重试
        """
        if not self.access_token and not self.api_key:
            logging.warning("[TMDB] 未配置 access_token 或 api_key")
            return None

        url = f"{self._base_url}{endpoint}"
        if params is None:
            params = {}
        # 自动注入 language（除非显式传了空字符串跳过）
        if "language" not in params:
            params["language"] = self.language

        # api_key-only 时通过查询参数认证
        if self._use_api_key_auth:
            params["api_key"] = self.api_key

        query = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )
        full_url = f"{url}?{query}" if query else url

        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        # Bearer token 认证（优先级最高）
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        # 反代鉴权头（两种认证方式都支持）
        if self.host:
            proxy_key = self.api_key
            if proxy_key:
                headers["X-API-Key"] = proxy_key

        # 构建 opener（含代理）
        if self.proxy:
            proxy_handler = urllib.request.ProxyHandler(
                {"http": self.proxy, "https": self.proxy}
            )
            opener = urllib.request.build_opener(proxy_handler)
        else:
            opener = urllib.request.build_opener()

        # 重试循环
        for attempt in range(retries):
            try:
                req = urllib.request.Request(
                    full_url, headers=headers, method="GET")
                with opener.open(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                # 429 速率限制：重试
                if e.code == 429 and attempt < retries - 1:
                    default_wait = backoff * (2 ** attempt)
                    try:
                        retry_after = float(e.headers.get("Retry-After", default_wait))
                    except (ValueError, TypeError):
                        # T14: RFC 允许 Retry-After 为 HTTP-date（如 "Wed, 21 Oct 2015 07:28:00 GMT"），
                        # float() 会抛 ValueError 逃逸重试逻辑；回退默认指数退避
                        retry_after = default_wait
                    # T14: 等待秒数设上限并拒绝负值，防止异常/恶意 Retry-After 无限挂起线程
                    if retry_after < 0:
                        retry_after = default_wait
                    elif retry_after > 60.0:
                        retry_after = 60.0
                    logging.warning("[TMDB] 速率限制，等待 %.1f 秒后重试 (%d/%d)",
                                   retry_after, attempt + 1, retries)
                    time.sleep(retry_after)
                    continue
                # 其他 HTTP 错误：不重试
                logging.error("[TMDB] 请求失败 %s: HTTP %d", endpoint, e.code)
                return None
            except Exception as e:
                # 网络错误：重试
                if attempt < retries - 1:
                    wait = backoff * (2 ** attempt)
                    logging.warning("[TMDB] 请求失败，%.1f 秒后重试 (%d/%d): %s",
                                   wait, attempt + 1, retries, e)
                    time.sleep(wait)
                    continue
                # 最终失败
                logging.error("[TMDB] 请求失败 %s: %s", endpoint, e)
                return None

        return None

    # ----------------------------------------------------------
    # 认证 / 验证
    # ----------------------------------------------------------

    def validate_key(self) -> bool:
        """验证认证凭据是否有效（同时支持 access_token 和 api_key）"""
        data = self.request("/3/authentication")
        if data and data.get("success"):
            source = "api_key" if self._use_api_key_auth else "access_token"
            logging.info("[TMDB] %s 验证成功", source)
            return True
        logging.warning("[TMDB] 认证凭据验证失败: %s", data)
        return False

    # ----------------------------------------------------------
    # 搜索
    # ----------------------------------------------------------

    def search_movie(self, title: str, page: int = 1) -> list[dict]:
        """搜索电影"""
        data = self.request(
            "/3/search/movie", {"query": title, "page": str(page)}
        )
        return data.get("results", []) if data else []

    def search_tv(self, name: str, page: int = 1) -> list[dict]:
        """搜索电视剧"""
        data = self.request(
            "/3/search/tv", {"query": name, "page": str(page)}
        )
        return data.get("results", []) if data else []

    # ----------------------------------------------------------
    # 详情
    # ----------------------------------------------------------

    def get_movie_details(self, tmdb_id: int) -> dict | None:
        """获取电影详情（含 external_ids / translations / credits）"""
        return self.request(
            f"/3/movie/{tmdb_id}",
            {"append_to_response": "external_ids,translations,credits"},
        )

    def get_tv_details(self, tmdb_id: int) -> dict | None:
        """获取剧集详情（含 last_episode_to_air 等完整信息）"""
        return self.request(
            f"/3/tv/{tmdb_id}",
            {"append_to_response": "external_ids,translations,credits,images"},
        )

    def get_tv_seasons_info(self, tmdb_id: int) -> int:
        """
        获取剧集的有效季数（排除 S0 特别篇）。
        调用 /3/tv/{id}（不带 append_to_response，轻量请求）。
        """
        data = self.request(f"/3/tv/{tmdb_id}")
        if not data:
            return 0
        seasons = data.get("seasons", [])
        return sum(1 for s in seasons if s.get("season_number", 0) > 0)

    # ----------------------------------------------------------
    # 别名 / Alternative Titles
    # ----------------------------------------------------------

    def get_movie_aliases(self, tmdb_id: int) -> list[str]:
        """获取电影别名列表"""
        data = self.request(f"/3/movie/{tmdb_id}/alternative_titles")
        if not data:
            return []
        return [x.get("title", "")
                for x in data.get("titles", []) if x.get("title")]

    def get_tv_aliases(self, tmdb_id: int) -> list[str]:
        """获取剧集别名列表"""
        data = self.request(f"/3/tv/{tmdb_id}/alternative_titles")
        if not data:
            return []
        return [x.get("title", "")
                for x in data.get("results", []) if x.get("title")]

    # ----------------------------------------------------------
    # 待看列表（Watchlist）
    # ----------------------------------------------------------

    def get_watchlist_movies(self, page: int = 1) -> tuple[list[dict], bool]:
        """
        获取待看电影列表（分页）。
        api_key-only 模式返回空列表。
        返回 (items, has_next_page)

        T5: 取回失败（api_key 模式、无 account_id、响应缺 results、请求异常）
        一律 raise，不再返回 ([], False) 与"清单真为空"同形——否则 sync() 会据此
        全清本地表。
        """
        if self._use_api_key_auth:
            raise RuntimeError("api_key 模式不支持 watchlist")
        aid = self.account_id
        if not aid:
            raise RuntimeError("未配置 account_id，无法拉取 watchlist")
        try:
            data = self.request(
                f"/3/account/{aid}/watchlist/movies",
                {"page": str(page)},
            )
            if not data or "results" not in data:
                raise RuntimeError("watchlist 响应缺少 results")
            items = data["results"]
            has_next = data.get("page", 1) < data.get("total_pages", 1)
            return items, has_next
        except Exception as e:
            logging.error("[TMDB] 获取电影待看列表请求失败: %s", e)
            if isinstance(e, RuntimeError):
                raise
            raise RuntimeError(f"获取电影待看列表请求失败: {e}") from e

    def get_watchlist_tv(self, page: int = 1) -> tuple[list[dict], bool]:
        """
        获取待看剧集列表（分页）。
        api_key-only 模式返回空列表。
        返回 (items, has_next_page)

        T5: 同 get_watchlist_movies，取回失败一律 raise。
        """
        if self._use_api_key_auth:
            raise RuntimeError("api_key 模式不支持 watchlist")
        aid = self.account_id
        if not aid:
            raise RuntimeError("未配置 account_id，无法拉取 watchlist")
        try:
            data = self.request(
                f"/3/account/{aid}/watchlist/tv",
                {"page": str(page)},
            )
            if not data or "results" not in data:
                raise RuntimeError("watchlist 响应缺少 results")
            items = data["results"]
            has_next = data.get("page", 1) < data.get("total_pages", 1)
            return items, has_next
        except Exception as e:
            logging.error("[TMDB] 获取剧集待看列表请求失败: %s", e)
            if isinstance(e, RuntimeError):
                raise
            raise RuntimeError(f"获取剧集待看列表请求失败: {e}") from e

    def fetch_all_watchlist_movies(self) -> list[dict]:
        """获取全部待看电影"""
        all_items: list[dict] = []
        page = 1
        while True:
            items, has_next = self.get_watchlist_movies(page)
            if not items:
                break
            all_items.extend(items)
            if not has_next:
                break
            page += 1
            time.sleep(0.3)
        return all_items

    def fetch_all_watchlist_tv(self) -> list[dict]:
        """获取全部待看剧集"""
        all_items: list[dict] = []
        page = 1
        while True:
            items, has_next = self.get_watchlist_tv(page)
            if not items:
                break
            all_items.extend(items)
            if not has_next:
                break
            page += 1
            time.sleep(0.3)
        return all_items

# ============================================================
# 便捷工厂函数
# ============================================================

def create_tmdb_client(
    access_token: str = "",
    language: str = "zh-CN",
    proxy: str | None = None,
    host: str = "",
    api_key: str = "",
    auto_validate: bool = False,
) -> TmdbClient:
    """
    创建 TmdbClient 实例。

    Args:
        access_token: TMDB v3 access_token (Bearer)（可选，有则使用 Bearer 认证）
        language: 语言代码，默认 zh-CN
        proxy: HTTP 代理地址，如 http://127.0.0.1:7890
        host: 自定义 TMDB 反代基础地址，留空使用官方地址
        api_key: TMDB API Key (v3)（可选，无 access_token 时通过 ?api_key=xxx 认证）
        auto_validate: 是否在创建时自动验证凭据

    Returns:
        TmdbClient 实例
    """
    client = TmdbClient(
        access_token=access_token,
        language=language,
        proxy=proxy,
        host=host,
        api_key=api_key,
    )
    if auto_validate:
        if not client.validate_key():
            raise RuntimeError("TMDB 认证凭据验证失败")
    return client
