# 测「交互式 CLI 的内存占用」踩过的四个坑

这个实验的脚本我写错了 **4 次**，每次都产出了看起来很干净、实际是假的数字。
记在这里，因为每一条都会让人得出「没有差异」的错误结论。

## 坑 1：在 cgroup 销毁之后才读 `memory.peak`

```bash
wait $runner
peak=$(cat /sys/fs/cgroup$cg/memory.peak)   # ❌ systemd 已经把 cgroup 删了
```

**症状**：所有档位都是 `0.0 MB`。
**正确做法**：在 scope **存活期间**轮询 `memory.current` 取最大值。

## 坑 2：循环体里的命令吃掉了 stdin

```bash
while IFS= read -r line; do
    measure ...          # ❌ 里面的 systemctl / wait 把 stdin 读空了
done < <(printf '%s\n' "${SAMPLES[@]}")
```

**症状**：只跑了第一个样本就退出，后面的档位静默消失。
**正确做法**：给每个可能读 stdin 的命令加 `</dev/null`。

## 坑 3：`timeout` 的 SIGTERM 杀不掉 claude

**症状**：`timeout 25s claude` 在 10 分钟后仍然活着，整个脚本挂住。
**实测**：claude 会吞掉 SIGTERM。
**正确做法**：`timeout -k 3 -s TERM 40s ...` 加 SIGKILL 兜底，
scope 再加一层 `-p RuntimeMaxSec=`。

## 坑 4（最阴险）：在命令里重定向 stdout，导致被测程序根本没干活

```bash
script -qec "timeout 40s claude --resume $id >/dev/null 2>&1" /dev/null
#                                            ^^^^^^^^^^^^^^^ ❌
```

`script` 本来给了子命令一个 PTY，但**命令内部的 `>/dev/null` 又把 stdout 换成了非终端**。
claude 检测到不在终端里，就不跑完整 TUI，**也就不会真正加载会话**。

**症状**：所有档位都是 71MB，看起来「转录大小对内存零影响」——
一个非常干净、非常有说服力、但完全错误的结论。

**戳破它的方法**：拿同一条会话手动跑一遍、**把输出留下来看**。
一看就发现 claude 其实停在这个界面上：

```
This session is 3h 31m old and 909.6k tokens.
❯ 1. Resume from summary (recommended)  2. Resume full session as-is
```

而峰值是 **334 MB**，不是 71 MB。

**正确做法**：typescript 写 `/dev/null` 就够了（`script -qec "cmd" /dev/null`），
**绝不要在 cmd 内部再重定向 stdout**。

---

## 通用教训

> 测量脚本产出「干净的负结果」时，**先怀疑测量本身**。
> 这四次里有三次都给出了「各档位完全一致」这种漂亮结果，而它们全是错的。

验证手段：**用一个已知会产生差异的场景做正对照**。
这里就是「手动跑一次、把输出打出来看被测程序到底走到哪一步了」。
