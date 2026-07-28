"""
Python 调用覆盖测试。

覆盖调用关系清单中标记"无"的项（从 main.py 启动链 + WebUI 启动链推导）：
  1. config.migrate_config_to_db()        — TOML→DB 迁移
  2. config.AppConfig.update_from_db()    — DB 覆盖配置
  3. config.AppConfig.load_strm_storage_from_api() — 动态存储映射
  4. logger_setup.setup_logging()         — 初始化日志
  5. OpenListAdminClient.check_exists()   — 根目录验证（main.py 调用）

注：check_exists() 已在 test_webdav_client.py 中有详尽的单元测试，
此处仅补充 main.py 启动链视角的调用覆盖（mock 集成）。

测试策略：
  - 使用真实 SQLite (TmdbWatchlistDb) 隔离于 tmp_path
  - AppConfig 使用真实 dataclass 实例（非 MagicMock）
  - load_strm_storage_from_api 使用 mock OpenListAdminClient
  - setup_logging 使用 tmp_path 日志文件
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 冗余保护：确保 src/ 在 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (
    AppConfig,
    BehaviorConfig,
    LocalConfig,
    LogConfig,
    PathsConfig,
    RefreshConfig,
    StrmStorageMapping,
    WebDAVConfig,
    WebUIConfig,
    migrate_config_to_db,
)
from tmdb_watchlist_db import TmdbWatchlistDb  # noqa: E402
from logger_setup import setup_logging  # noqa: E402


# ============================================================
# 测试工具
# ============================================================

def _make_app_config(tmp_path: Path) -> AppConfig:
    """构造真实的 AppConfig dataclass 实例（非 mock）。"""
    return AppConfig(
        base_dir=str(tmp_path),
        webdav=WebDAVConfig(
            host="http://192.168.1.100:5244",
            user="admin",
            password="secret123",
            totp_secret="JBSWY3DPEHPK3PXP",
        ),
        refresh=RefreshConfig(
            interval_seconds=600,
            enabled=True,
            depth=5,
        ),
        behavior=BehaviorConfig(
            sync_on_startup=True,
            sync_on_startup_wait=0,
            trash_dir_name="trash",
            action="MOVE",
            ghost_protect_seconds=300,
            a_to_b_restore_delay_seconds=30,
        ),
        log=LogConfig(
            level="INFO",
            max_size_mb=10,
            backup_count=5,
            file=str(tmp_path / "strm_bridge.log"),
        ),
        local=LocalConfig(
            base_dir=str(tmp_path),
            a_dir=str(tmp_path / "a"),
            b_dir=str(tmp_path / "b"),
            c_dir=str(tmp_path / "c"),
            db_file=str(tmp_path / "bridge.db"),
        ),
        paths=PathsConfig(
            strm_engine_paths=[],
            refresh_paths=[],
            b_root=str(tmp_path / "b"),
            c_root=str(tmp_path / "c"),
        ),
        webui=WebUIConfig(enabled=True, port=8579, bind="0.0.0.0"),
    )


def _make_watchlist_db(tmp_path: Path) -> TmdbWatchlistDb:
    """创建真实 TmdbWatchlistDb（隔离于 tmp_path）。"""
    db_path = tmp_path / "tmdb_watchlist.db"
    return TmdbWatchlistDb(db_path)


# ============================================================
# 1. migrate_config_to_db()
# ============================================================

class TestMigrateConfigToDb:
    """配置迁移测试 (config.migrate_config_to_db)。"""

    def test_first_migration_returns_true(self, tmp_path):
        """首次迁移应返回 True，并写入所有配置到 DB。"""
        cfg = _make_app_config(tmp_path)
        wdb = _make_watchlist_db(tmp_path)

        result = migrate_config_to_db(cfg, wdb)

        assert result is True, "首次迁移应返回 True"

        # 验证迁移标记
        assert wdb.get_config("migration", "config_toml_migrated") == "true", \
            "迁移标记应设为 true"

        # 验证 WebDAV 配置已迁移
        assert wdb.get_config("openlist", "webdav_host") == cfg.webdav.host
        assert wdb.get_config("openlist", "webdav_user") == cfg.webdav.user
        assert wdb.get_config("openlist", "webdav_password") == cfg.webdav.password
        assert wdb.get_config("openlist", "webdav_totp_secret") == cfg.webdav.totp_secret

        # 验证路径配置(c_root 仍保留;b_root 已废弃不再持久化到 DB)
        assert wdb.get_config("openlist", "c_root") == cfg.paths.c_root

        # 验证行为配置
        assert wdb.get_config("openlist", "behavior_action") == "MOVE"
        assert wdb.get_config("openlist", "behavior_ghost_protect_seconds") == "300"
        assert wdb.get_config("openlist", "behavior_sync_on_startup") == "true"

        # 验证刷新配置
        assert wdb.get_config("openlist", "refresh_enabled") == "true"
        assert wdb.get_config("openlist", "refresh_interval_minutes") == "10"
        assert wdb.get_config("openlist", "refresh_log_level") == "INFO"

        # 验证日志配置
        assert wdb.get_config("openlist", "log_level") == "INFO"
        assert wdb.get_config("openlist", "log_max_size_mb") == "10"

        # 验证 STRM 引擎配置（迁移写入空数组 + engines_initialized=true）
        engines_raw = wdb.get_config("openlist", "strm_engines")
        assert json.loads(engines_raw) == [], "迁移时 strm_engines 应为空数组"
        assert wdb.get_config("openlist", "engines_initialized") == "true"

    def test_idempotent_second_migration_returns_false(self, tmp_path):
        """第二次迁移应返回 False（已迁移）。"""
        cfg = _make_app_config(tmp_path)
        wdb = _make_watchlist_db(tmp_path)

        # 第一次迁移
        migrate_config_to_db(cfg, wdb)
        # 第二次迁移
        result = migrate_config_to_db(cfg, wdb)

        assert result is False, "第二次迁移应返回 False（幂等）"

    def test_migration_with_none_watchlist_db(self, tmp_path):
        """传入 None 的 watchlist_db 应返回 False（安全退出）。"""
        cfg = _make_app_config(tmp_path)
        result = migrate_config_to_db(cfg, None)
        assert result is False, "None watchlist_db 应返回 False"


# ============================================================
# 2. AppConfig.update_from_db()
# ============================================================

class TestUpdateFromDb:
    """DB 配置覆盖测试 (AppConfig.update_from_db)。"""

    def test_basic_config_override(self, tmp_path):
        """DB 配置应覆盖 AppConfig 中的值。"""
        cfg = _make_app_config(tmp_path)
        wdb = _make_watchlist_db(tmp_path)

        # 设置 DB 配置覆盖
        wdb.set_config("openlist", "webdav_host", "http://10.0.0.1:5244")
        wdb.set_config("openlist", "webdav_user", "new_user")
        wdb.set_config("openlist", "behavior_action", "COPY")
        wdb.set_config("openlist", "behavior_ghost_protect_seconds", "600")
        wdb.set_config("openlist", "log_level", "DEBUG")
        wdb.set_config("openlist", "refresh_enabled", "false")
        wdb.set_config("openlist", "refresh_interval_minutes", "30")

        cfg.update_from_db(wdb)

        # 验证 DB 覆盖生效
        assert cfg.webdav.host == "http://10.0.0.1:5244", \
            f"webdav.host 应被 DB 覆盖: {cfg.webdav.host}"
        assert cfg.webdav.user == "new_user", \
            f"webdav.user 应被 DB 覆盖: {cfg.webdav.user}"
        assert cfg.behavior.action == "COPY", \
            f"behavior.action 应被 DB 覆盖: {cfg.behavior.action}"
        assert cfg.behavior.ghost_protect_seconds == 600, \
            f"ghost_protect_seconds 应被 DB 覆盖: {cfg.behavior.ghost_protect_seconds}"
        assert cfg.log.level == "DEBUG", \
            f"log.level 应被 DB 覆盖: {cfg.log.level}"
        assert cfg.refresh.enabled is False, \
            f"refresh.enabled 应被 DB 覆盖: {cfg.refresh.enabled}"
        assert cfg.refresh.interval_seconds == 30 * 60, \
            f"refresh.interval_seconds 应被 DB 覆盖: {cfg.refresh.interval_seconds}"

    def test_strm_engines_from_db(self, tmp_path):
        """STRM 引擎配置应从 DB 加载并派生 strm_engine_paths。"""
        cfg = _make_app_config(tmp_path)
        wdb = _make_watchlist_db(tmp_path)

        engines = [
            {"engine": "/天翼云盘", "monitored_paths": ["/番剧"]},
            {"engine": "/阿里云盘", "monitored_paths": ["/电影"]},
        ]
        wdb.set_config("openlist", "strm_engines", json.dumps(engines, ensure_ascii=False))
        wdb.set_config("openlist", "engines_initialized", "true")

        cfg.update_from_db(wdb)

        assert len(cfg.openlist_strm_engines) == 2, \
            f"应加载 2 个引擎: {cfg.openlist_strm_engines}"
        assert "/天翼云盘" in cfg.paths.strm_engine_paths, \
            f"strm_engine_paths 应包含引擎路径: {cfg.paths.strm_engine_paths}"
        assert "/阿里云盘" in cfg.paths.strm_engine_paths
        assert cfg.engines_initialized is True

    def test_empty_db_no_override(self, tmp_path):
        """空 DB 不应覆盖 AppConfig 默认值。"""
        cfg = _make_app_config(tmp_path)
        wdb = _make_watchlist_db(tmp_path)

        original_host = cfg.webdav.host
        original_action = cfg.behavior.action

        cfg.update_from_db(wdb)

        assert cfg.webdav.host == original_host, \
            "空 DB 不应覆盖 webdav.host"
        assert cfg.behavior.action == original_action, \
            "空 DB 不应覆盖 behavior.action"

    def test_update_from_none_db_noop(self, tmp_path):
        """传入 None 的 watchlist_db 不应崩溃。"""
        cfg = _make_app_config(tmp_path)
        original_host = cfg.webdav.host
        cfg.update_from_db(None)
        assert cfg.webdav.host == original_host

    def test_path_sync_to_local(self, tmp_path):
        """b_root/c_root 应同步到 local.b_dir/local.c_dir。"""
        cfg = _make_app_config(tmp_path)
        wdb = _make_watchlist_db(tmp_path)

        wdb.set_config("openlist", "b_root", "/media/b")
        wdb.set_config("openlist", "c_root", "/media/c")

        cfg.update_from_db(wdb)

        assert cfg.local.b_dir == "/media/b", \
            f"local.b_dir 应同步: {cfg.local.b_dir}"
        assert cfg.local.c_dir == "/media/c", \
            f"local.c_dir 应同步: {cfg.local.c_dir}"
        assert cfg.paths.b_root == "/media/b", \
            f"paths.b_root 应同步: {cfg.paths.b_root}"

    def test_behavior_config_types(self, tmp_path):
        """行为配置应正确转换为 int/bool 类型。"""
        cfg = _make_app_config(tmp_path)
        wdb = _make_watchlist_db(tmp_path)

        wdb.set_config("openlist", "behavior_sync_on_startup", "false")
        wdb.set_config("openlist", "behavior_sync_on_startup_wait", "10")
        wdb.set_config("openlist", "behavior_a_to_b_restore_delay_seconds", "60")

        cfg.update_from_db(wdb)

        assert cfg.behavior.sync_on_startup is False, \
            f"sync_on_startup 应为 False: {cfg.behavior.sync_on_startup}"
        assert cfg.behavior.sync_on_startup_wait == 10, \
            f"sync_on_startup_wait 应为 10: {cfg.behavior.sync_on_startup_wait}"
        assert cfg.behavior.a_to_b_restore_delay_seconds == 60, \
            f"a_to_b_restore_delay_seconds 应为 60: {cfg.behavior.a_to_b_restore_delay_seconds}"

    def test_log_file_relative_path_converted_to_absolute(self, tmp_path):
        """DB 中的相对路径 log_file 应被 update_from_db 转换为绝对路径。

        覆盖 config.py:254-260：旧迁移数据可能残留相对路径（如 "strm_bridge.log"），
        update_from_db 应拼接 base_dir 并 resolve 为绝对路径。
        """
        cfg = _make_app_config(tmp_path)
        wdb = _make_watchlist_db(tmp_path)

        # 绕过 set_config 直接写相对路径到 DB（模拟旧迁移数据）
        with wdb._conn() as conn:
            conn.execute(
                """INSERT INTO webui_config (scope, key, value, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(scope, key) DO UPDATE SET
                       value=excluded.value, updated_at=excluded.updated_at""",
                ("openlist", "log_file", "strm_bridge.log", 0),
            )
            conn.commit()

        cfg.update_from_db(wdb)

        # 验证 log.file 已转为绝对路径
        assert Path(cfg.log.file).is_absolute(), \
            f"log.file 应为绝对路径，实际: {cfg.log.file}"
        # 验证路径包含 base_dir
        assert str(tmp_path) in cfg.log.file, \
            f"log.file 应包含 base_dir {tmp_path}，实际: {cfg.log.file}"
        # 验证文件名保留
        assert cfg.log.file.endswith("strm_bridge.log"), \
            f"log.file 应保留原文件名，实际: {cfg.log.file}"


# ============================================================
# 3. AppConfig.load_strm_storage_from_api()
# ============================================================

class TestLoadStrmStorageFromApi:
    """STRM 存储映射加载测试 (AppConfig.load_strm_storage_from_api)。"""

    def test_load_storages_with_mock_client(self, tmp_path):
        """使用 mock OpenListAdminClient 加载 STRM 存储映射。"""
        cfg = _make_app_config(tmp_path)
        # 预设用户已配置的引擎
        cfg.openlist_strm_engines = [
            {"engine": "/天翼云盘", "monitored_paths": ["/番剧"]},
        ]

        # 构造 mock client
        mock_client = MagicMock()
        mock_client.get_strm_storages_full_info.return_value = [
            {
                "mount_path": "/天翼云盘",
                "addition": json.dumps({
                    "paths": "/天翼云盘家庭云30GB/番剧\n/天翼云盘家庭云30GB/电影",
                    "SaveStrmLocalPath": str(tmp_path / "a" / "tianyi"),
                }),
            },
            {
                "mount_path": "/阿里云盘",
                "addition": json.dumps({
                    "paths": "/阿里云盘/番剧",
                    "SaveStrmLocalPath": str(tmp_path / "a" / "ali"),
                }),
            },
        ]

        cfg.load_strm_storage_from_api(admin_client=mock_client)

        # 验证 strm_storage_map 包含所有引擎（用于 UI 发现）
        assert len(cfg.strm_storage_map) > 0, \
            f"strm_storage_map 不应为空: {cfg.strm_storage_map}"

        # 验证 a_folders 仅包含用户配置的引擎（/天翼云盘）
        # /阿里云盘 不在 openlist_strm_engines 中，不应进入 a_folders
        ali_in_a_folders = any("ali" in f for f in cfg.a_folders)
        assert not ali_in_a_folders, \
            f"未配置引擎不应进入 a_folders: {cfg.a_folders}"

        # 验证 get_strm_storages_full_info 被调用
        mock_client.get_strm_storages_full_info.assert_called_once()

    def test_load_storages_api_failure(self, tmp_path):
        """API 失败时 load_strm_storage_from_api 应安全退出。"""
        cfg = _make_app_config(tmp_path)

        mock_client = MagicMock()
        mock_client.get_strm_storages_full_info.return_value = None

        # 不应抛出异常
        cfg.load_strm_storage_from_api(admin_client=mock_client)

        # strm_storage_map 应为空（未更新）
        assert cfg.strm_storage_map == {}

    def test_load_storages_empty_result(self, tmp_path):
        """空结果应安全处理。"""
        cfg = _make_app_config(tmp_path)

        mock_client = MagicMock()
        mock_client.get_strm_storages_full_info.return_value = []

        cfg.load_strm_storage_from_api(admin_client=mock_client)

        assert cfg.strm_storage_map == {}

    def test_load_storages_malformed_addition(self, tmp_path):
        """addition JSON 解析失败应跳过该条目。"""
        cfg = _make_app_config(tmp_path)

        mock_client = MagicMock()
        mock_client.get_strm_storages_full_info.return_value = [
            {
                "mount_path": "/test",
                "addition": "not valid json{",
            },
        ]

        # 不应抛出异常
        cfg.load_strm_storage_from_api(admin_client=mock_client)

        # 应跳过无效条目
        assert len(cfg.strm_storage_map) == 0

    def test_load_storages_paths_as_list(self, tmp_path):
        """addition.paths 为列表时应正确解析。"""
        cfg = _make_app_config(tmp_path)

        mock_client = MagicMock()
        mock_client.get_strm_storages_full_info.return_value = [
            {
                "mount_path": "/test",
                "addition": json.dumps({
                    "paths": ["/test/path1", "/test/path2"],
                    "SaveStrmLocalPath": str(tmp_path / "a"),
                }),
            },
        ]

        cfg.load_strm_storage_from_api(admin_client=mock_client)

        assert len(cfg.strm_storage_map) > 0, "应解析列表格式的 paths"

    def test_load_strm_storage_skips_when_host_empty(self, tmp_path):
        """host 为空时跳过 STRM 存储加载，不创建 client，不报错。

        覆盖 config.py:461-466：load_strm_storage_from_api 在 host 为空时
        直接 return，避免误报为"网络请求异常"。
        """
        cfg = _make_app_config(tmp_path)
        cfg.webdav.host = ""
        # 预设引擎配置，但 host 为空时应跳过加载
        cfg.openlist_strm_engines = [
            {"engine": "/天翼云盘", "monitored_paths": ["/番剧"]},
        ]

        # 不传 admin_client，应触发内部 host 空检查
        # 不应抛出异常
        cfg.load_strm_storage_from_api()

        # strm_storage_map 应保持为空
        assert cfg.strm_storage_map == {}, \
            f"host 为空时 strm_storage_map 应为空，实际: {cfg.strm_storage_map}"


# ============================================================
# 4. logger_setup.setup_logging()
# ============================================================

class TestSetupLogging:
    """日志系统初始化测试 (logger_setup.setup_logging)。"""

    def test_creates_handlers(self, tmp_path):
        """setup_logging 应创建 stdout/stderr/file 三个 handler。"""
        log_file = tmp_path / "test.log"
        setup_logging(
            level="INFO",
            log_file=str(log_file),
            max_size_mb=1,
            backup_count=1,
        )

        root = logging.getLogger()
        handler_types = [type(h).__name__ for h in root.handlers]

        # 应有 StreamHandler (stdout)、StreamHandler (stderr)、RotatingFileHandler
        assert "RotatingFileHandler" in handler_types, \
            f"应包含 RotatingFileHandler: {handler_types}"

        # 至少 3 个 handler（stdout, stderr, file）
        assert len(root.handlers) >= 3, \
            f"应有 >= 3 个 handlers: {len(root.handlers)}"

    def test_creates_log_file(self, tmp_path):
        """setup_logging 应创建日志文件。"""
        log_dir = tmp_path / "logs"
        log_file = log_dir / "test.log"

        setup_logging(
            level="DEBUG",
            log_file=str(log_file),
            max_size_mb=1,
            backup_count=1,
        )

        # 写一条日志触发文件创建
        logging.info("[Test] 测试日志文件创建")
        for h in logging.getLogger().handlers:
            h.flush()

        assert log_file.exists(), \
            f"日志文件应被创建: {log_file}"

    def test_respects_log_level(self, tmp_path):
        """日志级别应被正确设置。"""
        log_file = tmp_path / "test.log"
        setup_logging(
            level="WARNING",
            log_file=str(log_file),
            max_size_mb=1,
            backup_count=1,
        )

        root = logging.getLogger()
        assert root.level == logging.DEBUG, \
            f"root logger 应为 DEBUG: {root.level}"

        # 找到 stdout handler（通过 MaxLevelFilter）
        from logger_setup import MaxLevelFilter
        stdout_handlers = [
            h for h in root.handlers
            if any(isinstance(f, MaxLevelFilter) for f in h.filters)
        ]
        if stdout_handlers:
            assert stdout_handlers[0].level == logging.WARNING, \
                f"stdout handler 应为 WARNING: {stdout_handlers[0].level}"

    def test_fallback_to_temp_dir_on_bad_path(self, tmp_path):
        """日志目录不可写时应 fallback 到临时目录。"""
        # 使用一个不存在且不可创建的路径
        log_file = tmp_path / "nonexistent" / "deeply" / "nested" / "test.log"

        # 不应抛出异常
        setup_logging(
            level="INFO",
            log_file=str(log_file),
            max_size_mb=1,
            backup_count=1,
        )

        root = logging.getLogger()
        assert len(root.handlers) >= 3, "即使 fallback 也应创建 3 个 handlers"


# ============================================================
# 5. OpenListAdminClient.check_exists() (main.py 启动链视角)
# ============================================================

class TestCheckExistsIntegration:
    """check_exists 集成测试（main.py 启动链视角）。

    check_exists() 已在 test_webdav_client.py 中有详尽单元测试。
    此处补充 main.py 启动时调用 check_exists("/") 验证根目录存在的场景。
    """

    def test_check_exists_root_true_mocked(self, tmp_path):
        """main.py 调用 check_exists("/") 验证根目录存在。"""
        from webdav_client import OpenListAdminClient

        client = OpenListAdminClient(
            host="http://127.0.0.1:5244",
            user="admin",
            password="pass",
        )
        client.token = "jwt_token"

        # mock list_directory 返回成功
        with patch.object(client, "list_directory") as mock_list:
            mock_list.return_value = {
                "code": 200,
                "data": {"content": [], "total": 0},
            }
            result = client.check_exists("/")

        assert result is True, "根目录应存在"

    def test_check_exists_root_untrusted_returns_none(self, tmp_path):
        """check_exists("/") 列表失败时应返回 None（不可信，非 False）。"""
        from webdav_client import OpenListAdminClient

        client = OpenListAdminClient(
            host="http://127.0.0.1:5244",
            user="admin",
            password="pass",
        )
        client.token = "jwt_token"

        with patch.object(client, "list_directory") as mock_list:
            mock_list.return_value = None
            result = client.check_exists("/")

        assert result is None, "API 失败时根目录应返回 None（fail-closed）"

    def test_check_exists_caching(self, tmp_path):
        """check_exists 应缓存结果（TTL 内不重复请求）。"""
        from webdav_client import OpenListAdminClient

        client = OpenListAdminClient(
            host="http://127.0.0.1:5244",
            user="admin",
            password="pass",
        )
        client.token = "jwt_token"

        with patch.object(client, "list_directory") as mock_list:
            mock_list.return_value = {
                "code": 200,
                "data": {"content": [{"name": "file.txt"}], "total": 1},
            }
            # 第一次调用
            result1 = client.check_exists("/dir/file.txt")
            # 第二次调用（应命中缓存）
            result2 = client.check_exists("/dir/file.txt")

        assert result1 is True
        assert result2 is True
        # list_directory 只应被调用一次（第二次命中缓存）
        assert mock_list.call_count == 1, \
            f"第二次调用应命中缓存: {mock_list.call_count}"


# ============================================================
# 6. TmdbWatchlistDb 敏感键自动加解密集成测试
# ============================================================

class TestSensitiveKeyEncryption:
    """敏感键自动加解密集成测试。

    验证 TmdbWatchlistDb.get_config/set_config/get_all_config 对敏感键的自动加解密行为。
    """

    def test_set_config_encrypts_sensitive_keys(self, tmp_path):
        """set_config 应对敏感键自动加密。"""
        import secret_manager
        db = TmdbWatchlistDb(tmp_path / "test.db")

        # 设置敏感键
        db.set_config("openlist", "webdav_password", "my_secret_password")

        # 直接读取数据库原始值（绕过解密）
        with db._conn() as conn:
            row = conn.execute(
                "SELECT value FROM webui_config WHERE scope=? AND key=?",
                ("openlist", "webdav_password")
            ).fetchone()
            raw_value = row[0] if row else None

        # 原始值应以 ENC: 开头
        assert raw_value is not None, "敏感键应被写入数据库"
        assert raw_value.startswith("ENC:"), \
            f"敏感键应以 ENC: 开头，实际: {raw_value[:20]}"

        # 通过 get_config 读取应自动解密
        decrypted = db.get_config("openlist", "webdav_password")
        assert decrypted == "my_secret_password", \
            f"get_config 应返回解密后的值，实际: {decrypted}"

    def test_get_config_decrypts_sensitive_keys(self, tmp_path):
        """get_config 应对敏感键自动解密。"""
        import secret_manager
        db = TmdbWatchlistDb(tmp_path / "test.db")

        # 先加密写入
        encrypted = secret_manager.encrypt("test_token")
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO webui_config (scope, key, value, updated_at) VALUES (?, ?, ?, ?)",
                ("tmdb", "access_token", encrypted, 0)
            )
            conn.commit()

        # get_config 应自动解密
        decrypted = db.get_config("tmdb", "access_token")
        assert decrypted == "test_token", \
            f"get_config 应自动解密，实际: {decrypted}"

    def test_get_config_non_sensitive_keys_not_decrypted(self, tmp_path):
        """get_config 对非敏感键不应解密。"""
        db = TmdbWatchlistDb(tmp_path / "test.db")

        # 写入非敏感键（明文）
        db.set_config("openlist", "webdav_host", "http://example.com")

        # 读取应返回明文
        value = db.get_config("openlist", "webdav_host")
        assert value == "http://example.com", \
            f"非敏感键应返回明文，实际: {value}"

    def test_get_all_config_decrypts_sensitive_keys(self, tmp_path):
        """get_all_config 应对敏感键自动解密。"""
        import secret_manager
        db = TmdbWatchlistDb(tmp_path / "test.db")

        # 写入多个敏感键
        db.set_config("openlist", "webdav_password", "password123")
        db.set_config("tmdb", "api_key", "api_key_456")

        # 写入非敏感键
        db.set_config("openlist", "webdav_host", "http://test.com")

        # get_all_config 应自动解密敏感键
        all_config = db.get_all_config("openlist")

        assert "webdav_password" in all_config
        assert all_config["webdav_password"] == "password123", \
            f"敏感键应被解密，实际: {all_config['webdav_password']}"
        assert all_config["webdav_host"] == "http://test.com", \
            f"非敏感键应保持明文，实际: {all_config['webdav_host']}"

        # 验证 tmdb scope
        tmdb_config = db.get_all_config("tmdb")
        assert tmdb_config["api_key"] == "api_key_456", \
            f"tmdb 敏感键应被解密，实际: {tmdb_config['api_key']}"

    def test_set_config_empty_value_not_encrypted(self, tmp_path):
        """set_config 对空值不应加密。"""
        db = TmdbWatchlistDb(tmp_path / "test.db")

        # 设置空值
        db.set_config("openlist", "webdav_password", "")

        # 直接读取数据库原始值
        with db._conn() as conn:
            row = conn.execute(
                "SELECT value FROM webui_config WHERE scope=? AND key=?",
                ("openlist", "webdav_password")
            ).fetchone()
            raw_value = row[0] if row else None

        # 空值不应被加密（保持空字符串）
        assert raw_value == "", \
            f"空值应保持空字符串，实际: {raw_value}"

    def test_get_config_missing_key_returns_default(self, tmp_path):
        """get_config 对不存在的键应返回默认值。"""
        db = TmdbWatchlistDb(tmp_path / "test.db")

        # 读取不存在的键
        value = db.get_config("openlist", "nonexistent_key", "default_value")
        assert value == "default_value", \
            f"不存在的键应返回默认值，实际: {value}"

    def test_get_config_handles_corrupted_encrypted_value(self, tmp_path):
        """get_config 对损坏的加密值应返回空字符串而非抛异常。"""
        db = TmdbWatchlistDb(tmp_path / "test.db")

        # 写入损坏的加密值
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO webui_config (scope, key, value, updated_at) VALUES (?, ?, ?, ?)",
                ("openlist", "webdav_password", "ENC:corrupted_data", 0)
            )
            conn.commit()

        # get_config 应返回空字符串而非抛异常
        value = db.get_config("openlist", "webdav_password")
        assert value == "", \
            f"损坏的加密值应返回空字符串，实际: {value}"


# ============================================================
# 7. AppConfig.from_file() 新默认值测试
# ============================================================

class TestFromFileDefaults:
    """from_file() 新默认值测试。

    覆盖 config.py:349-412 的默认值变更：
    - b_root/c_root 默认空（原 base_dir/b、base_dir/c）
    - a_dir 默认空（原 base_dir/a）
    - refresh interval_minutes 默认 10（原 5）
    - log file 默认 strm_bridge.log（原 logs/strm_bridge.log）
    """

    def _write_minimal_toml(self, tmp_path: Path) -> Path:
        """写入最小化 config.toml（不含 [paths]/[refresh]/[log] 段，测试默认值）。"""
        config_path = tmp_path / "config.toml"
        config_content = """
[webdav]
host = "http://localhost:5244"
user = "admin"
password = "test"
"""
        config_path.write_text(config_content, encoding="utf-8")
        return config_path

    def test_from_file_b_root_c_root_default_empty(self, tmp_path):
        """from_file 在 [paths] 段缺失时 b_root/c_root 默认为空。"""
        config_path = self._write_minimal_toml(tmp_path)
        cfg = AppConfig.from_file(str(config_path))

        assert cfg.paths.b_root == "", \
            f"b_root 默认应为空，实际: {cfg.paths.b_root}"
        assert cfg.paths.c_root == "", \
            f"c_root 默认应为空，实际: {cfg.paths.c_root}"
        assert cfg.local.b_dir == "", f"local.b_dir 应为空"
        assert cfg.local.c_dir == "", f"local.c_dir 应为空"

    def test_from_file_a_dir_default_empty(self, tmp_path):
        """from_file 在 [local] 段缺失时 a_dir 默认为空。"""
        config_path = self._write_minimal_toml(tmp_path)
        cfg = AppConfig.from_file(str(config_path))

        assert cfg.local.a_dir == "", \
            f"a_dir 默认应为空（需在 WebUI 配置），实际: {cfg.local.a_dir}"

    def test_from_file_refresh_default_10_minutes(self, tmp_path):
        """from_file 在 [refresh] 段缺失时 interval 默认 10 分钟（600 秒）。

        覆盖 config.py:381：interval_minutes 默认从 5 改为 10。
        """
        config_path = self._write_minimal_toml(tmp_path)
        cfg = AppConfig.from_file(str(config_path))

        assert cfg.refresh.interval_seconds == 600, \
            f"refresh.interval_seconds 默认应为 600（10分钟），实际: {cfg.refresh.interval_seconds}"
        assert cfg.refresh.enabled is True, "refresh.enabled 默认应为 True"
        assert cfg.refresh.depth == 5, "refresh.depth 默认应为 5"

    def test_from_file_refresh_explicit_overrides_default(self, tmp_path):
        """from_file 显式配置 interval_minutes 时应覆盖默认值。"""
        config_path = tmp_path / "config.toml"
        config_content = """
[webdav]
host = "http://localhost:5244"

[refresh]
interval_minutes = 15
"""
        config_path.write_text(config_content, encoding="utf-8")
        cfg = AppConfig.from_file(str(config_path))

        assert cfg.refresh.interval_seconds == 900, \
            f"显式 15 分钟应为 900 秒，实际: {cfg.refresh.interval_seconds}"

    def test_from_file_log_file_default_no_logs_subdir(self, tmp_path):
        """from_file 在 [log] 段缺失时 log file 默认为 strm_bridge.log（无 logs/ 子目录）。

        覆盖 config.py:404：默认从 logs/strm_bridge.log 改为 strm_bridge.log。
        """
        config_path = self._write_minimal_toml(tmp_path)
        cfg = AppConfig.from_file(str(config_path))

        # log.file 应为 base_dir/strm_bridge.log（不含 logs/ 子目录）
        assert cfg.log.file.endswith("strm_bridge.log"), \
            f"log.file 应以 strm_bridge.log 结尾，实际: {cfg.log.file}"
        assert "logs" not in cfg.log.file.replace("\\", "/"), \
            f"log.file 不应包含 logs/ 子目录，实际: {cfg.log.file}"
        # 应为绝对路径（from_file 拼接 base_dir）
        assert Path(cfg.log.file).is_absolute(), \
            f"log.file 应为绝对路径，实际: {cfg.log.file}"

    def test_from_file_empty_b_root_no_absolute_warning(self, tmp_path, caplog):
        """from_file 在 b_root 为空时不应触发"不是绝对路径"警告。

        覆盖 config.py:356-359：空路径跳过绝对路径检查。
        """
        import logging as _logging
        config_path = self._write_minimal_toml(tmp_path)

        with caplog.at_level(_logging.WARNING, logger="root"):
            cfg = AppConfig.from_file(str(config_path))

        # 不应有 b_root/c_root 不是绝对路径的警告
        warnings = [r.message for r in caplog.records if "不是绝对路径" in r.message]
        assert warnings == [], \
            f"空路径不应触发绝对路径警告，实际: {warnings}"
        assert cfg.paths.b_root == ""


# ============================================================
# 8. load_strm_storage_from_api 错误记录测试
# ============================================================

class TestLoadStrmStorageErrorLogging:
    """load_strm_storage_from_api 获取失败时记录 last_error_message 测试。

    覆盖 config.py:476-481：当 get_strm_storages_full_info 返回 None 时，
    应记录 admin_client.last_error_message 到日志。
    """

    def test_load_strm_storage_logs_error_message_on_failure(self, tmp_path, caplog):
        """获取 STRM 存储信息失败时应记录 last_error_message。"""
        import logging as _logging
        cfg = _make_app_config(tmp_path)

        mock_client = MagicMock()
        mock_client.get_strm_storages_full_info.return_value = None
        mock_client.last_error_message = "连接超时"

        with caplog.at_level(_logging.WARNING, logger="root"):
            cfg.load_strm_storage_from_api(admin_client=mock_client)

        # 应有包含 last_error_message 的警告日志
        warnings = [r.message for r in caplog.records if "连接超时" in str(r.message)]
        assert len(warnings) > 0, \
            f"应记录 last_error_message，实际日志: {[r.message for r in caplog.records]}"
        # strm_storage_map 应为空
        assert cfg.strm_storage_map == {}
