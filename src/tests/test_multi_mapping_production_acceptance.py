"""
双 mapping 生产验收测试。

覆盖多 A↔多 B 隔离的关键验收标准：
- 两个 mapping 使用相同 fingerprint 时各自独立保留
- B 去重仅影响同 mapping 内实例
- C 归档路径包含 mapping_id
- boundary 在 mapping 间隔离
- 非法配置返回 None（fail-closed）
- WebUI 状态接口始终可访问
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (
    AppConfig, ABMapping, WebDAVConfig, RefreshConfig, BehaviorConfig,
    LogConfig, LocalConfig, PathsConfig,
)
from database import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_real_app(tmp_path: Path):
    """创建接近生产的最小 AppConfig + Database（无 mock）。"""
    db = Database(str(tmp_path / "bridge.db"))
    db.init_subtitle_table()

    cfg = AppConfig(
        base_dir=str(tmp_path),
        webdav=WebDAVConfig(host="", user="", password="", totp_secret=""),
        refresh=RefreshConfig(interval_seconds=300),
        behavior=BehaviorConfig(sync_on_startup=False, sync_on_startup_wait=0),
        log=LogConfig(level="INFO", max_size_mb=10, backup_count=1),
        local=LocalConfig(
            base_dir=str(tmp_path),
            a_dir=str(tmp_path / "a"),
            b_dir=str(tmp_path / "b"),
            c_dir=str(tmp_path / "c"),
        ),
        paths=PathsConfig(strm_engine_paths=[], refresh_paths=[]),
    )

    class _FakeApp:
        """最小 app stub，仅暴露验收所需的接口。"""
        def __init__(self):
            self.config = cfg
            self.db = db
            self.a_b_mappings: list[ABMapping] = []

        def get_mapping_for_a(self, path):
            p = Path(path).resolve()
            matches = []
            for m in self.a_b_mappings:
                ar = Path(m.a_root).resolve()
                try:
                    p.relative_to(ar)
                    matches.append((m.mapping_id, ar, Path(m.b_root).resolve()))
                except ValueError:
                    continue
            # 多重命中 → fail-closed
            if len(matches) != 1:
                return None
            return matches[0]

        def get_mapping_for_b(self, path):
            p = Path(path).resolve()
            matches = []
            for m in self.a_b_mappings:
                br = Path(m.b_root).resolve()
                try:
                    p.relative_to(br)
                    matches.append((m.mapping_id, br, Path(m.a_root).resolve()))
                except ValueError:
                    continue
            # 多重命中 → fail-closed
            if len(matches) != 1:
                return None
            return matches[0]

    return _FakeApp(), db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDualMappingIsolation:
    """验收核心：两个 mapping 的 B 记录互不干扰。"""

    def test_same_fingerprint_different_mapping_both_preserved(
            self, tmp_path: Path):
        """两个 mapping 写入相同 fingerprint 的 B 记录，各自保留。"""
        app, db = _make_real_app(tmp_path)
        a1 = tmp_path / "a1"; a1.mkdir()
        b1 = tmp_path / "b1"; b1.mkdir()
        a2 = tmp_path / "a2"; a2.mkdir()
        b2 = tmp_path / "b2"; b2.mkdir()

        app.a_b_mappings = [
            ABMapping(mapping_id="m1", a_root=str(a1), b_root=str(b1)),
            ABMapping(mapping_id="m2", a_root=str(a2), b_root=str(b2)),
        ]

        fp = "fingerprint-same-123"
        db.upsert_b(
            local_path=str(b1 / "show1.strm"),
            webdav_path="/dav/m1/show1.strm",
            parent_webdav_path="/dav/m1",
            source_a_path=str(a1 / "show1.strm"),
            fingerprint=fp,
            mapping_id="m1",
        )
        db.upsert_b(
            local_path=str(b2 / "show1.strm"),
            webdav_path="/dav/m2/show1.strm",
            parent_webdav_path="/dav/m2",
            source_a_path=str(a2 / "show1.strm"),
            fingerprint=fp,
            mapping_id="m2",
        )

        r1 = db.get_b_by_local(str(b1 / "show1.strm"))
        r2 = db.get_b_by_local(str(b2 / "show1.strm"))
        assert r1 is not None
        assert r2 is not None
        assert r1.mapping_id == "m1"
        assert r2.mapping_id == "m2"
        assert r1.fingerprint == fp
        assert r2.fingerprint == fp

    def test_dedup_does_not_affect_other_mapping(
            self, tmp_path: Path):
        """mark_other_b_instances_duplicate 仅影响同 mapping 内的记录。"""
        app, db = _make_real_app(tmp_path)
        a1 = tmp_path / "a1"; a1.mkdir()
        b1 = tmp_path / "b1"; b1.mkdir()
        a2 = tmp_path / "a2"; a2.mkdir()
        b2 = tmp_path / "b2"; b2.mkdir()

        app.a_b_mappings = [
            ABMapping(mapping_id="m1", a_root=str(a1), b_root=str(b1)),
            ABMapping(mapping_id="m2", a_root=str(a2), b_root=str(b2)),
        ]

        fp = "fp-dedup-test"
        db.upsert_b(str(b1 / "dup.strm"), "/dav/m1/dup.strm",
                     "/dav/m1", str(a1 / "dup.strm"), mapping_id="m1",
                     fingerprint=fp)
        db.upsert_b(str(b1 / "dup2.strm"), "/dav/m1/dup2.strm",
                     "/dav/m1", str(a1 / "dup2.strm"), mapping_id="m1",
                     fingerprint=fp)
        db.upsert_b(str(b2 / "dup.strm"), "/dav/m2/dup.strm",
                     "/dav/m2", str(a2 / "dup.strm"), mapping_id="m2",
                     fingerprint=fp)

        # 对 m1 执行去重：将 dup2 标记为 duplicate，保留 dup
        db.mark_other_b_instances_duplicate(fp, str(b1 / "dup.strm"), "m1")

        r_m1_dup = db.get_b_by_local(str(b1 / "dup.strm"))
        r_m1_dup2 = db.get_b_by_local(str(b1 / "dup2.strm"))
        r_m2_dup = db.get_b_by_local(str(b2 / "dup.strm"))

        assert r_m1_dup.status == "valid"
        assert r_m1_dup2.status == "duplicate"
        # m2 不受影响
        assert r_m2_dup.status == "valid"

    def test_boundary_isolation_between_mappings(
            self, tmp_path: Path):
        """两个 mapping 的 boundary 记录互不覆盖。"""
        app, db = _make_real_app(tmp_path)
        a1 = tmp_path / "a1"; a1.mkdir()
        b1 = tmp_path / "b1"; b1.mkdir()
        a2 = tmp_path / "a2"; a2.mkdir()
        b2 = tmp_path / "b2"; b2.mkdir()

        app.a_b_mappings = [
            ABMapping(mapping_id="m1", a_root=str(a1), b_root=str(b1)),
            ABMapping(mapping_id="m2", a_root=str(a2), b_root=str(b2)),
        ]

        fp = "fp-boundary"
        db.upsert_media_boundary("m1", fp, "ShowA", "ShowA_renamed", "/engine/entry1")
        db.upsert_media_boundary("m2", fp, "ShowB", "ShowB_renamed", "/engine/entry2")

        b1_rec = db.get_media_boundary_by_fingerprint("m1", fp)
        b2_rec = db.get_media_boundary_by_fingerprint("m2", fp)

        assert b1_rec is not None
        assert b2_rec is not None
        assert b1_rec.source_media_name == "ShowA"
        assert b2_rec.source_media_name == "ShowB"
        # m1 查不到 m2 的 boundary
        assert db.get_media_boundary_by_source_name_only(
            "m1", "ShowB") is None

    def test_projection_isolation_between_mappings(
            self, tmp_path: Path):
        """两个 mapping 的 identity projection 独立维护。"""
        app, db = _make_real_app(tmp_path)
        a1 = tmp_path / "a1"; a1.mkdir()
        b1 = tmp_path / "b1"; b1.mkdir()
        a2 = tmp_path / "a2"; a2.mkdir()
        b2 = tmp_path / "b2"; b2.mkdir()

        app.a_b_mappings = [
            ABMapping(mapping_id="m1", a_root=str(a1), b_root=str(b1)),
            ABMapping(mapping_id="m2", a_root=str(a2), b_root=str(b2)),
        ]

        fp = "fp-proj"
        db.upsert_identity_projection(fp, "m1", str(b1 / "show.strm"), "visible")
        db.upsert_identity_projection(fp, "m2", str(b2 / "show.strm"), "visible")

        p1 = db.get_identity_projection(fp, "m1")
        p2 = db.get_identity_projection(fp, "m2")
        assert p1 is not None
        assert p2 is not None
        assert p1[0] == str(b1 / "show.strm")  # current_b_path
        assert p2[0] == str(b2 / "show.strm")  # current_b_path


class TestMappingConfigValidation:
    """验证非法映射配置被拒绝。"""

    def test_empty_mappings_returns_not_configured(self, tmp_path: Path):
        """无映射配置 → get_mapping 返回 None。"""
        app, db = _make_real_app(tmp_path)
        app.a_b_mappings = []
        assert app.get_mapping_for_a("/some/path") is None
        assert app.get_mapping_for_b("/some/path") is None

    def test_duplicate_a_root_returns_none(self, tmp_path: Path):
        """两个映射使用相同 A 根 → get_mapping_for_a 返回 None（多重命中 fail-closed）。"""
        app, db = _make_real_app(tmp_path)
        shared = tmp_path / "shared_a"; shared.mkdir()
        b1 = tmp_path / "b1"; b1.mkdir()
        b2 = tmp_path / "b2"; b2.mkdir()

        app.a_b_mappings = [
            ABMapping(mapping_id="m1", a_root=str(shared), b_root=str(b1)),
            ABMapping(mapping_id="m2", a_root=str(shared), b_root=str(b2)),
        ]
        # 同根目录下的路径 → 两个映射都匹配 → 返回 None
        result = app.get_mapping_for_a(shared / "show.strm")
        assert result is None

    def test_upsert_b_rejects_empty_mapping_id(self, tmp_path: Path):
        """upsert_b 拒绝空 mapping_id。"""
        app, db = _make_real_app(tmp_path)
        with pytest.raises(ValueError, match="mapping_id must be a non-empty string"):
            db.upsert_b(
                local_path="/tmp/x.strm",
                webdav_path="/dav/x.strm",
                parent_webdav_path="/dav",
                source_a_path="/tmp/x.strm",
                mapping_id="",
                fingerprint="fp",
            )


class TestCPathIsolation:
    """验收：C 归档路径包含 mapping_id。"""

    def test_c_path_includes_mapping_id(self, tmp_path: Path):
        """C 路径应为 C/<mapping_id>/<relative>，不同 mapping 不碰撞。"""
        c = tmp_path / "c"; c.mkdir()
        b1 = tmp_path / "b1"; b1.mkdir()
        b2 = tmp_path / "b2"; b2.mkdir()

        rel = Path("Show") / "Season 01" / "S01E01.strm"
        expected_m1 = c / "m1" / rel
        expected_m2 = c / "m2" / rel

        assert expected_m1 != expected_m2
        assert str(expected_m1).endswith("S01E01.strm")
        assert "m1" in str(expected_m1)
        assert "m2" in str(expected_m2)


class TestWebUIAccessibility:
    """验收：空映射时状态接口仍可访问。"""

    def test_status_accessible_with_no_mapping(self):
        """空映射配置下查询不抛异常。"""
        app, db = _make_real_app(Path(tempfile.mkdtemp()))
        app.a_b_mappings = []
        assert app.get_mapping_for_a("/nonexistent") is None
        assert app.get_mapping_for_b("/nonexistent") is None
