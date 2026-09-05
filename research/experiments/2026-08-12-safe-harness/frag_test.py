"""碎片化假设的对照实验（2026-08-12）。

# 要回答的问题

2026-08-12 三次整机卡死，冻结前内核报：

    page allocation failure: order:6 ... Workqueue: hv_pri_chan vmbus_add_channel_work

我当时的解释是：「反复申请/释放大块内存 → 物理内存碎片化 → 拿不到连续 256KB」。
codex 复核时指出**这个因果链未经证实**，order:6 失败可能是下游征兆而非根因。

这个实验就是去证实或推翻它。判据很干脆：
**看 `/proc/buddyinfo` 里 Normal zone 的 order≥6 空闲块数会不会被负载打下去。**

# 三个对照组

| 组 | 负载 | 想分离出什么 |
|---|---|---|
| A 静置 | 什么都不做 | 基线漂移 |
| B 稳态占用 | 一次分配 ~1GB 并持有 | 「占内存」本身会不会碎片化 |
| C 高频翻搅 | 反复分配-释放 ~500MB | 「churn」会不会碎片化 ← **核心假设** |

B 和 C 的内存**总量相近**，区别只在「持有」vs「反复翻搅」。
如果只有 C 把 order≥6 打下去，假设成立；如果两者都不掉，假设被推翻。

# 安全

全程跑在 `guard.py` 的守护下：cgroup 硬上限 + 禁 swap + PSI/swap/碎片三重阈值 +
硬性时限 + 用户随时可 `touch /tmp/atm-experiment-abort` 叫停。
本机刚被类似实验搞死过三次，所以刹车先于实验。
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from guard import ABORT_FILE, Limits, run_guarded, snapshot  # noqa: E402

# 每组跑多久（秒）。刻意短 —— 目的是看趋势，不是压垮机器。
DURATION = 25

# 翻搅组每轮分配多少 MB
CHURN_MB = 500
HOLD_MB = 1000


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


CHURN_CODE = f"""
import time
end = time.time() + {DURATION + 5}
while time.time() < end:
    # 分配后立刻写满（强制真实缺页），再整块丢弃 —— 这就是「翻搅」
    buf = bytearray({CHURN_MB} * 1024 * 1024)
    for i in range(0, len(buf), 4096):
        buf[i] = 1
    del buf
"""

HOLD_CODE = f"""
import time
buf = bytearray({HOLD_MB} * 1024 * 1024)
for i in range(0, len(buf), 4096):
    buf[i] = 1
time.sleep({DURATION + 5})
"""


def measure_group(label: str, argv: list[str] | None) -> dict:
    """跑一组，返回 order≥6 空闲块的前/中/后。"""
    before = snapshot()
    print(f"\n{'=' * 70}")
    print(f"【{label}】开始前： {before.line()}")

    lows: list[int] = []
    if argv is None:
        # 静置组：只采样
        end = time.time() + DURATION
        while time.time() < end:
            lows.append(snapshot().order6plus_normal)
            time.sleep(0.5)
        aborted, reason = False, ""
    else:
        result = run_guarded(
            label,
            argv,
            limits=Limits(deadline_sec=DURATION, min_order6_blocks=2),
            mem_max="2G",
            csv_path=Path(__file__).parent / f"trace-{label.split()[0]}.csv",
            verbose=False,
        )
        aborted, reason = result.aborted, result.reason
        # run_guarded 内部已采样，这里再补采一段观察恢复情况
        for _ in range(6):
            lows.append(snapshot().order6plus_normal)
            time.sleep(0.5)

    after = snapshot()
    print(f"【{label}】结束后： {after.line()}")

    return {
        "label": label,
        "before_o6": before.order6plus_normal,
        "after_o6": after.order6plus_normal,
        "min_o6": min(lows) if lows else -1,
        "median_o6": int(statistics.median(lows)) if lows else -1,
        "aborted": aborted,
        "reason": reason,
    }


def main() -> int:
    ABORT_FILE.unlink(missing_ok=True)
    print("碎片化对照实验")
    print(f"  每组 {DURATION}s；翻搅组每轮 {CHURN_MB}MB，稳态组持有 {HOLD_MB}MB")
    print(f"  随时叫停： touch {ABORT_FILE}")

    rows = [
        measure_group("A静置 基线", None),
        measure_group("B稳态 持有1GB", _py(HOLD_CODE)),
        measure_group("C翻搅 反复500MB", _py(CHURN_CODE)),
    ]

    print(f"\n{'=' * 70}")
    print("结果：Normal zone 里 order≥6（连续 256KB）的空闲块数")
    print(f"{'组':<20} {'开始':>6} {'最低':>6} {'中位':>6} {'结束':>6}  {'被刹住?':>8}")
    print("-" * 70)
    for r in rows:
        flag = r["reason"][:22] if r["aborted"] else "否"
        print(
            f"{r['label']:<20} {r['before_o6']:>6} {r['min_o6']:>6} "
            f"{r['median_o6']:>6} {r['after_o6']:>6}  {flag:>8}"
        )

    base, hold, churn = rows
    print(f"\n{'=' * 70}")
    print("判读：")
    if churn["min_o6"] < 0 or base["min_o6"] < 0:
        print("  拿不到 buddyinfo，无法判读。")
        return 1

    drop_churn = base["min_o6"] - churn["min_o6"]
    drop_hold = base["min_o6"] - hold["min_o6"]
    print(f"  相对基线，稳态组 order≥6 下降 {drop_hold}，翻搅组下降 {drop_churn}")
    if drop_churn > max(20, drop_hold * 2):
        print("  → 翻搅显著更伤：**支持**「churn 造成碎片化」的假设")
    elif drop_churn <= 5 and drop_hold <= 5:
        print("  → 两组都几乎没掉：**不支持**该假设，order:6 失败另有原因")
    else:
        print("  → 差异不明显，这个负载量级下无法区分；需要更大负载或更长时间")
    print("\n  ⚠️ 本实验负载远小于事故当时，结论只对这个量级成立。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
