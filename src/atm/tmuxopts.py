"""`atm config` 的 `tmux.*` 怎么落到 tmux 上。

覆盖：mouse / focus-events / history-limit / base-index / renumber-windows。

这几行是大多数人手写在 ~/.tmux.conf 顶上的「常用配置」。收进 `atm config` 之后，编辑器里切一下、
保存，就同时写进文件 + 对活着的 server 生效，不用记 tmux 的选项名。

落地方式：**独立的一对 marker，放在文件最前面**。只写「开着 / 非默认」的选项，关着的不写 `off`，
免得盖掉用户自己配置里的 on；全关时整块删掉。对活着的 server 直接下 `set -g`，不 source 整份配置。

**atm 不承诺它写的值最终生效**，因为做不到（实测，见 conflicts.py 的模块文档）：
tmux 3.4 会同时读 `~/.tmux.conf` 和 `~/.config/tmux/tmux.conf`，后者在后面；tpm 用
`run-shell -b` 加载插件，那个后台任务在整份配置读完之后才跑。所以没有任何行位置能保证赢。
能做的是**不说假话**：把块外看得见的同名设置连行号一起报出来（conflicts.py），
并且说清楚这份报告不是全集。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import config as config_mod
from . import conflicts as conflicts_mod
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


# tmux 选项名 → atm 配置键。base-index 一个键管两个 tmux 选项，所以是多对一。
OPTION_TO_KEY: dict[str, str] = {
    name: f"tmux.{key}" for key, spec in SPECS.items() for name in spec.tmux_names
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
    # 块外同样设了这些选项的行。atm 只报告，不动它们 —— 见 conflicts.py
    conflicts: tuple[conflicts_mod.Conflict, ...] = ()
    other_files: tuple[Path, ...] = ()
    sourced: tuple[str, ...] = ()

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
        lines += _conflict_section(
            self.conflicts,
            self.conf_path,
            _("⚠ {path} 里这些行也设了同样的选项：").format(path=self.conf_path),
            _("  删掉它们能去掉这一层覆盖。atm 不会替你改你写的内容。"),
        )
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class TmuxOptsResult:
    conf_path: Path
    backup_path: Path | None
    written: bool  # 文件有没有动
    applied_live: bool
    live_error: str | None
    disabled: tuple[str, ...] = ()
    # 下面三项都是**写完文件之后**重新扫出来的：写入会插入/删除行，
    # 沿用 build_plan 时的行号会指到错误的位置（Codex 审出的 P1）。
    conflicts: tuple[conflicts_mod.Conflict, ...] = ()
    released: tuple[conflicts_mod.Conflict, ...] = ()
    other_files: tuple[Path, ...] = ()
    sourced: tuple[str, ...] = ()


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
    # 只对**开着**的选项报冲突：关掉的那些 atm 从不写入，用户自己设它天经地义
    watched = _watched(tuple(enabled))
    return TmuxOptsPlan(
        conf_path=path,
        enabled=tuple(enabled),
        commands=tuple(commands),
        previously_enabled=_enabled_in_block(existing) if already else (),
        already_installed=already,
        conflicts=conflicts_mod.scan(existing, watched),
        other_files=conflicts_mod.other_config_files(path),
        sourced=conflicts_mod.sourced_files(existing),
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
    # 重新读一遍写完后的文件再扫：行号必须对得上用户现在看到的文件
    final = _read(plan.conf_path)
    return TmuxOptsResult(
        plan.conf_path,
        backup,
        written,
        applied_live,
        live_error,
        plan.to_turn_off,
        conflicts_mod.scan(final, _watched(plan.enabled)),
        conflicts_mod.scan(final, _watched(plan.to_turn_off)),
        conflicts_mod.other_config_files(plan.conf_path),
        conflicts_mod.sourced_files(final),
    )


def _watched(names: tuple[str, ...]) -> dict[str, str]:
    """SPECS 的键 → 要扫的 tmux 选项名表。"""
    wanted = set(names)
    return {n: k for n, k in OPTION_TO_KEY.items() if k.removeprefix("tmux.") in wanted}


def disabled_note(names: tuple[str, ...]) -> str:
    return _("关闭选项 {names}：atm 不再管它们，运行中的值保持不变。").format(
        names=", ".join(names)
    )


def report_lines(result: TmuxOptsResult) -> list[str]:
    """写完之后要告诉用户的话。cli 和 sync 共用这一份，免得两处措辞跑偏。"""
    lines: list[str] = []
    lines += _conflict_section(
        result.conflicts,
        result.conf_path,
        _("⚠ {path} 里这些行也设了同样的选项：").format(path=result.conf_path),
        _("  删掉它们能去掉这一层覆盖。atm 不会替你改你写的内容。"),
    )
    lines += _conflict_section(
        result.released,
        result.conf_path,
        _("{path} 里这些行仍然在设它们，下次开 tmux 时还会执行：").format(path=result.conf_path),
        _("  要不要改由你决定 —— atm 不动你写的内容。"),
    )
    if result.conflicts or result.released:
        lines.append(
            _("  atm 只看了 {path}。这些地方没看，里面的同名设置也可能再盖一层：").format(
                path=result.conf_path
            )
        )
        for path in result.other_files:
            lines.append(f"    {path}")
        for src in result.sourced:
            lines.append(_("    {s}（由 source-file 引入）").format(s=src))
        lines.append(_("    tmux 插件（tpm 用 run-shell -b 加载，在配置读完之后才跑）"))
    return lines


def _conflict_section(
    found: tuple[conflicts_mod.Conflict, ...], conf_path: Path, heading: str, advice: str
) -> list[str]:
    if not found:
        return []
    certain = [c for c in found if c.certain]
    unsure = [c for c in found if not c.certain]
    lines: list[str] = [""]
    if certain:
        lines.append(heading)
        for c in certain:
            lines.append(f"    {conf_path}:{c.line_no}  {c.text}{_suffix(c)}")
        lines.append(advice)
    if unsure:
        lines.append(_("这些行也提到同样的选项，但在花括号块 / %if 里，或带了 -o，未必生效："))
        for c in unsure:
            lines.append(f"    {conf_path}:{c.line_no}  {c.text}{_suffix(c)}")
    return lines


def _suffix(c: conflicts_mod.Conflict) -> str:
    """给一行补一句「它是什么」。只说读得出来的，不猜最终结果。"""
    marks = []
    if c.unsets:
        marks.append(_("取消设置"))
    if c.after_atm_block:
        marks.append(_("在 atm 的块之后"))
    return f"   ({', '.join(marks)})" if marks else ""


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
