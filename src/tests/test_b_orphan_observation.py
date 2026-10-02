"""缺陷 D：B 区孤儿只读观测 + X-8/U-1 零组信号行升 INFO（c7 §三，红测先行）。

- `get_state_summary` 新增只读字段 `b_orphan_count`（dashboard 可见，不动盘）；
  R-6 纪律：孤儿 COUNT 不得进 `_state_lock`（锁外计算 + ≥60s TTL 缓存 +
  异常回退 None）。
- NULL 三值逻辑防御：两侧各加 `webdav_path IS NOT NULL`，防 NOT IN 恒
  UNKNOWN → 计数静默归零假象（v7）。
- U-1（F-7 精确文案）：零组信号行 `[启动清扫] 无重复指纹组，跳过清扫`
  DEBUG → INFO——INFO 日志下 X-8 观测必须能区分"清扫 0 组"与"清扫未运行"。
- 登记语义：启动不清理孤儿为有意设计（不动盘）。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app_service_core import AppService  # noqa: E402
from config import ABMapping, AppConfig  # noqa: E402


class _OrphanBase:
    def _make_app(self):
        config = Mock(spec=AppConfig)
        config.a_folders = []
        config.a_b_mappings = [
            ABMapping(mapping_id="m1", a_root="C:/nonexistent_a", b_root="C:/nonexistent_b")]
        config.paths = Mock()
        config.paths.b_root = "C:/nonexistent_b"
        config.paths.c_root = "C:/nonexistent_c"
        config.paths.strm_engine_paths = []
        config.behavior = Mock()
        config.behavior.ghost_protect_seconds = 300
        config.behavior.sync_on_startup_wait = 0
        config.behavior.sync_on_startup = False
        config.strm_engine_paths = []
        db = MagicMock()
        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SyncService"), \
             patch("app_service_core.SubtitleHandler"):
            app = AppService(config, db, Mock())
        return app, db


class TestBOrphanCountObservation(_OrphanBase):
    """b_orphan_count 只读观测契约。"""

    def test_state_summary_exposes_b_orphan_count(self):
        app, db = self._make_app()
        db.count_b_orphans.return_value = 7
        summary = app.get_state_summary()
        assert summary["b_orphan_count"] == 7

    def test_db_error_returns_none_not_crash(self):
        app, db = self._make_app()
        app._b_orphan_cache = None
        db.count_b_orphans.side_effect = RuntimeError("db gone")
        assert app.get_state_summary()["b_orphan_count"] is None

    def test_ttl_cache_single_db_call(self):
        """R-6：60s TTL 内 dashboard 高频轮询只触发一次 DB 查询"""
        app, db = self._make_app()
        db.count_b_orphans.return_value = 3
        app.get_state_summary()
        app.get_state_summary()
        app.get_state_summary()
        assert db.count_b_orphans.call_count == 1

    def test_orphan_count_computed_outside_state_lock(self):
        """R-6 红线：孤儿 COUNT 查询不得在 _state_lock 持锁期间发起

        c.9.2 Task 5 复活：补 `== 0` 断言——若将 COUNT 移回 _state_lock 内，
        fake 抛出的 AssertionError 会被 _get_b_orphan_count 的
        `except Exception: return None` 吞成 None，无此断言则回归照样绿。
        """
        app, db = self._make_app()
        lock_held = {"v": False}

        def fake_count():
            # _state_lock 是 threading.Lock（非可重入）：若在锁内调用，
            # 再取锁即死锁/抛 RuntimeError，这里以探测锁状态等价判定
            acquired = app._state_lock.acquire(blocking=False)
            if acquired:
                app._state_lock.release()
                return 0
            raise AssertionError("count_b_orphans 在 _state_lock 持锁期间被调用")

        db.count_b_orphans.side_effect = fake_count
        app._b_orphan_cache = None
        summary = app.get_state_summary()
        assert summary["b_orphan_count"] == 0, (
            f"回归场景：锁内异常被吞成 None 时该断言转红，实际值 {summary['b_orphan_count']}")

    def test_db_sql_has_null_defense(self):
        """NULL 三值逻辑防御：SQL 必须含两侧 webdav_path IS NOT NULL"""
        import inspect
        from database import Database
        src = inspect.getsource(Database.count_b_orphans)
        # docstring 亦含该词，故以 SELECT 段为准确口径
        sql_part = src[src.find("SELECT"):]
        assert sql_part.count("webdav_path IS NOT NULL") == 2


class TestStartupSweepZeroGroupsSignal(_OrphanBase):
    """U-1：零组信号行 DEBUG→INFO（X-8 人工门基准）。"""

    def test_zero_groups_signal_is_info_with_exact_text(self, caplog):
        app, db = self._make_app()
        db.get_duplicate_fingerprint_groups.return_value = []
        with caplog.at_level(logging.INFO):
            app._cleanup_startup_duplicates()
        messages = [r.message for r in caplog.records
                    if r.levelno >= logging.INFO]
        assert "[启动清扫] 无重复指纹组，跳过清扫" in messages, messages
        # 确认不再是 DEBUG（INFO 级下可见即为升级行为本身，上面断言已覆盖）

    def test_zero_groups_does_not_quarantine(self):
        """零组 → 不触发批量隔离（不动盘）"""
        app, db = self._make_app()
        db.get_duplicate_fingerprint_groups.return_value = []
        with patch.object(app, "_quarantine_duplicate_groups_batch") as q:
            app._cleanup_startup_duplicates()
        q.assert_not_called()
