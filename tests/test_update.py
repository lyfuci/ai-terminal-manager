"""`atm update`：怎么装的就用什么升，认不出就不乱动。不真跑升级、不真访问网络。"""

from __future__ import annotations

import argparse
import json

import pytest

from atm import cli, update

GIT = json.dumps({"url": update.GIT_URL, "vcs_info": {"vcs": "git", "commit_id": "2521c6b9b7f0"}})
EDITABLE = json.dumps({"url": "file:///src/atm-repo", "dir_info": {"editable": True}})


@pytest.mark.parametrize(
    ("exe", "direct_url", "installer", "origin"),
    [
        ("/home/u/.local/share/uv/tools/ai-terminal-manager/bin/python3", GIT, "uv tool", "git"),
        ("/home/u/.local/share/uv/tools/ai-terminal-manager/bin/python3", None, "uv tool", "index"),
        ("/home/u/.local/pipx/venvs/ai-terminal-manager/bin/python", None, "pipx", "index"),
        ("/usr/bin/python3", GIT, "pip", "git"),
        ("/src/atm-repo/.venv/bin/python", EDITABLE, "pip", "editable"),
    ],
)
def test_detect_installer_and_origin(exe, direct_url, installer, origin) -> None:
    info = update.detect(executable=exe, direct_url=direct_url)
    assert info.installer.value == installer
    assert info.origin.value == origin


def test_detect_git_detail_is_short_commit() -> None:
    info = update.detect(executable="/x/uv/tools/a/bin/python", direct_url=GIT)
    assert info.detail == "2521c6b"


def test_detect_editable_detail_is_directory() -> None:
    info = update.detect(executable="/src/atm-repo/.venv/bin/python", direct_url=EDITABLE)
    assert info.detail == "/src/atm-repo"


def test_detect_unknown_when_nothing_matches(monkeypatch) -> None:
    monkeypatch.setattr(update, "_dist_installed", lambda: False)
    info = update.detect(executable="/usr/bin/python3", direct_url=None)
    assert info.installer is update.Installer.UNKNOWN
    assert info.origin is update.Origin.UNKNOWN


def test_upgrade_command_per_installer() -> None:
    uv = update.detect(executable="/x/uv/tools/a/bin/python", direct_url=None)
    assert update.upgrade_command(uv) == ["uv", "tool", "upgrade", update.DIST_NAME]

    pipx = update.detect(executable="/x/pipx/venvs/a/bin/python", direct_url=None)
    assert update.upgrade_command(pipx) == ["pipx", "upgrade", update.DIST_NAME]

    pip_git = update.detect(executable="/usr/bin/python3", direct_url=GIT)
    cmd = update.upgrade_command(pip_git)
    assert cmd is not None and cmd[-1] == f"git+{update.GIT_URL}" and "--upgrade" in cmd


def test_editable_and_unknown_get_no_command(monkeypatch) -> None:
    editable = update.detect(executable="/x/.venv/bin/python", direct_url=EDITABLE)
    assert update.upgrade_command(editable) is None
    assert "git pull" in update.manual_hint(editable)

    monkeypatch.setattr(update, "_dist_installed", lambda: False)
    unknown = update.detect(executable="/usr/bin/python3", direct_url=None)
    assert update.upgrade_command(unknown) is None
    assert "uv tool upgrade" in update.manual_hint(unknown)


def test_version_tuple_ordering() -> None:
    assert update.version_tuple("0.2.0") < update.version_tuple("0.3.0")
    assert update.version_tuple("0.10.0") > update.version_tuple("0.9.9")
    assert update.version_tuple("1.0.0rc1") == (1, 0, 0)  # 够用就行


def test_latest_version_swallows_network_errors(monkeypatch) -> None:
    import urllib.request

    def boom(*a, **k):
        raise TimeoutError

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert update.latest_version() is None


def test_latest_version_parses_pypi_json(monkeypatch) -> None:
    import io
    import urllib.request

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: Resp(b'{"info": {"version": "9.9.9"}}')
    )
    assert update.latest_version() == "9.9.9"


def test_cmd_update_check_does_not_run_anything(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        update, "detect", lambda: update.InstallInfo(update.Installer.UV_TOOL, update.Origin.INDEX)
    )
    monkeypatch.setattr(update, "latest_version", lambda: "9.9.9")
    ran = []
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a))
    assert cli._cmd_update(argparse.Namespace(check=True, yes=False)) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "有新版本" in out and "uv tool upgrade" in out
    assert ran == []


def test_cmd_update_runs_command_with_yes(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        update, "detect", lambda: update.InstallInfo(update.Installer.PIPX, update.Origin.INDEX)
    )
    monkeypatch.setattr(update, "latest_version", lambda: None)
    import subprocess

    calls = []

    class R:
        returncode = 0
        stdout = "atm 9.9.9"

    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: (calls.append(cmd), R())[1])
    assert cli._cmd_update(argparse.Namespace(check=False, yes=True)) == cli.EXIT_OK
    assert calls[0] == ["pipx", "upgrade", update.DIST_NAME]
    assert "完成：atm 9.9.9" in capsys.readouterr().out


def test_cmd_update_editable_explains_and_exits_ok(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        update,
        "detect",
        lambda: update.InstallInfo(update.Installer.PIP, update.Origin.EDITABLE, "/src/repo"),
    )
    monkeypatch.setattr(update, "latest_version", lambda: None)
    assert cli._cmd_update(argparse.Namespace(check=False, yes=True)) == cli.EXIT_OK
    assert "git pull" in capsys.readouterr().out


def test_direct_index_command_only_for_index_installs() -> None:
    uv = update.InstallInfo(update.Installer.UV_TOOL, update.Origin.INDEX)
    cmd = update.direct_index_command(uv)
    assert cmd[:4] == ["uv", "tool", "install", "--reinstall"]
    assert cmd[4:] == ["--default-index", update.PYPI_SIMPLE, update.DIST_NAME]
    pipx = update.InstallInfo(update.Installer.PIPX, update.Origin.INDEX)
    assert "--index-url" in update.direct_index_command(pipx)
    git = update.InstallInfo(update.Installer.UV_TOOL, update.Origin.GIT, "abc1234")
    assert update.direct_index_command(git) is None
    assert (
        update.direct_index_command(
            update.InstallInfo(update.Installer.UNKNOWN, update.Origin.UNKNOWN)
        )
        is None
    )


def test_installed_version_from_output() -> None:
    assert update.installed_version_from("atm 0.6.0\n") == "0.6.0"
    assert update.installed_version_from("") is None


def test_cmd_update_falls_back_to_pypi_when_mirror_lags(monkeypatch, capsys) -> None:
    """镜像只有 0.4.0：uv tool upgrade 说 Nothing to upgrade → 直连 PyPI 再装一次。"""
    monkeypatch.setattr(
        update, "detect", lambda: update.InstallInfo(update.Installer.UV_TOOL, update.Origin.INDEX)
    )
    monkeypatch.setattr(update, "latest_version", lambda: "0.6.0")
    monkeypatch.setattr(update, "current_version", lambda: "0.4.0")
    import subprocess

    calls: list[list[str]] = []
    state = {"version": "atm 0.4.0"}

    class R:
        def __init__(self, stdout: str = "") -> None:
            self.returncode = 0
            self.stdout = stdout

    def fake_run(cmd, **k):
        calls.append(cmd)
        if cmd == ["atm", "--version"]:
            return R(state["version"])
        if "--default-index" in cmd:
            state["version"] = "atm 0.6.0"  # 直连 PyPI 那次才真的升上去
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cli._cmd_update(argparse.Namespace(check=False, yes=True)) == cli.EXIT_OK
    assert calls[0] == ["uv", "tool", "upgrade", update.DIST_NAME]
    assert any("--default-index" in c for c in calls)
    out = capsys.readouterr().out
    assert "镜像" in out and "完成：atm 0.6.0" in out


def test_cmd_update_no_fallback_when_upgrade_worked(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        update, "detect", lambda: update.InstallInfo(update.Installer.UV_TOOL, update.Origin.INDEX)
    )
    monkeypatch.setattr(update, "latest_version", lambda: "0.6.0")
    import subprocess

    calls: list[list[str]] = []

    class R:
        returncode = 0
        stdout = "atm 0.6.0"

    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: (calls.append(cmd), R())[1])
    assert cli._cmd_update(argparse.Namespace(check=False, yes=True)) == cli.EXIT_OK
    assert not any("--default-index" in c for c in calls)
    assert "镜像" not in capsys.readouterr().out


def test_version_flag() -> None:
    with pytest.raises(SystemExit) as exc:
        cli._build_parser().parse_args(["--version"])
    assert exc.value.code == 0
