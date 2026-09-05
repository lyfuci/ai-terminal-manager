"""`atm config` + `atm claude …` 启动包装 + 总量闸门 slice。

配置文件一律通过 ATM_CONFIG 指到 tmp_path；slice 单元写到 tmp_path；
systemd-run / systemctl 一律不真跑。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from atm import cli, config, dispatch, guard


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "atm" / "config.toml"
    monkeypatch.setenv("ATM_CONFIG", str(p))
    return p


@pytest.fixture
def cgroup_ok(monkeypatch):
    monkeypatch.setattr(dispatch, "memory_limits_available", lambda: True)


# ---------------------------------------------------------------- 读写


def test_missing_file_is_all_defaults(cfg_path: Path) -> None:
    cfg = config.load()
    assert cfg == config.Config()
    assert cfg.memory_high == dispatch.DEFAULT_MEMORY_HIGH


def test_set_save_load_roundtrip(cfg_path: Path) -> None:
    cfg = config.set_value(config.Config(), "memory.high", "4g")
    cfg = config.set_value(cfg, "memory.max", "8G")
    cfg = config.set_value(cfg, "memory.user", "false")
    config.save(cfg)

    loaded = config.load()
    assert loaded.memory_high == "4G"  # 大小写归一
    assert loaded.memory_max == "8G"
    assert loaded.memory_user is False
    assert loaded.memory_enabled is True  # 没动的保持默认


def test_unset_restores_default(cfg_path: Path) -> None:
    cfg = config.set_value(config.Config(), "memory.high", "4G")
    cfg = config.unset_value(cfg, "memory.high")
    assert cfg.memory_high == dispatch.DEFAULT_MEMORY_HIGH


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("memory.high", "4 gigs"),
        ("memory.max", "-1G"),
        ("memory.enabled", "maybe"),
        ("memory.slice", "no-suffix"),
        ("memory.nope", "1"),
    ],
)
def test_invalid_values_rejected(key: str, bad: str) -> None:
    with pytest.raises(config.ConfigError):
        config.set_value(config.Config(), key, bad)


def test_infinity_allowed() -> None:
    cfg = config.set_value(config.Config(), "memory.max", "infinity")
    assert cfg.memory_max == "infinity"


def test_corrupt_file_raises_not_silently_defaults(cfg_path: Path) -> None:
    """配置坏了要报错。静默回默认会让人以为限制生效了其实没有。"""
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("[memory\nhigh = ", encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load()


def test_dumps_is_valid_toml_and_flat() -> None:
    import tomllib

    text = config.dumps(config.Config(memory_high="3G", memory_user=False))
    data = tomllib.loads(text)
    assert data["memory"]["high"] == "3G"
    assert data["memory"]["user"] is False
    assert data["memory"]["swap_max"] == dispatch.DEFAULT_MEMORY_SWAP_MAX


# ---------------------------------------------------------------- 闸门 → argv


def test_memory_limit_none_when_disabled(cgroup_ok) -> None:
    assert config.Config(memory_enabled=False).memory_limit() is None


def test_memory_limit_none_when_cgroup_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(dispatch, "memory_limits_available", lambda: False)
    assert config.Config().memory_limit() is None


def test_launch_argv_wraps_with_systemd_run(cgroup_ok) -> None:
    cfg = config.Config(memory_high="4G", memory_max="8G")
    argv = config.launch_argv("/usr/bin/claude", ["--resume", "abc"], cfg)
    assert argv[0] == "systemd-run"
    assert "--user" in argv
    assert "MemoryHigh=4G" in argv
    assert "MemoryMax=8G" in argv
    assert f"--slice={dispatch.DEFAULT_SLICE}" in argv
    # 程序和参数在最后，原样透传
    assert argv[-3:] == ["/usr/bin/claude", "--resume", "abc"]


def test_launch_argv_plain_when_disabled(cgroup_ok) -> None:
    cfg = config.Config(memory_enabled=False)
    assert config.launch_argv("/usr/bin/codex", ["resume", "x"], cfg) == [
        "/usr/bin/codex",
        "resume",
        "x",
    ]


def test_launch_argv_system_scope_drops_user_flag(cgroup_ok) -> None:
    argv = config.launch_argv("/usr/bin/pi", [], config.Config(memory_user=False))
    assert "--user" not in argv
    assert argv[0] == "systemd-run"


# ---------------------------------------------------------------- cli 接线


def test_launch_args_pass_everything_through_untouched() -> None:
    """`atm claude --resume x -p y`：程序名之后的每个字节都是 claude 的，包括长得像 atm 选项的。"""
    ns = cli._launch_args(["claude", "--resume", "abc", "-p", "hello", "--no-mem-limit"])
    assert ns is not None
    assert ns.program == "claude"
    assert ns.args == ["--resume", "abc", "-p", "hello", "--no-mem-limit"]
    assert ns.handler is cli._cmd_launch


def test_launch_args_help_goes_to_program_not_atm() -> None:
    ns = cli._launch_args(["codex", "--help"])
    assert ns is not None and ns.args == ["--help"]


def test_launch_args_ignores_other_commands() -> None:
    assert cli._launch_args(["list", "-n", "3"]) is None
    assert cli._launch_args([]) is None


def test_main_routes_launch_before_argparse(cfg_path: Path, cgroup_ok, monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(config, "resolve_program", lambda name: f"/bin/{name}")
    monkeypatch.setattr(cli.os, "execvp", lambda file, argv: calls.append(list(argv)))
    cli.main(["pi", "--session", "abc", "--help"])
    assert calls and calls[0][-4:] == ["/bin/pi", "--session", "abc", "--help"]


def test_memory_limit_falls_back_to_config(cfg_path: Path, cgroup_ok) -> None:
    config.save(config.set_value(config.Config(), "memory.high", "6G"))
    ns = argparse.Namespace(no_mem_limit=False, mem_high=None, mem_max=None)
    limit = cli._memory_limit(ns)
    assert limit is not None
    assert limit.high == "6G"
    assert limit.max == dispatch.DEFAULT_MEMORY_MAX


def test_memory_limit_cli_flag_beats_config(cfg_path: Path, cgroup_ok) -> None:
    config.save(config.set_value(config.Config(), "memory.high", "6G"))
    ns = argparse.Namespace(no_mem_limit=False, mem_high="1G", mem_max=None)
    assert cli._memory_limit(ns).high == "1G"


def test_memory_limit_respects_disabled_config(cfg_path: Path, cgroup_ok) -> None:
    config.save(config.set_value(config.Config(), "memory.enabled", "false"))
    ns = argparse.Namespace(no_mem_limit=False, mem_high=None, mem_max=None)
    assert cli._memory_limit(ns) is None


def test_launch_execs_wrapped_argv(cfg_path: Path, cgroup_ok, monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(config, "resolve_program", lambda name: f"/bin/{name}")
    monkeypatch.setattr(cli.os, "execvp", lambda file, argv: calls.append([file, *argv[1:]]))
    ns = argparse.Namespace(program="claude", args=["--resume", "id1"])
    cli._cmd_launch(ns)
    assert calls and calls[0][0] == "systemd-run"
    assert calls[0][-3:] == ["/bin/claude", "--resume", "id1"]


def test_launch_missing_program_is_an_error(cfg_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(config, "resolve_program", lambda name: None)
    ns = argparse.Namespace(program="pi", args=[])
    assert cli._cmd_launch(ns) == cli.EXIT_ERROR
    assert "PATH 里没有 pi" in capsys.readouterr().err


def test_launch_without_systemd_run_falls_back_to_plain(
    cfg_path: Path, cgroup_ok, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        config, "resolve_program", lambda n: None if n == "systemd-run" else f"/bin/{n}"
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(cli.os, "execvp", lambda file, argv: calls.append(list(argv)))
    cli._cmd_launch(argparse.Namespace(program="claude", args=["x"]))
    assert calls == [["/bin/claude", "x"]]
    assert "不套内存闸门" in capsys.readouterr().err


def test_config_command_set_and_show(cfg_path: Path, capsys) -> None:
    assert cli.main(["config", "memory.high", "5G"]) == cli.EXIT_OK
    assert cli.main(["config"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "memory.high" in out and "5G" in out and "已设置" in out


def test_config_command_rejects_garbage(cfg_path: Path, capsys) -> None:
    assert cli.main(["config", "memory.high", "lots"]) == cli.EXIT_ERROR
    assert "memory.high" in capsys.readouterr().err
    assert not cfg_path.exists()  # 没写坏东西进去


# ---------------------------------------------------------------- 总量闸门 slice


def test_suggested_totals_scale_with_ram() -> None:
    gib = 1 << 30
    assert guard.suggested_totals(8 * gib) == ("4G", "5G")  # 和 8 月手写的那份一致
    assert guard.suggested_totals(48 * gib) == ("24G", "31G")
    high, max_ = guard.suggested_totals(1 * gib)
    assert int(high[:-1]) < int(max_[:-1])  # 再小也保证 High < Max


def test_slice_install_writes_once_and_never_overwrites(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(guard, "_daemon_reload", lambda: None)
    path, written = guard.install("atm-ai.slice", tmp_path, total_bytes=16 << 30)
    assert written and path.exists()
    st = guard.status("atm-ai.slice", tmp_path)
    assert st.ours and st.high == "8G" and st.max == "10G"

    path.write_text("[Slice]\nMemoryHigh=1G\n", encoding="utf-8")  # 用户手改
    _, written_again = guard.install("atm-ai.slice", tmp_path, total_bytes=16 << 30)
    assert not written_again
    assert guard.status("atm-ai.slice", tmp_path).high == "1G"


def test_slice_remove_only_touches_ours(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(guard, "_daemon_reload", lambda: None)
    theirs = tmp_path / "atm-ai.slice"
    theirs.write_text("[Slice]\nMemoryHigh=1G\n", encoding="utf-8")
    assert guard.remove("atm-ai.slice", tmp_path) is False
    assert theirs.exists()

    theirs.unlink()
    guard.install("atm-ai.slice", tmp_path, total_bytes=8 << 30)
    assert guard.remove("atm-ai.slice", tmp_path) is True
    assert not theirs.exists()


def test_slice_status_when_missing(tmp_path: Path) -> None:
    st = guard.status("atm-ai.slice", tmp_path)
    assert not st.exists and not st.ours and st.high is None
