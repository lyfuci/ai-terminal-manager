"""交互式 atm config：只测状态机（handle_key），不碰 curses。"""

from __future__ import annotations

import curses
from pathlib import Path

import pytest

from atm import config
from atm.config_tui import Action, ConfigEditor


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "atm" / "config.toml"
    monkeypatch.setenv("ATM_CONFIG", str(p))
    for key in config.KEYS:
        monkeypatch.delenv(config.env_var_for(key), raising=False)
    return p


def editor(cfg: config.Config | None = None, sources: dict | None = None) -> ConfigEditor:
    return ConfigEditor(cfg or config.Config(), sources or dict.fromkeys(config.KEYS, "default"))


def type_text(ed: ConfigEditor, text: str) -> None:
    for ch in text:
        ed.handle_key(ch)


def row_index(key: str) -> int:
    return list(config.KEYS).index(key)


def test_move_and_bounds() -> None:
    ed = editor()
    assert ed.cursor == 0
    ed.handle_key(curses.KEY_UP)
    assert ed.cursor == 0
    for _i in range(50):
        ed.handle_key(curses.KEY_DOWN)
    assert ed.cursor == len(config.KEYS) - 1
    ed.handle_key("k")
    assert ed.cursor == len(config.KEYS) - 2


def test_enter_on_bool_toggles_without_edit_mode() -> None:
    ed = editor()
    ed._cursor = row_index("memory.enabled")
    ed.handle_key("\n")
    assert ed.cfg.memory_enabled is False
    assert not ed.editing
    assert ed.dirty


def test_edit_validates_with_same_rules_as_cli() -> None:
    ed = editor()
    ed._cursor = row_index("memory.high")
    ed.handle_key("\n")
    assert ed.editing and ed.buffer == config.Config().memory_high
    ed.handle_key("\x15")  # ^U 清空
    type_text(ed, "lots")
    ed.handle_key("\n")
    assert ed.editing  # 停在编辑态
    assert ed.error and "memory.high" in ed.error
    ed.handle_key("\x15")
    type_text(ed, "6g")
    ed.handle_key("\n")
    assert not ed.editing and ed.error is None
    assert ed.cfg.memory_high == "6G"  # 大小写归一，和 atm config 一样


def test_esc_cancels_edit_keeps_old_value() -> None:
    ed = editor()
    ed._cursor = row_index("memory.max")
    ed.handle_key("\n")
    type_text(ed, "999G")
    ed.handle_key("\x1b")
    assert not ed.editing
    assert ed.cfg.memory_max == config.Config().memory_max


def test_backspace_in_edit() -> None:
    ed = editor()
    ed._cursor = row_index("memory.high")
    ed.handle_key("\n")
    ed.handle_key("\x15")
    type_text(ed, "12G")
    ed.handle_key("\x7f")
    assert ed.buffer == "12"


def test_reset_key_restores_default() -> None:
    ed = editor(
        config.Config(memory_high="9G"),
        {**dict.fromkeys(config.KEYS, "default"), "memory.high": "file"},
    )
    ed._cursor = row_index("memory.high")
    ed.handle_key("r")
    assert ed.cfg.memory_high == config.Config().memory_high
    assert ed.status and "memory.high" in ed.status


def test_quit_asks_twice_when_dirty() -> None:
    ed = editor()
    ed._cursor = row_index("memory.enabled")
    ed.handle_key("\n")
    assert ed.handle_key("q") is Action.NONE
    assert ed.status and "未保存" in ed.status
    assert ed.handle_key("q") is Action.QUIT


def test_quit_immediately_when_clean() -> None:
    ed = editor()
    assert ed.handle_key("q") is Action.QUIT


def test_capital_q_discards_immediately() -> None:
    ed = editor()
    ed._cursor = row_index("memory.enabled")
    ed.handle_key("\n")
    assert ed.handle_key("Q") is Action.QUIT


def test_save_writes_file_and_quits(cfg_path: Path) -> None:
    ed = editor()
    ed._cursor = row_index("memory.high")
    ed.handle_key("\n")
    ed.handle_key("\x15")
    type_text(ed, "5G")
    ed.handle_key("\n")
    assert ed.handle_key("s") is Action.SAVED_AND_QUIT
    assert config.load().memory_high == "5G"
    assert not ed.dirty
    assert ed.status and str(cfg_path) in ed.status


def test_save_runs_sync_and_shows_its_notes(cfg_path: Path, monkeypatch) -> None:
    from atm import sync

    seen: list[tuple[config.Config, config.Config]] = []
    monkeypatch.setattr(
        sync, "apply_changes", lambda old, new, **kw: seen.append((old, new)) or ["tmux 已生效"]
    )
    ed = editor()
    ed._cursor = row_index("tmux.mouse")
    ed.handle_key("\n")  # 布尔切换
    assert ed.handle_key("s") is Action.SAVED_AND_QUIT
    assert len(seen) == 1
    assert seen[0][0].tmux_mouse is False and seen[0][1].tmux_mouse is True
    assert ed.status and "已保存" in ed.status and "tmux 已生效" in ed.status


def test_save_refuses_same_pick_and_sidebar_key(cfg_path: Path) -> None:
    ed = editor()
    ed._cursor = row_index("keys.sidebar")
    ed.handle_key("\n")
    ed.handle_key("\x15")
    type_text(ed, "a")  # 和 keys.pick 默认的 a 撞了
    ed.handle_key("\n")
    assert ed.handle_key("s") is Action.NONE
    assert ed.error and "keys.pick" in ed.error
    assert not cfg_path.exists()


def test_int_key_edits_as_text(cfg_path: Path) -> None:
    ed = editor()
    ed._cursor = row_index("tmux.history-limit")
    ed.handle_key("\n")
    assert ed.editing and ed.buffer == "0"
    ed.handle_key("\x15")
    type_text(ed, "5万")
    ed.handle_key("\n")
    assert ed.editing and ed.error and "整数" in ed.error
    ed.handle_key("\x15")
    type_text(ed, "50000")
    ed.handle_key("\n")
    assert not ed.editing and ed.cfg.tmux_history_limit == 50000


def test_env_override_is_visible() -> None:
    sources = {**dict.fromkeys(config.KEYS, "default"), "memory.high": "env ATM_MEMORY_HIGH"}
    ed = editor(config.Config(memory_high="7G"), sources)
    row = ed._rows[row_index("memory.high")]
    assert ed.env_override(row) == "ATM_MEMORY_HIGH"
    assert ed.env_override(ed._rows[row_index("memory.max")]) is None


def test_editing_state_ignores_navigation_keys() -> None:
    ed = editor()
    ed._cursor = row_index("memory.high")
    ed.handle_key("\n")
    before = ed.cursor
    ed.handle_key(curses.KEY_DOWN)  # 编辑态里方向键不该换行
    assert ed.cursor == before and ed.editing


def test_config_command_routes_to_editor_on_tty(cfg_path: Path, monkeypatch, capsys) -> None:
    from atm import cli, config_tui

    called = []
    monkeypatch.setattr(config_tui, "run_config_editor", lambda: called.append(True) or 0)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    assert cli.main(["config"]) == cli.EXIT_OK
    assert called == [True]


def test_config_show_forces_text(cfg_path: Path, monkeypatch, capsys) -> None:
    from atm import cli, config_tui

    monkeypatch.setattr(config_tui, "run_config_editor", lambda: pytest.fail("不该进 TUI"))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    assert cli.main(["config", "--show"]) == cli.EXIT_OK
    assert "memory.high" in capsys.readouterr().out


def test_config_non_tty_prints_text(cfg_path: Path, monkeypatch, capsys) -> None:
    from atm import cli

    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False)
    assert cli.main(["config"]) == cli.EXIT_OK
    assert "memory.high" in capsys.readouterr().out


def test_draw_scrolls_to_keep_cursor_visible() -> None:
    """17 个键在 14 行高的窗口里画不完：光标在最后一行时要能看见它。"""

    class Win:
        def __init__(self) -> None:
            self.lines: dict[int, str] = {}

        def getmaxyx(self) -> tuple[int, int]:
            return (14, 100)

        def erase(self) -> None:
            self.lines.clear()

        def refresh(self) -> None:
            pass

        def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
            self.lines[y] = self.lines.get(y, "") + text

    ed = editor()
    win = Win()
    ed._draw(win)
    assert any("memory.enabled" in t for t in win.lines.values())
    assert not any("tmux.renumber-windows" in t for t in win.lines.values())
    for _i in range(len(config.KEYS)):
        ed.handle_key("j")
    ed._draw(win)
    assert any("tmux.renumber-windows" in t for t in win.lines.values())
    assert not any("memory.enabled" in t for t in win.lines.values())
