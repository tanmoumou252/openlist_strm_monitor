#!/usr/bin/env python3
"""
字体子集化脚本 — 为 openlist_strm_bridge WebUI 生成可变字体子集。

用法:
    python src/webui/scripts/subset_font.py
    python src/webui/scripts/subset_font.py --source-kr C:\\path\\to\\NotoSansKR-VF.ttf

输出:
    - src/webui/assets/fonts/NotoSansSC-Subset.woff2  (中日 + 标点 + 假名 + 全角)
    - src/webui/assets/fonts/NotoSansKR-Subset.woff2  (韩文, 仅当 --source-kr 提供时)

注意:
    - fontTools.merge.Merger 无法合并含 gvar 的可变字体 (VarStore 无 mergeMap),
      因此 SC/KR 输出为两个独立 woff2, 由 CSS unicode-range 分流。
    - 脚本不自动修改 CSS, 运行结束后打印需要手动执行的 CSS 修改指令。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Unicode 区块定义
# ---------------------------------------------------------------------------

RANGES = [
    ("ASCII",      0x0020, 0x007F, "SC"),
    ("CJK标点",    0x3000, 0x303F, "SC"),
    ("日文假名",   0x3040, 0x30FF, "SC"),
    ("CJK基本区",  0x4E00, 0x9FFF, "SC"),
    ("全角符号",   0xFF00, 0xFFEF, "SC"),
    ("韩文谚文",   0xAC00, 0xD7AF, "KR"),
]

DEFAULT_UNICODES = "U+0020-007F,U+3000-303F,U+3040-30FF,U+4E00-9FFF,U+FF00-FFEF,U+AC00-D7AF"
KR_UNICODES = "U+AC00-D7AF"

# 脚本所在目录: src/webui/scripts/
SCRIPT_DIR = Path(__file__).resolve().parent
# 默认输出目录: src/webui/assets/fonts/
DEFAULT_OUTPUT_DIR = SCRIPT_DIR.parent / "assets" / "fonts"
# 默认 SC 源
DEFAULT_SOURCE_SC = Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="为 WebUI 生成可变字体子集 (中日韩 + 标点)",
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
        default=DEFAULT_UNICODES,
        help=f"Unicode 范围串. 默认: {DEFAULT_UNICODES}",
    )
    p.add_argument(
        "--weights",
        type=str,
        default="300-700",
        help="字重范围 (仅自检报告用, 不裁轴). 默认: 300-700",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# 核心子集化
# ---------------------------------------------------------------------------

def subset_font(
    source_path: str,
    unicodes_str: str,
    output_path: str,
    label: str,
) -> None:
    """对单个源字体执行子集化并保存为 woff2."""
    from fontTools.ttLib import TTFont
    from fontTools.subset import Subsetter, Options, parse_unicodes

    print(f"\n{'='*60}")
    print(f"[{label}] 子集化: {source_path}")
    print(f"  输出: {output_path}")

    source_size = os.path.getsize(source_path)
    print(f"  源大小: {source_size / 1024 / 1024:.1f} MB")

    # 加载源字体
    font = TTFont(source_path)

    # 校验可变字体
    if "fvar" not in font:
        print(f"  ERROR: 源字体不含 fvar 表, 不是可变字体", file=sys.stderr)
        sys.exit(1)

    # 配置子集选项
    opts = Options()
    # flavor 在 save 前设置, 不在 Options 里设 (避免中间文件问题)
    opts.desubroutinize = True
    opts.ignore_missing_unicodes = True  # 关键: SC 源无韩文时不报错
    opts.hinting = True
    # 不 drop fvar/gvar/avar/STAT/HVAR → 保持可变字体

    # 解析 unicode 范围
    unicodes = parse_unicodes(unicodes_str)
    print(f"  请求 unicodes: {len(unicodes)} 个码位")

    # 执行子集化 (直接修改 font 对象)
    subsetter = Subsetter()
    subsetter.populate(unicodes=unicodes)
    subsetter.subset(font)

    # 设置 woff2 格式并保存
    font.flavor = "woff2"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    font.save(output_path)
    font.close()

    output_size = os.path.getsize(output_path)
    print(f"  输出大小: {output_size / 1024 / 1024:.2f} MB ({output_size / 1024:.0f} KB)")
    print(f"  压缩比: {output_size / source_size * 100:.1f}%")


# ---------------------------------------------------------------------------
# 自检校验
# ---------------------------------------------------------------------------

def verify_output(
    output_path: str,
    expected_unicodes_str: str,
    label: str,
    source_path: str | None = None,
) -> bool:
    """校验输出字体的 cmap 覆盖情况. 返回 True 表示通过."""
    from fontTools.ttLib import TTFont
    from fontTools.subset import parse_unicodes

    print(f"\n{'='*60}")
    print(f"[{label}] 自检校验: {output_path}")

    font = TTFont(output_path)
    cmap = font.getBestCmap()
    if cmap is None:
        print("  ERROR: 无法读取 cmap", file=sys.stderr)
        font.close()
        return False

    expected_unicodes = set(parse_unicodes(expected_unicodes_str))

    # 如果提供了源字体, 计算源本身覆盖的期望码位 (用于区分"源缺失" vs "子集丢失")
    source_cmap_set: set[int] | None = None
    if source_path and os.path.exists(source_path):
        src_font = TTFont(source_path)
        src_cmap = src_font.getBestCmap()
        if src_cmap is not None:
            source_cmap_set = set(src_cmap.keys()) & expected_unicodes
        src_font.close()

    # 按区块统计
    all_ok = True
    print(f"\n  {'区块':<10} {'实际':>6} {'期望':>6} {'缺失':>6} {'来源':>8}")
    print(f"  {'-'*42}")

    total_actual = 0
    total_expected = 0
    total_missing_from_source = 0
    total_missing_from_subset = 0

    for name, start, end, source_tag in RANGES:
        range_codes = set(range(start, end + 1))
        # 该区块在期望范围内的码位
        expected_in_range = range_codes & expected_unicodes
        actual_in_range = set(cmap.keys()) & expected_in_range
        missing = expected_in_range - actual_in_range

        actual_count = len(actual_in_range)
        expected_count = len(expected_in_range)
        missing_count = len(missing)

        # 区分缺失来源
        if source_cmap_set is not None:
            missing_from_source = len(missing - source_cmap_set)
            missing_from_subset = len(missing & source_cmap_set)
        else:
            missing_from_source = missing_count
            missing_from_subset = 0

        total_actual += actual_count
        total_expected += expected_count
        total_missing_from_source += missing_from_source
        total_missing_from_subset += missing_from_subset

        source_label = source_tag
        status = "OK" if missing_count == 0 else ("WARN" if missing_from_subset == 0 else "ERR")
        if status == "ERR":
            all_ok = False

        print(f"  {name:<10} {actual_count:>6} {expected_count:>6} {missing_count:>6} {source_label:>8}  {status}")

    print(f"  {'-'*42}")
    print(f"  {'合计':<10} {total_actual:>6} {total_expected:>6} {len(missing):>6}")

    # fvar / wght 轴检查
    has_fvar = "fvar" in font
    print(f"\n  fvar: {'有' if has_fvar else '无 (ERROR)'}")
    if not has_fvar:
        all_ok = False

    if has_fvar:
        fvar = font["fvar"]
        for ax in fvar.axes:
            print(f"  轴 {ax.axisTag}: min={ax.minValue}, default={ax.defaultValue}, max={ax.maxValue}")

    # 总字形数
    print(f"  总字形数: {len(font.getGlyphOrder())}")
    print(f"  cmap 总条目: {len(cmap)}")

    font.close()

    # 结论
    if total_missing_from_subset > 0:
        print(f"\n  ERROR: {total_missing_from_subset} 个码位在源中存在但子集丢失!")
        all_ok = False
    if total_missing_from_source > 0:
        print(f"\n  WARNING: {total_missing_from_source} 个码位在源字体中本身缺失 (非子集问题)")

    if all_ok:
        print(f"\n  结论: PASS")
    else:
        print(f"\n  结论: FAIL")

    return all_ok


# ---------------------------------------------------------------------------
# CSS 修改指令打印
# ---------------------------------------------------------------------------

def print_css_instructions(has_kr: bool, output_dir: str) -> None:
    """打印需要手动执行的 CSS 修改指令."""
    print(f"\n{'='*60}")
    print("CSS 修改指令 (需手动执行)")
    print(f"{'='*60}")
    print(f"\n文件: src/webui/styles/main.css")
    print()

    if has_kr:
        print("1. 将第 10 行 unicode-range 改为:")
        print()
        print("   unicode-range: U+0020-007F, U+3000-303F, U+3040-30FF, U+4E00-9FFF, U+FF00-FFEF;")
        print("   /* ASCII + 中日 + 标点 (韩文由第二个 @font-face 覆盖) */")
        print()
        print("2. 在第 11 行后追加韩文 @font-face:")
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
        print("1. 将第 10 行 unicode-range 改为:")
        print()
        print("   unicode-range: U+0020-007F, U+3000-303F, U+3040-30FF, U+4E00-9FFF, U+FF00-FFEF;")
        print("   /* ASCII + 中日 + 标点 */")
        print()
        print("   (韩文未生成, 无需追加 @font-face)")

    print()
    print("3. 重新构建前端:")
    print("   cd src/webui && npx vite build")
    print()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    source_sc = args.source
    source_kr = args.source_kr
    output_dir = Path(args.output)
    unicodes_str = args.unicodes
    weights = args.weights

    print(f"字体子集化脚本")
    print(f"  SC 源: {source_sc}")
    print(f"  KR 源: {source_kr or '(未提供, 跳过韩文)'}")
    print(f"  输出目录: {output_dir}")
    print(f"  Unicode 范围: {unicodes_str}")
    print(f"  字重范围 (报告用): {weights}")

    # 校验 SC 源
    if not os.path.exists(source_sc):
        print(f"\nERROR: SC 源字体不存在: {source_sc}", file=sys.stderr)
        return 1

    # SC 子集化
    sc_output = output_dir / "NotoSansSC-Subset.woff2"
    # SC 子集的 unicode 范围: 去掉韩文 (SC 源无韩文, 但 ignore_missing_unicodes 会跳过)
    # 为清晰起见, 仍传全部范围, 让 ignore_missing_unicodes 处理
    subset_font(source_sc, unicodes_str, str(sc_output), "SC")

    # KR 子集化 (可选)
    kr_output = None
    if source_kr:
        if not os.path.exists(source_kr):
            print(f"\nERROR: KR 源字体不存在: {source_kr}", file=sys.stderr)
            return 1
        kr_output = output_dir / "NotoSansKR-Subset.woff2"
        subset_font(source_kr, KR_UNICODES, str(kr_output), "KR")
    else:
        print(f"\nWARNING: 未提供 --source-kr, 韩文子集未生成.")
        print(f"  如需韩文支持, 请下载 NotoSansKR-VF.ttf 后重新运行:")
        print(f"  python {__file__} --source-kr <KR字体路径>")

    # 自检校验
    sc_ok = verify_output(str(sc_output), unicodes_str, "SC", source_sc)
    kr_ok = True
    if kr_output:
        kr_ok = verify_output(str(kr_output), KR_UNICODES, "KR", source_kr)

    # 最终报告
    print(f"\n{'='*60}")
    print("最终报告")
    print(f"{'='*60}")
    print(f"  SC 子集: {sc_output}")
    print(f"    大小: {os.path.getsize(sc_output) / 1024 / 1024:.2f} MB")
    print(f"    校验: {'PASS' if sc_ok else 'FAIL'}")
    if kr_output:
        print(f"  KR 子集: {kr_output}")
        print(f"    大小: {os.path.getsize(kr_output) / 1024:.0f} KB")
        print(f"    校验: {'PASS' if kr_ok else 'FAIL'}")

    # CSS 修改指令
    print_css_instructions(has_kr=kr_output is not None, output_dir=str(output_dir))

    # 退出码
    if not sc_ok or not kr_ok:
        print("\nERROR: 自检校验失败, 请检查输出.", file=sys.stderr)
        return 1

    print("\n完成.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
