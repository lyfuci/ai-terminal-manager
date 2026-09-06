"""总量闸门：所有 `atm claude` / 投递出去的会话共同归入的 systemd slice。

单进程闸门（MemoryHigh / Max，见 dispatch.py）拦的是「一个会话自己失控」；
真正把机器冻死的是**总量** —— 8 月那次事故是 4 个会话合计吃掉 87% 内存。
所以每个会话的 scope 都挂在同一个 slice 下，slice 上再设一层 High / Max 兜总量。

dispatch.py 一直假定这个 slice「由用户环境提供」，但从没有人去提供 —— 单元文件躺在
research/experiments/2026-08-12-memory-slice/ 里，机器上并没有。slice 不存在时 systemd 会
自动建一个**无限制**的同名 slice，投递不会失败，只是总量闸门形同虚设。这个模块把它补上：
`atm install` 写 `~/.config/systemd/user/<slice>`，`atm doctor` 查它在不在、限制多少。

数值按机器算：软限 50% 物理内存、硬限 65%。8G 的机器是 4G / 5G（和 8 月那份手写的一致），
48G 的机器是 24G / 31G。写死一个数对两种机器都不对。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config as config_mod
from .i18n import _

HEADER = "# 由 atm install 生成；atm uninstall 会删掉它。手改无妨，但请去掉这一行，否则会被卸载。"
HIGH_RATIO = 0.50
MAX_RATIO = 0.65


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "systemd" / "user"


def total_memory_bytes(meminfo: Path = Path("/proc/meminfo")) -> int | None:
    try:
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def suggested_totals(total_bytes: int) -> tuple[str, str]:
    """(MemoryHigh, MemoryMax)，按 GiB 取整，最小 1G。"""
    gib = 1 << 30
    high = max(1, round(total_bytes * HIGH_RATIO / gib))
    hard = max(high + 1, round(total_bytes * MAX_RATIO / gib))
    return f"{high}G", f"{hard}G"


def resolve_totals(cfg: config_mod.Config, *, total_bytes: int | None = None) -> tuple[str, str]:
    """配置里的 slice-high / slice-max；"auto" 用物理内存按比例算。"""
    total = total_bytes if total_bytes is not None else total_memory_bytes()
    auto_high, auto_max = suggested_totals(total) if total else ("4G", "5G")
    high = auto_high if cfg.memory_slice_high == "auto" else cfg.memory_slice_high
    max_ = auto_max if cfg.memory_slice_max == "auto" else cfg.memory_slice_max
    return high, max_


def unit_text(*, high: str, max_: str, swap_max: str = "1G") -> str:
    return (
        "\n".join(
            (
                HEADER,
                "[Unit]",
                "Description=atm: aggregate memory guard for AI CLI sessions",
                "Documentation=https://github.com/lyfuci/ai-terminal-manager/blob/main/docs/reference.md",
                "",
                "[Slice]",
                "MemoryAccounting=yes",
                _("# 软限：超了强回收 + 节流，不杀。"),
                f"MemoryHigh={high}",
                _("# 硬限：到这里才杀。宁可掉一个会话，也不要整机冻死。"),
                f"MemoryMax={max_}",
                f"MemorySwapMax={swap_max}",
            )
        )
        + "\n"
    )


@dataclass(frozen=True, slots=True)
class SliceStatus:
    name: str
    path: Path
    exists: bool
    ours: bool
    high: str | None
    max: str | None


def status(slice_name: str, directory: Path | None = None) -> SliceStatus:
    path = (directory or unit_dir()) / slice_name
    exists = path.exists()
    ours = False
    high = max_ = None
    if exists:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        ours = text.startswith(HEADER)
        for line in text.splitlines():
            if line.startswith("MemoryHigh="):
                high = line.split("=", 1)[1]
            elif line.startswith("MemoryMax="):
                max_ = line.split("=", 1)[1]
    return SliceStatus(slice_name, path, exists, ours, high, max_)


def install(
    slice_name: str,
    directory: Path | None = None,
    *,
    total_bytes: int | None = None,
    high: str | None = None,
    max_: str | None = None,
) -> tuple[Path, bool]:
    """写单元文件。已存在就**不动**（用户可能手调过），返回 (path, written)。

    high / max_ 不给就按物理内存比例算。要「按配置更新已有的」用 sync()。
    """
    path = (directory or unit_dir()) / slice_name
    if path.exists():
        return path, False
    if high is None or max_ is None:
        total = total_bytes if total_bytes is not None else total_memory_bytes()
        auto_high, auto_max = suggested_totals(total) if total else ("4G", "5G")
        high, max_ = high or auto_high, max_ or auto_max
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text(high=high, max_=max_), encoding="utf-8")
    _reload_after_write()
    return path, True


class UnsupportedManager(RuntimeError):
    """用户目录里的单元无法约束系统 scope，不能把写错位置报成安装成功。"""


def require_user_manager(cfg: config_mod.Config) -> None:
    if not cfg.memory_user:
        raise UnsupportedManager(
            _(
                "memory.user=false：不支持安装系统级总量 slice；"
                "请自行配置系统单元，或设 memory.user=true。"
            )
        )


def sync(
    cfg: config_mod.Config, directory: Path | None = None, *, total_bytes: int | None = None
) -> tuple[Path, str]:
    """让单元文件跟上配置。返回 (path, 发生了什么)：

    written   —— 之前没有，写了
    updated   —— 是 atm 写的、数变了，重写了
    unchanged —— 是 atm 写的、数没变
    kept-user —— 用户自己写的，不动
    """
    require_user_manager(cfg)
    high, max_ = resolve_totals(cfg, total_bytes=total_bytes)
    st = status(cfg.memory_slice, directory)
    if not st.exists:
        path, _written = install(cfg.memory_slice, directory, high=high, max_=max_)
        return path, "written"
    if not st.ours:
        return st.path, "kept-user"
    if (st.high, st.max) == (high, max_):
        return st.path, "unchanged"
    st.path.write_text(unit_text(high=high, max_=max_), encoding="utf-8")
    _reload_after_write()
    return st.path, "updated"


def describe_plan(cfg: config_mod.Config, directory: Path | None = None) -> str:
    """`atm install --print` 用：会不会写、写多少。"""
    try:
        require_user_manager(cfg)
    except UnsupportedManager as exc:
        return str(exc)
    high, max_ = resolve_totals(cfg)
    st = status(cfg.memory_slice, directory)
    if st.exists and not st.ours:
        return _(
            "总量闸门 {st_path} 是你自己写的（MemoryHigh={st_high} / MemoryMax={st_max}），不动。"
        ).format(st_path=st.path, st_high=st.high, st_max=st.max)
    if st.exists and (st.high, st.max) == (high, max_):
        return _("总量闸门 {st_path} 已是 MemoryHigh={high} / MemoryMax={max_}，不动。").format(
            st_path=st.path, high=high, max_=max_
        )
    how = (
        _("物理内存 {v0}G 的 50% / 65%").format(v0=(total_memory_bytes() or 0) >> 30)
        if "auto" in (cfg.memory_slice_high, cfg.memory_slice_max)
        else _("来自 atm config memory.slice-high / slice-max")
    )
    verb = _("将更新总量闸门") if st.exists else _("将写入总量闸门")
    return _(
        "{verb} {st_path}：MemoryHigh={high} / MemoryMax={max_}（{how}），"
        "然后 systemctl --user daemon-reload。"
    ).format(verb=verb, st_path=st.path, high=high, max_=max_, how=how)


def remove(slice_name: str, directory: Path | None = None) -> bool:
    """只删我们自己写的（带 HEADER）。用户手写的一律不碰。"""
    st = status(slice_name, directory)
    if not (st.exists and st.ours):
        return False
    st.path.unlink()
    error = _daemon_reload()
    if error:
        raise ReloadFailed(_("文件已删除，重载失败：{err}").format(err=error))
    return True


class ReloadFailed(RuntimeError):
    """单元文件已改动，只有 manager 重载失败，不能报成写文件失败。"""


def _reload_after_write() -> None:
    error = _daemon_reload()
    if error:
        raise ReloadFailed(_("文件已写入，重载失败：{err}").format(err=error))


def _daemon_reload() -> str | None:
    """重载失败的原因，None = 成功。

    机器上**没有** systemctl 时返回 None 而不是报错：那种环境本来就没有 manager 要重载，
    单元文件写了也只是躺着。把它当失败会让 `atm uninstall` 在非 systemd 机器上整条命令失败。
    """
    if shutil.which("systemctl") is None:
        return None
    try:
        result = subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if result.returncode:
        return (
            result.stderr.strip()
            or result.stdout.strip()
            or _("systemctl 退出码 {code}").format(code=result.returncode)
        )
    return None
