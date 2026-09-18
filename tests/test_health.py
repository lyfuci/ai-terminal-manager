"""格子健康：读 /proc 和 cgroup 的解析、问题判定、跟踪与统计日志。

全部用 tmp_path 造假的 /proc 和 /sys/fs/cgroup，不碰真系统。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from atm import health, tmux

USER = "user.slice/user-1000.slice/user@1000.service"
PANE_CG = f"{USER}/app.slice/tmux-spawn-a.scope"
RUN_CG = f"{USER}/atm.slice/atm-ai.slice/run-r1.scope"


def _psi(some: float, full: float = 0.0) -> str:
    return (
        f"some avg10={some:.2f} avg60=0.00 avg300=0.00 total=1\n"
        f"full avg10={full:.2f} avg60=0.00 avg300=0.00 total=1\n"
    )


class Fake:
    """一套假的 /proc + cgroup 树。"""

    def __init__(self, root: Path) -> None:
        self.proc = root / "proc"
        self.cg = root / "cgroup"
        self.proc.mkdir()
        self.cg.mkdir()

    def process(self, pid: int, ppid: int, *, comm="bash", state="S", cgroup=PANE_CG, wchan="0"):
        d = self.proc / str(pid)
        d.mkdir(exist_ok=True)
        (d / "stat").write_text(f"{pid} ({comm}) {state} {ppid} 1 1 0 -1\n", encoding="utf-8")
        (d / "cgroup").write_text(f"0::/{cgroup}\n", encoding="utf-8")
        (d / "wchan").write_text(wchan, encoding="utf-8")

    def group(self, path: str, **files: str) -> Path:
        d = self.cg / path
        d.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (d / name.replace("_", ".")).write_text(text, encoding="utf-8")
        return d

    def sample(self, panes, previous=None):
        return health.sample(panes, previous=previous, proc_root=self.proc, cgroup_root=self.cg)


@pytest.fixture
def fake(tmp_path):
    return Fake(tmp_path)


# ---------------------------------------------------------------- 解析


def test_read_psi(tmp_path):
    f = tmp_path / "p"
    f.write_text(_psi(12.5, 3.25), encoding="utf-8")
    assert health.read_psi(f) == health.Psi(some=12.5, full=3.25)


def test_read_psi_without_full_line(tmp_path):
    """老内核的 cpu.pressure 只有 some 行。"""
    f = tmp_path / "p"
    f.write_text("some avg10=7.00 avg60=0.00 avg300=0.00 total=1\n", encoding="utf-8")
    assert health.read_psi(f) == health.Psi(some=7.0, full=0.0)


@pytest.mark.parametrize("text", ["", "garbage\n", "some avg10=x total=1\n"])
def test_read_psi_garbage(tmp_path, text):
    f = tmp_path / "p"
    f.write_text(text, encoding="utf-8")
    assert health.read_psi(f) is None


def test_read_psi_missing(tmp_path):
    assert health.read_psi(tmp_path / "nope") is None


def test_stat_comm_with_parens_and_spaces(fake):
    fake.process(42, 1, comm="weird ) (name")
    table = health.process_table(fake.proc)
    assert table[42].comm == "weird ) (name"
    assert table[42].ppid == 1


def test_process_table_skips_garbage(fake):
    (fake.proc / "7").mkdir()
    (fake.proc / "7" / "stat").write_text("nonsense", encoding="utf-8")
    (fake.proc / "self").mkdir()
    assert health.process_table(fake.proc) == {}


def test_descendants(fake):
    fake.process(10, 1)
    fake.process(11, 10)
    fake.process(12, 11)
    fake.process(20, 1)
    table = health.process_table(fake.proc)
    assert sorted(health.descendants(table, 10)) == [10, 11, 12]
    assert health.descendants(table, 99) == []


def test_cgroup_of_ignores_v1_lines(fake):
    fake.process(5, 1)
    (fake.proc / "5" / "cgroup").write_text("1:name=systemd:/x\n0::/a/b\n", encoding="utf-8")
    assert health.cgroup_of(5, fake.proc) == "/a/b"


# ---------------------------------------------------------------- 判定


def test_healthy_pane(fake):
    fake.process(100, 1)
    fake.group(PANE_CG, memory_pressure=_psi(0), io_pressure=_psi(0), cpu_pressure=_psi(0))
    h = fake.sample([("%1", 100)])["%1"]
    assert h.ok
    assert h.cgroups == (f"/{PANE_CG}",)


def test_dead_or_unknown_pid_is_skipped(fake):
    assert fake.sample([("%1", 100), ("%2", 0)]) == {}


def test_psi_takes_worst_cgroup_in_pane(fake):
    """atm 投递的会话被挪进了 slice：一格的进程分在两个 cgroup，哪个卡都算这格卡。"""
    fake.process(100, 1)
    fake.process(101, 100, comm="claude", cgroup=RUN_CG)
    fake.group(PANE_CG, memory_pressure=_psi(0), io_pressure=_psi(1))
    fake.group(RUN_CG, memory_pressure=_psi(25, 5), io_pressure=_psi(0))
    h = fake.sample([("%1", 100)])["%1"]
    assert h.memory == health.Psi(25, 5)
    assert h.problems == (health.MEMORY,)


@pytest.mark.parametrize(
    ("files", "code"),
    [
        ({"io_pressure": _psi(45)}, health.IO),
        ({"io_pressure": _psi(5, 25)}, health.IO),
        ({"cpu_pressure": _psi(70)}, health.CPU),
        ({"memory_pressure": _psi(10)}, health.MEMORY),
    ],
)
def test_psi_thresholds(fake, files, code):
    fake.process(100, 1)
    fake.group(PANE_CG, **files)
    assert fake.sample([("%1", 100)])["%1"].problems == (code,)


def test_below_thresholds_is_ok(fake):
    fake.process(100, 1)
    fake.group(PANE_CG, memory_pressure=_psi(9), io_pressure=_psi(39, 19), cpu_pressure=_psi(59))
    assert fake.sample([("%1", 100)])["%1"].ok


def test_over_high_found_on_ancestor(fake):
    """会话 scope 自己没限制，限制设在外层 slice 上 —— 要往上找。"""
    fake.process(100, 1, cgroup=RUN_CG)
    fake.group(RUN_CG, memory_high="max\n", memory_current="100\n")
    fake.group(f"{USER}/atm.slice/atm-ai.slice", memory_high="1000\n", memory_current="2000\n")
    h = fake.sample([("%1", 100)])["%1"]
    assert h.over_high == "atm-ai.slice"
    assert health.OVER_HIGH in h.problems


def test_d_state_needs_two_consecutive_samples(fake):
    fake.process(100, 1)
    fake.process(101, 100, comm="git", state="D", wchan="folio_wait_bit_common")
    first = fake.sample([("%1", 100)])
    assert first["%1"].blocked and not first["%1"].stuck
    assert first["%1"].ok  # 一瞬间的 D 很正常，不报

    second = fake.sample([("%1", 100)], previous=first)
    assert second["%1"].problems == (health.STUCK,)
    assert second["%1"].stuck[0] == health.StuckProc(101, "git", "folio_wait_bit_common")


def test_d_state_of_different_pid_is_not_stuck(fake):
    fake.process(100, 1)
    fake.process(101, 100, state="D")
    first = fake.sample([("%1", 100)])
    fake.process(101, 100, state="S")
    fake.process(102, 100, state="D")
    assert fake.sample([("%1", 100)], previous=first)["%1"].ok


def _clock(monkeypatch, *values):
    it = iter(values)
    monkeypatch.setattr(health.time, "monotonic", lambda: next(it))


def test_reclaim_rate_from_local_events(fake, monkeypatch):
    fake.process(100, 1, cgroup=RUN_CG)
    run = fake.group(RUN_CG, memory_events_local="low 0\nhigh 100\nmax 0\n")
    _clock(monkeypatch, 10.0, 12.0)
    first = fake.sample([("%1", 100)])
    (run / "memory.events.local").write_text("low 0\nhigh 1000\nmax 0\n", encoding="utf-8")
    h = fake.sample([("%1", 100)], previous=first)["%1"]
    assert h.reclaim_rate == pytest.approx(450.0)
    assert h.reclaim_at == "run-r1.scope"
    assert h.problems == (health.RECLAIM,)


def test_reclaim_on_ancestor_slice_counts(fake, monkeypatch):
    """总量 slice 撞限，底下每一格都在被拖慢。"""
    fake.process(100, 1, cgroup=RUN_CG)
    fake.group(RUN_CG, memory_events_local="high 0\n")
    slice_ = fake.group(f"{USER}/atm.slice/atm-ai.slice", memory_events_local="high 0\n")
    _clock(monkeypatch, 0.0, 1.0)
    first = fake.sample([("%1", 100)])
    (slice_ / "memory.events.local").write_text("high 300\n", encoding="utf-8")
    h = fake.sample([("%1", 100)], previous=first)["%1"]
    assert h.reclaim_at == "atm-ai.slice"
    assert health.RECLAIM in h.problems


def test_hierarchical_events_on_ancestor_do_not_leak(fake, monkeypatch):
    """回归：`memory.events` 是层级累计的。实测一个撞限的 scope 会让同一 user@ 下
    所有格子都报「回收」—— 祖先层只能读 `.local`。"""
    fake.process(100, 1)
    fake.group(PANE_CG, memory_events_local="high 0\n", memory_events="high 0\n")
    user = fake.group(USER, memory_events_local="high 0\n", memory_events="high 0\n")
    _clock(monkeypatch, 0.0, 1.0)
    first = fake.sample([("%1", 100)])
    # 兄弟 scope 撞了自己的限制：只有 user@ 的层级计数涨
    (user / "memory.events").write_text("high 5000\n", encoding="utf-8")
    assert fake.sample([("%1", 100)], previous=first)["%1"].ok


def test_old_kernel_without_local_falls_back_on_leaf_only(fake, monkeypatch):
    fake.process(100, 1)
    leaf = fake.group(PANE_CG, memory_events="high 0\n")
    user = fake.group(USER, memory_events="high 0\n")
    _clock(monkeypatch, 0.0, 1.0)
    first = fake.sample([("%1", 100)])
    (leaf / "memory.events").write_text("high 50\n", encoding="utf-8")
    (user / "memory.events").write_text("high 9999\n", encoding="utf-8")
    h = fake.sample([("%1", 100)], previous=first)["%1"]
    assert h.reclaim_rate == pytest.approx(50.0)
    assert h.reclaim_at == "tmux-spawn-a.scope"


def test_counter_reset_is_ignored(fake, monkeypatch):
    """scope 被重建时计数归零 —— 不能算出负速率。"""
    fake.process(100, 1)
    leaf = fake.group(PANE_CG, memory_events_local="high 500\n")
    _clock(monkeypatch, 0.0, 1.0)
    first = fake.sample([("%1", 100)])
    (leaf / "memory.events.local").write_text("high 3\n", encoding="utf-8")
    assert fake.sample([("%1", 100)], previous=first)["%1"].reclaim_rate == 0.0


def test_problem_order():
    assert health.problem_order({health.CPU, health.STUCK, health.MEMORY}) == (
        health.STUCK,
        health.MEMORY,
        health.CPU,
    )


def test_summary_and_json():
    h = health.PaneHealth(
        pane_id="%1",
        memory=health.Psi(12, 1),
        reclaim_rate=450,
        reclaim_at="run-r1.scope",
        stuck=(health.StuckProc(9, "git", "x"),),
    )
    assert h.summary() == "mem 12% high 450/s(run-r1.scope) D:1(git)"
    data = h.to_json()
    assert data["problems"] == [health.STUCK, health.RECLAIM, health.MEMORY]
    assert data["stuck"] == [{"pid": 9, "comm": "git", "wchan": "x"}]
    json.dumps(data)


# ---------------------------------------------------------------- 跟踪 + 日志


def _bad(pane="%1"):
    return health.PaneHealth(pane_id=pane, memory=health.Psi(50, 10))


def _good(pane="%1"):
    return health.PaneHealth(pane_id=pane, memory=health.Psi(0, 0))


def test_tracker_episode_start_and_end(tmp_path):
    log = tmp_path / "h.jsonl"
    tracker = health.Tracker(log)
    assert tracker.update({"%1": _good()}, {"%1": "proj"}, now=100) == []

    changes = tracker.update({"%1": _bad()}, {"%1": "proj"}, now=103)
    assert [c.kind for c in changes] == ["started"]
    assert tracker.update({"%1": _bad()}, {"%1": "proj"}, now=106) == []  # 还在卡，不重复报

    changes = tracker.update({"%1": _good()}, {"%1": "proj"}, now=130)
    assert [(c.kind, c.duration) for c in changes] == [("ended", 27)]

    records = health.read_log(log)
    assert [r["event"] for r in records] == ["started", "ended"]
    assert records[1]["seconds"] == 27
    assert records[1]["label"] == "proj"
    assert records[1]["problems"] == [health.MEMORY]


def test_tracker_closes_episode_when_pane_disappears(tmp_path):
    tracker = health.Tracker(tmp_path / "h.jsonl")
    tracker.update({"%1": _bad()}, {}, now=0)
    changes = tracker.update({}, {}, now=5)
    assert [(c.kind, c.episode.pane_id) for c in changes] == [("ended", "%1")]
    assert tracker.open == {}


def test_tracker_without_log_writes_nothing(tmp_path):
    tracker = health.Tracker()
    tracker.update({"%1": _bad()}, {}, now=0)
    assert list(tmp_path.iterdir()) == []


def test_log_rotation(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "_LOG_MAX_BYTES", 100)
    log = tmp_path / "h.jsonl"
    for i in range(10):
        health.append_log(log, {"event": "ended", "ts": i, "seconds": 1, "label": "x" * 20})
    assert (tmp_path / "h.jsonl.1").exists()
    assert log.stat().st_size <= 200
    # 读的时候两代都算
    assert len(health.read_log(log)) > log.read_text(encoding="utf-8").count("\n")


def test_read_log_skips_bad_lines(tmp_path):
    log = tmp_path / "h.jsonl"
    log.write_text('not json\n[1]\n{"event": "other"}\n{"event": "ended"}\n', encoding="utf-8")
    assert health.read_log(log) == [{"event": "ended"}]


def test_summarize():
    records = [
        {"event": "started", "ts": 1, "label": "a"},
        {"event": "ended", "ts": 10, "seconds": 5, "label": "a", "problems": ["io"]},
        {"event": "ended", "ts": 20, "seconds": 30, "label": "a", "problems": ["stuck"]},
        {"event": "ended", "ts": 30, "seconds": 8, "label": "b", "problems": ["memory"]},
        {"event": "ended", "ts": 2, "seconds": 999, "label": "old"},
        {"event": "ended", "ts": "bad", "seconds": 1, "label": "c"},
    ]
    stats = health.summarize(records, since=5)
    assert [s.label for s in stats] == ["a", "b"]
    a = stats[0]
    assert (a.count, a.seconds, a.longest, a.last) == (2, 35, 30, 20)
    assert a.problems == (health.STUCK, health.IO)


def test_try_lock_is_exclusive(tmp_path):
    import os

    path = tmp_path / "state" / "health.lock"
    first = health.try_lock(path)
    assert first is not None
    try:
        assert health.try_lock(path) is None
    finally:
        os.close(first)
    second = health.try_lock(path)
    assert second is not None
    os.close(second)


# ---------------------------------------------------------------- tmux 字段


def test_parse_panes_reads_pid():
    fields = ["%1", "main", "1", "w", "0", "bash", "/tmp", "1", "0", "1", "80", "24", "t"]
    line = "\x1f".join([*fields, "@1", "0", "", "4242"])
    assert tmux.parse_panes(line)[0].pid == 4242


def test_parse_panes_without_pid_field():
    """老格式（没有 pane_pid）照样能解析，pid 取 0。"""
    fields = ["%1", "main", "1", "w", "0", "bash", "/tmp", "1", "0", "1", "80", "24", "t"]
    assert tmux.parse_panes("\x1f".join(fields))[0].pid == 0


# ---------------------------------------------------------------- atm health 命令


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ATM_LANG", "en")
    return tmp_path / "state" / "atm" / "health.jsonl"


def _pane(pane_id="%1", pid=100):
    return tmux.Pane(
        id=pane_id,
        session="main",
        window_index=1,
        window_name="w",
        pane_index=2,
        current_command="claude",
        current_path="/tmp",
        active=False,
        in_mode=False,
        window_active=True,
        width=80,
        height=24,
        title="task",
        pid=pid,
    )


def test_cmd_health_nothing_running(state, capsys):
    from atm import cli

    assert cli.main(["health"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "no running panes" in out
    assert "no records yet" in out


def test_cmd_health_reports_problem_and_history(state, monkeypatch, capsys):
    from atm import cli

    monkeypatch.setattr(cli, "_health_panes", lambda: (_pane(),))
    stuck = health.PaneHealth(
        pane_id="%1",
        cgroups=("/x",),
        stuck=(health.StuckProc(7, "git", "folio_wait_bit_common"),),
    )
    monkeypatch.setattr(health, "snapshot", lambda panes: {"%1": stuck})
    now = time.time()
    health.append_log(
        state,
        {"event": "ended", "ts": now - 60, "seconds": 42, "label": "proj", "problems": ["io"]},
    )
    health.append_log(
        state, {"event": "ended", "ts": now - 30 * 86400, "seconds": 1, "label": "old"}
    )

    assert cli.main(["health"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "⚠ main:1.2 claude task" in out
    assert "7 git @ folio_wait_bit_common" in out
    assert "proj  1x, total 42s, longest 42s" in out
    assert "old" not in out  # 超出 --days


def test_cmd_health_json(state, monkeypatch, capsys):
    from atm import cli

    monkeypatch.setattr(cli, "_health_panes", lambda: (_pane(),))
    monkeypatch.setattr(
        health, "snapshot", lambda panes: {"%1": health.PaneHealth("%1", io=health.Psi(50, 30))}
    )
    assert cli.main(["health", "--json"]) == cli.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["panes"][0]["label"] == "main:1.2"
    assert data["panes"][0]["problems"] == ["io"]
    assert data["history"] == []


def test_cmd_health_hides_healthy_panes_unless_all(state, monkeypatch, capsys):
    from atm import cli

    monkeypatch.setattr(cli, "_health_panes", lambda: (_pane(),))
    monkeypatch.setattr(
        health, "snapshot", lambda panes: {"%1": health.PaneHealth("%1", cgroups=("/x",))}
    )
    cli.main(["health"])
    assert "main:1.2" not in capsys.readouterr().out
    cli.main(["health", "--all"])
    assert "main:1.2" in capsys.readouterr().out


# ---------------------------------------------------------------- atm health --watch


def test_alerts_only_for_new_problems():
    started = health.Change("started", health.Episode("%1", "proj", 0, {health.IO, health.STUCK}))
    ended = health.Change("ended", health.Episode("%2", "other", 0, {health.IO}), 5)
    texts = health.alerts([started, ended])
    assert len(texts) == 1
    assert "proj" in texts[0]
    assert health.describe(health.STUCK) in texts[0]  # 按严重程度挑第一个说


class _Stop(Exception):
    pass


@pytest.fixture
def watch_env(state, monkeypatch):
    """跑 _health_watch 的假环境：tmux 调用、采样、sleep 全部替换，第 N 次 sleep 时停下。"""
    from atm import cli

    env = {"panes": [(_pane(),)], "health": [], "shown": [], "sleeps": 0, "stop_after": 3}

    def list_panes():
        item = env["panes"].pop(0) if len(env["panes"]) > 1 else env["panes"][0]
        if isinstance(item, Exception):
            raise item
        return item

    def sample(panes, previous=None):
        return env["health"].pop(0) if env["health"] else {}

    def sleep(_seconds):
        env["sleeps"] += 1
        if env["sleeps"] >= env["stop_after"]:
            raise _Stop

    monkeypatch.setattr(cli.tmux, "list_panes", list_panes)
    monkeypatch.setattr(cli.tmux, "display_message_all", env["shown"].append)
    monkeypatch.setattr(health, "sample", sample)
    monkeypatch.setattr("time.sleep", sleep)
    return env


def test_watch_announces_and_records(watch_env, state):
    from atm import cli

    bad = {"%1": health.PaneHealth("%1", io=health.Psi(90, 50))}
    watch_env["health"] = [bad, bad, {"%1": health.PaneHealth("%1")}]
    with pytest.raises(_Stop):
        cli._health_watch()
    assert len(watch_env["shown"]) == 1  # 卡了两轮只提醒一次
    assert [r["event"] for r in health.read_log(state)] == ["started", "ended"]


def test_watch_stays_quiet_when_sidebar_is_recorder(watch_env, state):
    """侧栏已经是记录员：盯梢进程不重复提醒、不重复记。"""
    import os

    from atm import cli

    held = health.try_lock(state.with_name("health.lock"))
    try:
        watch_env["health"] = [{"%1": health.PaneHealth("%1", io=health.Psi(90, 50))}]
        with pytest.raises(_Stop):
            cli._health_watch()
    finally:
        os.close(held)
    assert watch_env["shown"] == []
    assert not state.exists()


def test_watch_single_instance(watch_env, state):
    import os

    from atm import cli

    held = health.try_lock(state.with_name("watch.lock"))
    try:
        assert cli._health_watch() == cli.EXIT_OK  # 立刻退出，一次都没采
    finally:
        os.close(held)
    assert watch_env["sleeps"] == 0


def test_watch_exits_when_tmux_server_is_gone(watch_env):
    from atm import cli

    gone = tmux.TmuxError("no server running")
    watch_env["panes"] = [gone, gone, gone, gone]
    watch_env["stop_after"] = 99
    assert cli._health_watch() == cli.EXIT_OK
    assert watch_env["sleeps"] == cli._WATCH_GIVE_UP - 1


def test_watch_reexecs_after_upgrade(watch_env, monkeypatch):
    from atm import cli

    stamps = iter([1.0, 1.0, 2.0])
    monkeypatch.setattr(cli, "_mtime", lambda path: next(stamps))

    class _Reexec(Exception):
        pass

    def reexec():
        raise _Reexec

    monkeypatch.setattr(cli, "_reexec_watch", reexec)
    with pytest.raises(_Reexec):
        cli._health_watch()


@pytest.fixture
def hint_env(state, tmp_path, monkeypatch, _no_real_pane_health):
    from atm import cli, config

    monkeypatch.setattr(config, "load", lambda: config.Config())
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(cli.tmux, "has_server", lambda: True)
    return _no_real_pane_health, tmp_path / ".tmux.conf"


def test_watch_hint_silent_without_atm_block(hint_env):
    hint, conf = hint_env
    conf.write_text("set -g mouse on\n", encoding="utf-8")
    assert hint() is None


def test_watch_hint_old_block_needs_install(hint_env):
    from atm.install import MARKER_BEGIN, MARKER_END

    hint, conf = hint_env
    conf.write_text(f"{MARKER_BEGIN}\nbind-key a run-shell x\n{MARKER_END}\n", encoding="utf-8")
    assert "atm install -y" in hint()


def test_watch_hint_running_vs_not(hint_env, state):
    import os

    from atm import install

    hint, conf = hint_env
    install.apply(install.build_plan(conf_path=conf), live=False)
    assert "atm install -y" in hint()  # 块里有了，但没人拿着 watch.lock
    held = health.try_lock(state.with_name("watch.lock"))
    try:
        assert hint() is None
    finally:
        os.close(held)


def test_watch_survives_failed_reexec(watch_env, monkeypatch):
    from atm import cli

    monkeypatch.setattr(cli, "_mtime", lambda path: object())  # 每次都「变了」
    tries = []

    def reexec():
        tries.append(1)
        raise FileNotFoundError("python gone mid-upgrade")

    monkeypatch.setattr(cli, "_reexec_watch", reexec)
    with pytest.raises(_Stop):
        cli._health_watch()
    assert len(tries) == watch_env["stop_after"]  # 每一轮都重试，但一直在采样


# ---------------------------------------------------------------- 格子状态栏


def test_border_text():
    assert health.border_text(None) == ""
    assert health.border_text(health.PaneHealth("%1")) == ""  # 没读到 cgroup：不假装健康
    ok = health.PaneHealth("%1", cgroups=("/x",))
    assert health.border_text(ok) == "#[fg=green]✓#[default]"
    stuck = health.PaneHealth(
        "%1",
        cgroups=("/x",),
        reclaim_rate=464,
        stuck=(health.StuckProc(7, "we#ird", "x"),),
    )
    # 最严重的是卡D；进程名里的 # 要双写，否则会被 tmux 当格式
    assert health.border_text(stuck) == "#[fg=red,bold]⚠卡D#[default] D we##ird"
    reclaim = health.PaneHealth("%1", cgroups=("/x",), reclaim_rate=464.2)
    assert health.border_text(reclaim).endswith("high 464/s")


def test_watch_publishes_border_only_when_changed(watch_env, monkeypatch):
    from atm import cli

    sets = []
    monkeypatch.setattr(
        cli.tmux, "set_pane_user_option", lambda p, n, v: sets.append((p, n, v)) or True
    )
    ok = {"%1": health.PaneHealth("%1", cgroups=("/x",))}
    bad = {"%1": health.PaneHealth("%1", cgroups=("/x",), io=health.Psi(90, 50))}
    watch_env["health"] = [ok, ok, bad]
    with pytest.raises(_Stop):
        cli._health_watch()
    assert [v for _p, _n, v in sets] == [
        "#[fg=green]✓#[default]",
        "#[fg=red,bold]⚠IO#[default] io 90%",
    ]
    assert {n for _p, n, _v in sets} == {health.BORDER_OPTION}


def test_toggle_border_saves_and_restores(monkeypatch, _no_real_pane_health):
    """开的时候记下用户原来的边框设置，关的时候原样还回去。"""
    from atm import cli

    options = {"pane-border-status": "off", "pane-border-format": "USER FORMAT"}

    def run(args, **kw):
        if args[:2] == ["show-options", "-gqv"]:
            return options.get(args[2], "") + "\n"
        if args[:2] == ["set-option", "-g"]:
            options[args[2]] = args[3]
        elif args[:2] == ["set-option", "-gu"]:
            options.pop(args[2], None)
        return ""

    monkeypatch.setattr(cli.tmux, "has_server", lambda: True)
    monkeypatch.setattr(cli.tmux, "run", run)
    monkeypatch.setattr(cli.tmux, "display_message_all", lambda text: 1)
    before = dict(options)

    assert cli.main(["health", "--toggle-border"]) == cli.EXIT_OK
    assert options["pane-border-status"] == "top"
    assert health.BORDER_OPTION in options["pane-border-format"]
    assert options["@atm_border"] == "1"

    assert cli.main(["health", "--toggle-border"]) == cli.EXIT_OK
    assert options == before
