# atm — 用法

[English](usage.md) | **中文** | [日本語](usage-ja.md)

回 [README](../README-cn.md)。完整选项与格式：[reference.md](reference.md)。

---

## 用

装完就是四个键（`prefix` 默认 `Ctrl-b`）：

| 键 | 干什么 |
|---|---|
| `prefix + a` | **浮层**：模糊搜全部历史会话 → 选目标格子 → 会话在那格 `--resume` 起来 |
| `prefix + A` | 同上，但只看当前目录（含子目录）的会话 |
| `prefix + b` | **侧栏**：没开就在最左边开一条通高的；开了就切过去；已经在里面就收起 |
| `prefix + B` | 把当前格子收进后台窗口 `bg` —— 进程继续跑，之后从侧栏里还能选回来 |

**浮层里**：打字模糊搜索，`↑↓` / `^N` `^P` 移动，`Tab` 在 全部 / Claude / Codex / Pi 之间循环，`⏎` 选中，`Esc` 取消，`F1` / `?`（搜索框为空时）弹出完整键位表。
选中会话后进第二步：列出所有 pane（带忙闲状态）+「新分一个 pane」+「新开 window」+「只打印」。

**侧栏里**：上半段是**正在跑的格子**（选中 → `swap-pane` 换进主格，进程不断），下半段是**历史**
（选中 → 后台新窗口里 resume 再换进来）。`⏎` 换进主格，`^T` 挑具体换进哪格，`^X` 把选中的收进 `bg`，
`Tab` 切来源，`^R` 重建索引，`^C` 退出，`F1` / `?` 看完整键位。

**命令行**同样能用（不在 tmux 里时 `pick` 自动退化成打印命令，`eval "$(atm pick --print)"`）：

```bash
atm list -n 20            # 列最近 20 条；--source codex|claude|pi 只看一家；--json 喂给别的脚本
atm pick                  # 交互选会话 → 选目标 pane → 投递
atm resume <id前缀>       # 不进 TUI，按 id 直接投
atm panes                 # 列出所有 tmux pane 及忙闲状态
atm swap %7 --into %3     # 把 %7 换进 %3
atm park                  # 当前格子收进 bg
atm prune -n              # 看看 bg 里有哪些空闲 shell 可以关（去掉 -n 才真关）
atm index --rebuild       # 清缓存全量重建
atm update                # 升级 atm 自己（识别 uv tool / pipx / pip）；--check 只看不升。镜像没同步到新版时会改直连 PyPI 再试
```

> 投递默认套一层 cgroup 内存闸门（`MemoryHigh=2G` / `MemoryMax=4G`）。
> 起因是实测撞上 WSL 内存上限时**整个 tmux server 连同所有会话一起死掉**过一次。
> 阈值怎么定的、怎么关，见 `docs/reference.md`「内存闸门」。

**完整选项、实测性能、三个 JSONL 的格式细节：[`docs/reference.md`](reference.md)。**
开发和贡献：[CONTRIBUTING.md](../CONTRIBUTING.md)。

---

## 日常 CLI 基础

```bash
atm -v list                    # stderr 打过程信息；-vv 到逐文件 / 逐条 tmux 命令（ATM_DEBUG=1 等于 -vv）
atm doctor --json              # 机器可读的体检报告；只有配置文件坏了才 exit 1
atm config --json              # 每一项的值和来源：default / file / env
atm update --check --json
eval "$(atm completion bash)"  # 从真实的参数定义生成补全（zsh、fish 同理）
NO_COLOR=1 atm pick            # 遵守 https://no-color.org
ATM_LANG=en atm --help         # 界面语言：跟系统 LC_ALL / LC_MESSAGES / LANG（zh / ja，其余英文）；ATM_LANG 可强制
```

配置优先级：**命令行参数 > 环境变量 > 文件 > 默认**。每个键都有环境变量：
`memory.high` → `ATM_MEMORY_HIGH`，`memory.swap-max` → `ATM_MEMORY_SWAP_MAX`，以此类推。
拼错的键、坏掉的文件都是错误，不会静默忽略——否则你会以为限制生效了其实没有。

`atm install` 只问最后一句「继续吗」：所有可调的值都在 `atm config` 里，install 只是按配置应用（键位、总量 slice、tmux 选项）。
`--key s` 这类参数是快捷写法，会先记进 config 再装。
`atm install --conf PATH` / `atm uninstall --conf PATH` 可以指向 `~/.tmux.conf` 以外的配置。
`eval "$(atm pick --print)"` 在 stdout 被接走时也能用：选择器画在 `/dev/tty` 上。

## 内存闸门：`atm claude` 和 `claude` 的区别

```bash
atm config                     # 交互式编辑器：↑↓ 选键，Enter 改/切换，s 保存，? 帮助；右侧面板按界面语言说明选中项（格式 / 默认 / 环境变量 / 来源）（atm config --show 只打印）
atm config memory.high 4G      # 软上限：节流 + 回收，不杀
atm config keys.pick s         # 选择器键（大写 = 只看当前目录）；还有 keys.sidebar、keys.popup-width/-height。保存即对运行中的 server 重绑
atm config tmux.mouse true     # tmux 常用选项：mouse / focus-events / history-limit / base-index / renumber-windows → 写进 ~/.tmux.conf **最前面**的独立块（你后面的行能盖掉它），立即生效
atm config memory.slice-high 20G  # 总量 slice 的数（默认 auto = 物理内存 50% / 65%）；atm 写的单元会重写 + daemon-reload
atm config memory.max 8G       # 硬上限：杀整个会话 scope（含子进程）
atm claude --resume <id>       # 在这个 cgroup 里启动 claude；参数原样透传
claude                         # 不带前缀 = 原生，不套任何限制
```

`atm codex …` / `atm pi …` 同理。`prefix + a` 投递和侧栏恢复用的是同一套设置。
`atm install` 还会写一个总量 `atm-ai.slice`（物理内存的 50% / 65%），N 个会话加起来也压不垮机器；
`atm doctor` 两层都报。默认值怎么来的见 [reference.md](reference.md#内存闸门默认开)。

## 它怎么工作（三分钟版）

**「记住状态」其实是三层**，atm 只碰其中两层，第三层交给 tmux：

| 层 | 含义 | 谁负责 |
|---|---|---|
| **L1 视觉** | 布局分割、每格 cwd、滚动缓冲区 | tmux-resurrect（atm install 顺手装好） |
| **L2 进程** | 关掉 UI 后 `claude` 进程还在跑 | tmux server 本身；atm 的侧栏用 `swap-pane` 在这一层换位 |
| **L3 会话** | AI 对话上下文 | CLI 自带 `--resume`；atm 的索引 + 浮层负责把它找出来、投到正确的格子 |

> **L3 顶替不了 L2**：`--resume` 恢复的是对话历史，不是跑到一半的进程。这是侧栏存在的理由。

**数据从哪来**：只读三家 CLI 自己写的会话文件，只读文件头部（标题 / cwd / branch 实测都在头部），
按 `(mtime_ns, size)` 缓存——213 个会话 1.73 GB 的语料冷启动 198ms、热启动 5ms。
格式是逆向观察出来的、不是公开契约，所以解析全程防御式：一条脏行不会让整个列表挂掉。

**核心手势就一行**：

```
tmux send-keys -t %<pane-id> -l -- "cd <cwd> && claude --resume <sessionId>"
```

（`-l --` 是必须的：否则命令里的 `Enter` / `C-c` 这类词会被 tmux 当**键名**解释。）

---

## 项目状态

🟢 **路线 C 已拍板并落地**（2026-08-12）：范围收敛到「跨 agent 统一历史 → 投到指定 tmux pane」，
之后加了常驻侧栏（09-02）、Pi 支持和持久化安装（09-05）。
Python 3.11+ 零运行时依赖，240+ 测试，MIT。

> 架构分岔口 A（tmux 后端 + GUI）/ B（自写 daemon）**没有被否掉，只是没做**——
> 决策变量（需不需要跨端 SSH 接管）仍未回答。真要做时，`src/atm/index.py` 那层可以整块复用。详见[研究记录](../research/README-cn.md)。

