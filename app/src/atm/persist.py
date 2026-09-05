"""`atm install` 顺手把 tmux 的跨重启持久化装上：tpm + tmux-resurrect + tmux-continuum。

atm 解决的是「AI 会话历史」这一层；「重启后我的窗口 / 分格 / 每格目录还在不在」是 tmux 生态
早就解决好的（见仓库 README「路线 C」那张表）。两者拼起来才是完整体验：
resurrect 把骨架搭回来，用户在对应的格子里 `prefix + a` 把昨天的对话 resume 回去。

设计上和 install.py 的键位块**互相独立**：各自一对 marker、各自幂等、各自可卸载。
`run tpm` 那行不需要真的在文件最末 —— tpm 只要求它在所有 `@plugin` 之后，
后面再来几条 bind-key 不影响。所以两个块谁先谁后无所谓，重装键位把它挪到后面也没事。

刻意**不做**的三件事，都有血泪教训：
1. 不把 claude / codex 加进 `@resurrect-processes`。resurrect 会在开机时把它们**全部同时**拉起，
   本机实测一次吃掉 87% 内存冻死两次（notes/2026-08-12-incident.md 附三）。
   恢复出来的是带 cwd 的空 shell，会话由用户按需 resume —— 这正是 atm 的活。
2. 不对正在跑的 server 做「立即生效」。tpm 加载会跑 continuum 的初始化，
   `@continuum-restore on` 在某些时机会对活着的 server 做一次全量恢复，
   把用户现有布局搅乱。写好配置，告诉用户下次起 server 生效即可。
3. 不生成开机自启（`@continuum-boot`）。那会往 systemd / launchd 写单元，副作用超出「装个插件」，
   想要的用户自己加一行就行。

另一个查证过的坑（README「已知的坑」第 4 条）：continuum 加载时只要机器上**还有别的 tmux server**，
就静默不装自动存档钩子，`@continuum-*` 选项看着全正常。唯一可靠的判据是 status-right 里
有没有 `continuum_save.sh` —— `atm doctor` 就查这个。
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import tmux
from .install import _backup, _has_marker, _read, _strip_block

MARKER_BEGIN = "# >>> atm persist (tmux-resurrect + tmux-continuum) >>>"
MARKER_END = "# <<< atm persist <<<"

DEFAULT_PLUGINS_DIR = Path.home() / ".tmux" / "plugins"
PLUGINS: tuple[str, ...] = ("tpm", "tmux-resurrect", "tmux-continuum")
REPO_URL = "https://github.com/tmux-plugins/{name}.git"
DEFAULT_SAVE_INTERVAL = 10  # 分钟

# resurrect 3.x 起默认存这里（不是老文档写的 ~/.tmux/resurrect）。
SAVE_DIR = Path.home() / ".local" / "share" / "tmux" / "resurrect"

# 用户自己在管 tpm 的迹象：块外出现这两种行之一，就不再写我们的块（会重复初始化 tpm）。
_TPM_SIGNS = ("plugins/tpm/tpm", "@plugin")

Cloner = Callable[[str, Path], None]


@dataclass(frozen=True, slots=True)
class PersistPlan:
    conf_path: Path
    plugins_dir: Path
    block: str
    already_installed: bool
    user_manages_tpm: bool
    missing_plugins: tuple[str, ...]
    git_available: bool

    @property
    def will_write_block(self) -> bool:
        return not self.user_manages_tpm

    def describe(self) -> str:
        lines: list[str] = []
        if self.user_manages_tpm:
            lines.append(
                f"{self.conf_path} 里已经有你自己的 tpm 配置，不重复写插件块；"
                f"要恢复功能请自行加上 tmux-resurrect / tmux-continuum。"
            )
            return "\n".join(lines)

        lines.append(f"将写入 {self.conf_path}（持久化块）：")
        lines.append("")
        lines += [f"  {line}" for line in self.block.splitlines()]
        lines.append("")
        if self.missing_plugins:
            names = ", ".join(self.missing_plugins)
            lines.append(f"将 git clone 到 {self.plugins_dir}/：{names}")
            if not self.git_available:
                lines.append(
                    "  ⚠ 没找到 git，克隆会跳过；装好 git 后重跑，或在 tmux 里按 prefix + I"
                )
        else:
            lines.append(f"插件已在 {self.plugins_dir}/，不重复克隆")
        if self.already_installed:
            lines.append("（已存在持久化块，会被整块替换掉，不会重复追加）")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class PersistResult:
    conf_path: Path
    backup_path: Path | None
    block_written: bool
    cloned: tuple[str, ...]
    clone_errors: dict[str, str]


@dataclass(frozen=True, slots=True)
class PersistStatus:
    """给 `atm doctor` 用。`autosave_hooked` 为 None 表示没有 server、查不了。"""

    block_installed: bool
    plugins_present: tuple[str, ...]
    plugins_missing: tuple[str, ...]
    autosave_hooked: bool | None
    last_save: Path | None


def build_block(plugins_dir: Path, *, save_interval: int = DEFAULT_SAVE_INTERVAL) -> str:
    tpm = plugins_dir / "tpm" / "tpm"
    body = "\n".join(
        (
            "# 重启后恢复 session / window / pane / cwd（只搭骨架，不重新拉起 claude / codex ——",
            "# 会话在对应格子里用 atm 按需 resume，别把它们加进 @resurrect-processes）",
            "set -g @plugin 'tmux-plugins/tpm'",
            "set -g @plugin 'tmux-plugins/tmux-resurrect'",
            "set -g @plugin 'tmux-plugins/tmux-continuum'",
            "set -g @continuum-restore 'on'",
            f"set -g @continuum-save-interval '{save_interval}'",
            f"run '{tpm}'",
        )
    )
    return f"{MARKER_BEGIN}\n{body}\n{MARKER_END}"


def build_plan(
    *,
    conf_path: Path | None = None,
    plugins_dir: Path | None = None,
    save_interval: int = DEFAULT_SAVE_INTERVAL,
) -> PersistPlan:
    path = conf_path or Path.home() / ".tmux.conf"
    pdir = plugins_dir or DEFAULT_PLUGINS_DIR
    existing = _read(path)
    already = _has_marker(existing, MARKER_BEGIN)
    outside = _strip_block(existing, MARKER_BEGIN, MARKER_END) if already else existing
    return PersistPlan(
        conf_path=path,
        plugins_dir=pdir,
        block=build_block(pdir, save_interval=save_interval),
        already_installed=already,
        user_manages_tpm=_mentions_tpm(outside),
        missing_plugins=tuple(name for name in PLUGINS if not (pdir / name).is_dir()),
        git_available=shutil.which("git") is not None,
    )


def apply(plan: PersistPlan, *, cloner: Cloner | None = None) -> PersistResult:
    """克隆缺的插件 + 写配置块。克隆失败不算致命：配置照写，用户可以事后 prefix + I 补。"""
    clone = cloner or _git_clone
    cloned: list[str] = []
    errors: dict[str, str] = {}
    if plan.will_write_block and plan.missing_plugins:
        plan.plugins_dir.mkdir(parents=True, exist_ok=True)
        for name in plan.missing_plugins:
            try:
                clone(name, plan.plugins_dir / name)
                cloned.append(name)
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                errors[name] = str(exc)

    if not plan.will_write_block:
        return PersistResult(plan.conf_path, None, False, tuple(cloned), errors)

    existing = _read(plan.conf_path)
    backup = _backup(plan.conf_path) if existing else None
    updated = _strip_block(existing, MARKER_BEGIN, MARKER_END)
    if updated and not updated.endswith("\n"):
        updated += "\n"
    updated = f"{updated}\n{plan.block}\n" if updated else f"{plan.block}\n"
    plan.conf_path.parent.mkdir(parents=True, exist_ok=True)
    plan.conf_path.write_text(updated, encoding="utf-8")
    return PersistResult(plan.conf_path, backup, True, tuple(cloned), errors)


def remove(conf_path: Path | None = None) -> tuple[bool, Path | None]:
    """只删我们的块。已克隆的插件目录不动 —— 用户可能还想留着。"""
    path = conf_path or Path.home() / ".tmux.conf"
    existing = _read(path)
    if not _has_marker(existing, MARKER_BEGIN):
        return False, None
    backup = _backup(path)
    path.write_text(_strip_block(existing, MARKER_BEGIN, MARKER_END), encoding="utf-8")
    return True, backup


def status(*, conf_path: Path | None = None, plugins_dir: Path | None = None) -> PersistStatus:
    path = conf_path or Path.home() / ".tmux.conf"
    pdir = plugins_dir or DEFAULT_PLUGINS_DIR
    present = tuple(name for name in PLUGINS if (pdir / name).is_dir())
    missing = tuple(name for name in PLUGINS if name not in present)

    hooked: bool | None = None
    if tmux.has_server():
        try:
            hooked = "continuum_save.sh" in tmux.run(["show-options", "-gv", "status-right"])
        except tmux.TmuxError:
            hooked = None

    last = SAVE_DIR / "last"
    return PersistStatus(
        block_installed=_has_marker(_read(path), MARKER_BEGIN),
        plugins_present=present,
        plugins_missing=missing,
        autosave_hooked=hooked,
        last_save=last if last.exists() else None,
    )


def tmux_install_hint() -> str:
    """tmux 没装时给一条能直接复制的命令。不替用户跑 sudo。"""
    for manager, command in (
        ("apt-get", "sudo apt-get install -y tmux"),
        ("dnf", "sudo dnf install -y tmux"),
        ("pacman", "sudo pacman -S --noconfirm tmux"),
        ("zypper", "sudo zypper install -y tmux"),
        ("brew", "brew install tmux"),
    ):
        if shutil.which(manager):
            return command
    return "用你的包管理器装 tmux（>= 3.2，需要 display-popup）"


def _mentions_tpm(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if any(sign in stripped for sign in _TPM_SIGNS):
            return True
    return False


def _git_clone(name: str, dest: Path) -> None:
    if not shutil.which("git"):
        raise RuntimeError("没找到 git")
    result = subprocess.run(
        ["git", "clone", "--depth", "1", "--quiet", REPO_URL.format(name=name), str(dest)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or f"git clone {name} 失败")
