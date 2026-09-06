"""`atm config` 的 `tmux.*` 怎么落到 tmux 上。

覆盖：mouse / focus-events / history-limit / base-index / renumber-windows。

这几行是大多数人手写在 ~/.tmux.conf 顶上的「常用配置」。收进 `atm config` 之后，编辑器里切一下、
保存，就同时写进文件 + 对活着的 server 生效，不用记 tmux 的选项名。

落地方式：**独立的一对 marker，放在文件最前面** —— tmux 配置后来者胜，用户自己在下面写的任何一行
都能盖掉我们的。只写「开着 / 非默认」的选项，关着的不写 `off`，免得盖掉用户自己配置里的 on；
全关时整块删掉。对活着的 server 直接下 `set -g`，不 source 整份配置。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import config as config_mod
from . import tmux
from .i18n import _
from .install import _backup, _has_marker, _read, _strip_block

MARKER_BEGIN = "# >>> atm tmux options (atm config tmux.*) >>>"
MARKER_END = "# <<< atm tmux options <<<"


@dataclass(frozen=True, slots=True)
class OptionSpec:
    """一个 tmux.* 键怎么变成 tmux 命令。

    `active(value)` 为真才写；`on(value)` 是要下的命令（argv 列表，`set-option` 开头）；
    关闭时只撤文件里的设置；运行中可能已有用户值，不能猜一个默认值去覆盖。
    """

    field: str
    tmux_names: tuple[str, ...]  # 块里出现这些选项名就算「之前开着」
    active: Callable[[object], bool]
    on: Callable[[object], list[list[str]]]


SPECS: dict[str, OptionSpec] = {
    "mouse": OptionSpec(
        field="tmux_mouse",
        tmux_names=("mouse",),
        active=bool,
        on=lambda v: [["set-option", "-g", "mouse", "on"]],
    ),
    "focus-events": OptionSpec(
        field="tmux_focus_events",
        tmux_names=("focus-events",),
        active=bool,
        on=lambda v: [["set-option", "-g", "focus-events", "on"]],
    ),
    "history-limit": OptionSpec(
        field="tmux_history_limit",
        tmux_names=("history-limit",),
        active=lambda v: int(v) > 0,
        on=lambda v: [["set-option", "-g", "history-limit", str(v)]],
    ),
    "base-index": OptionSpec(
        field="tmux_base_index",
        tmux_names=("base-index", "pane-base-index"),
        active=lambda v: int(v) > 0,
        on=lambda v: [
            ["set-option", "-g", "base-index", str(v)],
            ["set-option", "-gw", "pane-base-index", str(v)],
        ],
    ),
    "renumber-windows": OptionSpec(
        field="tmux_renumber_windows",
        tmux_names=("renumber-windows",),
        active=bool,
        on=lambda v: [["set-option", "-g", "renumber-windows", "on"]],
    ),
}


def _conf_line(argv: list[str]) -> str:
    """argv → 配置文件里的一行：`set-option -g mouse on` 写成 `set -g mouse on`。"""
    return " ".join(["set", *argv[1:]])


@dataclass(frozen=True, slots=True)
class TmuxOptsPlan:
    conf_path: Path
    enabled: tuple[str, ...]  # 这次要写的选项（SPECS 的键）
    commands: tuple[tuple[str, ...], ...]  # 开着的选项要下的命令
    previously_enabled: tuple[str, ...]  # 现有块里有的
    already_installed: bool

    @property
    def block(self) -> str:
        if not self.enabled:
            return ""
        body = "\n".join(_conf_line(list(argv)) for argv in self.commands)
        return f"{MARKER_BEGIN}\n{body}\n{MARKER_END}"

    @property
    def to_turn_off(self) -> tuple[str, ...]:
        return tuple(n for n in self.previously_enabled if n not in self.enabled)

    @property
    def is_noop(self) -> bool:
        """文件里已经是这样了，什么都不用写。"""
        existing = _read(self.conf_path) if self.already_installed else ""
        # 块内容相同也不能掩盖后面缺 END 的第二个块。
        _strip_block(existing, MARKER_BEGIN, MARKER_END)
        if not self.enabled:
            return not self.already_installed
        return self.block in existing

    def describe(self) -> str:
        if not self.enabled and not self.already_installed:
            return _("tmux 常用选项（atm config tmux.*）都没开，不写。")
        if not self.enabled:
            return "\n".join(
                [
                    _("将从 {path} 移除 tmux 选项块（全部关了）").format(path=self.conf_path),
                    disabled_note(self.to_turn_off),
                ]
            )
        lines = [_("将写入 {path} 最前面（tmux 选项块）：").format(path=self.conf_path), ""]
        lines += [f"  {line}" for line in self.block.splitlines()]
        if self.to_turn_off:
            lines.append("")
            lines.append(disabled_note(self.to_turn_off))
        if self.already_installed and not self.is_noop:
            lines.append(_("（已存在 tmux 选项块，会被整块替换掉，不会重复追加）"))
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class TmuxOptsResult:
    conf_path: Path
    backup_path: Path | None
    written: bool  # 文件有没有动
    applied_live: bool
    live_error: str | None
    disabled: tuple[str, ...] = ()


def build_plan(cfg: config_mod.Config, *, conf_path: Path | None = None) -> TmuxOptsPlan:
    path = conf_path or Path.home() / ".tmux.conf"
    existing = _read(path)
    already = _has_marker(existing, MARKER_BEGIN)
    enabled: list[str] = []
    commands: list[tuple[str, ...]] = []
    for name, spec in SPECS.items():
        value = getattr(cfg, spec.field)
        if spec.active(value):
            enabled.append(name)
            commands.extend(tuple(argv) for argv in spec.on(value))
    return TmuxOptsPlan(
        conf_path=path,
        enabled=tuple(enabled),
        commands=tuple(commands),
        previously_enabled=_enabled_in_block(existing) if already else (),
        already_installed=already,
    )


def apply(plan: TmuxOptsPlan, *, live: bool = True) -> TmuxOptsResult:
    """写块（放最前面）或删块 + 对活着的 server 立即生效。文件没变化就不备份不写。"""
    written = False
    backup: Path | None = None
    if not plan.is_noop:
        existing = _read(plan.conf_path)
        backup = _backup(plan.conf_path) if existing else None
        rest = _strip_block(existing, MARKER_BEGIN, MARKER_END).lstrip("\n")
        # 开着 → 块放最前面，空一行再接用户原有内容；全关 → 只剩用户内容
        updated = f"{plan.block}\n" + (f"\n{rest}" if rest else "") if plan.enabled else rest
        plan.conf_path.parent.mkdir(parents=True, exist_ok=True)
        plan.conf_path.write_text(updated, encoding="utf-8")
        written = True

    applied_live = False
    live_error: str | None = None
    if live and plan.commands and tmux.has_server():
        try:
            for argv in plan.commands:
                tmux.run(list(argv))
            applied_live = True
        except tmux.TmuxError as exc:
            live_error = str(exc)
    return TmuxOptsResult(
        plan.conf_path, backup, written, applied_live, live_error, plan.to_turn_off
    )


def disabled_note(names: tuple[str, ...]) -> str:
    return _("关闭选项 {names}：运行中的值保持不变，变更对新 tmux server 生效。").format(
        names=", ".join(names)
    )


def sync(cfg: config_mod.Config, *, conf_path: Path | None = None) -> TmuxOptsResult:
    """`atm config` 保存后调：让 ~/.tmux.conf 和运行中的 server 跟上配置。"""
    return apply(build_plan(cfg, conf_path=conf_path))


def remove(conf_path: Path | None = None) -> tuple[bool, Path | None]:
    """`atm uninstall`：只删我们的块。不对活着的 server 恢复默认 —— 用户自己的配置里可能也开着。"""
    path = conf_path or Path.home() / ".tmux.conf"
    existing = _read(path)
    if not _has_marker(existing, MARKER_BEGIN):
        return False, None
    backup = _backup(path)
    path.write_text(_strip_block(existing, MARKER_BEGIN, MARKER_END).lstrip("\n"), encoding="utf-8")
    return True, backup


def _enabled_in_block(text: str) -> tuple[str, ...]:
    """现有块里出现了哪些我们认识的选项（按 SPECS 的键返回，去重保序）。"""
    inside = False
    names: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == MARKER_BEGIN:
            inside = True
            continue
        if stripped == MARKER_END:
            break
        if inside:
            parts = stripped.split()
            if len(parts) >= 3 and parts[0] == "set":
                names.add(parts[2])
    return tuple(name for name, spec in SPECS.items() if names & set(spec.tmux_names))
