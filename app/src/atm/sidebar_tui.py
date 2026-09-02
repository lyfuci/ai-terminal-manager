"""常驻侧栏的 curses 界面：上半段是**正在跑的格子**，下半段是**历史会话**。

跟 tui.py 的选择器不同，这个进程**不退出** —— 它一直坐在最左边那个窄 pane 里，
每秒刷一次 pane 列表。选中一个运行中的格子就把它换进主格；选中一条历史就
在新 window 里恢复它、再把那个新 pane 换进主格。

宽度只有 32 列，所以一行只放得下「标记 + 标题」，其余信息压到底部状态行。
"""

from __future__ import annotations

import contextlib
import curses
import locale
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from . import dispatch as dispatch_mod
from . import index as index_mod
from . import sidebar, tmux
from .dispatch import DispatchError, DispatchTarget, MemoryLimit
from .fuzzy import ScoredEntry, rank, score
from .model import SessionEntry, Source
from .sidebar import SidebarError
from .text import display_width, humanize_age, pad_display, tail_display, truncate_display
from .tmux import Pane
from .tui import (
    _KEY_BACKSPACE,
    _KEY_CTRL_N,
    _KEY_CTRL_P,
    _KEY_CTRL_U,
    _KEY_ENTER,
    _KEY_ESC,
    _KEY_TAB,
    _color,
    _drain_input,
    _has_pending_input,
    _Screen,
)

_KEY_CTRL_R = 18
_KEY_CTRL_X = 24

_PANE_REFRESH_SECONDS = 1.0
_INDEX_REFRESH_SECONDS = 15.0
_STATUS_SECONDS = 6.0
_SOURCE_TAG = {Source.CLAUDE: "CC", Source.CODEX: "CX"}


@dataclass(frozen=True, slots=True)
class SectionHeader:
    label: str


@dataclass(frozen=True, slots=True)
class RunningRow:
    pane: Pane
    is_main: bool
    here: bool  # 在侧栏所在的 window 里


Row = SectionHeader | RunningRow | ScoredEntry


def run_sidebar(*, memory: MemoryLimit | None = None) -> int:
    """入口。必须在 tmux 的某个 pane 里跑（靠 $TMUX_PANE 认自己）。"""
    self_id = tmux.current_pane_id()
    if self_id is None:
        raise SidebarError("atm sidebar 必须在 tmux pane 里跑（用 `atm sidebar --toggle` 打开）")
    sidebar.mark_sidebar(self_id)
    locale.setlocale(locale.LC_ALL, "")
    with contextlib.suppress(KeyboardInterrupt):
        curses.wrapper(_Sidebar(self_id, memory).run)
    return 0


class _Sidebar(_Screen):
    def __init__(self, self_id: str, memory: MemoryLimit | None) -> None:
        self._self_id = self_id
        self._memory = memory
        self._window_id = ""
        self._panes: tuple[Pane, ...] = ()
        self._entries: tuple[SessionEntry, ...] = ()
        self._missing: frozenset[str] = frozenset()
        self._query = ""
        self._source_filter: Source | None = None
        self._rows: tuple[Row, ...] = ()
        self._cursor = 0
        self._offset = 0
        self._status = ""
        self._status_until = 0.0
        self._panes_at = 0.0
        self._index_at = 0.0

    # ------------------------------------------------------------ 主循环

    def run(self, stdscr: curses.window) -> None:
        self._setup(stdscr)
        stdscr.timeout(int(_PANE_REFRESH_SECONDS * 1000))
        self._refresh_panes(force=True)
        self._refresh_index(force=True)
        self._recompute()

        while True:
            self._draw(stdscr)
            try:
                key = stdscr.get_wch()
            except curses.error:
                # 超时：没按键，刷一次数据再重画
                self._tick()
                continue
            self._handle_key(key, stdscr)

    def _tick(self) -> None:
        changed = self._refresh_panes()
        changed |= self._refresh_index()
        if changed:
            self._recompute(keep_cursor=True)

    def _refresh_panes(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self._panes_at < _PANE_REFRESH_SECONDS:
            return False
        self._panes_at = now
        try:
            panes = tmux.list_panes()
        except tmux.TmuxError as exc:
            self._say(f"tmux: {exc}")
            return False
        me = sidebar.pane_by_id(panes, self._self_id)
        if me is not None:
            self._window_id = me.window_id
        if panes == self._panes:
            return False
        self._panes = panes
        return True

    def _refresh_index(self, *, force: bool = False, rebuild: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self._index_at < _INDEX_REFRESH_SECONDS:
            return False
        self._index_at = now
        result = index_mod.build(use_cache=not rebuild)
        entries = index_mod.filter_entries(result.entries)
        if entries == self._entries and not rebuild:
            return False
        self._entries = entries
        self._missing = dispatch_mod.missing_cwds(entries)
        return True

    # ------------------------------------------------------------ 按键

    def _handle_key(self, key: object, stdscr: curses.window) -> None:
        if isinstance(key, str):
            code = ord(key) if len(key) == 1 else -1
            if code == _KEY_ESC:
                if _has_pending_input(stdscr):
                    _drain_input(stdscr)
                    return
                self._query = ""
                self._recompute()
            elif code in _KEY_ENTER:
                self._activate()
            elif code in _KEY_BACKSPACE:
                self._query = self._query[:-1]
                self._recompute()
            elif code == _KEY_CTRL_N:
                self._move(1)
            elif code == _KEY_CTRL_P:
                self._move(-1)
            elif code == _KEY_CTRL_U:
                self._query = ""
                self._recompute()
            elif code == _KEY_TAB:
                self._cycle_source()
            elif code == _KEY_CTRL_R:
                self._refresh_index(force=True, rebuild=True)
                self._refresh_panes(force=True)
                self._recompute(keep_cursor=True)
                self._say("已重建索引")
            elif code == _KEY_CTRL_X:
                self._park()
            elif code >= 32 or code == -1:
                self._query += key
                self._recompute()
            return

        if key == curses.KEY_UP:
            self._move(-1)
        elif key == curses.KEY_DOWN:
            self._move(1)
        elif key == curses.KEY_NPAGE:
            self._move(max(1, stdscr.getmaxyx()[0] - 3))
        elif key == curses.KEY_PPAGE:
            self._move(-max(1, stdscr.getmaxyx()[0] - 3))
        elif key in _KEY_ENTER:
            self._activate()
        elif key in _KEY_BACKSPACE:
            self._query = self._query[:-1]
            self._recompute()
        elif key == curses.KEY_RESIZE:
            pass  # 下一轮 _draw 会按新尺寸画

    def _cycle_source(self) -> None:
        order: list[Source | None] = [None, Source.CLAUDE, Source.CODEX]
        self._source_filter = order[(order.index(self._source_filter) + 1) % len(order)]
        self._recompute()

    # ------------------------------------------------------------ 动作

    def _activate(self) -> None:
        row = self._selected()
        if row is None:
            return
        try:
            if isinstance(row, RunningRow):
                plan = sidebar.plan_swap(
                    self._panes, window_id=self._window_id, target_id=row.pane.id
                )
                sidebar.execute_swap(plan)
                self._say(plan.describe())
            else:
                self._resume(row.entry)
        except (SidebarError, DispatchError) as exc:
            self._say(str(exc).splitlines()[0])
        self._refresh_panes(force=True)
        self._recompute(keep_cursor=True)

    def _resume(self, entry: SessionEntry) -> None:
        """历史会话：先在新 window 里拉起来，再把那个 pane 换进主格。

        走 new-window 而不是直接投主格：主格里跑着的东西不能被打断（它正是
        用户刚才在看的），换出去继续跑，才符合「进程是一等公民」。
        """
        notice = dispatch_mod.size_notice(entry)
        if notice:
            self._say(notice.splitlines()[0])
        result = dispatch_mod.dispatch(
            entry, DispatchTarget.window(), focus=False, memory=self._memory
        )
        assert result.pane_id is not None
        self._refresh_panes(force=True)
        plan = sidebar.plan_swap(self._panes, window_id=self._window_id, target_id=result.pane_id)
        sidebar.execute_swap(plan)
        self._say(f"已恢复 → {result.pane_id}")

    def _park(self) -> None:
        row = self._selected()
        if not isinstance(row, RunningRow):
            self._say("^X 只对运行中的格子有效")
            return
        try:
            bg = tmux.find_window(row.pane.session, sidebar.PARK_WINDOW)
            plan = sidebar.plan_park(self._panes, pane_id=row.pane.id, bg_window=bg)
            sidebar.execute_park(plan)
            self._say(plan.describe())
        except (SidebarError, tmux.TmuxError) as exc:
            self._say(str(exc))
        self._refresh_panes(force=True)
        self._recompute(keep_cursor=True)

    def _say(self, text: str) -> None:
        self._status = text
        self._status_until = time.monotonic() + _STATUS_SECONDS

    # ------------------------------------------------------------ 行

    def _recompute(self, *, keep_cursor: bool = False) -> None:
        previous = self._selected_key() if keep_cursor else None
        rows: list[Row] = []

        slot = sidebar.main_slot(self._panes, self._window_id) if self._window_id else None
        running = [
            RunningRow(
                pane=p,
                is_main=slot is not None and p.id == slot.id,
                here=p.window_id == self._window_id,
            )
            for p in sidebar.running_panes(self._panes, exclude=self._self_id)
        ]
        if self._query:
            running = [r for r in running if score(self._query, _pane_haystack(r.pane)) is not None]
        rows.append(SectionHeader(f"运行中 {len(running)}"))
        rows.extend(running)

        pool = self._entries
        if self._source_filter is not None:
            pool = tuple(e for e in pool if e.source is self._source_filter)
        scored = rank(pool, self._query)
        scope = self._source_filter.value if self._source_filter else "历史"
        rows.append(SectionHeader(f"{scope} {len(scored)}"))
        rows.extend(scored)

        self._rows = tuple(rows)
        self._cursor = 0
        if previous is not None:
            for i, row in enumerate(self._rows):
                if _row_key(row) == previous:
                    self._cursor = i
                    break
        if not self._is_selectable(self._cursor):
            self._move(1)

    def _selected_key(self) -> str | None:
        row = self._selected()
        return _row_key(row) if row is not None else None

    def _is_selectable(self, index: int) -> bool:
        return 0 <= index < len(self._rows) and not isinstance(self._rows[index], SectionHeader)

    def _selected(self) -> RunningRow | ScoredEntry | None:
        if not self._is_selectable(self._cursor):
            return None
        row = self._rows[self._cursor]
        assert not isinstance(row, SectionHeader)
        return row

    def _move(self, delta: int) -> None:
        if not self._rows or delta == 0:
            return
        step = 1 if delta > 0 else -1
        index = self._cursor
        for _ in range(abs(delta)):
            nxt = index + step
            while 0 <= nxt < len(self._rows) and not self._is_selectable(nxt):
                nxt += step
            if not (0 <= nxt < len(self._rows)):
                break
            index = nxt
        self._cursor = index

    # ------------------------------------------------------------ 画

    def _draw(self, stdscr: curses.window) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        list_rows = max(0, height - 3)  # 顶部查询行 + 底部状态行 + 键位行
        self._sync_offset(list_rows)

        self._put(stdscr, 0, 0, f"› {tail_display(self._query, max(4, width - 3))}", curses.A_BOLD)

        now = datetime.now(tz=UTC)
        for i in range(list_rows):
            index = self._offset + i
            if index >= len(self._rows):
                break
            self._draw_row(stdscr, i + 1, width, self._rows[index], index == self._cursor, now)

        self._draw_footer(stdscr, height, width)
        stdscr.refresh()

    def _sync_offset(self, rows: int) -> None:
        if rows <= 0:
            return
        if self._cursor < self._offset:
            self._offset = self._cursor
        elif self._cursor >= self._offset + rows:
            self._offset = self._cursor - rows + 1
        self._offset = max(0, min(self._offset, max(0, len(self._rows) - rows)))

    def _draw_row(
        self, stdscr: curses.window, y: int, width: int, row: Row, selected: bool, now: datetime
    ) -> None:
        base = curses.A_REVERSE if selected else 0
        if isinstance(row, SectionHeader):
            self._put(stdscr, y, 0, pad_display(f"── {row.label} ", width - 1), _color(4))
            return
        if isinstance(row, RunningRow):
            self._draw_running(stdscr, y, width, row, base)
            return
        entry = row.entry
        age = humanize_age((now - entry.updated_at).total_seconds())
        x = self._put(stdscr, y, 0, pad_display(age, 3), base | _color(5))
        x = self._put(stdscr, y, x + 1, _SOURCE_TAG.get(entry.source, "??"), base | _color(3))
        label = f"⟨{entry.name}⟩ {entry.title}" if entry.name else entry.title
        self._put(stdscr, y, x + 1, truncate_display(label, max(0, width - x - 2)), base)

    def _draw_running(
        self, stdscr: curses.window, y: int, width: int, row: RunningRow, base: int
    ) -> None:
        pane = row.pane
        label = pane.title if sidebar.is_ai_pane(pane) and pane.title else pane.current_command
        if not sidebar.is_ai_pane(pane):
            label = f"{pane.current_command}  {_basename(pane.current_path)}"
        # 右侧：主格标 ←；不在本窗口的标它所在 window 的名字（多半是 bg）
        tail = "←" if row.is_main else ("" if row.here else pane.window_name[:6])
        room = max(0, width - 1 - display_width(tail) - (1 if tail else 0))
        attr = base | (curses.A_BOLD if sidebar.is_ai_pane(pane) else 0)
        x = self._put(stdscr, y, 0, pad_display(truncate_display(label, room), room), attr)
        if tail:
            self._put(stdscr, y, x + 1, tail, base | _color(5))

    def _draw_footer(self, stdscr: curses.window, height: int, width: int) -> None:
        if height < 3:
            return
        line = ""
        if time.monotonic() < self._status_until:
            line = self._status
        else:
            row = self._selected()
            if isinstance(row, RunningRow):
                line = f"{row.pane.label}  {row.pane.current_path}"
            elif isinstance(row, ScoredEntry):
                e = row.entry
                gone = "⚠目录已删 " if e.cwd in self._missing else ""
                line = f"{gone}{e.project_name}  {dispatch_mod.size_label(e)}".rstrip()
        self._put(stdscr, height - 2, 0, truncate_display(line, width - 1), _color(5))
        hint = "⏎换入 ^X收后台 Tab来源 ^R重建 ^C退"
        self._put(stdscr, height - 1, 0, truncate_display(hint, width - 1), _color(5))


def _pane_haystack(pane: Pane) -> str:
    return " ".join((pane.title, pane.current_command, pane.current_path, pane.window_name))


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1] or path


def _row_key(row: Row) -> str:
    if isinstance(row, SectionHeader):
        return f"h:{row.label}"
    if isinstance(row, RunningRow):
        return f"p:{row.pane.id}"
    return f"e:{row.entry.source.value}:{row.entry.id}"


__all__: Sequence[str] = ("run_sidebar",)
