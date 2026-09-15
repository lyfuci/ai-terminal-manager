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


def make_entry(
    session_id: str = CLAUDE_ID,
    source: Source = Source.CLAUDE,
    *,
    name: str | None = None,
    cwd: str = "/tmp",
    day: int = 10,
) -> SessionEntry:
    return SessionEntry(
        id=session_id,
        title="把索引层的缓存加上",
        source=source,
        cwd=cwd,
        git_branch=None,
        updated_at=datetime(2026, 9, day, tzinfo=UTC),
        path="/tmp/x.jsonl",
        size_bytes=10,
        name=name,
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
        ("-r 没带值、后面是别的参数", save_line(full="claude -r --verbose")),
        ("没有恢复参数的新会话", save_line(full="/home/user/.local/bin/claude agents")),
        ("空行", ""),
    ],
)
def test_skips_lines_it_cannot_use(name: str, line: str) -> None:
    assert restore.parse_save(line) == (), name


def test_one_bad_line_does_not_lose_the_good_ones() -> None:
    """硬规则第 4 条：格式是逆向的，一行坏数据不能带走整次恢复。"""
    text = "\n".join(["pane\t坏了", save_line(), "pane\tmain\t1"])
    assert [s.target for s in restore.parse_save(text)] == ["main:1.1"]


def test_parses_the_short_resume_flag_with_a_session_name() -> None:
    """2026-09-15 真机：格子里全是手敲的 `claude -r github`。

    atm 当时只认 `--resume <id>`，每份存档都解析出 0 条。
    """
    lines = "\n".join(
        [
            save_line(pane="1", full="claude -r github"),
            save_line(pane="2", command="gemini", full=f"gemini -r {CLAUDE_ID}"),
            save_line(pane="3", command="opencode", full="opencode -s ses_abc"),
        ]
    )
    found = {s.source: s.session_id for s in restore.parse_save(lines)}
    assert found == {Source.CLAUDE: "github", Source.GEMINI: CLAUDE_ID, Source.OPENCODE: "ses_abc"}


@pytest.mark.parametrize(
    "full,expected",
    [
        ("claude --resume=abc", [("abc", "abc")]),
        ("claude --verbose -- --resume github", []),  # `--` 之后是位置参数
        ("claude -r github -- extra", [("github", "github -- extra")]),
    ],
)
def test_resume_argument_edge_cases(full: str, expected: list[tuple[str, str]]) -> None:
    found = restore.parse_save(save_line(full=full))
    assert [(s.session_id, s.ref_tail) for s in found] == expected


def test_a_title_that_starts_with_a_colon_is_not_mistaken_for_the_shifted_layout() -> None:
    (saved,) = restore.parse_save(save_line(title=":weird", cwd="/tmp"))
    assert (saved.title, saved.cwd) == (":weird", "/tmp")


def test_a_line_whose_empty_title_shifted_the_columns_keeps_its_cwd() -> None:
    """标题为空时从第 6 列起整体左移一格（真机语料：server 退出途中写的存档）。"""
    line = "\t".join(
        [
            "pane",
            "main",
            "1",
            "1",
            ":*",
            "1",
            ":/home/user/workdir",
            "0",
            "claude",
            "1946",
            ":claude -r github",
        ]
    )
    (saved,) = restore.parse_save(line)
    assert (saved.cwd, saved.title, saved.session_id) == ("/home/user/workdir", "", "github")


def test_counts_ai_panes_whether_or_not_they_are_restorable() -> None:
    # 真机语料：server 退出途中写的存档，这格的命令行被挤成了单独一行，程序列还是 claude
    lost_command = "\t".join(
        ["pane", "main", "1", "1", ":*", "4", ":/home/user/workdir", "1", "claude", "2549", ":"]
    )
    text = "\n".join(
        [
            save_line(pane="1", full="claude -r github"),
            save_line(pane="2", full="/home/user/.local/bin/claude agents"),
            save_line(pane="3", command="bash", full=""),
            lost_command,
            "window\tmain\t1\t:win\t1\t:*\tlayout\t:",
        ]
    )
    assert restore.count_ai_panes(text) == 3
    assert len(restore.parse_save(text)) == 1


def test_newest_restorable_save_skips_last_and_saves_without_sessions(tmp_path: Path) -> None:
    """重启后格子还空着时自动存档把 last 换成了空的，重启前那份要能被找出来。"""
    before = tmp_path / "tmux_resurrect_20260914T225445.txt"
    before.write_text(save_line(full="claude -r github"), encoding="utf-8")
    older = tmp_path / "tmux_resurrect_20260913T100000.txt"
    older.write_text(save_line(full="claude -r wsl"), encoding="utf-8")
    empty = tmp_path / "tmux_resurrect_20260915T094159.txt"
    empty.write_text(save_line(command="bash", full=""), encoding="utf-8")
    last = tmp_path / "last"
    last.symlink_to(empty.name)

    assert restore.newest_restorable_save(tmp_path, skip=last) == before

    last.unlink()
    last.symlink_to(before.name)  # last 本身就有会话时不该把它自己指回去
    assert restore.newest_restorable_save(tmp_path, skip=last) == older


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


def _by_id(*entries: SessionEntry) -> dict[str, SessionEntry]:
    return {e.id: e for e in entries}


def test_a_session_name_is_looked_up_in_the_index() -> None:
    (saved,) = restore.parse_save(save_line(full="claude -r github"))
    wanted = make_entry(name="github")
    other = make_entry("other-id", name="wsl")
    (item,) = restore.build_plan((saved,), (make_pane(),), _by_id(wanted, other))
    assert item.ready and item.entry is wanted


def test_a_name_with_spaces_matches_the_whole_tail() -> None:
    """存档里引号丢了：`claude -r "my project"` 记下来是 `-r my project`。

    只看第一个词会查成 `my`。
    """
    (saved,) = restore.parse_save(save_line(full="claude -r my project --verbose"))
    assert (saved.session_id, saved.ref_tail) == ("my", "my project --verbose")
    short = make_entry("a", name="my")
    full = make_entry("b", name="my project")
    assert restore.resolve(saved, _by_id(short, full)) == (full,)


@pytest.mark.parametrize(
    "full,names,expected",
    [
        # `project` 可能是名字的一半，也可能是 prompt —— 分不清就不拿 `my` 去配（codex 复核 P1）
        ("claude -r my project", ["my"], None),
        # 名字到第一个选项为止：`--verbose` 不是名字的一部分
        ("claude -r github --verbose", ["github", "github --verbose"], "github"),
        ("claude -r github -- extra", ["github", "github -- extra"], "github"),
    ],
)
def test_the_name_is_the_whole_segment_before_the_first_option(
    full: str, names: list[str], expected: str | None
) -> None:
    (saved,) = restore.parse_save(save_line(full=full))
    entries = _by_id(*(make_entry(f"id-{i}", name=n) for i, n in enumerate(names)))
    assert [e.name for e in restore.resolve(saved, entries)] == ([expected] if expected else [])


def test_a_name_the_index_had_to_truncate_is_never_treated_as_an_identity() -> None:
    """索引里的名字截到 40 列补「…」，只在 40 列之后不同的两个名字截断后相等 —— 不能拿来认身份。"""
    from atm.text import clean_title

    long_name = "a-very-long-session-name-that-goes-past-forty-columns-one"
    other_long = "a-very-long-session-name-that-goes-past-forty-columns-two"
    (saved,) = restore.parse_save(save_line(full=f"claude -r {long_name}"))
    truncated_other = make_entry(name=clean_title(other_long, limit=40))
    assert restore.resolve(saved, _by_id(truncated_other)) == ()


def test_names_only_match_in_the_same_directory() -> None:
    """`claude -r <名字>` 自己就只在当前项目目录里找，跨目录匹配等于替它猜。"""
    (saved,) = restore.parse_save(save_line(cwd="/work", full="claude -r video"))
    assert restore.resolve(saved, _by_id(make_entry(name="video", cwd="/elsewhere"))) == ()


def test_duplicate_names_are_reported_with_candidates_not_guessed() -> None:
    """本机就有两个 ⟨video⟩。按更新时间挑会选错。

    比的是**现在**的更新时间，存档之后才动过的另一个同名会话会被选中（2026-09-15 codex 复核）。
    """
    (saved,) = restore.parse_save(save_line(cwd="/work", full="claude -r video"))
    older = make_entry("a", name="video", cwd="/work", day=12)
    newer = make_entry("b", name="video", cwd="/work", day=13)

    (item,) = restore.build_plan((saved,), (make_pane(),), _by_id(older, newer))

    assert item.state == "ambiguous" and item.entry is None and not item.ready
    assert {e.id for e in item.candidates} == {"a", "b"}
    text = restore.describe((item,))
    assert "atm resume" in text
    assert "a  09-12 00:00" in text and "b  09-13 00:00" in text


def test_an_id_match_wins_over_a_name_match() -> None:
    (saved,) = restore.parse_save(save_line(full=f"claude --resume {CLAUDE_ID}"))
    by_id = make_entry()
    named_like_the_id = make_entry("other-id", name=CLAUDE_ID, day=20)
    assert restore.resolve(saved, _by_id(by_id, named_like_the_id)) == (by_id,)


def test_an_id_from_another_source_does_not_match() -> None:
    """claude 的引用不能落到恰好同 id 的 codex 条目上。"""
    (saved,) = restore.parse_save(save_line(full=f"claude --resume {CODEX_ID}"))
    assert restore.resolve(saved, _by_id(make_entry(CODEX_ID, source=Source.CODEX))) == ()


def test_names_are_only_matched_within_the_same_source() -> None:
    (saved,) = restore.parse_save(save_line(full="claude -r github"))
    codex_named_github = make_entry(CODEX_ID, source=Source.CODEX, name="github")
    (item,) = restore.build_plan((saved,), (make_pane(),), _by_id(codex_named_github))
    assert item.state == "no-session"


def test_a_search_term_that_is_not_a_session_name_is_reported_not_guessed() -> None:
    """`claude -r foo` 在 claude 里是「带搜索词开选择器」，不是精确名字 —— atm 不猜，报出来。"""
    (saved,) = restore.parse_save(save_line(full="claude -r git"))
    (item,) = restore.build_plan((saved,), (make_pane(),), _by_id(make_entry(name="github")))
    assert item.state == "no-session"
    assert "会话名" in restore.describe((item,))


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


def test_save_file_option_reads_that_file_and_says_which(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from atm import cli, tmux
    from atm import index as index_mod

    chosen = tmp_path / "tmux_resurrect_20260914T225445.txt"
    chosen.write_text(save_line(), encoding="utf-8")
    monkeypatch.setattr(restore, "save_path", lambda: pytest.fail("给了 --save-file 就不该读 last"))
    monkeypatch.setattr(tmux, "has_server", lambda: True)
    monkeypatch.setattr(tmux, "list_panes", lambda: (make_pane(),))
    monkeypatch.setattr(restore, "current_session", lambda: "main")
    monkeypatch.setattr(
        index_mod, "build", lambda **kw: index_mod.SessionIndex((make_entry(),), None)
    )
    monkeypatch.setattr(restore, "dispatch", lambda *a, **k: pytest.fail("--print 不该投递"))

    assert cli.main(["restore", "--print", "--save-file", str(chosen)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert str(chosen) in out and "将恢复 1 条" in out


def test_an_empty_last_points_at_the_newest_older_save_with_sessions(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from atm import cli, tmux

    before = tmp_path / "tmux_resurrect_20260914T225445.txt"
    before.write_text(save_line(full="claude -r github"), encoding="utf-8")
    after = tmp_path / "tmux_resurrect_20260915T094159.txt"
    after.write_text(save_line(command="bash", full=""), encoding="utf-8")
    last = tmp_path / "last"
    last.symlink_to(after.name)
    monkeypatch.setattr(restore, "save_path", lambda: last)
    monkeypatch.setattr(tmux, "has_server", lambda: True)

    assert cli.main(["restore"]) == cli.EXIT_OK
    assert f"atm restore --save-file {before}" in capsys.readouterr().out


def test_boot_mode_refuses_a_save_file_instead_of_silently_ignoring_it(monkeypatch, capsys) -> None:
    from atm import cli

    monkeypatch.setattr(restore, "dispatch", lambda *a, **k: pytest.fail("不该投递"))
    assert cli.main(["restore", "--boot", "--save-file", "/tmp/x"]) == cli.EXIT_ERROR
    assert "--save-file" in capsys.readouterr().out


def test_a_corrupt_older_save_does_not_stop_the_search(tmp_path: Path) -> None:
    """非法 UTF-8 曾经抛 UnicodeDecodeError，后面正常的存档就不再检查了（codex 复核 P2）。"""
    good = tmp_path / "tmux_resurrect_20260913T100000.txt"
    good.write_text(save_line(full="claude -r wsl"), encoding="utf-8")
    corrupt = tmp_path / "tmux_resurrect_20260914T225445.txt"
    corrupt.write_bytes(b"pane\t\xff\xfe broken\n")
    assert restore.newest_restorable_save(tmp_path) == good


def test_boot_mode_logs_why_nothing_was_restored(tmp_path: Path, monkeypatch) -> None:
    """开机没人看着：同名认不准时，候选只能在日志里查到。"""
    from atm import cli, tmux
    from atm import index as index_mod

    save = tmp_path / "last"
    save.write_text(save_line(full="claude -r video"), encoding="utf-8")
    log = tmp_path / "restore.log"
    monkeypatch.setattr(restore, "save_path", lambda: save)
    monkeypatch.setattr(restore, "log_path", lambda: log)
    monkeypatch.setattr(tmux, "list_panes", lambda: (make_pane(),))
    twins = (make_entry("a", name="video"), make_entry("b", name="video", day=11))
    monkeypatch.setattr(index_mod, "build", lambda **kw: index_mod.SessionIndex(twins, None))
    monkeypatch.setattr(restore, "boot_gate", lambda cfg, **kw: restore.Gate(True, "ok"))
    monkeypatch.setattr("atm.config.load", lambda *a, **k: cfg_on())
    monkeypatch.setattr(restore, "dispatch", lambda *a, **k: pytest.fail("认不准不该投递"))

    assert cli.main(["restore", "--boot"]) == cli.EXIT_OK
    text = log.read_text(encoding="utf-8")
    assert "atm resume" in text and "a  09-10" in text and "b  09-11" in text


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


def test_toggling_on_boot_writes_the_hook_block_regardless_of_tpm(
    tmp_path: Path, monkeypatch
) -> None:
    """改一次配置就该生效 —— 而且不管持久化块装不装、tpm 归谁管。"""
    from atm import persist, sync

    monkeypatch.setattr("atm.install.resolve_atm_command", lambda: "/opt/atm")
    conf = tmp_path / "tmux.conf"
    conf.write_text("run '~/.tmux/plugins/tpm/tpm'\n", encoding="utf-8")  # 用户自己的 tpm

    notes = sync.apply_changes(config.Config(), cfg_on(), conf_path=conf)

    text = conf.read_text(encoding="utf-8")
    assert persist.HOOK_MARKER_BEGIN in text
    assert "@resurrect-hook-post-restore-all '/opt/atm restore --boot'" in text
    assert any("开机恢复" in n for n in notes)
    assert not any("atm install" in n for n in notes)  # 不再指向那个没用的动作

    back = sync.apply_changes(cfg_on(), config.Config(), conf_path=conf)
    assert persist.HOOK_MARKER_BEGIN not in conf.read_text(encoding="utf-8")
    assert any("开机恢复" in n for n in back)


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


# ------------------------------------------------- on-boot 在自己管 tpm 的机器上会静默失效
#
# 2026-09-12 在真机上撞到：`restore.on-boot = true` 在配置里，但钩子既不在 ~/.tmux.conf
# 也不在运行中的 server 上 —— 因为用户自己管 tpm，atm 跳过整个持久化块，钩子跟着一起没写。
# 而当时 sync 打的提示是「跑一次 atm install 才会挂上钩子」，**那句是错的**：跑了也不会装。
# 和 PR #32 修的是同一类毛病 —— atm 声称了它没验证过的事。


def test_doctor_flags_on_boot_that_is_configured_but_inert(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """配置说开着、钩子却不在 —— doctor 必须报出来，否则用户以为它在工作。"""
    from atm import cli, config, tmux

    monkeypatch.setattr(config, "load", lambda *a, **k: cfg_on())
    monkeypatch.setattr(restore, "boot_gate", lambda cfg, **kw: restore.Gate(True, "ok"))
    monkeypatch.setattr(tmux, "has_server", lambda: True)
    monkeypatch.setattr(tmux, "run", lambda args, **kw: "")  # 钩子选项读出来是空

    cli._report_boot_restore()

    out = capsys.readouterr().out
    assert "钩子" in out
    # 钩子现在由 atm 自己写，所以指的是「跑 install」，不再让用户手工粘
    assert "atm install" in out


def test_doctor_flags_a_save_whose_ai_panes_atm_cannot_restore(tmp_path: Path, capsys) -> None:
    """2026-09-15：存档里 4 个 claude 格子，atm 一条都认不出，doctor 却只报了存档时间。"""
    from atm import cli

    save = tmp_path / "last"
    save.write_text(
        "\n".join(
            [
                save_line(pane="1", full="claude -r github"),
                save_line(pane="2", full="/home/user/.local/bin/claude agents"),
            ]
        ),
        encoding="utf-8",
    )
    cli._report_save_contents(save)
    assert "❌ 2 个格子只有 1 个" in capsys.readouterr().out

    save.write_text(save_line(full="claude -r github"), encoding="utf-8")
    cli._report_save_contents(save)
    assert "命令行里都带着" in capsys.readouterr().out

    save.write_text(save_line(command="bash", full=""), encoding="utf-8")
    cli._report_save_contents(save)
    assert capsys.readouterr().out == ""  # 没有 AI 格子就不说话


# ----------------------------------------- 钩子有自己的块，不再受「谁管 tpm」影响
#
# 2026-09-12 的结构性修法。在此之前钩子写在**持久化块**里，而那个块在用户自己管 tpm 时
# 整块不写 —— 于是一个 restore.* 的配置项，命运被一个 tpm 的判断绑住，出现了
# 「restore.on-boot = true 但永远不生效」这个状态。#39 只是把它报出来；这里让它不可能出现。
#
# 能这么改是因为 resurrect 读这个选项的时机是**恢复发生时**（helpers.sh 的 execute_hook
# 里才 get_tmux_option），不是配置加载时。所以它不需要待在插件块里，放文件最前面就行。


def hook_conf(tmp_path: Path, body: str = "") -> Path:
    p = tmp_path / "tmux.conf"
    p.write_text(body, encoding="utf-8")
    return p


def test_hook_block_is_written_even_when_the_user_manages_tpm(tmp_path: Path, monkeypatch) -> None:
    """这就是整个修法的目的：tpm 归谁管，和 on-boot 生不生效无关。"""
    from atm import persist

    monkeypatch.setattr("atm.install.resolve_atm_command", lambda: "/opt/atm")
    conf = hook_conf(tmp_path, "run '~/.tmux/plugins/tpm/tpm'\n")  # 用户自己的 tpm

    result = persist.apply_boot_hook(cfg_on(), conf_path=conf, live=False)

    text = conf.read_text(encoding="utf-8")
    assert result.written
    assert text.startswith(persist.HOOK_MARKER_BEGIN)  # 放最前面，一定在 run tpm 之前
    assert "@resurrect-hook-post-restore-all '/opt/atm restore --boot'" in text
    assert "run '~/.tmux/plugins/tpm/tpm'" in text  # 用户自己的行一个字没动


def test_turning_it_off_removes_only_that_block(tmp_path: Path, monkeypatch) -> None:
    from atm import persist

    monkeypatch.setattr("atm.install.resolve_atm_command", lambda: "/opt/atm")
    user = "# mine\nrun '~/.tmux/plugins/tpm/tpm'\n"
    conf = hook_conf(tmp_path, user)
    persist.apply_boot_hook(cfg_on(), conf_path=conf, live=False)

    persist.apply_boot_hook(config.Config(), conf_path=conf, live=False)

    assert conf.read_text(encoding="utf-8") == user  # 干净回到原样


def test_reapply_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    from atm import persist

    monkeypatch.setattr("atm.install.resolve_atm_command", lambda: "/opt/atm")
    conf = hook_conf(tmp_path, "# mine\n")
    persist.apply_boot_hook(cfg_on(), conf_path=conf, live=False)

    second = persist.apply_boot_hook(cfg_on(), conf_path=conf, live=False)

    assert not second.written  # 没变化就不写不备份
    assert conf.read_text(encoding="utf-8").count(persist.HOOK_MARKER_BEGIN) == 1


def test_the_plugin_block_no_longer_carries_the_hook(tmp_path: Path) -> None:
    """钩子搬走之后插件块里不能再有一份，否则两处各写一遍会打架。"""
    from atm import persist

    block = persist.build_block(tmp_path, atm_command="/opt/atm")
    assert "@resurrect-hook-post-restore-all" not in block


def test_it_takes_effect_on_the_running_server_too(tmp_path: Path, monkeypatch) -> None:
    """不能只等下次起 server —— 改完配置这次就该生效。"""
    from atm import persist, tmux

    calls: list[list[str]] = []
    monkeypatch.setattr("atm.install.resolve_atm_command", lambda: "/opt/atm")
    monkeypatch.setattr(tmux, "has_server", lambda: True)
    monkeypatch.setattr(tmux, "run", lambda args, **kw: calls.append(list(args)) or "")

    persist.apply_boot_hook(cfg_on(), conf_path=hook_conf(tmp_path))

    assert calls == [
        ["set-option", "-g", "@resurrect-hook-post-restore-all", "/opt/atm restore --boot"]
    ]


def test_turning_it_off_unsets_it_on_the_running_server(tmp_path: Path, monkeypatch) -> None:
    from atm import persist, tmux

    monkeypatch.setattr("atm.install.resolve_atm_command", lambda: "/opt/atm")
    conf = hook_conf(tmp_path)
    persist.apply_boot_hook(cfg_on(), conf_path=conf, live=False)

    calls: list[list[str]] = []
    monkeypatch.setattr(tmux, "has_server", lambda: True)
    monkeypatch.setattr(tmux, "run", lambda args, **kw: calls.append(list(args)) or "")
    persist.apply_boot_hook(config.Config(), conf_path=conf)

    assert calls == [["set-option", "-gu", "@resurrect-hook-post-restore-all"]]


def test_live_failure_is_reported_not_raised(tmp_path: Path, monkeypatch) -> None:
    from atm import persist, tmux

    monkeypatch.setattr("atm.install.resolve_atm_command", lambda: "/opt/atm")
    monkeypatch.setattr(tmux, "has_server", lambda: True)

    def boom(args, **kw):
        raise tmux.TmuxError("server gone")

    monkeypatch.setattr(tmux, "run", boom)
    result = persist.apply_boot_hook(cfg_on(), conf_path=hook_conf(tmp_path))
    assert result.written and "server gone" in (result.live_error or "")


def test_uninstall_removes_the_hook_block(tmp_path: Path, monkeypatch) -> None:
    from atm import persist

    monkeypatch.setattr("atm.install.resolve_atm_command", lambda: "/opt/atm")
    user = "# mine\n"
    conf = hook_conf(tmp_path, user)
    persist.apply_boot_hook(cfg_on(), conf_path=conf, live=False)

    removed, backup = persist.remove_boot_hook(conf)

    assert removed and backup is not None
    assert conf.read_text(encoding="utf-8") == user
    assert persist.remove_boot_hook(conf) == (False, None)  # 第二次没得删
