"""dashboard.js 主程序相位契约静态断言（零 node、零 DOM）。

模式仿 test_webui_source_contracts.py：直接读取源码文本做 in / 精确串断言。
锁定两个契约：
1. `_lastMainPhase` 相位基线门的终态集合必须含 stopping（停止中相位变化
   也要刷新引导④，避免 stop_main 失败时错误勾选滞留）。
2. updateMainStatus 渲染分支链必须存在独立 stopping case，文案表达
   「正在停止」语义，不得落入「已停止」else 分支。
"""

from pathlib import Path

WEBUI_ROOT = Path(__file__).parents[1] / "webui"

DASHBOARD_SOURCE = (WEBUI_ROOT / "modules" / "pages" / "dashboard.js").read_text(
    encoding="utf-8"
)


def test_phase_baseline_gate_includes_stopping():
    """相位基线门终态集合必须含 stopping。"""
    assert (
        "['ready', 'fail_safe', 'stopped', 'stopping'].includes(phase)" in DASHBOARD_SOURCE
    ), "_lastMainPhase 基线门终态集合应含 stopping（见 dashboard.js updateMainStatus）"


def test_stopping_phase_has_dedicated_render_branch():
    """渲染分支链必须有独立 stopping case，且文案表达「正在停止」语义。"""
    assert "phase === 'stopping'" in DASHBOARD_SOURCE, (
        "updateMainStatus 应有独立 stopping 渲染分支，不得落入「已停止」else"
    )
    assert "正在停止主程序..." in DASHBOARD_SOURCE, (
        "stopping 分支文案应表达「正在停止」而非「已停止」终态"
    )


def test_stopping_branch_not_shadowed_by_running_branch():
    """stopping 分支必须位于运行分支之前（契约防回归：消除
    fallback 公式 `phase not in {"stopped", "fail_safe"}` 与相位竞态下的
    理论遮蔽面——stopping 期间一旦被报 running，后置分支不可达）。
    运行分支以 status.running 为存活权威（running=true 任意相位走运行
    分支；phase==='ready' && !running 落异常态分支）。"""
    running_idx = DASHBOARD_SOURCE.find("} else if (status.running) {")
    stopping_idx = _stopping_branch_index(DASHBOARD_SOURCE)
    assert running_idx != -1 and stopping_idx != -1
    assert stopping_idx < running_idx, (
        "stopping 分支必须整体位于运行分支之前，"
        "防止 stopping 期间 running 判定为真时该分支被遮蔽不可达"
    )


def _stopping_branch_index(source: str) -> int:
    """stopping 渲染分支唯一定位锚：分支链形态精确串。
    严禁回退为 find("phase === 'stopping'")——任意早于分支链的出现点
    （注释/其他条件）都会令遮蔽断言误通过。"""
    return source.find("} else if (phase === 'stopping') {")


def test_stopping_branch_locator_rejects_shadowed_sample():
    """构造样例反假阳性：running 分支前插入其他 `phase === 'stopping'`
    出现点时，精确定位必须返回 -1（找不到分支链形态）而非误取早现索引。"""
    sample = (
        "// 早期出现点: phase === 'stopping'（假阳性注入）\n"
        "if (phase === 'stopping') { console.log('x'); }\n"
        "if (status.running) { run(); }\n"
        "} else if (status.running) {\n"
        "  text.textContent = '主程序运行中';\n"
        "}"
    )
    assert _stopping_branch_index(sample) == -1, (
        "早于分支链的 stopping 出现点不得被当作渲染分支定位（旧 find 形态在此误通过）")
    assert _stopping_branch_index(DASHBOARD_SOURCE) != -1, (
        "真实 dashboard 源必须能定位到 stopping 渲染分支链")


def test_running_branch_keys_on_survival_authority():
    """运行分支不得以相位 ready 单独判运行：必须以 status.running 为存活
    权威，且 ready 相位与存活脱钩（ready 且未运行）须落异常态恢复分支，
    不得误渲染为「主程序运行中」。"""
    legacy_idx = DASHBOARD_SOURCE.find("phase === 'ready' || status.running")
    assert legacy_idx == -1, (
        "运行分支不得回退为 `phase === 'ready' || status.running`——"
        "相位 ready 但未运行时会被误判为运行中（存活权威必须键于 "
        "status.running）")
    assert "} else if (status.running) {" in DASHBOARD_SOURCE, (
        "运行分支必须以 status.running 为存活权威")
    assert "phase === 'ready' && !status.running" in DASHBOARD_SOURCE, (
        "ready 且未运行的异常态必须有独立恢复分支（异常提示 + 启动入口）")


def test_stop_failure_awaits_status_refresh_before_button_reset():
    """E2 契约：停止失败分支必须 await updateMainStatus() 后再复位按钮，
    消除「刷新渲染与按钮复位交错」竞态；成功分支保持裸调（终态渲染自会
    隐藏 stopBtn），不该触发域零扰动。"""
    failure_idx = DASHBOARD_SOURCE.find("停止失败: ' + (result.message")
    assert failure_idx != -1
    seg_end = DASHBOARD_SOURCE.find("} catch (e)", failure_idx)
    seg = DASHBOARD_SOURCE[failure_idx:seg_end]
    assert "await updateMainStatus();" in seg, (
        "停止失败分支必须 await 状态刷新后再复位按钮（竞态消除）")
    # 复位必须位于 await 之后（顺序锚）
    await_idx = seg.find("await updateMainStatus();")
    reset_idx = seg.find("stopBtn.innerHTML = `${icon('check')} 停止主程序`;")
    assert reset_idx != -1 and reset_idx > await_idx, (
        "按钮 innerHTML 复位必须位于 await updateMainStatus() 之后")


def test_running_branch_resets_stop_button_and_surfaces_error():
    """E2 契约：运行态分支必须复位 stopBtn.innerHTML（spinner「停止中...」
    不得永久残留）且 error 非空时透出「主程序运行中：<error>」。"""
    running_idx = DASHBOARD_SOURCE.find("} else if (status.running) {")
    assert running_idx != -1
    seg_end = DASHBOARD_SOURCE.find("} else if (phase === 'ready'", running_idx)
    seg = DASHBOARD_SOURCE[running_idx:seg_end]
    assert "stopBtn.innerHTML = `${icon('check')} 停止主程序`;" in seg, (
        "运行态分支必须复位 stopBtn.innerHTML，防停止失败后 spinner 残留")
    assert "status.error" in seg and "主程序运行中：" in seg, (
        "运行态分支须透出 status.error（配合 stop_main 拒绝分支 error 透出）")


def test_stopping_branch_keeps_start_button_visible_but_disabled():
    """stopping 分支不得同时隐藏 startBtn 与 stopBtn：停止失败相位滞留
    stopping 时，用户必须仍能看到（但不可点）启动按钮作为恢复入口。"""
    stop_idx = DASHBOARD_SOURCE.find("phase === 'stopping'")
    assert stop_idx != -1
    # 分支体取到下一个 else if / else 为止（分支链单分支长度有限）
    next_branch = DASHBOARD_SOURCE.find("} else if", stop_idx)
    end_idx = DASHBOARD_SOURCE.find("} else {", stop_idx)
    seg_end = min(i for i in (next_branch, end_idx) if i != -1)
    seg = DASHBOARD_SOURCE[stop_idx:seg_end]
    assert "startBtn.style.display = 'inline-flex'" in seg, (
        "stopping 分支应保留 startBtn 可见（disabled 兜底，防空死角）")
    assert "startBtn.disabled = true" in seg, (
        "stopping 分支 startBtn 应为 disabled 状态（停止进行中不可再启动）")
    assert "startBtn.style.display = 'none'" not in seg, (
        "stopping 分支不得隐藏 startBtn（stop_main 失败滞留 stopping 时"
        "双按钮全隐属不可恢复 UI 死角）")


def test_running_branch_error_has_warning_color_and_length_guard():
    """N2 契约：运行态分支 error 非空时须转告警色（文本 + 状态点）并做
    120 字符截断 + title 存完整原因；error 为空路径不得染告警色（不该
    触发域零扰动）。期望值由行为契约推导：告警色只出现在 if (status.error)
    子分支内，else 子分支维持绿点健康配色。"""
    running_idx = DASHBOARD_SOURCE.find("} else if (status.running) {")
    assert running_idx != -1
    seg_end = DASHBOARD_SOURCE.find("} else if (phase === 'ready'", running_idx)
    seg = DASHBOARD_SOURCE[running_idx:seg_end]
    assert "String(status.error).slice(0, 120)" in seg, (
        "运行态 error 须截断 120 字符防溢出")
    assert "text.title = String(status.error);" in seg, (
        "完整拒绝原因须写入 title 供悬浮查看")
    err_if_idx = seg.find("if (status.error) {")
    assert err_if_idx != -1, "运行态分支须以 if (status.error) 区分错误/健康展示"
    else_idx = seg.find("} else {", err_if_idx)
    assert else_idx != -1, "运行态分支须有 error 为空的 else 健康态路径"
    warn_idx = seg.find("var(--warning, #ff9800)", err_if_idx, else_idx)
    assert warn_idx != -1, (
        "error 非空子分支内文本须转告警色 var(--warning, #ff9800)")
    assert "dot.style.background = '#ff9800';" in seg[:else_idx], (
        "error 非空时状态点须用告警色（对齐 stopping 分支形态）")
    assert seg.find("dot.style.background = '#4caf50';", else_idx) != -1, (
        "error 为空的 else 路径须维持绿点健康配色")
    assert "text.title = '';" in seg[else_idx:], (
        "error 为空时须清空 title 防上一轮渲染残留悬浮提示")
