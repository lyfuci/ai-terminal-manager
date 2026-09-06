"""F1 / `?` 帮助浮层：四个 TUI 共用 _Screen 里的开关逻辑，这里只测状态机和画图不炸。"""

from __future__ import annotations

import curses

import pytest

from atm import config, sidebar_tui, tmux, tui
from atm import index as index_mod
from atm.config_tui import ConfigEditor
from atm.sidebar_tui import _Sidebar
from atm.tui import PickerOption, _OptionPicker, _Screen, _SessionPicker


class FakeWin:
    """够 _put / getmaxyx 用的假窗口，把写过的字符串记下来。"""

    def __init__(self, height: int = 24, width: int = 80) -> None:
        self._size = (height, width)
        self.written: list[tuple[int, int, str]] = []

    def getmaxyx(self) -> tuple[int, int]:
        return self._size

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        self.written.append((y, x, text))

    def nodelay(self, flag: bool) -> None:
        pass

    def getch(self) -> int:
        return -1


def _text(win: FakeWin) -> str:
    return "\n".join(t for _y, _x, t in win.written)


# ------------------------------------------------------------------ 公共开关


class Demo(_Screen):
    def _help_entries(self):
        return (("a", "甲"), ("bb", "乙"))


def test_question_mark_opens_and_any_key_closes() -> None:
    s = Demo()
    assert s._help_key("?") is True and s._help_open
    assert s._help_key("j") is True  # 关浮层的那个键被吃掉
    assert not s._help_open
    assert s._help_key("j") is False  # 之后正常透传


def test_f1_opens_even_while_typing_but_question_mark_does_not() -> None:
    s = Demo()
    assert s._help_key("?", typing=True) is False
    assert s._help_key(curses.KEY_F1, typing=True) is True and s._help_open


def test_resize_is_not_a_key() -> None:
    s = Demo()
    s._help_key("?")
    assert s._help_key(curses.KEY_RESIZE) is False and s._help_open


def test_draw_help_centers_a_box_with_entries() -> None:
    s = Demo()
    s._help_key("?")
    win = FakeWin(24, 80)
    s._draw_help(win)
    out = _text(win)
    assert "┌" in out and "└" in out
    assert "a   甲" in out and "bb  乙" in out
    assert "任意键关闭" in out


def test_draw_help_survives_tiny_windows() -> None:
    s = Demo()
    s._help_key("?")
    for size in ((1, 1), (2, 10), (3, 6), (5, 12)):
        s._draw_help(FakeWin(*size))  # 不抛就行


def test_draw_help_is_noop_when_closed() -> None:
    win = FakeWin()
    Demo()._draw_help(win)
    assert win.written == []


# ------------------------------------------------------------------ 各 TUI 接线


def test_option_picker_help_swallows_shortcut() -> None:
    p = _OptionPicker([PickerOption("a", "A"), PickerOption("b", "B")], "t")
    assert p._handle_key("?") is None and p._help_open
    assert p._handle_key("1") is None  # 本来会直选第 1 项
    assert not p._help_open
    assert p._handle_key("1") == PickerOption("a", "A")


def test_session_picker_question_mark_is_help_only_when_query_empty() -> None:
    p = _SessionPicker((), "", tui.GroupMode.NONE)
    win = FakeWin()
    assert p._handle_key("?", win) is None and p._help_open
    p._handle_key("x", win)  # 关掉
    p._handle_key("a", win)  # 开始打字
    assert p._query == "a"
    p._handle_key("?", win)
    assert p._query == "a?" and not p._help_open
    p._handle_key(curses.KEY_F1, win)
    assert p._help_open


def test_config_editor_help_does_not_leak_into_edit_or_quit() -> None:
    from atm.config_tui import Action

    ed = ConfigEditor(config.Config(), dict.fromkeys(config.KEYS, "default"))
    ed.handle_key("?")
    assert ed.help_open
    assert ed.handle_key("q") is Action.NONE and not ed.help_open  # 关浮层，不退出
    # 编辑值的时候 ? 是字符
    ed._cursor = list(config.KEYS).index("memory.high")
    ed.handle_key("\n")
    ed.handle_key("?")
    assert ed.buffer.endswith("?") and not ed.help_open
    assert ed.handle_key(curses.KEY_F1) is Action.NONE and ed.help_open


@pytest.fixture
def sidebar(monkeypatch: pytest.MonkeyPatch) -> _Sidebar:
    monkeypatch.setattr(tmux, "run", lambda args, **kw: "")
    monkeypatch.setattr(tmux, "is_installed", lambda: True)
    monkeypatch.setattr(sidebar_tui, "_INDEX_REFRESH_SECONDS", 10**9)
    monkeypatch.setattr(
        index_mod, "build", lambda **kw: index_mod.SessionIndex(entries=(), stats=None)
    )
    s = _Sidebar("%sb", None)
    s._recompute()
    return s


def test_sidebar_help_and_hint(sidebar: _Sidebar) -> None:
    sidebar._handle_key("?", None)
    assert sidebar._help_open
    sidebar._handle_key("x", None)
    assert not sidebar._help_open and sidebar._query == ""
    sidebar._handle_key("c", None)
    sidebar._handle_key("?", None)
    assert sidebar._query == "c?"
    win = FakeWin(20, 32)
    sidebar._draw_footer(win, 20, 32)
    assert "?帮助" in _text(win)


# ------------------------------------------------------------------ 窄屏与滚动


class Long(_Screen):
    def _help_entries(self):
        return tuple((f"k{i}", "说明" * 12) for i in range(12))


def test_narrow_pane_stacks_key_and_wrapped_description() -> None:
    s = Long()
    s._help_key("?")
    win = FakeWin(40, 32)  # 侧栏尺寸：高够、宽不够
    s._draw_help(win)
    out = _text(win)
    assert any(t.startswith(" k0 ") for _y, _x, t in win.written)  # 键单独一行
    assert "…" not in out  # 说明不再被截成省略号，而是换行
    assert s._help_max_scroll == 0


def test_short_pane_scrolls_with_arrows_and_closes_on_other_keys() -> None:
    s = Long()
    s._help_key("?")
    win = FakeWin(10, 120)
    s._draw_help(win)
    assert s._help_max_scroll == 12 - 7  # 10 行高：边框 2 + 提示 1 → 7 行正文
    assert "还有 5 行" in _text(win)
    assert s._help_key(curses.KEY_DOWN) is True and s._help_scroll == 1 and s._help_open
    assert s._help_key(" ") is True and s._help_scroll == 5  # 到底夹住
    win2 = FakeWin(10, 120)
    s._draw_help(win2)
    assert "k11" in _text(win2) and "还有" not in _text(win2)
    assert s._help_key("k") is True and s._help_scroll == 4
    assert s._help_key("x") is True and not s._help_open  # 其它键关


def test_scroll_keys_close_when_everything_fits() -> None:
    s = Demo()
    s._help_key("?")
    s._draw_help(FakeWin(24, 80))
    assert s._help_key(curses.KEY_DOWN) is True and not s._help_open


def test_wrap_display_is_width_aware() -> None:
    from atm.text import wrap_display

    assert wrap_display("abc", 10) == ["abc"]
    assert wrap_display("中文字符", 4) == ["中文", "字符"]
    assert wrap_display("ab中", 3) == ["ab", "中"]
    assert wrap_display("x", 0) == ["x"]
    assert wrap_display("swap into the main pane", 12) == ["swap into", "the main", "pane"]
    assert wrap_display("a verylongword b", 6) == ["a", "verylo", "ngword", "b"]
