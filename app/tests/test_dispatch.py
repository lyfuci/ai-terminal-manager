"""投递层 —— 这个项目唯一的差异点，也是唯一会「往用户终端里打字」的地方。

转义和「目标 pane 是否空闲」这两条必须有测试：
前者错了会执行到别的命令，后者错了会把命令打进正在跑的 claude 对话框里。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from atm import dispatch as dispatch_mod
from atm import tmux as tmux_mod
from atm.dispatch import DispatchError, DispatchTarget, TargetKind, resume_command
from atm.model import SessionEntry, Source
from atm.tmux import Pane, SplitDirection


# cwd 必须用**真实存在**的目录：dispatch 现在会检查它，
# 因为投递的是 `cd <cwd> && ...`，目录没了会让整条命令静默中止。
def make_entry(source: Source = Source.CLAUDE, cwd: str = "/tmp") -> SessionEntry:
    return SessionEntry(
        id="aaaa-bbbb",
        title="标题",
        source=source,
        cwd=cwd,
        git_branch="main",
        updated_at=datetime(2026, 8, 12, tzinfo=UTC),
        path="/tmp/x.jsonl",
        size_bytes=10,
    )


def make_pane(pane_id: str = "%1", command: str = "bash", in_mode: bool = False) -> Pane:
    return Pane(
        id=pane_id,
        session="main",
        window_index=0,
        window_name="win",
        pane_index=0,
        current_command=command,
        current_path="/home/sean",
        active=False,
        in_mode=in_mode,
        window_active=True,
        width=80,
        height=24,
        title="t",
    )


# ---------------------------------------------------------------- 命令构造


def test_resume_command_for_claude() -> None:
    command = resume_command(make_entry(Source.CLAUDE))
    assert command.program == "claude"
    assert command.argv == ("--resume", "aaaa-bbbb")
    assert command.shell_line() == "cd /tmp && claude --resume aaaa-bbbb"


def test_resume_command_for_codex() -> None:
    command = resume_command(make_entry(Source.CODEX))
    assert command.program == "codex"
    assert command.argv == ("resume", "aaaa-bbbb")
    assert command.shell_line() == "cd /tmp && codex resume aaaa-bbbb"


def test_shell_line_quotes_dangerous_cwd() -> None:
    """cwd 里有空格/引号/分号时必须转义 —— 否则会执行到别的命令。"""
    entry = make_entry(cwd="/tmp")
    entry = __import__("dataclasses").replace(entry, cwd="/home/sean/my project; rm -rf /tmp/x")
    line = resume_command(entry).shell_line()
    assert "'/home/sean/my project; rm -rf /tmp/x'" in line
    assert line.count("&&") == 1


def test_shell_line_quotes_session_id() -> None:
    entry = SessionEntry(
        id="evil; touch /tmp/pwned",
        title="t",
        source=Source.CLAUDE,
        cwd="/tmp",
        git_branch=None,
        updated_at=datetime(2026, 8, 12, tzinfo=UTC),
        path="/tmp/x.jsonl",
        size_bytes=1,
    )
    line = resume_command(entry).shell_line()
    assert "'evil; touch /tmp/pwned'" in line


# ---------------------------------------------------------------- 忙碌检测


def test_idle_shell_is_safe_target() -> None:
    assert dispatch_mod.is_safe_target(make_pane(command="zsh")) is True


def test_running_agent_is_not_safe_target() -> None:
    """pane 里跑着 claude 时投进去会变成对话内容，必须挡下来。"""
    assert dispatch_mod.is_safe_target(make_pane(command="claude")) is False
    assert dispatch_mod.is_safe_target(make_pane(command="vim")) is False


# ---------------------------------------------------------------- 投递


def test_print_target_never_touches_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("print 模式不该调 tmux")

    monkeypatch.setattr(tmux_mod, "run", boom)
    result = dispatch_mod.dispatch(make_entry(), DispatchTarget.print_only())
    assert result.pane_id is None
    assert result.command.shell_line().startswith("cd /tmp &&")


def test_dispatch_to_existing_idle_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(tmux_mod, "has_server", lambda: True)
    monkeypatch.setattr(tmux_mod, "list_panes", lambda: (make_pane("%2", "bash"),))
    monkeypatch.setattr(tmux_mod, "send_line", lambda pane, text, **kw: sent.append((pane, text)))
    monkeypatch.setattr(tmux_mod, "select_pane", lambda pane: None)

    result = dispatch_mod.dispatch(make_entry(), DispatchTarget.existing("%2"))

    assert result.pane_id == "%2"
    assert result.created_pane is False
    assert sent == [("%2", "cd /tmp && claude --resume aaaa-bbbb")]


def test_dispatch_refuses_busy_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmux_mod, "has_server", lambda: True)
    monkeypatch.setattr(tmux_mod, "list_panes", lambda: (make_pane("%2", "claude"),))
    monkeypatch.setattr(tmux_mod, "send_line", lambda *a, **k: pytest.fail("不该往忙碌 pane 投递"))

    with pytest.raises(DispatchError, match="claude"):
        dispatch_mod.dispatch(make_entry(), DispatchTarget.existing("%2"))


def test_force_overrides_busy_check(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(tmux_mod, "has_server", lambda: True)
    monkeypatch.setattr(tmux_mod, "list_panes", lambda: (make_pane("%2", "claude"),))
    monkeypatch.setattr(tmux_mod, "send_line", lambda pane, text, **kw: sent.append(pane))
    monkeypatch.setattr(tmux_mod, "select_pane", lambda pane: None)

    dispatch_mod.dispatch(make_entry(), DispatchTarget.existing("%2"), force=True)
    assert sent == ["%2"]


def test_dispatch_split_creates_pane_in_session_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict = {}
    monkeypatch.setattr(tmux_mod, "has_server", lambda: True)
    monkeypatch.setattr(
        tmux_mod,
        "split_window",
        lambda **kw: (calls.setdefault("split", kw) and "%9") or "%9",
    )
    monkeypatch.setattr(tmux_mod, "send_line", lambda *a, **k: None)
    monkeypatch.setattr(tmux_mod, "select_pane", lambda pane: None)

    result = dispatch_mod.dispatch(
        make_entry(), DispatchTarget.split("%1", SplitDirection.VERTICAL)
    )

    assert result.created_pane is True
    assert result.pane_id == "%9"
    assert calls["split"]["cwd"] == "/tmp"
    assert calls["split"]["direction"] is SplitDirection.VERTICAL


def test_dispatch_without_server_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmux_mod, "has_server", lambda: False)
    with pytest.raises(DispatchError, match="tmux server"):
        dispatch_mod.dispatch(make_entry(), DispatchTarget.existing("%1"))


def test_dispatch_unknown_pane_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmux_mod, "has_server", lambda: True)
    monkeypatch.setattr(tmux_mod, "list_panes", lambda: ())
    with pytest.raises(DispatchError, match="找不到 pane"):
        dispatch_mod.dispatch(make_entry(), DispatchTarget.existing("%99"))


def test_focus_failure_does_not_fail_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """从 popup 里调用时 switch-client 可能失败，但命令已经投出去了。"""
    monkeypatch.setattr(tmux_mod, "has_server", lambda: True)
    monkeypatch.setattr(tmux_mod, "list_panes", lambda: (make_pane("%2", "bash"),))
    monkeypatch.setattr(tmux_mod, "send_line", lambda *a, **k: None)

    def fail(pane):
        raise tmux_mod.TmuxError("no client")

    monkeypatch.setattr(tmux_mod, "select_pane", fail)
    result = dispatch_mod.dispatch(make_entry(), DispatchTarget.existing("%2"))
    assert result.pane_id == "%2"


def test_target_constructors() -> None:
    assert DispatchTarget.window().kind is TargetKind.WINDOW
    assert DispatchTarget.print_only().kind is TargetKind.PRINT
    assert DispatchTarget.split().direction is SplitDirection.HORIZONTAL


# ------------------------------------------------- 体积闸门（2026-08-12 事故回归）


def _sized_entry(mb: float) -> SessionEntry:
    return SessionEntry(
        id="big",
        title="t",
        source=Source.CLAUDE,
        cwd="/tmp",
        git_branch=None,
        updated_at=datetime(2026, 8, 12, tzinfo=UTC),
        path="/tmp/x.jsonl",
        size_bytes=int(mb * 1048576),
    )


def test_small_session_has_no_size_noise() -> None:
    assert dispatch_mod.size_risk(_sized_entry(0.1)) is dispatch_mod.SizeRisk.OK
    assert dispatch_mod.size_label(_sized_entry(0.1)) == ""


def test_medium_session_warns() -> None:
    assert dispatch_mod.size_risk(_sized_entry(25)) is dispatch_mod.SizeRisk.WARN
    assert "25MB" in dispatch_mod.size_label(_sized_entry(25))


def test_huge_session_blocked() -> None:
    assert dispatch_mod.size_risk(_sized_entry(649)) is dispatch_mod.SizeRisk.BLOCK


def test_huge_session_is_not_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """atm **不拦**大会话 —— Claude Code 自己有基于 token 的闸门，做得比按字节拍脑袋好。

    实测：resume 时 claude 会弹「909.6k tokens / Resume from summary (recommended)」，
    而 atm 投递的是交互式命令，不会绕过它。
    """
    sent: list[str] = []
    monkeypatch.setattr(tmux_mod, "has_server", lambda: True)
    monkeypatch.setattr(tmux_mod, "split_window", lambda **kw: "%7")
    monkeypatch.setattr(tmux_mod, "send_line", lambda pane, text, **kw: sent.append(pane))
    monkeypatch.setattr(tmux_mod, "select_pane", lambda pane: None)

    dispatch_mod.dispatch(_sized_entry(649), DispatchTarget.split())
    assert sent == ["%7"]


def test_print_mode_never_blocked() -> None:
    result = dispatch_mod.dispatch(_sized_entry(649), DispatchTarget.print_only())
    assert result.command.shell_line().startswith("cd /tmp &&")


def test_large_session_gets_a_notice_not_an_error() -> None:
    assert "摘要" in dispatch_mod.size_notice(_sized_entry(649))
    assert dispatch_mod.size_notice(_sized_entry(0.1)) == ""


def test_warn_sized_session_still_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    """WARN 档只是标注，不该挡路 —— 否则 4% 的会话变得不可用。"""
    sent: list[str] = []
    monkeypatch.setattr(tmux_mod, "has_server", lambda: True)
    monkeypatch.setattr(tmux_mod, "split_window", lambda **kw: "%5")
    monkeypatch.setattr(tmux_mod, "send_line", lambda pane, text, **kw: sent.append(pane))
    monkeypatch.setattr(tmux_mod, "select_pane", lambda pane: None)

    dispatch_mod.dispatch(_sized_entry(25), DispatchTarget.split())
    assert sent == ["%5"]


# ------------------------------------------------- 工作目录已删除（实测 16% 命中）


def test_dispatch_refuses_when_cwd_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """cwd 没了就不该投 —— `cd` 失败会让整条 && 链静默中止，claude 根本不启动。

    实测本机 210 条会话里 33 条（16%）是这种：/tmp 的 scratchpad 被清、
    git worktree 被删、项目目录移动过。
    """
    entry = make_entry(cwd="/tmp/atm-definitely-does-not-exist-12345")
    monkeypatch.setattr(tmux_mod, "has_server", lambda: True)
    monkeypatch.setattr(tmux_mod, "send_line", lambda *a, **k: pytest.fail("不该投递"))

    with pytest.raises(DispatchError, match="工作目录已经不存在"):
        dispatch_mod.dispatch(entry, DispatchTarget.split())


def test_print_mode_also_refuses_when_cwd_gone() -> None:
    """--print 的输出用户可能直接 eval，同样要拦。"""
    entry = make_entry(cwd="/tmp/atm-definitely-does-not-exist-12345")
    with pytest.raises(DispatchError, match="工作目录已经不存在"):
        dispatch_mod.dispatch(entry, DispatchTarget.print_only())


def test_cwd_missing_detection() -> None:
    assert dispatch_mod.cwd_missing(make_entry(cwd="/tmp/nope-atm-xyz")) is True
    assert dispatch_mod.cwd_missing(make_entry(cwd="/tmp")) is False


def test_missing_cwds_dedupes_by_directory() -> None:
    """按去重后的目录 stat，不是每条会话一次。"""
    entries = [
        make_entry(cwd="/tmp"),
        make_entry(cwd="/tmp"),
        make_entry(cwd="/tmp/nope-atm-xyz"),
        make_entry(cwd="/tmp/nope-atm-xyz"),
    ]
    missing = dispatch_mod.missing_cwds(entries)
    assert missing == frozenset({"/tmp/nope-atm-xyz"})


# ------------------------------------------------- cgroup 内存闸门


def test_memory_limit_wraps_command_with_systemd_run() -> None:
    cmd = dispatch_mod.resume_command(make_entry(), dispatch_mod.MemoryLimit())
    line = cmd.shell_line()
    assert "systemd-run --user --scope" in line
    assert "MemoryHigh=2G" in line
    assert "MemoryMax=4G" in line
    assert line.endswith("claude --resume aaaa-bbbb")


def test_no_memory_limit_leaves_command_bare() -> None:
    line = dispatch_mod.resume_command(make_entry(), None).shell_line()
    assert "systemd-run" not in line
    assert line == "cd /tmp && claude --resume aaaa-bbbb"


def test_memory_limit_is_configurable() -> None:
    limit = dispatch_mod.MemoryLimit(high="512M", max="1G", swap_max="0")
    line = dispatch_mod.resume_command(make_entry(), limit).shell_line()
    assert "MemoryHigh=512M" in line
    assert "MemoryMax=1G" in line
    assert "MemorySwapMax=0" in line


def test_high_is_lower_than_max_by_default() -> None:
    """High 是软限（节流），Max 是硬限（杀）。High 必须更低，否则软限没意义。

    实测依据：会话内存峰值大多是瞬时尖峰（peak→current 回落 60~75%），
    所以要先给它机会被回收压回去，而不是一超就杀。
    """

    def to_bytes(v: str) -> int:
        return int(v[:-1]) * {"M": 1024**2, "G": 1024**3}[v[-1]]

    assert to_bytes(dispatch_mod.DEFAULT_MEMORY_HIGH) < to_bytes(dispatch_mod.DEFAULT_MEMORY_MAX)


def test_description_uses_session_name_when_present() -> None:
    """scope 的 description 要能认出是哪条会话（systemctl --user list-units 里看得见）。"""
    from dataclasses import replace

    named = replace(make_entry(), name="wsl")
    cmd = dispatch_mod.resume_command(named, dispatch_mod.MemoryLimit())
    assert "atm: wsl" in cmd.shell_line()


def test_dangerous_description_is_quoted() -> None:
    """会话名/标题是用户内容，进 systemd-run 参数前必须转义。"""
    from dataclasses import replace

    evil = replace(make_entry(), name="x'; rm -rf /tmp/y; echo '")
    line = dispatch_mod.resume_command(evil, dispatch_mod.MemoryLimit()).shell_line()
    assert "rm -rf /tmp/y" in line  # 作为数据出现
    assert line.count("&&") == 1  # 但没有变成第二条命令
    assert ";" not in line.split("&&")[0]  # cd 那半截干净


def test_copy_mode_pane_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """滚进 copy-mode 的 pane 前台仍是 bash，但 send-keys 会被当导航键吃掉。"""
    monkeypatch.setattr(tmux_mod, "has_server", lambda: True)
    monkeypatch.setattr(tmux_mod, "list_panes", lambda: (make_pane("%2", "bash", in_mode=True),))
    monkeypatch.setattr(tmux_mod, "send_line", lambda *a, **k: pytest.fail("不该投进 copy-mode"))

    with pytest.raises(DispatchError):
        dispatch_mod.dispatch(make_entry(), DispatchTarget.existing("%2"))
