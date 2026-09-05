"""`atm install` 的持久化块 —— 和键位块一样会改用户全局配置，同一套要求：幂等 / 可卸载 / 不误伤。

克隆一律注入假的 cloner，测试不碰网络。
"""

from __future__ import annotations

from pathlib import Path

from atm import install as install_mod
from atm import persist
from atm.persist import MARKER_BEGIN, MARKER_END, PLUGINS


def _plan(conf: Path, plugins: Path, **kwargs):
    return persist.build_plan(conf_path=conf, plugins_dir=plugins, **kwargs)


def _fake_cloner(calls: list[str]):
    def clone(name: str, dest: Path) -> None:
        calls.append(name)
        dest.mkdir(parents=True)

    return clone


def test_writes_block_and_clones_all_plugins(tmp_path: Path) -> None:
    conf = tmp_path / ".tmux.conf"
    plugins = tmp_path / "plugins"
    calls: list[str] = []

    result = persist.apply(_plan(conf, plugins), cloner=_fake_cloner(calls))

    text = conf.read_text(encoding="utf-8")
    assert MARKER_BEGIN in text and MARKER_END in text
    assert "@plugin 'tmux-plugins/tmux-resurrect'" in text
    assert "@continuum-restore 'on'" in text
    assert f"run '{plugins / 'tpm' / 'tpm'}'" in text
    assert result.block_written
    assert result.cloned == PLUGINS
    assert calls == list(PLUGINS)
    assert not result.clone_errors


def test_does_not_reclone_existing_plugins(tmp_path: Path) -> None:
    conf = tmp_path / ".tmux.conf"
    plugins = tmp_path / "plugins"
    (plugins / "tpm").mkdir(parents=True)
    (plugins / "tmux-resurrect").mkdir()
    calls: list[str] = []

    plan = _plan(conf, plugins)
    assert plan.missing_plugins == ("tmux-continuum",)
    result = persist.apply(plan, cloner=_fake_cloner(calls))

    assert calls == ["tmux-continuum"]
    assert result.cloned == ("tmux-continuum",)


def test_clone_failure_is_reported_not_raised(tmp_path: Path) -> None:
    """网络挂了配置照写 —— 用户事后 prefix + I 能补，但配置块不写就什么都没有。"""
    conf = tmp_path / ".tmux.conf"
    plugins = tmp_path / "plugins"

    def boom(name: str, dest: Path) -> None:
        raise RuntimeError("no network")

    result = persist.apply(_plan(conf, plugins), cloner=boom)

    assert result.block_written
    assert set(result.clone_errors) == set(PLUGINS)
    assert MARKER_BEGIN in conf.read_text(encoding="utf-8")


def test_never_restores_ai_cli_processes(tmp_path: Path) -> None:
    """notes/2026-08-12-incident.md 附三：开机批量拉起 claude 吃掉 87% 内存。别再犯。"""
    block = persist.build_block(tmp_path)
    assert "set -g @resurrect-processes" not in block


def test_install_is_idempotent(tmp_path: Path) -> None:
    conf = tmp_path / ".tmux.conf"
    plugins = tmp_path / "plugins"
    persist.apply(_plan(conf, plugins), cloner=_fake_cloner([]))
    persist.apply(_plan(conf, plugins), cloner=_fake_cloner([]))

    text = conf.read_text(encoding="utf-8")
    assert text.count(MARKER_BEGIN) == 1
    assert text.count("run '") == 1


def test_skips_when_user_already_manages_tpm(tmp_path: Path) -> None:
    """用户自己有 tpm 时不写块 —— 两份 `run tpm` 会重复初始化。也不替他克隆。"""
    conf = tmp_path / ".tmux.conf"
    conf.write_text(
        "set -g @plugin 'tmux-plugins/tpm'\nrun '~/.tmux/plugins/tpm/tpm'\n", encoding="utf-8"
    )
    plugins = tmp_path / "plugins"
    calls: list[str] = []

    plan = _plan(conf, plugins)
    assert plan.user_manages_tpm
    assert not plan.will_write_block
    result = persist.apply(plan, cloner=_fake_cloner(calls))

    assert not result.block_written
    assert calls == []
    assert MARKER_BEGIN not in conf.read_text(encoding="utf-8")


def test_commented_out_tpm_line_does_not_count(tmp_path: Path) -> None:
    conf = tmp_path / ".tmux.conf"
    conf.write_text("# run '~/.tmux/plugins/tpm/tpm'\nset -g mouse on\n", encoding="utf-8")
    assert not _plan(conf, tmp_path / "plugins").user_manages_tpm


def test_our_own_block_is_not_mistaken_for_user_tpm(tmp_path: Path) -> None:
    """重装时块里的 @plugin 行不能被当成「用户自己在管 tpm」。"""
    conf = tmp_path / ".tmux.conf"
    plugins = tmp_path / "plugins"
    persist.apply(_plan(conf, plugins), cloner=_fake_cloner([]))

    plan = _plan(conf, plugins)
    assert plan.already_installed
    assert not plan.user_manages_tpm


def test_coexists_with_binding_block_in_any_order(tmp_path: Path) -> None:
    """键位块和持久化块互不干扰：谁先谁后都行，各自卸载只删自己。"""
    conf = tmp_path / ".tmux.conf"
    conf.write_text("set -g mouse on\n", encoding="utf-8")
    plugins = tmp_path / "plugins"

    persist.apply(_plan(conf, plugins), cloner=_fake_cloner([]))
    install_mod.apply(install_mod.build_plan(conf_path=conf), live=False)

    text = conf.read_text(encoding="utf-8")
    assert text.index(MARKER_BEGIN) < text.index(install_mod.MARKER_BEGIN)

    removed, _ = persist.remove(conf)
    assert removed
    text = conf.read_text(encoding="utf-8")
    assert MARKER_BEGIN not in text
    assert install_mod.MARKER_BEGIN in text
    assert "set -g mouse on" in text


def test_remove_when_not_installed(tmp_path: Path) -> None:
    conf = tmp_path / ".tmux.conf"
    conf.write_text("set -g mouse on\n", encoding="utf-8")
    removed, backup = persist.remove(conf)
    assert not removed and backup is None
    assert conf.read_text(encoding="utf-8") == "set -g mouse on\n"


def test_backs_up_before_modifying(tmp_path: Path) -> None:
    conf = tmp_path / ".tmux.conf"
    conf.write_text("set -g mouse on\n", encoding="utf-8")
    result = persist.apply(_plan(conf, tmp_path / "plugins"), cloner=_fake_cloner([]))
    assert result.backup_path is not None
    assert result.backup_path.read_text(encoding="utf-8") == "set -g mouse on\n"


def test_tmux_install_hint_picks_package_manager(monkeypatch) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/dnf" if name == "dnf" else None)
    assert persist.tmux_install_hint().startswith("sudo dnf")

    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert "tmux" in persist.tmux_install_hint()


def test_status_without_server(tmp_path: Path, monkeypatch) -> None:
    from atm import tmux

    monkeypatch.setattr(tmux, "has_server", lambda: False)
    plugins = tmp_path / "plugins"
    (plugins / "tpm").mkdir(parents=True)

    st = persist.status(conf_path=tmp_path / ".tmux.conf", plugins_dir=plugins)

    assert not st.block_installed
    assert st.plugins_present == ("tpm",)
    assert st.plugins_missing == ("tmux-resurrect", "tmux-continuum")
    assert st.autosave_hooked is None


def test_status_detects_missing_autosave_hook(tmp_path: Path, monkeypatch) -> None:
    """README「已知的坑」第 4 条：唯一可靠判据是 status-right 里有没有 continuum_save.sh。"""
    from atm import tmux

    monkeypatch.setattr(tmux, "has_server", lambda: True)
    monkeypatch.setattr(tmux, "run", lambda args, **kw: '"#{host}" %H:%M')
    st = persist.status(conf_path=tmp_path / ".tmux.conf", plugins_dir=tmp_path / "plugins")
    assert st.autosave_hooked is False

    monkeypatch.setattr(tmux, "run", lambda args, **kw: "#(/x/continuum_save.sh) %H:%M")
    st = persist.status(conf_path=tmp_path / ".tmux.conf", plugins_dir=tmp_path / "plugins")
    assert st.autosave_hooked is True
