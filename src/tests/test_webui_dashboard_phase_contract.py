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
    """stopping 分支必须位于 ready/running 分支之前（契约防回归：消除
    fallback 公式 `phase not in {"stopped", "fail_safe"}` 与相位竞态下的
    理论遮蔽面——stopping 期间一旦被报 running，后置分支不可达）。"""
    ready_idx = DASHBOARD_SOURCE.find("phase === 'ready' || status.running")
    stopping_idx = DASHBOARD_SOURCE.find("phase === 'stopping'")
    assert ready_idx != -1 and stopping_idx != -1
    assert stopping_idx < ready_idx, (
        "stopping 分支必须整体位于 ready/running 分支之前，"
        "防止 stopping 期间 running 判定为真时该分支被遮蔽不可达"
    )


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
