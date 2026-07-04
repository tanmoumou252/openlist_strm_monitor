# autopep8: off
# isort: off

from __future__ import annotations
from dataclasses import dataclass, field
import logging
import os
import json
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

# autopep8: on
# isort: on

# Bootstrap: ensure src/ is first in sys.path
from utils.bootstrap import ensure_base_dir_first

ensure_base_dir_first()


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
    file: str = "./activity.log"


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
    strm_monitored_paths: list[str] = field(default_factory=list)
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
    # STRM 存储映射 mount_path -> StrmStorageMapping
    strm_storage_map: dict[str, StrmStorageMapping] = field(
        default_factory=dict)
    # OpenList 配置（从 DB 加载）
    openlist_strm_engines: list[dict] = field(default_factory=list)
    openlist_refresh_paths: list[str] = field(default_factory=list)

    def __getattr__(self, name: str):
        if name == "strm_engine_paths":
            return self.paths.strm_engine_paths
        if name == "refresh_paths":
            return self.paths.refresh_paths
        if name == "strm_monitored_paths":
            return self.paths.strm_monitored_paths
        raise AttributeError(
            f"'{self.__class__.__name__}' object has no attribute '{name}'"
        )

    def update_from_db(self, watchlist_db) -> None:
        """从 DB 的 webui_config 表加载 OpenList 配置覆盖。
        
        优先级：DB > config.toml
        """
        if not watchlist_db:
            return
        try:
            db_cfg = watchlist_db.get_all_config("openlist")
            if not db_cfg:
                return
            
            # WebDAV 配置
            webdav_cfg = self.webdav
            if "webdav_host" in db_cfg:
                webdav_cfg.host = db_cfg["webdav_host"]
            if "webdav_user" in db_cfg:
                webdav_cfg.user = db_cfg["webdav_user"]
            if "webdav_password" in db_cfg:
                webdav_cfg.password = db_cfg["webdav_password"]
            if "webdav_totp_secret" in db_cfg:
                webdav_cfg.totp_secret = db_cfg["webdav_totp_secret"]
            
            # 路径配置
            paths_cfg = self.paths
            if "b_root" in db_cfg:
                paths_cfg.b_root = db_cfg["b_root"]
                self.local.b_dir = db_cfg["b_root"]
            if "c_root" in db_cfg:
                paths_cfg.c_root = db_cfg["c_root"]
                self.local.c_dir = db_cfg["c_root"]
            
            # STRM 引擎配置（JSON 数组）
            if "strm_engines" in db_cfg:
                try:
                    self.openlist_strm_engines = json.loads(db_cfg["strm_engines"])
                    # 从 openlist_strm_engines 派生 strm_engine_paths 和 strm_monitored_paths
                    strm_engine_paths = []
                    strm_monitored_paths = []
                    for engine in self.openlist_strm_engines:
                        mount_path = engine.get("engine", "")
                        if mount_path and mount_path not in strm_engine_paths:
                            strm_engine_paths.append(mount_path)
                        for mp in engine.get("monitored_paths", []):
                            if mp not in strm_monitored_paths:
                                strm_monitored_paths.append(mp)
                    paths_cfg.strm_engine_paths = strm_engine_paths
                    paths_cfg.strm_monitored_paths = strm_monitored_paths
                except (json.JSONDecodeError, TypeError):
                    self.openlist_strm_engines = []
            
            # 刷新路径（JSON 数组）
            if "refresh_paths" in db_cfg:
                try:
                    self.openlist_refresh_paths = json.loads(db_cfg["refresh_paths"])
                    paths_cfg.refresh_paths = self.openlist_refresh_paths
                except (json.JSONDecodeError, TypeError):
                    self.openlist_refresh_paths = []
            
            # 从 strm_storage_map 派生 a_folders
            a_folders = []
            for entry_path, mapping in self.strm_storage_map.items():
                if mapping.local_path and mapping.local_path not in a_folders:
                    a_folders.append(mapping.local_path)
            self.a_folders = a_folders
            
            # 刷新配置
            refresh_cfg = self.refresh
            if "refresh_enabled" in db_cfg:
                refresh_cfg.enabled = str(db_cfg["refresh_enabled"]).lower() in ("true", "1", "yes")
            if "refresh_interval_minutes" in db_cfg:
                try:
                    refresh_cfg.interval_seconds = int(db_cfg["refresh_interval_minutes"]) * 60
                except (ValueError, TypeError):
                    pass
            if "refresh_depth" in db_cfg:
                try:
                    refresh_cfg.depth = int(db_cfg["refresh_depth"])
                except (ValueError, TypeError):
                    pass
            
            # 行为配置
            behavior_cfg = self.behavior
            if "behavior_action" in db_cfg:
                behavior_cfg.action = db_cfg["behavior_action"]
            if "behavior_trash_dir_name" in db_cfg:
                behavior_cfg.trash_dir_name = db_cfg["behavior_trash_dir_name"]
            if "behavior_ghost_protect_seconds" in db_cfg:
                try:
                    behavior_cfg.ghost_protect_seconds = int(db_cfg["behavior_ghost_protect_seconds"])
                except (ValueError, TypeError):
                    pass
            if "behavior_a_to_b_restore_delay_seconds" in db_cfg:
                try:
                    behavior_cfg.a_to_b_restore_delay_seconds = int(db_cfg["behavior_a_to_b_restore_delay_seconds"])
                except (ValueError, TypeError):
                    pass
            if "behavior_sync_on_startup" in db_cfg:
                behavior_cfg.sync_on_startup = str(db_cfg["behavior_sync_on_startup"]).lower() in ("true", "1", "yes")
            if "behavior_sync_on_startup_wait" in db_cfg:
                try:
                    behavior_cfg.sync_on_startup_wait = int(db_cfg["behavior_sync_on_startup_wait"])
                except (ValueError, TypeError):
                    pass
            
            # 日志配置
            log_cfg = self.log
            if "log_level" in db_cfg:
                log_cfg.level = db_cfg["log_level"]
            if "log_max_size_mb" in db_cfg:
                try:
                    log_cfg.max_size_mb = int(db_cfg["log_max_size_mb"])
                except (ValueError, TypeError):
                    pass
            if "log_backup_count" in db_cfg:
                try:
                    log_cfg.backup_count = int(db_cfg["log_backup_count"])
                except (ValueError, TypeError):
                    pass
            
            logging.info("[Config] 已从 DB 加载 OpenList 配置 (%d 项)", len(db_cfg))
        except Exception as e:
            logging.warning("[Config] 从 DB 加载 OpenList 配置失败: %s", e)

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
        b_root = paths_data.get("b_root", os.path.join(base_dir, "b"))
        c_root = paths_data.get("c_root", os.path.join(base_dir, "c"))

        local = LocalConfig(
            base_dir=base_dir,
            a_dir=os.path.join(base_dir, "a"),
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
            interval_seconds=refresh_data.get("interval_minutes", 5) * 60,
            enabled=refresh_data.get("enabled", True),
            depth=refresh_data.get("depth", 5),
        )

        behavior_data = data.get("behavior", {})
        behavior = BehaviorConfig(
            sync_on_startup=behavior_data.get("sync_on_startup", False),
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
            file=os.path.join(base_dir, log_data.get("file", "activity.log")),
        )

        paths = PathsConfig(
            strm_engine_paths=[],  # 从 API 或 DB 加载
            refresh_paths=read_line_list(
                paths_data.get("refresh_paths_file", "refresh_paths.txt"),
                base_dir,
                is_webdav=True,
            ),
            strm_monitored_paths=[],  # 从 API 或 DB 加载
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

    def load_strm_storage_from_api(self) -> None:
        """从 OpenList API 加载 STRM 存储映射（需要网络）。
        
        此方法会创建临时的 AdminClient 并登录，获取所有 STRM 存储的映射信息。
        调用后，a_folders、strm_engine_paths、strm_monitored_paths 会被更新。
        """
        try:
            from webdav_client import OpenListAdminClient

            admin_client = OpenListAdminClient(
                host=self.webdav.host,
                user=self.webdav.user,
                password=self.webdav.password,
                totp_secret=self.webdav.totp_secret,
            )
            if not admin_client.login():
                logging.warning("[STRM存储API] 登录失败，跳过 STRM 存储映射")
                return

            storages = admin_client.list_storages()
            if not storages or not isinstance(storages, dict):
                logging.warning("[STRM存储API] 获取存储列表失败")
                return

            data = storages.get("data", {})
            content = data.get("content", []) if isinstance(data, dict) else []

            strm_storage_map: dict[str, StrmStorageMapping] = {}
            for storage in content:
                if storage.get("driver", "").lower() != "strm":
                    continue
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
                        "[STRM存储解析] 解析 addition 失败: %s",
                        addition_str[:200],
                    )

            # 从 strm_storage_map 派生 a_folders、strm_engine_paths、strm_monitored_paths
            a_folders = []
            strm_engine_paths = []
            strm_monitored_paths = []
            for entry_path, mapping in strm_storage_map.items():
                if mapping.local_path and mapping.local_path not in a_folders:
                    a_folders.append(mapping.local_path)
                if mapping.mount_path not in strm_engine_paths:
                    strm_engine_paths.append(mapping.mount_path)
                for p in mapping.paths:
                    if p not in strm_monitored_paths:
                        strm_monitored_paths.append(p)

            self.a_folders = a_folders
            self.paths.strm_engine_paths = strm_engine_paths
            self.paths.strm_monitored_paths = strm_monitored_paths
            self.strm_storage_map = strm_storage_map
            logging.info("[STRM存储API] 成功加载 %d 个 STRM 存储映射", len(strm_storage_map))

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
        watchlist_db.set_config("openlist", "b_root", config.paths.b_root)
        watchlist_db.set_config("openlist", "c_root", config.paths.c_root)
        
        # 刷新路径（JSON 数组）
        watchlist_db.set_config("openlist", "refresh_paths", 
                                json.dumps(config.paths.refresh_paths, ensure_ascii=False))
        
        # STRM 引擎配置（从 strm_storage_map 推导）
        strm_engines = []
        if config.strm_storage_map:
            engine_groups: dict[str, list[str]] = {}
            for entry_path, mapping in config.strm_storage_map.items():
                mount_path = mapping.mount_path
                if mount_path not in engine_groups:
                    engine_groups[mount_path] = []
                for mp in mapping.paths:
                    if mp not in engine_groups[mount_path]:
                        engine_groups[mount_path].append(mp)
            for mount_path, monitored_paths in engine_groups.items():
                strm_engines.append({
                    "engine": mount_path,
                    "monitored_paths": monitored_paths,
                })
        watchlist_db.set_config("openlist", "strm_engines",
                                json.dumps(strm_engines, ensure_ascii=False))
        
        # 刷新配置
        watchlist_db.set_config("openlist", "refresh_enabled",
                                str(config.refresh.enabled).lower())
        watchlist_db.set_config("openlist", "refresh_interval_minutes",
                                str(config.refresh.interval_seconds // 60))
        watchlist_db.set_config("openlist", "refresh_depth",
                                str(config.refresh.depth))
        
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
        
        # 标记已迁移
        watchlist_db.set_config("migration", "config_toml_migrated", "true")
        
        logging.info("[Migration] OpenList 配置已迁移到 DB (20 个键 + 1 个迁移标记)")
        return True
    except Exception as e:
        logging.error("[Migration] 配置迁移失败: %s", e, exc_info=True)
        return False
