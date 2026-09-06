"""第二轮审计回归：文件和外部命令都隔离到临时目录 / 替身。"""

from dataclasses import replace
from pathlib import Path

import pytest

from atm import cli, config, config_tui, dispatch, guard, install, sync, tmux, tmuxopts


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("ATM_CONFIG", str(tmp_path / "config" / "atm.toml"))
    for key in config.KEYS:
        monkeypatch.delenv(config.env_var_for(key), raising=False)
    monkeypatch.setattr(tmux, "has_server", lambda: False)
    monkeypatch.setattr(tmux, "is_installed", lambda: True)
    monkeypatch.setattr(dispatch, "memory_limits_available", lambda: False)
    monkeypatch.setattr(guard, "total_memory_bytes", lambda: 16 << 30)
    return tmp_path


@pytest.mark.parametrize("args", [["memory.max", "9G"], ["--unset", "memory.max"]])
def test_config_edit_preserves_file_under_env(isolated, monkeypatch, args):
    config.save(config.Config(memory_high="3G"))
    monkeypatch.setenv("ATM_MEMORY_HIGH", "7G")
    assert cli.main(["config", *args]) == cli.EXIT_OK
    monkeypatch.delenv("ATM_MEMORY_HIGH")
    assert config.load().memory_high == "3G"


@pytest.mark.parametrize("edit", ["text", "bool", "reset", "unrelated"])
def test_editor_preserves_env_markers_and_file_values(isolated, monkeypatch, edit):
    config.save(config.Config(memory_high="3G"))
    monkeypatch.setenv("ATM_MEMORY_HIGH", "7G")
    monkeypatch.setenv("ATM_MEMORY_ENABLED", "false")

    def interact(ed):
        key = "memory.enabled" if edit == "bool" else "memory.high"
        ed._cursor = list(config.KEYS).index(key)
        if edit == "reset":
            ed.handle_key("r")
        elif edit == "unrelated":
            ed._cursor = list(config.KEYS).index("memory.max")
        else:
            ed.handle_key("\n")
            if edit == "text":
                ed.handle_key("\x15")
                for ch in "5G":
                    ed.handle_key(ch)
                ed.handle_key("\n")
        row = ed._rows[list(config.KEYS).index(key)]
        assert ed.env_override(row) == config.env_var_for(key)
        assert ed.handle_key("s") == config_tui.Action.SAVED_AND_QUIT

    monkeypatch.setattr(config_tui.curses, "wrapper", lambda run: interact(run.__self__))
    config_tui.run_config_editor()
    monkeypatch.delenv("ATM_MEMORY_HIGH")
    monkeypatch.delenv("ATM_MEMORY_ENABLED")
    expected = {"text": "5G", "reset": config.Config().memory_high}.get(edit, "3G")
    assert config.load().memory_high == expected


def test_install_flags_do_not_save_env(isolated, monkeypatch):
    config.save(config.Config(memory_high="3G"))
    monkeypatch.setenv("ATM_MEMORY_HIGH", "7G")
    assert cli.main(["install", "-y", "--no-persist", "--no-slice", "--key", "s"]) == 0
    monkeypatch.delenv("ATM_MEMORY_HIGH")
    assert config.load().memory_high == "3G"


@pytest.mark.parametrize("module", [install, tmuxopts, pytest.param("persist", id="persist")])
@pytest.mark.parametrize("shape", ["trailing", "nested", "orphan-end"])
def test_marker_pairs_refuse_malformed_blocks(isolated, module, shape):
    from atm import persist

    module = persist if module == "persist" else module
    begin, end = module.MARKER_BEGIN, module.MARKER_END
    blocks = {
        "trailing": f"{begin}\nold\n{end}\n{begin}\nuser content\n",
        "nested": f"{begin}\n{begin}\n{end}\nuser content\n",
        "orphan-end": f"{end}\n{begin}\n{end}\nuser content\n",
    }
    conf = isolated / "tmux.conf"
    conf.write_text(blocks[shape])
    with pytest.raises(install.ConfUnreadable):
        module.remove(conf)
    assert conf.read_text() == blocks[shape]


def test_reset_reconciles_installed_artifacts(isolated, monkeypatch):
    old = config.Config(
        keys_pick="s", tmux_mouse=True, memory_slice_high="9G", memory_slice_max="12G"
    )
    config.save(old)
    monkeypatch.setattr(guard, "_daemon_reload", lambda: None)
    monkeypatch.setattr(dispatch, "memory_limits_available", lambda: True)
    install.apply(install.build_plan(cfg=old))
    tmuxopts.apply(tmuxopts.build_plan(old))
    guard.sync(old)
    monkeypatch.setenv("ATM_MEMORY_SLICE_HIGH", "20G")
    assert cli.main(["config", "--reset"]) == 0
    assert not config.config_path().exists()
    conf = (isolated / ".tmux.conf").read_text()
    assert "bind-key a " in conf and "bind-key s " not in conf
    assert tmuxopts.MARKER_BEGIN not in conf
    assert guard.status(old.memory_slice).high == guard.resolve_totals(config.Config())[0]


def test_install_records_conf_path_for_sync_and_reset(isolated):
    conf = isolated / 'custom "tmux".conf'
    assert cli.main(["install", "-y", "--no-persist", "--no-slice", "--conf", str(conf)]) == 0
    assert getattr(config.load(), "keys_conf_path", None) == str(conf)
    assert cli.main(["config", "keys.pick", "s"]) == 0
    assert "bind-key s " in conf.read_text()
    assert cli.main(["config", "tmux.mouse", "true"]) == 0
    assert tmuxopts.MARKER_BEGIN in conf.read_text()
    assert cli.main(["config", "--reset"]) == 0
    assert "bind-key a " in conf.read_text()
    assert tmuxopts.MARKER_BEGIN not in conf.read_text()
    assert not (isolated / ".tmux.conf").exists()


@pytest.mark.parametrize("failure", [None, "backup", "bind"])
def test_sync_rebinds_before_unbinding(isolated, monkeypatch, failure):
    conf = isolated / ".tmux.conf"
    old = config.Config()
    install.apply(install.build_plan(cfg=old))
    calls = []
    monkeypatch.setattr(tmux, "has_server", lambda: True)

    def run(args, **kw):
        calls.append(args)
        if failure == "bind" and args[0] == "bind-key":
            raise tmux.TmuxError("bind failed")
        return ""

    monkeypatch.setattr(tmux, "run", run)
    if failure == "backup":

        def backup(path):
            raise install.BackupFailed("backup failed")

        monkeypatch.setattr(install, "_backup", backup)
    notes = sync.apply_changes(old, replace(old, keys_pick="s"))
    if failure:
        assert not any(c[0] == "unbind-key" for c in calls)
        assert any("failed" in n for n in notes)
    else:
        assert calls.index(["unbind-key", "a"]) > max(
            i for i, c in enumerate(calls) if c[0] == "bind-key"
        )
        assert "bind-key s " in conf.read_text()


@pytest.mark.parametrize("failure", [False, True])
def test_install_replaces_actual_installed_keys(isolated, monkeypatch, failure):
    # 文件里的旧绑定才是已安装状态，不能依赖可能已改过的 config。
    install.apply(install.build_plan(key="x"))
    calls = []
    monkeypatch.setattr(tmux, "has_server", lambda: True)

    def run(args, **kw):
        calls.append(args)
        if failure and args[:2] == ["bind-key", "s"]:
            raise tmux.TmuxError("bind failed")
        return ""

    monkeypatch.setattr(tmux, "run", run)
    assert cli.main(["install", "-y", "--no-persist", "--no-slice", "--key", "s"]) == 0
    if failure:
        assert not any(c[0] == "unbind-key" for c in calls)
    else:
        assert ["unbind-key", "x"] in calls and ["unbind-key", "X"] in calls
        assert ["unbind-key", "b"] not in calls
        assert calls.index(["unbind-key", "x"]) > max(
            i for i, c in enumerate(calls) if c[0] == "bind-key"
        )


@pytest.mark.parametrize("mode", ["exit", "timeout", "oserror"])
@pytest.mark.parametrize("entry", ["install", "sync"])
def test_reload_failure_reports_written_file(isolated, monkeypatch, capsys, mode, entry):
    import subprocess

    monkeypatch.setattr(dispatch, "memory_limits_available", lambda: True)
    monkeypatch.setattr(guard.shutil, "which", lambda name: "/bin/systemctl")

    def run(args, **kw):
        assert args == ["systemctl", "--user", "daemon-reload"]
        if mode == "timeout":
            raise subprocess.TimeoutExpired(args, 15)
        if mode == "oserror":
            raise OSError("reload unavailable")
        return subprocess.CompletedProcess(args, 1, "", "reload denied")

    monkeypatch.setattr(guard.subprocess, "run", run)
    cfg = config.Config(memory_slice_high="7G", memory_slice_max="9G")
    if entry == "install":
        cli._apply_slice(cfg)
        notes = capsys.readouterr().out
    else:
        path = guard.unit_dir() / cfg.memory_slice
        path.parent.mkdir(parents=True)
        path.write_text(guard.unit_text(high="3G", max_="4G"))
        notes = " ".join(sync.apply_changes(config.Config(), cfg))
    assert guard.status(cfg.memory_slice).high == "7G"
    assert "文件已写入，重载失败：" in notes
    assert "已写总量闸门" not in notes and "总量闸门单元没写" not in notes


@pytest.mark.parametrize("entry", ["install", "sync"])
def test_no_systemctl_is_success_not_a_reload_failure(isolated, monkeypatch, capsys, entry):
    """容器 / macOS 上根本没有 manager 要重载，写完文件就算成功，不能报成失败。"""
    monkeypatch.setattr(dispatch, "memory_limits_available", lambda: True)
    monkeypatch.setattr(guard.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        guard.subprocess, "run", lambda *a, **k: pytest.fail("没有 systemctl 就不该去跑它")
    )
    cfg = config.Config(memory_slice_high="7G", memory_slice_max="9G")
    if entry == "install":
        cli._apply_slice(cfg)
        notes = capsys.readouterr().out
    else:
        path = guard.unit_dir() / cfg.memory_slice
        path.parent.mkdir(parents=True)
        path.write_text(guard.unit_text(high="3G", max_="4G"))
        notes = " ".join(sync.apply_changes(config.Config(), cfg))
    assert guard.status(cfg.memory_slice).high == "7G"
    assert "重载失败" not in notes


@pytest.mark.parametrize(
    "field,value",
    [
        ("tmux_mouse", True),
        ("tmux_focus_events", True),
        ("tmux_history_limit", 50000),
        ("tmux_base_index", 1),
        ("tmux_renumber_windows", True),
    ],
)
def test_disabling_tmux_option_preserves_live_user_value(isolated, monkeypatch, field, value):
    conf = isolated / ".tmux.conf"
    user = "set -g mouse on\nset -g history-limit 80000\nset -g base-index 3\n"
    conf.write_text(user)
    old = replace(config.Config(), **{field: value})
    tmuxopts.apply(tmuxopts.build_plan(old))
    calls = []
    monkeypatch.setattr(tmux, "run", lambda args: calls.append(args))
    monkeypatch.setattr(tmux, "has_server", lambda: True)
    plan = tmuxopts.build_plan(config.Config())
    assert "新 tmux server" in plan.describe()
    notes = sync.apply_changes(old, config.Config())
    assert calls == []
    assert conf.read_text() == user
    assert any("新 tmux server" in note for note in notes)
    assert not any("已对运行中的 server 生效" in note for note in notes)


@pytest.mark.parametrize("available", [False, True])
def test_system_manager_refuses_aggregate_install(isolated, monkeypatch, capsys, available):
    monkeypatch.setattr(dispatch, "memory_limits_available", lambda: available)
    monkeypatch.setattr(
        guard, "_daemon_reload", lambda: pytest.fail("system mode must not reload user manager")
    )
    cfg = config.Config(memory_user=False)
    assert "memory.user=false" in guard.describe_plan(cfg)
    cli._apply_slice(cfg)
    assert "memory.user=false" in capsys.readouterr().out
    assert not guard.unit_dir().exists()
    notes = sync.apply_changes(config.Config(), cfg)
    assert any("memory.user=false" in n for n in notes)
    with pytest.raises(RuntimeError, match=r"memory\.user=false"):
        guard.sync(cfg)
    assert not guard.unit_dir().exists()


def test_uninstall_uses_recorded_conf_path(isolated):
    conf = isolated / "custom.conf"
    assert cli.main(["install", "-y", "--no-persist", "--no-slice", "--conf", str(conf)]) == 0
    assert cli.main(["uninstall", "-y"]) == 0
    assert install.MARKER_BEGIN not in conf.read_text()


def test_options_noop_still_refuses_unpaired_second_block(isolated):
    cfg = config.Config(tmux_mouse=True)
    conf = isolated / ".tmux.conf"
    tmuxopts.apply(tmuxopts.build_plan(cfg))
    before = conf.read_text() + tmuxopts.MARKER_BEGIN + "\nuser content\n"
    conf.write_text(before)
    with pytest.raises(install.ConfUnreadable):
        tmuxopts.apply(tmuxopts.build_plan(cfg))
    assert conf.read_text() == before


# ---------------------------------------------------------------- 复查这轮修复引入的边角


def test_missing_systemctl_is_not_a_reload_failure(tmp_path: Path, monkeypatch) -> None:
    """没有 systemd 的机器（容器 / macOS）本来就没什么可重载，不能算失败。"""
    from atm import guard

    monkeypatch.setattr(guard.shutil, "which", lambda name: None)
    assert guard._daemon_reload() is None
    path, written = guard.install("atm-ai.slice", tmp_path, total_bytes=8 << 30)
    assert written and path.exists()  # 不抛 ReloadFailed
    assert guard.remove("atm-ai.slice", tmp_path) is True


def test_uninstall_reports_reload_failure_without_losing_the_summary(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """删完了但 daemon-reload 失败：仍要告诉用户删了哪些块，再附一句实情。"""
    from atm import cli, config, guard, install

    conf = tmp_path / "tmux.conf"
    conf.write_text("set -g status off\n", encoding="utf-8")
    monkeypatch.setenv("ATM_CONFIG", str(tmp_path / "c.toml"))
    monkeypatch.setattr(guard, "unit_dir", lambda: tmp_path / "units")
    monkeypatch.setattr(guard, "_daemon_reload", lambda: None)
    monkeypatch.setattr(cli.tmux, "has_server", lambda: False)
    monkeypatch.setattr(cli.tmux, "is_installed", lambda: True)
    install.apply(install.build_plan(cfg=config.Config(), conf_path=conf), live=False)
    guard.install("atm-ai.slice", tmp_path / "units", total_bytes=8 << 30)
    capsys.readouterr()

    monkeypatch.setattr(guard, "_daemon_reload", lambda: "Failed to connect to bus")
    assert cli.main(["uninstall", "-y", "--conf", str(conf)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "移除键位块" in out  # 汇总没被吞
    assert "已删总量闸门" in out
    assert "Failed to connect to bus" in out  # 实情也说了
    assert not (tmp_path / "units" / "atm-ai.slice").exists()
    assert conf.read_text(encoding="utf-8") == "set -g status off\n"
