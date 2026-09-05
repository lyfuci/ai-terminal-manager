# resume vs 全新会话的内存代价（2026-08-12 实测）

## 数据来源：systemd 自己的 cgroup 记账

我自己写的采样脚本错了 5 次（见 `PITFALLS.md`）。最后**真正可信的数字来自 systemd** ——
每个 `systemd-run --scope` 结束时它会主动把峰值记进 journal，不需要我采样：

```
atmcost-*.scope: Consumed 5.5s CPU time over 40.7s wall clock time, 254.7M memory peak
```

取自 `journalctl --user -b -1 | grep 'memory peak'`。

## 结果

| 场景 | 峰值 RSS | 相对全新 |
|---|---|---|
| `claude`（全新会话） | **254.7 MB** | — |
| `claude --resume` 0.2MB 转录 | 268.6 MB | +5% |
| `claude --resume` 7.4MB 转录 | 269.0 MB | +6% |
| `claude --resume` 20.8MB 转录 | **311.2 MB** | **+22%** |

（另有一次 348.9MB / 37.1s，是被 SIGKILL 截断的那次，不计入。）

## 结论：一个被推翻的假设

我原本的假设是「**用户手动开 6 个是全新会话，atm 让他开的是 resume 会话，两者代价不是一个量级**」，
以此解释为什么「以前开 6 个没事，用了 atm 就崩」。

**这个假设是错的。** 实测 resume 只比全新贵 **5%~22%**，同一个量级。
转录从 0.2MB 涨到 20.8MB（100 倍），峰值内存只从 268MB 涨到 311MB（+16%）。

这与 codex 的判断一致：它在二进制里找到 `createReadStream` / `transcript ranged read`，
指出 claude **不是**把整个 jsonl 读进内存。实测证实了这一点。

## 顺带确认

- claude 单进程稳态约 **250–310 MB**。在 6GB 的 WSL 里，理论上能开十几个。
- claude **会吞掉 SIGTERM**：`timeout 25s claude` 十分钟后仍然存活。
  要用 `timeout -k` 加 SIGKILL 兜底，否则测量脚本会挂住。
- resume 大会话时 claude 自己会弹 token 级警告：
  `This session is 3h 31m old and 909.6k tokens. ❯ 1. Resume from summary (recommended)`
  —— 所以 atm 不需要再造一个字节级的闸门（那一版已删除）。
