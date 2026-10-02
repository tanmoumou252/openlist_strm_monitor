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


# ---- Audit Acceptance Regression Contracts ----

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
    """dialog.js 的运行时守卫应拒绝 XSS 标签（script/iframe/object/embed/img+事件/javascript:）。"""
    dialog_source = (WEBUI_ROOT / "modules" / "components" / "dialog.js").read_text(encoding="utf-8")
    # 提取运行时守卫正则
    assert "/<script|<iframe|<object|<embed|<img[^>]+\\bon\\w|javascript:/i" in dialog_source, \
        "dialog.js 应包含运行时 XSS 守卫正则"
    # 验证正则行为：拒绝 XSS 标签
    import re
    pattern = re.compile(r'<script|<iframe|<object|<embed|<img[^>]+\bon\w|javascript:', re.IGNORECASE)
    assert pattern.search("<script>") is not None
    assert pattern.search("<iframe>") is not None
    assert pattern.search("<object>") is not None
    assert pattern.search("<embed>") is not None
    assert pattern.search('javascript:alert(1)') is not None
    # 允许安全标签
    assert pattern.search("<br>") is None
    assert pattern.search("<br/>") is None
    assert pattern.search("<div>") is None
    assert pattern.search("<span>") is None


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
    """router.js 导出 captureRenderGuard() 工厂，页面渲染护栏用代际快照防陈旧覆盖。

    旧实现 `_pageRenderGen = myGen` 使 `_pageRenderGen === _renderGen` 恒成立，
    isRenderStale() 恒返回 false，12 处页面渲染护栏全部失效。
    docs/否决方案.md 架构约束：不要退回模块级单变量 isRenderStale。
    """
    source = _read("modules/core/router.js")
    # captureRenderGuard() 工厂是当前渲染护栏的基础
    assert "captureRenderGuard" in source
    # 废弃的单变量 _pageRenderGen 模式不得作为渲染护栏存在
    assert "return _pageRenderGen !== _renderGen" not in source
    # 旧实现 `_pageRenderGen = myGen;` 作为赋值语句不得存在
    # （注释中提及旧实现属正常，故用语句级关键词限定）
    assert "_pageRenderGen = myGen;" not in source


def test_parse_hash_tolerates_malformed_encoding():
    """parseHash() 必须用 try/catch 包裹 decodeURIComponent，畸形编码回退原始串。

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


def test_webui_enabled_self_disable_path_removed():
    """WebUI 是主程序入口，不存在可关闭自身的配置字段或运行分支。

    问题 33 统一审计收敛：`WebUIConfig.enabled` 已从模型、解析与消费路径
    正式移除；旧配置残留的 `[webui].enabled=false` 由 TOML 加载自然忽略，
    不增加永久兼容字段或迁移分支。Bridge 启动方式不受影响。
    """
    project_root = WEBUI_ROOT.parent
    config_source = (project_root / "config.py").read_text(encoding="utf-8")
    server_source = _read("server.py")

    # WebUIConfig 定义段不得包含 enabled 字段
    wui_idx = config_source.find("class WebUIConfig:")
    assert wui_idx != -1, "WebUIConfig 类未找到"
    # 仅截取 WebUIConfig 类体：到下一个 @dataclass 或 class 定义为止
    next_class = config_source.find("@dataclass", wui_idx + 1)
    if next_class == -1:
        next_class = config_source.find("class ", wui_idx + 1)
    wui_block = config_source[wui_idx:next_class if next_class != -1 else wui_idx + 400]
    assert "enabled" not in wui_block, (
        "WebUIConfig 仍定义 enabled 字段；该字段应已移除，"
        "旧配置中的 enabled 由 TOML 加载自然忽略"
    )

    # from_file 的 [webui] 解析段不得再读取 enabled
    assert 'webui_data.get("enabled"' not in config_source, (
        "AppConfig.from_file 仍在解析 [webui].enabled"
    )

    # WebUIServer 启动路径不得存在 _enabled 保存或自关闭分支
    assert "self._enabled" not in server_source, (
        "WebUIServer 仍保存 _enabled；应移除该消费路径"
    )
    assert "已禁用，跳过启动" not in server_source, (
        "WebUIServer.start() 仍存在自关闭分支；WebUI 应始终启动"
    )


# ---- 数字字段零值保护契约（Task C）----

def test_openlist_field_value_preserves_zero():
    """openlist.js 读取数字字段必须用严格空字符串判断，不得用 value || default 吞掉零值。

    回归守卫：refresh_full_audit_interval_days=0 与
    behavior_sync_on_startup_wait=0 是合法配置，`||` 会把 0 误判为缺省。
    """
    source = _read("modules/pages/openlist.js")
    assert "_olFieldValue" in source, "openlist.js 应存在 _olFieldValue 辅助函数"
    # 严格判断：仅当 raw === undefined 或 raw === '' 时回退
    assert "raw === undefined || raw === ''" in source or \
        "raw === '' || raw === undefined" in source, (
        "_olFieldValue 必须用严格空字符串判断回退"
    )
    # 读取零值字段的调用应走 _olFieldValue，而不是裸 || 表达式
    assert "_olFieldValue('ol-refresh-audit-days'" in source, (
        "refresh_full_audit_interval_days 应通过 _olFieldValue 读取"
    )
    assert "_olFieldValue('ol-startup-wait'" in source, (
        "behavior_sync_on_startup_wait 应通过 _olFieldValue 读取"
    )


def test_openlist_no_value_or_default_for_numeric_fields():
    """openlist.js 保存体不得用 `value || defaultValue` 处理数字字段。"""
    source = _read("modules/pages/openlist.js")
    # 定位保存体（_olFieldValue 调用点所在的对象字面量）
    idx = source.find("_olFieldValue('ol-refresh-interval'")
    assert idx != -1, "未找到保存体中的 _olFieldValue 调用"
    save_block = source[idx:idx + 600]
    # 数字字段不得出现 `|| '` 回退（._olFieldValue 内部已有回退逻辑）
    forbidden = [
        "getElementById('ol-refresh-interval')?.value ||",
        "getElementById('ol-refresh-depth')?.value ||",
        "getElementById('ol-refresh-audit-days')?.value ||",
        "getElementById('ol-ghost-protect')?.value ||",
        "getElementById('ol-restore-delay')?.value ||",
        "getElementById('ol-startup-wait')?.value ||",
        "getElementById('ol-log-max-size')?.value ||",
        "getElementById('ol-log-backup-count')?.value ||",
    ]
    for f in forbidden:
        assert f not in source, f"数字字段不得用 || 回退默认值（会吞掉合法零值）: {f}"


# ---- 详情返回状态契约（Task C）----

def test_area_detail_link_preserves_list_state():
    """area.js 详情链接必须保留 {kind, q, sort, order, page, page_size} 来源状态。

    关键约束：
    - kind / q / sort / order 必须透传；
    - page 仅当 page > 1 时透传（第 1 页省略）；
    - page_size 仅接受 50/100/200，其他值省略。
    """
    source = _read("modules/pages/area.js")

    # detailHref 函数体（列表 → 详情）
    idx = source.find("function detailHref(")
    assert idx != -1, "area.js 未找到 detailHref 函数"
    rest = source[idx:]
    end = rest.find("\n  }", 0)
    if end == -1:
        end = rest.find("\n\n", 0)
    fn_block = rest[:end if end != -1 else 900]

    # 来源状态透传
    assert "kind" in fn_block and "encodeURIComponent(kind)" in fn_block
    assert "q" in fn_block and "encodeURIComponent(q)" in fn_block
    assert "sort" in fn_block and "encodeURIComponent(sort)" in fn_block
    assert "order" in fn_block and "encodeURIComponent(order)" in fn_block
    # page 仅在 >1 时透传（避免第 1 页 URL 携带冗余 page=1）
    assert "if (page && page > 1) p.push('page='" in fn_block, \
        "detailHref 应在 page>1 时才透传 page"
    # page_size 白名单 50/100/200
    assert "pageSize === 50 || pageSize === 100 || pageSize === 200" in fn_block, \
        "detailHref 应仅透传合法 page_size (50/100/200)"


def test_area_back_link_preserves_list_state():
    """area.js 详情页返回链接必须保留来源列表状态。"""
    source = _read("modules/pages/area.js")
    idx = source.find("const backParts = []")
    assert idx != -1, "area.js 未找到 backParts 数组"
    rest = source[idx:]
    end = rest.find("\n  const kindPart")
    assert end != -1
    block = rest[:end]

    assert "encodeURIComponent(kind)" in block
    assert "encodeURIComponent(q)" in block
    assert "encodeURIComponent(sort)" in block
    assert "encodeURIComponent(order)" in block
    # page 仅 >1 透传
    assert "if (page && page > 1) backParts.push('page='" in block
    # page_size 白名单
    assert "pageSize !== null" in block, \
        "返回链接应仅在有合法 page_size 时透传"


def test_area_page_size_whitelist_rejects_invalid():
    """area.js 列表/详情解析 page_size 时必须过滤非 50/100/200 的值。

    0、负数、NaN、其他数值应解析为 null（不写入 URL）。
    """
    source = _read("modules/pages/area.js")
    idx = source.find("const pageSizeRaw = parseInt(params.page_size)")
    assert idx != -1, "area.js 未找到 pageSizeRaw 解析点"
    rest = source[idx:]
    end = rest.find("\n\n", 0)
    block = rest[:end if end != -1 else 500]
    # 白名单条件
    assert "pageSizeRaw === 50 || pageSizeRaw === 100 || pageSizeRaw === 200" in block, \
        "详情解析应仅接受 50/100/200，非法值解析为 null"


def test_area_search_and_kind_switch_reset_page():
    """搜索提交与分类切换必须主动移除旧 page（P2-1/P2-2 既有决定）。

    回归守卫：搜索或切换分类后应回到第 1 页，不得保留来源页。
    """
    source = _read("modules/pages/area.js")
    # 定位搜索提交事件绑定（// Bind search 注释之后）
    search_idx = source.find("// Bind search")
    assert search_idx != -1, "area.js 未找到搜索事件绑定"
    search_block = source[search_idx:search_idx + 800]
    # 搜索提交 URL 构造不含 page=（主动移除旧 page）
    assert "let h = `#area_" in search_block
    # 搜索 hash 只追加 kind/q，无 page
    assert "p.push('kind=all')" in search_block
    assert "p.push('q=' + encodeURIComponent(val))" in search_block
    assert "page=" not in search_block.split("navigate(h)")[0], \
        "搜索提交不应携带 page 参数（应回到第一页）"
    assert "navigate(h)" in search_block
    # 分类 tab 事件绑定（data-kind-href）
    tab_idx = source.find("document.querySelectorAll('.category-tab[data-kind-href]')")
    assert tab_idx != -1
    tab_block = source[tab_idx:tab_idx + 300]
    assert "navigate(tab.dataset.kindHref)" in tab_block


def test_create_field_passes_numeric_attributes():
    """createField 必须透传 min/max/step/inputMode 到 <input> 元素。"""
    source = _read("modules/core/utils.js")
    idx = source.find("export function createField(")
    assert idx != -1, "utils.js 未找到 createField"
    rest = source[idx:]
    end = rest.find("\n\n", 0)
    fn_block = rest[:end if end != -1 else 1200]

    for attr in ("min = ''", "max = ''", "step = ''", "inputMode = ''"):
        assert attr in fn_block, f"createField 应解构 {attr}"
    # 输出到 input 的属性拼接
    assert 'min="${esc(min)}"' in fn_block
    assert 'max="${esc(max)}"' in fn_block
    assert 'step="${esc(step)}"' in fn_block
    assert 'inputmode="${esc(inputMode)}"' in fn_block


def test_tmdb_ratio_fields_min_0_01():
    """config.js 三个 TMDB 比例字段 min 必须为 '0.01'（与后端 (0,1] 契约一致）。

    fuzzy_threshold / anime_min_ep_ratio / anime_min_season_ratio 由
    _handle_tmdb_configure 校验为 (0,1]，min='0' 与后端契约不一致。
    step 保留 '0.01'。
    """
    source = _read("modules/pages/config.js")
    for field_id, label in [
        ("cfg-tmdb-fuzzy", "模糊匹配阈值"),
        ("cfg-tmdb-ep-ratio", "番剧最少集数比例"),
        ("cfg-tmdb-min-season-ratio", "番剧最少季数比例"),
    ]:
        idx = source.find(f"field('{field_id}'")
        assert idx != -1, f"config.js 未找到 {field_id}"
        line_end = source.find("\n", idx)
        line = source[idx:line_end]
        assert "min: '0.01'" in line, (
            f"{label} ({field_id}) 的 min 应为 '0.01'，实际行: {line}")
        assert "step: '0.01'" in line, (
            f"{label} ({field_id}) 应保留 step: '0.01'，实际行: {line}")
        assert "min: '0'" not in line, (
            f"{label} ({field_id}) 不得再使用 min: '0'")

    # 非比例字段（季数差 / 缓存 TTL）不受影响
    season_diff_line = next(
        (l for l in source.splitlines() if "cfg-tmdb-season-diff'" in l), "")
    assert "min: '0'" in season_diff_line, \
        "cfg-tmdb-season-diff 为非比例步进字段，min 应保持 '0'"
    assert "step: '1'" in season_diff_line, \
        "cfg-tmdb-season-diff 应保留 step: '1'"


def test_b_reconcile_and_insert_are_serial_lineage_verification():
    """断言 B 区历史核对与新增入库中的血统校验副作用面严格在主线程串行调用。

    T2b 契约修订（C1/C12-3，与 docs/否决方案.md「启动血统预检并行化」同提交）：
    - _reconcile_b_historical_records：无线程池字面量；wave1 必须经
      _preflight_b_lineage_parallel（v6 P1，与 _insert_new_b_records 同构）；
      保留完整 _verify_b_path_lineage 串行回退段（预检未放行与 C/D 分支）；
    - _insert_new_b_records：允许 _preflight_b_lineage_parallel（第 1-4 步
      纯读并行预检）；_verify_b_path_lineage 仅出现在串行回退段；函数体
      不得内联 ThreadPoolExecutor 字面量（池必须留在 _quarantine_duplicate_
      groups_batch / _preflight_b_lineage_parallel 等独立方法体内）；
    - _b_lineage_preflight / _preflight_b_lineage_parallel 函数体纳入扫描：
      副作用面六函数名不得出现（第 5-9 步严禁进并行段）；
      _b_lineage_preflight 单函数体不得含 ThreadPoolExecutor。
    契约测试为字符串扫描，helper 体已纳入扫描范围，绕过路径已闭合（登记册）。
    """
    core_source = (Path(__file__).parents[1] / "app_service_core.py").read_text(encoding="utf-8")

    def _method_body(marker: str) -> str:
        start = core_source.find(marker)
        assert start != -1, f"未找到 {marker}"
        end = core_source.find("\n    def ", start + 1)
        assert end != -1, f"未找到 {marker} 的函数体结束"
        return core_source[start:end]

    # _reconcile_b_historical_records：两波结构（v6 P1）——禁内联线程池、
    # 必须经并行预检 helper、保留完整校验串行回退
    rec_body = _method_body("def _reconcile_b_historical_records(")
    assert "ThreadPoolExecutor" not in rec_body, "历史核对严禁内联线程池字面量"
    assert "_preflight_b_lineage_parallel" in rec_body, (
        "历史核对应经 _preflight_b_lineage_parallel 并行预检（wave1，v6 P1）")
    assert "_verify_b_path_lineage" in rec_body, (
        "历史核对必须保留 _verify_b_path_lineage 串行回退（wave2）")

    # _insert_new_b_records：允许并行预检，副作用面保持串行
    ins_body = _method_body("def _insert_new_b_records(")
    assert "ThreadPoolExecutor" not in ins_body, (
        "新增入库严禁内联线程池（改名池/预检池必须留在独立方法体内）")
    assert "_preflight_b_lineage_parallel" in ins_body, (
        "新增入库应经 _preflight_b_lineage_parallel 并行预检（wave1）")
    assert "_verify_b_path_lineage" in ins_body, (
        "新增入库必须保留 _verify_b_path_lineage 串行回退（wave2）")

    # 第 5-9 步副作用面严禁进入并行段（C12-3：helper 体纳入扫描）
    _SIDE_EFFECT_MARKERS = (
        "_check_solo_episode",
        "_handle_sync_phase_boundary",
        "safe_remove_file",
        "_check_boundary_files",
        "_check_boundary_mappings",
        "_resolve_cloud_and_physical_names",
    )
    preflight_body = _method_body("def _b_lineage_preflight(")
    assert "ThreadPoolExecutor" not in preflight_body, (
        "_b_lineage_preflight 单函数体严禁含线程池（并行 worker 只执行纯读预检）")
    for marker in _SIDE_EFFECT_MARKERS:
        assert marker not in preflight_body, (
            f"_b_lineage_preflight 不得调用副作用函数 {marker}（第 5-9 步保持串行）")

    parallel_body = _method_body("def _preflight_b_lineage_parallel(")
    for marker in _SIDE_EFFECT_MARKERS:
        assert marker not in parallel_body, (
            f"_preflight_b_lineage_parallel 不得调用副作用函数 {marker}（第 5-9 步保持串行）")

    # A1（c7 §5.3，F-5 同款规则）：Wave0 复用检查并行 helper——池字面量允许
    # 在 helper 体内，但六副作用 marker 严禁出现（纯读复用检查段）；reconcile
    # 函数体必须经 helper 引用（两遍串行保序消费结构）。
    reuse_body = _method_body("def _snapshot_reuse_check_parallel(")
    for marker in _SIDE_EFFECT_MARKERS:
        assert marker not in reuse_body, (
            f"_snapshot_reuse_check_parallel 不得调用副作用函数 {marker}（纯读复用检查）")
    assert "_snapshot_reuses_valid_lineage" in reuse_body, (
        "复用检查并行 worker 必须经 _snapshot_reuses_valid_lineage（单一事实源）")
    assert "_snapshot_reuse_check_parallel" in rec_body, (
        "历史核对 Wave0 应经 _snapshot_reuse_check_parallel 并行复用检查")


def test_denial_registry_contract_format():
    """验证 docs/否决方案.md 格式紧凑、无硬编码具体行号。"""
    import re
    doc_path = Path(__file__).parents[2] / "docs" / "否决方案.md"
    assert doc_path.exists()
    content = doc_path.read_text(encoding="utf-8")

    # 不含具体代码行号引用（如 .py:123）
    assert not re.search(r"\.py:\d+", content), "否决方案.md 不得包含硬编码代码行号"
    assert "# 设计决策:" in content
    assert "# 有意保留:" in content
    assert "# 已知取舍:" in content
    assert "# [已废弃]" in content

