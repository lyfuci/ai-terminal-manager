"""`atm restore`：把上次那些会话一键填回**已经恢复出来的空格子**里。

## 它解决什么

tmux-resurrect 重启后能把布局搭回来（窗口、分格、每格 cwd），但格子里是空 shell。
把 AI CLI 加进 `@resurrect-processes` 能让 resurrect 自己拉起它们，可那是**开机时全砸下去**：
2026-08-12 就是这么把机器冻死的（四个会话合计吃掉 87% 内存）。所以 atm 一直不用那条路。

代价是用户得一个一个手动 resume。窗口一多就很烦 —— 这个命令就是补这个缺口。

## 为什么 atm 做比 resurrect 做安全

- **只填空闲的格子。** 格子里在跑东西就跳过，绝不覆盖你正在用的会话。
- **串行，不并发。** 三个 claude 同时读各自 20MB+ 的转录会造成尖峰；一条一条来最坏只是慢。
- **走 atm 正常的投递路径**，所以每条都套上 cgroup 内存闸门，也会做 cwd 存在性检查和体积警告。
- **默认先给你看计划**，确认了才动手。

## 数据从哪来

复用 resurrect 已经在写的存档（默认每 10 分钟一次），不再造第二份状态。
格式是**逆向观察**的（仓库硬规则第 4 条），所以解析全程「拿不到就跳过这一行」，
字段数变了、列错位了都只是少恢复几条，不会让整条命令炸掉。

实测的 pane 行（制表符分隔）：

    pane  main  1  1  :*  1  ✳ github  :/home/sean  1  claude  :/home/…/claude --resume <uuid>
     0     1    2  3   4  5      6           7      8     9                  10
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import config as config_mod
from . import tmux
from .dispatch import RESUME_PROGRAMS, DispatchError, DispatchTarget, MemoryLimit, dispatch
from .i18n import _
from .model import SessionEntry, Source
from .tmux import Pane

_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
_MEMINFO = Path("/proc/meminfo")

# 存档里 pane 行至少要有这么多列才敢解析
_MIN_FIELDS = 11
_SESSION_FIELD, _WINDOW_FIELD, _PANE_FIELD = 1, 2, 5
_TITLE_FIELD, _CWD_FIELD, _COMMAND_FIELD = 6, 7, 10


@dataclass(frozen=True, slots=True)
class SavedPane:
    """存档里一行，且能从中认出一条可恢复的会话。"""

    target: str  # "main:1.2"，和 tmux 的目标写法一致
    session: str
    window: str
    pane: str
    title: str
    cwd: str
    source: Source
    session_id: str


@dataclass(frozen=True, slots=True)
class Item:
    """一条恢复计划。`state` 决定它会不会被执行。"""

    saved: SavedPane
    pane_id: str | None  # 对应的活格子；None = 布局里已经没有这一格
    entry: SessionEntry | None  # 索引里的会话；None = 会话文件已经没了
    state: str  # ready / occupied / no-pane / no-session

    @property
    def ready(self) -> bool:
        return self.state == "ready"


def save_path() -> Path:
    from . import persist

    return persist.save_dir() / "last"


def parse_save(text: str) -> tuple[SavedPane, ...]:
    """从存档文本里挑出「能恢复的会话」。认不出的行安静跳过。"""
    out: list[SavedPane] = []
    for line in text.splitlines():
        fields = line.split("\t")
        if not fields or fields[0] != "pane" or len(fields) < _MIN_FIELDS:
            continue
        command = fields[_COMMAND_FIELD].lstrip(":")
        found = _session_of(command)
        if found is None:
            continue
        source, session_id = found
        session = fields[_SESSION_FIELD]
        window = fields[_WINDOW_FIELD]
        pane = fields[_PANE_FIELD]
        if not (session and window and pane):
            continue
        out.append(
            SavedPane(
                target=f"{session}:{window}.{pane}",
                session=session,
                window=window,
                pane=pane,
                title=fields[_TITLE_FIELD],
                cwd=fields[_CWD_FIELD].lstrip(":"),
                source=source,
                session_id=session_id,
            )
        )
    return tuple(out)


def _session_of(command: str) -> tuple[Source, str] | None:
    """`/path/to/claude --resume <id>` → (Source.CLAUDE, id)。

    恢复参数各家不一样（`--resume` / `resume` / `--session`），统一从 RESUME_PROGRAMS 反查，
    这样加了新来源这里不用改。
    """
    words = command.split()
    if len(words) < 3:
        return None
    program = PurePosixPath(words[0]).name
    for source, argv in RESUME_PROGRAMS.items():
        if argv[0] != program:
            continue
        flag = argv[1]
        if flag in words[1:]:
            index = words.index(flag, 1)
            if index + 1 < len(words):
                return source, words[index + 1]
    return None


def matches(saved: SavedPane, target: str | None) -> bool:
    """`-t` 过滤。None = 全收；`main` = 整个会话；`main:1` = 那个窗口。"""
    if not target:
        return True
    session, _, window = target.partition(":")
    if session and saved.session != session:
        return False
    return not window or saved.window == window


def build_plan(
    saved: tuple[SavedPane, ...],
    panes: tuple[Pane, ...],
    entries: dict[str, SessionEntry],
    *,
    target: str | None = None,
) -> tuple[Item, ...]:
    """把存档、活着的格子、会话索引三者对上，得出每一条的状态。"""
    live = {f"{p.session}:{p.window_index}.{p.pane_index}": p for p in panes}
    items: list[Item] = []
    for entry_saved in saved:
        if not matches(entry_saved, target):
            continue
        pane = live.get(entry_saved.target)
        session = entries.get(entry_saved.session_id)
        if pane is None:
            state = "no-pane"
        elif session is None:
            state = "no-session"
        elif not pane.is_idle_shell:
            state = "occupied"  # 里面在跑东西，绝不覆盖
        else:
            state = "ready"
        items.append(
            Item(
                saved=entry_saved,
                pane_id=pane.id if pane else None,
                entry=session,
                state=state,
            )
        )
    return tuple(items)


def describe(items: tuple[Item, ...]) -> str:
    """给人看的计划。每一条都说清楚为什么做或不做。"""
    if not items:
        return _("存档里没有可以恢复的会话。")
    reasons = {
        "occupied": _("跳过：这个格子里已经在跑东西了"),
        "no-pane": _("跳过：布局里没有这一格了"),
        "no-session": _("跳过：这条会话的记录已经不在了"),
    }
    ready = [i for i in items if i.ready]
    lines = [_("将恢复 {n} 条会话：").format(n=len(ready))] if ready else []
    for item in ready:
        title = item.entry.title if item.entry else item.saved.title
        lines.append(f"  {item.saved.target}  {item.saved.source.value:9} {title}")
    skipped = [i for i in items if not i.ready]
    if skipped:
        lines.append("")
        lines.append(_("以下 {n} 条不动：").format(n=len(skipped)))
        for item in skipped:
            lines.append(f"  {item.saved.target}  {item.saved.title}  —— {reasons[item.state]}")
    return "\n".join(lines)


def execute(
    items: tuple[Item, ...],
    *,
    memory: MemoryLimit | None = None,
    floor: int | None = None,
    meminfo: Path = _MEMINFO,
    on_done: Callable[[int], None] | None = None,
) -> list[str]:
    """按计划逐条投递。**串行**，一条失败不影响后面的。返回每条的结果说明。

    `floor` 是可用内存下限（字节）：每投一条之前看一眼 MemAvailable，掉到下限以下就停手，
    剩下的留给用户自己挑。开机恢复靠这条把「一次性全拉起来」压成「拉到内存吃紧为止」。
    `on_done` 每投完一条调一次（传已投条数），开机模式用它把进度写进状态文件。
    """
    notes: list[str] = []
    done = 0
    for item in items:
        if not item.ready or item.entry is None or item.pane_id is None:
            continue
        if floor is not None:
            have = mem_available(meminfo)
            if have is not None and have < floor:
                notes.append(
                    _("  可用内存只剩 {have}，剩下的先不恢复了。").format(have=_human(have))
                )
                break
        try:
            dispatch(
                item.entry,
                DispatchTarget.existing(item.pane_id),
                focus=False,  # 别把光标抢走：恢复完用户还在原来那格
                memory=memory,
            )
        except DispatchError as exc:
            notes.append(
                _("  {target} 失败：{err}").format(
                    target=item.saved.target, err=str(exc).splitlines()[0]
                )
            )
        else:
            # 纯占位符 + 箭头，没有可翻译的自然语言，所以不过 _()
            notes.append(f"  {item.saved.target} ← {item.entry.title}")
        # 失败的也算「处理过」：状态文件记的是「走到哪了」，不是「成功了几条」。
        done += 1
        if on_done is not None:
            on_done(done)
    return notes


def current_session() -> str | None:
    """当前 attach 的会话名。不在 tmux 里就是 None。"""
    if not tmux.inside_tmux():
        return None
    try:
        return tmux.run(["display-message", "-p", "#{session_name}"]).strip() or None
    except tmux.TmuxError:
        return None


# ---------------------------------------------------------------- 开机自动恢复的闸门
#
# `restore.on-boot` 打开后，resurrect 的 post-restore-all 钩子会调 `atm restore --boot`。
# 这是仓库「不让 resurrect 在开机时批量拉起 AI CLI」那条禁令的边上走 —— 所以必须比它安全，
# 而不是换个地方犯同一个错（2026-08-12：四个会话同时起来吃掉 87% 内存，冻死两次）。
#
# 已经有的三层保护：串行投递、每条套 cgroup 闸门、所有会话共用一个总量 slice。
# 这里再加两层，都是**不用 root、跨重启也成立**的：
#
# 1. **上一次开机恢复有没有跑完。** 投递前先把「计划恢复 N 条」写进状态文件，每投一条 +1。
#    机器被压死时进程是被杀掉的，计数停在半路。下次开机看到 done < planned 就不自动跑了 ——
#    这条专门用来打断「恢复→冻死→重启→再恢复」的循环。
# 2. **可用内存够不够。** 开始前和每条投递前都读一次 MemAvailable，低于 restore.min-available
#    就停手。不管上一次是怎么死的，内存已经紧张时就不该再往里塞。
#
# 为什么不查 journal 里的 OOM 记录：普通用户不在 adm / systemd-journal 组时，
# `journalctl` **只看得到自己的日志**，内核的 oom-kill 是系统消息，查出来永远是「没有」。
# 一个只会返回「一切正常」的检查比没有检查更糟，所以不做。
# 同理 `systemctl --user show tmux.service -p Result` 只在同一次开机内有意义 ——
# user manager 每次登录重建，跨重启读到的是新一轮的结果。


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / "atm"


def state_path() -> Path:
    return state_dir() / "boot-restore.json"


def log_path() -> Path:
    """开机恢复没有终端，做了什么只能写进文件。`atm doctor` 会指这里。"""
    return state_dir() / "restore.log"


def boot_id(path: Path = _BOOT_ID) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def mem_available(meminfo: Path = _MEMINFO) -> int | None:
    """MemAvailable，字节。读不到就是 None —— 拿不到数就不拿它当拒绝的理由。"""
    try:
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


@dataclass(frozen=True, slots=True)
class Attempt:
    """一次开机恢复的痕迹。`done < planned` = 上次没跑完。"""

    boot_id: str
    at: str
    planned: int
    done: int

    @property
    def finished(self) -> bool:
        return self.done >= self.planned


def read_attempt(path: Path | None = None) -> Attempt | None:
    p = path or state_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return Attempt(
            boot_id=str(raw["boot_id"]),
            at=str(raw["at"]),
            planned=int(raw["planned"]),
            done=int(raw["done"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None  # 没有痕迹 / 文件坏了 = 当作没跑过，闸门的其它几条还在


def write_attempt(attempt: Attempt, path: Path | None = None) -> None:
    p = path or state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "boot_id": attempt.boot_id,
                    "at": attempt.at,
                    "planned": attempt.planned,
                    "done": attempt.done,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass  # 写不下痕迹不该挡住恢复本身；代价只是下次少一层保护


@dataclass(frozen=True, slots=True)
class Gate:
    ok: bool
    reason: str


def boot_gate(
    cfg,
    *,
    state: Path | None = None,
    meminfo: Path = _MEMINFO,
    limits_available: bool | None = None,
) -> Gate:
    """开机自动恢复到底跑不跑。每种拒绝都给一句能照着做的理由。"""
    if not cfg.restore_on_boot:
        return Gate(False, _("restore.on-boot 是关的（`atm config restore.on-boot true` 打开）。"))

    from . import dispatch as dispatch_mod

    available = (
        dispatch_mod.memory_limits_available() if limits_available is None else limits_available
    )
    if not available:
        return Gate(
            False,
            _("这台机器拿不到 cgroup 内存闸门，开机批量恢复没有兜底，不自动跑。"),
        )

    last = read_attempt(state)
    if last is not None and not last.finished:
        return Gate(
            False,
            _(
                "上次开机恢复只跑完 {done}/{planned} 条就中断了（多半是内存不够被杀）。"
                "这次不自动恢复，手动跑一次 `atm restore` 确认没问题后，"
                "删掉 {path} 就会重新自动恢复。"
            ).format(done=last.done, planned=last.planned, path=state or state_path()),
        )

    floor = config_mod.size_to_bytes(cfg.restore_min_available)
    have = mem_available(meminfo)
    if floor is not None and have is not None and have < floor:
        return Gate(
            False,
            _("可用内存只剩 {have}，低于 restore.min-available={want}，这次不自动恢复。").format(
                have=_human(have), want=cfg.restore_min_available
            ),
        )
    return Gate(True, _("闸门通过。"))


def _human(n: int) -> str:
    gib = 1 << 30
    return f"{n / gib:.1f}G" if n >= gib else f"{n / (1 << 20):.0f}M"


def append_log(lines: list[str], path: Path | None = None) -> None:
    """开机恢复的流水账。没有终端的时候这是唯一的现场。"""
    p = path or log_path()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(f"{stamp}  {line}\n")
    except OSError:
        pass
