"""P0 加固：Codex 复审 + 本机复现出来的九处。每条先有失败测试再修。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from atm import cli, config, dispatch, guard
from atm import install as install_mod


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "atm" / "config.toml"
    monkeypatch.setenv("ATM_CONFIG", str(p))
    return p


# ---------------------------------------------------------------- list --json 空结果


def test_list_json_empty_is_valid_json(monkeypatch, capsys) -> None:
    """`atm list --json | jq` 在没有会话的机器上不能拿到空 stdout。"""
    monkeypatch.setattr(cli, "_load_entries", lambda args: [])
    ns = argparse.Namespace(json=True, limit=30)
    assert cli._cmd_list(ns) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert json.loads(out) == []


# ---------------------------------------------------------------- 配置结构与未知键


def test_config_section_must_be_table(cfg_path: Path) -> None:
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text('memory = "x"\n', encoding="utf-8")
    with pytest.raises(config.ConfigError, match="应该是一个表"):
        config.load()


def test_config_unknown_key_is_an_error_not_silence(cfg_path: Path) -> None:
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text('[memory]\nhihg = "4G"\n', encoding="utf-8")
    with pytest.raises(config.ConfigError, match="hihg"):
        config.load()


def test_config_unknown_section_is_an_error(cfg_path: Path) -> None:
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text('[memroy]\nhigh = "4G"\n', encoding="utf-8")
    with pytest.raises(config.ConfigError, match="memroy"):
        config.load()


def test_config_save_is_atomic_leaves_no_tmp(cfg_path: Path) -> None:
    config.save(config.Config(memory_high="3G"))
    assert cfg_path.exists()
    assert not cfg_path.with_name(cfg_path.name + ".tmp").exists()
    assert config.load().memory_high == "3G"


# ---------------------------------------------------------------- 命令行值校验 / 互斥


def test_mem_flag_values_are_validated(cfg_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dispatch, "memory_limits_available", lambda: True)
    ns = argparse.Namespace(no_mem_limit=False, mem_high="lots", mem_max=None)
    with pytest.raises(config.ConfigError, match="--mem-high"):
        cli._memory_limit(ns)
    ns = argparse.Namespace(no_mem_limit=False, mem_high="3g", mem_max="6G")
    limit = cli._memory_limit(ns)
    assert limit is not None and limit.high == "3G"


def test_resume_target_flags_are_mutually_exclusive() -> None:
    parser = cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["resume", "abc", "--split", "--window"])
    with pytest.raises(SystemExit):
        parser.parse_args(["resume", "abc", "--pane", "%1", "--print"])
    ns = parser.parse_args(["resume", "abc", "--split", "--vertical"])
    assert ns.split and ns.vertical


# ---------------------------------------------------------------- main() 的错误面


def test_main_turns_expected_errors_into_one_line(monkeypatch, capsys) -> None:
    def boom(args):
        raise install_mod.ConfUnreadable("配置读不了")

    parser = cli._build_parser()
    monkeypatch.setattr(cli, "_build_parser", lambda: parser)
    ns = argparse.Namespace(handler=boom)
    monkeypatch.setattr(parser, "parse_args", lambda raw: ns)
    assert cli.main(["doctor"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert err.strip() == "atm: 配置读不了"


def test_main_reraises_with_debug_env(monkeypatch) -> None:
    def boom(args):
        raise install_mod.ConfUnreadable("x")

    parser = cli._build_parser()
    monkeypatch.setattr(cli, "_build_parser", lambda: parser)
    monkeypatch.setattr(parser, "parse_args", lambda raw: argparse.Namespace(handler=boom))
    monkeypatch.setenv("ATM_DEBUG", "1")
    with pytest.raises(install_mod.ConfUnreadable):
        cli.main(["doctor"])


# ---------------------------------------------------------------- 备份必须成功


def test_backup_failure_aborts_instead_of_overwriting(tmp_path: Path, monkeypatch) -> None:
    conf = tmp_path / ".tmux.conf"
    conf.write_text("set -g mouse on\n", encoding="utf-8")

    def cant_copy(src, dst):
        raise PermissionError("nope")

    monkeypatch.setattr(install_mod.shutil, "copy2", cant_copy)
    plan = install_mod.build_plan(conf_path=conf)
    with pytest.raises(install_mod.BackupFailed):
        install_mod.apply(plan, live=False)
    assert conf.read_text(encoding="utf-8") == "set -g mouse on\n"  # 一个字没动


def test_backup_names_do_not_collide(tmp_path: Path, monkeypatch) -> None:
    conf = tmp_path / ".tmux.conf"
    conf.write_text("a\n", encoding="utf-8")

    class FrozenClock:
        @staticmethod
        def now():
            from datetime import datetime

            return datetime(2026, 9, 6, 1, 2, 3, 4)

    monkeypatch.setattr(install_mod, "datetime", FrozenClock)
    first = install_mod._backup(conf)
    second = install_mod._backup(conf)
    assert first != second and first.exists() and second.exists()


# ---------------------------------------------------------------- doctor / install / uninstall


def test_doctor_exits_nonzero_on_broken_config(cfg_path: Path, monkeypatch, capsys) -> None:
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("[memory\n", encoding="utf-8")
    assert cli.main(["doctor"]) == cli.EXIT_ERROR
    assert "❌" in capsys.readouterr().out


def test_install_print_shows_slice_plan(
    cfg_path: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(guard, "unit_dir", lambda: tmp_path / "units")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert cli.main(["install", "--print", "--no-persist"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "总量闸门" in out and "MemoryHigh=" in out
    assert not (tmp_path / "units").exists()  # --print 什么都不写


def test_uninstall_removes_slice_even_without_tmux_blocks(
    cfg_path: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    units = tmp_path / "units"
    monkeypatch.setattr(guard, "unit_dir", lambda: units)
    monkeypatch.setattr(guard, "_daemon_reload", lambda: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    guard.install("atm-ai.slice", units, total_bytes=8 << 30)
    assert (units / "atm-ai.slice").exists()

    assert cli.main(["uninstall", "-y"]) == cli.EXIT_OK
    assert not (units / "atm-ai.slice").exists()
    assert "已删总量闸门" in capsys.readouterr().out


def test_index_json_includes_cache_written(monkeypatch, capsys) -> None:
    from atm import index as index_mod
    from atm.model import IndexStats, SessionIndex

    stats = IndexStats(
        total=0, from_cache=0, parsed=0, skipped=0, elapsed_ms=1, cache_written=False
    )
    monkeypatch.setattr(index_mod, "build", lambda use_cache=True: SessionIndex((), stats))
    assert cli._cmd_index(argparse.Namespace(rebuild=False, json=True)) == cli.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["cacheWritten"] is False
