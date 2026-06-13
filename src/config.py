from __future__ import annotations
from typing import Optional
from dataclasses import dataclass, field
import re
import os
import sys
import json
from pathlib import Path

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def ensure_base_dir_first():
    normalized_base_dir = os.path.normcase(os.path.abspath(BASE_DIR))
    sys.path[:] = [
        p
        for p in sys.path
        if os.path.normcase(os.path.abspath(p or os.getcwd())) != normalized_base_dir
    ]
    sys.path.insert(0, BASE_DIR)


def load_local_module(module_name: str, filename: str,
                      base_dir: str | None = None):
    import importlib.util
    from types import ModuleType

    if base_dir is None:
        base_dir = BASE_DIR
    module_path = os.path.join(base_dir, filename)
    if not os.path.isfile(module_path):
        raise FileNotFoundError(f"本地模块文件不存在: {module_path}")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法创建模块加载规范: {module_name} ({module_path})")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


ensure_base_dir_first()

try:
    import tomllib
except ImportError:
    import tomli as tomllib


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
    wait_seconds: int
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
    # strm_monitored_paths: list[str]
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
    a_folders: list[str] = field(default_factory=list)
    # STRM 存储映射 mount_path -> StrmStorageMapping
    strm_storage_map: dict[str, StrmStorageMapping] = field(
        default_factory=dict)

    def __getattr__(self, name: str):
        if name == "strm_engine_paths":
            return self.paths.strm_engine_paths
        if name == "refresh_paths":
            return self.paths.refresh_paths
        raise AttributeError(
            f"'{self.__class__.__name__}' object has no attribute '{name}'"
        )

    @classmethod
    def from_file(cls, toml_path: str) -> "AppConfig":
        """从 config.toml 文件加载配置"""
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
            db_file=os.path.join(
                base_dir, local_data.get(
                    "db_file", "bridge.db")),
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
            wait_seconds=refresh_data.get("depth", 5),
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
            strm_engine_paths=read_line_list(
                paths_data.get(
                    "strm_engine_paths_file",
                    "strm_engine_paths.txt"),
                base_dir,
                is_webdav=True,
            ),
            refresh_paths=read_line_list(
                paths_data.get("refresh_paths_file", "refresh_paths.txt"),
                base_dir,
                is_webdav=True,
            ),
            # strm_monitored_paths=read_line_list(
            #    paths_data.get("strm_monitored_paths_file", "strm_monitored_paths.txt"),
            #    base_dir,
            #    is_webdav=True,
            # ),
            b_root=b_root,
            c_root=c_root,
        )

        # 从 API 获取 STRM 存储信息
        strm_storage_map = {}
        try:
            from webdav_client import OpenListAdminClient

            admin_client = OpenListAdminClient(
                host=webdav.host,
                user=webdav.user,
                password=webdav.password,
                totp_secret=webdav.totp_secret,
            )
            if admin_client.login():
                storages = admin_client.list_storages()
                if storages and isinstance(storages, dict):
                    data = storages.get("data", {})
                    content = data.get(
                        "content", []) if isinstance(
                        data, dict) else []
                    for storage in content:
                        if storage.get("driver", "").lower() != "strm":
                            continue
                        mount_path = storage.get("mount_path", "")
                        addition_str = storage.get("addition", "{}")
                        try:
                            addition = json.loads(addition_str)

                            # 容 paths 为字符串和列表的两种情况
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
                            path_groups = {}
                            for p in storage_paths:
                                last_dir = p.rstrip("/").split("/")[-1]
                                if last_dir not in path_groups:
                                    path_groups[last_dir] = []
                                path_groups[last_dir].append(p)

                            # 为每个 group 创建 StrmStorageMapping
                            for last_dir, group_paths in path_groups.items():
                                entry_path = f"{
                                    mount_path.rstrip('/')}/{last_dir}"
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
            else:
                logging.warning("[STRM存储API] 登录失败，跳过 STRM 存储映射")
        except Exception as exc:
            logging.warning("[STRM存储API] 获取 STRM 存储信息失败: %s", exc)

        instance = cls.__new__(cls)
        instance.base_dir = base_dir
        instance.webdav = webdav
        instance.refresh = refresh
        instance.behavior = behavior
        instance.log = log
        instance.local = local
        instance.paths = paths
        instance.a_folders = read_line_list(
            paths_data.get("a_folders_file", "a_folders.txt"),
            base_dir,
        )
        instance.strm_storage_map = strm_storage_map
        return instance

    def __init__(self, base_dir: str) -> None:
        """从 .txt 文件读取配置（保留原有逻辑）"""
        self.base_dir = base_dir
        self.webdav = WebDAVConfig(
            host=self._read_single_line("webdav_host.txt", base_dir),
            user=self._read_single_line("webdav_user.txt", base_dir),
            password=self._read_single_line("webdav_password.txt", base_dir),
            totp_secret=self._read_single_line(
                "webdav_totp_secret.txt", base_dir),
        )
        self.refresh = RefreshConfig(
            interval_seconds=int(
                self._read_single_line(
                    "refresh_interval.txt", base_dir) or "300"
            ),
            wait_seconds=int(
                self._read_single_line("refresh_wait.txt", base_dir) or "30"
            ),
            enabled=True,
            depth=5,
        )
        self.behavior = BehaviorConfig(
            sync_on_startup=self._read_single_line(
                "sync_on_startup.txt", base_dir
            ).lower()
            == "true",
            sync_on_startup_wait=int(
                self._read_single_line(
                    "sync_on_startup_wait.txt", base_dir) or "0"
            ),
        )
        self.log = LogConfig(
            level=self._read_single_line("log_level.txt", base_dir) or "INFO",
            max_size_mb=int(
                self._read_single_line("log_max_size_mb.txt", base_dir) or "2"
            ),
            backup_count=int(
                self._read_single_line("log_backup_count.txt", base_dir) or "5"
            ),
        )
        self.local = LocalConfig(
            base_dir=base_dir,
            a_dir=os.path.join(base_dir, "a"),
            b_dir=os.path.join(base_dir, "b"),
            c_dir=os.path.join(base_dir, "c"),
        )
        self.paths = PathsConfig(
            strm_engine_paths=read_line_list(
                "strm_engine_paths.txt", base_dir, is_webdav=True
            ),
            refresh_paths=read_line_list(
                "refresh_paths.txt", base_dir, is_webdav=True),
            strm_monitored_paths=read_line_list(
                "strm_monitored_paths.txt", base_dir, is_webdav=True
            ),
            b_root=os.path.join(base_dir, "b"),
            c_root=os.path.join(base_dir, "c"),
        )
        self.a_folders = read_line_list("a_folders.txt", base_dir)

    @staticmethod
    def _read_single_line(file_path: str, base_dir: str | Path) -> str:
        full_path = Path(base_dir) / file_path
        if not full_path.exists():
            return ""
        with open(full_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
        return ""
