"""tmdb_watchlist.py 纯逻辑单元测试。

tmdb_watchlist 模块唯一既有引用 test_tmdb_api.py 被 conftest
collect_ignore_glob 排除出 pytest 收集（属手动真实服务器脚本），模块
pytest 覆盖为零。本文件补齐可离线执行的纯逻辑契约：

- TmdbItem.all_names：标题/原名/别名/原始 titles 的聚合、去重与去空白
- export_watchlist_csv：动态表头两分支、utf-8-sig BOM、别名按序 "|"
  连接、空别名写空列

期望值全部由 tmdb_watchlist.py 的 dataclass 与导出函数语义推导。
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tmdb_watchlist import MatchResult, TmdbItem, export_watchlist_csv  # noqa: E402


def _item(**overrides) -> TmdbItem:
    base = dict(
        media_type="movie", tmdb_id=313369, title="海王",
        original_title="Aquaman", release_date="2018-12-07",
        aliases={"aqualad"}, titles=[" 亚特兰蒂斯之王 "],
    )
    base.update(overrides)
    return TmdbItem(**base)


def _result(item, status="已收录"):
    return MatchResult(tmdb=item, status=status, matched_media=item.title)


def _rows(out) -> list[list[str]]:
    return list(csv.reader(out.read_text(encoding="utf-8-sig").splitlines()))


class TestAllNames:
    def test_aggregates_title_original_aliases_and_titles_list(self):
        assert _item().all_names == {
            "海王", "Aquaman", "aqualad", "亚特兰蒂斯之王"}

    def test_strips_whitespace_and_dedupes(self):
        item = _item(title=" 凡人修仙传 ", original_title="凡人修仙传",
                     aliases={"凡人修仙传"}, titles=[" 凡人修仙传 "])
        assert item.all_names == {"凡人修仙传"}

    def test_blank_entries_are_dropped(self):
        item = _item(title="", original_title="  ",
                     aliases=set(), titles=["", " "])
        assert item.all_names == set()


class TestExportWatchlistCsv:
    def test_utf8_sig_bom_is_written(self, tmp_path):
        out = tmp_path / "w.csv"
        export_watchlist_csv([_result(_item())], out)
        assert out.read_bytes().startswith(b"\xef\xbb\xbf"), (
            "导出必须带 utf-8-sig BOM（Excel 兼容）")

    def test_dynamic_header_with_alias_column(self, tmp_path):
        out = tmp_path / "w.csv"
        export_watchlist_csv([_result(_item())], out)
        rows = _rows(out)
        assert rows[0] == ["状态", "TMDB ID", "类型", "标题", "原标题",
                           "发布日期", "别名"]
        assert rows[1][:6] == ["已收录", "313369", "movie", "海王",
                               "Aquaman", "2018-12-07"]
        assert rows[1][6] == "aqualad"

    def test_aliases_sorted_and_joined_by_pipe(self, tmp_path):
        out = tmp_path / "w.csv"
        export_watchlist_csv(
            [_result(_item(aliases={"海王2", "aqua king", "咸水王"}))], out)
        rows = _rows(out)
        assert rows[1][6] == "|".join(sorted({"海王2", "aqua king", "咸水王"}))

    def test_header_without_alias_column_when_no_aliases(self, tmp_path):
        out = tmp_path / "w.csv"
        export_watchlist_csv([_result(_item(aliases=set()))], out)
        rows = _rows(out)
        assert rows[0] == ["状态", "TMDB ID", "类型", "标题", "原标题",
                           "发布日期"]
        assert len(rows[1]) == 6

    def test_mixed_results_keep_empty_alias_cell(self, tmp_path):
        out = tmp_path / "w.csv"
        results = [_result(_item()),
                   _result(_item(aliases=set()), status="未收录")]
        export_watchlist_csv(results, out)
        rows = _rows(out)
        assert rows[2][6] == "", "任一结果含别名时全表带别名列，无别名行写空单元格"
