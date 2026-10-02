"""N1-alt：boundary 二次盘扫惰性差集化（c7 §5.2，红测先行）。

三用例：
1. U-2：`_load_b_db_records` 返回 None → 零磁盘读 + 直接 return（不回退全读）
2. 差集为空 → 零内容读（`_scan_b_disk` 不被调用）
3. 差集 delta 与全读 delta 逐项一致（含解析失败文件丢弃语义）

复用 test_app_service_lifecycle.py 的 _LifecycleBase 构造（tmp A/B/C 根 +
mock db）。枚举失败回退全读路径经 `_enumerate_b_strm_paths` 返回 False 触发。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_app_service_lifecycle import _LifecycleBase


class TestBoundaryLazyDiff(_LifecycleBase):
    """N1-alt 惰性差集化行为契约。"""

    def _write_strm(self, rel: str, content: str) -> Path:
        p = self.b_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def _db_record(self, local_path: str) -> MagicMock:
        rec = MagicMock()
        rec.local_path = local_path
        return rec

    def test_db_load_none_skips_disk_scan(self):
        """U-2：_load_b_db_records None → 零磁盘读 + 直接 return"""
        self._write_strm("x.strm", "/dav/x.mp4")
        with patch.object(self.app, "_scan_b_disk") as scan, \
             patch.object(self.app, "_load_b_db_records", return_value=None):
            self.app._reconcile_boundary_catch_up()
        scan.assert_not_called()
        assert self.app.get_state_summary()["catch_up"]["pending_candidates"] == 0

    def test_empty_diff_zero_content_read(self):
        """差集为空 → 不做内容读（_scan_b_disk 零调用）"""
        p = self._write_strm("x.strm", "/dav/x.mp4")
        with patch.object(self.app, "_scan_b_disk") as scan, \
             patch.object(self.app, "_load_b_db_records",
                          return_value=[self._db_record(str(p))]):
            self.app._reconcile_boundary_catch_up()
        scan.assert_not_called()
        assert self.app.get_state_summary()["catch_up"]["pending_candidates"] == 0

    def test_diff_equals_full_scan_delta(self):
        """差集 delta 与全读 delta 逐项一致（含解析失败文件丢弃）"""
        indexed = self._write_strm("Indexed/indexed.strm", "/dav/indexed.mp4")
        late1 = self._write_strm("Movie/late1.strm", "/dav/late1.mp4")
        self._write_strm("Movie/late2.strm", "/dav/late2.mp4")
        self._write_strm("Movie/empty.strm", "")  # 空内容 → 解析失败丢弃
        with patch.object(self.app, "_load_b_db_records",
                          return_value=[self._db_record(str(indexed))]):
            # N1-alt 差集路径
            self.app._reconcile_boundary_catch_up()
            lazy_delta = sorted(d["local_path"] for d in self.app._catch_up_delta)
            self.app._catch_up_delta = []
            # 枚举失败 → 回退现行全读基线
            with patch.object(self.app, "_enumerate_b_strm_paths",
                              return_value=([], False)):
                self.app._reconcile_boundary_catch_up()
            full_delta = sorted(d["local_path"] for d in self.app._catch_up_delta)
        assert lazy_delta == full_delta
        assert lazy_delta == sorted([str(late1), str(self.b_dir / "Movie" / "late2.strm")])

    def test_enum_failure_falls_back_to_full_scan(self):
        """仅 rglob 枚举异常 → 回退现行全读（_scan_b_disk 被调用）"""
        self._write_strm("x.strm", "/dav/x.mp4")
        with patch.object(self.app, "_scan_b_disk",
                          return_value=({}, {})) as scan, \
             patch.object(self.app, "_load_b_db_records", return_value=[]), \
             patch.object(self.app, "_enumerate_b_strm_paths",
                          return_value=([], False)):
            self.app._reconcile_boundary_catch_up()
        scan.assert_called_once()
