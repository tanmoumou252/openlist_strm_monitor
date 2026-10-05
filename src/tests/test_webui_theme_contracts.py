"""主题三方一致性与 CSS 变量定义守护。

- index.html 主题菜单 data-val 集合 ≡ theme.js 内置映射键（colorMap/fsMap）
- localStorage 键 webui_theme_*：theme.js 写入键 ≡ main.js 读取键
- JS 内联样式引用的 CSS 变量必须定义于 styles/main.css（当前无豁免项；
  新增未定义引用即刻红）
"""
from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEBUI_DIR = PROJECT_ROOT / "src" / "webui"
INDEX_HTML = WEBUI_DIR / "index.html"
THEME_JS = WEBUI_DIR / "modules" / "core" / "theme.js"
MAIN_JS = WEBUI_DIR / "main.js"
MAIN_CSS = WEBUI_DIR / "styles" / "main.css"

# 既有漂移豁免登记：当前为空（--error / --text-error 已在 main.css 基础 :root
# 定义为 --color-danger 别名）。如出现新的既有漂移，在此登记并注明出处。
KNOWN_UNDEFINED = {}


def _menu_values(html, menu_id):
    """按位置归属提取某个 theme 菜单内的全部 data-val（不依赖脆弱的 HTML 截取）。"""
    events = []
    for m in re.finditer(r'id="(theme-[a-z]+-menu)"', html):
        events.append((m.start(), "menu", m.group(1)))
    for m in re.finditer(r'data-val="([^"]+)"', html):
        events.append((m.start(), "val", m.group(1)))
    events.sort()
    current = None
    values = set()
    for _, kind, payload in events:
        if kind == "menu":
            current = payload
        elif current == menu_id:
            values.add(payload)
    assert values, f"index.html 菜单 {menu_id} 无 data-val 条目"
    return values


class TestThemeTripleConsistency:
    def test_color_menu_matches_colormap(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        theme = THEME_JS.read_text(encoding="utf-8")
        menu = _menu_values(html, "theme-color-menu")
        m = re.search(r"colorMap\s*=\s*\{([^}]*)\}", theme)
        assert m, "theme.js 缺少 colorMap"
        keys = set(re.findall(r"(\w+)\s*:", m.group(1)))
        assert menu == keys == {"blue", "purple", "green", "orange"}

    def test_fontsize_menu_matches_fsmap(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        theme = THEME_JS.read_text(encoding="utf-8")
        menu = _menu_values(html, "theme-fontsize-menu")
        m = re.search(r"fsMap\s*=\s*\{([^}]*)\}", theme)
        assert m, "theme.js 缺少 fsMap"
        keys = set(re.findall(r"(\w+)\s*:", m.group(1)))
        assert menu == keys == {"lg", "sm", "xs"}

    def test_system_menu_is_material_fluent(self):
        menu = _menu_values(INDEX_HTML.read_text(encoding="utf-8"),
                            "theme-system-menu")
        assert menu == {"material", "fluent"}

    def test_theme_localstorage_keys_symmetric(self):
        theme = THEME_JS.read_text(encoding="utf-8")
        main = MAIN_JS.read_text(encoding="utf-8")
        written = set(re.findall(
            r"localStorage\.setItem\('(webui_theme_\w+)'", theme))
        read = set(re.findall(
            r"localStorage\.getItem\('(webui_theme_\w+)'", main))
        assert written and written == read, (
            f"主题 localStorage 键不对称：写 {sorted(written)} / 读 {sorted(read)}")


class TestInlineCssVarDefinitions:
    def test_js_referenced_css_vars_defined_in_main_css(self):
        refs = set()
        for js in (WEBUI_DIR / "modules").rglob("*.js"):
            refs.update(re.findall(r"var\(--([a-zA-Z0-9-]+)\)",
                                   js.read_text(encoding="utf-8")))
        css = MAIN_CSS.read_text(encoding="utf-8")
        defined = set(re.findall(r"--([a-zA-Z0-9-]+)\s*:", css))
        undefined = {v for v in refs
                     if v not in defined and v not in KNOWN_UNDEFINED}
        assert not undefined, (
            f"JS 内联样式引用了未定义的 CSS 变量: {sorted(undefined)}；"
            "若属既有漂移请登记 KNOWN_UNDEFINED，否则请在 styles/main.css 定义")
