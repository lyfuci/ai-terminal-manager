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

**浮层里**：打字模糊搜索，`↑↓` / `^N` `^P` 移动，`Tab` 在 全部 / Claude / Codex / Pi / Gemini / opencode 之间循环，`⏎` 选中，`Esc` 取消，`F1` / `?`（搜索框为空时）弹出完整键位表。
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
atm restore               # 重启之后：把上次的会话填回恢复出来的空格子
atm update                # 升级 atm 自己（识别 uv tool / pipx / pip）；--check 只看不升。镜像没同步到新版时会改直连 PyPI 再试
```

> 投递默认套一层 cgroup 内存闸门，数值按机器算（`memory.high` / `memory.max` 默认 `auto`：
> Max = 物理内存 35%、下限 4G，High = Max 的 80%）。
> 起因是实测撞上 WSL 内存上限时**整个 tmux server 连同所有会话一起死掉**过一次。
> 阈值怎么定的、怎么关，见 `docs/reference.md`「内存闸门」。

**完整选项、实测性能、三个 JSONL 的格式细节：[`docs/reference.md`](reference.md)。**
开发和贡献：[CONTRIBUTING.md](../CONTRIBUTING.md)。

---

## 重启之后：`atm restore`

tmux-resurrect 能把**骨架**搭回来 —— window、分格、每格的工作目录 —— 但格子里是空 shell。
atm 刻意**不**把 AI CLI 加进 `@resurrect-processes`，因为那会在开机时把它们**全部同时**拉起：
本机实测四个会话合计吃掉 87% 内存，冻死过两次。

`atm restore` 就是把这些格子一条一条填回去：

```bash
atm restore                  # 当前所在的 tmux 会话；先给计划，确认后再填
atm restore -t work          # 换一个会话；`-t work:1` 只填那个 window
atm restore --all            # 存档里所有会话
atm restore --print          # 只看计划
atm restore -y               # 不问直接填
```

计划会把每一条的去向都说清楚，包括**不动**的那些：

```
将恢复 2 条会话：
  main:1.1  claude    把索引层的缓存加上
  main:1.4  codex     整理发版脚本

以下 2 条不动：
  main:1.2  * github  —— 跳过：这个格子里已经在跑东西了
  main:2.1  旧的活    —— 跳过：布局里没有这一格了
```

**在跑东西的格子绝不会被覆盖** —— 这是整个命令围着转的那条不变量。
投递是**串行**的（三个 CLI 同时各读一份 20MB+ 的转录是实实在在的尖峰），走 atm 正常的投递路径，
所以每一条都套着 cgroup 内存闸门。

### 让它开机自动跑

默认关。打开之后 `atm install` 会把这条命令挂到 resurrect 的 post-restore 钩子上：

```bash
atm config restore.on-boot true
atm install                  # 写钩子；下次起 tmux server 生效
```

**如果 tpm 是你自己管的**，atm 不会写持久化块，`atm install` 也就不会装钩子 —— 它会把这件事说出来，
并给出可直接粘贴的那一行，放进你自己的块里、`run '…/tpm'` 之前：

```tmux
set -g @resurrect-hook-post-restore-all '/path/to/atm restore --boot'
```

`atm doctor` 会去运行中的 server 上查这个钩子，不在就把这个设置标成「开着但不会真的恢复」——
`restore.on-boot = true` 不会再悄悄什么都不做。

开机那一次在动手之前先查三件事，任何一条不过就让路：

| 查什么 | 为什么 |
|---|---|
| cgroup 内存闸门在不在 | 没有闸门，批量恢复就没有兜底 |
| 上一次开机恢复有没有跑完 | 没跑完说明被杀了 —— 这条专门用来打断「恢复 → 冻死 → 重启 → 再恢复」的循环 |
| `MemAvailable` 高于 `restore.min-available`（默认 `4G`） | 每投一条之前都重查一遍，所以开机恢复会退化成「填到内存吃紧为止」，而不是硬填完 |

开机时没有终端，所以做了什么写进 `~/.local/state/atm/restore.log`，`atm doctor` 里也有一行当前状态。
上一次被打断的话，提示会直接告诉你怎么重新打开：手动跑一次 `atm restore` 确认没问题，
然后删掉 `~/.local/state/atm/boot-restore.json`。

> atm 不去 journal 里查 OOM 记录。不在 `adm` / `systemd-journal` 组时，`journalctl` **只看得到你自己的日志**，
> 内核的 OOM 行根本看不见 —— 一个只会回答「一切正常」的检查比没有检查更糟。

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

## 配置变更

配置编辑只保存文件值和你明确修改的项；环境变量覆盖保持临时生效，编辑器修改后仍保留 `← env` 标记。`atm config --reset` 删除 TOML，并按默认值同步 tmux 块和总量限制；这次重置仍使用原安装路径。环境变量在运行时仍有优先权。

`atm install --conf PATH` 把绝对路径记到 `keys.conf-path`（TOML 的 `[keys]` 下写作 `conf_path`；空串表示 `~/.tmux.conf`）。后续配置编辑、安装和卸载沿用它，显式 `--conf` 可覆盖。重置也会清掉路径记录，之后安装或卸载需再次传 `--conf PATH`。直接改路径设置只选择后续编辑的目标，不搬迁现有块。

`atm install --key s` 和 `atm config keys.pick s` 都先写入并绑定新键，成功后才解绑原安装块里的废弃键；写入或重绑失败时保留旧绑定。标记不完整或嵌套时拒绝修改文件。

开启 tmux 选项仍立即生效。关闭选项只撤掉文件中 atm 的设置，运行中的值保持不变；变更对新 tmux server 生效，届时按你自己的配置加载。总量 slice 只支持 `memory.user=true`：系统模式（`memory.user=false`）会明确拒绝安装，请自行配置系统单元。`daemon-reload` 失败会报告「文件已写入，重载失败」及具体原因，不会报成成功。

## 内存闸门：`atm claude` 和 `claude` 的区别

```bash
atm config                     # 交互式编辑器：↑↓ 选键，Enter 改/切换，s 保存，? 帮助；右侧面板按界面语言说明选中项（格式 / 默认 / 环境变量 / 来源）（atm config --show 只打印）
atm config memory.high 4G      # 软上限：节流 + 回收，不杀。默认 auto = Max 的 80%
atm config keys.pick s         # 选择器键（大写 = 只看当前目录）；还有 keys.sidebar、keys.popup-width/-height。保存即对运行中的 server 重绑
atm config tmux.mouse true     # tmux 常用选项：mouse / focus-events / history-limit / base-index / renumber-windows → 写进 ~/.tmux.conf 的独立块，立即生效。你自己的行要是也设了同一个选项，atm 会连行号一起报出来，而不是默默被盖掉
atm config memory.slice-high 20G  # 总量 slice 的数（默认 auto = 物理内存 50% / 65%）；atm 写的单元会重写 + daemon-reload
atm config memory.max 8G       # 硬上限：杀整个会话 scope（含子进程）。默认 auto = 物理内存 35%，下限 4G
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

