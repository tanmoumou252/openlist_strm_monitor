"""subset_font.py 单元测试

测试范围（纯函数 / 结构校验，不依赖开发机固定的 C:\\Windows\\Fonts）：
- 参数解析：``parse_args`` 默认值与覆盖
- Unicode 集合运算：``static_codepoints``、``merge_codepoints``、
  ``parse_unicode_range_str``、``format_unicode_ranges`` 往返一致性
- 网页字符扫描：``is_scannable_char`` 排除控制字符、
  ``scan_text_codepoints``、``iter_web_source_files`` 扫描范围边界
- 缺字来源区分：``classify_missing`` 分辨"源字体缺字"（WARNING）与
  "子集丢字"（ERROR）
- CSS 一致性：``extract_css_unicode_range``、``verify_css_declaration``、
  ``build_css_unicode_range``
- 输出 cmap 校验与失败退出码：``read_font_cmap``、``main``

字体相关测试使用 fontTools 现场构造的最小可变字体，或跳过（缺 fontTools 时），
不依赖仓库内置字体，也不把源字体写入仓库。

运行方式：
  python -m pytest src/tests/test_subset_font.py -v
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

_SCRIPT = (Path(__file__).resolve().parent.parent
           / "webui" / "scripts" / "subset_font.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("subset_font_under_test", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sf = _load_module()

fontTools = pytest.importorskip("fontTools", reason="字体结构测试需要 fontTools")


# ============================================================
# 参数解析
# ============================================================

class TestParseArgs:
    def test_defaults(self):
        args = sf.parse_args([])
        assert args.source_kr is None
        assert args.no_scan is False
        assert args.unicodes is None
        assert args.weights == "300-700"

    def test_default_output_is_webui_fonts_dir(self):
        args = sf.parse_args([])
        assert Path(args.output).name == "fonts"
        assert Path(args.output).parent.name == "assets"

    def test_default_webui_dir_points_to_source_tree(self):
        args = sf.parse_args([])
        assert (Path(args.webui_dir) / "modules").exists()

    def test_source_override(self):
        args = sf.parse_args(["--source", "X:/font.ttf"])
        assert args.source == "X:/font.ttf"

    def test_output_override(self):
        args = sf.parse_args(["--output", "X:/out"])
        assert args.output == "X:/out"

    def test_source_kr_override(self):
        args = sf.parse_args(["--source-kr", "X:/kr.ttf"])
        assert args.source_kr == "X:/kr.ttf"

    def test_no_scan_flag(self):
        assert sf.parse_args(["--no-scan"]).no_scan is True

    def test_unicodes_override(self):
        args = sf.parse_args(["--unicodes", "U+2190"])
        assert args.unicodes == "U+2190"

    def test_css_override(self):
        args = sf.parse_args(["--css", "X:/main.css"])
        assert args.css == "X:/main.css"

    def test_unknown_argument_exits(self):
        with pytest.raises(SystemExit):
            sf.parse_args(["--not-a-flag"])


# ============================================================
# 静态区块
# ============================================================

class TestStaticCodepoints:
    def test_includes_ascii_space(self):
        assert 0x0020 in sf.static_codepoints()

    def test_includes_cjk_basic(self):
        assert 0x4E2D in sf.static_codepoints()  # 中

    def test_includes_kana(self):
        assert 0x3042 in sf.static_codepoints()  # ぁ

    def test_includes_latin1_supplement(self):
        """Latin-1 补充区块必须被覆盖，CSS 声明了 U+0020-00FF。"""
        cps = sf.static_codepoints()
        assert 0x00B7 in cps  # ·
        assert 0x00D7 in cps  # ×
        assert 0x00E9 in cps  # é

    def test_excludes_hangul_from_sc(self):
        assert 0xAC00 not in sf.static_codepoints(("SC",))

    def test_kr_tag_selects_hangul(self):
        kr = sf.static_codepoints(("KR",))
        assert 0xAC00 in kr
        assert 0x4E2D not in kr

    def test_blocks_have_four_fields(self):
        for entry in sf.STATIC_BLOCKS:
            assert len(entry) == 4

    def test_ranges_alias_kept_for_compat(self):
        assert sf.RANGES is sf.STATIC_BLOCKS


# ============================================================
# Unicode 范围串解析与格式化
# ============================================================

class TestParseUnicodeRangeStr:
    def test_single_codepoint(self):
        assert sf.parse_unicode_range_str("U+2190") == {0x2190}

    def test_range(self):
        assert sf.parse_unicode_range_str("U+0020-0022") == {0x20, 0x21, 0x22}

    def test_comma_separated(self):
        assert sf.parse_unicode_range_str("U+2190,U+2192") == {0x2190, 0x2192}

    def test_css_style_space_separated(self):
        result = sf.parse_unicode_range_str("U+0020-0021, U+00B7")
        assert result == {0x20, 0x21, 0xB7}

    def test_lowercase_hex_accepted(self):
        assert sf.parse_unicode_range_str("u+00b7") == {0xB7}

    def test_trailing_semicolon_tolerated(self):
        assert sf.parse_unicode_range_str("U+00B7;") == {0xB7}

    def test_reversed_range_is_normalized(self):
        assert sf.parse_unicode_range_str("U+0022-0020") == {0x20, 0x21, 0x22}

    def test_empty_string_returns_empty(self):
        assert sf.parse_unicode_range_str("") == set()

    def test_garbage_tokens_ignored(self):
        assert sf.parse_unicode_range_str("nonsense,U+00B7") == {0xB7}


class TestFormatUnicodeRanges:
    def test_empty_set(self):
        assert sf.format_unicode_ranges(set()) == ""

    def test_single_codepoint(self):
        assert sf.format_unicode_ranges({0xB7}) == "U+00B7"

    def test_contiguous_run_is_collapsed(self):
        assert sf.format_unicode_ranges({0x20, 0x21, 0x22}) == "U+0020-0022"

    def test_disjoint_runs(self):
        result = sf.format_unicode_ranges({0x20, 0x21, 0xB7})
        assert result == "U+0020-0021,U+00B7"

    def test_custom_separator(self):
        result = sf.format_unicode_ranges({0x20, 0xB7}, separator=", ")
        assert result == "U+0020, U+00B7"

    def test_round_trip_is_lossless(self):
        original = {0x20, 0x21, 0x22, 0xB7, 0x4E2D, 0x4E2E}
        assert sf.parse_unicode_range_str(
            sf.format_unicode_ranges(original)) == original

    def test_round_trip_on_static_blocks(self):
        original = sf.static_codepoints()
        assert sf.parse_unicode_range_str(
            sf.format_unicode_ranges(original)) == original


class TestMergeCodepoints:
    def test_merges_multiple_groups(self):
        assert sf.merge_codepoints({1}, {2}, {3}) == {1, 2, 3}

    def test_none_is_treated_as_empty(self):
        assert sf.merge_codepoints({1}, None) == {1}

    def test_no_args_returns_empty(self):
        assert sf.merge_codepoints() == set()


# ============================================================
# 字符扫描
# ============================================================

class TestIsScannableChar:
    @pytest.mark.parametrize("ch", ["a", " ", "中", "·", "→", "▾", "，"])
    def test_displayable_chars_kept(self, ch):
        assert sf.is_scannable_char(ch) is True

    @pytest.mark.parametrize("ch", ["\n", "\r", "\t", "\x00", "\x1b"])
    def test_control_chars_rejected(self, ch):
        assert sf.is_scannable_char(ch) is False

    def test_variation_selector_rejected(self):
        """VARIATION SELECTOR-16 无可见字形，不应成为硬性覆盖要求。"""
        assert sf.is_scannable_char("\ufe0f") is False

    def test_zero_width_joiner_rejected(self):
        assert sf.is_scannable_char("\u200d") is False

    def test_empty_string_rejected(self):
        assert sf.is_scannable_char("") is False

    def test_unassigned_codepoint_rejected(self):
        assert sf.is_scannable_char("\u0378") is False


class TestScanTextCodepoints:
    def test_extracts_unique_codepoints(self):
        assert sf.scan_text_codepoints("aab") == {ord("a"), ord("b")}

    def test_drops_newlines_and_tabs(self):
        assert sf.scan_text_codepoints("a\n\tb") == {ord("a"), ord("b")}

    def test_keeps_space(self):
        assert 0x20 in sf.scan_text_codepoints("a b")

    def test_keeps_cjk_and_symbols(self):
        result = sf.scan_text_codepoints("中→▾")
        assert result == {0x4E2D, 0x2192, 0x25BE}

    def test_empty_text_returns_empty(self):
        assert sf.scan_text_codepoints("") == set()


class TestIterWebSourceFiles:
    def _tree(self, tmp_path):
        (tmp_path / "index.html").write_text("<p>首页</p>", encoding="utf-8")
        (tmp_path / "main.js").write_text("// 入口", encoding="utf-8")
        (tmp_path / "public").mkdir()
        (tmp_path / "public" / "icon-preview.html").write_text(
            "预览", encoding="utf-8")
        (tmp_path / "modules" / "core").mkdir(parents=True)
        (tmp_path / "modules" / "core" / "api.js").write_text(
            "接口", encoding="utf-8")
        (tmp_path / "styles").mkdir()
        (tmp_path / "styles" / "main.css").write_text("样式", encoding="utf-8")
        return tmp_path

    def test_collects_expected_files(self, tmp_path):
        files = sf.iter_web_source_files(self._tree(tmp_path))
        names = {f.name for f in files}
        assert names == {"index.html", "main.js", "icon-preview.html",
                         "api.js", "main.css"}

    def test_excludes_unrelated_extensions(self, tmp_path):
        root = self._tree(tmp_path)
        (root / "strm_bridge.log").write_text("日志正文", encoding="utf-8")
        (root / "bridge.db").write_bytes(b"\x00binary")
        (root / "assets").mkdir()
        (root / "assets" / "logo.svg").write_text("图形", encoding="utf-8")
        names = {f.name for f in sf.iter_web_source_files(root)}
        assert "strm_bridge.log" not in names
        assert "bridge.db" not in names
        assert "logo.svg" not in names

    def test_result_is_deduplicated(self, tmp_path):
        root = self._tree(tmp_path)
        files = sf.iter_web_source_files(
            root, globs=("index.html", "index.html"))
        assert len(files) == 1

    def test_missing_dir_returns_empty(self, tmp_path):
        assert sf.iter_web_source_files(tmp_path / "absent") == []

    def test_directory_matching_glob_is_skipped(self, tmp_path):
        root = tmp_path
        (root / "index.html").mkdir()
        assert sf.iter_web_source_files(root, globs=("index.html",)) == []


class TestScanWebSourceCodepoints:
    def test_scans_nested_modules(self, tmp_path):
        (tmp_path / "modules" / "pages").mkdir(parents=True)
        (tmp_path / "modules" / "pages" / "openlist.js").write_text(
            "const label = 'A↔B 目录映射';", encoding="utf-8")
        codepoints, files = sf.scan_web_source_codepoints(tmp_path)
        assert 0x2194 in codepoints
        assert len(files) == 1

    def test_excludes_newlines(self, tmp_path):
        (tmp_path / "index.html").write_text("a\nb\n", encoding="utf-8")
        codepoints, _ = sf.scan_web_source_codepoints(tmp_path)
        assert 0x0A not in codepoints

    def test_replacement_char_is_dropped(self, tmp_path):
        (tmp_path / "index.html").write_bytes(b"\xff\xfe invalid")
        codepoints, _ = sf.scan_web_source_codepoints(tmp_path)
        assert 0xFFFD not in codepoints

    def test_empty_dir_returns_empty(self, tmp_path):
        codepoints, files = sf.scan_web_source_codepoints(tmp_path)
        assert codepoints == set()
        assert files == []

    def test_real_webui_sources_are_scannable(self):
        """真实 WebUI 源目录必须能扫出字符，防止 glob 与实际结构脱节。"""
        webui_dir = Path(sf.parse_args([]).webui_dir)
        codepoints, files = sf.scan_web_source_codepoints(webui_dir)
        assert len(files) > 5
        assert 0x4E2D in codepoints or len(codepoints) > 100

    def test_real_webui_arrow_chars_are_detected(self):
        """openlist.js 的 A↔B 与 index.html 的 ▾ 必须被扫描发现。"""
        webui_dir = Path(sf.parse_args([]).webui_dir)
        codepoints, _ = sf.scan_web_source_codepoints(webui_dir)
        assert 0x2194 in codepoints
        assert 0x25BE in codepoints


# ============================================================
# 缺字来源区分
# ============================================================

class TestClassifyMissing:
    def test_nothing_missing(self):
        from_source, from_subset = sf.classify_missing({1, 2}, {1, 2}, {1, 2})
        assert from_source == set()
        assert from_subset == set()

    def test_missing_in_source_is_warning(self):
        from_source, from_subset = sf.classify_missing({1, 2}, {1}, {1})
        assert from_source == {2}
        assert from_subset == set()

    def test_missing_only_in_subset_is_error(self):
        from_source, from_subset = sf.classify_missing({1, 2}, {1}, {1, 2})
        assert from_source == set()
        assert from_subset == {2}

    def test_mixed_missing(self):
        from_source, from_subset = sf.classify_missing(
            {1, 2, 3}, {1}, {1, 2})
        assert from_source == {3}
        assert from_subset == {2}

    def test_unknown_source_is_conservative(self):
        """源 cmap 不可读时不得把缺字误判为子集化失败。"""
        from_source, from_subset = sf.classify_missing({1, 2}, {1}, None)
        assert from_source == {2}
        assert from_subset == set()

    def test_extra_glyphs_in_output_are_ignored(self):
        from_source, from_subset = sf.classify_missing({1}, {1, 9}, {1, 9})
        assert from_source == set()
        assert from_subset == set()


class TestDescribeCodepoints:
    def test_names_known_codepoint(self):
        lines = sf.describe_codepoints({0x2192})
        assert lines == ["U+2192 RIGHTWARDS ARROW"]

    def test_unnamed_codepoint_is_labelled(self):
        lines = sf.describe_codepoints({0xE000})
        assert "未命名" in lines[0]

    def test_truncates_and_reports_remainder(self):
        lines = sf.describe_codepoints(set(range(0x4E00, 0x4E00 + 50)), limit=5)
        assert len(lines) == 6
        assert "其余 45" in lines[-1]

    def test_empty_set_returns_empty_list(self):
        assert sf.describe_codepoints(set()) == []


# ============================================================
# CSS 一致性
# ============================================================

_CSS_TEMPLATE = """
@font-face {
  font-family: 'Noto Sans SC';
  src: url('../assets/fonts/NotoSansSC-Subset.woff2') format('woff2');
  unicode-range: %s;
}
@font-face {
  font-family: 'Noto Sans SC';
  src: local('Noto Sans SC Regular');
}
"""


class TestExtractCssUnicodeRange:
    def test_extracts_declared_range(self):
        css = _CSS_TEMPLATE % "U+0020-00FF, U+2192"
        raw = sf.extract_css_unicode_range(css, "NotoSansSC-Subset.woff2")
        assert raw == "U+0020-00FF, U+2192"

    def test_returns_none_for_unknown_font(self):
        css = _CSS_TEMPLATE % "U+0020"
        assert sf.extract_css_unicode_range(css, "Absent.woff2") is None

    def test_returns_none_when_block_has_no_range(self):
        css = """
        @font-face {
          src: url('../assets/fonts/NotoSansSC-Subset.woff2') format('woff2');
        }
        """
        assert sf.extract_css_unicode_range(
            css, "NotoSansSC-Subset.woff2") is None

    def test_ignores_local_fallback_blocks(self):
        css = _CSS_TEMPLATE % "U+0020"
        raw = sf.extract_css_unicode_range(css, "NotoSansSC-Subset.woff2")
        assert raw == "U+0020"


class TestVerifyCssDeclaration:
    def test_missing_css_file_passes(self, tmp_path):
        assert sf.verify_css_declaration(tmp_path / "absent.css", {0x20}) is True

    def test_declaration_within_cmap_passes(self, tmp_path):
        css = tmp_path / "main.css"
        css.write_text(_CSS_TEMPLATE % "U+0020-0021", encoding="utf-8")
        assert sf.verify_css_declaration(css, {0x20, 0x21, 0x22}) is True

    def test_over_declaration_fails(self, tmp_path):
        css = tmp_path / "main.css"
        css.write_text(_CSS_TEMPLATE % "U+0020-0022", encoding="utf-8")
        assert sf.verify_css_declaration(css, {0x20, 0x21}) is False

    def test_no_range_declared_passes(self, tmp_path):
        css = tmp_path / "main.css"
        css.write_text(
            "@font-face { src: url('NotoSansSC-Subset.woff2'); }",
            encoding="utf-8")
        assert sf.verify_css_declaration(css, set()) is True

    def test_real_css_matches_shipped_font(self):
        """仓库内 main.css 的声明不得超出已构建字体的实际 cmap。"""
        webui = Path(sf.parse_args([]).webui_dir)
        css = webui / "styles" / "main.css"
        font = webui / "assets" / "fonts" / "NotoSansSC-Subset.woff2"
        if not css.exists() or not font.exists():
            pytest.skip("仓库缺少 main.css 或已构建字体")
        cmap = sf.read_font_cmap(font)
        assert cmap is not None
        assert sf.verify_css_declaration(css, cmap) is True


class TestBuildCssUnicodeRange:
    def test_only_includes_available_codepoints(self):
        result = sf.build_css_unicode_range({0x20, 0x21}, {0x20, 0x21, 0x22})
        assert result == "U+0020-0021"

    def test_empty_intersection_returns_empty(self):
        assert sf.build_css_unicode_range({0x99}, {0x20}) == ""

    def test_uses_css_friendly_separator(self):
        result = sf.build_css_unicode_range({0x20, 0xB7}, {0x20, 0xB7})
        assert result == "U+0020, U+00B7"


# ============================================================
# 字体读取与端到端子集化
# ============================================================

def _make_variable_font(path: Path, codepoints: list[int]) -> None:
    """用 fontTools 现场构造一个含 fvar 的最小可变字体。"""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    glyph_order = [".notdef"] + [f"uni{cp:04X}" for cp in codepoints]
    cmap = {cp: f"uni{cp:04X}" for cp in codepoints}

    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(glyph_order)
    fb.setupCharacterMap(cmap)

    pen = TTGlyphPen(None)
    pen.moveTo((0, 0))
    pen.lineTo((0, 700))
    pen.lineTo((500, 700))
    pen.lineTo((500, 0))
    pen.closePath()
    box = pen.glyph()
    empty = TTGlyphPen(None).glyph()
    fb.setupGlyf({name: (empty if name == ".notdef" else box)
                  for name in glyph_order})
    fb.setupHorizontalMetrics({name: (600, 50) for name in glyph_order})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "Probe", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    fb.setupFvar(
        axes=[("wght", 300, 400, 700, "Weight")],
        instances=[],
    )
    fb.save(str(path))


@pytest.fixture
def probe_font(tmp_path):
    path = tmp_path / "probe.ttf"
    _make_variable_font(path, [0x41, 0x42, 0x4E2D])
    return path


class TestReadFontCmap:
    def test_reads_codepoints(self, probe_font):
        cmap = sf.read_font_cmap(probe_font)
        assert cmap is not None
        assert {0x41, 0x42, 0x4E2D} <= cmap

    def test_missing_file_returns_none(self, tmp_path):
        assert sf.read_font_cmap(tmp_path / "absent.ttf") is None

    def test_invalid_font_returns_none(self, tmp_path):
        bad = tmp_path / "bad.ttf"
        bad.write_bytes(b"not a font")
        assert sf.read_font_cmap(bad) is None


class TestSubsetFontEndToEnd:
    def test_subset_keeps_requested_codepoints(self, probe_font, tmp_path):
        out = tmp_path / "out.woff2"
        sf.subset_font(str(probe_font), {0x41, 0x4E2D}, str(out), "PROBE")
        cmap = sf.read_font_cmap(out)
        assert cmap is not None
        assert 0x41 in cmap
        assert 0x4E2D in cmap

    def test_subset_drops_unrequested_codepoints(self, probe_font, tmp_path):
        out = tmp_path / "out.woff2"
        sf.subset_font(str(probe_font), {0x41}, str(out), "PROBE")
        cmap = sf.read_font_cmap(out)
        assert cmap is not None
        assert 0x42 not in cmap

    def test_subset_keeps_fvar(self, probe_font, tmp_path):
        from fontTools.ttLib import TTFont
        out = tmp_path / "out.woff2"
        sf.subset_font(str(probe_font), {0x41}, str(out), "PROBE")
        font = TTFont(str(out))
        try:
            assert "fvar" in font
        finally:
            font.close()

    def test_missing_codepoints_in_source_do_not_raise(self, probe_font, tmp_path):
        """源字体缺字必须靠 ignore_missing_unicodes 跳过，不能中断子集化。"""
        out = tmp_path / "out.woff2"
        sf.subset_font(str(probe_font), {0x41, 0x2190}, str(out), "PROBE")
        assert out.exists()

    def test_accepts_range_string(self, probe_font, tmp_path):
        out = tmp_path / "out.woff2"
        sf.subset_font(str(probe_font), "U+0041", str(out), "PROBE")
        cmap = sf.read_font_cmap(out)
        assert cmap is not None and 0x41 in cmap

    def test_non_variable_source_exits(self, tmp_path):
        from fontTools.fontBuilder import FontBuilder
        from fontTools.pens.ttGlyphPen import TTGlyphPen

        static = tmp_path / "static.ttf"
        fb = FontBuilder(1000, isTTF=True)
        fb.setupGlyphOrder([".notdef", "uni0041"])
        fb.setupCharacterMap({0x41: "uni0041"})
        empty = TTGlyphPen(None).glyph()
        fb.setupGlyf({".notdef": empty, "uni0041": empty})
        fb.setupHorizontalMetrics({".notdef": (600, 50), "uni0041": (600, 50)})
        fb.setupHorizontalHeader(ascent=800, descent=-200)
        fb.setupNameTable({"familyName": "Static", "styleName": "Regular"})
        fb.setupOS2()
        fb.setupPost()
        fb.save(str(static))

        with pytest.raises(SystemExit):
            sf.subset_font(str(static), {0x41},
                           str(tmp_path / "out.woff2"), "PROBE")


class TestVerifyOutput:
    def test_full_coverage_passes(self, probe_font, tmp_path):
        out = tmp_path / "out.woff2"
        expected = {0x41, 0x42, 0x4E2D}
        sf.subset_font(str(probe_font), expected, str(out), "PROBE")
        assert sf.verify_output(str(out), expected, "PROBE",
                               str(probe_font)) is True

    def test_source_missing_glyph_is_only_warning(self, probe_font, tmp_path):
        """期望包含源字体没有的字符时应通过（WARNING），不视为失败。"""
        out = tmp_path / "out.woff2"
        expected = {0x41, 0x2190}
        sf.subset_font(str(probe_font), expected, str(out), "PROBE")
        assert sf.verify_output(str(out), expected, "PROBE",
                               str(probe_font)) is True

    def test_subset_dropping_glyph_fails(self, probe_font, tmp_path):
        """源字体有但输出缺失时必须 FAIL。"""
        out = tmp_path / "out.woff2"
        sf.subset_font(str(probe_font), {0x41}, str(out), "PROBE")
        assert sf.verify_output(str(out), {0x41, 0x42}, "PROBE",
                               str(probe_font)) is False

    def test_scan_codepoints_are_verified_individually(self, probe_font, tmp_path):
        out = tmp_path / "out.woff2"
        sf.subset_font(str(probe_font), {0x41}, str(out), "PROBE")
        assert sf.verify_output(
            str(out), {0x41}, "PROBE", str(probe_font),
            scan_codepoints={0x41, 0x42}) is False

    def test_unknown_source_treats_missing_as_warning(self, probe_font, tmp_path):
        out = tmp_path / "out.woff2"
        sf.subset_font(str(probe_font), {0x41}, str(out), "PROBE")
        assert sf.verify_output(str(out), {0x41, 0x42}, "PROBE", None) is True


class TestMainExitCodes:
    def test_missing_source_returns_one(self, tmp_path):
        code = sf.main([
            "--source", str(tmp_path / "absent.ttf"),
            "--output", str(tmp_path / "out"),
            "--webui-dir", str(tmp_path),
            "--css", str(tmp_path / "absent.css"),
            "--no-scan",
        ])
        assert code == 1

    def test_missing_kr_source_returns_one(self, probe_font, tmp_path):
        code = sf.main([
            "--source", str(probe_font),
            "--source-kr", str(tmp_path / "absent_kr.ttf"),
            "--output", str(tmp_path / "out"),
            "--webui-dir", str(tmp_path),
            "--css", str(tmp_path / "absent.css"),
            "--no-scan",
        ])
        assert code == 1

    def test_successful_run_returns_zero(self, tmp_path):
        source = tmp_path / "wide.ttf"
        # 覆盖 SC 静态区块中会被检查的少量码位即可：源缺字只报 WARNING
        _make_variable_font(source, [0x41, 0x4E2D])
        out_dir = tmp_path / "out"
        webui = tmp_path / "webui"
        webui.mkdir()
        (webui / "index.html").write_text("中A", encoding="utf-8")
        code = sf.main([
            "--source", str(source),
            "--output", str(out_dir),
            "--webui-dir", str(webui),
            "--css", str(tmp_path / "absent.css"),
        ])
        assert code == 0
        assert (out_dir / "NotoSansSC-Subset.woff2").exists()

    def test_css_over_declaration_fails_run(self, tmp_path):
        source = tmp_path / "wide.ttf"
        _make_variable_font(source, [0x41])
        css = tmp_path / "main.css"
        css.write_text(_CSS_TEMPLATE % "U+2190", encoding="utf-8")
        webui = tmp_path / "webui"
        webui.mkdir()
        (webui / "index.html").write_text("A", encoding="utf-8")
        code = sf.main([
            "--source", str(source),
            "--output", str(tmp_path / "out"),
            "--webui-dir", str(webui),
            "--css", str(css),
        ])
        assert code == 1


class TestResolveScCodepoints:
    def test_scan_results_are_merged(self, tmp_path):
        webui = tmp_path / "webui"
        webui.mkdir()
        (webui / "index.html").write_text("↔", encoding="utf-8")
        args = sf.parse_args(["--webui-dir", str(webui)])
        expected, scanned = sf.resolve_sc_codepoints(args)
        assert 0x2194 in scanned
        assert 0x2194 in expected

    def test_static_blocks_always_included(self, tmp_path):
        webui = tmp_path / "webui"
        webui.mkdir()
        args = sf.parse_args(["--webui-dir", str(webui)])
        expected, _ = sf.resolve_sc_codepoints(args)
        assert 0x4E2D in expected

    def test_no_scan_skips_scanning(self, tmp_path):
        webui = tmp_path / "webui"
        webui.mkdir()
        (webui / "index.html").write_text("↔", encoding="utf-8")
        args = sf.parse_args(["--webui-dir", str(webui), "--no-scan"])
        expected, scanned = sf.resolve_sc_codepoints(args)
        assert scanned == set()
        assert 0x2194 not in expected

    def test_hangul_excluded_from_sc_subset(self, tmp_path):
        webui = tmp_path / "webui"
        webui.mkdir()
        args = sf.parse_args(["--webui-dir", str(webui), "--no-scan"])
        expected, _ = sf.resolve_sc_codepoints(args)
        assert 0xAC00 not in expected

    def test_extra_unicodes_flag_is_merged(self, tmp_path):
        webui = tmp_path / "webui"
        webui.mkdir()
        args = sf.parse_args([
            "--webui-dir", str(webui), "--no-scan", "--unicodes", "U+2190"])
        expected, _ = sf.resolve_sc_codepoints(args)
        assert 0x2190 in expected


# ============================================================
# 静态 icon preview 与 icons.js 的一致性
# ============================================================

_ICON_KEY_RE = re.compile(r"^\s*'([a-z0-9_]+)':\s*'", re.M)
_PREVIEW_KEY_RE = re.compile(r'data-icon="([a-z0-9_]+)"')


def _webui_dir() -> Path:
    return Path(sf.parse_args([]).webui_dir)


def _icons_js_keys() -> list[str]:
    text = (_webui_dir() / "modules" / "core" / "icons.js").read_text(
        encoding="utf-8")
    body = text.split("export const ICONS = {", 1)[1].split("\n};", 1)[0]
    return _ICON_KEY_RE.findall(body)


def _js_set(name: str) -> set[str]:
    text = (_webui_dir() / "modules" / "core" / "icons.js").read_text(
        encoding="utf-8")
    m = re.search(name + r"\s*=\s*new Set\(\[(.*?)\]\)", text, re.S)
    return set(re.findall(r"'([a-z0-9_]+)'", m.group(1))) if m else set()


def _preview_paths() -> list[Path]:
    """源静态页 + 构建产物（若已构建）。"""
    candidates = [_webui_dir() / "public" / "icon-preview.html"]
    dist = _webui_dir().parent.parent / "dist" / "icon-preview.html"
    if dist.exists():
        candidates.append(dist)
    return candidates


class TestIconPreviewParity:
    """icon preview 必须完整包含 ICONS，且用自动比较而非人工目测验收。"""

    def test_icons_js_is_parsable(self):
        keys = _icons_js_keys()
        assert len(keys) > 20
        assert "info" in keys  # icon() 的兜底图标

    def test_icons_js_has_no_duplicate_keys(self):
        keys = _icons_js_keys()
        assert len(keys) == len(set(keys))

    @pytest.mark.parametrize("preview", _preview_paths(), ids=lambda p: p.parent.name)
    def test_preview_covers_all_icons(self, preview):
        rendered = _PREVIEW_KEY_RE.findall(preview.read_text(encoding="utf-8"))
        missing = sorted(set(_icons_js_keys()) - set(rendered))
        assert missing == [], f"icon preview 缺少图标: {missing}"

    @pytest.mark.parametrize("preview", _preview_paths(), ids=lambda p: p.parent.name)
    def test_preview_has_no_unknown_icons(self, preview):
        rendered = _PREVIEW_KEY_RE.findall(preview.read_text(encoding="utf-8"))
        extra = sorted(set(rendered) - set(_icons_js_keys()))
        assert extra == [], f"icon preview 含未知图标: {extra}"

    @pytest.mark.parametrize("preview", _preview_paths(), ids=lambda p: p.parent.name)
    def test_each_icon_appears_exactly_once(self, preview):
        rendered = _PREVIEW_KEY_RE.findall(preview.read_text(encoding="utf-8"))
        dupes = sorted({k for k in rendered if rendered.count(k) > 1})
        assert dupes == [], f"icon preview 重复渲染: {dupes}"

    @pytest.mark.parametrize("preview", _preview_paths(), ids=lambda p: p.parent.name)
    def test_svg_count_matches_icon_count(self, preview):
        text = preview.read_text(encoding="utf-8")
        assert text.count("<svg") == len(_icons_js_keys())

    @pytest.mark.parametrize("preview", _preview_paths(), ids=lambda p: p.parent.name)
    def test_preview_headline_count_matches_icon_count(self, preview):
        """副标题里的图标总数必须等于 ICONS 实际键数。

        回归：该文案曾被从正确的 47 单方面改成 49（依据一份误判"新增了
        loading/lock/arrow_back 所以是 49"的人工审计，而这三个图标本就在 47 之内）。
        test_svg_count_matches_icon_count 只比对 <svg> 数量，抓不到人工文案里的数字。
        """
        text = preview.read_text(encoding="utf-8")
        match = re.search(r"共\s*(\d+)\s*个图标", text)
        assert match is not None, f"{preview} 缺少「共 N 个图标」文案"
        assert int(match.group(1)) == len(_icons_js_keys())

    @pytest.mark.parametrize("preview", _preview_paths(), ids=lambda p: p.parent.name)
    def test_filled_icons_are_tagged(self, preview):
        text = preview.read_text(encoding="utf-8")
        for key in _js_set("FILLED_ICONS"):
            block = text.split(f'data-icon="{key}"', 1)[1].split("</div>\n  </div>", 1)[0]
            assert "tag-filled" in block, f"{key} 缺少 FILLED 标签"

    @pytest.mark.parametrize("preview", _preview_paths(), ids=lambda p: p.parent.name)
    def test_brand_icons_are_tagged(self, preview):
        text = preview.read_text(encoding="utf-8")
        for key in _js_set("BRAND_ICONS"):
            block = text.split(f'data-icon="{key}"', 1)[1].split("</div>\n  </div>", 1)[0]
            assert "tag-brand" in block, f"{key} 缺少 BRAND 标签"

    @pytest.mark.parametrize("preview", _preview_paths(), ids=lambda p: p.parent.name)
    def test_preview_is_fully_static(self, preview):
        """预览页不得依赖 JS 模块或后端接口，必须能直接双击打开。"""
        text = preview.read_text(encoding="utf-8")
        assert "<script" not in text.lower()
        assert "/api/" not in text

    @pytest.mark.parametrize("preview", _preview_paths(), ids=lambda p: p.parent.name)
    def test_preview_supports_both_themes(self, preview):
        text = preview.read_text(encoding="utf-8")
        assert "prefers-color-scheme: dark" in text
        assert "color-scheme" in text

    @pytest.mark.parametrize("preview", _preview_paths(), ids=lambda p: p.parent.name)
    def test_preview_has_failure_placeholder(self, preview):
        """SVG 渲染失败时要有可识别的占位框，而不是空白。"""
        assert "dashed" in preview.read_text(encoding="utf-8")

    @pytest.mark.parametrize("preview", _preview_paths(), ids=lambda p: p.parent.name)
    def test_preview_uses_no_absolute_local_paths(self, preview):
        """独立打开时不能引用开发机绝对路径。"""
        text = preview.read_text(encoding="utf-8")
        assert "C:\\" not in text
        assert "file:///" not in text


class TestDistStaticPages:
    """dist/ 下的静态页面必须能在目录内直接打开。"""

    def _dist(self) -> Path:
        return _webui_dir().parent.parent / "dist"

    def test_dist_index_exists(self):
        dist = self._dist()
        if not dist.exists():
            pytest.skip("dist/ 尚未构建")
        assert (dist / "index.html").exists()

    def test_dist_index_references_existing_assets(self):
        dist = self._dist()
        index = dist / "index.html"
        if not index.exists():
            pytest.skip("dist/ 尚未构建")
        text = index.read_text(encoding="utf-8")
        refs = re.findall(r'(?:src|href)="(\.?/?assets/[^"]+)"', text)
        assert refs, "dist/index.html 未引用任何 assets 资源"
        for ref in refs:
            assert (dist / ref.lstrip("./")).exists(), f"缺少资源: {ref}"

    def test_dist_icon_preview_needs_no_assets(self):
        dist = self._dist()
        preview = dist / "icon-preview.html"
        if not preview.exists():
            pytest.skip("dist/ 尚未构建")
        text = preview.read_text(encoding="utf-8")
        assert "assets/" not in text
