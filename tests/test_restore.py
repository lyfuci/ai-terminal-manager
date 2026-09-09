"""`atm restore`：把上次的会话填回恢复出来的空格子。

最重要的一条不变量：**绝不覆盖正在跑东西的格子**。其余状态（格子没了、会话记录没了）
都必须报出来而不是安静跳过 —— 用户得知道为什么少恢复了几条。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from atm import restore
from atm.model import SessionEntry, Source
from atm.tmux import Pane

CLAUDE_ID = "1b21e2f4-518e-492a-b1ad-61fbbeb27dec"
CODEX_ID = "0199c0de-1111-2222-3333-444455556666"


def save_line(
    *,
    session: str = "main",
    window: str = "1",
    pane: str = "1",
    title: str = "✳ github",
    cwd: str = "/tmp",
    command: str = "claude",
    full: str = f"/home/user/.local/bin/claude --resume {CLAUDE_ID}",
) -> str:
    """一行 resurrect 存档。列的位置来自本机真实语料，见 restore.py 的模块文档。"""
    return "\t".join(
        ["pane", session, window, "1", ":*", pane, title, f":{cwd}", "1", command, f":{full}"]
    )


def make_entry(session_id: str = CLAUDE_ID, source: Source = Source.CLAUDE) -> SessionEntry:
    return SessionEntry(
        id=session_id,
        title="把索引层的缓存加上",
        source=source,
        cwd="/tmp",
        git_branch=None,
        updated_at=datetime(2026, 9, 10, tzinfo=UTC),
        path="/tmp/x.jsonl",
        size_bytes=10,
    )


def make_pane(
    pane_id: str = "%1",
    session: str = "main",
    window: int = 1,
    pane: int = 1,
    command: str = "bash",
) -> Pane:
    return Pane(
        id=pane_id,
        session=session,
        window_index=window,
        window_name="win",
        pane_index=pane,
        current_command=command,
        current_path="/tmp",
        active=False,
        in_mode=False,
        window_active=True,
        width=80,
        height=24,
        title="t",
    )


# ---------------------------------------------------------------- 解析存档


def test_parses_a_claude_pane() -> None:
    (saved,) = restore.parse_save(save_line())
    assert saved.target == "main:1.1"
    assert saved.source is Source.CLAUDE
    assert saved.session_id == CLAUDE_ID
    assert saved.title == "✳ github"
    assert saved.cwd == "/tmp"


def test_parses_every_source_resume_spelling() -> None:
    """各家的恢复参数不一样，解析要从 RESUME_PROGRAMS 反查而不是写死 --resume。"""
    lines = "\n".join(
        [
            save_line(pane="1", command="codex", full=f"/usr/bin/codex resume {CODEX_ID}"),
            save_line(pane="2", command="gemini", full=f"/usr/bin/gemini --resume {CLAUDE_ID}"),
            save_line(pane="3", command="opencode", full="/usr/bin/opencode --session ses_abc"),
            save_line(pane="4", command="pi", full="/usr/bin/pi --session pi_xyz"),
        ]
    )
    found = {s.source: s.session_id for s in restore.parse_save(lines)}
    assert found == {
        Source.CODEX: CODEX_ID,
        Source.GEMINI: CLAUDE_ID,
        Source.OPENCODE: "ses_abc",
        Source.PI: "pi_xyz",
    }


@pytest.mark.parametrize(
    "name,line",
    [
        ("空 shell 的格子", save_line(command="bash", full="")),
        ("不是 pane 行", "window\tmain\t1\t:win\t1\t:*\tlayout\t:"),
        ("列数不够", "pane\tmain\t1"),
        ("认不出的程序", save_line(command="vim", full="/usr/bin/vim /tmp/a.txt")),
        ("有程序名但没 id", save_line(full="/home/user/.local/bin/claude --resume")),
        ("空行", ""),
    ],
)
def test_skips_lines_it_cannot_use(name: str, line: str) -> None:
    assert restore.parse_save(line) == (), name


def test_one_bad_line_does_not_lose_the_good_ones() -> None:
    """硬规则第 4 条：格式是逆向的，一行坏数据不能带走整次恢复。"""
    text = "\n".join(["pane\t坏了", save_line(), "pane\tmain\t1"])
    assert [s.target for s in restore.parse_save(text)] == ["main:1.1"]


# ---------------------------------------------------------------- -t 过滤


@pytest.mark.parametrize(
    "target,expected",
    [
        (None, True),
        ("main", True),
        ("main:1", True),
        ("main:2", False),
        ("work", False),
        ("work:1", False),
    ],
)
def test_target_filter(target: str | None, expected: bool) -> None:
    (saved,) = restore.parse_save(save_line(session="main", window="1"))
    assert restore.matches(saved, target) is expected


# ---------------------------------------------------------------- 计划


def test_ready_when_the_pane_is_an_idle_shell() -> None:
    saved = restore.parse_save(save_line())
    (item,) = restore.build_plan(saved, (make_pane(),), {CLAUDE_ID: make_entry()})
    assert item.ready and item.pane_id == "%1"


def test_never_overwrites_a_pane_that_is_running_something() -> None:
    """这是整个功能最重要的一条：正在用的会话不能被顶掉。"""
    saved = restore.parse_save(save_line())
    panes = (make_pane(command="claude"),)
    (item,) = restore.build_plan(saved, panes, {CLAUDE_ID: make_entry()})
    assert item.state == "occupied" and not item.ready


def test_reports_a_pane_that_no_longer_exists() -> None:
    saved = restore.parse_save(save_line(window="9", pane="9"))
    (item,) = restore.build_plan(saved, (make_pane(),), {CLAUDE_ID: make_entry()})
    assert item.state == "no-pane" and item.pane_id is None


def test_reports_a_session_that_is_gone_from_the_index() -> None:
    saved = restore.parse_save(save_line())
    (item,) = restore.build_plan(saved, (make_pane(),), {})
    assert item.state == "no-session"


def test_plan_honours_the_target_filter() -> None:
    text = "\n".join([save_line(session="main"), save_line(session="work")])
    saved = restore.parse_save(text)
    panes = (make_pane(session="main"), make_pane(pane_id="%9", session="work"))
    entries = {CLAUDE_ID: make_entry()}
    assert len(restore.build_plan(saved, panes, entries, target="main")) == 1
    assert len(restore.build_plan(saved, panes, entries, target=None)) == 2


# ---------------------------------------------------------------- 计划的说明


def test_describe_says_why_each_skipped_one_is_skipped() -> None:
    text = "\n".join([save_line(pane="1"), save_line(pane="2"), save_line(pane="3", window="9")])
    saved = restore.parse_save(text)
    panes = (make_pane("%1", pane=1), make_pane("%2", pane=2, command="claude"))
    items = restore.build_plan(saved, panes, {CLAUDE_ID: make_entry()})

    text_out = restore.describe(items)

    assert "将恢复 1 条" in text_out
    assert "已经在跑东西" in text_out
    assert "没有这一格" in text_out


def test_describe_when_there_is_nothing_to_do() -> None:
    assert "没有可以恢复" in restore.describe(())


# ---------------------------------------------------------------- 执行


def test_executes_only_ready_items_and_never_steals_focus(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        restore, "dispatch", lambda entry, target, **kw: calls.append((entry.id, target, kw))
    )
    text = "\n".join([save_line(pane="1"), save_line(pane="2")])
    saved = restore.parse_save(text)
    panes = (make_pane("%1", pane=1), make_pane("%2", pane=2, command="claude"))
    items = restore.build_plan(saved, panes, {CLAUDE_ID: make_entry()})

    notes = restore.execute(items)

    assert len(calls) == 1  # 被占的那个没投
    assert calls[0][1].pane_id == "%1"
    assert calls[0][2]["focus"] is False  # 恢复完光标还留在用户那格
    assert len(notes) == 1 and "main:1.1" in notes[0]


def test_one_failure_does_not_stop_the_rest(monkeypatch) -> None:
    """串行执行，前一条挂了后面还得继续 —— 否则一条坏会话会挡住全部恢复。"""
    from atm.dispatch import DispatchError

    seen = []

    def flaky(entry, target, **kw):
        seen.append(target.pane_id)
        if target.pane_id == "%1":
            raise DispatchError("工作目录已不存在")

    monkeypatch.setattr(restore, "dispatch", flaky)
    text = "\n".join([save_line(pane="1"), save_line(pane="2")])
    saved = restore.parse_save(text)
    panes = (make_pane("%1", pane=1), make_pane("%2", pane=2))
    items = restore.build_plan(saved, panes, {CLAUDE_ID: make_entry()})

    notes = restore.execute(items)

    assert seen == ["%1", "%2"]
    assert any("失败" in n and "工作目录" in n for n in notes)
    assert any("main:1.2" in n for n in notes)


# ---------------------------------------------------------------- 命令行


def test_print_shows_the_plan_without_dispatching(tmp_path: Path, monkeypatch, capsys) -> None:
    from atm import cli, tmux
    from atm import index as index_mod

    save = tmp_path / "last"
    save.write_text(save_line(), encoding="utf-8")
    monkeypatch.setattr(restore, "save_path", lambda: save)
    monkeypatch.setattr(tmux, "has_server", lambda: True)
    monkeypatch.setattr(tmux, "list_panes", lambda: (make_pane(),))
    monkeypatch.setattr(restore, "current_session", lambda: "main")
    monkeypatch.setattr(
        index_mod, "build", lambda **kw: index_mod.SessionIndex((make_entry(),), None)
    )
    monkeypatch.setattr(restore, "dispatch", lambda *a, **k: pytest.fail("--print 不该投递"))

    assert cli.main(["restore", "--print"]) == cli.EXIT_OK
    assert "将恢复 1 条" in capsys.readouterr().out


def test_outside_tmux_without_target_refuses(tmp_path: Path, monkeypatch, capsys) -> None:
    from atm import cli, tmux

    save = tmp_path / "last"
    save.write_text(save_line(), encoding="utf-8")
    monkeypatch.setattr(restore, "save_path", lambda: save)
    monkeypatch.setattr(tmux, "has_server", lambda: True)
    monkeypatch.setattr(restore, "current_session", lambda: None)

    assert cli.main(["restore"]) == cli.EXIT_ERROR
    assert "-t" in capsys.readouterr().out


def test_no_server_is_a_clear_error(monkeypatch, capsys) -> None:
    from atm import cli, tmux

    monkeypatch.setattr(tmux, "has_server", lambda: False)
    assert cli.main(["restore"]) == cli.EXIT_ERROR
    assert "没有正在跑的 tmux server" in capsys.readouterr().out


def test_missing_save_file_is_a_clear_error(tmp_path: Path, monkeypatch, capsys) -> None:
    from atm import cli, tmux

    monkeypatch.setattr(tmux, "has_server", lambda: True)
    monkeypatch.setattr(restore, "save_path", lambda: tmp_path / "nope")
    assert cli.main(["restore"]) == cli.EXIT_ERROR
    assert "读不到" in capsys.readouterr().out


# ---------------------------------------------------------------- 开机恢复的闸门


from atm import config  # noqa: E402


def cfg_on(**kw) -> config.Config:
    return config.Config(restore_on_boot=True, **kw)


def meminfo(tmp_path: Path, available_kb: int) -> Path:
    p = tmp_path / "meminfo"
    p.write_text(f"MemTotal:       49332392 kB\nMemAvailable:   {available_kb} kB\n")
    return p


def test_gate_is_closed_until_you_turn_it_on(tmp_path: Path) -> None:
    """默认关。开机自动拉起所有会话是 2026-08-12 冻死机器的那条路，不能是默认行为。"""
    gate = restore.boot_gate(config.Config(), state=tmp_path / "s.json")
    assert not gate.ok and "restore.on-boot" in gate.reason


def test_gate_refuses_without_a_cgroup_gate(tmp_path: Path) -> None:
    gate = restore.boot_gate(
        cfg_on(),
        state=tmp_path / "s.json",
        meminfo=meminfo(tmp_path, 10**8),
        limits_available=False,
    )
    assert not gate.ok and "cgroup" in gate.reason


def test_gate_refuses_when_the_last_boot_restore_was_cut_short(tmp_path: Path) -> None:
    """整个功能的止损点：恢复→压死→重启→再恢复，这个循环必须在第二轮被打断。"""
    state = tmp_path / "s.json"
    restore.write_attempt(restore.Attempt("b1", "2026-09-10T08:00:00", planned=4, done=2), state)

    gate = restore.boot_gate(
        cfg_on(), state=state, meminfo=meminfo(tmp_path, 10**8), limits_available=True
    )

    assert not gate.ok and "2/4" in gate.reason and str(state) in gate.reason


def test_gate_passes_when_the_last_one_finished(tmp_path: Path) -> None:
    state = tmp_path / "s.json"
    restore.write_attempt(restore.Attempt("b1", "2026-09-10T08:00:00", planned=4, done=4), state)
    gate = restore.boot_gate(
        cfg_on(), state=state, meminfo=meminfo(tmp_path, 10**8), limits_available=True
    )
    assert gate.ok


def test_gate_refuses_when_memory_is_already_tight(tmp_path: Path) -> None:
    gate = restore.boot_gate(
        cfg_on(restore_min_available="4G"),
        state=tmp_path / "s.json",
        meminfo=meminfo(tmp_path, 1_000_000),  # ~1G
        limits_available=True,
    )
    assert not gate.ok and "restore.min-available" in gate.reason


def test_a_corrupt_state_file_does_not_block_forever(tmp_path: Path) -> None:
    """状态文件是我们自己写的，但坏了不能变成永久拒绝 —— 那样用户只能靠猜。"""
    state = tmp_path / "s.json"
    state.write_text("{ 半个 json", encoding="utf-8")
    gate = restore.boot_gate(
        cfg_on(), state=state, meminfo=meminfo(tmp_path, 10**8), limits_available=True
    )
    assert gate.ok


def test_attempt_round_trips(tmp_path: Path) -> None:
    state = tmp_path / "s.json"
    attempt = restore.Attempt("boot-x", "2026-09-10T08:00:00", planned=3, done=1)
    restore.write_attempt(attempt, state)
    assert restore.read_attempt(state) == attempt
    assert restore.read_attempt(tmp_path / "nope") is None


def test_execute_stops_when_memory_drops_below_the_floor(tmp_path: Path, monkeypatch) -> None:
    """开机模式的第二层：不管上次怎么死的，内存已经紧张就不该再往里塞。"""
    calls = []
    monkeypatch.setattr(restore, "dispatch", lambda e, t, **kw: calls.append(t.pane_id))
    text = "\n".join([save_line(pane="1"), save_line(pane="2"), save_line(pane="3")])
    saved = restore.parse_save(text)
    panes = tuple(make_pane(f"%{i}", pane=i) for i in (1, 2, 3))
    items = restore.build_plan(saved, panes, {CLAUDE_ID: make_entry()})

    # 第一条之前还够，投完就跌破下限
    sizes = iter([10**8, 1_000, 1_000])
    monkeypatch.setattr(restore, "mem_available", lambda *a, **kw: next(sizes) * 1024)

    notes = restore.execute(items, floor=4 << 30)

    assert calls == ["%1"]
    assert any("剩下的先不恢复" in n for n in notes)


def test_progress_counts_failures_too(monkeypatch) -> None:
    """状态文件记的是「走到哪了」。失败也算走过 —— 否则一条坏会话会让下次开机永远拒绝。"""
    from atm.dispatch import DispatchError

    monkeypatch.setattr(
        restore, "dispatch", lambda e, t, **kw: (_ for _ in ()).throw(DispatchError("坏了"))
    )
    saved = restore.parse_save("\n".join([save_line(pane="1"), save_line(pane="2")]))
    panes = (make_pane("%1", pane=1), make_pane("%2", pane=2))
    items = restore.build_plan(saved, panes, {CLAUDE_ID: make_entry()})

    seen: list[int] = []
    restore.execute(items, on_done=seen.append)

    assert seen == [1, 2]


def test_size_to_bytes() -> None:
    assert config.size_to_bytes("4G") == 4 << 30
    assert config.size_to_bytes("512M") == 512 << 20
    assert config.size_to_bytes("1024") == 1024
    assert config.size_to_bytes("infinity") is None  # 没有下限，别拦
    assert config.size_to_bytes("大概吧") is None


# ---------------------------------------------------------------- 开机的接线


def test_persist_block_carries_the_hook_only_when_on_boot(tmp_path: Path) -> None:
    from atm import persist

    off = persist.build_block(tmp_path, on_boot=False)
    on = persist.build_block(tmp_path, on_boot=True, atm_command="/opt/atm")

    assert "@resurrect-hook-post-restore-all" not in off
    assert "set -g @resurrect-hook-post-restore-all '/opt/atm restore --boot'" in on
    # 钩子必须排在 run tpm 之前，否则 tpm 加载 continuum 时读不到它
    assert on.index("@resurrect-hook") < on.index("run '")
    # 这条禁令没变：resurrect 自己不许拉起 AI CLI
    assert "set -g @resurrect-processes" not in on


def test_toggling_on_boot_rewrites_the_installed_block(tmp_path: Path, monkeypatch) -> None:
    from atm import persist, sync

    conf = tmp_path / "tmux.conf"
    monkeypatch.setattr(persist, "resolve_atm_command", lambda: "/opt/atm", raising=False)
    monkeypatch.setattr("atm.install.resolve_atm_command", lambda: "/opt/atm")
    persist.apply(persist.build_plan(conf_path=conf, plugins_dir=tmp_path, cfg=config.Config()))
    assert "@resurrect-hook" not in conf.read_text(encoding="utf-8")

    notes = sync.apply_changes(config.Config(), cfg_on(), conf_path=conf)

    assert "@resurrect-hook-post-restore-all '/opt/atm restore --boot'" in conf.read_text(
        encoding="utf-8"
    )
    assert any("开机恢复的钩子已写进" in n for n in notes)


def test_toggling_on_boot_without_the_block_just_says_run_install(
    tmp_path: Path, monkeypatch
) -> None:
    notes = __import__("atm.sync", fromlist=["sync"]).apply_changes(
        config.Config(), cfg_on(), conf_path=tmp_path / "nothing.conf"
    )
    assert any("atm install" in n for n in notes)


def test_boot_mode_stands_down_and_says_why(tmp_path: Path, monkeypatch, capsys) -> None:
    """闸门拦下时不该报错退出 —— 它是钩子里的后台动作，退出码只会污染 resurrect 的日志。"""
    from atm import cli

    log = tmp_path / "restore.log"
    monkeypatch.setattr(restore, "log_path", lambda: log)
    monkeypatch.setattr(restore, "dispatch", lambda *a, **k: pytest.fail("闸门没拦住"))

    assert cli.main(["restore", "--boot"]) == cli.EXIT_OK
    assert "restore.on-boot" in log.read_text(encoding="utf-8")


def test_boot_mode_restores_and_leaves_a_finished_record(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from atm import cli, tmux
    from atm import index as index_mod

    save = tmp_path / "last"
    save.write_text(save_line(), encoding="utf-8")
    state, log = tmp_path / "s.json", tmp_path / "restore.log"
    monkeypatch.setattr(restore, "save_path", lambda: save)
    monkeypatch.setattr(restore, "state_path", lambda: state)
    monkeypatch.setattr(restore, "log_path", lambda: log)
    monkeypatch.setattr(tmux, "list_panes", lambda: (make_pane(),))
    monkeypatch.setattr(
        index_mod, "build", lambda **kw: index_mod.SessionIndex((make_entry(),), None)
    )
    monkeypatch.setattr(restore, "boot_gate", lambda cfg, **kw: restore.Gate(True, "ok"))
    monkeypatch.setattr("atm.config.load", lambda *a, **k: cfg_on())
    calls = []
    monkeypatch.setattr(restore, "dispatch", lambda e, t, **kw: calls.append(t.pane_id))

    assert cli.main(["restore", "--boot"]) == cli.EXIT_OK

    assert calls == ["%1"]
    assert restore.read_attempt(state).finished  # 下次开机才不会被当成「被杀了」
    assert "开机恢复：1 条" in log.read_text(encoding="utf-8")
