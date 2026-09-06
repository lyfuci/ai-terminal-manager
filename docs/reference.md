# atm — AI Terminal Manager

把 **Claude Code、Codex 和 Pi 的历史会话**合成一份统一列表，选一条，**投递到你指定的 tmux pane**。

```
prefix + a  →  浮层里搜  →  Enter  →  选目标格子  →  会话在那个格子里恢复
```

## 为什么只做这一件事

`../notes/survey-existing-tools.md` 调研的结论：布局这块**官方 Claude Code Desktop
和 tmux 生态已经吃掉了**，历史浏览也有 clauhist / claude-sessions 在做。
只剩一条没人做全：

> **跨 agent（Claude + Codex + Pi）的统一历史 → 投到我指定的那个 pane。**

atm 就只做这一条。没有 GUI，没有控制模式解析器，没有布局同步 —— 那些都被砍掉了。

## 装

零运行时依赖，只要 Python ≥ 3.11。

```bash
uv sync                      # 开发用（装 pytest / ruff）
uv run atm doctor            # 体检：数据源在不在、tmux 通不通
```

### 日常使用装法（推荐）

```bash
uv tool install --editable .     # atm 进 ~/.local/bin，全局可用
atm doctor
```

`--editable` 表示改了源码立刻生效，不用重装 —— 研究阶段一直在改，这个更省事。
（不想要 editable 就去掉这个 flag。卸载：`uv tool uninstall atm`。）

### 打包成单文件（可选）

零依赖的好处：**stdlib 的 `zipapp` 就够了**，不需要 PyInstaller。

```bash
python3 -m zipapp src -m atm.cli:main -o atm.pyz -p "/usr/bin/env python3"
./atm.pyz list -n 5
```

产出一个 196KB 的 `atm.pyz`，拷到任何有 Python ≥3.11 的机器上就能跑。

> 注意 `.pyz` **不是自带解释器的独立二进制**。真要「目标机器连 Python 都没有」，
> 那才需要 PyInstaller / Nuitka（产物 10MB+，且要在目标平台上构建）。
> 本项目跑在 WSL 里、系统自带 python3.13，所以没必要。

### 启动延迟实测（`atm list -n 1`，热缓存，5 次平均）

浮层要「瞬开」，所以这个数字比打包方式本身更重要：

| 方式 | 延迟 |
|---|---|
| `.venv/bin/atm` 直调 / `uv tool install` 后的 `atm` | **55ms** |
| `atm.pyz`（zipapp） | 76ms |
| `uv run atm` | 92ms |

`uv run` 每次要解析项目环境，多花 ~37ms。`atm.tmux` 会**优先用 PATH 里的 `atm`**，
装过之后浮层自动走最快那条路。

tmux 绑定 —— 一条命令搞定，**不用自己去编辑 `~/.tmux.conf`**：

```bash
atm install            # 终端里：四问向导（键位 / 侧栏键 / 持久化 / 总量 slice，回车 = 默认）→ 打出要写的内容 → 确认才动手
atm install --print    # 只想看会写什么，什么都不改
atm install -y         # 不问直接装
atm install --no-persist   # 只装键位，不装 tmux-resurrect / tmux-continuum
atm uninstall          # 干净移除（键位块 + 持久化块；~/.tmux/plugins 里的插件不动）
```

它做的事：

- 往 `~/.tmux.conf` 里写一个 **marker 包起来的块**（幂等：装几次都只有一块；
  换键位重装会替换掉旧的，不会并存）
- 改之前**自动备份**成 `~/.tmux.conf.bak-atm-<时间戳>`
- **对正在运行的 tmux server 立即生效** —— 不用 reload，装完直接按 `prefix + a`
- `atm uninstall` 只删 marker 之间的内容，**你自己写的配置一个字都不动**

写进去的就是两行普通 tmux 配置，你随时能自己改：

```tmux
# >>> atm (ai-terminal-manager) >>>
bind-key a display-popup -E -w 80% -h 70% 'atm pick'
bind-key A display-popup -E -w 80% -h 70% 'atm pick --here'
# <<< atm <<<
```

键位和尺寸可调：`atm install --key s --width 90% --height 80%`（带了任何选项就不进向导）。

> 刻意写成 `bind-key` 行而不是 `run-shell /abs/path/atm.tmux`：后者把用户的 tmux 配置
> **钉死在源码目录的绝对路径上**，`src/atm/` 一搬走就悄悄失效。
> `atm.tmux` 仍保留给 tpm 用户（`set -g @plugin '<app 目录>'`）。

立即生效那一步用的是直接下 `bind-key` 命令，**不是 `source-file ~/.tmux.conf`** ——
后者会把整份配置重新执行一遍，用户配置里若有带副作用的行会被重复触发。

> 浮层管「查历史、投一次」。**常驻左侧 pane** 是 2026-09-02 加的第二个维度（下一节），
> 两者并存：`prefix + a` 浮层，`prefix + b` 侧栏。

## 常驻侧栏：切换正在跑的进程（2026-09-02）

`atm pick` 管的是**历史**（磁盘上的对话 → `--resume`）。侧栏管的是**正在跑的格子**：
它们已经在 tmux 里了，只是不在当前窗口。所以核心手势不是 `send-keys` 而是 **`swap-pane`** ——
进程不断，只是换个位置。

```
┌────────┬──────────────────────────┐
│ › _    │                          │
│ ── 运行中 3                       │
│ ✳ atm        ←   （主格）          │      主格 = 侧栏拿到焦点前你在看的那格
│ ✳ sample-project     bg   （在后台窗口）    │      选它 → swap-pane 换进主格，主格去 bg
│ uv  control-c…                    │
│ ── 历史 213                       │
│ 2h CC 帮我把索引层的缓存加上       │      选它 → 新 window 里 resume，再换进主格
└────────┴──────────────────────────┘
```

- `prefix + b`：**一个键三种含义** —— 没开就在当前 window 最左边开一个通高 32 列的侧栏；
  开了就切过去；焦点已经在侧栏上就收起（kill 那个 pane，主区自动占满）。
- 换位时**被换出去的旧格子怎么办**：里面跑着东西（claude / vim…）→ 收进后台，侧栏里还能选回来；
  只是个**空闲 shell** → 直接关掉，不留在后台（否则每换一次 `bg` 里就多一个空 bash，实机踩到）。
  命令行 `atm swap --keep` 强制留、`--discard` 强制关（后者会连跑着的进程一起杀，侧栏里不提供）。
- `prefix + B`：把当前格子收进后台窗口 `bg`（进程继续跑，侧栏里还能选回来）。
- 侧栏里：打字模糊搜索（同时过滤运行中和历史），`⏎` 换进主格，**`^T` 选格**（列出当前窗口的格子，
  编号 = `prefix + q` 屏幕上标的数字，按数字或回车指定换进哪格；历史会话同样适用），
  `^X` 把选中的格子收进 `bg`，`Tab` 切来源，`^R` 重建索引，`^C` 退出。
- 列表每秒刷一次 `list-panes`，标题直接用 pane 标题 —— Claude Code 自己会把它设成 `✳ <任务名>`，
  所以**不需要反查会话 id**。

不进 TUI 的等价命令（脚本 / 键位里用）：

```bash
atm sidebar --toggle --pane '#{pane_id}'   # 键位绑定里就是这一条；run-shell 里 $TMUX_PANE 不可靠，必须传
atm swap %7                                # 在哪格里跑就把 %7 换进哪格；从侧栏里跑则换进主格
atm swap %7 --into %3                      # 指定换进 %3
atm park [%7]                              # 收进 bg（默认当前 pane）
atm prune [-n]                             # 关掉 bg 里空闲的 shell 格子（-n 只看不关）
```

几条实测过的边界（tmux 3.6，`research/experiments/2026-09-02-sidebar-swap/`，22 项全过）：
`swap-pane` 跨 window / 跨 session 都行；`split-window -f` 才是通高（不带 -f 只切当前 pane 的高度）；
单 pane 窗口 `break-pane` 不报错、只是把窗口改名成 `bg`，所以 `park` 对独占窗口的 pane 直接拒绝。
`new-window` 默认把你的视图切到新窗口 —— 侧栏从历史恢复时必须 `-d` 后台开再 swap，
否则你会被带到新窗口里、看到的是被换出去的旧主格，像「弹出了一个全新会话」（实机踩过，已修）。

## 用

```bash
atm pick                     # 主命令：交互选会话 → 选目标 pane → 投递
atm pick --here              # 只看当前目录（含子目录）的会话
atm pick --split             # 跳过选目标，直接分一个新 pane 投进去
atm pick --print             # 只打印命令，不投递

atm install                  # 装 tmux 键位绑定（幂等、自动备份、立即生效）
atm sidebar --toggle         # 开/切/收常驻侧栏（键位 prefix + b）
atm swap %7 [--into %3]      # 把 %7 换进当前格子（或指定的格子）
atm park                     # 把当前格子收进后台窗口 bg
atm uninstall                # 干净移除

atm list -n 20               # 列最近 20 条
atm list --source codex      # 只看 Codex（还有 claude / pi）
atm list --json              # 结构化输出，喂给别的脚本

atm resume <id前缀>          # 不进 TUI，按 id 直接投
atm panes                    # 列出所有 tmux pane 及其忙闲状态
atm index --rebuild          # 清缓存全量重建
atm doctor                   # 体检
```

选择器里：`↑↓` / `Ctrl-N` `Ctrl-P` 移动，直接打字模糊搜索，
`Tab` 在 全部 / Claude / Codex / Pi 之间循环，`Enter` 选中，`Esc` 取消。
`F1` / `?`（搜索框为空时）弹出完整键位表 —— 选会话、选目标、侧栏、`atm config` 四个界面都有，任意键关闭。

**不在 tmux 里也能用** —— 这时 `pick` 自动退化成打印命令：

```bash
eval "$(atm pick --print)"
```


## 升级

```bash
atm update            # 识别安装方式后跑对应命令：uv tool upgrade / pipx upgrade / pip install -U
atm update --check    # 只比对版本，不动；--json 机器可读
atm --version
```

识别依据是 `sys.executable` 的路径（`…/uv/tools/…`、`…/pipx/venvs/…`）和 PEP 610 的 `direct_url.json`
（git 装的跟 main 最新 commit，PyPI 装的跟最新发行版；editable 装的只提示 `git pull`，不代劳）。
**不要**对 uv tool 装的 atm 用 `pip install -U`：那会装进另一个解释器，PATH 上的 `atm` 还是旧的；
而且 PyPI 上的 `atm` 是别人的包，本项目叫 `ai-terminal-manager`。

## 界面语言

CLI 的输出、`--help`、TUI 提示、报错都有中 / 英 / 日三语。选择顺序：`ATM_LANG`（`zh` / `en` / `ja`）> `LC_ALL` > `LC_MESSAGES` > `LANG` > `locale.getlocale()`；`C` / `POSIX` 或认不出的语言 → 英文。
`--json` 的字段名与语言无关；`-v` 日志不翻译。源码里的消息是中文原文，翻译表在 `src/atm/i18n_catalog.py`，测试保证每条消息都有两种翻译且占位符一致——漏翻的会原样显示中文，不会崩。

## 日志与诊断

```bash
atm -v list        # INFO：概要（比如「机器拿不到 cgroup，本次不套闸门」）
atm -vv list       # DEBUG：逐文件解析失败原因、每条 tmux 命令
ATM_DEBUG=1 atm …  # 等于 -vv，且预期错误也给完整堆栈
atm doctor --json  # 结构化体检；只有配置文件坏了才 exit 1
```

默认输出保持安静：一条脏会话文件不会刷屏。日志全走 stderr，不污染 `--json` 的 stdout。
日志里没有会话正文、标题或完整命令行；atm 不上传任何东西。

## 补全

```bash
eval "$(atm completion bash)"      # ~/.bashrc
eval "$(atm completion zsh)"       # ~/.zshrc（需要 compinit）
atm completion fish > ~/.config/fish/completions/atm.fish
```

补全脚本从 argparse 的定义生成（子命令、每个子命令的选项、`memory.*` 配置键、`--source` 的取值），不会和 `--help` 漂移。

## 内存闸门（默认开）

两层，都是 cgroup：

| 层 | 管什么 | 谁设 |
|---|---|---|
| **单会话** scope | 一个会话（含它拉起的 MCP server / bash 工具等子进程）最多用多少 | `atm config memory.high / memory.max` |
| **总量** slice | 所有 atm 启动或投递的会话合计最多用多少 | `atm install` 写 `~/.config/systemd/user/atm-ai.slice`，按物理内存 50% / 65% |

### 三条进闸门的路，一条不进

```bash
atm claude --resume <id>     # 带闸门启动 claude；参数原样透传，包括 --help
atm codex resume <id>        # 同理
atm pi --session <id>
claude                       # 不带 atm 前缀 = 原生，不套任何限制
```

`prefix + a` 投递和侧栏恢复历史走的是同一套参数。**前缀即选择**：atm 不劫持 `claude` 命令本身，
你随时可以裸跑。

### 配置

```bash
atm config                        # 终端里：交互式编辑器（↑↓ 选、Enter 改/切换、r 恢复默认、s 保存、? 帮助）；管道里 / --show：打印当前值和来源
atm config memory.high 4G         # 软上限：超了节流 + 回收，不杀
atm config memory.max 8G          # 硬上限：回收压不住才杀 —— 杀的是整个 scope，会话和它的子命令一起没
atm config memory.enabled false   # 全关（atm claude 就等于 claude）
atm config --unset memory.high    # 恢复单项默认
atm config --reset                # 全部默认
```

配置文件 `~/.config/atm/config.toml`（尊重 `$XDG_CONFIG_HOME`），改完立即生效。
**优先级：命令行参数 > 环境变量 > 文件 > 默认**；每个键对应一个环境变量（`memory.high` → `ATM_MEMORY_HIGH`，`memory.swap-max` → `ATM_MEMORY_SWAP_MAX`）。`atm config` 会标出每项的来源，`--json` 给机器读。
未知的键 / 段、`[memory]` 不是表、值格式不对，都是错误而不是静默忽略。
其它可设项：`memory.swap-max`（默认 512M，WSL 的 swap 在宿主 SSD 上，别放大）、
`memory.slice`（默认 `atm-ai.slice`）、`memory.user`（systemd-run 是否走 `--user`，一般 true）。

单次覆盖：`atm pick --mem-high 3G --mem-max 6G`、`atm pick --no-mem-limit`。

### 默认值怎么来的

单会话默认 **2G / 4G**，来自 8G 机器上 25 个真实会话的 `memory.peak` 分布：

| 上限 | 会杀掉 |
|---|---|
| 1G | 3/25（12%）|
| **2G** | **1/25（4%）** |
| 4G | 1/25（4%）|

2G 是拐点——再往上加没有改善，只有那个 4813MB 的失控户超标。**大内存机器请自己往上调**
（48G 的机器 4G / 8G 比较合理）：默认值偏保守是因为它是为 8G 的 WSL 定的，不是为你的机器定的。

**为什么 High / Max 分开**：实测峰值大多是瞬时尖峰，同一批会话 peak→current 回落 60~75%。
`MemoryHigh` 只节流 + 强制回收（**不杀**），让尖峰自然压回去；`MemoryMax` 是硬底线。只设 Max 会误杀正常尖峰。

**内存增长跟「开多久」无关，跟「干多少活」有关**——一个挂了 22h35m 的会话只用 346MB，
一个 CPU 时间 49 分钟的会话到了 4.7GB。

### 为什么需要

一个持续干活的 claude 会话内存峰值到过 **4.7GB**，四个长会话合计约 8GB——正好是当时 WSL 的上限。
撞上限的后果不是「那个会话变慢」，而是**整个 tmux server 连同所有会话一起死掉**（2026-08-12 11:59 实际发生过）。
套上 cgroup 后，最坏情况从「全丢」变成「丢一个」。

拿不到 cgroup 支持（无 systemd user manager / memory 控制器未 delegate）时**静默降级为不限**，
`atm config` 和 `atm doctor` 会把这件事说出来。

## 实测数字（不是估的）

压测脚本和原始输出在 `../experiments/2026-08-12-index-bench/`：

| 指标 | 实测值 |
|---|---|
| 语料规模 | 213 个会话，1.73 GB（最大单文件 **680 MB**） |
| 冷启动（全量解析） | **198ms**（中位数，5 轮） |
| 热启动（缓存命中） | **5ms**（中位数，5 轮） |
| 冷启动实际读取 | 47 MB / 1.73 GB = **2.7%** |
| 缓存文件 | 137 KB |

能做到只读 2.7% 是因为**标题、cwd、gitBranch 实测都在文件头部**
（Claude 的 `ai-title` 首次出现行号中位数 11），所以每个文件只读头部 256KB / 512KB，
再按 `(mtime_ns, size)` 缓存解析结果。680MB 的文件也只读它的头 256KB。

## 数据来源（逆向观察，不是公开契约）

> ⚠️ 这两个格式都是**观察出来的**，CLI 升级随时可能变。
> 解析层对未知 type / 缺字段 / 脏行全部容忍 —— 单条坏数据不会让侧栏挂掉。

**Claude Code** `~/.claude/projects/<cwd 的 / 换成 ->/<sessionId>.jsonl`

- 标题优先级：`type:"ai-title"` → 首条非 `isMeta` 的 user 消息 → `(untitled)`
- 实测覆盖率：`ai-title` 只有 **2%**（2.1.228 的新功能），首条 user 消息 **94%**
- **没有 `type:"summary"` 记录**（抽样 120 个文件的尾部，一条都没有）
- 只扫 `<project>/<sessionId>.jsonl` **一层**。本机 1459 个 jsonl 里只有 127 个是可 resume 的会话，
  其余 1332 个在 `<sessionId>/{subagents,workflows,tool-results}/` 下，是产物不是会话
- 恢复：`claude --resume <sessionId>`

**Codex** `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl`

- 第 1 行 `type:"session_meta"` 带 `session_id` / `cwd` / `git`（`git` 可能是 `null`）
- 标题优先级：老 `session_index.jsonl` 的 `thread_name` → `event_msg`/`user_message`
  → `response_item` role=user → `(untitled)`
- **`~/.codex/session_index.jsonl` 已经不能当会话列表用**（停更于 2026-08-03，只剩 5 条），
  但它那几条 `thread_name` 是人类可读标题，留作标题来源
- `~/.codex/thread_history_1.sqlite` 是**单线程的投影缓存**，不是全局索引，没用它
- 恢复：`codex resume <SESSION_ID>`

**Pi** `~/.pi/agent/sessions/--<cwd 把 '/' 换成 '-'>--/<ts>_<uuid>.jsonl`

> ⚠️ **这一个是按上游文档写的，不是实测**。本机没装 pi，依据是 earendil-works/pi 的
> `packages/coding-agent/docs/session-format.md`（schema `version: 3`）。
> 字段对不上的话 `tests/test_sources.py` 里的 pi 用例会第一个红。

- 第 1 行 `type:"session"` 是 SessionHeader，**只有它带 `cwd`**
- 显示名走独立记录 `type:"session_info"` 的 `name`（`/name` 或 `--name` 产生），可改多次，取最后一条
- 标题取首条 `message` 且 `message.role == "user"` —— pi 的 role 枚举比另外两家宽
  （还有 `toolResult` / `bashExecution` / `compactionSummary` / `branchSummary`），不过滤会让 bash 输出冒充标题
- schema v3 里**没有 git 字段**，所以 pi 的条目不显示分支
- 恢复：`pi --session <id>`

**所有会话文件一律只读。** 不原地改、不整理、不删。

## 安全

投递到 pane 的命令会经过两道处理：

1. **`shlex.quote` 转义** —— cwd 或 id 里有空格 / 引号 / 分号也不会被 shell 拆成两条命令
   （`../experiments/2026-08-12-tmux-e2e/` 里有真实 pane 上的注入验证）
2. **忙闲检测** —— 目标 pane 里跑的不是空闲 shell（比如正跑着另一个 `claude`）时拒绝投递，
   否则命令会变成那个程序的输入。要强投用 `--force`

`send-keys` 用 `-l --`（literal），避免命令里的 `Enter` / `C-c` 之类被当**键名**解释。

## 结构

```
src/atm/
├── model.py        # SessionEntry / FileRef / IndexStats —— 全部 frozen，跨边界不传裸 dict
├── jsonl.py        # 防御式头部读取（字节上限 + 单行上限 + 脏行跳过）
├── text.py         # 标题清洗、注入包装剥离、CJK 显示宽度
├── sources/
│   ├── claude.py   # ~/.claude/projects 适配器
│   └── codex.py    # ~/.codex/sessions 适配器
├── cache.py        # (mtime_ns, size) 指纹缓存，原子写
├── index.py        # 统一索引 —— 不 import tmux，可整块搬走
├── fuzzy.py        # 子序列模糊匹配 + 打分
├── tui.py          # curses 选择器（本机没装 fzf，不能把它当硬依赖）
├── tmux.py         # 只用公开 CLI，绝不碰内部 unix socket
├── dispatch.py     # 组装 resume 命令 + 投递
├── sidebar.py      # 运行态：主格 / swap / park / toggle 的纯规划函数（2026-09-02）
├── sidebar_tui.py  # 常驻侧栏的 curses 循环，每秒刷 pane 列表
└── cli.py          # argparse 入口
```

`index.py` 这一层**在任何后端路线下都能复用**（tmux / 自写 daemon / GUI），
所以它刻意不依赖 `tmux.py`。

## 开发

```bash
uv run pytest                # 240+ 个测试
uv run ruff check . && uv run ruff format --check .
```

一次性的验证脚本放 `../experiments/<yyyy-mm-dd-tag>/`，不进这里。
