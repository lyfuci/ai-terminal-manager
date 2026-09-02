"""`atm install` —— 这是唯一会改用户全局配置的代码路径，所以幂等 / 可卸载 / 不误伤要有测试。"""

from __future__ import annotations

from pathlib import Path

from atm import install as install_mod
from atm.install import MARKER_BEGIN, MARKER_END


def _plan(conf: Path, **kwargs):
    return install_mod.build_plan(conf_path=conf, **kwargs)


def test_writes_block_into_new_conf(tmp_path: Path) -> None:
    conf = tmp_path / ".tmux.conf"
    result = install_mod.apply(_plan(conf), live=False)

    text = conf.read_text(encoding="utf-8")
    assert MARKER_BEGIN in text
    assert MARKER_END in text
    assert "display-popup" in text
    assert result.backup_path is None  # 原本不存在，没什么好备份的


def test_preserves_existing_user_config(tmp_path: Path) -> None:
    """用户自己的配置一个字都不能动。"""
    conf = tmp_path / ".tmux.conf"
    conf.write_text("set -g mouse on\nbind-key R source-file ~/.tmux.conf\n", encoding="utf-8")

    install_mod.apply(_plan(conf), live=False)

    text = conf.read_text(encoding="utf-8")
    assert "set -g mouse on" in text
    assert "bind-key R source-file ~/.tmux.conf" in text
    assert MARKER_BEGIN in text


def test_backs_up_before_modifying(tmp_path: Path) -> None:
    conf = tmp_path / ".tmux.conf"
    conf.write_text("set -g mouse on\n", encoding="utf-8")

    result = install_mod.apply(_plan(conf), live=False)

    assert result.backup_path is not None
    assert result.backup_path.read_text(encoding="utf-8") == "set -g mouse on\n"


def test_install_is_idempotent(tmp_path: Path) -> None:
    """装两次不能出现两个块。"""
    conf = tmp_path / ".tmux.conf"
    install_mod.apply(_plan(conf), live=False)
    install_mod.apply(_plan(conf), live=False)

    text = conf.read_text(encoding="utf-8")
    assert text.count(MARKER_BEGIN) == 1
    assert text.count(MARKER_END) == 1


def test_reinstall_replaces_old_bindings(tmp_path: Path) -> None:
    """换键位重装，旧绑定要消失而不是并存。"""
    conf = tmp_path / ".tmux.conf"
    install_mod.apply(_plan(conf, key="a"), live=False)
    install_mod.apply(_plan(conf, key="s"), live=False)

    text = conf.read_text(encoding="utf-8")
    assert "bind-key s display-popup" in text
    assert "bind-key a display-popup" not in text


def test_plan_reports_already_installed(tmp_path: Path) -> None:
    conf = tmp_path / ".tmux.conf"
    assert _plan(conf).already_installed is False
    install_mod.apply(_plan(conf), live=False)
    assert _plan(conf).already_installed is True


def test_uninstall_removes_only_our_block(tmp_path: Path) -> None:
    conf = tmp_path / ".tmux.conf"
    conf.write_text("set -g mouse on\n", encoding="utf-8")
    install_mod.apply(_plan(conf), live=False)

    removed, backup = install_mod.remove(conf)

    assert removed is True
    assert backup is not None
    text = conf.read_text(encoding="utf-8")
    assert "set -g mouse on" in text
    assert MARKER_BEGIN not in text
    assert "display-popup" not in text


def test_uninstall_when_not_installed(tmp_path: Path) -> None:
    conf = tmp_path / ".tmux.conf"
    conf.write_text("set -g mouse on\n", encoding="utf-8")
    removed, backup = install_mod.remove(conf)
    assert removed is False
    assert backup is None
    assert conf.read_text(encoding="utf-8") == "set -g mouse on\n"


def test_install_uninstall_roundtrip_restores_file(tmp_path: Path) -> None:
    """装了再卸，文件内容应该回到原样。"""
    conf = tmp_path / ".tmux.conf"
    original = "set -g mouse on\nset -g base-index 1\n"
    conf.write_text(original, encoding="utf-8")

    install_mod.apply(_plan(conf), live=False)
    install_mod.remove(conf)

    assert conf.read_text(encoding="utf-8") == original


def test_binding_quotes_command_safely() -> None:
    """命令里有空格（比如退回 `python -m atm`）时不能把配置行拆坏。"""
    binding = install_mod.Binding(key="a", command="/usr/bin/python3 -m atm pick", description="x")
    line = binding.conf_line()
    assert "'/usr/bin/python3 -m atm pick'" in line


def test_binding_tmux_args_are_argv_not_shell() -> None:
    binding = install_mod.Binding(key="a", command="atm pick", description="x")
    args = binding.tmux_args()
    assert args[0] == "bind-key"
    assert args[1] == "a"
    assert args[-1] == "atm pick"  # 作为单个 argv 元素传，不经过 shell


def test_upper_key_bound_for_here_variant(tmp_path: Path) -> None:
    plan = _plan(tmp_path / ".tmux.conf", key="a")
    keys = {b.key for b in plan.bindings}
    # 2026-09-02 起多了侧栏的 b / B（见 test_sidebar_bindings_use_run_shell_with_pane_id）
    assert keys == {"a", "A", "b", "B"}
    assert any("--here" in b.command for b in plan.bindings)


def test_unreadable_conf_refuses_instead_of_clobbering(tmp_path: Path) -> None:
    """存在但读不出来的 conf **必须中止**，绝不能当成空文件覆盖掉。

    之前 _read() 把 UnicodeDecodeError 也返回 ""，于是一个非 UTF-8 的 ~/.tmux.conf
    会被判成「空」→ 跳过备份 → 整份覆盖，用户全部配置无声消失。
    """
    import pytest

    conf = tmp_path / ".tmux.conf"
    original = b"\xff\xfe\x00set -g mouse on"
    conf.write_bytes(original)

    with pytest.raises(install_mod.ConfUnreadable):
        _plan(conf)
    assert conf.read_bytes() == original, "文件被动过了"


def test_missing_conf_is_not_an_error(tmp_path: Path) -> None:
    """不存在是正常情况（首次安装），要和「读不出来」区分开。"""
    plan = _plan(tmp_path / "nope.conf")
    assert plan.already_installed is False
    assert plan.conf_exists is False


def test_missing_end_marker_refuses_to_delete_rest(tmp_path: Path) -> None:
    """用户手删了 MARKER_END 之后，绝不能把标记之后的配置一起删掉。"""
    import pytest

    conf = tmp_path / ".tmux.conf"
    conf.write_text(
        f"{MARKER_BEGIN}\nbind-key a display-popup 'atm pick'\n"
        f"set -g mouse on\nset -g history-limit 50000\n",
        encoding="utf-8",
    )
    with pytest.raises(install_mod.ConfUnreadable):
        install_mod.remove(conf)
    assert "history-limit 50000" in conf.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 侧栏键位（2026-09-02）


def test_sidebar_bindings_use_run_shell_with_pane_id(tmp_path: Path) -> None:
    """侧栏开关走 run-shell -b，且必须把 #{pane_id} 传给 atm —— run-shell 里 $TMUX_PANE 不可靠。"""
    plan = _plan(tmp_path / ".tmux.conf")
    by_key = {b.key: b for b in plan.bindings}
    assert by_key["b"].kind is install_mod.BindingKind.SHELL
    assert "sidebar --toggle --pane '#{pane_id}'" in by_key["b"].command
    assert by_key["B"].kind is install_mod.BindingKind.SHELL
    assert "park '#{pane_id}'" in by_key["B"].command

    line = by_key["b"].conf_line()
    assert line.startswith("bind-key b run-shell -b ")
    assert "display-popup" not in line
    assert by_key["b"].tmux_args()[:4] == ["bind-key", "b", "run-shell", "-b"]


def test_popup_bindings_unchanged_by_sidebar(tmp_path: Path) -> None:
    plan = _plan(tmp_path / ".tmux.conf")
    popup = [b for b in plan.bindings if b.kind is install_mod.BindingKind.POPUP]
    assert [b.key for b in popup] == ["a", "A"]
    assert all("display-popup -E" in b.conf_line() for b in popup)


def test_sidebar_key_validation(tmp_path: Path) -> None:
    import pytest

    conf = tmp_path / ".tmux.conf"
    with pytest.raises(ValueError, match="sidebar-key"):
        _plan(conf, sidebar_key="1")
    with pytest.raises(ValueError, match="不能相同"):
        _plan(conf, key="b", sidebar_key="b")
    assert {b.key for b in _plan(conf, sidebar_key="s").bindings} == {"a", "A", "s", "S"}
