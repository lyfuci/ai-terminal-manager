"""config 改了 → sync.apply_changes 让 tmux / systemd 跟上。只测分派和文案。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from atm import config, dispatch, guard, install, sync, tmux, tmuxopts


@pytest.fixture
def conf(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "tmux.conf"
    p.write_text("set -g status off\n", encoding="utf-8")
    monkeypatch.setattr(tmux, "has_server", lambda: False)
    monkeypatch.setattr(dispatch, "memory_limits_available", lambda: False)
    return p


def test_no_change_no_action(conf: Path) -> None:
    cfg = config.Config()
    assert sync.apply_changes(cfg, cfg, conf_path=conf) == []
    assert conf.read_text(encoding="utf-8") == "set -g status off\n"


def test_tmux_change_writes_block(conf: Path) -> None:
    old = config.Config()
    new = replace(old, tmux_mouse=True)
    notes = sync.apply_changes(old, new, conf_path=conf)
    assert any("tmux 选项已写进" in n for n in notes)
    assert tmuxopts.MARKER_BEGIN in conf.read_text(encoding="utf-8")


def test_key_change_without_install_only_hints(conf: Path) -> None:
    old = config.Config()
    new = replace(old, keys_pick="s")
    notes = sync.apply_changes(old, new, conf_path=conf)
    assert len(notes) == 1 and "atm install" in notes[0]
    assert "bind-key" not in conf.read_text(encoding="utf-8")


def test_key_change_after_install_rebinds_and_unbinds_old(conf: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(tmux, "has_server", lambda: True)
    monkeypatch.setattr(tmux, "run", lambda args, **kw: calls.append(list(args)) or "")
    old = config.Config()
    install.apply(install.build_plan(cfg=old, conf_path=conf))
    calls.clear()
    new = replace(old, keys_pick="s")
    notes = sync.apply_changes(old, new, conf_path=conf)
    text = conf.read_text(encoding="utf-8")
    assert "bind-key s " in text and "bind-key a " not in text
    assert ["unbind-key", "a"] in calls and ["unbind-key", "A"] in calls
    assert not any(c == ["unbind-key", "b"] for c in calls)  # 侧栏键没变，不解
    assert any(c[:2] == ["bind-key", "s"] for c in calls)
    assert any("重绑" in n for n in notes)


def test_slice_change_without_cgroup_just_notes(conf: Path) -> None:
    old = config.Config()
    new = replace(old, memory_slice_high="10G")
    notes = sync.apply_changes(old, new, conf_path=conf)
    assert len(notes) == 1 and "cgroup" in notes[0]


def test_slice_change_updates_our_unit_but_not_users(
    conf: Path, tmp_path: Path, monkeypatch
) -> None:
    units = tmp_path / "units"
    monkeypatch.setattr(dispatch, "memory_limits_available", lambda: True)
    monkeypatch.setattr(guard, "unit_dir", lambda: units)
    monkeypatch.setattr(guard, "_daemon_reload", lambda: None)
    old = config.Config()
    new = replace(old, memory_slice_high="10G", memory_slice_max="12G")
    notes = sync.apply_changes(old, new, conf_path=conf)
    assert any("写入" in n and "MemoryHigh=10G" in n for n in notes)
    assert guard.status("atm-ai.slice", units).high == "10G"

    newer = replace(new, memory_slice_max="14G")
    notes = sync.apply_changes(new, newer, conf_path=conf)
    assert any("更新" in n and "MemoryMax=14G" in n for n in notes)

    (units / "atm-ai.slice").write_text("[Slice]\nMemoryHigh=1G\n", encoding="utf-8")  # 用户手改
    notes = sync.apply_changes(newer, replace(newer, memory_slice_high="2G"), conf_path=conf)
    assert any("你自己写的" in n for n in notes)
    assert guard.status("atm-ai.slice", units).high == "1G"
