"""侧栏 TUI 的状态机（不碰 curses）：Ctrl-T 选格 → 数字 / 回车 → swap 进指定格。"""

from __future__ import annotations

import pytest

from atm import index as index_mod
from atm import sidebar_tui, tmux
from atm.sidebar_tui import RunningRow, SectionHeader, SlotRow, _Sidebar

SEP = "\x1f"


def _line(id: str, window_id: str = "@0", command: str = "bash", **kw: object) -> str:
    f = {
        "id": id, "session": "main", "wi": "0", "wn": "w", "pi": "0", "cmd": command,
        "path": "/p", "active": "0", "in_mode": "0", "wactive": "1", "w": "80", "h": "24",
        "title": "", "window_id": window_id, "last": "0", "sidebar": "",
    }  # fmt: skip
    f.update({k: str(v) for k, v in kw.items()})
    return SEP.join(f.values())


LAYOUT = "\n".join(
    [
        _line("%sb", sidebar="1", active="1", pi="0"),
        _line("%top", last="1", pi="1", command="claude", title="✳ top"),
        _line("%bottom", pi="2", command="claude", title="✳ bottom"),
        _line("%bg", "@1", "claude", wn="bg", title="✳ parked"),
    ]
)


@pytest.fixture
def ui(monkeypatch: pytest.MonkeyPatch) -> tuple[_Sidebar, list[list[str]]]:
    calls: list[list[str]] = []

    def run(args: list[str], **kw: object) -> str:
        calls.append(args)
        return LAYOUT if args[0] == "list-panes" else ""

    monkeypatch.setattr(tmux, "run", run)
    monkeypatch.setattr(tmux, "is_installed", lambda: True)
    monkeypatch.setattr(sidebar_tui, "_INDEX_REFRESH_SECONDS", 10**9)
    monkeypatch.setattr(
        index_mod, "build", lambda **kw: index_mod.SessionIndex(entries=(), stats=None)
    )
    s = _Sidebar("%sb", None)
    s._refresh_panes(force=True)
    s._recompute()
    return s, calls


def _select(s: _Sidebar, pane_id: str) -> None:
    for i, row in enumerate(s._rows):
        if isinstance(row, RunningRow) and row.pane.id == pane_id:
            s._cursor = i
            return
    raise AssertionError(pane_id)


def test_enter_without_placing_swaps_into_main_slot(ui) -> None:
    s, calls = ui
    _select(s, "%bg")
    s._activate()
    assert ["swap-pane", "-s", "%bg", "-t", "%top"] in calls


def test_ctrl_t_lists_window_panes_numbered_by_pane_index(ui) -> None:
    s, _ = ui
    _select(s, "%bg")
    s._begin_place()
    assert s._placing is not None
    assert isinstance(s._rows[0], SectionHeader)
    slots = [r for r in s._rows if isinstance(r, SlotRow)]
    assert [(r.pane.id, r.pane.pane_index, r.is_main) for r in slots] == [
        ("%top", 1, True),
        ("%bottom", 2, False),
    ]
    assert isinstance(s._rows[s._cursor], SlotRow), "光标直接落在第一个候选上"


def test_digit_picks_slot_by_pane_index(ui) -> None:
    s, calls = ui
    _select(s, "%bg")
    s._begin_place()
    s._pick_slot_by_index(2)
    assert ["swap-pane", "-s", "%bg", "-t", "%bottom"] in calls
    assert s._placing is None, "换完退出选格模式"


def test_enter_on_slot_row_swaps_there(ui) -> None:
    s, calls = ui
    _select(s, "%bg")
    s._begin_place()
    s._cursor = next(
        i for i, r in enumerate(s._rows) if isinstance(r, SlotRow) and r.pane.id == "%bottom"
    )
    s._activate()
    assert ["swap-pane", "-s", "%bg", "-t", "%bottom"] in calls


def test_unknown_digit_keeps_placing(ui) -> None:
    s, calls = ui
    _select(s, "%bg")
    s._begin_place()
    s._pick_slot_by_index(7)
    assert s._placing is not None
    assert not any(c[0] == "swap-pane" for c in calls)
    assert "7" in s._status


def test_placing_can_target_a_pane_in_same_window(ui) -> None:
    """目标已在本窗口时，默认只切焦点；指定了格子就真的换位。"""
    s, calls = ui
    _select(s, "%bottom")
    s._begin_place()
    s._pick_slot_by_index(1)
    assert ["swap-pane", "-s", "%bottom", "-t", "%top"] in calls
