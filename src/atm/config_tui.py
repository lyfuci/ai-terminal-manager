"""`atm config` 不带参数、在终端里跑时的交互式编辑器。

为什么不只做 `atm config key value`：内存闸门的几个键要一起看才有意义（high < max、slice 名、
是否启用），一条条敲命令既看不到全貌也不知道当前值是文件给的还是环境变量盖的。
这里一屏列出所有键 + 当前值 + 来源 + 一句说明，↑↓ 选、Enter 改（布尔直接切）、s 保存。

设计上把「状态机」和「画屏」分开：`ConfigEditor.handle_key()` 是纯逻辑，测试直接喂键码，
不需要 curses；`run()` / `_draw()` 才碰终端。校验复用 config._coerce —— 和命令行、
配置文件、环境变量走的是同一套规则，不会出现"界面接受了、启动时报错"。

环境变量覆盖的键会标出来：改文件里的值不会生效，除非先 unset 那个环境变量。
"""

from __future__ import annotations

import curses
from collections.abc import Sequence
from dataclasses import dataclass, fields, replace
from enum import StrEnum

from . import config, i18n
from .i18n import _
from .text import display_width, truncate_display, wrap_display
from .tui import _color, _Screen

# 终端够宽才在右边放说明面板；窄了退回底部一行。列表本身要 ~50 列（键名 + 值 + 来源）。
_PANEL_MIN_WIDTH = 90
_LIST_MIN_WIDTH = 52

_KEY_ESC = 27
_KEY_ENTER = (curses.KEY_ENTER, 10, 13)
_KEY_BACKSPACE = (curses.KEY_BACKSPACE, 127, 8)
_KEY_CTRL_N = 14
_KEY_CTRL_P = 16
_KEY_CTRL_U = 21


class Action(StrEnum):
    NONE = "none"
    QUIT = "quit"  # 无改动或已确认放弃
    SAVED_AND_QUIT = "saved"


@dataclass(frozen=True, slots=True)
class Row:
    key: str
    field: str
    is_bool: bool


class ConfigEditor(_Screen):
    def __init__(self, cfg: config.Config, sources: dict[str, str]) -> None:
        self._original = cfg
        self._cfg = cfg
        self._sources = dict(sources)
        self._rows = tuple(
            Row(key, field, isinstance(getattr(cfg, field), bool))
            for key, field in config.KEYS.items()
        )
        self._cursor = 0
        self._offset = 0  # 列表滚动：第一行显示的是第几个键
        self._editing = False
        self._buffer = ""
        self._error: str | None = None
        self._status: str | None = None
        self._confirm_discard = False

    # ------------------------------------------------------------ 状态查询（测试用）

    @property
    def cfg(self) -> config.Config:
        return self._cfg

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def editing(self) -> bool:
        return self._editing

    @property
    def buffer(self) -> str:
        return self._buffer

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def status(self) -> str | None:
        return self._status

    @property
    def dirty(self) -> bool:
        return self._cfg != self._original

    def value_of(self, row: Row) -> str:
        value = getattr(self._cfg, row.field)
        return str(value).lower() if isinstance(value, bool) else str(value)

    def env_override(self, row: Row) -> str | None:
        """这个键当前被哪个环境变量盖着（None = 没有）。"""
        src = self._sources.get(row.key, "default")
        return src.split(" ", 1)[1] if src.startswith("env ") else None

    # ------------------------------------------------------------ 纯逻辑：按键

    @property
    def help_open(self) -> bool:
        return self._help_open

    def _help_title(self) -> str:
        return _("atm 配置")

    def _help_entries(self) -> Sequence[tuple[str, str]]:
        return (
            ("↑↓ / j k", _("选择")),
            (_("Enter / 空格"), _("修改；布尔值直接切换")),
            ("r", _("恢复这一项的默认值（还没保存）")),
            ("s", _("保存并退出")),
            ("q / Esc", _("退出；有未保存改动时要按两次")),
            ("Q", _("不保存直接退出")),
            (_("编辑中"), _("Enter 确认  Esc 取消  ^U 清空")),
            ("F1 / ?", _("这份帮助")),
            ("", ""),
            (_("← env"), _("这项被环境变量盖着，改文件不生效")),
        )

    def handle_key(self, key: object) -> Action:
        """喂一个 get_wch() 的返回值（str 或 int），返回要不要退出。"""
        self._status = None
        if self._help_key(key, typing=self._editing):
            return Action.NONE
        if self._editing:
            return self._handle_edit_key(key)

        if isinstance(key, str):
            code = ord(key) if len(key) == 1 else -1
            if code == _KEY_ESC or key == "q":
                return self._quit()
            if key == "Q":
                return Action.QUIT
            if key == "s":
                return self._save()
            if key == "r":
                self._reset_selected()
                return Action.NONE
            if code in _KEY_ENTER or key == " ":
                self._activate()
                return Action.NONE
            if code == _KEY_CTRL_N or key == "j":
                self._move(1)
            elif code == _KEY_CTRL_P or key == "k":
                self._move(-1)
            return Action.NONE

        if key == curses.KEY_UP:
            self._move(-1)
        elif key == curses.KEY_DOWN:
            self._move(1)
        elif key in _KEY_ENTER:
            self._activate()
        return Action.NONE

    def _handle_edit_key(self, key: object) -> Action:
        if isinstance(key, str):
            code = ord(key) if len(key) == 1 else -1
            if code == _KEY_ESC:
                self._editing = False
                self._error = None
                return Action.NONE
            if code in _KEY_ENTER:
                self._commit_edit()
                return Action.NONE
            if code in _KEY_BACKSPACE:
                self._buffer = self._buffer[:-1]
                return Action.NONE
            if code == _KEY_CTRL_U:
                self._buffer = ""
                return Action.NONE
            if code >= 32:
                self._buffer += key
            return Action.NONE
        if key in _KEY_ENTER:
            self._commit_edit()
        elif key == curses.KEY_BACKSPACE:
            self._buffer = self._buffer[:-1]
        return Action.NONE

    def _move(self, delta: int) -> None:
        self._cursor = max(0, min(len(self._rows) - 1, self._cursor + delta))
        self._confirm_discard = False

    def _activate(self) -> None:
        row = self._rows[self._cursor]
        self._confirm_discard = False
        if row.is_bool:
            current = getattr(self._cfg, row.field)
            self._cfg = replace(self._cfg, **{row.field: not current})
            self._sources[row.key] = "file"
            return
        self._editing = True
        self._buffer = self.value_of(row)
        self._error = None

    def _commit_edit(self) -> None:
        row = self._rows[self._cursor]
        try:
            self._cfg = config.set_value(self._cfg, row.key, self._buffer)
        except config.ConfigError as exc:
            self._error = str(exc)
            return  # 停在编辑态让人改
        self._sources[row.key] = "file"
        self._editing = False
        self._error = None

    def _reset_selected(self) -> None:
        row = self._rows[self._cursor]
        self._cfg = config.unset_value(self._cfg, row.key)
        self._sources[row.key] = "default"
        self._status = _("{key} 已恢复默认（未保存）").format(key=row.key)

    def _save(self) -> Action:
        try:
            path = config.save(self._cfg)
        except config.ConfigError as exc:
            self._error = str(exc)  # 跨字段校验（如 keys.pick == keys.sidebar）
            return Action.NONE
        except OSError as exc:
            self._error = _("保存失败：{exc}").format(exc=exc)
            return Action.NONE
        from . import sync

        notes = sync.apply_changes(self._original, self._cfg)
        self._original = self._cfg
        self._status = "；".join([_("已保存 → {path}").format(path=path), *notes])
        return Action.SAVED_AND_QUIT

    def _quit(self) -> Action:
        if not self.dirty:
            return Action.QUIT
        if self._confirm_discard:
            return Action.QUIT
        self._confirm_discard = True
        self._status = _("有未保存的改动：s 保存并退出，再按一次 q 放弃")
        return Action.NONE

    # ------------------------------------------------------------ 说明面板（纯逻辑，可测）

    @staticmethod
    def panel_x(width: int) -> int | None:
        """面板从第几列开始；None = 太窄，不画面板。"""
        if width < _PANEL_MIN_WIDTH:
            return None
        return max(_LIST_MIN_WIDTH, width * 11 // 20)

    @staticmethod
    def format_hint(key: str) -> str:
        """这个键接受什么样的值。和 config._coerce 的规则一一对应。"""
        field = config.KEYS[key]
        kind = next(f.type for f in fields(config.Config) if f.name == field)
        if kind == "bool":
            return _("true / false")
        if kind == "int":
            return _("0 或 1") if key == "tmux.base-index" else _("非负整数")
        if key == "memory.slice":
            return _("形如 name.slice")
        if key in ("memory.slice-high", "memory.slice-max"):
            return _("auto，或 24G / 512M 这样的大小")
        if key in ("keys.pick", "keys.sidebar"):
            return _("单个小写字母")
        if key in ("keys.popup-width", "keys.popup-height"):
            return _("80% 或 120")
        return _("4G / 512M / infinity 这样的大小")

    def panel_lines(self, row: Row, width: int) -> list[str]:
        """面板正文，已按 width 折行。第一行是键名（调用方加粗）。"""
        default = getattr(config.Config(), row.field)
        default_shown = str(default).lower() if isinstance(default, bool) else str(default)
        src = self._sources.get(row.key, "default")
        lines: list[str] = [row.key, ""]
        lines += wrap_display(_(config._HELP[row.key]), width)
        lines.append("")
        lines += wrap_display(_("格式：{v}").format(v=self.format_hint(row.key)), width)
        lines.append(_("默认：{v}").format(v=default_shown))
        lines.append(_("环境变量：{v}").format(v=config.env_var_for(row.key)))
        lines.append(_("来源：{v}").format(v=src))
        if self.env_override(row):
            lines.append("")
            lines += wrap_display(_("⚠ 环境变量覆盖中，改文件不生效；先 unset 它"), width)
        lines.append("")
        lines += wrap_display(
            _("界面语言：{lang}（来自 {source}；ATM_LANG=zh|en|ja 可强制）").format(
                lang=i18n.lang(), source=i18n.lang_source()
            ),
            width,
        )
        return lines

    # ------------------------------------------------------------ 画屏

    def run(self, stdscr: curses.window) -> Action:
        self._setup(stdscr)
        while True:
            self._draw(stdscr)
            try:
                key = stdscr.get_wch()
            except curses.error:
                continue
            except KeyboardInterrupt:
                return Action.QUIT
            action = self.handle_key(key)
            if action is not Action.NONE:
                return action

    def _draw(self, stdscr: curses.window) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        panel_x = self.panel_x(width)
        list_limit = (panel_x - 1) if panel_x is not None else width - 1
        path = config.config_path()
        title = _("atm 配置  {path}{note}").format(
            path=path, note="" if path.exists() else _("（尚不存在，保存时创建）")
        )
        self._put(stdscr, 0, 0, title, curses.A_BOLD)
        self._put(
            stdscr,
            1,
            0,
            _("优先级：命令行参数 > 环境变量 > 文件 > 默认。这里改的是文件。"),
            _color(5),
        )

        key_w = max(len(r.key) for r in self._rows) + 2
        val_w = 14
        # 键多了以后小终端 / tmux 浮层放不下：跟着光标滚，永远能看到选中的那行
        visible = max(1, height - 7)  # 顶两行 + 空行 + 底部四行
        if self._cursor < self._offset:
            self._offset = self._cursor
        elif self._cursor >= self._offset + visible:
            self._offset = self._cursor - visible + 1
        self._offset = max(0, min(self._offset, max(0, len(self._rows) - visible)))
        for i in range(self._offset, min(len(self._rows), self._offset + visible)):
            row = self._rows[i]
            y = 3 + i - self._offset
            selected = i == self._cursor
            attr = curses.A_REVERSE if selected else 0
            marker = "▸ " if selected else "  "
            x = self._put(stdscr, y, 0, marker + row.key.ljust(key_w), attr | _color(3))
            if selected and self._editing:
                text = self._buffer + "▏"
                x = self._put(
                    stdscr, y, x, truncate_display(text, list_limit - x), attr | _color(2)
                )
            else:
                text = self.value_of(row).ljust(val_w)
                x = self._put(stdscr, y, x, truncate_display(text, list_limit - x), attr)
            src = self._sources.get(row.key, "default")
            if src != "default":
                tag = truncate_display(f"← {src}", max(0, list_limit - x - 1))
                x = self._put(stdscr, y, x + 1, tag, attr | _color(4))
            if self.env_override(row) and panel_x is None:
                note = _("（环境变量覆盖中，改文件不生效）")
                self._put(
                    stdscr,
                    y,
                    x + 1,
                    truncate_display(note, max(0, list_limit - x - 1)),
                    attr | _color(6),
                )

        row = self._rows[self._cursor]
        if panel_x is not None:
            self._draw_panel(stdscr, row, panel_x, 3, height - 5, width)
        else:
            # 窄终端：说明退回底部一行
            self._put(stdscr, height - 3, 0, _(config._HELP[row.key]), _color(5))
        if self._error:
            self._put(stdscr, height - 2, 0, "❌ " + self._error, _color(6))
        elif self._status:
            self._put(stdscr, height - 2, 0, self._status, _color(2))
        elif self.dirty:
            self._put(stdscr, height - 2, 0, _("● 有未保存的改动"), _color(2))

        if self._editing:
            footer = _("输入新值  Enter 确认  Esc 取消  ^U 清空")
        else:
            footer = _("↑↓ 选择  Enter 修改/切换  r 恢复默认  s 保存退出  q 退出  ? 帮助")
        self._put(stdscr, height - 1, 0, footer, _color(5))
        self._draw_help(stdscr)
        stdscr.refresh()

    def _draw_panel(
        self, stdscr: curses.window, row: Row, x0: int, y0: int, y1: int, width: int
    ) -> None:
        """右侧带边框的说明面板：占 x0 .. width-2，y0 .. y1。放不下的行直接不画。"""
        box_w = width - 1 - x0
        inner = box_w - 2
        if inner < 8 or y1 - y0 < 2:
            return
        lines = self.panel_lines(row, inner - 2)
        self._put(stdscr, y0, x0, "┌" + "─" * inner + "┐", _color(1))
        body = y1 - y0 - 1
        for i in range(body):
            y = y0 + 1 + i
            text = lines[i] if i < len(lines) else ""
            self._put(stdscr, y, x0, "│", _color(1))
            attr = curses.A_BOLD | _color(3) if i == 0 else 0
            if text.startswith("⚠"):
                attr = _color(6)
            pad = " " * max(0, inner - 1 - display_width(text))
            self._put(stdscr, y, x0 + 1, " " + text + pad, attr)
            self._put(stdscr, y, x0 + box_w - 1, "│", _color(1))
        self._put(stdscr, y1, x0, "└" + "─" * inner + "┘", _color(1))


def run_config_editor() -> int:
    """入口：返回 0 表示正常退出（含保存），非 0 表示配置文件读不了。"""
    cfg, sources = config.load_with_sources()
    editor = ConfigEditor(cfg, sources)
    curses.wrapper(editor.run)
    if editor.status:
        print(editor.status)
    return 0
