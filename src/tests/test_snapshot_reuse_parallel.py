"""A1+A1b 等价红测（c7 §5.3）。

- A1：`_snapshot_reuse_check_parallel` 与逐行串行判定等价；前置门（M-12/E3）
  cache 未构建 → helper 零调用、串行回退结果一致；force_full → helper 零调用。
- A1b：`snapshots_loaded` 整 mapping 预载成功后，dict miss → 直接 False
  免 DB（Task 0 实测 DB 回退 5.45ms/次 × 12666 次 = 69s 收益主体）；
  miss 方向安全（假 miss → 全量校验重写快照，绝不假命中）。
- R-2：`_snapshot_reuses_valid_lineage` 异常分支降级 DEBUG，并行下零 WARNING 刷屏。
- C9 双锏：reconcile 语境 patch `_verify_b_path_lineage` 的用例同时钉
  `_b_lineage_preflight=False`。
"""
from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app_service_core import AppService  # noqa: E402
from config import ABMapping, AppConfig  # noqa: E402
from database import Database  # noqa: E402
from utils import make_strm_fingerprint  # noqa: E402


class TestSnapshotReuseParallel:
    """A1+A1b 等价与门控契约。"""

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="reuse_parallel_"))
        self.a_dir = self.tmp / "a"
        self.b_dir = self.tmp / "b"
        self.c_dir = self.tmp / "c"
        for d in (self.a_dir, self.b_dir, self.c_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.db = Database(str(self.tmp / "bridge.db"))
        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.a_b_mappings = [
            ABMapping(mapping_id="test_m1", a_root=str(self.a_dir), b_root=str(self.b_dir))]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []
        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, self.db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_pair(self, webdav: str, name: str = "a.strm", show: str = "show") -> Path:
        """播种 A 源 + 同构 B 文件 + A 记录 + B 行。"""
        a_src = self.a_dir / show / "Season 01" / name
        a_src.parent.mkdir(parents=True, exist_ok=True)
        a_src.write_text(webdav, encoding="utf-8")
        self.db.upsert_a(str(a_src), webdav, f"/cloud/{show}")
        b_file = self.b_dir / show / "Season 01" / name
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text(webdav, encoding="utf-8")
        self.db.upsert_b(
            str(b_file), webdav, f"/cloud/{show}", str(a_src),
            mapping_id="test_m1", fingerprint=make_strm_fingerprint(webdav))
        return b_file

    def _store_snapshot(self, b_file: Path, webdav: str):
        self.app.ensure_scan_mapping_roots()
        try:
            self.app._store_valid_lineage_snapshot(str(b_file), make_strm_fingerprint(webdav))
        finally:
            self.app.clear_scan_mapping_roots()

    def test_helper_equals_serial_over_mixed_profiles(self):
        """helper == 逐行串行：热快照 / 无快照 / 指纹不匹配 混合画像"""
        # 热快照行
        w1 = "/cloud/show/S01E01.mp4"
        b1 = self._seed_pair(w1, name="e1.strm", show="hot")
        self._store_snapshot(b1, w1)
        # 无快照行（磁盘存在、B 行在）
        w2 = "/cloud/show/S01E02.mp4"
        b2 = self._seed_pair(w2, name="e2.strm", show="cold")
        # 指纹不匹配行（B 行 fp 与磁盘不同 → 判定 False）
        w3 = "/cloud/show/S01E03.mp4"
        b3 = self._seed_pair(w3, name="e3.strm", show="mismatch")
        self.db.upsert_b(
            str(b3), w3, "/cloud/show", None,
            mapping_id="test_m1", fingerprint="stale_fp")

        candidates = [(str(b1), make_strm_fingerprint(w1)),
                      (str(b2), make_strm_fingerprint(w2)),
                      (str(b3), make_strm_fingerprint(w3))]
        parallel = self.app._snapshot_reuse_check_parallel(candidates)
        serial = {lp for lp, fp in candidates
                  if self.app._snapshot_reuses_valid_lineage(lp, fp)}
        assert parallel == serial == {str(b1)}
        # 确定性：跑两次一致
        assert self.app._snapshot_reuse_check_parallel(candidates) == parallel

    def test_helper_empty_input_no_pool(self):
        """空入参不建池、直接返回空集"""
        assert self.app._snapshot_reuse_check_parallel([]) == set()

    def test_front_gate_cache_none_falls_back_serial(self):
        """M-12/E3 前置门：cache 未构建 → helper 零调用，串行回退结果一致"""
        w1 = "/cloud/show/S01E01.mp4"
        b1 = self._seed_pair(w1)
        self._store_snapshot(b1, w1)
        disk_data = self.app._scan_b_disk()
        db_records = self.db.get_all_b_records()
        processed: set[str] = set()
        # cache 保持 None（未经 initial_scan_b 构建）→ 前置门回退串行
        assert self.app._reconcile_cache is None
        with patch.object(
                self.app, "_snapshot_reuse_check_parallel",
                side_effect=AssertionError("cache None 时禁止走并行 helper")) as helper:
            self.app._reconcile_b_historical_records(disk_data, db_records, processed)
        helper.assert_not_called()
        assert str(b1) in processed  # 串行复用判定照常命中

    def test_force_full_zero_helper_calls(self):
        """force_full=True → Wave0 不收集候选，helper 仅以空参调用一次

        c.9.2 Task 7（方案 a 锁定）：补 `ensure_scan_mapping_roots()` 打开
        并行门后，生产代码 `if use_parallel_reuse:` 对空 candidates 仍调用
        helper（`_snapshot_reuse_check_parallel` 首行空集早退，无池开销、
        无副作用，且与 force_full「零候选」语义精确对齐）——断言相应收紧为
        `assert_called_once_with([])`，不再断言 `assert_not_called`（那会
        假红）。方案 b（生产门收紧 `and candidates`）会改变 M1 观测日志行
        的 `并行=` 取值，被否决。
        """
        w1 = "/cloud/show/S01E01.mp4"
        b1 = self._seed_pair(w1)
        self._store_snapshot(b1, w1)
        disk_data = self.app._scan_b_disk()
        db_records = self.db.get_all_b_records()
        processed: set[str] = set()
        self.app._reconcile_cache = {"snapshots": {}, "snapshots_loaded": {"test_m1"},
                                     "pending_snapshots": [], "a_by_webdav": {},
                                     "boundaries": {"by_fingerprint": {},
                                                    "by_source_name_only": {},
                                                    "by_current_name": {}}}
        try:
            self.app.ensure_scan_mapping_roots()
            with patch.object(self.app, "_snapshot_reuse_check_parallel") as helper:
                self.app._reconcile_b_historical_records(
                    disk_data, db_records, processed, force_full=True)
            helper.assert_called_once_with([])
        finally:
            self.app._reconcile_cache = None
            self.app.clear_scan_mapping_roots()

    def test_a1b_cache_miss_no_db_call(self):
        """A1b：整 mapping 预载成功后 dict miss → get_b_lineage_snapshot 零调用"""
        w1 = "/cloud/show/S01E01.mp4"
        b1 = self._seed_pair(w1)
        self.app.ensure_scan_mapping_roots()
        try:
            self.app._build_reconcile_cache()
            cache = self.app._reconcile_cache
            assert "test_m1" in cache["snapshots_loaded"]
            # 该 mapping 无快照 → dict miss
            with patch.object(self.db, "get_b_lineage_snapshot") as mock_get:
                ok = self.app._snapshot_reuses_valid_lineage(
                    str(b1), make_strm_fingerprint(w1))
            mock_get.assert_not_called()
            assert ok is False  # miss 方向安全：直接 False（不假命中）
        finally:
            self.app._reconcile_cache = None
            self.app.clear_scan_mapping_roots()

    def test_a1b_unloaded_mapping_still_falls_back_db(self):
        """A1b：mapping 预载失败（未入 snapshots_loaded）→ 维持 DB 回退"""
        w1 = "/cloud/show/S01E01.mp4"
        b1 = self._seed_pair(w1)
        self.app.ensure_scan_mapping_roots()
        try:
            self.app._build_reconcile_cache()
            # 模拟该 mapping 预载失败：从 snapshots_loaded 移除
            self.app._reconcile_cache["snapshots_loaded"].discard("test_m1")
            with patch.object(self.db, "get_b_lineage_snapshot",
                              return_value=None) as mock_get:
                ok = self.app._snapshot_reuses_valid_lineage(
                    str(b1), make_strm_fingerprint(w1))
            mock_get.assert_called_once()  # 未覆盖 mapping → DB 回退
            assert ok is False
        finally:
            self.app._reconcile_cache = None
            self.app.clear_scan_mapping_roots()

    def test_r2_exception_downgraded_to_debug_no_warning_flood(self, caplog):
        """R-2：异常分支降级 DEBUG——并行复用检查下零 WARNING 刷屏"""
        w1 = "/cloud/show/S01E01.mp4"
        b1 = self._seed_pair(w1)
        candidates = [(str(b1), make_strm_fingerprint(w1))]
        with patch.object(
                self.app, "_snapshot_reuses_valid_lineage",
                side_effect=OSError("simulated stat failure")), \
             caplog.at_level(logging.WARNING):
            result = self.app._snapshot_reuse_check_parallel(candidates)
        assert result == set()  # 异常 → False（与串行 catch 同类别）
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []

    def test_reconcile_reuse_info_observation_line(self, caplog):
        """观测面：Wave0 结束后一条 INFO「B 区快照复用: 命中 X / 待核 Y」"""
        w1 = "/cloud/show/S01E01.mp4"
        b1 = self._seed_pair(w1)
        self._store_snapshot(b1, w1)
        disk_data = self.app._scan_b_disk()
        db_records = self.db.get_all_b_records()
        processed: set[str] = set()
        with caplog.at_level(logging.INFO):
            self.app._reconcile_b_historical_records(disk_data, db_records, processed)
        reuse_lines = [r for r in caplog.records if "快照复用" in r.message]
        assert any("命中 1" in r.message and "待核 0" in r.message
                   for r in reuse_lines), [r.message for r in reuse_lines]

    def test_c9_reconcile_verify_patch_pins_preflight_false(self):
        """C9 双锏：reconcile 语境 patch _verify_b_path_lineage 必须钉
        _b_lineage_preflight=False，否则回退段不被行使、测试静默失效"""
        # 无 A 源孤儿行 → 预检 False → wave2 串行回退完整校验
        webdav = "/cloud/ghost/S01E02.mp4"
        b_file = self.b_dir / "ghost" / "Season 01" / "x.strm"
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text(webdav, encoding="utf-8")
        self.db.upsert_b(
            str(b_file), webdav, "/cloud/ghost", None,
            mapping_id="test_m1", fingerprint=make_strm_fingerprint(webdav))
        disk_data = self.app._scan_b_disk()
        db_records = self.db.get_all_b_records()
        processed: set[str] = set()
        with patch.object(self.app, "_b_lineage_preflight", return_value=False), \
             patch.object(self.app, "_verify_b_path_lineage",
                          wraps=self.app._verify_b_path_lineage) as mock_verify:
            self.app._reconcile_b_historical_records(disk_data, db_records, processed)
        # 回退段被真实行使（若 preflight 未被钉 False，预检短路会让本断言假绿）
        mock_verify.assert_called_once()


class TestN3A2T5Reuse:
    """N3（pass1 T5 透传）+ A2（store 仅关键字 stat_result/_resolved_target）。

    N3：pass1 每条 local_path 恰 resolve 一次，以 _resolved_target 透传
    fast mapping；A2：Wave2 复用 Wave1 被丢弃的 _stat_map/_resolved_map，
    缺省 None 零行为变化；迁移分支不改；禁 mapping_id 形参。
    """

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="n3a2_"))
        self.a_dir = self.tmp / "a"
        self.b_dir = self.tmp / "b"
        self.c_dir = self.tmp / "c"
        for d in (self.a_dir, self.b_dir, self.c_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.db = Database(str(self.tmp / "bridge.db"))
        config = Mock(spec=AppConfig)
        config.a_folders = [str(self.a_dir)]
        config.a_b_mappings = [
            ABMapping(mapping_id="test_m1", a_root=str(self.a_dir), b_root=str(self.b_dir))]
        config.paths = Mock()
        config.paths.b_root = str(self.b_dir)
        config.paths.c_root = str(self.c_dir)
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.strm_engine_paths = []
        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(config, self.db, Mock())

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_pair(self, webdav: str, name: str = "a.strm", show: str = "show") -> Path:
        a_src = self.a_dir / show / "Season 01" / name
        a_src.parent.mkdir(parents=True, exist_ok=True)
        a_src.write_text(webdav, encoding="utf-8")
        self.db.upsert_a(str(a_src), webdav, f"/cloud/{show}")
        b_file = self.b_dir / show / "Season 01" / name
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text(webdav, encoding="utf-8")
        self.db.upsert_b(
            str(b_file), webdav, f"/cloud/{show}", str(a_src),
            mapping_id="test_m1", fingerprint=make_strm_fingerprint(webdav))
        return b_file

    def test_wave2_store_receives_wave1_stat_and_resolved(self):
        """A2：预检放行行的 store 调用携带 Wave1 的 stat_result/_resolved_target"""
        w1 = "/cloud/show/S01E01.mp4"
        b1 = self._seed_pair(w1)
        disk_data = self.app._scan_b_disk()
        db_records = self.db.get_all_b_records()
        processed: set[str] = set()
        self.app.ensure_scan_mapping_roots()
        try:
            with patch.object(
                    self.app, "_store_valid_lineage_snapshot",
                    wraps=self.app._store_valid_lineage_snapshot) as store:
                self.app._reconcile_b_historical_records(disk_data, db_records, processed)
            store.assert_called_once()
            kwargs = store.call_args.kwargs
            assert kwargs.get("buffered") is True
            assert kwargs.get("stat_result") is not None, "Wave2 应复用 Wave1 的 stat"
            assert kwargs.get("_resolved_target") is not None, \
                "Wave2 应复用 Wave1 的 resolved target"
        finally:
            self.app.clear_scan_mapping_roots()

    def test_store_default_no_kwargs_unchanged(self):
        """A2：缺省（不传新参）→ 零行为变化（旧 stat/resolve 路径）"""
        w1 = "/cloud/show/S01E01.mp4"
        b1 = self._seed_pair(w1)
        self.app.ensure_scan_mapping_roots()
        try:
            self.app._store_valid_lineage_snapshot(str(b1), make_strm_fingerprint(w1))
            snap = self.db.get_b_lineage_snapshot("test_m1", str(b1))
            assert snap is not None and snap.validation_state == "valid"
        finally:
            self.app.clear_scan_mapping_roots()

    def test_store_rejects_mapping_id_kwarg(self):
        """A2 红线：禁 mapping_id 形参——传 mapping_id 必须 TypeError"""
        w1 = "/cloud/show/S01E01.mp4"
        b1 = self._seed_pair(w1)
        self.app.ensure_scan_mapping_roots()
        try:
            with pytest.raises(TypeError):
                self.app._store_valid_lineage_snapshot(
                    str(b1), make_strm_fingerprint(w1), mapping_id="test_m1")
        finally:
            self.app.clear_scan_mapping_roots()

    def test_pass1_t5_resolved_target_passthrough(self):
        """N3：pass1 对已通过 exists 门与过滤门的行，fast mapping 收到
        非 None 的 _resolved_target（T5 透传，消除重复 resolve）"""
        w1 = "/cloud/show/S01E01.mp4"
        self._seed_pair(w1)
        # setup_method 里 SyncService 被 patch 成 mock——此处换回真实实现，
        # 否则 scan_a_to_b_full_sync 委托转发落到 mock 上（pass1 不执行）
        from domain.sync.sync_service import SyncService
        real_sync = SyncService(self.app)
        seen_targets = []
        orig = self.app._get_mapping_for_a_fast

        def spy(local_path, **kwargs):
            seen_targets.append(kwargs.get("_resolved_target"))
            return orig(local_path, **kwargs)

        # use_bulk=False 走 pass1/pass2 逐条路径
        with patch.object(self.app, "_get_mapping_for_a_fast", side_effect=spy):
            real_sync.scan_a_to_b_full_sync(valid_engine_paths=None, use_bulk=False)
        assert seen_targets, "pass1 未调用 fast mapping"
        assert all(t is not None for t in seen_targets), \
            f"pass1 存在未透传 _resolved_target 的调用: {seen_targets}"

    def test_build_b_path_from_a_uses_resolved_kwarg_without_recompute(self):
        """N3 兑现（c.9.2 Task 6）：`_a_local_resolved` 透传后方法内部不得
        再对首参 resolve——首参故意用解析后必然越出 A 根的伪路径，实现若
        仍走 `Path(a_local_path).resolve()` 则 relative_to 抛 ValueError（红）；
        透传命中时结果与 resolved 的相对路径一致（绿）。"""
        a_src = self.a_dir / "show" / "Season 01" / "e1.strm"
        a_src.parent.mkdir(parents=True, exist_ok=True)
        a_src.write_text("/cloud/show/S01E01.mp4", encoding="utf-8")
        resolved = a_src.resolve()
        bogus = self.tmp / "outside_a_root" / "e1.strm"
        b_path = self.app.build_b_path_from_a(
            bogus, "/cloud/show/S01E01.mp4",
            a_root=self.a_dir, b_root=self.b_dir,
            _a_local_resolved=resolved)
        assert b_path == self.b_dir / resolved.relative_to(self.a_dir)

    def test_pass1_build_b_path_receives_resolved_kwarg(self):
        """N3 兑现（c.9.2 Task 6）：pass1 两处 build_b_path_from_a 调用
        传入 `_a_local_resolved=resolved_target`（非 skip 行消除第二段
        resolve）；仅关键字形参对其余 6 处默认调用方向后兼容。"""
        # 只播种 A 侧（无 B 行/无同指纹）→ pass1 不被 skip_fp 拦截，
        # 必然走到 build_b_path_from_a
        w1 = "/cloud/fresh/S01E01.mp4"
        a_src = self.a_dir / "fresh" / "Season 01" / "new1.strm"
        a_src.parent.mkdir(parents=True, exist_ok=True)
        a_src.write_text(w1, encoding="utf-8")
        self.db.upsert_a(str(a_src), w1, "/cloud/fresh")

        from domain.sync.sync_service import SyncService
        real_sync = SyncService(self.app)
        kwarg_seen: list = []
        orig = self.app.build_b_path_from_a

        def spy(*args, **kwargs):
            kwarg_seen.append(kwargs.get("_a_local_resolved"))
            return orig(*args, **kwargs)

        with patch.object(self.app, "build_b_path_from_a", side_effect=spy):
            real_sync.scan_a_to_b_full_sync(valid_engine_paths=None, use_bulk=False)
        assert kwarg_seen, "pass1 未调用 build_b_path_from_a"
        # pass1 两处必须传 _a_local_resolved（任一命中即证）；kwarg_seen 中
        # 为 None 的条目来自 pass2/其他调用方——按计划有意保持默认（入参未
        # resolve，需方法内解析），不纳入断言。
        assert any(k is not None for k in kwarg_seen), \
            f"pass1 未传 _a_local_resolved（全部调用 kwarg=None）: {kwarg_seen}"
