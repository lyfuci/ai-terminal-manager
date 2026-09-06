"""`atm install` 不再问问题：--key 等参数先记进 config 再装，其余一律按 config 应用。"""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from atm import cli, config, dispatch, guard, install, tmuxopts


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    conf = tmp_path / "tmux.conf"
    conf.write_text("set -g status off\n", encoding="utf-8")
    monkeypatch.setattr(guard, "unit_dir", lambda: tmp_path / "units")
    monkeypatch.setattr(guard, "_daemon_reload", lambda: None)
    monkeypatch.setattr(dispatch, "memory_limits_available", lambda: False)
    monkeypatch.setenv("ATM_CONFIG", str(tmp_path / "c.toml"))
    monkeypatch.setattr(cli.tmux, "has_server", lambda: False)
    monkeypatch.setattr(cli.tmux, "is_installed", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda prompt="": pytest.fail(f"不该问：{prompt!r}"))
    return conf


def test_bare_install_never_asks_beyond_confirm(env: Path, monkeypatch) -> None:
    asked: list[str] = []
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda prompt="": asked.append(prompt) or "y")
    assert cli.main(["install", "--no-persist", "--conf", str(env)]) == cli.EXIT_OK
    assert len(asked) == 1 and "继续" in asked[0]
    text = env.read_text(encoding="utf-8")
    assert f"bind-key {install.DEFAULT_KEY} " in text
    assert tmuxopts.MARKER_BEGIN not in text  # tmux.* 默认全关
    assert not config.config_path().exists()  # 没给参数就不碰配置文件


def test_key_flags_are_saved_to_config_then_applied(env: Path, capsys) -> None:
    args = ["install", "-y", "--no-persist", "--key", "s", "--sidebar-key", "g", "--conf", str(env)]
    assert cli.main(args) == cli.EXIT_OK
    cfg = config.load()
    assert cfg.keys_pick == "s" and cfg.keys_sidebar == "g"
    text = env.read_text(encoding="utf-8")
    assert "bind-key s " in text and "bind-key g " in text
    assert "keys.*" in capsys.readouterr().out


def test_install_uses_config_keys_when_no_flags(env: Path) -> None:
    config.save(config.Config(keys_pick="x", keys_popup_width="90%"))
    assert cli.main(["install", "-y", "--no-persist", "--conf", str(env)]) == cli.EXIT_OK
    text = env.read_text(encoding="utf-8")
    assert "bind-key x " in text and "-w 90%" in text


def test_install_applies_tmux_options_from_config(env: Path) -> None:
    config.save(config.Config(tmux_mouse=True, tmux_history_limit=50000))
    assert cli.main(["install", "-y", "--no-persist", "--conf", str(env)]) == cli.EXIT_OK
    text = env.read_text(encoding="utf-8")
    assert text.startswith(tmuxopts.MARKER_BEGIN)
    assert "set -g history-limit 50000" in text and "bind-key a " in text


def test_invalid_flag_is_one_line_error_and_no_write(env: Path, capsys) -> None:
    before = env.read_text(encoding="utf-8")
    assert cli.main(["install", "-y", "--key", "1", "--conf", str(env)]) == cli.EXIT_ERROR
    assert "小写字母" in capsys.readouterr().err
    assert env.read_text(encoding="utf-8") == before
    assert not config.config_path().exists()


def test_same_pick_and_sidebar_flag_rejected(env: Path, capsys) -> None:
    args = ["install", "-y", "--key", "b", "--conf", str(env)]  # 侧栏默认也是 b
    assert cli.main(args) == cli.EXIT_ERROR
    assert "keys.sidebar" in capsys.readouterr().err


def test_print_writes_nothing(env: Path, capsys) -> None:
    before = env.read_text(encoding="utf-8")
    assert cli.main(["install", "--print", "--key", "s", "--conf", str(env)]) == cli.EXIT_OK
    assert "bind-key s " in capsys.readouterr().out
    assert env.read_text(encoding="utf-8") == before
    assert not config.config_path().exists()  # --print 连配置也不记


def test_non_tty_without_yes_cancels(env: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert cli.main(["install", "--conf", str(env)]) == cli.EXIT_CANCELLED
    assert "非交互" in capsys.readouterr().out
