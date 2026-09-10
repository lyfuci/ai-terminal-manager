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
        current_path="/home/user",
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


def test_resume_command_for_pi() -> None:
    """上游文档 sessions.md：`--session <path|id>` 接完整路径或**部分** session id。

    传完整 id 而不是路径，和另外两家保持一致（路径里有空格照样能过 shlex，
    但 id 更短、也是 pi 自己 `-r` 列表里显示的东西）。
    """
    command = resume_command(make_entry(Source.PI))
    assert command.program == "pi"
    assert command.argv == ("--session", "aaaa-bbbb")
    assert command.shell_line() == "cd /tmp && pi --session aaaa-bbbb"


def test_every_source_has_a_resume_command() -> None:
    """加了新 Source 却忘了在 RESUME_PROGRAMS 里登记 —— 只会在用户点下去那一刻炸。"""
    for source in Source:
        assert resume_command(make_entry(source)).program


def test_shell_line_quotes_dangerous_cwd() -> None:
    """cwd 里有空格/引号/分号时必须转义 —— 否则会执行到别的命令。"""
    entry = make_entry(cwd="/tmp")
    entry = __import__("dataclasses").replace(entry, cwd="/home/user/my project; rm -rf /tmp/x")
    line = resume_command(entry).shell_line()
    assert "'/home/user/my project; rm -rf /tmp/x'" in line
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
    assert "MemoryHigh=6G" in line  # MemoryLimit() 的字段默认值 = 读不到 meminfo 时的退路
    assert "MemoryMax=8G" in line
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


def test_sessions_join_the_aggregate_slice() -> None:
    """单进程闸门拦不住「一次开 4 个」—— 总量得靠共同的 slice。

    实测本机 app-tmux.slice 峰值 6.75GB / 总内存 7.8GB，就是这么冻死的。
    resurrect 恢复出来的会话靠 @resurrect-processes 的 `->` 映射进同一个池，
    两条路径必须落在**同一个 slice 名**上，否则总量限制形同虚设。
    """
    line = dispatch_mod.resume_command(make_entry(), dispatch_mod.MemoryLimit()).shell_line()
    assert "--slice=atm-ai.slice" in line
    assert dispatch_mod.DEFAULT_SLICE == "atm-ai.slice"


def test_slice_is_overridable() -> None:
    limit = dispatch_mod.MemoryLimit(slice_name="other.slice")
    assert "--slice=other.slice" in dispatch_mod.resume_command(make_entry(), limit).shell_line()


def _gib(v: str) -> int:
    return int(v[:-1]) * {"M": 1024**2, "G": 1024**3}[v[-1]]


@pytest.mark.parametrize("total_gib", [2, 4, 8, 16, 32, 49, 128])
def test_high_is_lower_than_max_at_every_machine_size(total_gib: int) -> None:
    """High 是软限（节流），Max 是硬限（杀）。High 必须更低，否则软限没意义。

    实测依据：会话内存峰值大多是瞬时尖峰（peak→current 回落 60~75%），
    所以要先给它机会被回收压回去，而不是一超就杀。auto 之后这条要在每种机器尺寸上都成立。
    """
    high, max_ = dispatch_mod.suggested_session_limits(total_gib << 30)
    assert _gib(high) < _gib(max_)


@pytest.mark.parametrize("total_gib", [1, 2, 4, 8])
def test_small_machines_get_the_floor_not_a_throttle(total_gib: int) -> None:
    """2026-09-10 的 bug：软上限低于正常工作集 = 永久限流。

    小机器上按比例算会得出 1G 之类的数，那正是当年 High=2G 的翻版。下限必须兜住。
    """
    _high, max_ = dispatch_mod.suggested_session_limits(total_gib << 30)
    assert _gib(max_) >= dispatch_mod.SESSION_MIN_MAX_GIB << 30


def test_auto_scales_with_ram_and_stays_under_the_slice() -> None:
    """单会话闸门是「挑替死鬼」，总量归 slice —— 所以单会话 Max 必须明显小于 slice 软限。"""
    from atm import guard

    total = 48 << 30
    _high, max_ = dispatch_mod.suggested_session_limits(total)
    slice_high, _slice_max = guard.suggested_totals(total)
    assert _gib(max_) < _gib(slice_high)


def test_meminfo_that_cannot_be_read_is_none(tmp_path) -> None:
    assert dispatch_mod.total_memory_bytes(tmp_path / "nope") is None
    (tmp_path / "junk").write_text("garbage\n")
    assert dispatch_mod.total_memory_bytes(tmp_path / "junk") is None


def test_auto_falls_back_loose_when_meminfo_is_unreadable(monkeypatch) -> None:
    """读不到内存就宁松不紧：松了还有 slice 兜总量，紧了就是那个永久限流的 bug。"""
    monkeypatch.setattr(dispatch_mod, "total_memory_bytes", lambda *a, **kw: None)
    high, max_ = dispatch_mod.resolve_session_limits("auto", "auto")
    assert (high, max_) == (dispatch_mod.FALLBACK_MEMORY_HIGH, dispatch_mod.FALLBACK_MEMORY_MAX)


def test_pinned_values_are_left_alone() -> None:
    assert dispatch_mod.resolve_session_limits("1G", "2G", total_bytes=48 << 30) == ("1G", "2G")
    # 两个可以独立设：只 auto 一个
    high, max_ = dispatch_mod.resolve_session_limits("1G", "auto", total_bytes=48 << 30)
    assert high == "1G" and max_ != "auto"


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


# ---------------------------------------------------- focus=False 不能抢视图（2026-09-02）


def test_unfocused_window_dispatch_creates_window_detached(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """侧栏从历史恢复：新窗口必须 -d 开。实机踩过：不带 -d 用户的视图被带到新窗口，
    swap 之后那里装的是旧主格，看起来像弹出了一个全新会话。"""
    from atm import tmux

    calls: list[list[str]] = []

    def run(args, **kw):
        calls.append(args)
        return "%42\n" if args[0] == "new-window" else ""

    monkeypatch.setattr(tmux, "run", run)
    monkeypatch.setattr(tmux, "has_server", lambda: True)
    entry = make_entry(cwd=str(tmp_path))
    result = dispatch_mod.dispatch(entry, DispatchTarget.window(), focus=False)
    assert result.pane_id == "%42"
    new_window = next(c for c in calls if c[0] == "new-window")
    assert "-d" in new_window
    assert not any(c[0] in ("select-pane", "switch-client") for c in calls)

    calls.clear()
    dispatch_mod.dispatch(entry, DispatchTarget.window(), focus=True)
    assert "-d" not in next(c for c in calls if c[0] == "new-window")


# ---------------------------------------------------------------- 正在被限流吗
#
# 2026-09-10：一个 pane 卡死，现场 memory.events 里 227 万次 high 事件摆在那儿，
# 而 atm doctor 只报「闸门是多少」，不报「有没有生效」。这几条守住那个洞。


def fake_cgroup(root, slice_name="atm-ai.slice", scopes=()):
    """按 systemd 真实的**嵌套**布局搭一棵假 cgroup 树。"""
    import os

    uid = os.getuid()
    directory = root / "user.slice" / f"user-{uid}.slice" / f"user@{uid}.service"
    for part in dispatch_mod.slice_path_parts(slice_name):
        directory = directory / part
    for name, current, high, events in scopes:
        d = directory / name
        d.mkdir(parents=True)
        (d / "memory.current").write_text(str(current))
        (d / "memory.high").write_text(high)
        (d / "memory.events").write_text(events)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def test_slice_name_dashes_are_a_cgroup_hierarchy() -> None:
    """systemd 用 `-` 表示 slice 层级。按扁平路径找，一个 scope 都看不到 —— 实测踩过。"""
    assert dispatch_mod.slice_path_parts("atm-ai.slice") == ("atm.slice", "atm-ai.slice")
    assert dispatch_mod.slice_path_parts("atm.slice") == ("atm.slice",)
    assert dispatch_mod.slice_path_parts("a-b-c.slice") == ("a.slice", "a-b.slice", "a-b-c.slice")
    parts = dispatch_mod.slice_cgroup_dir("atm-ai.slice").parts
    assert parts[-2:] == ("atm.slice", "atm-ai.slice")


def test_reads_current_high_and_event_counts(tmp_path) -> None:
    fake_cgroup(
        tmp_path,
        scopes=[("run-a.scope", 2761998336, str(2 << 30), "low 0\nhigh 2276338\nmax 0\noom 0\n")],
    )
    (scope,) = dispatch_mod.scope_pressure(root=tmp_path)
    assert scope.name == "run-a.scope"
    assert scope.current == 2761998336
    assert scope.high == 2 << 30
    assert scope.high_events == 2276338
    assert scope.throttled and scope.over_high  # 用量高于软上限 = 此刻正卡着


def test_a_healthy_scope_is_not_reported_as_throttled(tmp_path) -> None:
    fake_cgroup(tmp_path, scopes=[("run-b.scope", 300 << 20, str(4 << 30), "high 0\nmax 0\n")])
    (scope,) = dispatch_mod.scope_pressure(root=tmp_path)
    assert not scope.throttled and not scope.over_high


def test_past_throttling_is_reported_but_distinguished_from_now(tmp_path) -> None:
    """撞过又回落 ≠ 现在卡着。两种都要说，但不能混为一谈。"""
    fake_cgroup(tmp_path, scopes=[("run-c.scope", 300 << 20, str(2 << 30), "high 2712\nmax 0\n")])
    (scope,) = dispatch_mod.scope_pressure(root=tmp_path)
    assert scope.throttled and not scope.over_high


def test_an_unlimited_scope_can_never_be_throttled(tmp_path) -> None:
    """set-property MemoryHigh=infinity 之后 memory.high 读出来是 `max`。"""
    fake_cgroup(tmp_path, scopes=[("run-d.scope", 9 << 30, "max", "high 500\nmax 0\n")])
    (scope,) = dispatch_mod.scope_pressure(root=tmp_path)
    assert scope.high is None
    assert not scope.throttled and not scope.over_high


def test_missing_or_unreadable_cgroup_returns_nothing(tmp_path) -> None:
    """诊断代码绝不能自己抛 —— 它是用户查问题的最后一根绳子。"""
    assert dispatch_mod.scope_pressure(root=tmp_path / "nope") == ()
    directory = fake_cgroup(tmp_path)  # slice 在，但底下没有 scope
    assert dispatch_mod.scope_pressure(root=tmp_path) == ()
    broken = directory / "run-x.scope"
    broken.mkdir()
    (broken / "memory.current").write_text("不是数字")
    assert dispatch_mod.scope_pressure(root=tmp_path) == ()


def test_doctor_names_the_stuck_scope_and_how_to_release_it(tmp_path, monkeypatch, capsys) -> None:
    """卡死时用户需要的是「哪个 scope」和「怎么解开」，不是「闸门是 2G」。"""
    from atm import cli, config

    fake_cgroup(
        tmp_path,
        scopes=[("run-p207309-i208192.scope", 2761998336, str(2 << 30), "high 2276338\nmax 0\n")],
    )
    real = dispatch_mod.scope_pressure
    monkeypatch.setattr(dispatch_mod, "memory_limits_available", lambda: True)
    monkeypatch.setattr(config, "load", lambda *a, **k: config.Config())
    monkeypatch.setattr(
        dispatch_mod, "scope_pressure", lambda name, **kw: real(name, root=tmp_path)
    )

    cli._report_guard()

    out = capsys.readouterr().out
    assert "run-p207309-i208192.scope" in out
    assert "2276338" in out
    assert "set-property" in out and "MemoryHigh=infinity" in out


# ---------------------------------------------------------------- 总量那一层也会限流
#
# 2026-09-10 第二次踩：只报单个 scope 的话，5 个会话合计 4725M 撞着 slice 的 4096M
# 软上限时，doctor 会说「没有会话撞过软上限」—— 而实际上每一个都在被回收拖慢。
# 小内存机器上先撞的往往就是总量这一层。


def fake_slice(root, slice_name="atm-ai.slice", *, current, high, events, scopes=()):
    """带 slice 自身数值的假 cgroup 树。"""
    directory = fake_cgroup(root, slice_name, scopes)
    (directory / "memory.current").write_text(str(current))
    (directory / "memory.high").write_text(high)
    (directory / "memory.events").write_text(events)
    return directory


def test_reads_the_slice_itself_not_just_its_scopes(tmp_path) -> None:
    fake_slice(
        tmp_path,
        current=4725 << 20,
        high=str(4 << 30),
        events="high 91234\nmax 0\n",
        scopes=[("run-a.scope", 340 << 20, str(3 << 30), "high 0\nmax 0\n")],
    )
    total = dispatch_mod.slice_pressure(root=tmp_path)
    assert total is not None
    assert total.name == "atm-ai.slice"
    assert total.throttled and total.over_high  # 4725M > 4096M


def test_slice_throttling_is_reported_even_when_no_single_scope_is(
    tmp_path, monkeypatch, capsys
) -> None:
    """现场原话：五个 scope 各自都在软上限以下，合计却超了 —— 这才是当时的真相。"""
    from atm import cli, config

    fake_slice(
        tmp_path,
        current=4725 << 20,
        high=str(4 << 30),
        events="high 91234\nmax 0\n",
        scopes=[
            ("run-a.scope", 340 << 20, str(3 << 30), "high 0\nmax 0\n"),
            ("run-b.scope", 1065 << 20, str(3 << 30), "high 0\nmax 0\n"),
            ("run-c.scope", 2633 << 20, str(3 << 30), "high 0\nmax 0\n"),
        ],
    )
    real_slice, real_scopes = dispatch_mod.slice_pressure, dispatch_mod.scope_pressure
    monkeypatch.setattr(dispatch_mod, "memory_limits_available", lambda: True)
    monkeypatch.setattr(config, "load", lambda *a, **k: config.Config())
    monkeypatch.setattr(
        dispatch_mod, "slice_pressure", lambda name, **kw: real_slice(name, root=tmp_path)
    )
    monkeypatch.setattr(
        dispatch_mod, "scope_pressure", lambda name, **kw: real_scopes(name, root=tmp_path)
    )

    cli._report_guard()

    out = capsys.readouterr().out
    assert "4725" in out and "91234" in out
    assert "没有会话撞过软上限" not in out  # 正是这句当时把人误导了
    assert "memory.slice-high" in out  # 给的是「少开几个」+ 怎么放宽，不是「调大就完了」


def test_a_slice_under_its_limit_says_nothing_extra(tmp_path) -> None:
    fake_slice(
        tmp_path,
        current=900 << 20,
        high=str(4 << 30),
        events="high 0\nmax 0\n",
        scopes=[("run-a.scope", 300 << 20, str(3 << 30), "high 0\nmax 0\n")],
    )
    total = dispatch_mod.slice_pressure(root=tmp_path)
    assert total is not None and not total.throttled


def test_missing_slice_pressure_is_none_not_a_crash(tmp_path) -> None:
    assert dispatch_mod.slice_pressure(root=tmp_path / "nope") is None
    directory = fake_cgroup(tmp_path)  # 目录在，但没有 memory.current
    assert directory.exists()
    assert dispatch_mod.slice_pressure(root=tmp_path) is None
