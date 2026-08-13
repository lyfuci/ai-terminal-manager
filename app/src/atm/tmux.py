"""tmux 交互层。

只用**公开的命令行接口**（`tmux list-panes` / `send-keys` / `split-window` …）。
绝不碰 `/tmp/tmux-<UID>/default` 那个 unix socket —— 那是二进制的、未文档化的、
版本间会变的内部协议（见 ../../README.md「已知的坑」第 3 条）。

控制模式 `-CC` 这里也用不上：atm 不托管终端输出，只是往已有 pane 里投一行命令，
所以连状态机都不需要。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum

# 用 \x1f (unit separator) 当分隔符：pane 标题和路径里可能有 | 或 tab，但不会有控制字符。
_SEP = "\x1f"
_PANE_FORMAT = _SEP.join(
    [
        "#{pane_id}",
        "#{session_name}",
        "#{window_index}",
        "#{window_name}",
        "#{pane_index}",
        "#{pane_current_command}",
        "#{pane_current_path}",
        "#{pane_active}",
        "#{pane_in_mode}",
        "#{window_active}",
        "#{pane_width}",
        "#{pane_height}",
        "#{pane_title}",
    ]
)

# 往这些命令里投 send-keys 是安全的（它们在等一行输入）。
# 别的（vim / claude / codex 本身 / less）说明那个 pane 正忙，投进去会被当成对话内容。
# 我们投递的是 POSIX 语法：`cd '<路径>' && <命令>`，引用由 shlex.quote 生成。
# 只有能正确理解这套语法的 shell 才算**可投递目标**。
POSIX_SHELLS = frozenset(
    {
        "bash",
        "zsh",
        "sh",
        "dash",
        "ksh",
        "ash",
        "mksh",
        "yash",
        "rbash",
        # fish 3.0+ 支持 `&&`，单引号语义也与 POSIX 一致
        "fish",
    }
)

# 这些**是 shell**（所以不算「正在跑别的程序」），但我们生成的 POSIX 引用在它们里面
# 不保证成立：csh/tcsh 的引号与重定向语义不同；nu / elvish / xonsh / rc 是完全另一套语法；
# pwsh 虽然支持 `&&`，但 shlex.quote 的单引号转义规则与 PowerShell 不同。
# 投进去不会报错，会**执行出别的东西** —— 所以默认拒绝，要投得显式 --force。
NON_POSIX_SHELLS = frozenset(
    {"csh", "tcsh", "nu", "elvish", "xonsh", "osh", "rc", "pwsh", "powershell"}
)

SHELL_COMMANDS = POSIX_SHELLS | NON_POSIX_SHELLS


class TmuxError(RuntimeError):
    """tmux 命令失败。带上 stderr，别让用户对着空错误猜。"""


class SplitDirection(StrEnum):
    HORIZONTAL = "horizontal"  # 左右分（tmux 的 -h）
    VERTICAL = "vertical"  # 上下分（tmux 的 -v）


@dataclass(frozen=True, slots=True)
class Pane:
    """一个 tmux pane。`id` 形如 `%3`，是投递时唯一可靠的定位方式。"""

    id: str
    session: str
    window_index: int
    window_name: str
    pane_index: int
    current_command: str
    current_path: str
    active: bool
    in_mode: bool  # 处在 copy-mode / view-mode 等模式里
    window_active: bool
    width: int
    height: int
    title: str

    @property
    def label(self) -> str:
        return f"{self.session}:{self.window_index}.{self.pane_index}"

    @property
    def is_idle_shell(self) -> bool:
        """pane 里跑的是不是一个**能接命令**的 shell。

        `pane_in_mode` 必须一起看：pane 滚进 copy-mode 之后前台进程仍然是 shell，
        `pane_current_command` 照样报 bash —— 但这时 send-keys 会被 copy-mode
        当成导航按键吃掉，命令静默落空而 atm 还报成功。
        """
        return self.current_command in POSIX_SHELLS and not self.in_mode

    @property
    def is_non_posix_shell(self) -> bool:
        """是 shell，但我们生成的 POSIX 命令行在它里面不保证成立。"""
        return self.current_command in NON_POSIX_SHELLS


def is_installed() -> bool:
    return shutil.which("tmux") is not None


def inside_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def current_pane_id() -> str | None:
    """当前进程所在的 pane，形如 `%3`；拿不到就是 None。

    ⚠️ 在 `display-popup -E` 里跑时这个值是什么**没有实测过**
    （popup 需要一个已 attach 的客户端才会显示，headless 环境里验证不了）。
    所以调用方不要依赖「popup 里一定是 None」这个假设 —— 上层逻辑对两种情况都要成立。
    """
    return os.environ.get("TMUX_PANE") or None


def has_server() -> bool:
    """有没有活着的 tmux server。没有 server 时所有命令都会失败，先问清楚好报错。"""
    if not is_installed():
        return False
    try:
        result = subprocess.run(
            ["tmux", "list-sessions"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def run(args: list[str], *, timeout: float = 10.0) -> str:
    """跑一条 tmux 命令，返回 stdout。失败抛 TmuxError。"""
    if not is_installed():
        raise TmuxError("tmux 没装或不在 PATH 里")
    try:
        result = subprocess.run(
            ["tmux", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TmuxError(f"tmux {' '.join(args)} 超时") from exc
    except OSError as exc:
        raise TmuxError(f"无法执行 tmux: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise TmuxError(f"tmux {' '.join(args)} 失败: {detail}")
    return result.stdout


def list_panes() -> tuple[Pane, ...]:
    """列出所有 session 的所有 pane（`-a`），不只当前 window。"""
    return parse_panes(run(["list-panes", "-a", "-F", _PANE_FORMAT]))


def parse_panes(stdout: str) -> tuple[Pane, ...]:
    """纯函数，方便测 —— tmux 的输出格式变了这里能第一时间测出来。"""
    panes: list[Pane] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split(_SEP)
        if len(fields) < 13:
            continue  # 格式对不上就跳过这一行，别让整个列表挂掉
        try:
            panes.append(
                Pane(
                    id=fields[0],
                    session=fields[1],
                    window_index=int(fields[2]),
                    window_name=fields[3],
                    pane_index=int(fields[4]),
                    current_command=fields[5],
                    current_path=fields[6],
                    active=fields[7] == "1",
                    in_mode=fields[8] == "1",
                    window_active=fields[9] == "1",
                    width=int(fields[10]),
                    height=int(fields[11]),
                    title=fields[12],
                )
            )
        except ValueError:
            continue
    return tuple(panes)


def send_line(pane_id: str, text: str, *, enter: bool = True, clear_first: bool = True) -> None:
    """往 pane 里投一行命令。

    用 `-l`（literal）+ `--`：否则 tmux 会把 `Enter` `C-c` 这类词当**键名**解释，
    命令里带这些词就会被吃掉。回车单独发一次真正的键名。

    `clear_first` 会先发 `C-e C-u` 清掉目标 pane 里**已经打了一半、还没回车**的输入。
    不清的话我们的命令会直接拼在它后面 —— 实测（tmux 3.6）用户打了 `git st` 没回车时，
    投进去会变成 `git stcd /path && claude --resume ...` 然后真的执行。
    `pane_current_command` 看不到 readline 缓冲区，所以忙闲检查挡不住这种情况。

    C-e（行尾）+ C-u（往前删到行首）是 readline 的标准组合，比单发 C-u 稳
    —— 后者只删光标之前的部分，光标在中间时后半截会留下。
    """
    if clear_first:
        run(["send-keys", "-t", pane_id, "C-e", "C-u"])
    run(["send-keys", "-t", pane_id, "-l", "--", text])
    if enter:
        run(["send-keys", "-t", pane_id, "Enter"])


def split_window(
    *,
    target: str | None = None,
    direction: SplitDirection = SplitDirection.HORIZONTAL,
    cwd: str | None = None,
) -> str:
    """开一个新 pane，返回它的 pane_id。"""
    args = ["split-window", "-P", "-F", "#{pane_id}"]
    args.append("-h" if direction is SplitDirection.HORIZONTAL else "-v")
    if target:
        args += ["-t", target]
    if cwd:
        args += ["-c", cwd]
    return run(args).strip()


def new_window(*, cwd: str | None = None, name: str | None = None) -> str:
    args = ["new-window", "-P", "-F", "#{pane_id}"]
    if cwd:
        args += ["-c", cwd]
    if name:
        args += ["-n", name]
    return run(args).strip()


def select_pane(pane_id: str) -> None:
    """把焦点切到指定 pane（连带切到它所在的 window / session）。"""
    run(["select-pane", "-t", pane_id])
    run(["switch-client", "-t", pane_id])


def display_message(message: str) -> None:
    """在 tmux 状态栏上提示一句。失败无所谓，纯锦上添花。"""
    with contextlib.suppress(TmuxError):
        run(["display-message", message])
