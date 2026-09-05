# 内存保护绕过修复的验证（2026-08-12）

结论与完整修法见 `../../research/notes/2026-08-12-incident.md` 的「附三」。

| 文件 | 是什么 |
|---|---|
| `test-resurrect-slice.sh` | 端到端验证：resurrect 的 `->` 映射能否让**恢复出来的**进程带上 cgroup 限制 |
| `atm-ai.slice` | 装到 `~/.config/systemd/user/` 的总量闸门（副本，便于重装） |
| `ai-scope` | 装到 `~/.local/bin/` 的单进程 wrapper（副本） |

## 跑测试

```bash
bash test-resurrect-slice.sh     # 约 20 秒
```

⚠️ 脚本里有两条**必须遵守**的约束，都是踩过才知道的：

1. **`-L` 独立 socket + `-f /dev/null`**。独立 server 照样 source `~/.tmux.conf`，
   而配置里 `@continuum-restore on` 会让它把用户的真实存档整个恢复一遍
   —— 实测一次拉起 5 个 claude。
2. **先建占位会话再 kill 原会话**。杀掉唯一的会话会连 server 一起销毁，
   设在 server 上的 `@resurrect-dir` 随之消失，restore 回落到默认目录（用户的真实存档）。

## 期望输出

```
存档里记的        sleep 4321        ← 裸命令，wrapper 对 ps 不可见
恢复后的 cgroup   .../atm-ai.slice/run-*.scope     ✅
单进程限制        memory.high=2G  memory.max=4G    ✅
```
