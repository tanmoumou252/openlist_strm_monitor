"""config.py 单元测试

测试范围：
- ``normalize_local_root``：根路径归一化
- ``ABMapping.generate_mapping_id``：稳定 mapping_id 生成
- ``mapping_version``：mapping 集合 + C 根的稳定版本摘要
- ``AppConfig.from_file``：纯 TOML 解析（无网络）
- ``AppConfig.update_from_db``：DB > config.toml 覆盖优先级
- ``AppConfig.load_strm_storage_from_api``：mock OpenList 响应
- ``migrate_config_to_db``：一次性迁移与幂等
- 无效配置 fail-closed：解析失败不得污染运行时配置

所有测试使用临时 TOML / 内存字典 mock，不访问真实云端或真实 DB 文件。

运行方式：
  python -m pytest src/tests/test_config.py -v
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (  # noqa: E402
    ABMapping,
    AppConfig,
    LINEAGE_VERSION,
    StrmStorageMapping,
    mapping_version,
    migrate_config_to_db,
    normalize_local_root,
    read_line_list,
)


# ============================================================
# 辅助
# ============================================================

_MINIMAL_TOML = """
[webui]
port = 8579
bind = "0.0.0.0"
"""


def _write_toml(tmp_path: Path, body: str) -> str:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return str(path)


class FakeWatchlistDb:
    """最小化 webui_config 替身，行为对齐 TmdbWatchlistDb 的 scope/key 语义。"""

    def __init__(self, initial: dict[str, dict[str, str]] | None = None) -> None:
        self.store: dict[str, dict[str, str]] = {}
        for scope, kv in (initial or {}).items():
            self.store[scope] = dict(kv)

    def get_config(self, scope: str, key: str, default: str = "") -> str:
        return self.store.get(scope, {}).get(key, default)

    def set_config(self, scope: str, key: str, value: str) -> None:
        self.store.setdefault(scope, {})[key] = value

    def get_all_config(self, scope: str) -> dict[str, str]:
        return dict(self.store.get(scope, {}))


# ============================================================
# normalize_local_root
# ============================================================

class TestNormalizeLocalRoot:
    """根路径归一化：用于 mapping 归属判断。"""

    def test_returns_absolute_path(self, tmp_path):
        result = normalize_local_root(tmp_path)
        assert result.is_absolute()

    def test_accepts_str_and_path(self, tmp_path):
        assert normalize_local_root(str(tmp_path)) == normalize_local_root(tmp_path)

    def test_collapses_dot_segments(self, tmp_path):
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        noisy = tmp_path / "a" / "." / "b"
        assert normalize_local_root(noisy) == normalize_local_root(sub)

    def test_collapses_parent_segments(self, tmp_path):
        sub = tmp_path / "a"
        sub.mkdir()
        noisy = tmp_path / "a" / "x" / ".."
        assert normalize_local_root(noisy) == normalize_local_root(sub)

    def test_trailing_separator_is_normalized(self, tmp_path):
        assert normalize_local_root(str(tmp_path) + os.sep) == normalize_local_root(tmp_path)

    def test_expands_user_home(self):
        result = normalize_local_root("~")
        assert result == Path.home().resolve()


# ============================================================
# ABMapping.generate_mapping_id
# ============================================================

class TestGenerateMappingId:
    """mapping_id 必须只由 A 根决定，且稳定、短。"""

    def test_id_is_eight_hex_chars(self, tmp_path):
        mapping_id = ABMapping.generate_mapping_id(str(tmp_path))
        assert len(mapping_id) == 8
        int(mapping_id, 16)  # 必须是合法十六进制

    def test_id_is_stable_across_calls(self, tmp_path):
        first = ABMapping.generate_mapping_id(str(tmp_path))
        second = ABMapping.generate_mapping_id(str(tmp_path))
        assert first == second

    def test_id_ignores_path_noise(self, tmp_path):
        sub = tmp_path / "a"
        sub.mkdir()
        plain = ABMapping.generate_mapping_id(str(sub))
        noisy = ABMapping.generate_mapping_id(str(tmp_path / "a" / "." / ""))
        assert plain == noisy

    def test_different_roots_get_different_ids(self, tmp_path):
        one = tmp_path / "one"
        two = tmp_path / "two"
        one.mkdir()
        two.mkdir()
        assert (ABMapping.generate_mapping_id(str(one))
                != ABMapping.generate_mapping_id(str(two)))

    def test_b_root_change_does_not_change_id(self, tmp_path):
        a_root = tmp_path / "a"
        a_root.mkdir()
        m1 = ABMapping(
            mapping_id=ABMapping.generate_mapping_id(str(a_root)),
            a_root=str(a_root), b_root=str(tmp_path / "b1"))
        m2 = ABMapping(
            mapping_id=ABMapping.generate_mapping_id(str(a_root)),
            a_root=str(a_root), b_root=str(tmp_path / "b2"))
        assert m1.mapping_id == m2.mapping_id

    def test_label_defaults_to_empty(self, tmp_path):
        m = ABMapping(mapping_id="x", a_root=str(tmp_path), b_root=str(tmp_path))
        assert m.label == ""


# ============================================================
# mapping_version
# ============================================================

class TestMappingVersion:
    """mapping_version 是 lineage snapshot 失效判断的输入，必须稳定且对顺序不敏感。"""

    def _mappings(self, tmp_path):
        a1 = tmp_path / "a1"
        a2 = tmp_path / "a2"
        b1 = tmp_path / "b1"
        b2 = tmp_path / "b2"
        for d in (a1, a2, b1, b2):
            d.mkdir()
        return (
            ABMapping(mapping_id="m1", a_root=str(a1), b_root=str(b1)),
            ABMapping(mapping_id="m2", a_root=str(a2), b_root=str(b2)),
        )

    def test_returns_sha256_hex(self, tmp_path):
        m1, _ = self._mappings(tmp_path)
        digest = mapping_version([m1], tmp_path)
        assert len(digest) == 64
        int(digest, 16)

    def test_stable_for_same_input(self, tmp_path):
        m1, m2 = self._mappings(tmp_path)
        assert (mapping_version([m1, m2], tmp_path)
                == mapping_version([m1, m2], tmp_path))

    def test_order_insensitive(self, tmp_path):
        m1, m2 = self._mappings(tmp_path)
        assert (mapping_version([m1, m2], tmp_path)
                == mapping_version([m2, m1], tmp_path))

    def test_changes_when_b_root_changes(self, tmp_path):
        m1, _ = self._mappings(tmp_path)
        changed = ABMapping(mapping_id="m1", a_root=m1.a_root,
                            b_root=str(tmp_path / "b_other"))
        assert mapping_version([m1], tmp_path) != mapping_version([changed], tmp_path)

    def test_changes_when_c_root_changes(self, tmp_path):
        m1, _ = self._mappings(tmp_path)
        other_c = tmp_path / "c_other"
        other_c.mkdir()
        assert mapping_version([m1], tmp_path) != mapping_version([m1], other_c)

    def test_label_is_not_part_of_version(self, tmp_path):
        m1, _ = self._mappings(tmp_path)
        labeled = ABMapping(mapping_id="m1", a_root=m1.a_root,
                            b_root=m1.b_root, label="番剧")
        assert mapping_version([m1], tmp_path) == mapping_version([labeled], tmp_path)

    def test_empty_mapping_list_is_valid(self, tmp_path):
        digest = mapping_version([], tmp_path)
        assert len(digest) == 64

    def test_lineage_version_is_int(self):
        assert isinstance(LINEAGE_VERSION, int)


# ============================================================
# read_line_list
# ============================================================

class TestReadLineList:
    """txt 清单读取：注释与空行必须被剔除。"""

    def test_missing_file_returns_empty(self, tmp_path):
        assert read_line_list("nope.txt", tmp_path) == []

    def test_skips_comments_and_blank_lines(self, tmp_path):
        (tmp_path / "list.txt").write_text(
            "# comment\n\n/a\n  /b  \n#/c\n", encoding="utf-8")
        assert read_line_list("list.txt", tmp_path) == ["/a", "/b"]

    def test_webdav_mode_strips_trailing_slash(self, tmp_path):
        (tmp_path / "list.txt").write_text("/a/\n/b//\n", encoding="utf-8")
        assert read_line_list("list.txt", tmp_path, is_webdav=True) == ["/a", "/b"]


# ============================================================
# AppConfig.from_file
# ============================================================

class TestAppConfigFromFile:
    """from_file 必须是纯文件解析，不发起任何网络请求。"""

    def test_minimal_toml_loads(self, tmp_path):
        cfg = AppConfig.from_file(_write_toml(tmp_path, _MINIMAL_TOML))
        assert cfg.base_dir == str(tmp_path)

    def test_db_file_is_normalized_absolute(self, tmp_path):
        # db_file 固定在项目根，[local].db_file 不再从 config.toml 读取
        cfg = AppConfig.from_file(_write_toml(tmp_path, _MINIMAL_TOML))
        assert Path(cfg.local.db_file) == tmp_path / "bridge.db"

    def test_db_file_ignores_custom_local_db_file(self, tmp_path):
        # 即使 TOML 含 [local] db_file，运行路径仍为 base_dir/bridge.db
        toml = _MINIMAL_TOML + '\n[local]\ndb_file = "custom.db"\n'
        cfg = AppConfig.from_file(_write_toml(tmp_path, toml))
        assert Path(cfg.local.db_file) == tmp_path / "bridge.db"

    def test_log_file_is_absolute(self, tmp_path):
        cfg = AppConfig.from_file(_write_toml(tmp_path, _MINIMAL_TOML))
        assert Path(cfg.log.file).is_absolute()

    def test_refresh_interval_minutes_to_seconds(self, tmp_path):
        toml = _MINIMAL_TOML + "\n[refresh]\ninterval_minutes = 3\n"
        cfg = AppConfig.from_file(_write_toml(tmp_path, toml))
        assert cfg.refresh.interval_seconds == 180

    def test_full_audit_interval_days_default_is_seven(self, tmp_path):
        cfg = AppConfig.from_file(_write_toml(tmp_path, _MINIMAL_TOML))
        assert cfg.refresh.full_audit_interval_days == 7

    def test_full_audit_interval_days_override(self, tmp_path):
        toml = _MINIMAL_TOML + "\n[refresh]\nfull_audit_interval_days = 0\n"
        cfg = AppConfig.from_file(_write_toml(tmp_path, toml))
        assert cfg.refresh.full_audit_interval_days == 0

    def test_paths_start_empty_and_come_from_db(self, tmp_path):
        """strm_engine_paths / refresh_paths 只从 DB 或 API 加载，不来自 TOML。"""
        cfg = AppConfig.from_file(_write_toml(tmp_path, _MINIMAL_TOML))
        assert cfg.paths.strm_engine_paths == []
        assert cfg.paths.refresh_paths == []

    def test_getattr_shim_exposes_paths(self, tmp_path):
        cfg = AppConfig.from_file(_write_toml(tmp_path, _MINIMAL_TOML))
        assert cfg.strm_engine_paths is cfg.paths.strm_engine_paths
        assert cfg.refresh_paths is cfg.paths.refresh_paths

    def test_getattr_shim_rejects_unknown_attribute(self, tmp_path):
        cfg = AppConfig.from_file(_write_toml(tmp_path, _MINIMAL_TOML))
        with pytest.raises(AttributeError):
            _ = cfg.definitely_not_a_config_field

    def test_from_file_initializes_mapping_fields(self, tmp_path):
        """from_file 必须初始化 a_b_mappings / engines_initialized。

        旧行为是留下未赋值的 slot（访问抛 AttributeError），导致
        routes.handle_config_api 在全新安装时整页 500。现改为与 dataclass
        默认值对齐；读取侧仍建议 getattr 兜底。
        """
        cfg = AppConfig.from_file(_write_toml(tmp_path, _MINIMAL_TOML))
        assert cfg.a_b_mappings == []
        assert cfg.engines_initialized is False
        assert getattr(cfg, "a_b_mappings", None) == []

    def test_b_root_and_c_root_propagate_to_local(self, tmp_path):
        b_root = tmp_path / "b"
        c_root = tmp_path / "c"
        toml = _MINIMAL_TOML + (
            f'\n[paths]\nb_root = "{b_root.as_posix()}"\n'
            f'c_root = "{c_root.as_posix()}"\n')
        cfg = AppConfig.from_file(_write_toml(tmp_path, toml))
        assert cfg.local.b_dir == b_root.as_posix()
        assert cfg.local.c_dir == c_root.as_posix()

    def test_relative_root_only_warns(self, tmp_path, caplog):
        toml = _MINIMAL_TOML + '\n[paths]\nb_root = "relative/b"\n'
        with caplog.at_level("WARNING"):
            cfg = AppConfig.from_file(_write_toml(tmp_path, toml))
        assert cfg.paths.b_root == "relative/b"
        assert any("b_root" in r.message for r in caplog.records)

    def test_webui_defaults(self, tmp_path):
        cfg = AppConfig.from_file(_write_toml(tmp_path, _MINIMAL_TOML))
        assert cfg.webui.port == 8579
        assert cfg.webui.bind == "0.0.0.0"

    def test_webui_override(self, tmp_path):
        toml = _MINIMAL_TOML.replace('port = 8579', 'port = 9000').replace('bind = "0.0.0.0"', 'bind = "127.0.0.1"')
        cfg = AppConfig.from_file(_write_toml(tmp_path, toml))
        assert cfg.webui.port == 9000
        assert cfg.webui.bind == "127.0.0.1"

    def test_tmdb_section_is_ignored_by_design(self, tmp_path):
        """TMDB 配置已迁移到 DB；TOML 中的 [tmdb] 不应生效。"""
        toml = _MINIMAL_TOML + '\n[tmdb]\naccess_token = "from_toml"\n'
        cfg = AppConfig.from_file(_write_toml(tmp_path, toml))
        assert cfg.tmdb.access_token == ""

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(OSError):
            AppConfig.from_file(str(tmp_path / "absent.toml"))


# ============================================================
# AppConfig.update_from_db
# ============================================================

class TestUpdateFromDb:
    """DB > config.toml 覆盖优先级与类型转换。"""

    def _cfg(self, tmp_path):
        return AppConfig.from_file(_write_toml(tmp_path, _MINIMAL_TOML))

    def test_none_db_is_noop(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.update_from_db(None)
        assert cfg.webdav.host == ""

    def test_empty_scope_is_noop(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.update_from_db(FakeWatchlistDb())
        assert cfg.webdav.host == ""

    def test_webdav_fields_override(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.update_from_db(FakeWatchlistDb({"openlist": {
            "webdav_host": "http://127.0.0.1:5244",
            "webdav_user": "admin",
            "webdav_password": "pw",
            "webdav_totp_secret": "sec",
        }}))
        assert cfg.webdav.host == "http://127.0.0.1:5244"
        assert cfg.webdav.user == "admin"
        assert cfg.webdav.password == "pw"
        assert cfg.webdav.totp_secret == "sec"

    def test_refresh_interval_minutes_converted(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.update_from_db(FakeWatchlistDb(
            {"openlist": {"refresh_interval_minutes": "15"}}))
        assert cfg.refresh.interval_seconds == 900

    def test_invalid_interval_keeps_previous_value(self, tmp_path):
        cfg = self._cfg(tmp_path)
        before = cfg.refresh.interval_seconds
        cfg.update_from_db(FakeWatchlistDb(
            {"openlist": {"refresh_interval_minutes": "abc"}}))
        assert cfg.refresh.interval_seconds == before

    def test_full_audit_interval_days_override(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.update_from_db(FakeWatchlistDb(
            {"openlist": {"refresh_full_audit_interval_days": "3"}}))
        assert cfg.refresh.full_audit_interval_days == 3

    def test_full_audit_interval_days_zero_disables(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.update_from_db(FakeWatchlistDb(
            {"openlist": {"refresh_full_audit_interval_days": "0"}}))
        assert cfg.refresh.full_audit_interval_days == 0

    def test_full_audit_interval_days_negative_clamped_to_zero(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.update_from_db(FakeWatchlistDb(
            {"openlist": {"refresh_full_audit_interval_days": "-5"}}))
        assert cfg.refresh.full_audit_interval_days == 0

    def test_full_audit_interval_days_invalid_keeps_previous(self, tmp_path):
        cfg = self._cfg(tmp_path)
        before = cfg.refresh.full_audit_interval_days
        cfg.update_from_db(FakeWatchlistDb(
            {"openlist": {"refresh_full_audit_interval_days": "later"}}))
        assert cfg.refresh.full_audit_interval_days == before

    @pytest.mark.parametrize("raw,expected", [
        ("true", True), ("True", True), ("1", True), ("yes", True),
        ("false", False), ("0", False), ("", False), ("no", False),
    ])
    def test_bool_conversion(self, tmp_path, raw, expected):
        cfg = self._cfg(tmp_path)
        cfg.update_from_db(FakeWatchlistDb(
            {"openlist": {"engines_initialized": raw}}))
        assert cfg.engines_initialized is expected

    def test_b_root_and_c_root_sync_to_local(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.update_from_db(FakeWatchlistDb({"openlist": {
            "b_root": "D:/media/b", "c_root": "D:/media/c"}}))
        assert cfg.paths.b_root == "D:/media/b"
        assert cfg.local.b_dir == "D:/media/b"
        assert cfg.paths.c_root == "D:/media/c"
        assert cfg.local.c_dir == "D:/media/c"

    def test_relative_log_file_becomes_absolute(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.update_from_db(FakeWatchlistDb(
            {"openlist": {"log_file": "strm_bridge.log"}}))
        assert Path(cfg.log.file) == (tmp_path / "strm_bridge.log").resolve()

    def test_strm_engines_derive_engine_paths(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.update_from_db(FakeWatchlistDb({"openlist": {
            "strm_engines": json.dumps([
                {"engine": "/strm_a"}, {"engine": "/strm_b"},
                {"engine": "/strm_a"},
            ])}}))
        assert cfg.paths.strm_engine_paths == ["/strm_a", "/strm_b"]

    def test_invalid_strm_engines_json_fails_closed(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.update_from_db(FakeWatchlistDb(
            {"openlist": {"strm_engines": "{not json"}}))
        assert cfg.openlist_strm_engines == []

    def test_refresh_paths_loaded_from_db(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.update_from_db(FakeWatchlistDb({"openlist": {
            "refresh_paths": json.dumps(["/strm_a/番剧"])}}))
        assert cfg.paths.refresh_paths == ["/strm_a/番剧"]
        assert cfg.refresh_paths == ["/strm_a/番剧"]

    def test_invalid_refresh_paths_json_fails_closed(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.update_from_db(FakeWatchlistDb(
            {"openlist": {"refresh_paths": "[oops"}}))
        assert cfg.openlist_refresh_paths == []

    def test_a_b_mappings_parsed(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.update_from_db(FakeWatchlistDb({"openlist": {
            "a_b_mappings": json.dumps([
                {"mapping_id": "m1", "a_root": "C:/a1",
                 "b_root": "C:/b1", "label": "番剧"},
            ])}}))
        assert len(cfg.a_b_mappings) == 1
        assert cfg.a_b_mappings[0].mapping_id == "m1"
        assert cfg.a_b_mappings[0].label == "番剧"

    def test_a_b_mappings_drops_entries_missing_roots(self, tmp_path):
        """缺少 a_root 或 b_root 的映射必须被丢弃，不允许半配置进入运行时。"""
        cfg = self._cfg(tmp_path)
        cfg.update_from_db(FakeWatchlistDb({"openlist": {
            "a_b_mappings": json.dumps([
                {"mapping_id": "ok", "a_root": "C:/a1", "b_root": "C:/b1"},
                {"mapping_id": "no_b", "a_root": "C:/a2", "b_root": ""},
                {"mapping_id": "no_a", "a_root": "", "b_root": "C:/b3"},
            ])}}))
        assert [m.mapping_id for m in cfg.a_b_mappings] == ["ok"]

    def test_a_b_mappings_backfills_missing_mapping_id(self, tmp_path):
        """前端保存体不含 mapping_id（openlist.js 的真实形态），读取侧必须补齐稳定 ID。

        回归防护：mapping_id 为空会让 AppService.get_config_status() 返回
        fail_safe_active，引擎静默不同步、A→B 永不落地。
        """
        cfg = self._cfg(tmp_path)
        cfg.update_from_db(FakeWatchlistDb({"openlist": {
            "a_b_mappings": json.dumps([
                {"a_root": "C:/a1", "b_root": "C:/b1", "label": ""},
            ])}}))
        assert len(cfg.a_b_mappings) == 1
        assert cfg.a_b_mappings[0].mapping_id == ABMapping.generate_mapping_id("C:/a1")

    def test_a_b_mappings_backfill_keeps_explicit_mapping_id(self, tmp_path):
        """已存在的 mapping_id 不得被覆盖（B 根变更不改 mapping_id 的既有契约）。"""
        cfg = self._cfg(tmp_path)
        cfg.update_from_db(FakeWatchlistDb({"openlist": {
            "a_b_mappings": json.dumps([
                {"mapping_id": "legacy", "a_root": "C:/a1", "b_root": "C:/b1"},
            ])}}))
        assert cfg.a_b_mappings[0].mapping_id == "legacy"

    def test_a_b_mappings_backfill_unique_per_a_root(self, tmp_path):
        """不同 A 根补齐后必须得到不同 mapping_id，否则门禁会判为重复 ID。"""
        cfg = self._cfg(tmp_path)
        cfg.update_from_db(FakeWatchlistDb({"openlist": {
            "a_b_mappings": json.dumps([
                {"a_root": "C:/a1", "b_root": "C:/b1"},
                {"a_root": "C:/a2", "b_root": "C:/b2"},
            ])}}))
        ids = [m.mapping_id for m in cfg.a_b_mappings]
        assert "" not in ids
        assert len(set(ids)) == 2

    def test_invalid_a_b_mappings_json_fails_closed(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.update_from_db(FakeWatchlistDb(
            {"openlist": {"a_b_mappings": "not-json"}}))
        assert cfg.a_b_mappings == []

    def test_a_folders_only_from_configured_engines(self, tmp_path):
        """a_folders 只从用户已配置的引擎派生，OpenList 端自动发现的不算。"""
        cfg = self._cfg(tmp_path)
        cfg.strm_storage_map = {
            "/strm_a/番剧": StrmStorageMapping(
                mount_path="/strm_a", paths=["/云盘/番剧"], local_path="C:/a1"),
            "/strm_b/电影": StrmStorageMapping(
                mount_path="/strm_b", paths=["/云盘/电影"], local_path="C:/a2"),
        }
        cfg.update_from_db(FakeWatchlistDb({"openlist": {
            "strm_engines": json.dumps([{"engine": "/strm_a"}])}}))
        assert cfg.a_folders == ["C:/a1"]

    def test_db_exception_is_swallowed(self, tmp_path):
        """DB 读取抛异常时只降级告警，不得让启动崩溃。"""
        cfg = self._cfg(tmp_path)
        broken = MagicMock()
        broken.get_all_config.side_effect = RuntimeError("db down")
        cfg.update_from_db(broken)
        assert cfg.webdav.host == ""


# ============================================================
# load_strm_storage_from_api
# ============================================================

class TestLoadStrmStorageFromApi:
    """mock OpenList Admin 响应，验证映射派生与失败降级。"""

    def _cfg(self, tmp_path):
        return AppConfig.from_file(_write_toml(tmp_path, _MINIMAL_TOML))

    def _client(self, storages):
        client = MagicMock()
        client.get_strm_storages_full_info.return_value = storages
        client.last_error_message = ""
        return client

    def test_no_host_skips_without_client(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.load_strm_storage_from_api()
        assert cfg.strm_storage_map == {}

    def test_empty_content_keeps_map_empty(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.load_strm_storage_from_api(self._client([]))
        assert cfg.strm_storage_map == {}

    def test_entry_paths_are_mount_plus_last_dir(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.openlist_strm_engines = [{"engine": "/strm_a"}]
        cfg.load_strm_storage_from_api(self._client([{
            "mount_path": "/strm_a",
            "addition": json.dumps({
                "paths": "/云盘/番剧\n/云盘/电影",
                "SaveStrmLocalPath": "C:/a1",
            }),
        }]))
        assert set(cfg.strm_storage_map) == {"/strm_a/番剧", "/strm_a/电影"}

    def test_paths_accepts_list_form(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.openlist_strm_engines = [{"engine": "/strm_a"}]
        cfg.load_strm_storage_from_api(self._client([{
            "mount_path": "/strm_a",
            "addition": json.dumps({
                "paths": ["/云盘/番剧"], "SaveStrmLocalPath": "C:/a1"}),
        }]))
        assert "/strm_a/番剧" in cfg.strm_storage_map

    def test_same_last_dir_paths_are_grouped(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.load_strm_storage_from_api(self._client([{
            "mount_path": "/strm_a",
            "addition": json.dumps({
                "paths": "/盘1/番剧\n/盘2/番剧",
                "SaveStrmLocalPath": "C:/a1",
            }),
        }]))
        mapping = cfg.strm_storage_map["/strm_a/番剧"]
        assert mapping.paths == ["/盘1/番剧", "/盘2/番剧"]

    def test_unconfigured_engine_not_in_a_folders(self, tmp_path):
        """引擎存在于 OpenList 但用户未配置时，不得进入扫描范围。"""
        cfg = self._cfg(tmp_path)
        cfg.openlist_strm_engines = []
        cfg.load_strm_storage_from_api(self._client([{
            "mount_path": "/strm_a",
            "addition": json.dumps({
                "paths": "/云盘/番剧", "SaveStrmLocalPath": "C:/a1"}),
        }]))
        assert cfg.a_folders == []
        assert cfg.paths.strm_engine_paths == []
        # 但 storage_map 仍保留，供 WebUI 下拉框发现
        assert "/strm_a/番剧" in cfg.strm_storage_map

    def test_configured_engine_derives_a_folders(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.openlist_strm_engines = [{"engine": "/strm_a"}]
        cfg.load_strm_storage_from_api(self._client([{
            "mount_path": "/strm_a",
            "addition": json.dumps({
                "paths": "/云盘/番剧", "SaveStrmLocalPath": "C:/a1"}),
        }]))
        assert cfg.a_folders == ["C:/a1"]
        assert cfg.paths.strm_engine_paths == ["/strm_a"]

    def test_invalid_addition_json_is_skipped(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.load_strm_storage_from_api(self._client([
            {"mount_path": "/broken", "addition": "{not json"},
        ]))
        assert cfg.strm_storage_map == {}

    def test_api_returning_none_keeps_previous_state(self, tmp_path):
        cfg = self._cfg(tmp_path)
        client = MagicMock()
        client.get_strm_storages_full_info.return_value = None
        client.last_error_message = "timeout"
        cfg.load_strm_storage_from_api(client)
        assert cfg.strm_storage_map == {}
        assert cfg.a_folders == []

    def test_client_exception_is_swallowed(self, tmp_path):
        cfg = self._cfg(tmp_path)
        client = MagicMock()
        client.get_strm_storages_full_info.side_effect = RuntimeError("boom")
        cfg.load_strm_storage_from_api(client)
        assert cfg.strm_storage_map == {}


# ============================================================
# StrmStorageMapping 派生属性
# ============================================================

class TestStrmStorageMapping:
    """引擎入口路径 / 云端路径 / 本地 A 路径派生。"""

    def _mapping(self):
        return StrmStorageMapping(
            mount_path="/strm_a/",
            paths=["/云盘/番剧", "/云盘/电影"],
            local_path=os.path.join("C:", os.sep, "a1"),
        )

    def test_engine_entry_paths(self):
        assert self._mapping().engine_entry_paths == ["/strm_a/番剧", "/strm_a/电影"]

    def test_get_engine_entry_path_with_subpath(self):
        assert self._mapping().get_engine_entry_path("x") == "/strm_a/番剧/x"

    def test_get_engine_entry_path_falls_back_to_mount(self):
        m = StrmStorageMapping(mount_path="/strm_a", paths=[], local_path="")
        assert m.get_engine_entry_path() == "/strm_a"

    def test_get_cloud_path(self):
        assert self._mapping().get_cloud_path("S01") == "/云盘/番剧/S01"

    def test_get_local_path_appends_last_dir(self):
        result = self._mapping().get_local_path()
        assert result.endswith(os.path.join("a1", "番剧"))

    def test_get_local_path_empty_when_no_local(self):
        m = StrmStorageMapping(mount_path="/strm_a", paths=["/云盘/番剧"], local_path="")
        assert m.get_local_path() == ""


# ============================================================
# migrate_config_to_db
# ============================================================

class TestMigrateConfigToDb:
    """一次性迁移：幂等、不注入自动发现的引擎。"""

    def _cfg(self, tmp_path):
        return AppConfig.from_file(_write_toml(tmp_path, _MINIMAL_TOML))

    def test_none_db_returns_false(self, tmp_path):
        assert migrate_config_to_db(self._cfg(tmp_path), None) is False

    def test_first_run_migrates_and_marks_flag(self, tmp_path):
        db = FakeWatchlistDb()
        assert migrate_config_to_db(self._cfg(tmp_path), db) is True
        assert db.get_config("migration", "config_toml_migrated") == "true"

    def test_second_run_is_idempotent(self, tmp_path):
        db = FakeWatchlistDb()
        cfg = self._cfg(tmp_path)
        migrate_config_to_db(cfg, db)
        assert migrate_config_to_db(cfg, db) is False

    def test_strm_engines_migrated_as_empty_array(self, tmp_path):
        """迁移绝不把自动发现的引擎写成"用户已配置"。"""
        db = FakeWatchlistDb()
        cfg = self._cfg(tmp_path)
        cfg.openlist_strm_engines = [{"engine": "/auto_discovered"}]
        migrate_config_to_db(cfg, db)
        assert json.loads(db.get_config("openlist", "strm_engines")) == []
        assert db.get_config("openlist", "engines_initialized") == "true"

    def test_full_audit_interval_days_is_migrated(self, tmp_path):
        db = FakeWatchlistDb()
        cfg = self._cfg(tmp_path)
        cfg.refresh.full_audit_interval_days = 3
        migrate_config_to_db(cfg, db)
        assert db.get_config("openlist", "refresh_full_audit_interval_days") == "3"

    def test_log_file_key_is_written(self, tmp_path):
        """log_file 是 DB 往返的关键键，必须存在，否则热更新会丢日志路径。"""
        db = FakeWatchlistDb()
        migrate_config_to_db(self._cfg(tmp_path), db)
        assert db.get_config("openlist", "log_file")

    def test_legacy_refresh_paths_txt_is_consumed(self, tmp_path):
        (tmp_path / "refresh_paths.txt").write_text(
            "# c\n/strm_a/番剧/\n", encoding="utf-8")
        db = FakeWatchlistDb()
        migrate_config_to_db(self._cfg(tmp_path), db)
        assert json.loads(db.get_config("openlist", "refresh_paths")) == ["/strm_a/番剧"]

    def test_missing_legacy_file_writes_empty_array(self, tmp_path):
        db = FakeWatchlistDb()
        migrate_config_to_db(self._cfg(tmp_path), db)
        assert json.loads(db.get_config("openlist", "refresh_paths")) == []

    def test_write_failure_returns_false(self, tmp_path):
        db = MagicMock()
        db.get_config.return_value = ""
        db.set_config.side_effect = RuntimeError("readonly db")
        assert migrate_config_to_db(self._cfg(tmp_path), db) is False

    def test_migrated_config_round_trips_into_update_from_db(self, tmp_path):
        """迁移写入的键必须能被 update_from_db 读回，避免键名漂移。"""
        db = FakeWatchlistDb()
        cfg = self._cfg(tmp_path)
        cfg.webdav.host = "http://openlist.local"
        cfg.refresh.depth = 4
        cfg.behavior.action = "COPY"
        migrate_config_to_db(cfg, db)

        fresh = self._cfg(tmp_path)
        fresh.update_from_db(db)
        assert fresh.webdav.host == "http://openlist.local"
        assert fresh.refresh.depth == 4
        assert fresh.behavior.action == "COPY"
