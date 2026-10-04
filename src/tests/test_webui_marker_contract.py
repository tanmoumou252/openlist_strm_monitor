"""webui 回归 marker 覆盖完整性与回归入口契约守护。

conftest.py 以文件名前缀（webui_names 元组）自动打 ``webui`` marker，
run_webui_regression.bat 以 ``-m webui`` 收集回归用例。凡真实测试 webui
模块（``webui.server`` / ``webui.routes`` / ``webui_fixtures``）却未命中
前缀的文件，都会被静默排除出回归收集——本文件守护该盲区：

1. 扫描 src/tests 顶层 test_*.py，凡 import/patch webui 模块的文件必须命中
   webui_names 前缀（动态读自 conftest.py 源码，保证单一事实源），
   或登记于 EXEMPT_FILES（附甄别理由）。
2. conftest 注册的 marker 名与 bat 中的 ``-m webui`` 字面一致。
3. bat 的 node:test glob 与 pytest marker 参数原文在场，防回归入口空转。
4. conftest collect_ignore_glob 条目不悬空（排除文件仍物理存在）。
5. node:test 测试文件数 >= 1（glob 失配时立刻红，而非静默 0 用例）。
"""
from __future__ import annotations

import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent.parent
CONFTEST_PATH = TESTS_DIR / "conftest.py"
BAT_PATH = PROJECT_ROOT / "run_webui_regression.bat"

# 非 webui 前缀却 import 了 webui 符号的豁免清单（逐一人工甄别登记）。
# 两者主体均为引擎套件，仅单点偶发引用 webui.routes 内的通用工具函数，
# 整文件改名会把引擎测试误拉入 -m webui 回归。
EXEMPT_FILES = {
    "test_boundary_conditions.py": "仅模块级一处 from webui.routes import _validate_strm_engines",
    "test_app_service_core.py": "仅函数内一处 from webui.routes import _natural_sort_key",
}

_WEBUI_IMPORT_RE = re.compile(
    r"^\s*(?:from webui[. ]|import webui[. ]|from webui_fixtures"
    r"|import webui_fixtures)\b",
    re.MULTILINE,
)


def _conftest_source() -> str:
    return CONFTEST_PATH.read_text(encoding="utf-8")


def _webui_names() -> tuple[str, ...]:
    """动态提取 conftest 的 webui_names 前缀元组（单一事实源，不重复硬编码）。"""
    m = re.search(r"webui_names\s*=\s*\(([^)]*)\)", _conftest_source())
    assert m is not None, "conftest.py 缺少 webui_names 前缀元组定义"
    names = tuple(
        s.strip().strip("'\"") for s in m.group(1).split(",") if s.strip()
    )
    assert names, "conftest.py 的 webui_names 前缀元组为空"
    return names


def _is_webui_test_source(text: str) -> bool:
    return bool(_WEBUI_IMPORT_RE.search(text) or 'patch("webui.' in text)


class TestMarkerCoverage:
    def test_every_webui_importing_test_file_carries_webui_prefix(self):
        webui_names = _webui_names()
        offenders = []
        for path in sorted(TESTS_DIR.glob("test_*.py")):
            if path.name in EXEMPT_FILES:
                continue
            if _is_webui_test_source(
                    path.read_text(encoding="utf-8", errors="replace")):
                if not path.name.startswith(webui_names):
                    offenders.append(path.name)
        assert not offenders, (
            "以下测试文件 import/patch 了 webui 模块但文件名未命中 conftest "
            f"webui_names 前缀，被 -m webui 回归静默漏收: {offenders}；"
            "请改名加前缀，或（确属引擎套件时）登记 EXEMPT_FILES 并附甄别理由")

    def test_exempt_files_still_exist_and_still_reference_webui(self):
        for name, reason in EXEMPT_FILES.items():
            path = TESTS_DIR / name
            assert path.is_file(), f"豁免登记的文件已不存在: {name}"
            assert _is_webui_test_source(
                path.read_text(encoding="utf-8", errors="replace")), (
                f"豁免文件 {name} 已不再 import webui 符号，应从豁免清单移除"
                f"（登记理由: {reason}）")

    def test_conftest_marker_name_matches_bat_literal(self):
        assert re.search(r'addinivalue_line\("markers",\s*"webui:',
                         _conftest_source()), "conftest 未注册 webui marker"
        bat = BAT_PATH.read_text(encoding="utf-8", errors="replace")
        assert "-m webui" in bat, (
            "run_webui_regression.bat 缺少 -m webui 参数；marker 改名将导致"
            "收集 0 用例静默通过")


class TestRegressionEntry:
    def test_bat_enumerates_js_files_and_guards_zero_coverage(self):
        """回归入口接线契约：cmd 侧枚举 JS 用例文件（不依赖 Node 的引号 glob
        展开语义），并对零覆盖显式判红。"""
        bat = BAT_PATH.read_text(encoding="utf-8", errors="replace")
        assert 'for %%F in ("src\\webui\\tests\\*.test.mjs") do (' in bat, (
            "run_webui_regression.bat 必须以 cmd 侧枚举 JS 用例文件，"
            "回归入口空转风险")
        assert "if !JS_COUNT! EQU 0 (" in bat, (
            "run_webui_regression.bat 缺少零用例判红闸（JS 侧零覆盖必须显式失败）")
        assert "node.exe --test !JS_ARGS!" in bat, (
            "run_webui_regression.bat 的 node:test 调用形态变更，回归入口空转风险")
        assert "python.exe -m pytest src/tests -m webui" in bat, (
            "run_webui_regression.bat 的 pytest 收集原文变更，回归入口空转风险")

    def test_node_test_suite_is_non_empty(self):
        js_tests = list(
            (PROJECT_ROOT / "src" / "webui" / "tests").glob("*.test.mjs"))
        assert len(js_tests) >= 1, (
            "src/webui/tests 下无任何 *.test.mjs，node:test 回归空转")

    def test_collect_ignore_glob_entries_still_exist(self):
        m = re.search(r"collect_ignore_glob\s*=\s*\[(.*?)\]",
                      _conftest_source(), re.DOTALL)
        assert m is not None, "conftest.py 缺少 collect_ignore_glob 定义"
        entries = re.findall(r'"([^"]+)"', m.group(1))
        assert entries, "collect_ignore_glob 为空"
        missing = [e for e in entries if not (TESTS_DIR / e).is_file()]
        assert not missing, (
            f"collect_ignore_glob 指向的文件已不存在（条目悬空）: {missing}")
