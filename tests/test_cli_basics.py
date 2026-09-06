"""P1：通用 CLI 能力 —— -v 日志、--json 契约、补全、配置优先级、NO_COLOR、控制字符、--conf。"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pytest

from atm import cli, completion, config, dispatch, text, update
from atm.model import Source


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "atm" / "config.toml"
    monkeypatch.setenv("ATM_CONFIG", str(p))
    for key in config.KEYS:
        monkeypatch.delenv(config.env_var_for(key), raising=False)
    return p


# ---------------------------------------------------------------- -v / 日志


def test_verbose_flag_counts_and_launch_prefix() -> None:
    ns = cli._build_parser().parse_args(["-vv", "list"])
    assert ns.verbose == 2
    launch = cli._launch_args(["-v", "claude", "--resume", "x"])
    assert launch is not None and launch.verbose == 1 and launch.args == ["--resume", "x"]


def test_setup_logging_levels(monkeypatch) -> None:
    monkeypatch.delenv("ATM_DEBUG", raising=False)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    cli._setup_logging(0)
    assert root.level == logging.WARNING
    for h in list(root.handlers):
        root.removeHandler(h)
    cli._setup_logging(2)
    assert root.level == logging.DEBUG


def test_safe_parse_logs_reason_at_debug(caplog, tmp_path: Path) -> None:
    from atm import index as index_mod
    from atm.model import FileRef

    ref = FileRef(path=str(tmp_path / "x.jsonl"), mtime_ns=0, size_bytes=0)

    def boom(_ref):
        raise ValueError("bad line")

    with caplog.at_level(logging.DEBUG, logger="atm.index"):
        assert index_mod._safe_parse(ref, boom) is None
    assert "bad line" in caplog.text


# ---------------------------------------------- 优先级 flag > env > file > default


def test_env_overrides_file(cfg_path: Path, monkeypatch) -> None:
    config.save(config.set_value(config.Config(), "memory.high", "3G"))
    monkeypatch.setenv("ATM_MEMORY_HIGH", "6G")
    cfg, sources = config.load_with_sources()
    assert cfg.memory_high == "6G"
    assert sources["memory.high"] == "env ATM_MEMORY_HIGH"
    assert sources["memory.max"] == "default"


def test_flag_overrides_env(cfg_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ATM_MEMORY_HIGH", "6G")
    monkeypatch.setattr(dispatch, "memory_limits_available", lambda: True)
    ns = argparse.Namespace(no_mem_limit=False, mem_high="1G", mem_max=None)
    assert cli._memory_limit(ns).high == "1G"


def test_env_value_is_validated(cfg_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ATM_MEMORY_MAX", "lots")
    with pytest.raises(config.ConfigError, match=r"memory\.max"):
        config.load()


def test_env_bool_and_key_mapping(cfg_path: Path, monkeypatch) -> None:
    assert config.env_var_for("memory.swap-max") == "ATM_MEMORY_SWAP_MAX"
    monkeypatch.setenv("ATM_MEMORY_ENABLED", "off")
    assert config.load().memory_enabled is False


def test_config_json_reports_sources(cfg_path: Path, monkeypatch, capsys) -> None:
    config.save(config.set_value(config.Config(), "memory.high", "3G"))
    monkeypatch.setenv("ATM_MEMORY_MAX", "9G")
    assert cli.main(["config", "--json"]) == cli.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["values"]["memory.high"] == {"value": "3G", "source": "file"}
    assert data["values"]["memory.max"]["source"] == "env ATM_MEMORY_MAX"
    assert data["values"]["memory.slice"]["source"] == "default"


def test_config_describe_shows_source(cfg_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("ATM_MEMORY_HIGH", "5G")
    assert cli.main(["config"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "← env ATM_MEMORY_HIGH" in out and "优先级" in out


# ---------------------------------------------------------------- --json 契约


def test_doctor_json_shape(cfg_path: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    from atm import index as index_mod
    from atm.model import IndexStats, SessionIndex

    stats = IndexStats(total=0, from_cache=0, parsed=0, skipped=0, elapsed_ms=1)
    monkeypatch.setattr(index_mod, "build", lambda use_cache=True: SessionIndex((), stats))
    assert cli.main(["doctor", "--json"]) == cli.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    for key in ("version", "sources", "clis", "tmux", "persistence", "memory", "index", "ok"):
        assert key in data
    assert set(data["sources"]) == {"claude", "codex", "pi"}
    assert data["ok"] is True


def test_doctor_json_reports_config_error_and_exit_1(
    cfg_path: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    from atm import index as index_mod
    from atm.model import IndexStats, SessionIndex

    monkeypatch.setattr(
        index_mod,
        "build",
        lambda use_cache=True: SessionIndex((), IndexStats(0, 0, 0, 0, 1)),
    )
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("[memory\n", encoding="utf-8")
    assert cli.main(["doctor", "--json"]) == cli.EXIT_ERROR
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False and data["configError"]


def test_update_check_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        update, "detect", lambda: update.InstallInfo(update.Installer.UV_TOOL, update.Origin.INDEX)
    )
    monkeypatch.setattr(update, "latest_version", lambda: "99.0.0")
    assert cli._cmd_update(argparse.Namespace(check=True, yes=False, json=True)) == cli.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["updateAvailable"] is True
    assert data["command"] == ["uv", "tool", "upgrade", update.DIST_NAME]


# ---------------------------------------------------------------- 补全


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_lists_every_subcommand_and_config_key(shell: str, capsys) -> None:
    assert cli.main(["completion", shell]) == cli.EXIT_OK
    out = capsys.readouterr().out
    _, specs = completion.collect(cli._build_parser())
    for spec in specs:
        assert spec.name in out
    for key in config.KEYS:
        assert key in out
    assert "no-persist" in out  # 子命令选项也进了（fish 写成 -l no-persist）


def test_completion_collect_tracks_parser() -> None:
    top, specs = completion.collect(cli._build_parser())
    names = {s.name for s in specs}
    assert {"list", "pick", "config", "update", "completion", "claude", "codex", "pi"} <= names
    assert "--version" in top and "-v" in top


def test_completion_rejects_unknown_shell() -> None:
    with pytest.raises(ValueError):
        completion.render("powershell", cli._build_parser(), config_keys=(), sources=())


# ---------------------------------------------------------------- NO_COLOR / 控制字符 / --conf


def test_strip_control_keeps_tab_and_text() -> None:
    assert text.strip_control("a\x1b[31mb\tc\x7f\x07\x1b]0;t\x07") == "ab\tc"


def test_list_output_strips_control_chars(monkeypatch, capsys) -> None:
    from datetime import UTC, datetime

    from atm.model import SessionEntry

    entry = SessionEntry(
        id="abc",
        title="t\x1b[2Jitle",
        source=Source.CLAUDE,
        cwd="/tmp/p\x07roj",
        git_branch=None,
        updated_at=datetime.now(tz=UTC),
        path="/tmp/x.jsonl",
        size_bytes=1,
    )
    monkeypatch.setattr(cli, "_load_entries", lambda args: [entry])
    assert cli._cmd_list(argparse.Namespace(json=False, limit=5)) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out and "title" in out


def test_no_color_skips_curses_color_setup(monkeypatch) -> None:
    import curses

    from atm import tui

    calls = []
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(curses, "use_default_colors", lambda: calls.append("colors"))
    monkeypatch.setattr(curses, "init_pair", lambda *a: calls.append("pair"))
    monkeypatch.setattr(curses, "curs_set", lambda n: None)

    class FakeWin:
        def keypad(self, flag):
            pass

    tui._Screen()._setup(FakeWin())
    assert calls == []


def test_install_and_uninstall_honour_conf(tmp_path: Path, monkeypatch, capsys) -> None:
    from atm import guard

    conf = tmp_path / "custom.conf"
    conf.write_text("set -g mouse on\n", encoding="utf-8")
    monkeypatch.setattr(guard, "unit_dir", lambda: tmp_path / "units")
    monkeypatch.setattr(guard, "_daemon_reload", lambda: None)
    monkeypatch.setenv("ATM_CONFIG", str(tmp_path / "c.toml"))
    monkeypatch.setattr(cli.tmux, "has_server", lambda: False)
    assert (
        cli.main(["install", "-y", "--no-persist", "--no-slice", "--conf", str(conf)])
        == cli.EXIT_OK
    )
    assert "atm (ai-terminal-manager)" in conf.read_text(encoding="utf-8")
    from atm import tmuxopts

    tmuxopts.apply(tmuxopts.build_plan(config.Config(tmux_focus_events=True), conf_path=conf))
    assert tmuxopts.MARKER_BEGIN in conf.read_text(encoding="utf-8")
    assert cli.main(["uninstall", "-y", "--conf", str(conf)]) == cli.EXIT_OK
    assert "atm (ai-terminal-manager)" not in conf.read_text(encoding="utf-8")
    assert tmuxopts.MARKER_BEGIN not in conf.read_text(encoding="utf-8")
    assert "set -g mouse on" in conf.read_text(encoding="utf-8")  # 用户自己的行不动
