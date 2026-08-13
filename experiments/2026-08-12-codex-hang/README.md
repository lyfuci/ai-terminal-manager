# codex exec「假死」复现（2026-08-12）

结论写在 `../../notes/2026-08-12-incident.md` 的「附二」，这里只放复现方式和原始输出。

| 文件 | 是什么 |
|---|---|
| `codex-stdin-probe.sh` | 四组对照：短/长 prompt × argv/stdin × EOF/永不 EOF |
| `out-stdin-probe.txt` | 上面那轮的原始输出（D 组 rc=124 复现了死锁） |
| `codex-longprompt-probe.sh` | 长 prompt + `< /dev/null` 连跑 3 轮，`--json` 看事件流 |
| `out-longprompt-probe.txt` | 3/3 成功 —— 推翻了「长 prompt 会挂」 |

## 复现

```bash
bash codex-stdin-probe.sh        # 约 5 分钟，D 组会占满 45s 的 timeout
bash codex-longprompt-probe.sh   # 约 1 分钟
```

两个脚本都**自带刹车**：每次调用都套 `timeout`，`rc=124` 即「被强杀＝确实挂住了」。
D 组的「永不 EOF 的写端」用 FIFO + 记录 PID 精确回收，**刻意不用 `pkill -f`** ——
本项目排障期间 `pkill -f` 自匹配误杀过三次自己的 shell。

均使用 `--ephemeral`，不往 `~/.codex` 写会话文件。

## 一次性的边界测量

单个 argv 字符串的长度上限（`MAX_ARG_STRLEN`，与 `ARG_MAX` 无关）：

```bash
for kb in 100 127 128 130 200; do
  n=$((kb*1024))
  /bin/true "$(head -c $n /dev/zero | tr '\0' x)" 2>/dev/null \
    && echo "$kb KB ok" || echo "$kb KB E2BIG"
done
```

本机结果：127KB ok / 128KB E2BIG，而 `getconf ARG_MAX` = 2097152。
