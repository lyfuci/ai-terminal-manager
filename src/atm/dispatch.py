"""把一条历史会话投递到指定的 tmux pane —— 这个项目唯一还站得住的差异点。

核心手势就一行 `tmux send-keys`，但真正要处理的是它周围的三件事：

1. **必须先 cd 回原 cwd** —— 两个 CLI 的会话都是项目作用域的，在别的目录 resume 会找不到会话。
2. **命令要转义** —— cwd 里可能有空格/引号，直接拼字符串会炸（或者更糟：被当成别的命令执行）。
3. **不能往正忙的 pane 里投** —— 那个 pane 里可能正跑着另一个 claude，
   投进去会变成对话内容而不是命令。
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from . import tmux
from .i18n import _
from .model import SessionEntry, Source
from .tmux import Pane, SplitDirection, TmuxError

# ---------------------------------------------------------------- 体积标注
#
# ⚠️ 这里**只做展示，不拦截**。曾经有过一版「≥100MB 必须 --force」的硬闸门，已经删掉。
# 删除理由（2026-08-12 实测，详见 research/notes/2026-08-12-incident.md）：
#
# 1. **Claude Code 自己已经有这道闸门，而且做得更好。** 实测 resume 一条大会话时它会弹：
#        This session is 3h 31m old and 909.6k tokens.
#        Resuming the full session will consume a substantial portion of your usage limits.
#        ❯ 1. Resume from summary (recommended)  2. Resume full session as-is
#    它按 **token** 判断并提供「从摘要恢复」的降级选项；我按**字节**拍脑袋、只会粗暴拒绝。
# 2. **atm 不会绕过它** —— 投递的是交互式命令，那个选择框照常出现。
# 3. **字节数根本不代表代价**：实测 649MB 那条里 83.7% 是已卸载插件的 hook 记录，
#    按字节估 token 会严重高估。20.8MB ↔ 909.6k tokens ≈ 43.7k tok/MB，而大文件远低于这个比例。
# 4. 那版闸门是为一个**已被推翻的事故诊断**加的：崩溃前真正被动过的会话只有 7.4MB，
#    100MB 的阈值根本不会触发。
#
# 保留体积**标注**的理由与事故无关：选之前能看见「这条有多大」是有用信息，
# 而 213 条会话里体积差 6 个数量级（中位数 0.1MB，最大 649MB）。
WARN_BYTES = 20 * 1024 * 1024
BLOCK_BYTES = 100 * 1024 * 1024


class SizeRisk(StrEnum):
    OK = "ok"
    WARN = "warn"
    BLOCK = "block"


def size_risk(entry: SessionEntry) -> SizeRisk:
    """只用于**着色和排版**，不参与任何拦截决策。"""
    if entry.size_bytes >= BLOCK_BYTES:
        return SizeRisk.BLOCK
    if entry.size_bytes >= WARN_BYTES:
        return SizeRisk.WARN
    return SizeRisk.OK


def size_label(entry: SessionEntry) -> str:
    """给 TUI / 列表用的体积标注。小会话返回空串，不加噪音。"""
    risk = size_risk(entry)
    if risk is SizeRisk.OK:
        return ""
    return f"{entry.size_bytes / 1048576:.0f}MB"


def size_notice(entry: SessionEntry) -> str:
    """投递大会话时往 stderr 提一句。**不阻止**，只是让人知道下一步会看到什么。"""
    if size_risk(entry) is SizeRisk.OK:
        return ""
    mb = entry.size_bytes / 1048576
    return _("提示：这条会话的转录有 {mb:.0f}MB，{v0} 可能会先问你要不要「从摘要恢复」。").format(
        mb=mb, v0=RESUME_PROGRAMS.get(entry.source, ("CLI",))[0]
    )


# ---------------------------------------------------------------- 内存闸门
#
# 为什么需要：实测一个持续干活 1h40m 的 claude 会话内存峰值到过 **4.7GB**，
# 四个长会话合计约 8GB —— 正好是本机 WSL 的上限。撞上限的后果不是「那个会话变慢」，
# 而是**整个 tmux server 连同所有会话一起死掉**（2026-08-12 11:59 就这么死过一次）。
# 套上 cgroup 之后，最坏情况从「全丢」变成「丢一个」。
#
# 为什么 High/Max 分开（这是关键）：实测峰值**大多是瞬时尖峰**，
# 同一批会话的 peak→current 回落达 60~75%。所以：
#   - MemoryHigh 是**软**上限：超了只节流 + 强制回收，**不杀进程**；
#   - MemoryMax 是**硬**底线，只在回收也压不住时才动手，拦的是真正失控的那种。
# 只设 MemoryMax 会把正常的尖峰误杀。
#
# ⚠ 2026-09-10 修正过一次严重的定值错误，别再犯：
# 原来两个数写死成 High=2G / Max=4G，依据是「25 个真实会话的 memory.peak 分布里
# 1G 杀掉 12%、2G 只杀 4%」。但那是**「设成多少会杀掉多少」**的分析，
# 却被拿去定 **MemoryHigh** —— 而 MemoryHigh 从来不杀进程，它只节流。
# 同一段实测里还写着单会话峰值到过 **4.7GB**：把软上限设在实测峰值的不到一半，
# 结果是任何一个正常干活的长会话都被**永久限流**（内核在每次分配时同步回收）。
# 现场实测：一个 current=2633M 的会话在 high=2G 下攒了 **227 万次** high 事件，
# 进程活着、不报错、慢到像卡死。48G 内存的机器上不明显（回收几乎免费），
# 小内存机器上直接不可用。
#
# 现在的分工才是对的：
#   - **slice 总量**（guard.py，物理内存 50% / 65%）才是「防机器整体死掉」的那一层；
#   - **单会话 Max** 只负责挑替死鬼 —— 让一个会话去死，而不是全部一起死；
#   - **单会话 High** 贴在 Max 下面（80%），只在真失控时介入，不碰正常工作集。
# 所以两个数都默认 "auto"，按物理内存算（见 suggested_session_limits）。
DEFAULT_MEMORY_HIGH = "auto"
DEFAULT_MEMORY_MAX = "auto"
# 读不到 /proc/meminfo 时的退路。宁松不紧：松了还有 slice 兜总量，紧了就是上面那个 bug。
FALLBACK_MEMORY_HIGH = "6G"
FALLBACK_MEMORY_MAX = "8G"
# swap 单独限死：WSL 的 swap.vhdx 在 Windows 文件系统上，一旦开始刷就是宿主 SSD 100%。
DEFAULT_MEMORY_SWAP_MAX = "512M"

# 单会话 Max 取物理内存的这个比例（下限 4G）；High 取 Max 的 80%。
# 比 slice 的 50% 小得多是故意的：单会话闸门不是总量控制，总量归 slice。
SESSION_MAX_RATIO = 0.35
SESSION_MIN_MAX_GIB = 4
SESSION_HIGH_OF_MAX = 0.8


def total_memory_bytes(meminfo: Path = Path("/proc/meminfo")) -> int | None:
    """物理内存，字节。读不到就是 None（调用方退回写死的值）。"""
    try:
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def suggested_session_limits(total_bytes: int) -> tuple[str, str]:
    """(MemoryHigh, MemoryMax)，单个会话，按 GiB 取整。"""
    gib = 1 << 30
    hard = max(SESSION_MIN_MAX_GIB, round(total_bytes * SESSION_MAX_RATIO / gib))
    soft = max(1, round(hard * SESSION_HIGH_OF_MAX))
    return f"{soft}G", f"{hard}G"


def resolve_session_limits(
    high: str, max_: str, *, total_bytes: int | None = None
) -> tuple[str, str]:
    """把 "auto" 换成具体数字。写死的值原样返回，两个可以独立设。"""
    if high != "auto" and max_ != "auto":
        return high, max_
    total = total_bytes if total_bytes is not None else total_memory_bytes()
    auto_high, auto_max = (
        suggested_session_limits(total) if total else (FALLBACK_MEMORY_HIGH, FALLBACK_MEMORY_MAX)
    )
    return (auto_high if high == "auto" else high, auto_max if max_ == "auto" else max_)


# 上面那些是**单个进程**的闸门，拦的是一个会话自己失控。
# 但真正把机器冻死的是**总量**：实测本机 app-tmux.slice 峰值 6.75GB / 总内存 7.8GB，
# 4 个活跃会话就够。所以所有会话再共同归进一个 slice，由它兜总量。
#
# 这个 slice 由用户环境提供（`~/.config/systemd/user/atm-ai.slice`），
# tmux-resurrect 恢复出来的会话也靠 `@resurrect-processes` 的 `->` 映射进同一个池 ——
# 于是「谁把会话拉起来的」不再决定「有没有限制」。
# slice 不存在时 systemd-run 会自动创建一个无限制的同名 slice，等于只剩单进程闸门，
# 不会导致投递失败。
DEFAULT_SLICE = "atm-ai.slice"


@dataclass(frozen=True, slots=True)
class MemoryLimit:
    """投递时给会话套的 cgroup 内存限制。None 表示不限。"""

    high: str = FALLBACK_MEMORY_HIGH
    max: str = FALLBACK_MEMORY_MAX
    swap_max: str = DEFAULT_MEMORY_SWAP_MAX
    slice_name: str = DEFAULT_SLICE
    # False = 系统级 scope（需要 root）。默认走当前用户的 user manager。
    user: bool = True

    def systemd_args(self, description: str) -> list[str]:
        return [
            "systemd-run",
            *(["--user"] if self.user else []),
            "--scope",
            "-q",
            f"--slice={self.slice_name}",
            "--description",
            description,
            "-p",
            f"MemoryHigh={self.high}",
            "-p",
            f"MemoryMax={self.max}",
            "-p",
            f"MemorySwapMax={self.swap_max}",
            "-p",
            "MemoryAccounting=1",
        ]


# ---------------------------------------------------------------- 正在被限流吗
#
# 这是 2026-09-10 那次「一个 pane 卡死」暴露的诊断缺口：`atm doctor` 原来只查
# 「闸门在不在、数字是多少」，不查**有没有正在生效**。而 MemoryHigh 生效的样子就是
# 进程活着、不报错、慢到像卡死 —— 现场 `memory.events` 里 227 万次 high 事件摆在那儿，
# 工具一个字都不说。定值改对了不代表这个洞补上了：用户改小了数一样会撞。


@dataclass(frozen=True, slots=True)
class ScopePressure:
    """slice 底下一个会话 scope 的内存现状。`high_events` 是超过软上限的次数。"""

    name: str
    current: int
    high: int | None  # None = max（无限制）
    high_events: int
    max_events: int

    @property
    def throttled(self) -> bool:
        """在被节流：软上限有限，且已经撞上去过。"""
        return self.high is not None and self.high_events > 0

    @property
    def over_high(self) -> bool:
        """**此刻**还在软上限之上 —— 这才是「现在就卡着」。"""
        return self.high is not None and self.current > self.high


def _cgroup_int(path: Path) -> int | None:
    """cgroup 里的数值文件。`max` = 无限制 → None。"""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return None if raw == "max" else int(raw) if raw.isdigit() else None


def slice_path_parts(slice_name: str) -> tuple[str, ...]:
    """`atm-ai.slice` → ("atm.slice", "atm-ai.slice")。

    systemd 用 `-` 表示 slice 的**层级**，所以 `atm-ai.slice` 在 cgroup 里不是
    `user@UID.service/atm-ai.slice`，而是嵌在 `atm.slice/` 下面一层。
    本机实测就是这样，按扁平路径找会一个 scope 都看不到。
    """
    stem = slice_name.removesuffix(".slice")
    parts = [p for p in stem.split("-") if p]
    return tuple(f"{'-'.join(parts[: i + 1])}.slice" for i in range(len(parts)))


def slice_cgroup_dir(slice_name: str = DEFAULT_SLICE, *, root: Path | None = None) -> Path:
    """slice 在 cgroup 里的目录。路径口径和 memory_limits_available() 保持一致。"""
    base = root or Path("/sys/fs/cgroup")
    uid = os.getuid()
    directory = base / "user.slice" / f"user-{uid}.slice" / f"user@{uid}.service"
    for part in slice_path_parts(slice_name):
        directory = directory / part
    return directory


def _read_pressure(directory: Path, name: str) -> ScopePressure | None:
    """读一个 cgroup 目录的内存现状。读不到 / 不是数字都返回 None。"""
    current = _cgroup_int(directory / "memory.current")
    if current is None:
        return None
    events: dict[str, int] = {}
    try:
        for line in (directory / "memory.events").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(" ")
            if value.isdigit():
                events[key] = int(value)
    except OSError:
        pass
    return ScopePressure(
        name=name,
        current=current,
        high=_cgroup_int(directory / "memory.high"),
        high_events=events.get("high", 0),
        max_events=events.get("max", 0),
    )


def slice_pressure(
    slice_name: str = DEFAULT_SLICE, *, root: Path | None = None
) -> ScopePressure | None:
    """**slice 本身**的内存现状。

    为什么单独要这一层：小内存机器上先撞上限的往往是总量，不是某一个会话。
    2026-09-10 现场就是这样 —— 5 个 scope 合计 4725M 撞着 slice 的 4096M 软上限，
    只报每个 scope 自己会得出「没有会话撞过软上限」，而实际上**每一个都在被回收拖慢**。
    """
    return _read_pressure(slice_cgroup_dir(slice_name, root=root), slice_name)


def scope_pressure(
    slice_name: str = DEFAULT_SLICE, *, root: Path | None = None
) -> tuple[ScopePressure, ...]:
    """slice 底下每个 scope 的内存现状。读不到就返回空 —— 诊断绝不能自己抛。"""
    directory = slice_cgroup_dir(slice_name, root=root)
    try:
        children = sorted(d for d in directory.iterdir() if d.is_dir())
    except OSError:
        return ()
    found = (_read_pressure(child, child.name) for child in children)
    return tuple(p for p in found if p is not None)


def memory_limits_available() -> bool:
    """这台机器能不能套 cgroup 限制。

    需要 systemd 的 user manager 且已把 memory 控制器 delegate 下来。
    拿不到就静默降级成不限 —— 没有闸门也比投递失败强。
    """
    if shutil.which("systemd-run") is None:
        return False
    controllers = Path(f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/cgroup.controllers")
    try:
        return "memory" in controllers.read_text()
    except OSError:
        return False


RESUME_PROGRAMS: dict[Source, tuple[str, ...]] = {
    # 实测 `claude --help`：`-r, --resume [value]  Resume a conversation by session ID`
    Source.CLAUDE: ("claude", "--resume"),
    # 实测 `codex resume --help`：`Usage: codex resume [OPTIONS] [SESSION_ID] [PROMPT]`
    Source.CODEX: ("codex", "resume"),
    # 上游文档 sessions.md：`--session <path|id>  Use a specific session file or partial session ID`
    # （未在真机验证过，本机没装 pi。）
    Source.PI: ("pi", "--session"),
    # 实测 `gemini --help`：`-r, --resume  Resume a previous session.`
    # 只吃**完整 UUID**（或纯数字序号），且只在会话所属项目的 cwd 下才找得到。
    # 细节见 sources/gemini.py。
    Source.GEMINI: ("gemini", "--resume"),
    # 实测 `opencode --help`：`-s, --session  session id to continue`
    # （TUI 和 `opencode run` 都认这个参数）
    Source.OPENCODE: ("opencode", "--session"),
}


class TargetKind(StrEnum):
    EXISTING = "existing"  # 投到已有 pane
    SPLIT = "split"  # 从某个 pane 分一个新的出来
    WINDOW = "window"  # 开新 window
    PRINT = "print"  # 只打印命令，不投（不在 tmux 里时的降级路径）


@dataclass(frozen=True, slots=True)
class DispatchTarget:
    kind: TargetKind
    pane_id: str | None = None
    direction: SplitDirection = SplitDirection.HORIZONTAL

    @classmethod
    def existing(cls, pane_id: str) -> DispatchTarget:
        return cls(kind=TargetKind.EXISTING, pane_id=pane_id)

    @classmethod
    def split(
        cls, from_pane: str | None = None, direction: SplitDirection = SplitDirection.HORIZONTAL
    ) -> DispatchTarget:
        return cls(kind=TargetKind.SPLIT, pane_id=from_pane, direction=direction)

    @classmethod
    def window(cls) -> DispatchTarget:
        return cls(kind=TargetKind.WINDOW)

    @classmethod
    def print_only(cls) -> DispatchTarget:
        return cls(kind=TargetKind.PRINT)


@dataclass(frozen=True, slots=True)
class ResumeCommand:
    """恢复一条会话要执行的命令。具名类型，不返回裸字符串。"""

    program: str
    argv: tuple[str, ...]
    cwd: str
    memory: MemoryLimit | None = None
    description: str = "atm session"

    def shell_line(self) -> str:
        """拼成一行可以直接喂给 shell 的文本，所有部分都过 shlex.quote。

        带内存限制时外面裹一层 `systemd-run --user --scope`。
        实测它**完全保留 tty**（stdin/stdout/TERM/cwd 都原样传递），
        所以 claude 的全屏 TUI 不受影响。
        """
        parts: list[str] = []
        if self.memory is not None:
            parts.extend(self.memory.systemd_args(self.description))
        parts.extend((self.program, *self.argv))
        command = " ".join(shlex.quote(part) for part in parts)
        return f"cd {shlex.quote(self.cwd)} && {command}"


@dataclass(frozen=True, slots=True)
class DispatchResult:
    entry: SessionEntry
    command: ResumeCommand
    pane_id: str | None
    created_pane: bool

    def describe(self) -> str:
        if self.pane_id is None:
            return self.command.shell_line()
        verb = _("新开") if self.created_pane else _("投递到")
        return f"{verb} {self.pane_id}: {self.command.shell_line()}"


class DispatchError(RuntimeError):
    pass


def resume_command(entry: SessionEntry, memory: MemoryLimit | None = None) -> ResumeCommand:
    """构造 resume 命令。纯函数，好测。"""
    spec = RESUME_PROGRAMS.get(entry.source)
    if spec is None:
        raise DispatchError(_("不认识的会话来源: {entry_source}").format(entry_source=entry.source))
    program, *flags = spec
    label = entry.name or entry.title
    return ResumeCommand(
        program=program,
        argv=(*flags, entry.id),
        cwd=entry.cwd,
        memory=memory,
        description=f"atm: {label[:40]}",
    )


def cwd_missing(entry: SessionEntry) -> bool:
    """会话原本的工作目录还在不在。

    实测本机 210 条会话里有 **33 条（16%）** 的 cwd 已经不存在了：
    `/tmp/claude-*/scratchpad`（临时目录被清）、删掉的 git worktree、移动过的项目目录。

    为什么必须拦：投递的是 `cd <cwd> && claude --resume <id>`，
    `cd` 失败会让整条 `&&` 链中止 —— 用户只看到一句 `No such file or directory`，
    claude 根本不会启动，而且看不出是为什么。
    """
    return not Path(entry.cwd).is_dir()


def missing_cwds(entries: Iterable[SessionEntry]) -> frozenset[str]:
    """一次性算出哪些 cwd 已失效。

    按**去重后的目录**做 stat，而不是每条会话一次 —— 210 条会话通常只有几十个不同目录。
    结果给 TUI 用来标记，避免在重绘循环里反复 stat。
    """
    seen: set[str] = set()
    missing: set[str] = set()
    for entry in entries:
        if entry.cwd in seen:
            continue
        seen.add(entry.cwd)
        if not Path(entry.cwd).is_dir():
            missing.add(entry.cwd)
    return frozenset(missing)


def is_safe_target(pane: Pane) -> bool:
    """这个 pane 现在能不能接命令。"""
    return pane.is_idle_shell


def busy_reason(pane: Pane) -> str:
    return (
        _(
            "pane {pane_id} ({pane_label}) 里跑的是 "
            "`{pane_current_command}`，它的语法和我们生成的 POSIX "
            "命令行（`cd '…' && …`，shlex 单引号转义）不兼容 ——"
            " 投进去不会报错，会**执行成别的东西**。用 --force 强制投递。"
        ).format(pane_id=pane.id, pane_label=pane.label, pane_current_command=pane.current_command)
        if pane.is_non_posix_shell
        else _(
            "pane {pane_id} ({pane_label}) 里正在跑 `{pane_current_command}`，"
            "不是空闲 shell —— 投进去会被当成那个程序的输入。用 --force 强制投递。"
        ).format(pane_id=pane.id, pane_label=pane.label, pane_current_command=pane.current_command)
    )


def dispatch(
    entry: SessionEntry,
    target: DispatchTarget,
    *,
    force: bool = False,
    focus: bool = True,
    memory: MemoryLimit | None = None,
) -> DispatchResult:
    """执行投递。返回具名结果，调用方决定怎么展示。"""
    command = resume_command(entry, memory)

    # cwd 没了就别投 —— `cd` 失败会让整条命令静默中止，用户看不出原因。
    if cwd_missing(entry):
        raise DispatchError(
            _(
                "这条会话的工作目录已经不存在了：{entry_cwd}\n        投递会执行 `cd` 到该目录，"
                "失败后整条命令会中止、claude 不会启动。\n        "
                "（常见原因：/tmp 下的 scratchpad 被清理、git worktree 被删、项目目录移动过）"
            ).format(entry_cwd=entry.cwd)
        )

    if target.kind is TargetKind.PRINT:
        return DispatchResult(entry=entry, command=command, pane_id=None, created_pane=False)

    if not tmux.has_server():
        raise DispatchError(_("没有正在运行的 tmux server —— 先 `tmux new -s main`，或用 --print"))

    created = False
    try:
        if target.kind is TargetKind.EXISTING:
            if not target.pane_id:
                raise DispatchError(_("投递到已有 pane 需要 pane_id"))
            pane_id = target.pane_id
            if not force:
                pane = _lookup_pane(pane_id)
                if not is_safe_target(pane):
                    raise DispatchError(busy_reason(pane))

        elif target.kind is TargetKind.SPLIT:
            pane_id = tmux.split_window(
                target=target.pane_id,
                direction=target.direction,
                cwd=entry.cwd,
                detached=not focus,
            )
            created = True

        elif target.kind is TargetKind.WINDOW:
            # focus=False 的调用方（侧栏）会自己决定视图去哪，新窗口不能抢焦点
            pane_id = tmux.new_window(cwd=entry.cwd, name=_window_name(entry), detached=not focus)
            created = True

        else:  # pragma: no cover - StrEnum 已穷举
            raise DispatchError(
                _("不认识的投递目标: {target_kind}").format(target_kind=target.kind)
            )

        tmux.send_line(pane_id, command.shell_line())
        if focus:
            _try_focus(pane_id)
    except TmuxError as exc:
        raise DispatchError(str(exc)) from exc

    return DispatchResult(entry=entry, command=command, pane_id=pane_id, created_pane=created)


def _lookup_pane(pane_id: str) -> Pane:
    for pane in tmux.list_panes():
        if pane.id == pane_id:
            return pane
    raise DispatchError(_("找不到 pane {pane_id}").format(pane_id=pane_id))


def _try_focus(pane_id: str) -> None:
    """切焦点是锦上添花 —— 从 popup 里调用时 switch-client 可能失败，不该让投递算失败。"""
    with contextlib.suppress(TmuxError):
        tmux.select_pane(pane_id)


def _window_name(entry: SessionEntry) -> str:
    name = entry.project_name or entry.source.value
    return name[:20]
