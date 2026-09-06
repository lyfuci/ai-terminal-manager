"""常驻侧栏的「运行态」逻辑：哪些格子在跑、选中一个之后怎么把它换进视野。

这是 2026-09-02 加的第二个维度。原来 atm 只管**历史**（L3：磁盘上的对话 → `--resume`），
侧栏管的是**正在跑的进程**（L2）：它们已经在 tmux 里了，只是不在当前窗口。
所以核心手势不是 send-keys 而是 `swap-pane` —— 进程不断，只是换个位置。

约定：
- 侧栏是当前 window 最左边一个通高的窄 pane，用 pane 选项 `@atm_sidebar` 标记自己。
- 「主格」= 侧栏所在 window 里用户刚才在看的那格（`pane_last`），换位就换它。
- 不在视野里的进程停在一个叫 `bg` 的 window 里（停车场）。
- 这里的规划函数都是纯函数（吃 Pane 元组，吐计划），tmux 只在 execute_* 里碰。
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from enum import StrEnum

from . import tmux
from .i18n import _
from .tmux import Pane, TmuxError

SIDEBAR_WIDTH = 32
PARK_WINDOW = "bg"
SIDEBAR_TITLE = "atm"

# 侧栏把这些命令的格子排在前面 —— 它们才是「后台 AI 进程」，其余 pane 只是顺带列出。
AI_COMMANDS = frozenset({"claude", "codex", "pi", "gemini", "opencode"})


class SidebarError(RuntimeError):
    pass


# ---------------------------------------------------------------- 列表


def is_ai_pane(pane: Pane) -> bool:
    return pane.current_command in AI_COMMANDS


def running_panes(panes: tuple[Pane, ...], *, exclude: str | None = None) -> tuple[Pane, ...]:
    """侧栏列表：去掉侧栏自己，AI 进程排前面，其余按 session/window/pane 顺序。

    `exclude` 是侧栏自己的 pane_id —— 侧栏还没来得及打标记时也能把自己排除掉。
    """
    rows = [p for p in panes if not p.is_sidebar and p.id != exclude]
    rows.sort(key=lambda p: (0 if is_ai_pane(p) else 1, p.session, p.window_index, p.pane_index))
    return tuple(rows)


def find_sidebar(panes: tuple[Pane, ...], window_id: str) -> Pane | None:
    """这个 window 里有没有侧栏。"""
    return next((p for p in panes if p.is_sidebar and p.window_id == window_id), None)


def pane_by_id(panes: tuple[Pane, ...], pane_id: str) -> Pane | None:
    return next((p for p in panes if p.id == pane_id), None)


# ---------------------------------------------------------------- 主格


def main_slot(panes: tuple[Pane, ...], window_id: str) -> Pane | None:
    """window 里要被换出去的那格。

    优先级：`pane_last`（侧栏拿到焦点前用户在看的）→ 活动 pane → 面积最大的。
    面积兜底是为了「侧栏刚创建、还没人点过主区」这种情况。
    """
    candidates = [p for p in panes if p.window_id == window_id and not p.is_sidebar]
    if not candidates:
        return None
    for pick in (lambda p: p.last, lambda p: p.active):
        hit = next((p for p in candidates if pick(p)), None)
        if hit is not None:
            return hit
    return max(candidates, key=lambda p: p.area)


# ---------------------------------------------------------------- 换位


class SwapKind(StrEnum):
    FOCUS = "focus"  # 目标已经在本 window 里，只用切焦点
    SWAP = "swap"  # 目标在别的 window，换进主格


@dataclass(frozen=True, slots=True)
class SwapPlan:
    kind: SwapKind
    target: str  # 要看的那个 pane
    slot: str | None = None  # 被换出去的主格（FOCUS 时为 None）
    # 换完之后把被换出去的那格**关掉**而不是留在后台。
    # 默认只对空闲 shell 这么做：一个刚开的 bash 被换走后留在 bg 里毫无意义，
    # 每换一次后台就多一个空格子（实机踩到）。里面跑着东西的格子仍然收进后台。
    discard: bool = False

    def describe(self) -> str:
        if self.kind is SwapKind.FOCUS:
            return _("切到 {self_target}（已在本窗口）").format(self_target=self.target)
        tail = _("，旧格子是空闲 shell，已关掉") if self.discard else _("，旧格子进后台")
        return _("把 {self_target} 换进 {self_slot} 的位置{tail}").format(
            self_target=self.target, self_slot=self.slot, tail=tail
        )


def plan_swap(
    panes: tuple[Pane, ...],
    *,
    window_id: str,
    target_id: str,
    slot_id: str | None = None,
    discard: bool | None = None,
) -> SwapPlan:
    """纯函数：决定选中 target 之后该做什么。

    `discard`：None = 自动（被换出去的格子是空闲 shell 就关掉，否则收后台）；
    True = 一律关掉（**会杀掉里面跑着的进程**，只给显式要求的命令行用）；False = 一律留着。
    `slot_id` 指定换进哪一格；不给就用 `main_slot`（侧栏场景）。
    命令行里从某个格子跑 `atm swap` 时应把那个格子传进来 —— 「我在哪格跑的就换进哪格」
    比 `pane_last` 直观，后者是给侧栏用的（侧栏拿焦点前用户在看的格子）。
    """
    target = pane_by_id(panes, target_id)
    if target is None:
        raise SidebarError(_("找不到 pane {target_id}（可能刚被关掉）").format(target_id=target_id))
    if target.is_sidebar:
        raise SidebarError(_("那是侧栏自己"))
    if slot_id is not None:
        slot = pane_by_id(panes, slot_id)
        if slot is None:
            raise SidebarError(_("找不到目标格子 {slot_id}").format(slot_id=slot_id))
        if slot.is_sidebar:
            raise SidebarError(_("不能换进侧栏的位置"))
        if slot.id == target.id:
            return SwapPlan(kind=SwapKind.FOCUS, target=target_id)
        return SwapPlan(
            kind=SwapKind.SWAP, target=target_id, slot=slot.id, discard=_discard(slot, discard)
        )
    if target.window_id == window_id:
        return SwapPlan(kind=SwapKind.FOCUS, target=target_id)
    slot = main_slot(panes, window_id)
    if slot is None:
        raise SidebarError(_("本窗口除了侧栏没有别的格子，没法换位 —— 先分一格出来"))
    return SwapPlan(
        kind=SwapKind.SWAP, target=target_id, slot=slot.id, discard=_discard(slot, discard)
    )


def _discard(slot: Pane, wanted: bool | None) -> bool:
    if wanted is not None:
        return wanted
    return slot.is_idle_shell


def execute_swap(plan: SwapPlan) -> None:
    try:
        if plan.kind is SwapKind.SWAP:
            assert plan.slot is not None
            tmux.swap_pane(plan.target, plan.slot)
        tmux.focus_pane(plan.target)
        if plan.kind is SwapKind.SWAP and plan.discard:
            # 先换、先切焦点、最后才关：关掉的是已经被换到别处的那个 pane，
            # 它所在的窗口若因此变空会一起消失 —— 正是要的效果（不留空窗口）。
            assert plan.slot is not None
            tmux.kill_pane(plan.slot)
    except TmuxError as exc:
        raise SidebarError(str(exc)) from exc


# ---------------------------------------------------------------- 停车


@dataclass(frozen=True, slots=True)
class ParkPlan:
    pane: str
    session: str
    existing_window: str | None  # bg 已存在 → join；否则 break 出一个新的

    def describe(self) -> str:
        verb = _("并入") if self.existing_window else _("新建")
        return _("把 {self_pane} 收进后台窗口 {PARK_WINDOW}（{verb}）").format(
            self_pane=self.pane, PARK_WINDOW=PARK_WINDOW, verb=verb
        )


def plan_park(panes: tuple[Pane, ...], *, pane_id: str, bg_window: str | None) -> ParkPlan:
    """纯函数：把 pane 收进 bg 窗口该怎么做。`bg_window` 由调用方查好传进来。"""
    pane = pane_by_id(panes, pane_id)
    if pane is None:
        raise SidebarError(_("找不到 pane {pane_id}").format(pane_id=pane_id))
    if pane.is_sidebar:
        raise SidebarError(_("侧栏自己不能收进后台 —— 要收起侧栏用 `atm sidebar --toggle`"))
    if bg_window == pane.window_id:
        raise SidebarError(
            _("pane {pane_id} 已经在 {PARK_WINDOW} 里了").format(
                pane_id=pane_id, PARK_WINDOW=PARK_WINDOW
            )
        )
    window_panes = [p for p in panes if p.window_id == pane.window_id]
    if len(window_panes) == 1:
        # 实测 break-pane 对单 pane 窗口不报错，只是把那个窗口**改名成 bg** ——
        # 用户的窗口就这么没了。这种情况下它本来就已经在后台，不用动。
        # （旁边还有侧栏的话不算：收走它之后侧栏还在，窗口不会消失。）
        raise SidebarError(
            _("pane {pane_id} 已经独占一个窗口，本来就在后台，不用收").format(pane_id=pane_id)
        )
    return ParkPlan(pane=pane_id, session=pane.session, existing_window=bg_window)


def execute_park(plan: ParkPlan) -> None:
    try:
        if plan.existing_window:
            tmux.join_pane(plan.pane, window=plan.existing_window)
        else:
            tmux.break_pane(plan.pane, window_name=PARK_WINDOW)
    except TmuxError as exc:
        raise SidebarError(str(exc)) from exc


# ---------------------------------------------------------------- 清理后台空 shell


def prunable(panes: tuple[Pane, ...]) -> tuple[Pane, ...]:
    """后台窗口 `bg` 里的空闲 shell —— 换位 / 停车攒下来的空格子。

    只看 bg 窗口、只看空闲 shell（POSIX shell 且不在 copy-mode）：
    别的窗口里的 shell 可能是用户自己开着等用的，里面跑着东西的更不能碰。
    """
    return tuple(
        p for p in panes if p.window_name == PARK_WINDOW and p.is_idle_shell and not p.is_sidebar
    )


def execute_prune(targets: tuple[Pane, ...]) -> tuple[Pane, ...]:
    """逐个关掉，返回真正关掉的。某一个关不掉（刚被用户关了）不影响其余。"""
    killed: list[Pane] = []
    for pane in targets:
        try:
            tmux.kill_pane(pane.id)
        except TmuxError:
            continue
        killed.append(pane)
    return tuple(killed)


# ---------------------------------------------------------------- 开/关侧栏


class ToggleKind(StrEnum):
    OPEN = "open"
    FOCUS = "focus"  # 已开但焦点不在侧栏 → 切过去
    CLOSE = "close"  # 已开且焦点就在侧栏 → 收起


@dataclass(frozen=True, slots=True)
class TogglePlan:
    kind: ToggleKind
    window_id: str
    sidebar_id: str | None = None

    def describe(self) -> str:
        return {
            ToggleKind.OPEN: _("打开侧栏"),
            ToggleKind.FOCUS: _("切到侧栏 {self_sidebar_id}").format(
                self_sidebar_id=self.sidebar_id
            ),
            ToggleKind.CLOSE: _("收起侧栏 {self_sidebar_id}").format(
                self_sidebar_id=self.sidebar_id
            ),
        }[self.kind]


def plan_toggle(panes: tuple[Pane, ...], *, current_pane: str) -> TogglePlan:
    """一个键三种含义：没开 → 开；开了没焦点 → 切过去；焦点在侧栏上 → 收起。

    这样用户不用记两个键，按一下总是「往侧栏走」，在侧栏上再按一下就是「走开」。
    """
    current = pane_by_id(panes, current_pane)
    if current is None:
        raise SidebarError(_("找不到当前 pane {current_pane}").format(current_pane=current_pane))
    sidebar = find_sidebar(panes, current.window_id)
    if sidebar is None:
        return TogglePlan(kind=ToggleKind.OPEN, window_id=current.window_id)
    if current.id == sidebar.id:
        return TogglePlan(kind=ToggleKind.CLOSE, window_id=current.window_id, sidebar_id=sidebar.id)
    return TogglePlan(kind=ToggleKind.FOCUS, window_id=current.window_id, sidebar_id=sidebar.id)


def execute_toggle(plan: TogglePlan, *, command: str, width: int = SIDEBAR_WIDTH) -> str | None:
    """返回侧栏 pane_id（OPEN / FOCUS），CLOSE 返回 None。"""
    try:
        if plan.kind is ToggleKind.OPEN:
            pane_id = tmux.split_sidebar(plan.window_id, width=width, command=command)
            # 标记在这里打而不是等侧栏进程自己打：进程起来前的那一小段时间里
            # 再按一次 toggle 会因为找不到标记而开出第二个侧栏。
            mark_sidebar(pane_id)
            return pane_id
        assert plan.sidebar_id is not None
        if plan.kind is ToggleKind.FOCUS:
            tmux.focus_pane(plan.sidebar_id)
            return plan.sidebar_id
        tmux.kill_pane(plan.sidebar_id)
        return None
    except TmuxError as exc:
        raise SidebarError(str(exc)) from exc


def mark_sidebar(pane_id: str) -> None:
    """给 pane 打上侧栏标记 + 标题。失败不致命（标题纯装饰），但标记必须成功。"""
    tmux.set_pane_option(pane_id, tmux.SIDEBAR_OPTION, "1")
    with contextlib.suppress(TmuxError):
        tmux.set_pane_title(pane_id, SIDEBAR_TITLE)
