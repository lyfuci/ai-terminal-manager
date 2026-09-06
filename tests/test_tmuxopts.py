"""atm config tmux.* → ~/.tmux.conf 最前面的独立块 + 对活着的 server 立即生效。"""

from __future__ import annotations

from pathlib import Path

import pytest

from atm import config, tmux, tmuxopts

USER = "# mine\nset -g status off\n"


@pytest.fixture
def conf(tmp_path: Path) -> Path:
    p = tmp_path / "tmux.conf"
    p.write_text(USER, encoding="utf-8")
    return p


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(tmux, "has_server", lambda: True)
    monkeypatch.setattr(tmux, "run", lambda args, **kw: calls.append(list(args)) or "")
    return calls


def test_nothing_enabled_writes_nothing(conf: Path, live) -> None:
    plan = tmuxopts.build_plan(config.Config(), conf_path=conf)
    assert plan.is_noop and plan.block == ""
    result = tmuxopts.apply(plan)
    assert not result.written and not result.applied_live
    assert conf.read_text(encoding="utf-8") == USER
    assert live == [] and "没开" in plan.describe()


def test_block_goes_first_so_user_lines_win(conf: Path, live) -> None:
    cfg = config.Config(tmux_mouse=True)
    result = tmuxopts.apply(tmuxopts.build_plan(cfg, conf_path=conf))
    text = conf.read_text(encoding="utf-8")
    assert result.written and result.applied_live
    assert text.startswith(tmuxopts.MARKER_BEGIN + "\nset -g mouse on\n" + tmuxopts.MARKER_END)
    assert text.endswith("\n\n" + USER)  # 用户内容原样跟在后面
    assert "focus-events" not in text  # 关着的不写 off
    assert live == [["set-option", "-g", "mouse", "on"]]


def test_all_five_options_render(conf: Path, live) -> None:
    cfg = config.Config(
        tmux_mouse=True,
        tmux_focus_events=True,
        tmux_history_limit=50000,
        tmux_base_index=1,
        tmux_renumber_windows=True,
    )
    plan = tmuxopts.build_plan(cfg, conf_path=conf)
    assert plan.enabled == (
        "mouse",
        "focus-events",
        "history-limit",
        "base-index",
        "renumber-windows",
    )
    assert plan.block.splitlines()[1:-1] == [
        "set -g mouse on",
        "set -g focus-events on",
        "set -g history-limit 50000",
        "set -g base-index 1",
        "set -gw pane-base-index 1",
        "set -g renumber-windows on",
    ]
    tmuxopts.apply(plan)
    assert ["set-option", "-gw", "pane-base-index", "1"] in live


def test_reapply_is_idempotent_and_skips_backup(conf: Path, live, tmp_path: Path) -> None:
    cfg = config.Config(tmux_mouse=True, tmux_history_limit=50000)
    tmuxopts.apply(tmuxopts.build_plan(cfg, conf_path=conf))
    backups_before = len(list(tmp_path.glob("tmux.conf.bak*")))
    plan = tmuxopts.build_plan(cfg, conf_path=conf)
    assert plan.is_noop
    result = tmuxopts.apply(plan)
    assert not result.written and result.backup_path is None
    assert len(list(tmp_path.glob("tmux.conf.bak*"))) == backups_before
    assert conf.read_text(encoding="utf-8").count(tmuxopts.MARKER_BEGIN) == 1


def test_changing_a_value_rewrites_block(conf: Path, live) -> None:
    tmuxopts.apply(tmuxopts.build_plan(config.Config(tmux_history_limit=50000), conf_path=conf))
    plan = tmuxopts.build_plan(config.Config(tmux_history_limit=10000), conf_path=conf)
    assert not plan.is_noop and plan.to_turn_off == ()
    tmuxopts.apply(plan)
    text = conf.read_text(encoding="utf-8")
    assert "history-limit 10000" in text and "50000" not in text
    assert text.count(tmuxopts.MARKER_BEGIN) == 1


def test_turning_one_off_leaves_live_value_alone(conf: Path, live) -> None:
    tmuxopts.apply(
        tmuxopts.build_plan(config.Config(tmux_mouse=True, tmux_base_index=1), conf_path=conf)
    )
    live.clear()
    plan = tmuxopts.build_plan(config.Config(tmux_mouse=True), conf_path=conf)
    assert plan.to_turn_off == ("base-index",)
    tmuxopts.apply(plan)
    text = conf.read_text(encoding="utf-8")
    assert "set -g mouse on" in text and "base-index" not in text
    assert live == [["set-option", "-g", "mouse", "on"]]


def test_turning_all_off_removes_block(conf: Path, live) -> None:
    tmuxopts.apply(tmuxopts.build_plan(config.Config(tmux_mouse=True), conf_path=conf))
    plan = tmuxopts.build_plan(config.Config(), conf_path=conf)
    assert not plan.is_noop and "移除" in plan.describe()
    tmuxopts.apply(plan)
    assert conf.read_text(encoding="utf-8") == USER
    assert ["set-option", "-g", "mouse", "off"] not in live


def test_no_server_means_file_only(conf: Path, monkeypatch) -> None:
    monkeypatch.setattr(tmux, "has_server", lambda: False)
    result = tmuxopts.apply(tmuxopts.build_plan(config.Config(tmux_mouse=True), conf_path=conf))
    assert result.written and not result.applied_live and result.live_error is None


def test_live_failure_is_reported_not_raised(conf: Path, monkeypatch) -> None:
    monkeypatch.setattr(tmux, "has_server", lambda: True)

    def boom(args, **kw):
        raise tmux.TmuxError("server gone")

    monkeypatch.setattr(tmux, "run", boom)
    result = tmuxopts.apply(tmuxopts.build_plan(config.Config(tmux_mouse=True), conf_path=conf))
    assert result.written and not result.applied_live and "server gone" in (result.live_error or "")


def test_remove_only_touches_our_block(conf: Path, live) -> None:
    tmuxopts.apply(tmuxopts.build_plan(config.Config(tmux_mouse=True), conf_path=conf))
    removed, backup = tmuxopts.remove(conf)
    assert removed and backup is not None
    assert conf.read_text(encoding="utf-8") == USER
    assert tmuxopts.remove(conf) == (False, None)


def test_empty_conf_gets_just_the_block(tmp_path: Path, live) -> None:
    conf = tmp_path / "new.conf"
    tmuxopts.apply(tmuxopts.build_plan(config.Config(tmux_mouse=True), conf_path=conf))
    assert conf.read_text(encoding="utf-8") == (
        f"{tmuxopts.MARKER_BEGIN}\nset -g mouse on\n{tmuxopts.MARKER_END}\n"
    )
