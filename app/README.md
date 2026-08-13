# atm — AI Terminal Manager

把 **Claude Code 和 Codex 的历史会话**合成一份统一列表，选一条，**投递到你指定的 tmux pane**。

```
prefix + a  →  浮层里搜  →  Enter  →  选目标格子  →  会话在那个格子里恢复
```

## 为什么只做这一件事

`../notes/survey-existing-tools.md` 调研的结论：布局这块**官方 Claude Code Desktop
和 tmux 生态已经吃掉了**，历史浏览也有 clauhist / claude-sessions 在做。
只剩一条没人做全：

> **跨 agent（Claude + Codex）的统一历史 → 投到我指定的那个 pane。**

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
atm install            # 会先把要写的内容打出来，问过你才动手
atm install --print    # 只想看会写什么，什么都不改
atm install -y         # 不问直接装
atm uninstall          # 干净移除
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

键位和尺寸可调：`atm install --key s --width 90% --height 80%`。

> 刻意写成 `bind-key` 行而不是 `run-shell /abs/path/atm.tmux`：后者把用户的 tmux 配置
> **钉死在源码目录的绝对路径上**，`app/` 一搬走就悄悄失效。
> `atm.tmux` 仍保留给 tpm 用户（`set -g @plugin '<app 目录>'`）。

立即生效那一步用的是直接下 `bind-key` 命令，**不是 `source-file ~/.tmux.conf`** ——
后者会把整份配置重新执行一遍，用户配置里若有带副作用的行会被重复触发。

> 用 `display-popup` 浮层而不是常驻左侧 pane 是刻意的：**收纳不该扰动布局树**。
> 浮层关掉后布局一格没动，常驻 pane 则会一直占着宽度。

## 用

```bash
atm pick                     # 主命令：交互选会话 → 选目标 pane → 投递
atm pick --here              # 只看当前目录（含子目录）的会话
atm pick --split             # 跳过选目标，直接分一个新 pane 投进去
atm pick --print             # 只打印命令，不投递

atm install                  # 装 tmux 键位绑定（幂等、自动备份、立即生效）
atm uninstall                # 干净移除

atm list -n 20               # 列最近 20 条
atm list --source codex      # 只看 Codex
atm list --json              # 结构化输出，喂给别的脚本

atm resume <id前缀>          # 不进 TUI，按 id 直接投
atm panes                    # 列出所有 tmux pane 及其忙闲状态
atm index --rebuild          # 清缓存全量重建
atm doctor                   # 体检
```

选择器里：`↑↓` / `Ctrl-N` `Ctrl-P` 移动，直接打字模糊搜索，
`Tab` 在 全部 / Claude / Codex 之间切，`Enter` 选中，`Esc` 取消。

**不在 tmux 里也能用** —— 这时 `pick` 自动退化成打印命令：

```bash
eval "$(atm pick --print)"
```


## 内存闸门（默认开）

每次投递会把会话裹进一个带 cgroup 限制的 systemd scope：

```
cd <cwd> && systemd-run --user --scope -q --description 'atm: <名字>' \
    -p MemoryHigh=2G -p MemoryMax=4G -p MemorySwapMax=512M \
    claude --resume <id>
```

**为什么需要**：实测一个持续干活 1h40m 的 claude 会话内存峰值到过 **4.7GB**，
四个长会话合计约 8GB —— 正好是本机 WSL 的上限。撞上限的后果不是「那个会话变慢」，
而是**整个 tmux server 连同所有会话一起死掉**（2026-08-12 11:59 实际发生过一次）。
套上 cgroup 后，最坏情况从「全丢」变成「丢一个」。

**阈值怎么来的**（journal 里 25 个真实会话的 `memory.peak` 分布）：

| 上限 | 会杀掉 |
|---|---|
| 1G | 3/25（12%）|
| **2G** | **1/25（4%）** |
| 4G | 1/25（4%）|

2G 是拐点 —— 再往上加没有任何改善，只有那个 4813MB 的失控户超标。

**为什么 High/Max 分开**：实测峰值大多是**瞬时尖峰**，同一批会话 peak→current 回落达 60~75%。
所以 `MemoryHigh` 只节流 + 强制回收（**不杀**），让尖峰自然被压回去；
`MemoryMax` 是硬底线，只在回收也压不住时才动手。只设 Max 会误杀正常尖峰。

**内存增长跟「开多久」无关，跟「干多少活」有关** —— 实测一个挂了 22h35m 的会话只用 346MB，
而一个 CPU 时间 49 分钟的会话到了 4.7GB。tmux 不参与任何内存管理；claude 也确实会回收
（那 60~75% 的回落就是证据）。

调整：

```bash
atm pick --mem-high 3G --mem-max 6G   # 放宽
atm pick --no-mem-limit               # 关掉
```

拿不到 cgroup 支持（无 systemd user manager / memory 控制器未 delegate）时**静默降级为不限**。

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
└── cli.py          # argparse 入口
```

`index.py` 这一层**在任何后端路线下都能复用**（tmux / 自写 daemon / GUI），
所以它刻意不依赖 `tmux.py`。

## 开发

```bash
uv run pytest                # 68 个测试
uv run ruff check . && uv run ruff format --check .
```

一次性的验证脚本放 `../experiments/<yyyy-mm-dd-tag>/`，不进这里。
