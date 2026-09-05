"""带安全闸的实验台 —— 先有刹车，再谈实验。

# 为什么有这个东西

2026-08-12 我跑的负载实验把这台机器搞死了 2~3 次（整机卡死、用户手动重启）。
问题不在实验本身，在于**我没有任何能在出事前刹住的机制**：
脚本一旦跑起来就只能等它跑完或者机器死。

这个模块负责三件事，缺一不可：

1. **看得见** —— 持续采样 PSI / buddyinfo / swap / MemAvailable，
   这几项正是判定「内存碎片化 vs 存储栈延迟」所必需的证据（也是 codex 复核时点名要的）。
2. **刹得住** —— 任一指标越线就立刻 SIGKILL 整个 cgroup，不等、不重试、不商量。
3. **收得干净** —— 无论正常结束、越线中止、异常、还是 Ctrl-C，
   都保证 scope 被停掉、不留孤儿进程。

# 阈值刻意保守

宁可误停一次实验，也不能再死一次机器。默认阈值离真正危险还很远：
swap 只要动了 10% 就停 —— 因为 WSL 的 swap.vhdx 在 Windows 盘上，
一旦开始刷就是用户看到的「SSD 100%」。

# 安全边界

被测命令跑在独立的 systemd scope 里，带 `MemoryMax` + `MemorySwapMax=0`。
就算守护逻辑本身有 bug，内核也会先把它 OOM-kill 在 cgroup 内，不会波及整个 WSL。
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import csv
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# 用户随时可以 `touch` 这个文件来叫停正在跑的实验
ABORT_FILE = Path("/tmp/atm-experiment-abort")


# ---------------------------------------------------------------- 健康快照


@dataclass(frozen=True, slots=True)
class Health:
    """某一刻的系统健康快照。全部来自 /proc，采样本身几乎不产生负载。"""

    t: float
    mem_total_kb: int
    mem_available_kb: int
    swap_total_kb: int
    swap_free_kb: int
    psi_mem_avg10: float
    psi_io_avg10: float
    psi_cpu_avg10: float
    order6plus_normal: int  # Normal zone 里 order>=6 的空闲块总数
    order4plus_normal: int

    @property
    def mem_available_pct(self) -> float:
        return 100.0 * self.mem_available_kb / max(1, self.mem_total_kb)

    @property
    def swap_used_pct(self) -> float:
        if self.swap_total_kb <= 0:
            return 0.0
        return 100.0 * (self.swap_total_kb - self.swap_free_kb) / self.swap_total_kb

    def line(self) -> str:
        return (
            f"mem可用 {self.mem_available_pct:5.1f}%  "
            f"swap用 {self.swap_used_pct:5.1f}%  "
            f"PSI(mem/io) {self.psi_mem_avg10:5.2f}/{self.psi_io_avg10:5.2f}  "
            f"order≥6块 {self.order6plus_normal:4d}"
        )


def _meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            value = rest.strip().split(" ")[0]
            if value.isdigit():
                out[key] = int(value)
    except OSError:
        pass
    return out


def _psi(resource: str) -> float:
    """取 `some avg10`。拿不到就返回 0（不因为读不到指标而误停实验）。"""
    try:
        first = Path(f"/proc/pressure/{resource}").read_text().splitlines()[0]
    except (OSError, IndexError):
        return 0.0
    match = re.search(r"avg10=([\d.]+)", first)
    return float(match.group(1)) if match else 0.0


def _buddy_orders(zone: str = "Normal") -> tuple[int, int]:
    """返回 (order>=6 的空闲块数, order>=4 的空闲块数)。

    order:6 = 连续 256KB。2026-08-12 冻结时内核正是在申请 order:6 时失败的，
    所以这一列是判断「是不是真的碎片化」的直接证据。
    """
    try:
        for line in Path("/proc/buddyinfo").read_text().splitlines():
            if f"zone {zone:>8}" not in line and f"zone   {zone}" not in line:
                if zone not in line:
                    continue
            counts = [int(x) for x in line.split()[4:]]
            if not counts:
                continue
            return sum(counts[6:]), sum(counts[4:])
    except (OSError, ValueError):
        pass
    return -1, -1


def snapshot() -> Health:
    mi = _meminfo()
    o6, o4 = _buddy_orders()
    return Health(
        t=time.time(),
        mem_total_kb=mi.get("MemTotal", 0),
        mem_available_kb=mi.get("MemAvailable", 0),
        swap_total_kb=mi.get("SwapTotal", 0),
        swap_free_kb=mi.get("SwapFree", 0),
        psi_mem_avg10=_psi("memory"),
        psi_io_avg10=_psi("io"),
        psi_cpu_avg10=_psi("cpu"),
        order6plus_normal=o6,
        order4plus_normal=o4,
    )


# ---------------------------------------------------------------- 阈值


@dataclass(frozen=True, slots=True)
class Limits:
    """越过任一条就立刻中止。默认值刻意保守 —— 误停远好过再死一次机器。"""

    min_mem_available_pct: float = 25.0
    max_swap_used_pct: float = 10.0  # swap 一动就意味着开始刷宿主 SSD
    max_psi_mem_avg10: float = 10.0
    max_psi_io_avg10: float = 40.0
    min_order6_blocks: int = 2  # 连续 256KB 块快没了 = 碎片化正在形成
    deadline_sec: float = 300.0


def breach(h: Health, limits: Limits, started: float) -> str | None:
    """返回中止原因；一切正常返回 None。"""
    if ABORT_FILE.exists():
        return f"用户叫停（{ABORT_FILE} 出现）"
    if time.time() - started > limits.deadline_sec:
        return f"超过硬性时限 {limits.deadline_sec:.0f}s"
    if h.mem_available_pct < limits.min_mem_available_pct:
        return f"可用内存 {h.mem_available_pct:.1f}% < {limits.min_mem_available_pct}%"
    if h.swap_used_pct > limits.max_swap_used_pct:
        return f"swap 已用 {h.swap_used_pct:.1f}% > {limits.max_swap_used_pct}%（会刷宿主 SSD）"
    if h.psi_mem_avg10 > limits.max_psi_mem_avg10:
        return f"内存压力 PSI {h.psi_mem_avg10:.1f} > {limits.max_psi_mem_avg10}"
    if h.psi_io_avg10 > limits.max_psi_io_avg10:
        return f"IO 压力 PSI {h.psi_io_avg10:.1f} > {limits.max_psi_io_avg10}"
    if 0 <= h.order6plus_normal < limits.min_order6_blocks:
        return f"order≥6 空闲块仅剩 {h.order6plus_normal}（碎片化）"
    return None


# ---------------------------------------------------------------- 运行


@dataclass(frozen=True, slots=True)
class RunResult:
    label: str
    aborted: bool
    reason: str
    duration_sec: float
    peak_mem_mb: float
    samples: int


class Scope:
    """一个受 cgroup 约束的子进程，保证能被清理掉。"""

    def __init__(self, unit: str, mem_max: str) -> None:
        self.unit = unit
        self.mem_max = mem_max
        self.proc: subprocess.Popen | None = None

    def start(self, argv: list[str]) -> None:
        self.proc = subprocess.Popen(
            [
                "systemd-run", "--user", "--scope", "-q", f"--unit={self.unit}",
                "-p", f"MemoryMax={self.mem_max}", "-p", "MemorySwapMax=0",
                "-p", "MemoryAccounting=1", "-p", "IOAccounting=1",
                *argv,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        atexit.register(self.kill)

    def cgroup_dir(self) -> Path | None:
        try:
            out = subprocess.run(
                ["systemctl", "--user", "show", f"{self.unit}.scope", "-p", "ControlGroup",
                 "--value"],
                capture_output=True, text=True, timeout=5, check=False,
                stdin=subprocess.DEVNULL,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        if not out:
            return None
        path = Path("/sys/fs/cgroup" + out)
        return path if path.is_dir() else None

    def peak_mb(self) -> float:
        cg = self.cgroup_dir()
        if cg is None:
            return 0.0
        for name in ("memory.peak", "memory.current"):
            with contextlib.suppress(OSError, ValueError):
                return int((cg / name).read_text().strip()) / 1048576
        return 0.0

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self) -> None:
        """无条件停掉 —— 先 SIGKILL 进程组，再让 systemd 收尸。"""
        if self.proc is not None and self.proc.poll() is None:
            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["systemctl", "--user", "stop", f"{self.unit}.scope"],
                capture_output=True, timeout=10, check=False, stdin=subprocess.DEVNULL,
            )


def run_guarded(
    label: str,
    argv: list[str],
    *,
    limits: Limits | None = None,
    mem_max: str = "1500M",
    csv_path: Path | None = None,
    verbose: bool = True,
) -> RunResult:
    """在守护下跑一条命令。**任何情况下都会清理干净。**"""
    limits = limits or Limits()
    unit = f"atmguard-{os.getpid()}-{int(time.time() * 1000) % 100000}"
    scope = Scope(unit, mem_max)
    started = time.time()
    peak = 0.0
    samples: list[Health] = []
    reason = ""

    if verbose:
        print(f"\n▶ {label}")
        print(f"  cgroup 上限 {mem_max} / 禁 swap / 时限 {limits.deadline_sec:.0f}s")
        print(f"  随时叫停： touch {ABORT_FILE}")

    try:
        scope.start(argv)
        while scope.alive():
            h = snapshot()
            samples.append(h)
            peak = max(peak, scope.peak_mb())

            why = breach(h, limits, started)
            if why:
                reason = why
                if verbose:
                    print(f"  ⛔ 中止：{why}")
                    print(f"     现场：{h.line()}")
                break

            if verbose and len(samples) % 10 == 0:
                print(f"  · {h.line()}  峰值 {peak:6.1f}MB")
            time.sleep(0.5)
    except KeyboardInterrupt:
        reason = "Ctrl-C"
    except Exception as exc:  # noqa: BLE001 - 守护逻辑本身不能把异常漏出去
        reason = f"守护异常：{exc!r}"
    finally:
        peak = max(peak, scope.peak_mb())
        scope.kill()

    duration = time.time() - started
    if csv_path and samples:
        _write_csv(csv_path, samples)

    result = RunResult(
        label=label,
        aborted=bool(reason),
        reason=reason,
        duration_sec=duration,
        peak_mem_mb=peak,
        samples=len(samples),
    )
    if verbose:
        status = f"⛔ {reason}" if reason else "✓ 正常结束"
        print(f"  {status}   峰值 {peak:.1f}MB   {duration:.1f}s   采样 {len(samples)} 次")
    return result


def _write_csv(path: Path, samples: list[Health]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(samples[0]).keys()))
        writer.writeheader()
        for s in samples:
            writer.writerow(asdict(s))


# ---------------------------------------------------------------- 自检


def self_test() -> int:
    """**验证刹车真的能刹住** —— 光说安全没用，得让它当场刹一次。

    用一个受 cgroup 限制的内存炸弹触发闸门。因为有 MemoryMax 兜底，
    这个测试本身对整机是安全的。
    """
    print("=" * 66)
    print("自检 1/3：正常命令应当跑完且不触发闸门")
    ok1 = run_guarded("sleep 2", ["sleep", "2"], limits=Limits(deadline_sec=30))

    print("\n" + "=" * 66)
    print("自检 2/3：超时应当被硬性时限刹住")
    ok2 = run_guarded("sleep 600（应被时限中止）", ["sleep", "600"],
                      limits=Limits(deadline_sec=3))

    print("\n" + "=" * 66)
    print("自检 3/3：用户叫停文件应当立刻生效")
    ABORT_FILE.touch()
    try:
        ok3 = run_guarded("sleep 600（应被用户叫停）", ["sleep", "600"],
                          limits=Limits(deadline_sec=60))
    finally:
        ABORT_FILE.unlink(missing_ok=True)

    print("\n" + "=" * 66)
    checks = [
        ("正常命令不被误停", not ok1.aborted),
        ("硬性时限能刹住", ok2.aborted and "时限" in ok2.reason),
        ("用户叫停能刹住", ok3.aborted and "用户叫停" in ok3.reason),
        ("叫停后无残留进程", _no_leftovers()),
    ]
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    return 0 if all(p for _, p in checks) else 1


def _no_leftovers() -> bool:
    try:
        out = subprocess.run(
            ["systemctl", "--user", "list-units", "--type=scope", "--no-legend"],
            capture_output=True, text=True, timeout=10, check=False, stdin=subprocess.DEVNULL,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return "atmguard-" not in out


def main() -> int:
    parser = argparse.ArgumentParser(description="带安全闸的实验台")
    parser.add_argument("--self-test", action="store_true", help="验证刹车机制本身")
    parser.add_argument("--watch", type=float, metavar="SEC", help="只监控，不跑命令")
    parser.add_argument("cmd", nargs="*", help="要在守护下运行的命令")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if args.watch:
        end = time.time() + args.watch
        while time.time() < end:
            print(f"  {snapshot().line()}")
            time.sleep(1)
        return 0
    if not args.cmd:
        print(snapshot().line())
        return 0
    result = run_guarded(" ".join(args.cmd), args.cmd)
    return 1 if result.aborted else 0


if __name__ == "__main__":
    raise SystemExit(main())
