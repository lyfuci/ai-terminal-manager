"""格子健康：哪个 pane 正在被拖慢 / 卡住，以及卡过几次、卡了多久。

起因：跑某些命令时一个格子会「卡死」—— 进程活着、不报错、就是不动。常见的三种样子：

- **内存回收**：撞到 MemoryHigh（atm 的闸门或更外层的 slice），每次分配都要先同步回收。
- **IO 等待**：进程在等盘 / 9P（WSL 访问 /mnt/c）/ 网络文件系统。
- **D 状态**：进程停在内核里不可中断睡眠，Ctrl-C 都叫不醒 —— 用户看到的「卡死」多半是它。

怎么判断，全部来自内核现成的数，不自己猜：

- 每个 tmux pane 在 systemd 下是独立的 `tmux-spawn-*.scope`，atm 投递的会话在 slice 里有
  自己的 `run-*.scope`。所以**一格一个 cgroup**，cgroup v2 的 PSI（`memory.pressure` /
  `io.pressure` / `cpu.pressure`）就是「这一格有多少时间在干等」。`avg10` 是内核算好的
  10 秒滑动平均，本身就是去抖过的。
- **回收速率**看 `memory.events` 里 `high` 计数涨得多快（这一格的 cgroup 和它往上每一层）。
  这条是实测补的：MemoryHigh=64M 的 scope 里反复摸 200M 内存，进程慢了约 600 倍，
  CPU 几乎全耗在内核回收上（stime 占满），可 memory PSI 只有 2%、io PSI 10% ——
  回收是在「干活」而不是「等」，PSI 不算它。high 事件却每秒涨 400 多次。
- D 状态看 `/proc/<pid>/stat` 的第三个字段。偶尔一瞬间的 D 很正常（读盘），所以要求
  **同一个进程连着两次采样都在 D** 才算卡住。

这里只读 /proc 和 /sys/fs/cgroup，不碰 tmux —— pane 列表由调用方给。读不到任何东西都
当「没有数据」，诊断绝不能自己抛。
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import _

# 阈值都是 PSI avg10 的百分比（最近 10 秒里有多少时间在干等）。
# 取值依据：内核文档把 some > 10% 视为「开始能感觉到」；这里 IO 用更宽的线，因为编译、
# git 这类正常命令就会让 io some 冲到二三十。
MEMORY_SOME = 10.0
IO_SOME = 40.0
IO_FULL = 20.0
CPU_SOME = 60.0
# memory.high 每秒被撞多少次算「在回收里打转」。没撞限制时这个数是 0；实测卡住时 400+。
RECLAIM_PER_SECOND = 20.0

# 问题代码。放进日志里的是这几个字面值，和界面语言无关。
MEMORY = "memory"
IO = "io"
CPU = "cpu"
STUCK = "stuck"
OVER_HIGH = "over-high"
RECLAIM = "reclaim"

CODES: tuple[str, ...] = (STUCK, RECLAIM, OVER_HIGH, MEMORY, IO, CPU)

# 盯梢进程把每格的状态写进这个 pane 选项，格子状态栏（prefix + m）的格式串引用它。
BORDER_OPTION = "@atm_health"

_CGROUP_ROOT = Path("/sys/fs/cgroup")
_PROC_ROOT = Path("/proc")

# 日志超过这个大小就轮转成 .1，只留一代。一条几百字节，够记上万次。
_LOG_MAX_BYTES = 1 << 20


def short_label(code: str) -> str:
    """侧栏里 32 列放得下的短标签。"""
    return {
        STUCK: _("卡D"),
        OVER_HIGH: _("超限"),
        RECLAIM: _("回收"),
        MEMORY: _("内存"),
        IO: "IO",
        CPU: "CPU",
    }.get(code, code)


def describe(code: str) -> str:
    """doctor / atm health 里的一句话解释。"""
    return {
        STUCK: _("进程卡在不可中断睡眠（D 状态），Ctrl-C 叫不醒"),
        OVER_HIGH: _("内存超过软上限，正被同步回收拖慢"),
        RECLAIM: _("反复撞内存软上限，CPU 全耗在回收上"),
        MEMORY: _("在等内存回收"),
        IO: _("在等磁盘 / 文件系统"),
        CPU: _("抢不到 CPU"),
    }.get(code, code)


# ---------------------------------------------------------------- 原始读数


@dataclass(frozen=True, slots=True)
class Psi:
    """一个 `*.pressure` 文件。百分比是 avg10。"""

    some: float
    full: float


def read_psi(path: Path) -> Psi | None:
    """`some avg10=1.23 avg60=... total=...` / `full ...`。读不到 / 格式不对返回 None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    values: dict[str, float] = {}
    for line in text.splitlines():
        kind, _sep, rest = line.partition(" ")
        for item in rest.split():
            key, _eq, raw = item.partition("=")
            if key == "avg10":
                with contextlib.suppress(ValueError):
                    values[kind] = float(raw)
    if "some" not in values:
        return None
    # cpu.pressure 在老内核上没有 full 行
    return Psi(some=values["some"], full=values.get("full", 0.0))


@dataclass(frozen=True, slots=True)
class Proc:
    pid: int
    ppid: int
    state: str
    comm: str


def _read_stat(path: Path) -> Proc | None:
    """`/proc/<pid>/stat`。comm 里可以有空格和括号，所以从**最后一个** `)` 往后切。"""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    head, sep, tail = raw.rpartition(")")
    if not sep:
        return None
    pid_str, _paren, comm = head.partition(" (")
    fields = tail.split()
    if len(fields) < 2 or not pid_str.strip().isdigit() or not fields[1].isdigit():
        return None
    return Proc(pid=int(pid_str), ppid=int(fields[1]), state=fields[0], comm=comm)


def process_table(proc_root: Path = _PROC_ROOT) -> dict[int, Proc]:
    """当前所有进程。实测本机一次全扫 < 1ms（几百个进程）。"""
    table: dict[int, Proc] = {}
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return table
    for entry in entries:
        if entry.name.isdigit():
            proc = _read_stat(entry / "stat")
            if proc is not None:
                table[proc.pid] = proc
    return table


def descendants(table: Mapping[int, Proc], root: int) -> list[int]:
    """root 自己 + 所有后代。root 不在表里（进程已退出）返回空。"""
    if root not in table:
        return []
    children: dict[int, list[int]] = {}
    for proc in table.values():
        children.setdefault(proc.ppid, []).append(proc.pid)
    out: list[int] = []
    stack = [root]
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
        stack.extend(children.get(pid, ()))
    return out


def cgroup_of(pid: int, proc_root: Path = _PROC_ROOT) -> str | None:
    """cgroup v2 路径（`0::/user.slice/...` 里的那段）。v1 / 读不到返回 None。"""
    try:
        text = (proc_root / str(pid) / "cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("0::"):
            path = line[3:].strip()
            return path or None
    return None


def _wchan(pid: int, proc_root: Path) -> str:
    """进程停在哪个内核函数上。拿不到（权限 / 内核没开）就空着。"""
    try:
        value = (proc_root / str(pid) / "wchan").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return "" if value == "0" else value


def _cgroup_int(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def _ancestors(cgroups: Iterable[str]) -> list[str]:
    """这些 cgroup 自己 + 往上每一层（不含根）。限制可能设在任何一层。"""
    out: dict[str, None] = {}
    for cgroup in cgroups:
        parts = [p for p in cgroup.split("/") if p]
        while parts:
            out.setdefault("/".join(parts), None)
            parts.pop()
    return list(out)


def _high_events(directory: Path, *, leaf: bool) -> int | None:
    """这一层**自己的** memory.high 被撞了几次。

    必须读 `memory.events.local`：`memory.events` 是层级累计的，子 cgroup 撞自己的限制
    也会算到每一层祖先头上 —— 实测一个撞限的 scope 会让同一 user@ 下所有格子都报「回收」。
    老内核（< 5.7）没有 .local，只对格子自己那层退回 memory.events。
    """
    names = ("memory.events.local", "memory.events") if leaf else ("memory.events.local",)
    text = None
    for name in names:
        try:
            text = (directory / name).read_text(encoding="utf-8")
            break
        except OSError:
            continue
    if text is None:
        return None
    for line in text.splitlines():
        key, _sep, value = line.partition(" ")
        if key == "high" and value.strip().isdigit():
            return int(value)
    return None


def _over_high(cgroup: str, cgroup_root: Path) -> str | None:
    """从这个 cgroup 往上逐层看，哪一层此刻在自己的 memory.high 之上。

    atm 的会话 scope 自己可能没限制，限制在外面那层 slice 上 —— 所以不能只看一层。
    """
    parts = [p for p in cgroup.split("/") if p]
    while parts:
        directory = cgroup_root.joinpath(*parts)
        high = _cgroup_int(directory / "memory.high")  # "max" → None
        current = _cgroup_int(directory / "memory.current")
        if high is not None and current is not None and current > high:
            return parts[-1]
        parts.pop()
    return None


# ---------------------------------------------------------------- 一格的体检


@dataclass(frozen=True, slots=True)
class StuckProc:
    pid: int
    comm: str
    wchan: str


@dataclass(frozen=True, slots=True)
class PaneHealth:
    pane_id: str
    cgroups: tuple[str, ...] = ()
    memory: Psi | None = None
    io: Psi | None = None
    cpu: Psi | None = None
    over_high: str | None = None  # 超限的那一层 cgroup 名
    blocked: tuple[StuckProc, ...] = ()  # 这一次采样里处在 D 状态的进程
    stuck: tuple[StuckProc, ...] = ()  # 连续两次都在 D —— 真卡住
    # 各层 cgroup 的 memory.events high 计数，和采样时刻（monotonic）—— 下一次算速率用
    high_events: tuple[tuple[str, int], ...] = ()
    taken: float = 0.0
    reclaim_rate: float = 0.0  # 每秒 high 事件，取涨得最快的那一层
    reclaim_at: str = ""  # 那一层的名字

    @property
    def problems(self) -> tuple[str, ...]:
        found: list[str] = []
        if self.stuck:
            found.append(STUCK)
        if self.reclaim_rate >= RECLAIM_PER_SECOND:
            found.append(RECLAIM)
        if self.over_high:
            found.append(OVER_HIGH)
        if self.memory is not None and self.memory.some >= MEMORY_SOME:
            found.append(MEMORY)
        if self.io is not None and (self.io.some >= IO_SOME or self.io.full >= IO_FULL):
            found.append(IO)
        if self.cpu is not None and self.cpu.some >= CPU_SOME:
            found.append(CPU)
        return tuple(found)

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        """一行读数，doctor / atm health / 侧栏底栏共用。"""
        bits = []
        for name, psi in (("mem", self.memory), ("io", self.io), ("cpu", self.cpu)):
            if psi is not None:
                bits.append(f"{name} {psi.some:.0f}%")
        if self.reclaim_rate >= 1:
            bits.append(f"high {self.reclaim_rate:.0f}/s({self.reclaim_at})")
        if self.over_high:
            bits.append(f">high({self.over_high})")
        procs = self.stuck or self.blocked
        if procs:
            names = ",".join(sorted({p.comm for p in procs}))
            bits.append(f"D:{len(procs)}({names})")
        return " ".join(bits)

    def to_json(self) -> dict:
        def psi(p: Psi | None) -> dict | None:
            return None if p is None else {"some": p.some, "full": p.full}

        return {
            "pane": self.pane_id,
            "problems": list(self.problems),
            "memory": psi(self.memory),
            "io": psi(self.io),
            "cpu": psi(self.cpu),
            "overHigh": self.over_high,
            "reclaimPerSecond": round(self.reclaim_rate, 1),
            "reclaimAt": self.reclaim_at or None,
            "stuck": [{"pid": p.pid, "comm": p.comm, "wchan": p.wchan} for p in self.stuck],
            "cgroups": list(self.cgroups),
        }


def _max_psi(values: Iterable[Psi | None]) -> Psi | None:
    found = [v for v in values if v is not None]
    if not found:
        return None
    return Psi(some=max(v.some for v in found), full=max(v.full for v in found))


def sample(
    panes: Sequence[tuple[str, int]],
    *,
    previous: Mapping[str, PaneHealth] | None = None,
    proc_root: Path = _PROC_ROOT,
    cgroup_root: Path = _CGROUP_ROOT,
) -> dict[str, PaneHealth]:
    """给每个 (pane_id, pane_pid) 采一次样。

    `previous` 是上一次的结果：同一个 pid 上次也在 D，这次才记进 `stuck`；
    high 事件的速率也要拿上一次的计数来减。
    一格里的进程可能分在几个 cgroup（atm 投递的会话被 systemd-run 挪进了 slice），
    PSI 取各 cgroup 的最大值 —— 哪一块卡住，这一格就算卡住。
    """
    table = process_table(proc_root)
    taken = time.monotonic()
    result: dict[str, PaneHealth] = {}
    for pane_id, pid in panes:
        if pid <= 0:
            continue
        pids = descendants(table, pid)
        if not pids:
            continue
        cgroups = tuple(dict.fromkeys(c for c in (cgroup_of(p, proc_root) for p in pids) if c))
        leaves = {"/".join(p for p in c.split("/") if p) for c in cgroups}
        dirs = [cgroup_root.joinpath(*[p for p in c.split("/") if p]) for c in cgroups]
        blocked = tuple(
            StuckProc(pid=p, comm=table[p].comm, wchan=_wchan(p, proc_root))
            for p in pids
            if table[p].state == "D"
        )
        before = previous.get(pane_id) if previous else None
        earlier = {p.pid for p in before.blocked} if before else set()
        counts: dict[str, int] = {}
        for cgroup in _ancestors(cgroups):
            n = _high_events(cgroup_root.joinpath(*cgroup.split("/")), leaf=cgroup in leaves)
            if n is not None:
                counts[cgroup] = n
        rate, at = 0.0, ""
        if before is not None and taken > before.taken:
            old = dict(before.high_events)
            for cgroup, n in counts.items():
                if cgroup in old and n >= old[cgroup]:
                    r = (n - old[cgroup]) / (taken - before.taken)
                    if r > rate:
                        rate, at = r, cgroup.rsplit("/", 1)[-1]
        result[pane_id] = PaneHealth(
            pane_id=pane_id,
            cgroups=cgroups,
            memory=_max_psi(read_psi(d / "memory.pressure") for d in dirs),
            io=_max_psi(read_psi(d / "io.pressure") for d in dirs),
            cpu=_max_psi(read_psi(d / "cpu.pressure") for d in dirs),
            over_high=next((o for o in (_over_high(c, cgroup_root) for c in cgroups) if o), None),
            blocked=blocked,
            stuck=tuple(p for p in blocked if p.pid in earlier),
            high_events=tuple(counts.items()),
            taken=taken,
            reclaim_rate=rate,
            reclaim_at=at,
        )
    return result


def snapshot(
    panes: Sequence[tuple[str, int]], *, interval: float = 1.0, **roots: Path
) -> dict[str, PaneHealth]:
    """一次性体检：采两次样（隔 `interval` 秒）—— 分清「瞬间的 D」和「卡住的 D」，
    也才算得出回收速率。"""
    first = sample(panes, **roots)
    if not first:
        return first
    time.sleep(interval)
    return sample(panes, previous=first, **roots)


# ---------------------------------------------------------------- 持续跟踪 + 统计日志


@dataclass(slots=True)
class Episode:
    """一格从「出问题」到「恢复」的一段。"""

    pane_id: str
    label: str
    started: float  # time.time()
    problems: set[str] = field(default_factory=set)
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Change:
    """一次状态变化：`started` 或 `ended`。调用方据此提醒用户。"""

    kind: str
    episode: Episode
    duration: float = 0.0


class Tracker:
    """侧栏每次采样都喂进来；进 / 出问题状态时产生 Change，并写统计日志。

    日志是 JSON Lines，一段问题一条 start、一条 end —— 事后能回答「哪格、什么时候、
    卡了多久、卡在什么上」。
    """

    def __init__(self, log: Path | None = None) -> None:
        self.log = log
        self.open: dict[str, Episode] = {}
        self._last: dict[str, PaneHealth] = {}

    @property
    def last(self) -> Mapping[str, PaneHealth]:
        return self._last

    def update(
        self, health: Mapping[str, PaneHealth], labels: Mapping[str, str], *, now: float
    ) -> list[Change]:
        changes: list[Change] = []
        for pane_id, h in health.items():
            episode = self.open.get(pane_id)
            if h.problems and episode is None:
                episode = Episode(
                    pane_id=pane_id,
                    label=labels.get(pane_id, pane_id),
                    started=now,
                    problems=set(h.problems),
                    detail=h.summary(),
                )
                self.open[pane_id] = episode
                changes.append(Change("started", episode))
            elif h.problems and episode is not None:
                episode.problems.update(h.problems)
                episode.detail = h.summary()
            elif episode is not None:
                changes.append(self._close(pane_id, now))
        # 格子关掉了（不在这次的结果里）也要把那一段收尾
        for pane_id in [p for p in self.open if p not in health]:
            changes.append(self._close(pane_id, now))
        self._last = dict(health)
        for change in changes:
            self._write(change, now)
        return changes

    def _close(self, pane_id: str, now: float) -> Change:
        episode = self.open.pop(pane_id)
        return Change("ended", episode, duration=max(0.0, now - episode.started))

    def _write(self, change: Change, now: float) -> None:
        if self.log is None:
            return
        e = change.episode
        record = {
            "ts": round(now, 3),
            "event": change.kind,
            "pane": e.pane_id,
            "label": e.label,
            "problems": sorted(e.problems),
            "detail": e.detail,
        }
        if change.kind == "ended":
            record["seconds"] = round(change.duration, 1)
        append_log(self.log, record)


def log_path() -> Path:
    from .restore import state_dir

    return state_dir() / "health.jsonl"


def try_lock(path: Path) -> int | None:
    """抢「记录员」锁：拿到返回 fd（进程退出自动释放），被占返回 None。

    每个 window 都可能开一个侧栏；不加锁的话同一次卡顿会被记 N 遍、提醒 N 遍。
    """
    import fcntl

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def append_log(path: Path, record: dict) -> None:
    """追加一条；超过上限先轮转。写不进去就算了 —— 统计不能拖垮侧栏。"""
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _LOG_MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_log(path: Path) -> list[dict]:
    """读统计日志（含轮转出去的上一代）。坏行跳过。"""
    records: list[dict] = []
    for p in (path.with_name(path.name + ".1"), path):
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            with contextlib.suppress(ValueError):
                record = json.loads(line)
                if isinstance(record, dict) and record.get("event") in ("started", "ended"):
                    records.append(record)
    return records


@dataclass(frozen=True, slots=True)
class Stat:
    label: str
    count: int
    seconds: float
    longest: float
    last: float
    problems: tuple[str, ...]


def summarize(records: Iterable[dict], *, since: float = 0.0) -> list[Stat]:
    """按格子名汇总已结束的问题段：几次、合计多久、最长一次、最近一次。

    按 label 而不是 pane id 聚合：pane id 重启就变，「哪个项目 / 哪个任务老卡」才是
    用户关心的。按合计时长倒序。
    """
    groups: dict[str, list[dict]] = {}
    for r in records:
        if r.get("event") != "ended":
            continue
        ts = r.get("ts")
        seconds = r.get("seconds")
        if not isinstance(ts, int | float) or not isinstance(seconds, int | float) or ts < since:
            continue
        groups.setdefault(str(r.get("label") or r.get("pane") or "?"), []).append(r)
    stats = []
    for label, rs in groups.items():
        problems: dict[str, None] = {}
        for r in rs:
            for p in r.get("problems") or ():
                problems.setdefault(str(p), None)
        stats.append(
            Stat(
                label=label,
                count=len(rs),
                seconds=sum(float(r["seconds"]) for r in rs),
                longest=max(float(r["seconds"]) for r in rs),
                last=max(float(r["ts"]) for r in rs),
                problems=tuple(c for c in CODES if c in problems)
                + tuple(p for p in problems if p not in CODES),
            )
        )
    stats.sort(key=lambda s: s.seconds, reverse=True)
    return stats


def problem_order(problems: Iterable[str]) -> tuple[str, ...]:
    """按严重程度排：卡D > 回收 > 超限 > 内存 > IO > CPU。"""
    found = set(problems)
    return tuple(c for c in CODES if c in found) + tuple(sorted(found - set(CODES)))


def alerts(changes: Iterable[Change]) -> list[str]:
    """新出问题的格子各一句提醒（给 tmux 状态栏）。恢复不提醒 —— 那会把提醒刷成噪音。"""
    from .text import truncate_display

    out = []
    for change in changes:
        if change.kind != "started" or not change.episode.problems:
            continue
        episode = change.episode
        out.append(
            _("atm: ⚠ {label} —— {what}（atm health 看详情）").format(
                label=truncate_display(episode.label, 30),
                what=describe(problem_order(episode.problems)[0]),
            )
        )
    return out


def _tmux_escape(text: str) -> str:
    """tmux 格式里 `#` 是转义起点；进程名之类的外来文本要把它双写。"""
    return text.replace("#", "##")


def border_text(h: PaneHealth | None) -> str:
    """格子顶边右侧显示的那一小段（带 tmux 样式），存进 pane 选项 `@atm_health`。

    必须短：顶边还要放格子编号和标题。只说最严重的那一个问题和它的关键读数。
    """
    if h is None or not h.cgroups:
        return ""  # 没数据就什么都不显示，不假装健康
    problems = problem_order(h.problems)
    if not problems:
        return "#[fg=green]✓#[default]"
    code = problems[0]
    reading = ""
    if code == STUCK and h.stuck:
        reading = f"D {h.stuck[0].comm}"
    elif code == RECLAIM:
        reading = f"high {h.reclaim_rate:.0f}/s"
    elif code == OVER_HIGH and h.over_high:
        reading = f">high {h.over_high}"
    elif code == MEMORY and h.memory is not None:
        reading = f"mem {h.memory.some:.0f}%"
    elif code == IO and h.io is not None:
        reading = f"io {max(h.io.some, h.io.full):.0f}%"
    elif code == CPU and h.cpu is not None:
        reading = f"cpu {h.cpu.some:.0f}%"
    tag = f"#[fg=red,bold]⚠{_tmux_escape(short_label(code))}#[default]"
    return f"{tag} {_tmux_escape(reading)}".rstrip()
