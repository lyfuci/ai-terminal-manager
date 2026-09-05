"""curses 选择器：两步走 —— 先选会话，再选投到哪个 pane。

为什么自己写而不是调 fzf：本机没装 fzf（实测），而「唤出来就能搜」是这个工具的全部价值，
不能把它压在一个可能不存在的二进制上。curses 在标准库里，零依赖。

这个文件只管画和收键，不碰 tmux、不解析 jsonl —— 那些都在调用方。
"""

from __future__ import annotations

import contextlib
import curses
import locale
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from . import dispatch
from .fuzzy import ScoredEntry, rank
from .model import SOURCE_TAG, SessionEntry, Source
from .text import display_width, humanize_age_ago, pad_display, tail_display, truncate_display

# 各列的固定宽度（显示宽度，不是字符数）
_AGE_WIDTH = 9
_TAG_WIDTH = 2
_PROJECT_WIDTH = 22
_SIZE_WIDTH = 5
_GUTTER = 1

_KEY_ESC = 27
_KEY_ENTER = (curses.KEY_ENTER, 10, 13)
_KEY_BACKSPACE = (curses.KEY_BACKSPACE, 127, 8)
_KEY_CTRL_N = 14
_KEY_CTRL_P = 16
_KEY_CTRL_U = 21
_KEY_CTRL_W = 23
_KEY_TAB = 9
_KEY_CTRL_G = 7


@dataclass(frozen=True, slots=True)
class PickerOption:
    """第二步（选 pane）用的通用选项。key 是回给调用方的标识。"""

    key: str
    label: str
    detail: str = ""
    enabled: bool = True


class GroupMode(StrEnum):
    """聚合维度。

    刻意只做**一维可切换**而不是「目录 → agent」嵌套分组：嵌套会把列表拉得很深，
    而「Tab 过滤来源」+「Ctrl-G 切聚合维度」这两个一维操作组合起来
    已经能表达想要的二维视图（例如「只看 Codex，按项目分组」）。
    """

    CWD = "目录"
    AGENT = "agent"
    NONE = "平铺"

    def next(self) -> GroupMode:
        order = list(GroupMode)
        return order[(order.index(self) + 1) % len(order)]


_AGENT_NAME = {Source.CLAUDE: "Claude Code", Source.CODEX: "Codex", Source.PI: "Pi"}


@dataclass(frozen=True, slots=True)
class GroupHeader:
    """组标题行。**不可选中** —— 光标移动要跳过它。"""

    mode: GroupMode
    key: str
    count: int
    newest: datetime

    def label(self) -> str:
        if self.mode is GroupMode.AGENT:
            shown = _AGENT_NAME.get(Source(self.key), self.key)
        else:
            home = str(Path.home())
            shown = f"~{self.key[len(home) :]}" if self.key.startswith(home) else self.key
        return f"{shown}  ({self.count})"


# 列表里的一行：要么是组标题，要么是一条会话
Row = GroupHeader | ScoredEntry


def pick_session(
    entries: Sequence[SessionEntry],
    *,
    initial_query: str = "",
    mode: GroupMode = GroupMode.CWD,
    missing_cwds: frozenset[str] = frozenset(),
) -> SessionEntry | None:
    """会话选择器。Esc / Ctrl-C 返回 None。

    `mode` 默认按 cwd 聚合 —— 213 条平铺很难扫，按项目分组才看得出「我在哪干过什么」。
    运行时 Ctrl-G 在 目录 → agent → 平铺 之间循环。
    """
    if not entries:
        return None
    locale.setlocale(locale.LC_ALL, "")
    try:
        return curses.wrapper(_SessionPicker(entries, initial_query, mode, missing_cwds).run)
    except KeyboardInterrupt:
        return None


def pick_option(options: Sequence[PickerOption], *, title: str = "") -> PickerOption | None:
    """通用单列选择器（选 pane / 选投递方式）。"""
    selectable = [o for o in options if o.enabled]
    if not selectable:
        return None
    locale.setlocale(locale.LC_ALL, "")
    try:
        return curses.wrapper(_OptionPicker(options, title).run)
    except KeyboardInterrupt:
        return None


class _Screen:
    """curses 的公共部分：初始化配色、安全写字符串。"""

    def _setup(self, stdscr: curses.window) -> None:
        stdscr.keypad(True)
        # curs_set 必须也在 try 里：terminfo 没有 `civis` 的终端（实测 vt100/ansi/dumb
        # 三种 `tput civis` 都失败）会让它抛 _curses.error，整个 picker 起不来。
        with contextlib.suppress(curses.error):
            curses.curs_set(0)
        try:
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)  # 选中行
            curses.init_pair(2, curses.COLOR_YELLOW, -1)  # 匹配高亮
            curses.init_pair(3, curses.COLOR_GREEN, -1)  # 来源标签
            curses.init_pair(4, curses.COLOR_MAGENTA, -1)  # 项目名
            curses.init_pair(5, curses.COLOR_WHITE, -1)  # 次要信息
            curses.init_pair(6, curses.COLOR_RED, -1)  # 危险体积
        except curses.error:
            pass

    @staticmethod
    def _sanitize(text: str) -> str:
        """剥掉控制字符再交给 ncurses。

        display_width 把 C0 控制符算 0 列，而 ncurses 的 addstr 会把它们渲染成
        `^[` / `^G` 这样的**2 列**形式 —— 两边对不上，返回的 x 就偏小，
        后面的列会被冲掉、整行盖出预留边界。会话标题里混进 ANSI 转义并不罕见。
        """
        return "".join(ch for ch in text if ch == "\t" or (ord(ch) >= 32 and ord(ch) != 0x7F))

    @staticmethod
    def _put(win: curses.window, y: int, x: int, text: str, attr: int = 0) -> int:
        """写一段文本，返回写完后的 x。越界不抛 —— curses 在最后一格写字符必然报错。"""
        text = _Screen._sanitize(text)
        if not text:
            return x
        height, width = win.getmaxyx()
        if y >= height or x >= width:
            return x
        clipped = truncate_display(text, max(0, width - x - 1), ellipsis="")
        if not clipped:
            return x
        with contextlib.suppress(curses.error):
            win.addstr(y, x, clipped, attr)
        return x + display_width(clipped)


class _SessionPicker(_Screen):
    def __init__(
        self,
        entries: Sequence[SessionEntry],
        initial_query: str,
        mode: GroupMode,
        missing_cwds: frozenset[str] = frozenset(),
    ) -> None:
        self._all = tuple(entries)
        self._query = initial_query
        self._source_filter: Source | None = None
        self._mode = mode
        self._missing_cwds = missing_cwds
        self._cursor = 0
        self._offset = 0
        self._matches = 0
        self._rows: tuple[Row, ...] = ()

    def run(self, stdscr: curses.window) -> SessionEntry | None:
        self._setup(stdscr)
        self._recompute()

        while True:
            self._draw(stdscr)
            try:
                key = stdscr.get_wch()
            except curses.error:
                continue
            except KeyboardInterrupt:
                return None

            action = self._handle_key(key, stdscr)
            if action is _QUIT:
                return None
            if action is not None:
                return action

    def _handle_key(self, key: object, stdscr: curses.window) -> SessionEntry | object | None:
        height = stdscr.getmaxyx()[0]
        page = max(1, height - 3)

        if isinstance(key, str):
            code = ord(key) if len(key) == 1 else -1
            if code == _KEY_ESC:
                # 裸 ESC 才算取消。终端发来的、terminfo 里没有的转义序列
                # （CSI 形式的 Home/End、Alt-组合键）会让 ncurses 先吐一个 ESC，
                # 后面跟着 `[H` 之类 —— 直接退出的话选择器会莫名消失，
                # 残留字节还会漏进 shell。所以先探一下后面有没有紧跟的字节。
                if _has_pending_input(stdscr):
                    _drain_input(stdscr)
                    return None
                return _QUIT
            if code in _KEY_ENTER:
                return self._selected()
            if code in _KEY_BACKSPACE:
                self._query = self._query[:-1]
                self._recompute()
                return None
            if code == _KEY_CTRL_N:
                self._move(1)
                return None
            if code == _KEY_CTRL_P:
                self._move(-1)
                return None
            if code == _KEY_CTRL_U:
                self._query = ""
                self._recompute()
                return None
            if code == _KEY_CTRL_W:
                self._query = self._query.rstrip().rpartition(" ")[0]
                self._recompute()
                return None
            if code == _KEY_TAB:
                self._cycle_source()
                return None
            if code == _KEY_CTRL_G:
                self._cycle_grouping()
                return None
            if code >= 32 or code == -1:
                self._query += key
                self._recompute()
            return None

        if key in (curses.KEY_UP,):
            self._move(-1)
        elif key in (curses.KEY_DOWN,):
            self._move(1)
        elif key == curses.KEY_NPAGE:
            self._page(page)
        elif key == curses.KEY_PPAGE:
            self._page(-page)
        elif key == curses.KEY_HOME:
            self._cursor = 0
            if not self._is_selectable(0):
                self._move(1)
        elif key == curses.KEY_END:
            self._move(len(self._rows))
        elif key in _KEY_ENTER:
            return self._selected()
        elif key in _KEY_BACKSPACE:
            self._query = self._query[:-1]
            self._recompute()
        return None

    def _cycle_source(self) -> None:
        order: list[Source | None] = [None, *Source]
        self._source_filter = order[(order.index(self._source_filter) + 1) % len(order)]
        self._recompute()

    def _cycle_grouping(self) -> None:
        self._mode = self._mode.next()
        self._recompute()

    def _recompute(self) -> None:
        pool = self._all
        if self._source_filter is not None:
            pool = tuple(e for e in pool if e.source is self._source_filter)

        scored = rank(pool, self._query)
        self._matches = len(scored)
        self._rows = (
            tuple(scored) if self._mode is GroupMode.NONE else self._group(scored, self._mode)
        )
        self._offset = 0
        # 光标必须落在可选行上 —— 分组模式下第 0 行是组标题
        self._cursor = 0
        if self._rows and isinstance(self._rows[0], GroupHeader):
            self._cursor = 1 if len(self._rows) > 1 else 0

    @staticmethod
    def _group(scored: Sequence[ScoredEntry], mode: GroupMode) -> tuple[Row, ...]:
        """按 cwd 聚合。

        组内保持 `rank` 给出的顺序（空查询=时间倒序，有查询=分数序）；
        组间按「组内最好的那条」排 —— 这样最近用过 / 最匹配的项目永远在最上面，
        不会因为某个目录会话多就把它顶上去。
        """

        def key_of(item: ScoredEntry) -> str:
            return item.entry.source.value if mode is GroupMode.AGENT else item.entry.cwd

        buckets: dict[str, list[ScoredEntry]] = {}
        best_rank: dict[str, int] = {}
        for position, item in enumerate(scored):
            key = key_of(item)
            buckets.setdefault(key, []).append(item)
            # rank 已排好序，所以每组第一次出现的位置就是它最好的那条
            best_rank.setdefault(key, position)

        order = sorted(buckets.items(), key=lambda kv: best_rank[kv[0]])

        rows: list[Row] = []
        for key, items in order:
            rows.append(
                GroupHeader(
                    mode=mode,
                    key=key,
                    count=len(items),
                    newest=max(i.entry.updated_at for i in items),
                )
            )
            rows.extend(items)
        return tuple(rows)

    def _is_selectable(self, index: int) -> bool:
        return 0 <= index < len(self._rows) and not isinstance(self._rows[index], GroupHeader)

    def _move(self, delta: int) -> None:
        """移动光标，跳过组标题行。"""
        if not self._rows or delta == 0:
            return
        step = 1 if delta > 0 else -1
        remaining = abs(delta)
        index = self._cursor

        while remaining:
            nxt = index + step
            while 0 <= nxt < len(self._rows) and not self._is_selectable(nxt):
                nxt += step
            if not (0 <= nxt < len(self._rows)):
                break  # 到头了，停在最后一个可选行
            index = nxt
            remaining -= 1

        self._cursor = index

    def _page(self, rows: int) -> None:
        """翻页：按**显示行数**移动，再吸附到最近的可选行。

        不能直接 `_move(page)` —— `_move` 走的是**可选行**，中间跳过的组标题不计数。
        分组模式下每组一条时，一次 PgDn 会跨过 2 倍屏高，**整屏内容被跳过没看到**。
        （实测：20 个单会话目录组，PgDn 移动了 14 个显示行而屏幕只有 7 行。）
        """
        if not self._rows:
            return
        target = max(0, min(len(self._rows) - 1, self._cursor + rows))
        if self._is_selectable(target):
            self._cursor = target
            return
        # 落在组标题上就往同方向找最近的可选行，找不到再往回找
        step = 1 if rows > 0 else -1
        for direction in (step, -step):
            probe = target
            while 0 <= probe < len(self._rows):
                if self._is_selectable(probe):
                    self._cursor = probe
                    return
                probe += direction

    def _selected(self) -> SessionEntry | None:
        if not self._is_selectable(self._cursor):
            return None
        row = self._rows[self._cursor]
        assert isinstance(row, ScoredEntry)
        return row.entry

    def _draw(self, stdscr: curses.window) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        rows = max(0, height - 3)  # 顶部查询行 + 底部详情行 + 键位提示行
        self._sync_offset(rows)

        scope = self._source_filter.value if self._source_filter else "all"
        counter = f"[{scope}·{self._mode.value}] {self._matches}/{len(self._all)}"
        # 查询行要给右边的计数器让位，并且**跟着光标横向滚** ——
        # 之前是整串原样画出去，字数一过窗口宽度就被右边截掉，
        # 用户继续打字看不到自己在打什么（列表还在实时过滤，所以他甚至不知道打错了）。
        budget = max(8, width - display_width(counter) - 4)
        self._put(stdscr, 0, 0, f"› {tail_display(self._query, budget)}", curses.A_BOLD)
        self._put(stdscr, 0, max(0, width - display_width(counter) - 1), counter, _color(5))

        now = datetime.now(tz=UTC)
        for row in range(rows):
            index = self._offset + row
            if index >= len(self._rows):
                break
            item = self._rows[index]
            if isinstance(item, GroupHeader):
                self._draw_group(stdscr, row + 1, width, item, now)
            else:
                self._draw_row(stdscr, row + 1, width, item, index == self._cursor, now)

        self._draw_footer(stdscr, height, width)
        stdscr.refresh()

    def _draw_footer(self, stdscr: curses.window, height: int, width: int) -> None:
        """底部两行：选中项的绝对时间/体积/cwd + 键位提示。

        2026-08-12 的事故里，用户完全看不出自己选的是一条 649MB 的会话 ——
        列表一行放不下这些，所以给选中项单独一行。
        """
        selected = self._selected()
        if selected is not None and height >= 3:
            stamp = selected.updated_at.astimezone().strftime("%Y-%m-%d %H:%M")
            size_mb = selected.size_bytes / 1048576
            risk = dispatch.size_risk(selected)
            mark = "  ⚠ resume 会让 CLI 读完整个文件" if risk is not dispatch.SizeRisk.OK else ""
            gone = "⚠ 工作目录已不存在  " if selected.cwd in self._missing_cwds else ""
            named = f"{gone}⟨{selected.name}⟩  " if selected.name else gone
            detail = f"{named}{stamp}  {size_mb:.1f}MB{mark}  {selected.cwd}"
            attr = _color(6) if risk is dispatch.SizeRisk.BLOCK else _color(5)
            self._put(stdscr, height - 2, 0, truncate_display(detail, width - 1), attr)

        hint = "↑↓/^N^P 移动  Tab 切来源  ^G 切聚合(目录/agent/平铺)  Enter 选中  Esc 取消"
        self._put(stdscr, height - 1, 0, truncate_display(hint, width - 1), _color(5))

    def _sync_offset(self, rows: int) -> None:
        if rows <= 0:
            return
        if self._cursor < self._offset:
            self._offset = self._cursor
        elif self._cursor >= self._offset + rows:
            self._offset = self._cursor - rows + 1
        # 上界必须夹回来：终端/浮层变大后 rows 变大，旧的 offset 会让列表卡在底部 ——
        # 上半屏内容看不到，下半屏全是空白。实测 60 条、rows 7→50 时 offset 停在 53。
        self._offset = max(0, min(self._offset, len(self._rows) - rows))

    def _draw_group(
        self, stdscr: curses.window, y: int, width: int, group: GroupHeader, now: datetime
    ) -> None:
        """组标题行：目录 + 条数 + 该组最近活动时间。整行不可选中。"""
        age = humanize_age_ago((now - group.newest).total_seconds())
        x = self._put(stdscr, y, 0, pad_display(age, _AGE_WIDTH), _color(5))
        x += _GUTTER
        self._put(
            stdscr, y, x, truncate_display(group.label(), width - x - 1), curses.A_BOLD | _color(4)
        )

    def _draw_row(
        self,
        stdscr: curses.window,
        y: int,
        width: int,
        scored: ScoredEntry,
        selected: bool,
        now: datetime,
    ) -> None:
        entry = scored.entry
        base = curses.A_REVERSE if selected else 0

        age = _age_label(entry, now)
        x = self._put(stdscr, y, 0, pad_display(age, _AGE_WIDTH), base | _color(5))
        x += _GUTTER
        tag = SOURCE_TAG.get(entry.source, "??")
        x = self._put(stdscr, y, x, pad_display(tag, _TAG_WIDTH), base | _color(3))
        x += _GUTTER
        project = pad_display(entry.project_name, _PROJECT_WIDTH)
        x = self._put(stdscr, y, x, project, base | _color(4))
        x += _GUTTER

        # 体积列：只有「大到会拖垮机器」的会话才占位置，其余留空不加噪音。
        # 2026-08-12 的事故就是因为用户完全看不出自己选的是 649MB 的转录。
        risk = dispatch.size_risk(entry)
        size_attr = _color(6) if risk is dispatch.SizeRisk.BLOCK else _color(2)
        x = self._put(
            stdscr, y, x, pad_display(dispatch.size_label(entry), _SIZE_WIDTH), base | size_attr
        )
        x += _GUTTER

        # 用户手动命名过的会话，名字用 ⟨⟩ 包起来放在标题前面并高亮 ——
        # 「人取的名字」和「推断出来的标题」是两种东西，不该长得一样。
        # 工作目录已删除的会话直接标出来 —— 投递它只会得到一句 cd 失败。
        # 实测本机 210 条里有 33 条（16%）是这种，选之前就该看见。
        if entry.cwd in self._missing_cwds:
            x = self._put(stdscr, y, x, "⚠目录已删 ", base | _color(6))

        if entry.name:
            tag_text = f"⟨{entry.name}⟩ "
            x = self._put(stdscr, y, x, tag_text, base | curses.A_BOLD | _color(1))

        remaining = max(0, width - x - 1)
        self._put(stdscr, y, x, truncate_display(entry.title, remaining), base)


class _OptionPicker(_Screen):
    def __init__(self, options: Sequence[PickerOption], title: str) -> None:
        self._options = tuple(options)
        self._title = title
        self._cursor = next((i for i, o in enumerate(self._options) if o.enabled), 0)
        self._offset = 0

    def run(self, stdscr: curses.window) -> PickerOption | None:
        self._setup(stdscr)
        while True:
            self._draw(stdscr)
            try:
                key = stdscr.get_wch()
            except curses.error:
                continue
            except KeyboardInterrupt:
                return None

            if isinstance(key, str):
                code = ord(key) if len(key) == 1 else -1
                if code == _KEY_ESC:
                    return None
                if code in _KEY_ENTER:
                    return self._selected()
                if code == _KEY_CTRL_N:
                    self._move(1)
                elif code == _KEY_CTRL_P:
                    self._move(-1)
                else:
                    hit = self._by_shortcut(key)
                    if hit is not None:
                        return hit
            elif key == curses.KEY_UP:
                self._move(-1)
            elif key == curses.KEY_DOWN:
                self._move(1)
            elif key in _KEY_ENTER:
                return self._selected()

    def _by_shortcut(self, char: str) -> PickerOption | None:
        """数字键直选前 9 项 —— 「投到 2 号格」这种手势不该还要按方向键。"""
        if not char.isdigit() or char == "0":
            return None
        index = self._offset + int(char) - 1  # 相对**当前可见窗口**，与画出来的编号一致
        if index < len(self._options) and self._options[index].enabled:
            return self._options[index]
        return None

    def _selected(self) -> PickerOption | None:
        if not self._options:
            return None
        option = self._options[self._cursor]
        return option if option.enabled else None

    def _move(self, delta: int) -> None:
        count = len(self._options)
        if count == 0:
            return
        index = self._cursor
        for _ in range(count):
            index = max(0, min(count - 1, index + delta))
            if self._options[index].enabled:
                self._cursor = index
                return
            if index in (0, count - 1):
                break

    def _draw(self, stdscr: curses.window) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        self._put(stdscr, 0, 0, truncate_display(self._title, width - 1), curses.A_BOLD)

        # ⚠️ 必须跟着光标滚动。之前从 index 0 直画到画不下为止，而光标能移到
        # len(options)-1 —— pane 多于 height-3 时，光标停在**画不出来的选项**上，
        # Enter 就把会话投到了一个用户根本看不见的 pane。
        rows = max(1, height - 3)
        if self._cursor < self._offset:
            self._offset = self._cursor
        elif self._cursor >= self._offset + rows:
            self._offset = self._cursor - rows + 1
        self._offset = max(0, min(self._offset, len(self._options) - rows))

        for row in range(rows):
            index = self._offset + row
            if index >= len(self._options):
                break
            option = self._options[index]
            y = row + 2
            if y >= height - 1:
                break
            selected = index == self._cursor
            attr = curses.A_REVERSE if selected else 0
            if not option.enabled:
                attr |= curses.A_DIM
            # 数字键直选的是**当前可见的**前 9 项，所以编号跟着 offset 走
            prefix = f"{row + 1}. " if row < 9 else "   "
            x = self._put(stdscr, y, 0, prefix, attr | _color(5))
            x = self._put(stdscr, y, x, option.label, attr)
            if option.detail:
                self._put(stdscr, y, x + 1, option.detail, attr | _color(5))

        more = len(self._options) - (self._offset + rows)
        hint = "↑↓ 移动  数字键直选  Enter 确认  Esc 取消"
        if more > 0 or self._offset > 0:
            hint = f"↑↓ 移动({self._offset + 1}-{min(self._offset + rows, len(self._options))}"
            hint += f"/{len(self._options)})  数字键直选  Enter 确认  Esc 取消"
        self._put(stdscr, height - 1, 0, truncate_display(hint, width - 1), _color(5))
        stdscr.refresh()


def _age_label(entry: SessionEntry, now: datetime) -> str:
    return humanize_age_ago((now - entry.updated_at).total_seconds())


def _color(pair: int) -> int:
    try:
        return curses.color_pair(pair)
    except curses.error:
        return 0


def _has_pending_input(stdscr: curses.window) -> bool:
    """ESC 之后是不是紧跟着还有字节（说明这是一段转义序列而不是用户按了 Esc）。"""
    stdscr.nodelay(True)
    try:
        ch = stdscr.getch()
    except curses.error:
        ch = -1
    finally:
        stdscr.nodelay(False)
    if ch == -1:
        return False
    curses.ungetch(ch)
    return True


def _drain_input(stdscr: curses.window) -> None:
    """把一段未识别的转义序列剩余字节吃掉，避免它们漏进后续按键处理。"""
    stdscr.nodelay(True)
    try:
        for _ in range(16):
            if stdscr.getch() == -1:
                break
    except curses.error:
        pass
    finally:
        stdscr.nodelay(False)


_QUIT = object()
