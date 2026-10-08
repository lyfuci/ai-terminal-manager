"""Extended-key format: actual server capability, safe persistence, truthful readback.

3.5+ is mocked here; no real server or terminal input is exercised by pytest.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from atm import cli, config, config_tui, install, sync, tmux, tmuxopts


@pytest.fixture
def server(monkeypatch):
    values = {"version": "3.5", "extended-keys": "off", "extended-keys-format": "xterm"}
    calls = []
    monkeypatch.setattr(tmux, "has_server", lambda: True)

    def run(args, **kw):
        calls.append(list(args))
        if args == ["display-message", "-p", "#{version}"]:
            return values["version"] + "\n"
        if args[:2] == ["show-options", "-gv"]:
            value = values.get(args[2])
            if isinstance(value, Exception):
                raise value
            return value + "\n"
        if args[:2] == ["set-option", "-g"]:
            values[args[2]] = args[3]
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(tmux, "run", run)
    return values, calls


@pytest.fixture
def conf(tmp_path):
    path = tmp_path / "tmux.conf"
    path.write_text("# user\nset -g status off\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("value", ["", "xterm", "csi-u"])
def test_format_config_roundtrip_env_unset(tmp_path, monkeypatch, value):
    key = "tmux.extended-keys-format"
    assert config.Config().tmux_extended_keys_format == ""
    cfg = config.set_value(config.Config(), key, value)
    path = tmp_path / "config.toml"
    config.save(cfg, path)
    assert config.load_file(path).tmux_extended_keys_format == value
    monkeypatch.setenv("ATM_TMUX_EXTENDED_KEYS_FORMAT", "xterm")
    loaded, sources = config.load_with_sources(path)
    assert loaded.tmux_extended_keys_format == "xterm"
    assert sources[key] == "env ATM_TMUX_EXTENDED_KEYS_FORMAT"
    assert config.unset_value(cfg, key).tmux_extended_keys_format == ""


@pytest.mark.parametrize("value", ["always", "kitty", "CSI-U", "xterm; run-shell bad", True])
def test_format_enum_rejects_invalid(value):
    with pytest.raises(config.ConfigError):
        config.set_value(config.Config(), "tmux.extended-keys-format", value)


@pytest.mark.parametrize("version", ["3.5", "3.5a", "3.6", "4.0"])
def test_supported_server_format_render_apply_rebuild_remove(conf, server, version):
    values, calls = server
    values["version"] = version
    original = conf.read_text()
    cfg = config.Config(tmux_extended_keys=True, tmux_extended_keys_format="csi-u")
    plan = tmuxopts.build_plan(cfg, conf_path=conf)
    assert plan.block.splitlines()[1:-1] == [
        "set -g extended-keys on",
        "set -g extended-keys-format csi-u",
    ]
    result = tmuxopts.apply(plan)
    assert result.written and result.applied_live and result.live_error is None
    assert result.backup_path.read_text() == original
    assert values["extended-keys"] == "on" and values["extended-keys-format"] == "csi-u"
    rebuilt = tmuxopts.build_plan(cfg, conf_path=conf)
    assert rebuilt.is_noop
    assert tmuxopts.apply(rebuilt).backup_path is None
    calls.clear()
    disabled = tmuxopts.apply(tmuxopts.build_plan(config.Config(), conf_path=conf))
    assert disabled.disabled == ("extended-keys", "extended-keys-format")
    assert conf.read_text() == original and calls == []
    tmuxopts.apply(tmuxopts.build_plan(cfg, conf_path=conf))
    calls.clear()
    assert tmuxopts.remove(conf)[0]
    assert conf.read_text() == original and calls == []


@pytest.mark.parametrize("version", ["3.4", "3.2", "", "next-3.6", "unknown"])
def test_unsupported_unknown_version_refuses_before_backup(conf, server, version):
    values, calls = server
    values["version"] = version
    before = conf.read_text()
    with pytest.raises(config.ConfigError, match="extended-keys-format"):
        tmuxopts.build_plan(config.Config(tmux_extended_keys_format="csi-u"), conf_path=conf)
    assert conf.read_text() == before
    assert not list(conf.parent.glob("tmux.conf.bak*"))
    assert not any(c[0] == "set-option" for c in calls)


def test_capability_failure_unknown_even_with_new_version(conf, server):
    values, _calls = server
    values["extended-keys-format"] = tmux.TmuxError("timeout")
    status = tmux.key_options_status()
    assert status.format_support == "unknown" and "timeout" in status.reason
    with pytest.raises(config.ConfigError):
        tmuxopts.build_plan(config.Config(tmux_extended_keys_format="xterm"), conf_path=conf)


def test_no_server_refuses_nonempty_but_default_and_bool_still_work(conf, monkeypatch):
    monkeypatch.setattr(tmux, "has_server", lambda: False)
    assert tmux.key_options_status().format_support == "unknown"
    with pytest.raises(config.ConfigError):
        tmuxopts.build_plan(config.Config(tmux_extended_keys_format="csi-u"), conf_path=conf)
    assert tmuxopts.build_plan(config.Config(), conf_path=conf).block == ""
    result = tmuxopts.apply(
        tmuxopts.build_plan(config.Config(tmux_extended_keys=True), conf_path=conf)
    )
    assert result.written and not result.applied_live and result.live_error
    assert "extended-keys-format" not in conf.read_text()


def test_apply_reprobes_before_any_mutation(conf, server):
    values, calls = server
    plan = tmuxopts.build_plan(config.Config(tmux_extended_keys_format="csi-u"), conf_path=conf)
    values["version"] = "3.4"
    before = conf.read_text()
    with pytest.raises(config.ConfigError):
        tmuxopts.apply(plan, live=False)
    assert conf.read_text() == before and not list(conf.parent.glob("tmux.conf.bak*"))
    assert not any(c[0] == "set-option" for c in calls)


def test_failed_backup_never_sets_live(conf, server, monkeypatch):
    _values, calls = server
    plan = tmuxopts.build_plan(config.Config(tmux_extended_keys_format="csi-u"), conf_path=conf)
    before = conf.read_text()

    def fail(path):
        raise install.BackupFailed("backup failed")

    monkeypatch.setattr(tmuxopts, "_backup", fail)
    with pytest.raises(install.BackupFailed):
        tmuxopts.apply(plan)
    assert conf.read_text() == before and not any(c[0] == "set-option" for c in calls)


def test_outside_format_conflicts_preserved_with_current_line_numbers(conf, server):
    foreign = (
        "set -g extended-keys-format xterm\n"
        "%if 1\nset -g extended-keys-format xterm\n%endif\n"
        "set -go extended-keys-format xterm\n"
        "bind R { set -g extended-keys-format xterm }\n"
    )
    conf.write_text(foreign)
    cfg = config.Config(tmux_extended_keys_format="csi-u")
    plan = tmuxopts.build_plan(cfg, conf_path=conf)
    assert [c.certain for c in plan.conflicts] == [True, False, False, False]
    result = tmuxopts.apply(plan)
    assert conf.read_text().endswith(foreign)
    for c in result.conflicts:
        assert conf.read_text().splitlines()[c.line_no - 1].strip() == c.text
    released = tmuxopts.apply(tmuxopts.build_plan(config.Config(), conf_path=conf))
    assert len(released.released) == 4 and conf.read_text() == foreign


@pytest.mark.parametrize("failure", ["set", "read", "mismatch"])
def test_live_failure_preserves_saved_file_and_reports_unconfirmed(
    conf, server, monkeypatch, failure
):
    _values, _calls = server
    original_run = tmux.run
    wrote = False

    def run(args, **kw):
        nonlocal wrote
        if args[:2] == ["set-option", "-g"]:
            wrote = True
            if failure == "set" and args[2] == "extended-keys-format":
                raise tmux.TmuxError("set failed")
        if wrote and args == ["show-options", "-gv", "extended-keys-format"]:
            if failure == "read":
                raise tmux.TmuxError("read unknown")
            if failure == "mismatch":
                return "xterm\n"
        return original_run(args, **kw)

    monkeypatch.setattr(tmux, "run", run)
    cfg = config.Config(tmux_extended_keys=True, tmux_extended_keys_format="csi-u")
    notes = sync.apply_changes(config.Config(), cfg, conf_path=conf)
    assert any("已写进" in n for n in notes)
    assert any("失败" in n for n in notes)
    assert not any("已对运行中的 server 生效" in n for n in notes)
    assert "set -g extended-keys-format csi-u" in conf.read_text()


def test_cli_editor_preflight_preserves_both_files_on_refusal(conf, server, monkeypatch, capsys):
    values, _calls = server
    values["version"] = "3.4"
    config_path = conf.parent / "config.toml"
    monkeypatch.setenv("ATM_CONFIG", str(config_path))
    cfg = config.Config(keys_conf_path=str(conf))
    config.save(cfg)
    before = config_path.read_text(), conf.read_text()
    assert cli.main(["config", "tmux.extended-keys-format", "csi-u"]) == cli.EXIT_ERROR
    assert "extended-keys-format" in capsys.readouterr().err
    editor = config_tui.ConfigEditor(cfg, {})
    editor._cursor = list(config.KEYS).index("tmux.extended-keys-format")
    editor.handle_key("\n")
    for key in "csi-u":
        editor.handle_key(key)
    editor.handle_key("\n")
    assert editor.handle_key("s") == config_tui.Action.NONE
    assert editor.error and "extended-keys-format" in editor.error
    assert (config_path.read_text(), conf.read_text()) == before
    assert not list(conf.parent.glob("tmux.conf.bak*"))


def test_preflight_unrelated_edits_and_format_release_never_probe(server):
    _values, calls = server
    old = config.Config(tmux_extended_keys_format="csi-u")
    sync.validate_changes(old, replace(old, memory_high="4G"))
    sync.validate_changes(old, replace(old, tmux_extended_keys_format=""))
    assert calls == []


def test_install_refuses_before_keys_or_config_write(conf, server, monkeypatch, capsys):
    values, _calls = server
    values["version"] = "3.4"
    config_path = conf.parent / "config.toml"
    monkeypatch.setenv("ATM_CONFIG", str(config_path))
    config.save(config.Config(tmux_extended_keys_format="csi-u"))
    before = config_path.read_text(), conf.read_text()
    assert (
        cli.main(["install", "--conf", str(conf), "--no-persist", "--no-slice", "-y"])
        == cli.EXIT_ERROR
    )
    assert "extended-keys-format" in capsys.readouterr().err
    assert (config_path.read_text(), conf.read_text()) == before


def test_server_probe_never_uses_path_client_version(server):
    values, calls = server
    values["version"] = "3.4"
    status = tmux.key_options_status()
    assert status.server_version == "3.4" and status.format_support == "unsupported"
    assert ["-V"] not in calls


def test_doctor_status_unknown_is_not_hardware_success(server, monkeypatch, capsys):
    values, _calls = server
    values["extended-keys-format"] = tmux.TmuxError("unknown")
    status = tmux.key_options_status()
    assert status.extended_keys == "off" and status.extended_keys_format is None
    assert status.format_support == "unknown"
    cli._report_key_options(status.to_json())
    out = capsys.readouterr().out
    assert "未知" in out and "终端" in out


def test_format_editor_hint():
    assert "xterm" in config_tui.ConfigEditor.format_hint("tmux.extended-keys-format")


@pytest.mark.parametrize("failure", ["set", "read", "mismatch"])
def test_cli_format_partial_failure_exits_nonzero_but_preserves_both_saves(
    conf, server, monkeypatch, capsys, failure
):
    values, _calls = server
    config_path = conf.parent / "config.toml"
    monkeypatch.setenv("ATM_CONFIG", str(config_path))
    config.save(config.Config(keys_conf_path=str(conf)))
    original_run = tmux.run
    wrote = False

    def run(args, **kw):
        nonlocal wrote
        if args[:3] == ["set-option", "-g", "extended-keys-format"]:
            wrote = True
            if failure == "set":
                raise tmux.TmuxError("set failed")
        if wrote and args == ["show-options", "-gv", "extended-keys-format"]:
            if failure == "read":
                raise tmux.TmuxError("read unknown")
            if failure == "mismatch":
                return "xterm\n"
        return original_run(args, **kw)

    monkeypatch.setattr(tmux, "run", run)
    assert cli.main(["config", "tmux.extended-keys-format", "csi-u"]) == cli.EXIT_ERROR
    assert config.load_file().tmux_extended_keys_format == "csi-u"
    assert "set -g extended-keys-format csi-u" in conf.read_text()
    assert values["extended-keys-format"] == ("xterm" if failure == "set" else "csi-u")
    out = capsys.readouterr().out
    assert "已写进" in out and "失败" in out
    assert not any("已对运行中的 server 生效" in line for line in out.splitlines())


def test_editor_saved_format_partial_failure_has_nonzero_exit(conf, server, monkeypatch):
    monkeypatch.setenv("ATM_CONFIG", str(conf.parent / "config.toml"))
    cfg = config.Config(keys_conf_path=str(conf))
    editor = config_tui.ConfigEditor(cfg, {})
    editor._cursor = list(config.KEYS).index("tmux.extended-keys-format")
    editor.handle_key("\n")
    for key in "csi-u":
        editor.handle_key(key)
    editor.handle_key("\n")
    original_run = tmux.run

    def run(args, **kw):
        if args[:3] == ["set-option", "-g", "extended-keys-format"]:
            raise tmux.TmuxError("set failed")
        return original_run(args, **kw)

    monkeypatch.setattr(tmux, "run", run)
    assert editor.handle_key("s") == config_tui.Action.SAVED_AND_QUIT
    assert editor.exit_code == 1 and "已保存" in editor.status and "失败" in editor.status
    assert not editor.dirty


def test_cli_unrelated_saved_unsupported_format_does_not_probe(conf, server, monkeypatch):
    values, calls = server
    values["version"] = "3.4"
    monkeypatch.setenv("ATM_CONFIG", str(conf.parent / "config.toml"))
    config.save(config.Config(keys_conf_path=str(conf), tmux_extended_keys_format="csi-u"))
    assert cli.main(["config", "memory.high", "4G"]) == cli.EXIT_OK
    assert config.load_file().memory_high == "4G" and calls == []


@pytest.mark.parametrize("bad", ["", "kitty", "csi-u xterm"])
def test_option_probe_unrecognized_value_is_unknown(conf, server, bad):
    values, _calls = server
    values["extended-keys-format"] = bad
    assert tmux.key_options_status().format_support == "unknown"
    with pytest.raises(config.ConfigError):
        tmuxopts.build_plan(config.Config(tmux_extended_keys_format="xterm"), conf_path=conf)


def test_version_query_failure_is_unknown(conf, server, monkeypatch):
    original_run = tmux.run

    def run(args, **kw):
        if args[0] == "display-message":
            raise tmux.TmuxError("timeout")
        return original_run(args, **kw)

    monkeypatch.setattr(tmux, "run", run)
    status = tmux.key_options_status()
    assert status.server_version is None and status.format_support == "unknown"
    assert "timeout" in status.reason
    with pytest.raises(config.ConfigError):
        tmuxopts.build_plan(config.Config(tmux_extended_keys_format="csi-u"), conf_path=conf)


def test_format_reset_uses_original_conf_without_server_probe(conf, server, monkeypatch):
    _values, calls = server
    monkeypatch.setenv("ATM_CONFIG", str(conf.parent / "config.toml"))
    original = conf.read_text()
    cfg = config.Config(keys_conf_path=str(conf), tmux_extended_keys_format="csi-u")
    config.save(cfg)
    tmuxopts.apply(tmuxopts.build_plan(cfg, conf_path=conf))
    calls.clear()
    monkeypatch.setattr(tmux, "has_server", lambda: False)
    assert cli.main(["config", "--reset"]) == cli.EXIT_OK
    assert not config.config_path().exists()
    assert conf.read_text() == original and calls == []


def test_doctor_json_includes_actual_server_key_status(conf, server, monkeypatch, capsys):
    import json

    from atm import index
    from atm.model import IndexStats, SessionIndex

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: conf.parent))
    monkeypatch.setattr(index, "build", lambda: SessionIndex((), IndexStats(0, 0, 0, 0, 1)))
    expected = tmux.key_options_status().to_json()
    # Other doctor probes are outside this seam; don't query the mock as persistence.
    monkeypatch.setattr(tmux, "has_server", lambda: False)
    monkeypatch.setattr(
        tmux,
        "key_options_status",
        lambda: tmux.KeyOptionsStatus(
            expected["serverVersion"],
            expected["extendedKeys"],
            expected["extendedKeysFormat"],
            expected["formatSupport"],
            expected["reason"],
        ),
    )
    assert cli.main(["doctor", "--json"]) == cli.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["tmux"]["keyOptions"] == expected
    assert data["tmux"]["keyOptions"]["terminalKeysVerified"] is False


def test_install_format_partial_failure_exits_nonzero_without_rollback(
    conf, server, monkeypatch, capsys
):
    values, calls = server
    monkeypatch.setenv("ATM_CONFIG", str(conf.parent / "config.toml"))
    monkeypatch.setattr(install, "resolve_atm_command", lambda: "/fixture/atm")
    config.save(config.Config(tmux_extended_keys_format="csi-u"))
    original_run = tmux.run

    def run(args, **kw):
        if args[0] in ("bind-key", "run-shell") or args == [
            "set-option",
            "-gu",
            "@resurrect-hook-post-restore-all",
        ]:
            calls.append(list(args))
            return ""
        if args[:3] == ["set-option", "-g", "extended-keys-format"]:
            calls.append(list(args))
            raise tmux.TmuxError("set failed")
        return original_run(args, **kw)

    monkeypatch.setattr(tmux, "run", run)
    rc = cli.main(["install", "--conf", str(conf), "--no-persist", "--no-slice", "-y"])
    assert rc == cli.EXIT_ERROR
    assert config.load_file().keys_conf_path == str(conf)
    assert "set -g extended-keys-format csi-u" in conf.read_text()
    assert install.MARKER_BEGIN in conf.read_text()
    assert list(conf.parent.glob("tmux.conf.bak*"))
    assert values["extended-keys-format"] == "xterm"
    assert sum(c[:3] == ["set-option", "-g", "extended-keys-format"] for c in calls) == 1
    out = capsys.readouterr().out
    assert "已写 tmux 选项块" in out and "失败" in out and "set failed" in out


def test_missing_tmux_never_probes_or_writes_format(conf, monkeypatch):
    monkeypatch.setattr(tmux, "is_installed", lambda: False)

    def forbidden_run(args, **kw):
        raise AssertionError("no tmux command expected")

    monkeypatch.setattr(tmux, "run", forbidden_run)
    before = conf.read_text()
    assert tmux.key_options_status().format_support == "unknown"
    with pytest.raises(config.ConfigError):
        tmuxopts.build_plan(config.Config(tmux_extended_keys_format="xterm"), conf_path=conf)
    assert conf.read_text() == before and not list(conf.parent.glob("tmux.conf.bak*"))
