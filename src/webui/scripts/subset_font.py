#!/usr/bin/env python3
"""
字体子集化脚本 — 为 openlist_strm_bridge WebUI 生成可变字体子集。

用法:
    python src/webui/scripts/subset_font.py
    python src/webui/scripts/subset_font.py --source-kr C:\\path\\to\\NotoSansKR-VF.ttf
    python src/webui/scripts/subset_font.py --source <字体> --output <目录>
    python src/webui/scripts/subset_font.py --no-scan   # 只用静态区块，不扫描网页源

输出:
    - src/webui/assets/fonts/NotoSansSC-Subset.woff2  (中日 + 标点 + 假名 + 全角)
    - src/webui/assets/fonts/NotoSansKR-Subset.woff2  (韩文, 仅当 --source-kr 提供时)

覆盖范围来源:
    1. 静态区块 (STATIC_BLOCKS): ASCII、Latin-1 补充、CJK 标点、日文假名、
       CJK 基本区、全角符号；韩文由独立 KR 子集覆盖。
    2. 网页实际字符扫描 (scan_web_source_codepoints): 扫描 index.html、
       public/*.html、modules/**/*.js、styles/**/*.css、main.js 中真实出现的
       可显示字符，与静态区块合并后再子集化。扫描范围严格限定 WebUI 源文件，
       不含日志、数据库和二进制资源。

缺字判定:
    - 源字体本身没有该字形 → WARNING，不算失败（需靠 CSS 让系统字体回退）
    - 源字体有但输出 cmap 缺失 → ERROR，返回非零退出码
    - CSS 声明的 unicode-range 超出输出 cmap → ERROR，声明必须与实际一致

注意:
    - fontTools.merge.Merger 无法合并含 gvar 的可变字体 (VarStore 无 mergeMap),
      因此 SC/KR 输出为两个独立 woff2, 由 CSS unicode-range 分流。
    - 脚本不自动修改 CSS, 运行结束后打印需要手动执行的 CSS 修改指令。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from pathlib import Path

# ---------------------------------------------------------------------------
# Unicode 区块定义
# ---------------------------------------------------------------------------

# (区块名, 起始码位, 结束码位, 归属子集)
STATIC_BLOCKS: list[tuple[str, int, int, str]] = [
    ("ASCII",       0x0020, 0x007F, "SC"),
    ("Latin1补充",  0x00A0, 0x00FF, "SC"),
    ("CJK标点",     0x3000, 0x303F, "SC"),
    ("日文假名",    0x3040, 0x30FF, "SC"),
    ("CJK基本区",   0x4E00, 0x9FFF, "SC"),
    ("全角符号",    0xFF00, 0xFFEF, "SC"),
    ("韩文谚文",    0xAC00, 0xD7AF, "KR"),
]

# 向后兼容旧名称（部分文档/脚本引用 RANGES）
RANGES = STATIC_BLOCKS

# 网页扫描得到、但不落在任何静态区块内的码位，统一归入该虚拟区块用于报告
SCAN_BLOCK_NAME = "网页扫描"

SC_BLOCK_TAGS = ("SC",)
KR_UNICODES = "U+AC00-D7AF"

# 脚本所在目录: src/webui/scripts/
SCRIPT_DIR = Path(__file__).resolve().parent
# WebUI 源目录: src/webui/
WEBUI_DIR = SCRIPT_DIR.parent
# 默认输出目录: src/webui/assets/fonts/
DEFAULT_OUTPUT_DIR = WEBUI_DIR / "assets" / "fonts"
# 默认 CSS 路径
DEFAULT_CSS_PATH = WEBUI_DIR / "styles" / "main.css"
# 默认 SC 源
DEFAULT_SOURCE_SC = Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf")

# 扫描范围：只扫 WebUI 源文件与静态 HTML，绝不扫日志/数据库/二进制
SCAN_GLOBS: tuple[str, ...] = (
    "index.html",
    "main.js",
    "public/*.html",
    "modules/**/*.js",
    "styles/**/*.css",
    "*.py",  # G'.3: 后端 Python 源文件（routes.py / server.py 中的 showToast/dialog 中文）
)


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="为 WebUI 生成可变字体子集 (中日韩 + 标点 + 网页实际字符)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--source",
        type=str,
        default=str(DEFAULT_SOURCE_SC),
        help=f"主源字体路径 (中日标点). 默认: {DEFAULT_SOURCE_SC}",
    )
    p.add_argument(
        "--source-kr",
        type=str,
        default=None,
        help="韩文源字体路径. 不提供则跳过韩文子集.",
    )
    p.add_argument(
        "--output",
        type=str,
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"输出目录. 默认: {DEFAULT_OUTPUT_DIR}",
    )
    p.add_argument(
        "--unicodes",
        type=str,
        default=None,
        help="额外 Unicode 范围串 (形如 U+0020-007F,U+2192). "
             "默认使用静态区块; 该参数与静态区块和扫描结果合并, 不替换它们.",
    )
    p.add_argument(
        "--webui-dir",
        type=str,
        default=str(WEBUI_DIR),
        help=f"待扫描的 WebUI 源目录. 默认: {WEBUI_DIR}",
    )
    p.add_argument(
        "--css",
        type=str,
        default=str(DEFAULT_CSS_PATH),
        help=f"用于一致性校验的 CSS 路径. 默认: {DEFAULT_CSS_PATH}",
    )
    p.add_argument(
        "--no-scan",
        action="store_true",
        help="跳过网页字符扫描, 只使用静态区块 (调试用).",
    )
    p.add_argument(
        "--weights",
        type=str,
        default="300-700",
        help="字重范围 (仅自检报告用, 不裁轴). 默认: 300-700",
    )
    return p


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_arg_parser().parse_args(argv)


# ---------------------------------------------------------------------------
# Unicode 集合工具（纯函数，便于单测）
# ---------------------------------------------------------------------------

def static_codepoints(tags: tuple[str, ...] = SC_BLOCK_TAGS) -> set[int]:
    """返回指定归属标签的静态区块码位集合。"""
    result: set[int] = set()
    for _name, start, end, tag in STATIC_BLOCKS:
        if tag in tags:
            result.update(range(start, end + 1))
    return result


def is_scannable_char(ch: str) -> bool:
    """判断字符是否应纳入字体子集。

    排除控制字符、格式字符、代理对、私用区与未分配码位（换行、制表、回车都属于
    Cc），同时排除变体选择符（U+FE00-FE0F、U+E0100-E01EF）——它们只改变相邻字符
    的呈现形式，本身没有独立字形，不应成为"必须有字形"的硬要求。
    保留空格、可显示符号、CJK 与标点。
    """
    if not ch:
        return False
    if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
        return False
    cp = ord(ch)
    if 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF:
        return False
    return True


def scan_text_codepoints(text: str) -> set[int]:
    """从一段文本中提取需要字形的码位集合。"""
    return {ord(ch) for ch in set(text) if is_scannable_char(ch)}


def iter_web_source_files(webui_dir: str | Path,
                          globs: tuple[str, ...] = SCAN_GLOBS) -> list[Path]:
    """返回需要扫描的 WebUI 源文件列表（稳定排序，去重）。"""
    base = Path(webui_dir)
    seen: set[Path] = set()
    files: list[Path] = []
    for pattern in globs:
        for path in sorted(base.glob(pattern)):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(path)
    return files


def scan_web_source_codepoints(
    webui_dir: str | Path,
    globs: tuple[str, ...] = SCAN_GLOBS,
) -> tuple[set[int], list[Path]]:
    """扫描 WebUI 源文件，返回 (码位集合, 已扫描文件列表)。"""
    codepoints: set[int] = set()
    files = iter_web_source_files(webui_dir, globs)
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"  WARNING: 无法读取 {path}: {exc}", file=sys.stderr)
            continue
        codepoints |= scan_text_codepoints(text)
    # errors="replace" 可能引入 U+FFFD，它不是真实网页字符
    codepoints.discard(0xFFFD)
    return codepoints, files


def parse_unicode_range_str(value: str) -> set[int]:
    """解析 "U+0020-007F,U+2192" 形式的范围串。

    同时兼容 CSS 的空格分隔与十六进制大小写；无法识别的片段被忽略。
    """
    result: set[int] = set()
    if not value:
        return result
    for token in re.split(r"[,\s]+", value.strip()):
        if not token:
            continue
        token = token.strip().rstrip(";")
        m = re.fullmatch(r"[uU]\+([0-9a-fA-F]+)(?:-([0-9a-fA-F]+))?", token)
        if not m:
            continue
        start = int(m.group(1), 16)
        end = int(m.group(2), 16) if m.group(2) else start
        if end < start:
            start, end = end, start
        result.update(range(start, end + 1))
    return result


def format_unicode_ranges(codepoints: set[int], separator: str = ",") -> str:
    """把码位集合压缩为紧凑的 "U+0020-007F,U+00B7" 形式。"""
    if not codepoints:
        return ""
    ordered = sorted(codepoints)
    chunks: list[str] = []
    start = prev = ordered[0]
    for cp in ordered[1:]:
        if cp == prev + 1:
            prev = cp
            continue
        chunks.append(_format_chunk(start, prev))
        start = prev = cp
    chunks.append(_format_chunk(start, prev))
    return separator.join(chunks)


def _format_chunk(start: int, end: int) -> str:
    if start == end:
        return f"U+{start:04X}"
    return f"U+{start:04X}-{end:04X}"


def merge_codepoints(*groups: set[int] | None) -> set[int]:
    """合并多组码位集合，None 视为空集。"""
    merged: set[int] = set()
    for group in groups:
        if group:
            merged |= group
    return merged


def classify_missing(
    expected: set[int],
    output_cmap: set[int],
    source_cmap: set[int] | None,
) -> tuple[set[int], set[int]]:
    """区分缺字来源。

    返回 (missing_from_source, missing_from_subset):
    - missing_from_source: 源字体本身没有 → WARNING
    - missing_from_subset: 源字体有但输出丢了 → ERROR

    source_cmap 为 None（无法读取源）时保守地全部归为"源缺失"，
    避免把不可知情况误判为子集化失败。
    """
    missing = expected - output_cmap
    if source_cmap is None:
        return missing, set()
    return missing - source_cmap, missing & source_cmap


def describe_codepoints(codepoints: set[int], limit: int = 40) -> list[str]:
    """把码位集合渲染为便于阅读的清单（截断到 limit 条）。"""
    lines: list[str] = []
    for cp in sorted(codepoints)[:limit]:
        try:
            name = unicodedata.name(chr(cp))
        except ValueError:
            name = "<未命名>"
        lines.append(f"U+{cp:04X} {name}")
    remaining = len(codepoints) - limit
    if remaining > 0:
        lines.append(f"... 其余 {remaining} 个码位省略")
    return lines


# ---------------------------------------------------------------------------
# CSS 一致性
# ---------------------------------------------------------------------------

def extract_css_unicode_range(css_text: str, font_file: str) -> str | None:
    """从 CSS 中提取引用 font_file 的 @font-face 的 unicode-range 原始串。

    找不到对应 @font-face 或该块没有 unicode-range 时返回 None。
    """
    for block in re.findall(r"@font-face\s*\{(.*?)\}", css_text, re.S):
        if font_file not in block:
            continue
        m = re.search(r"unicode-range\s*:\s*([^;}]+)", block)
        if m:
            return m.group(1).strip()
    return None


def renderable_codepoints(codepoints: set[int]) -> set[int]:
    """过滤出真正可能渲染出字形的码位。

    控制字符、格式字符、未分配码位与变体选择符即便落在声明的 unicode-range 里
    也不会显示豆腐块，因此把它们从"声明超出实际"的判定中排除，避免 CSS 为了
    绕开无意义的空洞而被切成几十段。
    """
    return {cp for cp in codepoints if is_scannable_char(chr(cp))}


def verify_css_declaration(
    css_path: str | Path,
    output_cmap: set[int],
    font_file: str = "NotoSansSC-Subset.woff2",
) -> bool:
    """校验 CSS 声明的 unicode-range 未超出输出字体实际 cmap。

    声明覆盖但字体没有字形时，浏览器不会回退系统字体，会显示豆腐块；
    因此这属于必须修正的 ERROR。仅统计可渲染码位（见 renderable_codepoints）。
    """
    path = Path(css_path)
    print(f"\n{'=' * 60}")
    print(f"[CSS] unicode-range 一致性校验: {path}")
    if not path.exists():
        print("  WARNING: CSS 文件不存在，跳过校验")
        return True

    raw = extract_css_unicode_range(
        path.read_text(encoding="utf-8"), font_file)
    if raw is None:
        print(f"  WARNING: 未找到引用 {font_file} 的 @font-face unicode-range，跳过校验")
        return True

    declared = parse_unicode_range_str(raw)
    over_declared = renderable_codepoints(declared - output_cmap)
    print(f"  声明: {raw}")
    print(f"  声明码位: {len(declared)}，字体 cmap: {len(output_cmap)}")
    if not over_declared:
        print("  结论: PASS（声明范围内的可渲染码位均有字形）")
        return True

    print(f"  ERROR: 声明覆盖但字体缺字形的可渲染码位 {len(over_declared)} 个:")
    for line in describe_codepoints(over_declared):
        print(f"    {line}")
    print("  请从 unicode-range 中移除这些码位，让系统字体回退生效。")
    return False


def build_css_unicode_range(output_cmap: set[int],
                            expected: set[int]) -> str:
    """生成建议写入 CSS 的 unicode-range。

    只声明真的有字形的码位；但允许跨越不可渲染的空洞（控制字符、未分配码位）
    合并区间，避免生成几十段无意义的碎片声明。
    """
    holes = expected - renderable_codepoints(expected)
    return format_unicode_ranges(
        expected & (output_cmap | holes), separator=", ")


# ---------------------------------------------------------------------------
# 字体读取
# ---------------------------------------------------------------------------

def read_font_cmap(font_path: str | Path) -> set[int] | None:
    """读取字体 cmap 码位集合。失败返回 None（表示不可知，而非空集）。"""
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        print("  ERROR: 缺少 fontTools，请先 pip install fontTools brotli",
              file=sys.stderr)
        return None
    try:
        font = TTFont(str(font_path))
    except Exception as exc:  # noqa: BLE001 - 字体格式异常种类繁多
        print(f"  ERROR: 无法打开字体 {font_path}: {exc}", file=sys.stderr)
        return None
    try:
        cmap = font.getBestCmap()
        if cmap is None:
            return None
        return set(cmap.keys())
    finally:
        font.close()


# ---------------------------------------------------------------------------
# 核心子集化
# ---------------------------------------------------------------------------

def subset_font(
    source_path: str,
    unicodes: set[int] | str,
    output_path: str,
    label: str,
) -> None:
    """对单个源字体执行子集化并保存为 woff2."""
    from fontTools.ttLib import TTFont
    from fontTools.subset import Subsetter, Options, parse_unicodes

    print(f"\n{'=' * 60}")
    print(f"[{label}] 子集化: {source_path}")
    print(f"  输出: {output_path}")

    source_size = os.path.getsize(source_path)
    print(f"  源大小: {source_size / 1024 / 1024:.1f} MB")

    font = TTFont(source_path)

    if "fvar" not in font:
        print("  ERROR: 源字体不含 fvar 表, 不是可变字体", file=sys.stderr)
        font.close()
        raise SystemExit(1)

    opts = Options()
    # flavor 在 save 前设置, 不在 Options 里设 (避免中间文件问题)
    opts.desubroutinize = True
    opts.ignore_missing_unicodes = True  # 关键: 源字体缺字时不报错, 由自检区分
    opts.hinting = True
    # 不 drop fvar/gvar/avar/STAT/HVAR → 保持可变字体

    codepoints = (
        set(parse_unicodes(unicodes)) if isinstance(unicodes, str)
        else set(unicodes)
    )
    print(f"  请求 unicodes: {len(codepoints)} 个码位")

    subsetter = Subsetter(options=opts)
    subsetter.populate(unicodes=sorted(codepoints))
    subsetter.subset(font)

    font.flavor = "woff2"
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    font.save(output_path)
    font.close()

    output_size = os.path.getsize(output_path)
    print(f"  输出大小: {output_size / 1024 / 1024:.2f} MB "
          f"({output_size / 1024:.0f} KB)")
    print(f"  压缩比: {output_size / source_size * 100:.1f}%")


# ---------------------------------------------------------------------------
# 自检校验
# ---------------------------------------------------------------------------

def verify_output(
    output_path: str,
    expected: set[int] | str,
    label: str,
    source_path: str | None = None,
    scan_codepoints: set[int] | None = None,
) -> bool:
    """校验输出字体的 cmap 覆盖情况. 返回 True 表示通过."""
    from fontTools.ttLib import TTFont
    from fontTools.subset import parse_unicodes

    print(f"\n{'=' * 60}")
    print(f"[{label}] 自检校验: {output_path}")

    expected_unicodes = (
        set(parse_unicodes(expected)) if isinstance(expected, str)
        else set(expected)
    )

    font = TTFont(output_path)
    cmap = font.getBestCmap()
    if cmap is None:
        print("  ERROR: 无法读取 cmap", file=sys.stderr)
        font.close()
        return False
    output_cmap = set(cmap.keys())

    source_cmap = read_font_cmap(source_path) if source_path else None

    all_ok = True
    print(f"\n  {'区块':<12} {'实际':>6} {'期望':>6} {'源缺失':>7} {'子集丢字':>9}")
    print(f"  {'-' * 48}")

    total_actual = 0
    total_expected = 0
    total_missing_from_source = 0
    total_missing_from_subset = 0
    covered_by_blocks: set[int] = set()

    report_blocks: list[tuple[str, set[int]]] = [
        (name, set(range(start, end + 1)))
        for name, start, end, _tag in STATIC_BLOCKS
    ]
    # 扫描得到但不在任何静态区块中的码位单列一行
    static_all = set()
    for _name, codes in report_blocks:
        static_all |= codes
    scan_only = (scan_codepoints or set()) - static_all
    extra = (expected_unicodes - static_all) | scan_only
    if extra:
        report_blocks.append((SCAN_BLOCK_NAME, extra))

    for name, codes in report_blocks:
        expected_in_block = codes & expected_unicodes
        if not expected_in_block:
            continue
        covered_by_blocks |= expected_in_block
        actual_in_block = output_cmap & expected_in_block
        from_source, from_subset = classify_missing(
            expected_in_block, output_cmap, source_cmap)

        total_actual += len(actual_in_block)
        total_expected += len(expected_in_block)
        total_missing_from_source += len(from_source)
        total_missing_from_subset += len(from_subset)

        if from_subset:
            status = "ERR"
            all_ok = False
        elif from_source:
            status = "WARN"
        else:
            status = "OK"

        print(f"  {name:<12} {len(actual_in_block):>6} "
              f"{len(expected_in_block):>6} {len(from_source):>7} "
              f"{len(from_subset):>9}  {status}")

    print(f"  {'-' * 48}")
    print(f"  {'合计':<12} {total_actual:>6} {total_expected:>6} "
          f"{total_missing_from_source:>7} {total_missing_from_subset:>9}")

    # 网页实际字符必须逐字符验证，不能只看区块统计
    if scan_codepoints:
        scan_from_source, scan_from_subset = classify_missing(
            scan_codepoints, output_cmap, source_cmap)
        print(f"\n  网页实际字符: {len(scan_codepoints)} 个")
        print(f"    源字体缺失 (WARNING, 需 CSS 回退): {len(scan_from_source)}")
        print(f"    子集丢字 (ERROR): {len(scan_from_subset)}")
        for line in describe_codepoints(scan_from_source):
            print(f"      WARN {line}")
        for line in describe_codepoints(scan_from_subset):
            print(f"      ERR  {line}")
        if scan_from_subset:
            all_ok = False

    has_fvar = "fvar" in font
    print(f"\n  fvar: {'有' if has_fvar else '无 (ERROR)'}")
    if not has_fvar:
        all_ok = False
    else:
        for ax in font["fvar"].axes:
            print(f"  轴 {ax.axisTag}: min={ax.minValue}, "
                  f"default={ax.defaultValue}, max={ax.maxValue}")

    print(f"  总字形数: {len(font.getGlyphOrder())}")
    print(f"  cmap 总条目: {len(output_cmap)}")
    font.close()

    if total_missing_from_subset > 0:
        print(f"\n  ERROR: {total_missing_from_subset} 个码位在源中存在但子集丢失!")
        all_ok = False
    if total_missing_from_source > 0:
        print(f"\n  WARNING: {total_missing_from_source} 个码位在源字体中本身缺失 "
              f"(非子集问题, 由系统字体回退)")

    print(f"\n  结论: {'PASS' if all_ok else 'FAIL'}")
    return all_ok


# ---------------------------------------------------------------------------
# CSS 修改指令打印
# ---------------------------------------------------------------------------

def print_css_instructions(
    has_kr: bool,
    suggested_range: str,
) -> None:
    """打印需要手动执行的 CSS 修改指令."""
    print(f"\n{'=' * 60}")
    print("CSS 修改指令 (需手动执行)")
    print(f"{'=' * 60}")
    print("\n文件: src/webui/styles/main.css")
    print("\n1. 将 NotoSansSC-Subset.woff2 所在 @font-face 的 unicode-range 改为:")
    print()
    print(f"   unicode-range: {suggested_range};")
    print("   /* 仅声明子集真实包含的码位；源字体缺字的符号交由系统字体回退 */")
    print()

    if has_kr:
        print("2. 追加韩文 @font-face:")
        print()
        print("   @font-face {")
        print("     font-family: 'Noto Sans SC';")
        print("     font-style: normal;")
        print("     font-weight: 300 700;")
        print("     font-display: swap;")
        print("     src: url('../assets/fonts/NotoSansKR-Subset.woff2') format('woff2');")
        print("     unicode-range: U+AC00-D7AF; /* 韩文谚文 */")
        print("   }")
    else:
        print("2. (韩文未生成, 无需追加 @font-face)")

    print()
    print("3. 重新构建前端:")
    print("   cd src/webui && npx vite build")
    print()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def resolve_sc_codepoints(args: argparse.Namespace) -> tuple[set[int], set[int]]:
    """汇总 SC 子集需要的码位。返回 (expected, scanned)。"""
    expected = static_codepoints(SC_BLOCK_TAGS)
    scanned: set[int] = set()

    if not args.no_scan:
        scanned, files = scan_web_source_codepoints(args.webui_dir)
        print(f"  扫描 WebUI 源文件: {len(files)} 个，"
              f"提取可显示码位 {len(scanned)} 个")
        expected |= scanned
    else:
        print("  已跳过网页字符扫描 (--no-scan)")

    if args.unicodes:
        extra = parse_unicode_range_str(args.unicodes)
        if not extra:
            from fontTools.subset import parse_unicodes
            extra = set(parse_unicodes(args.unicodes))
        print(f"  追加 --unicodes 指定码位: {len(extra)} 个")
        expected |= extra

    # 韩文由独立 KR 子集覆盖，不进入 SC 子集
    expected -= static_codepoints(("KR",))
    return expected, scanned


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    source_sc = args.source
    source_kr = args.source_kr
    output_dir = Path(args.output)

    print("字体子集化脚本")
    print(f"  SC 源: {source_sc}")
    print(f"  KR 源: {source_kr or '(未提供, 跳过韩文)'}")
    print(f"  输出目录: {output_dir}")
    print(f"  WebUI 源目录: {args.webui_dir}")
    print(f"  字重范围 (报告用): {args.weights}")

    if not os.path.exists(source_sc):
        print(f"\nERROR: SC 源字体不存在: {source_sc}", file=sys.stderr)
        return 1

    expected, scanned = resolve_sc_codepoints(args)
    print(f"  SC 期望码位合计: {len(expected)}")

    sc_output = output_dir / "NotoSansSC-Subset.woff2"
    subset_font(source_sc, expected, str(sc_output), "SC")

    kr_output = None
    if source_kr:
        if not os.path.exists(source_kr):
            print(f"\nERROR: KR 源字体不存在: {source_kr}", file=sys.stderr)
            return 1
        kr_output = output_dir / "NotoSansKR-Subset.woff2"
        subset_font(source_kr, KR_UNICODES, str(kr_output), "KR")
    else:
        print("\nWARNING: 未提供 --source-kr, 韩文子集未生成.")
        print("  如需韩文支持, 请下载 NotoSansKR-VF.ttf 后重新运行:")
        print(f"  python {__file__} --source-kr <KR字体路径>")

    sc_ok = verify_output(
        str(sc_output), expected, "SC", source_sc, scan_codepoints=scanned)
    kr_ok = True
    if kr_output:
        kr_ok = verify_output(str(kr_output), KR_UNICODES, "KR", source_kr)

    output_cmap = read_font_cmap(sc_output) or set()
    css_ok = verify_css_declaration(args.css, output_cmap)

    print(f"\n{'=' * 60}")
    print("最终报告")
    print(f"{'=' * 60}")
    print(f"  SC 子集: {sc_output}")
    print(f"    大小: {os.path.getsize(sc_output) / 1024 / 1024:.2f} MB")
    print(f"    校验: {'PASS' if sc_ok else 'FAIL'}")
    if kr_output:
        print(f"  KR 子集: {kr_output}")
        print(f"    大小: {os.path.getsize(kr_output) / 1024:.0f} KB")
        print(f"    校验: {'PASS' if kr_ok else 'FAIL'}")
    print(f"  CSS 一致性: {'PASS' if css_ok else 'FAIL'}")

    print_css_instructions(
        has_kr=kr_output is not None,
        suggested_range=build_css_unicode_range(output_cmap, expected),
    )

    if not sc_ok or not kr_ok or not css_ok:
        print("\nERROR: 自检校验失败, 请检查输出.", file=sys.stderr)
        return 1

    print("\n完成.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
