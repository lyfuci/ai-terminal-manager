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
    return (
        f"提示：这条会话的转录有 {mb:.0f}MB，"
        f"{RESUME_PROGRAMS.get(entry.source, ('CLI',))[0]} 可能会先问你要不要「从摘要恢复」。"
    )


# ---------------------------------------------------------------- 内存闸门
#
# 为什么需要：实测一个持续干活 1h40m 的 claude 会话内存峰值到过 **4.7GB**，
# 四个长会话合计约 8GB —— 正好是本机 WSL 的上限。撞上限的后果不是「那个会话变慢」，
# 而是**整个 tmux server 连同所有会话一起死掉**（2026-08-12 11:59 就这么死过一次）。
# 套上 cgroup 之后，最坏情况从「全丢」变成「丢一个」。
#
# 阈值来自实测（journal 里 25 个真实会话的 memory.peak 分布）：
#   1G 会杀掉 12%，2G 只杀 4%，再往上加到 4G 没有任何改善 —— 2G 是拐点。
#
# 为什么 High/Max 分开（这是关键）：实测峰值**大多是瞬时尖峰**，
# 同一批会话的 peak→current 回落达 60~75%。所以：
#   - MemoryHigh 是**软**上限：超了只节流 + 强制回收，**不杀进程**，尖峰会被自然压回去；
#   - MemoryMax 是**硬**底线，只在回收也压不住时才动手，拦的是真正失控的那种。
# 只设 MemoryMax 会把正常的尖峰误杀。
DEFAULT_MEMORY_HIGH = "2G"
DEFAULT_MEMORY_MAX = "4G"
# swap 单独限死：WSL 的 swap.vhdx 在 Windows 文件系统上，一旦开始刷就是宿主 SSD 100%。
DEFAULT_MEMORY_SWAP_MAX = "512M"

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

    high: str = DEFAULT_MEMORY_HIGH
    max: str = DEFAULT_MEMORY_MAX
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
        verb = "新开" if self.created_pane else "投递到"
        return f"{verb} {self.pane_id}: {self.command.shell_line()}"


class DispatchError(RuntimeError):
    pass


def resume_command(entry: SessionEntry, memory: MemoryLimit | None = None) -> ResumeCommand:
    """构造 resume 命令。纯函数，好测。"""
    spec = RESUME_PROGRAMS.get(entry.source)
    if spec is None:
        raise DispatchError(f"不认识的会话来源: {entry.source}")
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
        f"pane {pane.id} ({pane.label}) 里跑的是 `{pane.current_command}`，"
        f"它的语法和我们生成的 POSIX 命令行（`cd '…' && …`，shlex 单引号转义）不兼容 —— "
        f"投进去不会报错，会**执行成别的东西**。用 --force 强制投递。"
        if pane.is_non_posix_shell
        else f"pane {pane.id} ({pane.label}) 里正在跑 `{pane.current_command}`，"
        f"不是空闲 shell —— 投进去会被当成那个程序的输入。用 --force 强制投递。"
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
            f"这条会话的工作目录已经不存在了：{entry.cwd}\n"
            f"        投递会执行 `cd` 到该目录，失败后整条命令会中止、claude 不会启动。\n"
            f"        （常见原因：/tmp 下的 scratchpad 被清理、git worktree 被删、项目目录移动过）"
        )

    if target.kind is TargetKind.PRINT:
        return DispatchResult(entry=entry, command=command, pane_id=None, created_pane=False)

    if not tmux.has_server():
        raise DispatchError("没有正在运行的 tmux server —— 先 `tmux new -s main`，或用 --print")

    created = False
    try:
        if target.kind is TargetKind.EXISTING:
            if not target.pane_id:
                raise DispatchError("投递到已有 pane 需要 pane_id")
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
            raise DispatchError(f"不认识的投递目标: {target.kind}")

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
    raise DispatchError(f"找不到 pane {pane_id}")


def _try_focus(pane_id: str) -> None:
    """切焦点是锦上添花 —— 从 popup 里调用时 switch-client 可能失败，不该让投递算失败。"""
    with contextlib.suppress(TmuxError):
        tmux.select_pane(pane_id)


def _window_name(entry: SessionEntry) -> str:
    name = entry.project_name or entry.source.value
    return name[:20]
