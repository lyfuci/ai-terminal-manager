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
                "# 软限：超了强回收 + 节流，不杀。",
                f"MemoryHigh={high}",
                "# 硬限：到这里才杀。宁可掉一个会话，也不要整机冻死。",
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
    slice_name: str, directory: Path | None = None, *, total_bytes: int | None = None
) -> tuple[Path, bool]:
    """写单元文件。已存在就**不动**（用户可能手调过），返回 (path, written)。"""
    path = (directory or unit_dir()) / slice_name
    if path.exists():
        return path, False
    total = total_bytes if total_bytes is not None else total_memory_bytes()
    high, max_ = suggested_totals(total) if total else ("4G", "5G")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text(high=high, max_=max_), encoding="utf-8")
    _daemon_reload()
    return path, True


def remove(slice_name: str, directory: Path | None = None) -> bool:
    """只删我们自己写的（带 HEADER）。用户手写的一律不碰。"""
    st = status(slice_name, directory)
    if not (st.exists and st.ours):
        return False
    st.path.unlink()
    _daemon_reload()
    return True


def _daemon_reload() -> None:
    if shutil.which("systemctl") is None:
        return
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
