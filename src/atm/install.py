"""把 tmux 键位绑定装进 `~/.tmux.conf`（以及立刻对正在跑的 server 生效）。

为什么不用 `run-shell /abs/path/atm.tmux`：那条路径指向源码目录，
`src/atm/` 一旦搬走或改名，用户的 tmux.conf 就悄悄失效了。
直接写 `bind-key` 行是自包含的，而且用户能直接看懂和改。
（`atm.tmux` 保留给 tpm 用户。）绑定里调的是 `atm` 的绝对路径而不是裸名 ——
tmux server 的 PATH 是它启动那刻的快照，见 `resolve_atm_command`。

这个模块会**改用户的全局配置**，所以：
- 用 marker 包起来，可精确识别 / 幂等 / 可干净卸载；
- 改之前先备份；
- 默认只生成计划，由调用方拿去给用户确认后再 apply。
"""

from __future__ import annotations

import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from . import tmux
from .i18n import _

MARKER_BEGIN = "# >>> atm (ai-terminal-manager) >>>"
MARKER_END = "# <<< atm <<<"

# 只接受**单个小写字母**作为键位。
# 原因：第二条绑定用 key.upper() 区分「全部会话 / 当前目录」，而 `.upper()` 对数字、
# 功能键、符号都是恒等变换 —— 实测 `--key 1` 会生成两条 `bind-key 1`，
# 第一条被第二条静默覆盖，用户永远按不出「全部会话」那个浮层。
_VALID_KEY = re.compile(r"^[a-z]$")

DEFAULT_KEY = "a"
DEFAULT_WIDTH = "80%"
DEFAULT_HEIGHT = "70%"
# 侧栏开关键（大写 = 把当前格子收进后台）。tmux 默认没绑 b / B。
DEFAULT_SIDEBAR_KEY = "b"


class BindingKind(StrEnum):
    POPUP = "popup"  # display-popup -E 浮层里跑
    SHELL = "shell"  # run-shell -b 后台跑（侧栏开关 / 停车这类不需要界面的动作）


@dataclass(frozen=True, slots=True)
class Binding:
    """一条 tmux 键位绑定。拆成具名类型，好同时用于「写配置」和「对活着的 server 立刻生效」。"""

    key: str
    command: str
    description: str
    kind: BindingKind = BindingKind.POPUP
    width: str = DEFAULT_WIDTH
    height: str = DEFAULT_HEIGHT

    def conf_line(self) -> str:
        # 用 shlex.quote 而不是 Python 的 !r：后者在字符串含单引号时会改用双引号，
        # 那在 tmux 配置里的转义规则是不一样的。
        if self.kind is BindingKind.SHELL:
            return f"bind-key {self.key} run-shell -b {shlex.quote(self.command)}"
        return (
            f"bind-key {self.key} display-popup -E "
            f"-w {self.width} -h {self.height} {shlex.quote(self.command)}"
        )

    def tmux_args(self) -> list[str]:
        if self.kind is BindingKind.SHELL:
            return ["bind-key", self.key, "run-shell", "-b", self.command]
        return [
            "bind-key",
            self.key,
            "display-popup",
            "-E",
            "-w",
            self.width,
            "-h",
            self.height,
            self.command,
        ]


@dataclass(frozen=True, slots=True)
class InstallPlan:
    conf_path: Path
    bindings: tuple[Binding, ...]
    block: str
    already_installed: bool
    conf_exists: bool
    atm_command: str

    def describe(self) -> str:
        lines = [_("将写入 {self_conf_path}：").format(self_conf_path=self.conf_path), ""]
        lines += [f"  {line}" for line in self.block.splitlines()]
        lines.append("")
        lines.append(_("绑定说明："))
        for binding in self.bindings:
            lines.append(f"  prefix + {binding.key}  →  {binding.description}")
        if self.already_installed:
            lines.append("")
            lines.append(_("（已存在 atm 配置块，会被整块替换掉，不会重复追加）"))
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class InstallResult:
    conf_path: Path
    backup_path: Path | None
    applied_live: bool
    live_error: str | None


def resolve_atm_command() -> str:
    """决定 tmux 里该怎么调 atm —— 永远是**绝对路径**。

    优先 PATH 上的 `atm`，但写进 tmux.conf 的是 `which()` 解析出的完整路径，
    不是裸名。因为 tmux server 用的是**启动它那一刻**的环境，而 `atm` 一般装在
    `~/.local/bin`（uv tool / pipx）：只要 server 比那条 PATH 更早起来，
    `run-shell 'atm …'` 就是 127，报错只有一行
    `'atm sidebar --toggle --pane %1' returned 127`，非常难查。
    安装时的 shell 找得到，不代表 tmux 找得到。

    找不到就退回当前解释器的 `-m atm`（同样是绝对路径），这样从源码树直接跑也能装。
    """
    found = shutil.which("atm")
    if found:
        return shlex.quote(found)
    return f"{shlex.quote(sys.executable)} -m atm"


def validate_key(name: str, value: str) -> None:
    """绑定键必须是单个小写字母；命令行和 install 向导共用这一条规则。"""
    if not _VALID_KEY.match(value):
        raise ValueError(
            _(
                "{name} 只能是单个小写字母，收到 {value!r}。（配套的第二条绑定靠 "
                ".upper() 区分，非字母的 upper() 是恒等变换，"
                "两条绑定会撞成同一个键、第一条静默失效。）"
            ).format(name=name, value=value)
        )


def build_plan(
    *,
    conf_path: Path | None = None,
    key: str = DEFAULT_KEY,
    sidebar_key: str = DEFAULT_SIDEBAR_KEY,
    width: str = DEFAULT_WIDTH,
    height: str = DEFAULT_HEIGHT,
) -> InstallPlan:
    validate_key("--key", key)
    validate_key("--sidebar-key", sidebar_key)
    if key == sidebar_key:
        raise ValueError(_("--key 和 --sidebar-key 不能相同（都是 {key!r}）").format(key=key))
    path = conf_path or Path.home() / ".tmux.conf"
    atm_command = resolve_atm_command()

    bindings = (
        Binding(
            key=key,
            command=f"{atm_command} pick",
            description=_("全部会话"),
            width=width,
            height=height,
        ),
        Binding(
            key=key.upper(),
            command=f"{atm_command} pick --here",
            description=_("只看当前目录的会话"),
            width=width,
            height=height,
        ),
        # `#{pane_id}` 由 tmux 在 run-shell 执行前展开成按键时的当前 pane。
        # 实测 run-shell 里的 $TMUX_PANE 不可靠（指向别的 pane），必须显式传。
        Binding(
            key=sidebar_key,
            command=f"{atm_command} sidebar --toggle --pane '#{{pane_id}}'",
            description=_("侧栏：开 / 切过去 / 收起"),
            kind=BindingKind.SHELL,
        ),
        Binding(
            key=sidebar_key.upper(),
            command=f"{atm_command} park '#{{pane_id}}'",
            description=_("把当前格子收进后台窗口 bg"),
            kind=BindingKind.SHELL,
        ),
    )

    body = "\n".join(binding.conf_line() for binding in bindings)
    block = f"{MARKER_BEGIN}\n{body}\n{MARKER_END}"

    existing = _read(path)
    return InstallPlan(
        conf_path=path,
        bindings=bindings,
        block=block,
        already_installed=_has_marker(existing),
        conf_exists=path.exists(),
        atm_command=atm_command,
    )


def apply(plan: InstallPlan, *, live: bool = True) -> InstallResult:
    """写配置 + （可选）立刻对正在跑的 server 生效。"""
    existing = _read(plan.conf_path)
    backup = _backup(plan.conf_path) if existing else None

    updated = _strip_block(existing)
    if updated and not updated.endswith("\n"):
        updated += "\n"
    updated = f"{updated}\n{plan.block}\n" if updated else f"{plan.block}\n"

    plan.conf_path.parent.mkdir(parents=True, exist_ok=True)
    plan.conf_path.write_text(updated, encoding="utf-8")

    applied_live = False
    live_error: str | None = None
    if live:
        applied_live, live_error = _apply_live(plan)

    return InstallResult(
        conf_path=plan.conf_path,
        backup_path=backup,
        applied_live=applied_live,
        live_error=live_error,
    )


def remove(conf_path: Path | None = None) -> tuple[bool, Path | None]:
    """卸载：只删 marker 之间的块，用户自己写的东西一律不动。"""
    path = conf_path or Path.home() / ".tmux.conf"
    existing = _read(path)
    if not _has_marker(existing):
        return False, None

    backup = _backup(path)
    path.write_text(_strip_block(existing), encoding="utf-8")
    return True, backup


def _apply_live(plan: InstallPlan) -> tuple[bool, str | None]:
    """对正在跑的 server 直接下 bind-key，免得用户还要手动 reload。

    刻意不用 `source-file ~/.tmux.conf` —— 那会把整份配置重新执行一遍，
    用户配置里若有带副作用的行（new-session / run-shell 之类）会被重复触发。
    """
    if not tmux.has_server():
        return False, None
    try:
        for binding in plan.bindings:
            tmux.run(binding.tmux_args())
    except tmux.TmuxError as exc:
        return False, str(exc)
    return True, None


class ConfUnreadable(RuntimeError):
    """配置文件存在但读不出来。**绝不能**当成「文件不存在」处理。"""


def _read(path: Path) -> str:
    """读配置。区分「不存在」和「存在但读不了」—— 这两者后果天差地别。

    之前两者都返回 ""，于是一个非 UTF-8 或没有读权限的 ~/.tmux.conf 会被判定为
    「空文件」→ 跳过备份 → 整份覆盖掉用户的全部配置。
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfUnreadable(
            _(
                "{path} 存在但读不出来（{v0}）。"
                "为避免覆盖掉你现有的配置，这里直接中止。请先确认它的编码/权限，或手动备份后删除它。"
            ).format(path=path, v0=exc.__class__.__name__)
        ) from exc


def _has_marker(text: str, begin: str = MARKER_BEGIN) -> bool:
    """配置里有没有 atm 的块。

    **必须和 _strip_block 用同一个口径（整行相等）**。之前检测用子串包含、
    剥离用整行相等 —— 用户在 marker 行尾加了注释后，检测说「装过了」、
    剥离却一行都删不掉，于是 `atm uninstall` 报成功、实际什么都没删。

    `begin` 可换：persist.py 的插件块用另一对 marker，复用同一套口径。
    """
    return any(line.strip() == begin for line in text.splitlines())


def _strip_block(text: str, begin: str = MARKER_BEGIN, end: str = MARKER_END) -> str:
    """删掉 marker 之间的内容（含 marker 本身）。没有就原样返回。"""
    if not _has_marker(text, begin):
        return text

    lines = text.splitlines()

    # 先确认 BEGIN/END 成对。缺 END 时**一行都不删** ——
    # 之前的单遍状态机会让 inside 一直为真到 EOF，把标记之后用户自己的配置全部吃掉。
    begins = [i for i, ln in enumerate(lines) if ln.strip() == begin]
    ends = [i for i, ln in enumerate(lines) if ln.strip() == end]
    if not ends or ends[-1] < begins[0]:
        raise ConfUnreadable(
            _(
                "{begin} 有开始标记但找不到对应的 {end}。"
                "为避免误删标记之后的配置，这里不做任何改动 —— 请手动检查 ~/.tmux.conf。"
            ).format(begin=begin, end=end)
        )

    out: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == begin:
            inside = True
            continue
        if inside:
            if line.strip() == end:
                inside = False
            continue
        out.append(line)

    # 去掉块被摘掉后留下的连续空行
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + ("\n" if out else "")


class BackupFailed(RuntimeError):
    """备份写不出去。**不能**继续改原文件——没有退路的修改不做。"""


def _backup(path: Path) -> Path:
    """改全局配置前先留一份。

    之前备份失败返回 None 然后照样往下写，等于「备份」是装饰。现在失败就抛，调用方整个中止。
    文件名带微秒并在碰撞时加序号：同一秒内连跑两次 `atm install` 不会互相覆盖备份。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S.%f")
    for n in range(100):
        suffix = f".bak-atm-{stamp}" + (f"-{n}" if n else "")
        backup = path.with_name(path.name + suffix)
        if backup.exists():
            continue
        try:
            shutil.copy2(path, backup)
        except OSError as exc:
            raise BackupFailed(
                _(
                    "备份 {path} 到 {backup} "
                    "失败（{v0}: {exc}）。为避免没有退路地改你的配置，这里直接中止。"
                ).format(path=path, backup=backup, v0=exc.__class__.__name__, exc=exc)
            ) from exc
        return backup
    raise BackupFailed(_("{path} 的备份文件名连撞 100 次，放弃。").format(path=path))
