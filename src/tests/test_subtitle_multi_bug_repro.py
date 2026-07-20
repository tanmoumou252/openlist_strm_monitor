"""
回归测试：番剧同集多字幕场景下 _process_anime_subtitle 不再触发 NameError。

复现计划 BUG-01：当番剧目录中同一集有多个字幕文件（如 .ass + .srt）且
process_subtitle_group 成功返回映射时，修复前 lang_info 从未定义，
后续 upsert_subtitle 引用 lang_info[0] 触发 NameError，字幕永远不会写入 DB。

运行方式：
  pytest src/tests/test_subtitle_multi_bug_repro.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import Database
from domain.media.subtitle_handler import SubtitleHandler
from _test_helpers import build_mock_app


def _make_app(tmp_path: Path) -> MagicMock:
    return build_mock_app(tmp_path, setup_b_root=True)


class TestAnimeMultiSubtitleNoNameError:
    """验证多字幕成功映射分支不再 NameError。"""

    @pytest.fixture
    def real_db(self, tmp_path: Path) -> Database:
        db = Database(str(tmp_path / "test.db"))
        db.init_subtitle_table()
        return db

    def test_multi_subtitle_same_episode_writes_db(self, tmp_path: Path, real_db: Database):
        """同集 .ass + .srt 两个字幕 → 两个都应写入 DB，无 NameError。"""
        app = _make_app(tmp_path)
        a_root = tmp_path / "a"
        a_root.mkdir()
        app.get_a_root_for_path.return_value = a_root
        # 使用真实 DB 以验证 lang_code 被正确写入
        app.db = real_db

        show_dir = a_root / "番剧" / "ShowName"
        show_dir.mkdir(parents=True)
        sub_ass = show_dir / "S01E01.sc.ass"
        sub_srt = show_dir / "S01E01.chs.srt"
        sub_ass.write_text("content", encoding="utf-8")
        sub_srt.write_text("content", encoding="utf-8")

        handler = SubtitleHandler(app)
        # 修复前：这里会抛 NameError（被 _safe_call 吞掉，DB 不写入）
        handler._process_anime_subtitle(sub_ass, a_root, "fp_test")
        handler._process_anime_subtitle(sub_srt, a_root, "fp_test")

        # 两个字幕都应写入 DB（修复前一个都不会写入）
        rec_ass = real_db.get_subtitle_by_local(str(sub_ass))
        rec_srt = real_db.get_subtitle_by_local(str(sub_srt))
        assert rec_ass is not None, "ass 字幕未写入 DB（可能触发 NameError）"
        assert rec_srt is not None, "srt 字幕未写入 DB（可能触发 NameError）"
        # lang_code 应被正确写入（非 None 表示 lang_info 被正确定义）
        assert rec_ass.lang_code is not None
        assert rec_srt.lang_code is not None

    def test_multi_subtitle_target_files_created(self, tmp_path: Path, real_db: Database):
        """同集多字幕 → B 区目标文件实际生成。"""
        app = _make_app(tmp_path)
        a_root = tmp_path / "a"
        a_root.mkdir()
        app.get_a_root_for_path.return_value = a_root
        app.db = real_db

        show_dir = a_root / "番剧" / "ShowName"
        show_dir.mkdir(parents=True)
        sub = show_dir / "S01E01.sc.ass"
        sub.write_text("content", encoding="utf-8")
        # 同集第二个字幕（不同扩展名）触发多字幕分支
        (show_dir / "S01E01.chs.srt").write_text("content", encoding="utf-8")

        handler = SubtitleHandler(app)
        handler._process_anime_subtitle(sub, a_root, "fp_test")

        rec = real_db.get_subtitle_by_local(str(sub))
        assert rec is not None
        assert Path(rec.target_path).exists(), "B 区目标文件未生成"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
