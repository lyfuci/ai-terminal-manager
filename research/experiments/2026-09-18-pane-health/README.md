# 格子健康的依据（2026-09-18）

`measure.sh` 回答两个问题：一个撞 MemoryHigh 的进程到底多慢；内核的哪个数能看出来。
结果在 `result-2026-09-18.txt`（WSL2，内核 6.6，cgroup v2，tmux 3.6）。

| | 不限制 | MemoryHigh=64M（工作集 200M） |
|---|---|---|
| 吞吐 | 560 轮/秒 | 0.88 轮/秒（慢约 640 倍） |
| memory PSI some avg10 | — | 1.2% |
| io PSI some avg10 | — | 8.7% |
| utime / stime（5 秒，100 tick/s） | — | +6 / +135 |
| scope `memory.events.local` high | 0 | 464/s |
| 父层 `app.slice` `memory.events` high | — | 464/s（层级累计） |
| 父层 `app.slice` `memory.events.local` high | — | 0/s |

结论：

1. 被回收拖住的进程不是在「等」，是在内核里「干活」——PSI 几乎看不见。判定必须加上 high 事件速率。
2. 速率必须读 `memory.events.local`，否则一个格子撞限，同一祖先下所有格子都会被误报。
3. 侧栏（隔离 socket）在 12 秒内标出 `⚠回收`，进程结束后标记消失，日志里有一对 started / ended（15.2 秒）。

跑法：`research/experiments/2026-09-18-pane-health/measure.sh`（需要 `uv sync` 过的 `.venv`；
只动临时 scope 和 `-L` + `-f /dev/null` 的隔离 tmux server，退出时清掉）。
