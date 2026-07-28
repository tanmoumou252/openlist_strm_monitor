# autopep8: off
# isort: off

from __future__ import annotations
from dataclasses import dataclass, field
import logging
import os
import json
import hashlib
from pathlib import Path


def normalize_local_root(path: str | Path) -> Path:
    """返回用于 mapping 归属判断的规范化本地根路径。"""
    # 保留 resolved 的实际大小写用于文件系统和 DB 路径；Windows 大小写
    # 等价性由 Path/比较方处理，mapping_id 单独使用 normcase。
    return Path(path).expanduser().resolve()


try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

# autopep8: on
# isort: on

# Bootstrap: ensure src/ is first in sys.path
from utils.bootstrap import ensure_base_dir_first

ensure_base_dir_first()


LINEAGE_VERSION = 1


def mapping_version(a_b_mappings: list["ABMapping"], c_root: str | Path) -> str:
    """根据规范化 mapping 集合和全局 C 根生成稳定版本摘要。"""
    payload = [
        {
            "mapping_id": str(m.mapping_id).strip(),
            "a_root": str(normalize_local_root(m.a_root)),
            "b_root": str(normalize_local_root(m.b_root)),
        }
        for m in a_b_mappings
    ]
    payload.sort(key=lambda item: item["mapping_id"])
    canonical = json.dumps(
        {"mappings": payload, "c_root": str(normalize_local_root(c_root))},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ABMapping:
    """A 区根目录到 B 区根目录的映射关系"""
    mapping_id: str       # 稳定短标识（A 根规范化路径 SHA1 前 8 位），B 根变更不改变 mapping_id
    a_root: str           # A 根路径，只读，从引擎 API 自动获取
    b_root: str           # B 根路径，用户在 UI 填写
    label: str = ""       # 可读标签，用于 C 区子目录和 WebUI 显示

    @staticmethod
    def generate_mapping_id(a_root: str) -> str:
        """由 A 根规范化路径生成稳定 mapping_id"""
        normalized = str(normalize_local_root(a_root))
        return hashlib.sha1(normalized.encode()).hexdigest()[:8]


def read_line_list(
    file_path: str, base_dir: str | Path, is_webdav: bool = False
) -> list[str]:
    full_path = Path(base_dir) / file_path
    if not full_path.exists():
        return []
    with open(full_path, "r", encoding="utf-8") as f:
        lines = [
            line.strip()
            for line in f
            if line.strip() and not line.strip().startswith("#")
        ]
    if is_webdav:
        return [line.rstrip("/") for line in lines]
    return lines


@dataclass(slots=True)
class WebDAVConfig:
    host: str
    user: str
    password: str
    totp_secret: str


@dataclass(slots=True)
class RefreshConfig:
    interval_seconds: int
    enabled: bool = True
    depth: int = 5
    timeout_seconds: int = 300  # 刷新操作超时时间（秒）
    log_level: str = "INFO"  # 刷新日志级别：DEBUG/INFO/WARNING


@dataclass(slots=True)
class BehaviorConfig:
    sync_on_startup: bool
    sync_on_startup_wait: int
    trash_dir_name: str = "trash"
    action: str = "MOVE"
    ghost_protect_seconds: int = 300
    a_to_b_restore_delay_seconds: int = 30


@dataclass(slots=True)
class LogConfig:
    level: str
    max_size_mb: int
    backup_count: int
    file: str = "strm_bridge.log"


@dataclass(slots=True)
class WebUIConfig:
    enabled: bool = True
    port: int = 8579
    bind: str = "0.0.0.0"


@dataclass(slots=True)
class TmdbProxyConfig:
    """TMDB 代理配置"""
    enabled: bool = False
    http: str = ""
    https: str = ""


@dataclass(slots=True)
class TmdbConfig:
    """TMDB 待看列表配置"""
    access_token: str = ""
    language: str = "zh-CN"
    host: str = ""
    api_key: str = ""
    csv_watchlist_file: str = ""
    watchlist_db: str = ""
    watchlist_cache_ttl: float = 604800  # 默认 7 天
    fuzzy_threshold: float = 0.60
    anime_min_ep_ratio: float = 0.3
    anime_max_season_diff: float = 0.3  # 新增：动漫最大季度差异阈值
    anime_min_season_ratio: float = 0.3  # 新增：动漫最少季数比例阈值
    proxy: TmdbProxyConfig = field(default_factory=TmdbProxyConfig)
    # 扁平化代理字段（供前端/测试 WebUI 直接读写，与嵌套 proxy 双向同步）
    proxy_enabled: bool = False
    proxy_http: str = ""


@dataclass(slots=True)
class LocalConfig:
    base_dir: str
    a_dir: str
    b_dir: str
    c_dir: str
    db_file: str = "./bridge.db"


@dataclass(slots=True)
class PathsConfig:
    strm_engine_paths: list[str]
    refresh_paths: list[str]
    b_root: str = ""
    c_root: str = ""


@dataclass
class StrmStorageMapping:
    """STRM 存储路径映射"""

    mount_path: str  # /strm
    paths: list[str]  # [/天翼云盘家庭云30GB/番剧, ...]
    local_path: str  # C:\box\strm

    @property
    def engine_entry_paths(self) -> list[str]:
        """STRM 引擎入口路径列表"""
        result = []
        for p in self.paths:
            last_dir = p.rstrip("/").split("/")[-1]  # "番剧"
            result.append(f"{self.mount_path.rstrip('/')}/{last_dir}")
        return result

    def get_engine_entry_path(self, sub_path: str = "") -> str:
        """获取 STRM 引擎入口路径"""
        base = (
            self.engine_entry_paths[0] if self.engine_entry_paths else self.mount_path
        )
        if sub_path:
            return f"{base}/{sub_path.lstrip('/')}"
        return base

    def get_cloud_path(self, sub_path: str = "") -> str:
        """获取实际云盘路径"""
        base = self.paths[0] if self.paths else ""
        if sub_path:
            return f"{base}/{sub_path.lstrip('/')}"
        return base

    def get_local_path(self, sub_path: str = "") -> str:
        """获取本地 A 区路径"""
        if not self.local_path:
            return ""
        last_dir = ""
        if self.paths:
            last_dir = self.paths[0].rstrip("/").split("/")[-1]
        base = os.path.join(self.local_path,
                            last_dir) if last_dir else self.local_path
        if sub_path:
            return os.path.join(base, sub_path.lstrip("/\\"))
        return base


@dataclass(slots=True)
class AppConfig:
    base_dir: str
    webdav: WebDAVConfig
    refresh: RefreshConfig
    behavior: BehaviorConfig
    log: LogConfig
    local: LocalConfig
    paths: PathsConfig
    webui: WebUIConfig = field(default_factory=WebUIConfig)
    tmdb: TmdbConfig = field(default_factory=TmdbConfig)
    a_folders: list[str] = field(default_factory=list)
    # 多 A↔多 B 根映射（每个 A 根绑定唯一 B 根）
    a_b_mappings: list[ABMapping] = field(default_factory=list)
    # STRM 存储映射 mount_path -> StrmStorageMapping
    strm_storage_map: dict[str, StrmStorageMapping] = field(
        default_factory=dict)
    # OpenList 配置（从 DB 加载）
    openlist_strm_engines: list[dict] = field(default_factory=list)
    openlist_refresh_paths: list[str] = field(default_factory=list)
    engines_initialized: bool = field(default=False)

    def __getattr__(self, name: str):
        if name == "strm_engine_paths":
            return self.paths.strm_engine_paths
        if name == "refresh_paths":
            return self.paths.refresh_paths
        raise AttributeError(
            f"'{self.__class__.__name__}' object has no attribute '{name}'"
        )

    def update_from_db(self, watchlist_db) -> None:
        """从 DB 的 webui_config 表加载 OpenList 配置覆盖。

        优先级：DB > config.toml
        使用映射表简化重复代码，避免大量 if-statement。
        """
        if not watchlist_db:
            return
        try:
            db_cfg = watchlist_db.get_all_config("openlist")
            if not db_cfg:
                return

            # 配置映射：(DB key, 目标对象, 属性名, 转换函数)
            config_mappings = [
                # WebDAV 配置
                ("webdav_host", self.webdav, "host", str),
                ("webdav_user", self.webdav, "user", str),
                ("webdav_password", self.webdav, "password", str),
                ("webdav_totp_secret", self.webdav, "totp_secret", str),

                # 路径配置
                ("b_root", self.paths, "b_root", str),
                ("c_root", self.paths, "c_root", str),

                # 行为配置
                ("behavior_action", self.behavior, "action", str),
                ("behavior_trash_dir_name", self.behavior, "trash_dir_name", str),
                ("behavior_ghost_protect_seconds", self.behavior, "ghost_protect_seconds", int),
                ("behavior_a_to_b_restore_delay_seconds", self.behavior, "a_to_b_restore_delay_seconds", int),
                ("behavior_sync_on_startup", self.behavior, "sync_on_startup", self._to_bool),
                ("behavior_sync_on_startup_wait", self.behavior, "sync_on_startup_wait", int),

                # 刷新配置
                ("refresh_log_level", self.refresh, "log_level", str),

                # 日志配置
                ("log_level", self.log, "level", str),
                ("log_max_size_mb", self.log, "max_size_mb", int),
                ("log_backup_count", self.log, "backup_count", int),
                # 日志保存路径（DB 往返的关键键，勿删；留空时回退 strm_bridge.log）
                ("log_file", self.log, "file", str),
            ]

            # 应用简单配置
            for db_key, target, attr, converter in config_mappings:
                if db_key in db_cfg:
                    try:
                        value = converter(db_cfg[db_key])
                        setattr(target, attr, value)
                    except (ValueError, TypeError) as e:
                        logging.warning("[Config] 转换 %s 失败: %s", db_key, e)

            # log_file 确保为绝对路径：DB 中可能存的是相对路径（如旧迁移数据
            # "strm_bridge.log"），此处拼接项目根目录，避免覆盖 from_file()
            # 生成的绝对路径
            if hasattr(self, 'log') and self.log.file:
                log_path = Path(self.log.file)
                if not log_path.is_absolute():
                    self.log.file = str((Path(self.local.base_dir) / log_path).resolve())

            # 特殊处理：b_root 和 c_root 需要同步到 local 配置
            if "b_root" in db_cfg:
                self.local.b_dir = db_cfg["b_root"]
            if "c_root" in db_cfg:
                self.local.c_dir = db_cfg["c_root"]

            # 刷新配置（需要特殊处理：分钟转秒）
            refresh_cfg = self.refresh
            if "refresh_enabled" in db_cfg:
                refresh_cfg.enabled = self._to_bool(db_cfg["refresh_enabled"])
            if "refresh_interval_minutes" in db_cfg:
                try:
                    refresh_cfg.interval_seconds = int(db_cfg["refresh_interval_minutes"]) * 60
                except (ValueError, TypeError) as e:
                    logging.warning("[Config] 转换 refresh_interval_minutes 失败: %s", e)
            if "refresh_depth" in db_cfg:
                try:
                    refresh_cfg.depth = int(db_cfg["refresh_depth"])
                except (ValueError, TypeError) as e:
                    logging.warning("[Config] 转换 refresh_depth 失败: %s", e)

            # OpenList 初始化标志
            if "engines_initialized" in db_cfg:
                self.engines_initialized = self._to_bool(db_cfg["engines_initialized"])

# JSON 数组字段
            if "strm_engines" in db_cfg:
                try:
                    self.openlist_strm_engines = json.loads(db_cfg["strm_engines"])
                    # 从 openlist_strm_engines 派生 strm_engine_paths
                    strm_engine_paths = []
                    for engine in self.openlist_strm_engines:
                        mount_path = engine.get("engine", "")
                        if mount_path and mount_path not in strm_engine_paths:
                            strm_engine_paths.append(mount_path)
                    self.paths.strm_engine_paths = strm_engine_paths
                except (json.JSONDecodeError, TypeError) as e:
                    logging.warning("[Config] 解析 strm_engines 失败: %s", e)
                    self.openlist_strm_engines = []

            if "refresh_paths" in db_cfg:
                try:
                    self.openlist_refresh_paths = json.loads(db_cfg["refresh_paths"])
                    self.paths.refresh_paths = self.openlist_refresh_paths
                except (json.JSONDecodeError, TypeError) as e:
                    logging.warning("[Config] 解析 refresh_paths 失败: %s", e)
                    self.openlist_refresh_paths = []

            # 从 strm_storage_map 派生 a_folders（仅限用户配置的引擎，
            # 与 load_strm_storage_from_api 的派生逻辑保持一致）
            configured_engines = set(
                e.get("engine", "").rstrip("/")
                for e in self.openlist_strm_engines
                if e.get("engine", "").strip()
            )
            a_folders = []
            for entry_path, mapping in self.strm_storage_map.items():
                if mapping.mount_path.rstrip("/") not in configured_engines:
                    continue
                if mapping.local_path and mapping.local_path not in a_folders:
                    a_folders.append(mapping.local_path)
            self.a_folders = a_folders

            # 读取 a_b_mappings（新配置）
            if "a_b_mappings" in db_cfg:
                try:
                    mappings_data = json.loads(db_cfg["a_b_mappings"])
                    self.a_b_mappings = [
                        ABMapping(
                            mapping_id=m.get("mapping_id", ""),
                            a_root=m.get("a_root", ""),
                            b_root=m.get("b_root", ""),
                            label=m.get("label", "")
                        )
                        for m in mappings_data
                        if m.get("a_root") and m.get("b_root")
                    ]
                except (json.JSONDecodeError, TypeError) as e:
                    logging.warning("[Config] 解析 a_b_mappings 失败: %s", e)
                    self.a_b_mappings = []

            logging.info("[Config] 已从 DB 加载 OpenList 配置 (%d 项)", len(db_cfg))
        except Exception as e:
            logging.warning("[Config] 从 DB 加载 OpenList 配置失败: %s", e)

    def _to_bool(self, value) -> bool:
        """转换为布尔值（支持字符串和布尔类型）"""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)

    @classmethod
    def from_file(cls, toml_path: str) -> "AppConfig":
        """从 config.toml 文件加载配置（纯文件解析，无网络调用）。

        STRM 存储映射需要后续调用 load_strm_storage_from_api() 显式加载。
        """
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)

        # toml_path 指向项目根目录下的 config.toml
        # base_dir 应该是项目根目录（用于查找 .txt 配置文件）
        base_dir = os.path.dirname(toml_path)

        local_data = data.get("local", {})
        paths_data = data.get("paths", {})
        b_root = paths_data.get("b_root", "")
        c_root = paths_data.get("c_root", "")

        # 仅在路径非空时检查是否为绝对路径
        if b_root and not Path(b_root).is_absolute():
            logging.warning("[Config] b_root 不是绝对路径: %s", b_root)
        if c_root and not Path(c_root).is_absolute():
            logging.warning("[Config] c_root 不是绝对路径: %s", c_root)

        local = LocalConfig(
            base_dir=base_dir,
            a_dir="",  # 默认为空，需在 WebUI 配置
            b_dir=b_root,
            c_dir=c_root,
            db_file=os.path.normpath(os.path.join(
                base_dir, local_data.get(
                    "db_file", "bridge.db"))),
        )

        webdav_data = data.get("webdav", {})
        webdav = WebDAVConfig(
            host=webdav_data.get("host", ""),
            user=webdav_data.get("user", ""),
            password=webdav_data.get("password", ""),
            totp_secret=webdav_data.get("totp_secret", ""),
        )

        refresh_data = data.get("refresh", {})
        refresh = RefreshConfig(
            interval_seconds=refresh_data.get("interval_minutes", 10) * 60,
            enabled=refresh_data.get("enabled", True),
            depth=refresh_data.get("depth", 5),
            timeout_seconds=refresh_data.get("timeout_seconds", 300),
            log_level=refresh_data.get("log_level", "INFO"),
        )

        behavior_data = data.get("behavior", {})
        behavior = BehaviorConfig(
            sync_on_startup=behavior_data.get("sync_on_startup", True),
            sync_on_startup_wait=behavior_data.get("sync_on_startup_wait", 0),
            trash_dir_name=behavior_data.get("trash_dir_name", "trash"),
            action=behavior_data.get("action", "MOVE"),
            ghost_protect_seconds=behavior_data.get(
                "ghost_protect_seconds", 300),
            a_to_b_restore_delay_seconds=behavior_data.get(
                "a_to_b_restore_delay_seconds", 30
            ),
        )

        log_data = data.get("log", {})
        log = LogConfig(
            level=log_data.get("level", "INFO"),
            max_size_mb=log_data.get("max_size_mb", 2),
            backup_count=log_data.get("backup_count", 5),
            file=os.path.normpath(os.path.join(base_dir, log_data.get("file", "strm_bridge.log"))),
        )

        paths = PathsConfig(
            strm_engine_paths=[],  # 从 API 或 DB 加载
            refresh_paths=[],  # 从 DB 加载（WebUI 配置）；迁移时由 migrate_config_to_db 读取 txt
            b_root=b_root,
            c_root=c_root,
        )

        # 解析 [webui] 配置
        webui_data = data.get("webui", {})
        webui = WebUIConfig(
            enabled=webui_data.get("enabled", True),
            port=int(webui_data.get("port", 8579)),
            bind=webui_data.get("bind", "0.0.0.0"),
        )

        # 解析 [tmdb] 配置 — 已废弃，TMDB 配置迁移至 DB (webui_config 表)
        # TmdbConfig 使用默认值初始化，运行时从 DB 加载覆盖
        tmdb = TmdbConfig()

        instance = cls.__new__(cls)
        instance.base_dir = base_dir
        instance.webdav = webdav
        instance.refresh = refresh
        instance.behavior = behavior
        instance.log = log
        instance.local = local
        instance.paths = paths
        instance.webui = webui
        instance.tmdb = tmdb

        # 初始化为空，后续由 load_strm_storage_from_api() 填充
        instance.a_folders = []
        instance.strm_storage_map = {}
        instance.openlist_strm_engines = []
        instance.openlist_refresh_paths = []

        return instance

    def load_strm_storage_from_api(self, admin_client=None) -> None:
        """从 OpenList API 加载 STRM 存储映射（需要网络）。

        Args:
            admin_client: 可选的已初始化 OpenListAdminClient。传入时直接复用，
                避免重复登录；不传时内部创建临时 client（保持向后兼容）。
                传入的 client 应已登录或可自动登录（token 缓存有效）。

        调用后，a_folders、strm_engine_paths、strm_monitored_paths 会被更新。
        """
        try:
            from webdav_client import OpenListAdminClient

            # 复用外部 client 避免重复登录；未传入时自建（向后兼容）
            owns_client = admin_client is None
            if owns_client:
                # 前置检查：host 未配置时直接给出明确提示，避免误报为"网络请求异常"
                if not self.webdav.host:
                    logging.info(
                        "[STRM存储API] OpenList 未配置（host 为空），跳过 STRM 存储映射加载。"
                        "请在 WebUI 配置页面填写 OpenList 连接信息。")
                    return
                admin_client = OpenListAdminClient(
                    host=self.webdav.host,
                    user=self.webdav.user,
                    password=self.webdav.password,
                    totp_secret=self.webdav.totp_secret,
                )
                # 强制重新登录，不使用缓存 token，确保真实验证连接
                if not admin_client.login(force=True):
                    logging.warning("[STRM存储API] 登录失败，跳过 STRM 存储映射加载")
                    return

            content = admin_client.get_strm_storages_full_info()
            if not content:
                err = admin_client.last_error_message or "未知"
                logging.warning(
                    "[STRM存储 API] 获取 STRM 存储信息失败（原因: %s）。"
                    "若 OpenList 尚未配置 STRM 引擎或连接不可达，此为正常现象。",
                    err,
                )
                return

            # strm_storage_map 加载全部引擎（供 UI 下拉框发现）；
            # a_folders / 扫描范围则严格只从用户配置的引擎派生（见下方）
            configured_engines = set(
                e.get("engine", "").rstrip("/")
                for e in self.openlist_strm_engines
                if e.get("engine", "").strip()
            )

            strm_storage_map: dict[str, StrmStorageMapping] = {}
            for storage in content:
                mount_path = storage.get("mount_path", "")
                addition_str = storage.get("addition", "{}")
                try:
                    addition = json.loads(addition_str)

                    # 兼容 paths 为字符串和列表的两种情况
                    paths_val = addition.get("paths", "")
                    if isinstance(paths_val, list):
                        storage_paths = [
                            str(p).strip() for p in paths_val if str(p).strip()
                        ]
                    else:
                        storage_paths = [
                            p.strip()
                            for p in paths_val.split("\n")
                            if p.strip()
                        ]

                    local_path = addition.get("SaveStrmLocalPath", "")

                    # 合并相同最后一级的 paths
                    path_groups: dict[str, list[str]] = {}
                    for p in storage_paths:
                        last_dir = p.rstrip("/").split("/")[-1]
                        if last_dir not in path_groups:
                            path_groups[last_dir] = []
                        path_groups[last_dir].append(p)

                    # 为每个 group 创建 StrmStorageMapping
                    for last_dir, group_paths in path_groups.items():
                        entry_path = f"{mount_path.rstrip('/')}/{last_dir}"
                        strm_storage_map[entry_path] = StrmStorageMapping(
                            mount_path=mount_path,
                            paths=group_paths,
                            local_path=local_path,
                        )
                except json.JSONDecodeError:
                    logging.warning(
                        "[STRM存储解析] 解析 addition 失败：%s",
                        addition_str[:200],
                    )

            # a_folders / 扫描范围严格只从用户配置的引擎派生；
            # strm_storage_map 仍保留全部引擎供 UI 下拉框发现。
            # engines_initialized 用于区分"首次运行"（不限制）与"用户已保存配置"：
            #   - 首次运行（未初始化）：configured_engines 为空 → a_folders 也为空，
            #     避免把 OpenList 端全部引擎错误注入扫描范围；
            #   - 用户已保存：仅保留用户显式配置的引擎。
            a_folders = []
            strm_engine_paths = []
            for entry_path, mapping in strm_storage_map.items():
                if mapping.mount_path.rstrip("/") not in configured_engines:
                    continue
                if mapping.local_path and mapping.local_path not in a_folders:
                    a_folders.append(mapping.local_path)
                if mapping.mount_path not in strm_engine_paths:
                    strm_engine_paths.append(mapping.mount_path)

            self.a_folders = a_folders
            self.paths.strm_engine_paths = strm_engine_paths
            self.strm_storage_map = strm_storage_map
            logging.info(
                "[STRM存储API] 成功加载 %d 个 STRM 存储映射，其中 %d 个为用户配置引擎",
                len(strm_storage_map), len(configured_engines))

        except Exception as exc:
            logging.warning("[STRM存储API] 获取 STRM 存储信息失败: %s", exc)


def migrate_config_to_db(config: "AppConfig", watchlist_db) -> bool:
    """将 config.toml 和 txt 文件中的配置迁移到 DB。

    检查 migration scope 的 config_toml_migrated key：
    - 如果已迁移，返回 False
    - 如果未迁移，从 config 读取旧配置写入 DB openlist scope，返回 True
    """
    if not watchlist_db:
        return False

    # 检查是否已迁移
    migrated = watchlist_db.get_config("migration", "config_toml_migrated", "")
    if migrated == "true":
        logging.debug("[Migration] OpenList 配置已迁移，跳过")
        return False

    logging.info("[Migration] 首次启动，正在将 config.toml 配置迁移到 DB...")

    try:
        # WebDAV 配置
        watchlist_db.set_config("openlist", "webdav_host", config.webdav.host)
        watchlist_db.set_config("openlist", "webdav_user", config.webdav.user)
        watchlist_db.set_config("openlist", "webdav_password", config.webdav.password)
        watchlist_db.set_config("openlist", "webdav_totp_secret", config.webdav.totp_secret)

        # 路径配置
        watchlist_db.set_config("openlist", "c_root", config.paths.c_root)

        # 刷新路径（JSON 数组）
        # 运行时 refresh_paths 已不再从 txt 文件加载（改为 WebUI/DB 配置）。
        # 但为兼容首次升级、尚未迁移的旧用户，迁移阶段仍尝试读取遗留的
        # refresh_paths.txt 作为一次性数据源写入 DB；文件不存在或为空则写 []。
        legacy_refresh_paths = config.paths.refresh_paths
        if not legacy_refresh_paths:
            legacy_file = os.path.join(
                config.local.base_dir, "refresh_paths.txt"
            )
            legacy_refresh_paths = read_line_list(
                os.path.basename(legacy_file),
                os.path.dirname(legacy_file),
                is_webdav=True,
            )
        watchlist_db.set_config("openlist", "refresh_paths",
                                json.dumps(legacy_refresh_paths, ensure_ascii=False))

        # STRM 引擎配置
        # ⚠️ a 区来源仅以 "用户在 WebUI 手动添加并保存的引擎" 为唯一真相来源
        # （DB 键 openlist.strm_engines，由 WebUI POST 写入）。
        # 迁移时【绝不】把 OpenList 端自动发现的引擎注入为"用户已配置"：
        # 即使 OpenList 存在 strm 引擎而用户没显式添加，也不进入 a_folders / b 区
        # / protected_roots。因此迁移阶段写入空数组 []，仅标记 engines_initialized
        # 表示"迁移已完成、等待用户在 WebUI 配置"。
        # 关联：load_strm_storage_from_api() 仅对 openlist_strm_engines 中的引擎
        # 派生 a_folders（configured_engines 过滤），本函数不应改写它。
        watchlist_db.set_config("openlist", "strm_engines",
                                json.dumps([], ensure_ascii=False))
        # 迁移时即标记 engines_initialized=true：表示配置迁移已完成，
        # 让 load_strm_storage_from_api 走"用户已保存（可能为空）"分支而非"首次运行"
        # 分支，避免后续误注入全部 OpenList 引擎。
        watchlist_db.set_config("openlist", "engines_initialized", "true")

        # 刷新配置
        watchlist_db.set_config("openlist", "refresh_enabled",
                                str(config.refresh.enabled).lower())
        watchlist_db.set_config("openlist", "refresh_interval_minutes",
                                str(config.refresh.interval_seconds // 60))
        watchlist_db.set_config("openlist", "refresh_depth",
                                str(config.refresh.depth))
        watchlist_db.set_config("openlist", "refresh_log_level",
                                config.refresh.log_level)

        # 行为配置
        watchlist_db.set_config("openlist", "behavior_action", config.behavior.action)
        watchlist_db.set_config("openlist", "behavior_trash_dir_name", config.behavior.trash_dir_name)
        watchlist_db.set_config("openlist", "behavior_ghost_protect_seconds",
                                str(config.behavior.ghost_protect_seconds))
        watchlist_db.set_config("openlist", "behavior_a_to_b_restore_delay_seconds",
                                str(config.behavior.a_to_b_restore_delay_seconds))
        watchlist_db.set_config("openlist", "behavior_sync_on_startup",
                                str(config.behavior.sync_on_startup).lower())
        watchlist_db.set_config("openlist", "behavior_sync_on_startup_wait",
                                str(config.behavior.sync_on_startup_wait))

        # 日志配置
        watchlist_db.set_config("openlist", "log_level", config.log.level)
        watchlist_db.set_config("openlist", "log_max_size_mb", str(config.log.max_size_mb))
        watchlist_db.set_config("openlist", "log_backup_count", str(config.log.backup_count))
        # 日志保存路径：迁移写入 DB，保证后续 update_from_db 能读回（勿删此键）
        watchlist_db.set_config("openlist", "log_file", config.log.file)

        # 标记已迁移
        watchlist_db.set_config("migration", "config_toml_migrated", "true")

        logging.info("[Migration] OpenList 配置已迁移到 DB (20 个键 + 1 个迁移标记)")
        return True
    except Exception as e:
        logging.error("[Migration] 配置迁移失败: %s", e, exc_info=True)
        return False
