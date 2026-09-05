"""侧栏的运行态逻辑：纯函数吃 Pane 元组、吐计划，这里一条 tmux 都不跑。

execute_* 那几行只是转发 tmux 命令，真正的验证在
research/experiments/2026-09-02-sidebar-swap/ 的隔离 socket 端到端脚本里。
"""

from __future__ import annotations

import pytest

from atm import sidebar, tmux
from atm.sidebar import ParkPlan, SidebarError, SwapKind, ToggleKind
from atm.tmux import Pane


def pane(
    id: str,
    *,
    window_id: str = "@0",
    command: str = "bash",
    last: bool = False,
    active: bool = False,
    is_sidebar: bool = False,
    width: int = 80,
    height: int = 24,
    session: str = "main",
    window_index: int = 0,
    pane_index: int = 0,
    title: str = "",
) -> Pane:
    return Pane(
        id=id,
        session=session,
        window_index=window_index,
        window_name="w",
        pane_index=pane_index,
        current_command=command,
        current_path="/home/sean",
        active=active,
        in_mode=False,
        window_active=True,
        width=width,
        height=height,
        title=title,
        window_id=window_id,
        last=last,
        is_sidebar=is_sidebar,
    )


# ---------------------------------------------------------------- parse：新字段


def test_parse_reads_window_id_last_and_sidebar_flag() -> None:
    fields = ["%7", "main", "0", "w", "1", "claude", "/p", "0", "0", "1", "80", "24", "✳ x"]
    p = tmux.parse_panes("\x1f".join([*fields, "@3", "1", "1"]))[0]
    assert p.window_id == "@3"
    assert p.last is True
    assert p.is_sidebar is True


def test_parse_tolerates_old_13_field_output() -> None:
    """老格式（没有后三个字段）也要能解析 —— 别让新字段把旧 tmux 的输出打挂。"""
    line = "\x1f".join(["%1", "main", "0", "w", "0", "bash", "/p", "1", "0", "1", "80", "24", "t"])
    p = tmux.parse_panes(line)[0]
    assert p.window_id == ""
    assert p.last is False
    assert p.is_sidebar is False


def test_sidebar_flag_empty_means_false() -> None:
    """没设过 @atm_sidebar 的 pane，格式串展开是空串，不是 0。"""
    line = "\x1f".join(
        ["%1", "main", "0", "w", "0", "bash", "/p", "1", "0", "1", "80", "24", "t", "@0", "0", ""]
    )
    assert tmux.parse_panes(line)[0].is_sidebar is False


# ---------------------------------------------------------------- 列表


def test_running_panes_excludes_sidebar_and_puts_ai_first() -> None:
    panes = (
        pane("%1", command="bash", pane_index=0),
        pane("%2", is_sidebar=True, pane_index=1),
        pane("%3", command="claude", window_id="@1", window_index=1),
        pane("%4", command="codex", pane_index=2),
    )
    rows = sidebar.running_panes(panes)
    assert [p.id for p in rows] == ["%4", "%3", "%1"]


def test_running_panes_can_exclude_self_before_marked() -> None:
    panes = (pane("%1"), pane("%2"))
    assert [p.id for p in sidebar.running_panes(panes, exclude="%2")] == ["%1"]


# ---------------------------------------------------------------- 主格


def test_main_slot_prefers_last_then_active_then_largest() -> None:
    sb = pane("%s", is_sidebar=True, active=True)
    big = pane("%big", width=200, height=50)
    last = pane("%last", last=True, width=10, height=10)
    act = pane("%act", active=True, width=10, height=10)

    assert sidebar.main_slot((sb, big, last, act), "@0").id == "%last"
    assert sidebar.main_slot((sb, big, act), "@0").id == "%act"
    assert sidebar.main_slot((sb, big, pane("%small")), "@0").id == "%big"


def test_main_slot_ignores_other_windows_and_sidebar() -> None:
    panes = (pane("%s", is_sidebar=True, last=True), pane("%o", window_id="@9", last=True))
    assert sidebar.main_slot(panes, "@0") is None


# ---------------------------------------------------------------- 换位


def test_plan_swap_target_in_same_window_is_focus_only() -> None:
    panes = (pane("%s", is_sidebar=True), pane("%a", last=True), pane("%b"))
    plan = sidebar.plan_swap(panes, window_id="@0", target_id="%b")
    assert plan.kind is SwapKind.FOCUS
    assert plan.slot is None


def test_plan_swap_target_elsewhere_swaps_with_main_slot() -> None:
    panes = (
        pane("%s", is_sidebar=True, active=True),
        pane("%a", last=True),
        pane("%bg", window_id="@1", command="claude"),
    )
    plan = sidebar.plan_swap(panes, window_id="@0", target_id="%bg")
    assert plan.kind is SwapKind.SWAP
    assert plan.target == "%bg"
    assert plan.slot == "%a"


def test_plan_swap_with_explicit_slot() -> None:
    panes = (
        pane("%s", is_sidebar=True),
        pane("%a", last=True),
        pane("%b"),
        pane("%bg", window_id="@1"),
    )
    plan = sidebar.plan_swap(panes, window_id="@0", target_id="%bg", slot_id="%b")
    assert (plan.kind, plan.slot) == (SwapKind.SWAP, "%b")
    # 指定了格子，就算目标在同一窗口也真的换位
    plan = sidebar.plan_swap(panes, window_id="@0", target_id="%a", slot_id="%b")
    assert (plan.kind, plan.slot) == (SwapKind.SWAP, "%b")
    with pytest.raises(SidebarError, match="侧栏"):
        sidebar.plan_swap(panes, window_id="@0", target_id="%bg", slot_id="%s")
    with pytest.raises(SidebarError, match="找不到目标格子"):
        sidebar.plan_swap(panes, window_id="@0", target_id="%bg", slot_id="%zz")


def test_swap_discards_idle_shell_slot_but_parks_busy_one() -> None:
    """换出去的旧格子：空闲 bash 直接关，跑着 claude 的收后台。"""
    bg = pane("%bg", window_id="@1", command="claude")
    idle = (pane("%s", is_sidebar=True), pane("%a", last=True, command="bash"), bg)
    assert sidebar.plan_swap(idle, window_id="@0", target_id="%bg").discard is True
    busy = (pane("%s", is_sidebar=True), pane("%a", last=True, command="claude"), bg)
    assert sidebar.plan_swap(busy, window_id="@0", target_id="%bg").discard is False
    # 显式覆盖
    assert sidebar.plan_swap(idle, window_id="@0", target_id="%bg", discard=False).discard is False
    assert sidebar.plan_swap(busy, window_id="@0", target_id="%bg", discard=True).discard is True


def test_execute_swap_kills_discarded_slot_last(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(tmux, "run", lambda args, **kw: calls.append(args) or "")
    plan = sidebar.SwapPlan(kind=SwapKind.SWAP, target="%bg", slot="%a", discard=True)
    sidebar.execute_swap(plan)
    assert calls == [
        ["swap-pane", "-s", "%bg", "-t", "%a"],
        ["select-pane", "-t", "%bg"],
        ["kill-pane", "-t", "%a"],
    ]


def test_plan_swap_refuses_missing_or_sidebar_target() -> None:
    panes = (pane("%s", is_sidebar=True), pane("%a"))
    with pytest.raises(SidebarError):
        sidebar.plan_swap(panes, window_id="@0", target_id="%gone")
    with pytest.raises(SidebarError):
        sidebar.plan_swap(panes, window_id="@0", target_id="%s")


def test_plan_swap_needs_a_main_slot() -> None:
    """窗口里只有侧栏，没地方放换进来的 pane。"""
    panes = (pane("%s", is_sidebar=True), pane("%bg", window_id="@1"))
    with pytest.raises(SidebarError, match="没法换位"):
        sidebar.plan_swap(panes, window_id="@0", target_id="%bg")


# ---------------------------------------------------------------- 停车


def test_plan_park_joins_existing_bg_or_breaks_new() -> None:
    panes = (pane("%a", last=True), pane("%b"))
    joined = sidebar.plan_park(panes, pane_id="%a", bg_window="@5")
    assert joined == ParkPlan(pane="%a", session="main", existing_window="@5")
    fresh = sidebar.plan_park(panes, pane_id="%a", bg_window=None)
    assert fresh.existing_window is None


def test_plan_park_refuses_sidebar_and_lone_pane() -> None:
    """单 pane 窗口 break-pane 实测不报错、只是把窗口改名 —— 那不是用户要的。"""
    panes = (pane("%s", is_sidebar=True), pane("%a"), pane("%solo", window_id="@2"))
    with pytest.raises(SidebarError, match="侧栏"):
        sidebar.plan_park(panes, pane_id="%s", bg_window=None)
    with pytest.raises(SidebarError, match="已经独占"):
        sidebar.plan_park(panes, pane_id="%solo", bg_window=None)


def test_plan_park_refuses_pane_already_in_bg() -> None:
    panes = (pane("%a", window_id="@5"), pane("%b", window_id="@5"))
    with pytest.raises(SidebarError, match="已经在"):
        sidebar.plan_park(panes, pane_id="%a", bg_window="@5")


def test_park_lone_pane_next_to_sidebar_is_allowed() -> None:
    """窗口里除侧栏外只剩一格：收走它之后侧栏还在，窗口不会消失。"""
    panes = (pane("%s", is_sidebar=True), pane("%a"))
    assert sidebar.plan_park(panes, pane_id="%a", bg_window=None).pane == "%a"


# ---------------------------------------------------------------- 清理


def test_prunable_only_idle_shells_in_bg() -> None:
    panes = (
        pane("%idle", window_id="@5", command="bash"),
        pane("%busy", window_id="@5", command="claude"),
        pane("%elsewhere", window_id="@0", command="bash"),
        pane("%sb", window_id="@5", command="bash", is_sidebar=True),
    )
    # pane() 的 window_name 固定是 "w"，这里手动造 bg 里的
    from dataclasses import replace

    panes = tuple(replace(p, window_name="bg") if p.window_id == "@5" else p for p in panes)
    assert [p.id for p in sidebar.prunable(panes)] == ["%idle"]


def test_execute_prune_skips_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(args, **kw):
        if args == ["kill-pane", "-t", "%gone"]:
            raise tmux.TmuxError("can't find pane")
        return ""

    monkeypatch.setattr(tmux, "run", run)
    killed = sidebar.execute_prune((pane("%gone"), pane("%ok")))
    assert [p.id for p in killed] == ["%ok"]


# ---------------------------------------------------------------- 开关


def test_toggle_open_focus_close_cycle() -> None:
    no_sidebar = (pane("%a", active=True),)
    assert sidebar.plan_toggle(no_sidebar, current_pane="%a").kind is ToggleKind.OPEN

    with_sidebar = (pane("%s", is_sidebar=True), pane("%a", active=True))
    plan = sidebar.plan_toggle(with_sidebar, current_pane="%a")
    assert plan.kind is ToggleKind.FOCUS
    assert plan.sidebar_id == "%s"

    plan = sidebar.plan_toggle(with_sidebar, current_pane="%s")
    assert plan.kind is ToggleKind.CLOSE


def test_toggle_only_sees_sidebar_in_own_window() -> None:
    panes = (pane("%s", is_sidebar=True, window_id="@1"), pane("%a"))
    assert sidebar.plan_toggle(panes, current_pane="%a").kind is ToggleKind.OPEN


def test_toggle_unknown_pane() -> None:
    with pytest.raises(SidebarError):
        sidebar.plan_toggle((pane("%a"),), current_pane="%zz")


# ---------------------------------------------------------------- execute：只验证转发给 tmux 的参数


def test_execute_swap_calls_swap_then_focus(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(tmux, "run", lambda args, **kw: calls.append(args) or "")
    sidebar.execute_swap(sidebar.SwapPlan(kind=SwapKind.SWAP, target="%bg", slot="%a"))
    assert calls == [["swap-pane", "-s", "%bg", "-t", "%a"], ["select-pane", "-t", "%bg"]]


def test_execute_toggle_open_marks_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args, **kw):
        calls.append(args)
        return "%9\n" if args[0] == "split-window" else ""

    monkeypatch.setattr(tmux, "run", fake_run)
    plan = sidebar.TogglePlan(kind=ToggleKind.OPEN, window_id="@0")
    assert sidebar.execute_toggle(plan, command="atm sidebar", width=30) == "%9"
    split = calls[0]
    assert split[:2] == ["split-window", "-f"], "必须 -f 才是通高侧栏"
    assert "-b" in split and split[split.index("-l") + 1] == "30"
    assert ["set-option", "-p", "-t", "%9", tmux.SIDEBAR_OPTION, "1"] in calls
