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
    """stopping 分支必须位于 ready/running 分支之后、else 之前，不被遮蔽。"""
    ready_idx = DASHBOARD_SOURCE.find("phase === 'ready' || status.running")
    stopping_idx = DASHBOARD_SOURCE.find("phase === 'stopping'")
    else_idx = DASHBOARD_SOURCE.find("} else {", ready_idx)
    assert ready_idx != -1 and stopping_idx != -1 and else_idx != -1
    assert ready_idx < stopping_idx < else_idx, (
        "stopping 分支应插在 ready 分支与最终 else 之间"
    )
