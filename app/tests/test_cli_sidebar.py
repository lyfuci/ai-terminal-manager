"""`atm swap` / `atm park` / `atm sidebar --toggle` 的命令行接线。tmux 全部打桩。"""

from __future__ import annotations

import pytest

from atm import cli, tmux
from atm.tmux import Pane

SEP = "\x1f"


def _line(id: str, window_id: str = "@0", command: str = "bash", **kw: object) -> str:
    f = {
        "id": id, "session": "main", "wi": "0", "wn": "w", "pi": "0", "cmd": command,
        "path": "/p", "active": "0", "in_mode": "0", "wactive": "1", "w": "80", "h": "24",
        "title": "", "window_id": window_id, "last": "0", "sidebar": "",
    }  # fmt: skip
    f.update({k: str(v) for k, v in kw.items()})
    return SEP.join(f.values())


@pytest.fixture
def fake_tmux(monkeypatch: pytest.MonkeyPatch):
    """把 tmux.run 换成记录器；list-panes 返回预设的布局。"""
    state = {"panes": "", "calls": []}

    def run(args: list[str], **kw: object) -> str:
        state["calls"].append(args)
        if args[0] == "list-panes":
            return state["panes"]
        if args[0] == "list-windows":
            return "@9\tbg\n"
        if args[0] == "split-window":
            return "%new\n"
        return ""

    monkeypatch.setattr(tmux, "run", run)
    monkeypatch.setattr(tmux, "has_server", lambda: True)
    monkeypatch.setattr(tmux, "is_installed", lambda: True)
    return state


def test_swap_from_other_window(fake_tmux, capsys) -> None:
    fake_tmux["panes"] = "\n".join(
        [_line("%me", active="1"), _line("%main", last="1"), _line("%bg", "@1", "claude")]
    )
    assert cli.main(["swap", "%bg", "--pane", "%me"]) == 0
    assert ["swap-pane", "-s", "%bg", "-t", "%main"] in fake_tmux["calls"]
    assert ["select-pane", "-t", "%bg"] in fake_tmux["calls"]
    assert "换进 %main" in capsys.readouterr().err


def test_swap_unknown_pane_is_an_error(fake_tmux, capsys) -> None:
    fake_tmux["panes"] = _line("%me")
    assert cli.main(["swap", "%zz", "--pane", "%me"]) == 1
    assert "找不到" in capsys.readouterr().err


def test_park_joins_existing_bg(fake_tmux) -> None:
    fake_tmux["panes"] = "\n".join([_line("%a"), _line("%b")])
    assert cli.main(["park", "%a"]) == 0
    assert ["join-pane", "-d", "-s", "%a", "-t", "@9"] in fake_tmux["calls"]


def test_park_refuses_lone_pane(fake_tmux, capsys) -> None:
    fake_tmux["panes"] = _line("%solo", "@3")
    assert cli.main(["park", "%solo"]) == 1
    assert "独占" in capsys.readouterr().err


def test_toggle_opens_full_height_sidebar_and_marks_it(fake_tmux, monkeypatch) -> None:
    from atm import install as install_mod

    monkeypatch.setattr(install_mod, "resolve_atm_command", lambda: "atm")
    fake_tmux["panes"] = _line("%a", active="1")
    assert cli.main(["sidebar", "--toggle", "--pane", "%a", "--width", "28"]) == 0
    split = next(c for c in fake_tmux["calls"] if c[0] == "split-window")
    assert "-f" in split and "-b" in split and split[split.index("-l") + 1] == "28"
    assert split[-1] == "atm sidebar"
    assert ["set-option", "-p", "-t", "%new", tmux.SIDEBAR_OPTION, "1"] in fake_tmux["calls"]


def test_toggle_on_sidebar_closes_it(fake_tmux) -> None:
    fake_tmux["panes"] = "\n".join([_line("%s", sidebar="1", active="1"), _line("%a")])
    assert cli.main(["sidebar", "--toggle", "--pane", "%s"]) == 0
    assert ["kill-pane", "-t", "%s"] in fake_tmux["calls"]


def test_toggle_elsewhere_focuses_sidebar(fake_tmux) -> None:
    fake_tmux["panes"] = "\n".join([_line("%s", sidebar="1"), _line("%a", active="1")])
    assert cli.main(["sidebar", "--toggle", "--pane", "%a"]) == 0
    assert ["select-pane", "-t", "%s"] in fake_tmux["calls"]
    assert not any(c[0] in ("split-window", "kill-pane") for c in fake_tmux["calls"])


def test_toggle_needs_a_pane(fake_tmux, monkeypatch, capsys) -> None:
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert cli.main(["sidebar", "--toggle"]) == 1
    assert "--pane" in capsys.readouterr().err


def test_resident_mode_refuses_non_tty(capsys) -> None:
    assert cli.main(["sidebar"]) == 1
    assert "--toggle" in capsys.readouterr().err


def test_pane_fixture_shape_matches_parser() -> None:
    """守住 _line 和 parse_panes 的字段顺序一致 —— 不然上面的测试在测一个假布局。"""
    p: Pane = tmux.parse_panes(_line("%x", "@7", "claude", last="1", sidebar="1"))[0]
    assert (p.id, p.window_id, p.current_command, p.last, p.is_sidebar) == (
        "%x", "@7", "claude", True, True,
    )  # fmt: skip
