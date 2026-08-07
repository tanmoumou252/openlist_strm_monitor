from pathlib import Path
from unittest.mock import MagicMock, patch


WEBUI_ROOT = Path(__file__).parents[1] / "webui"


def _read(relative_path: str) -> str:
    return (WEBUI_ROOT / relative_path).read_text(encoding="utf-8")


def test_dashboard_does_not_keep_duplicate_onboarding_state():
    source = _read("modules/pages/dashboard.js")

    assert "_onboardingState" not in source


def test_visibility_resume_invalidates_stale_async_imports():
    source = _read("main.js")

    assert "_visibilityGeneration" in source
    assert "generation !== _visibilityGeneration" in source


def test_ping_status_does_not_override_configured_state():
    source = _read("modules/pages/openlist.js")
    ping_block = source.split("const resp = await api('/api/openlist/ping');", 1)[1]
    ping_block = ping_block.split("} catch (e)", 1)[0]

    assert "OpenListState.configured =" not in ping_block


# ---- V9 Audit Acceptance Regression Contracts ----

def test_build_engine_row_no_delete_disabled_reference():
    """buildEngineRow() must not reference undefined `deleteDisabled`.

    The variable only exists in _refreshEngineTable(); buildEngineRow()
    is called on initial render and would throw ReferenceError.
    """
    source = _read("modules/pages/openlist.js")
    # Extract buildEngineRow function body (between its declaration and the next function)
    idx = source.find("function buildEngineRow(")
    assert idx != -1, "buildEngineRow not found in openlist.js"
    # Find the closing by looking for the next top-level function or const
    rest = source[idx:]
    # Check for ${deleteDisabled} usage outside _refreshEngineTable
    # _refreshEngineTable has its own scope; buildEngineRow must not use it
    row_section = rest.split("function _refreshEngineTable")[0]
    assert "${deleteDisabled}" not in row_section, (
        "buildEngineRow() references undefined `deleteDisabled`; "
        "use a local computed value or inline the condition"
    )


def test_render_area_detail_no_mapping_id_param_reference():
    """renderAreaDetail() must not reference undefined `mappingIdParam`.

    The variable was removed but a template literal at line 206 still
    references it, causing ReferenceError on every A/B detail page.
    """
    source = _read("modules/pages/area.js")
    idx = source.find("async function renderAreaDetail(")
    assert idx != -1, "renderAreaDetail not found in area.js"
    rest = source[idx:]
    # Find the end of renderAreaDetail (next top-level function)
    end = rest.find("\nasync function ", len("async function renderAreaDetail("))
    if end == -1:
        end = rest.find("\nfunction ", len("async function renderAreaDetail("))
    if end != -1:
        rest = rest[:end]
    assert "mappingIdParam" not in rest, (
        "renderAreaDetail() still references undefined `mappingIdParam`; "
        "remove the reference or define the variable"
    )


def test_area_detail_no_dead_mapping_id_in_url():
    """createSortLink() must not output dead `mapping_id` in area detail URLs.

    List pages never pass mapping_id; the param is dead weight.
    """
    source = _read("modules/core/utils.js")
    assert "params.mapping_id" not in source, (
        "createSortLink() still includes dead `mapping_id` parameter; "
        "remove the mapping_id branch from URL construction"
    )


def test_csv_text_cell_safety():
    """CSV text cells starting with formula prefixes must be escaped.

    Cells starting with =, +, -, @ can trigger formula execution in
    spreadsheet applications. The export must prefix such cells.
    """
    routes_source = _open_routes()
    # Look for CSV export section
    csv_section = routes_source
    if "export.csv" in routes_source:
        idx = routes_source.find("export.csv")
        csv_section = routes_source[max(0, idx - 200):idx + 2000]
    # Check that formula-prefix safety is applied
    has_formula_guard = (
        "CSV_TEXT_PREFIX" in csv_section
        or "_csv_safe" in csv_section
        or ('startswith' in csv_section and ('=' in csv_section or '+' in csv_section))
        or 'prefix="="' in csv_section
        or "prefix='" in csv_section
        or '"""=' in csv_section
    )
    assert has_formula_guard, (
        "CSV export lacks formula-prefix safety for text cells; "
        "prefix cells starting with =, +, -, @ to prevent formula injection"
    )


def test_csv_safe_text_behavior():
    """_csv_safe_text() 纯函数行为：公式前缀加 tab，其余原样。"""
    from webui.routes import _csv_safe_text, _CSV_FORMULA_PREFIXES
    # 公式前缀字符
    for prefix in _CSV_FORMULA_PREFIXES:
        assert _csv_safe_text(prefix + "value") == "\t" + prefix + "value"
    # 非公式前缀原样返回
    assert _csv_safe_text("normal") == "normal"
    assert _csv_safe_text("123") == "123"
    assert _csv_safe_text("") == ""
    # 空值/非字符串原样返回
    assert _csv_safe_text(None) is None
    assert _csv_safe_text(123) == 123


def test_bg_sync_precheck_tmdb_client():
    """_do_bg_sync() should explicitly check _tmdb_client before calling sync().

    While try/except catches AttributeError, an explicit pre-check with
    a clear log message is safer and prevents silent degradation.
    """
    routes_source = _open_routes()
    idx = routes_source.find("def _do_bg_sync(")
    assert idx != -1, "_do_bg_sync not found in routes.py"
    func_body = routes_source[idx:idx + 1500]
    has_precheck = (
        "_tmdb_client" in func_body
        and ("is None" in func_body or "is not None" in func_body or "if not" in func_body)
    )
    assert has_precheck, (
        "_do_bg_sync() lacks explicit pre-check for _tmdb_client being None; "
        "add a guard before calling sync() with clear error logging"
    )


def test_bg_sync_none_client_logs_and_releases():
    """_do_bg_sync() 在 _tmdb_client 为 None 时应记录 warning 且释放 _sync_running。"""
    from webui.routes import _do_bg_sync
    import logging

    webui_server = MagicMock()
    webui_server._tmdb_client = None
    webui_server._watchlist_db = None
    webui_server._sync_running = True
    lock = MagicMock()
    lock.__enter__ = MagicMock(return_value=None)
    lock.__exit__ = MagicMock(return_value=False)
    webui_server._sync_lock = lock

    with patch("webui.routes.logging") as mock_logging:
        _do_bg_sync(webui_server)

    # 应记录 warning
    mock_logging.warning.assert_called()
    warning_msg = mock_logging.warning.call_args[0][0]
    assert "_tmdb_client" in warning_msg or "TMDB" in warning_msg
    # _sync_running 应在 finally 中释放
    assert webui_server._sync_running is False


def _open_routes() -> str:
    return (WEBUI_ROOT / "routes.py").read_text(encoding="utf-8")


def test_dialog_html_content_assert_regex():
    """dialog.js 的 console.assert 正则应允许 <br>/<br/> 且拒绝其他 <xxx 标签。"""
    dialog_source = (WEBUI_ROOT / "modules" / "components" / "dialog.js").read_text(encoding="utf-8")
    # 提取断言正则
    assert "/<(?!br\\s*\\/?>)[a-z]/i" in dialog_source, \
        "dialog.js 应包含允许 <br> 的负向前瞻正则"
    # 验证正则行为：允许 <br> 和 <br/>
    import re
    pattern = re.compile(r'<(?!br\s*\/?>)[a-z]', re.IGNORECASE)
    assert pattern.search("<br>") is None
    assert pattern.search("<br/>") is None
    assert pattern.search("<BR>") is None
    # 拒绝其他标签
    assert pattern.search("<script>") is not None
    assert pattern.search("<div>") is not None
    assert pattern.search("<span>") is not None


def test_shipped_docs_have_no_line_number_references():
    r"""交付文档（wiki/、docs/、README.md、src/tests/README.md）不应含行号引用。

    排除 .kilo/plans/（历史计划文件，gitignored）和 AGENTS.md（其规则 12 本身含示例）。
    正则限定：\.(py|js):\d+、lines?\s+\d+\s*-\s*\d+、第\s*\d+\s*行、:\d+-\d+
    避免误报端口（如 127.0.0.1:8579）。
    """
    import re
    project_root = WEBUI_ROOT.parent.parent
    # 要扫描的目录/文件（排除 .kilo/plans/ 和 AGENTS.md）
    doc_roots = [
        project_root / "wiki",
        project_root / "docs",
        project_root / "README.md",
        project_root / "src" / "tests" / "README.md",
    ]
    # 行号引用正则（避免宽泛 :\d{2,4} 误报端口；全程非捕获组）
    line_ref_re = re.compile(
        r'(?:\.(?:py|js):\d+)'
        r'|(?:lines?\s+\d+\s*-\s*\d+)'
        r'|(?:第\s*\d+\s*行)'
        r'|(?::\d+-\d+)'
    )
    violations = []
    for root in doc_roots:
        if not root.exists():
            continue
        if root.is_file():
            files = [root]
        else:
            files = list(root.rglob("*"))
        for f in files:
            if not f.is_file():
                continue
            # 排除 .kilo/plans/ 和 AGENTS.md
            rel = f.relative_to(project_root)
            if ".kilo" in rel.parts or f.name == "AGENTS.md":
                continue
            # 跳过二进制文件（图片、字体、数据库等）
            binary_exts = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff2", ".woff", ".ttf", ".eot", ".svg",
                           ".db", ".sqlite", ".sqlite3", ".woff2", ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
                           ".exe", ".dll", ".so", ".dylib", ".pyc", ".pyo"}
            if f.suffix.lower() in binary_exts:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            # 跳过含 null 字节的文件（二进制残留）
            if '\x00' in text:
                continue
            matches = line_ref_re.findall(text)
            if matches:
                violations.append((str(rel), matches[:3]))  # 最多报 3 个
    assert not violations, (
        "交付文档发现行号引用（应使用函数/类名而非行号）：\n"
        + "\n".join(f"  {f}: {m}" for f, m in violations)
    )


def test_router_render_guard_not_always_stale_free():
    """M9: router.js 必须将 _pageRenderGen 置为 -1，使 isRenderStale() 正确失效旧页。

    旧实现 `_pageRenderGen = myGen` 使 `_pageRenderGen === _renderGen` 恒成立，
    isRenderStale() 恒返回 false，12 处页面渲染护栏全部失效。
    """
    source = _read("modules/core/router.js")
    # isRenderStale 依赖 _pageRenderGen !== _renderGen
    assert "return _pageRenderGen !== _renderGen" in source
    # router() 内必须把 _pageRenderGen 置为 -1（而非同步为 myGen）
    assert "_pageRenderGen = -1" in source, (
        "router.js 应把 _pageRenderGen 置为 -1（原 `= myGen` 使 isRenderStale() 恒 false）"
    )
    # 旧实现 `_pageRenderGen = myGen;` 作为赋值语句不得存在
    # （注释中提及旧实现属正常，故用语句级关键词限定）
    assert "_pageRenderGen = myGen;" not in source


def test_parse_hash_tolerates_malformed_encoding():
    """L5: parseHash() 必须用 try/catch 包裹 decodeURIComponent，畸形编码回退原始串。

    旧实现 %zz 触发 URIError 使 router() 整体中止，SPA 路由失效。
    """
    source = _read("modules/core/router.js")
    assert "decodeURIComponent" in source
    # safeDecode 辅助函数应含 try/catch
    assert "try {" in source and "catch {" in source
    assert "return s;" in source, "safeDecode 回退分支应返回原始字符串"
    # 调用点应使用 safeDecode 而非裸 decodeURIComponent
    assert "safeDecode(k)" in source
    assert "safeDecode((v" in source or "safeDecode(" in source


def test_onboarding_completed_strict_comparison():
    """dashboard.js 中 onboarding_completed 必须使用严格比较 === '1'，不得出现真值判断。"""
    source = _read("modules/pages/dashboard.js")

    # 提取所有 onboarding_completed 出现位置
    import re
    occurrences = [m.start() for m in re.finditer(r'onboarding_completed', source)]

    assert occurrences, "onboarding_completed 未在 dashboard.js 中出现"

    forbidden_truthy_patterns = [
        r'if\s*\(\s*status\.onboarding_completed\s*\)',           # if (status.onboarding_completed)
        r'if\s*\(\s*status\s*&&\s*status\.onboarding_completed\s*\)',  # if (status && status.onboarding_completed)
        r'\?\s*status\.onboarding_completed\s*:',                  # ternary ? status.onboarding_completed :
        r'if\s*\(\s*[^)]*onboarding_completed[^)]*\)\s*\{',       # generic if with onboarding_completed
    ]

    violations = []
    for pattern in forbidden_truthy_patterns:
        for m in re.finditer(pattern, source):
            # 排除已包含 === '1' 的行（严格比较是合法的）
            line_start = source.rfind('\n', 0, m.start()) + 1
            line_end = source.find('\n', m.start())
            line_text = source[line_start:line_end]
            if "==='1'" not in line_text and "=== '1'" not in line_text:
                violations.append(f"行 {source[:m.start()].count(chr(10)) + 1}: {line_text.strip()}")

    assert not violations, (
        "dashboard.js 中发现 onboarding_completed 的真值判断（应使用 === '1'）：\n"
        + "\n".join(violations)
    )

    # 额外断言：至少存在一处 === '1' 用法（证明修复已应用）
    assert "=== '1'" in source or "==='1'" in source, \
        "dashboard.js 中 onboarding_completed 应至少有一处 === '1' 严格比较"
