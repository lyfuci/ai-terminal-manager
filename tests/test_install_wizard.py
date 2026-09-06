"""裸 `atm install` 在终端里跑时的问答向导。input() 用脚本喂，不碰真终端。"""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from atm import cli, guard, install


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    conf = tmp_path / "tmux.conf"
    conf.write_text("set -g mouse on\n", encoding="utf-8")
    monkeypatch.setattr(guard, "unit_dir", lambda: tmp_path / "units")
    monkeypatch.setattr(guard, "_daemon_reload", lambda: None)
    monkeypatch.setenv("ATM_CONFIG", str(tmp_path / "c.toml"))
    monkeypatch.setattr(cli.tmux, "has_server", lambda: False)
    monkeypatch.setattr(cli.tmux, "is_installed", lambda: True)
    # 不直接补 sys.stdout.isatty：pytest 的 capsys 会换掉 sys.stdout，补丁跟着丢
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)  # 给最后的 _confirm 用
    return conf


def _script(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> list[str]:
    """把 input() 换成按顺序吐答案；返回被问过的提示，测试可以数问了几次。"""
    prompts: list[str] = []
    it = iter(answers)

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        try:
            return next(it)
        except StopIteration as exc:  # 问多了 = 逻辑错
            raise AssertionError(f"多问了一次：{prompt!r}") from exc

    monkeypatch.setattr(builtins, "input", fake_input)
    return prompts


def test_enter_everywhere_uses_defaults(env: Path, monkeypatch, capsys) -> None:
    prompts = _script(monkeypatch, ["", "", "n", "n", "y"])
    assert cli.main(["install", "--conf", str(env)]) == cli.EXIT_OK
    text = env.read_text(encoding="utf-8")
    assert f"bind-key {install.DEFAULT_KEY} " in text
    assert f"bind-key {install.DEFAULT_SIDEBAR_KEY} " in text
    assert "resurrect" not in text  # 答了 n
    assert len(prompts) == 5  # 4 个问题 + 最后的「继续吗？」
    assert "向导" in capsys.readouterr().out


def test_invalid_and_duplicate_keys_are_reasked(env: Path, monkeypatch, capsys) -> None:
    _script(monkeypatch, ["1", "s", "s", "t", "n", "n", "y"])
    assert cli.main(["install", "--conf", str(env)]) == cli.EXIT_OK
    text = env.read_text(encoding="utf-8")
    assert "bind-key s " in text and "bind-key t " in text
    out = capsys.readouterr().out
    assert "单个小写字母" in out and "相同" in out


def test_yes_to_persist_writes_plugin_block(env: Path, monkeypatch) -> None:
    from atm import persist

    applied: list[object] = []
    monkeypatch.setattr(cli, "_apply_persist", lambda plan: applied.append(plan))

    class Plan:
        def describe(self) -> str:
            return "persist-plan"

    monkeypatch.setattr(persist, "build_plan", lambda **kw: Plan())
    _script(monkeypatch, ["", "", "", "n", "y"])
    assert cli.main(["install", "--conf", str(env)]) == cli.EXIT_OK
    assert len(applied) == 1


def test_ctrl_c_in_wizard_changes_nothing(env: Path, monkeypatch, capsys) -> None:
    before = env.read_text(encoding="utf-8")

    def boom(prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(builtins, "input", boom)
    assert cli.main(["install", "--conf", str(env)]) == cli.EXIT_CANCELLED
    assert env.read_text(encoding="utf-8") == before
    assert "已取消" in capsys.readouterr().out


def test_any_option_skips_wizard(env: Path, monkeypatch) -> None:
    prompts = _script(monkeypatch, ["y"])
    assert cli.main(["install", "--key", "s", "--conf", str(env)]) == cli.EXIT_OK
    assert len(prompts) == 1  # 只剩「继续吗？」
    text = env.read_text(encoding="utf-8")
    assert "bind-key s " in text and f"bind-key {install.DEFAULT_SIDEBAR_KEY} " in text


def test_non_tty_never_asks(env: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    prompts = _script(monkeypatch, [])
    assert cli.main(["install", "--conf", str(env)]) == cli.EXIT_CANCELLED
    assert prompts == []
    assert "非交互" in capsys.readouterr().out


def test_print_shows_defaults_without_asking(env: Path, monkeypatch, capsys) -> None:
    prompts = _script(monkeypatch, [])
    assert cli.main(["install", "--print", "--conf", str(env)]) == cli.EXIT_OK
    assert prompts == []
    assert f"bind-key {install.DEFAULT_KEY} " in capsys.readouterr().out
