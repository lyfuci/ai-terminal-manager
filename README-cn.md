# ai-terminal-manager (atm)

[English](README.md) | **中文** | [日本語](README-ja.md)

---

## 为什么做这个

AI CLI 已经强到可以承担大部分日常开发了。围绕它的管理工具正在爆发，但几乎全是**桌面 GUI**。
问题是相当一部分人的开发根本不在桌面上：

- 代码在服务器上，SSH 进去干活，GUI 装不了也不该装；
- 一个人同时开着三四个 Claude Code / Codex 会话，各占一个 tty，**哪个对话在哪个窗口全靠记**；
- 断线、重启、换台机器，所有会话一起消失，只剩磁盘上一堆 jsonl。

这些人缺的不是又一个 GUI，是**在 tmux 里的多会话管理**。tmux 是他们本来就开着的东西，
它已经解决了进程常驻、断线重连、跨机器接管、布局序列化——缺的只是「把 AI 会话当一等公民」这一层。
atm 就补这一层，别的都不碰。

顺带得到的两个好处：

- **轻。** 没有 Electron、没有常驻 daemon。`atm` 只在你按键的那一刻跑一下（热启动 5ms，实测见下），
  侧栏就是一个普通 tmux pane 里的 Python TUI。比桌面 GUI 省多少没量化过——这是使用感受，不是测出来的数字。
- **tmux 的会话恢复直接复用。** tmux-resurrect / continuum 重启后把窗口、分格、目录搭回来，
  你在对应的格子里按一个键把昨天的对话 resume 回去。不用自己再发明一套状态持久化。

## 它是什么、不是什么

**是**：面向 AI CLI（Claude Code / Codex / Pi）的 tmux 会话管理器。三件事：

1. 把三家 CLI 各自的历史会话**合成一份列表**，模糊搜索，选一条**投进你指定的 tmux 格子**去 `--resume`；
2. 一个可收纳的**常驻左侧栏**，列出正在跑的格子，选中就 `swap-pane` 换进主格，进程不断；
3. 顺手把 tmux-resurrect + continuum 装好配好，重启后骨架自动回来。

**不是**：不做 GUI、不做布局同步、不做控制模式解析器。这些 tmux 生态和官方 Desktop 已经吃掉了
（调研见 `research/notes/survey-existing-tools.md`）。也不把会话数据传到任何网络——它只读本地文件。

**适合**：在 Linux / WSL2 / 服务器上用 tmux 开发、同时开着多个 AI 会话的人。
**不适合**：不用 tmux 的人；只用一个 AI 会话的人；需要浮动窗口那类自由布局的人（tmux 是二叉分割树）。

> 这个仓库同时是**研究记录**：怎么用在上半部分，「为什么这么设计 / 实测踩过哪些坑」在
> [下半部分](#以下是研究记录) 和 `research/notes/`。所有被推翻的旧结论都用删除线留着，不抹掉。

---

## 装

### 要求

- Linux 或 WSL2 + **tmux ≥ 3.0**（开发基准 3.6，3.4 实测兼容）
- **Python ≥ 3.11**，零运行时依赖
- 装了 Claude Code / Codex / Pi 至少一个（atm 只读它们写在 `~/.claude/projects/`、`~/.codex/sessions/`、`~/.pi/agent/sessions/` 的会话文件）

### 安装

推荐 [uv](https://docs.astral.sh/uv/)：

```bash
# 从 PyPI 装（命令名仍是 atm）
uv tool install ai-terminal-manager

# 或不用 clone、直接从仓库装——永远是最新 main
uv tool install git+https://github.com/lyfuci/ai-terminal-manager

# 或者 clone 下来装（加 --editable 可以改了源码立刻生效）
git clone https://github.com/lyfuci/ai-terminal-manager
uv tool install ./ai-terminal-manager
```

没有 uv 用 `pipx install ai-terminal-manager` 也一样。升级：`uv tool upgrade ai-terminal-manager`（git 装法则重跑加 `--reinstall`）。

### 体检、键位、持久化

```bash
atm doctor      # 数据源在不在、tmux 通不通、能扫到多少条会话、自动存档钩子有没有真的装上
atm install     # 往 ~/.tmux.conf 写键位 + 装 resurrect/continuum。先把要写的内容打出来问过你；-y 跳过确认
```

`atm install` 做两件事，各自写成一个 marker 包起来的块，改前自动备份：

- **键位块**：四个绑定（见下）。对正在跑的 tmux server 立即生效。键位可换：`atm install --key s --sidebar-key g`。
- **持久化块**：经 tpm 装 **tmux-resurrect + tmux-continuum**（克隆到 `~/.tmux/plugins/`），
  开 `@continuum-restore`、10 分钟自动存档。重启后 session / window / pane / cwd 自动搭回来。
  刻意**不**让它重新拉起 claude / codex ——开机批量拉起会瞬间吃光内存（`research/notes/2026-08-12-incident.md` 附三）；
  会话由你在对应格子里按需 resume。不想要：`--no-persist`。你自己已经在用 tpm 会自动跳过，不重复写。

tmux 没装的话 `atm install` 给出对应包管理器的安装命令，不替你跑 sudo。
卸载：`atm uninstall && uv tool uninstall ai-terminal-manager`——只删这两个块，你自己的配置一个字不动，克隆下来的插件也不动。

---

## 用

装完就是四个键（`prefix` 默认 `Ctrl-b`）：

| 键 | 干什么 |
|---|---|
| `prefix + a` | **浮层**：模糊搜全部历史会话 → 选目标格子 → 会话在那格 `--resume` 起来 |
| `prefix + A` | 同上，但只看当前目录（含子目录）的会话 |
| `prefix + b` | **侧栏**：没开就在最左边开一条通高的；开了就切过去；已经在里面就收起 |
| `prefix + B` | 把当前格子收进后台窗口 `bg` —— 进程继续跑，之后从侧栏里还能选回来 |

**浮层里**：打字模糊搜索，`↑↓` / `^N` `^P` 移动，`Tab` 在 全部 / Claude / Codex / Pi 之间循环，`⏎` 选中，`Esc` 取消。
选中会话后进第二步：列出所有 pane（带忙闲状态）+「新分一个 pane」+「新开 window」+「只打印」。

**侧栏里**：上半段是**正在跑的格子**（选中 → `swap-pane` 换进主格，进程不断），下半段是**历史**
（选中 → 后台新窗口里 resume 再换进来）。`⏎` 换进主格，`^T` 挑具体换进哪格，`^X` 把选中的收进 `bg`，
`Tab` 切来源，`^R` 重建索引，`^C` 退出。

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
```

> 投递默认套一层 cgroup 内存闸门（`MemoryHigh=2G` / `MemoryMax=4G`）。
> 起因是实测撞上 WSL 内存上限时**整个 tmux server 连同所有会话一起死掉**过一次。
> 阈值怎么定的、怎么关，见 `docs/reference.md`「内存闸门」。

**完整选项、实测性能、三个 JSONL 的格式细节：[`docs/reference.md`](docs/reference.md)。**
开发和贡献：[CONTRIBUTING.md](CONTRIBUTING.md)。

---

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
> 决策变量（需不需要跨端 SSH 接管）仍未回答。真要做时，`src/atm/index.py` 那层可以整块复用。详见下方研究记录。

---

# 以下是研究记录

## 起因（用户原话）

> 想做个 terminal 管理程序，因为我现在经常用 claude code / codex 这些工具来开发，
> 感觉任何编辑器和 IDE 都太重了。问题是开很多个窗口不好管理，
> 所以想做个相对自由布局、并且可以记住各个命令行最终状态的工具；
> 左侧能有个可收纳的窗口，在这个窗口里能快速打开最近的对话到指定的分窗口。

## 关键概念：「记住状态」的三层

| 层 | 含义 | 谁能给 |
|---|---|---|
| **L1 视觉** | 布局分割、每格 cwd、滚动缓冲区文字 | 自己存 JSON，容易；tmux-resurrect 也给 |
| **L2 进程** | 关掉 UI 后 `claude` 进程还在跑 | **只有常驻进程宿主能给**（tmux server 或自写 daemon） |
| **L3 会话** | AI 对话上下文本身 | CLI 自带：`claude --resume` / `codex resume` |

> **L3 顶替不了 L2**：`--resume` 恢复的是对话历史，不是跑到一半的进程。
> 十分钟的重构跑一半 UI 崩了，L3 只让你不用重讲需求，救不回已经跑掉的工作。

**L2 的准确边界**（在本机实测配置下）：保得住「UI 关了/崩了」「所有登录会话退出」（因为 `KillUserProcesses=no`），
**保不住 `wsl --shutdown` / Windows 重启** —— 整个 VM 没了，只能靠 L3 降级恢复。

## 已确认

| 议题 | 结论 |
|---|---|
| **L2 是否必需** | **必需** —— 常挂长任务，UI 生命周期不能绑着进程 |
| **运行形态** | Windows 原生 GUI 连进 WSL（当时的选择） |
| **侧栏的数据源** | 现成，不用自己记录，见下节 |
| ~~**侧栏不做成 tmux pane**~~ | ~~做原生组件 —— 收纳不该扰动布局树~~ **2026-09-02 改口径**：侧栏就是一个常驻的通高左侧 tmux pane（`prefix + b` 开/切/收），列出**正在跑**的格子，选中就 `swap-pane` 换进主格。收起 = kill 那个 pane，主区自动占满，对布局树的扰动只有宽度 |

## 侧栏数据源（2026-08-12 实现时重新实测，下面是修正后的版本）

> ⚠️ 本节早前的三条描述**在实现时被实测推翻**了。原文保留在 git 历史里，这里只写现在成立的。
> 完整细节见 `docs/reference.md`「数据来源」一节。

**Codex** —— 真实数据源是 `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl`（86 个，125MB）。

- ❌ ~~`~/.codex/session_index.jsonl` 一行一条自带标题~~ —— 该文件**停更于 2026-08-03、只剩 5 条**，
  不能当会话列表用。但那几条 `thread_name` 质量最好，仍作为标题的第一优先级。
- ❌ `~/.codex/thread_history_1.sqlite` 看着像索引，实测是**单个 thread 的投影缓存**（1 thread / 3 turn），不是全局索引。
- ✅ 第 1 行 `session_meta` 带 `session_id` / `cwd` / `git`（`git` 实测可能是 `null`）。
- 恢复：`codex resume <SESSION_ID>`（实测 `codex resume --help`）

**Claude Code** —— `~/.claude/projects/<cwd 把 '/' 换成 '-'>/<sessionId>.jsonl`，本机 63 个项目目录。

- ❌ ~~从 `type:"summary"` 行提标题~~ —— 抽样 120 个文件的尾部，**一条 summary 都没有**。
- ⚠️ `type:"ai-title"` 确实存在（2.1.228 新功能），但覆盖率只有 **2%**（150 抽样中 3 个）。
- ✅ **真正的标题主力是「首条非 `isMeta` 的 user 消息」，覆盖 94%**；`cwd` / `gitBranch` 同样 94%。
- ⚠️ **1459 个 jsonl 里只有 127 个是可 resume 的会话**，其余 1332 个在
  `<sessionId>/{subagents,workflows,tool-results}/` 下，是会话的产物、没有独立 sessionId。
  扫描只能扫一层，扫成递归会多出 1332 条点了没反应的假条目。
- 恢复：`claude --resume <sessionId>`

**Pi** —— `~/.pi/agent/sessions/--<cwd 把 '/' 换成 '-'>--/<ts>_<uuid>.jsonl`（schema v3）。
适配器按上游 `session-format.md` 写成，**本机没装 pi、未经真实语料验证**；`cwd` 只在第 1 行 SessionHeader，
显示名是独立的 `session_info` 记录、可改多次（尾部再扫一遍取最后一个）。恢复：`pi --session <id>`。

**三边都要防注入包装**：Codex 实测会在真实提问前塞一条 10865 字的
`<recommended_plugins>…</environment_context>`，Claude 则是 `<local-command-caveat>` / `<command-name>` 那一套，
Pi 的 role 枚举更宽（`toolResult` / `bashExecution` / `compactionSummary`），不过滤会让 bash 输出冒充标题。
直接拿「第一条 user 消息」当标题会得到一屏垃圾。

> ~~这部分是整个想法里最容易做、又最有差异化的 —— 没人把「AI 会话历史」当一等公民。~~
> ❌ **2026-08-12 调研推翻**：clauhist、claude-sessions 已经在做历史浏览+resume，
> tmux-agent-sidebar / tmux-agent-status / opensessions 在做 agent 侧栏，官方 Desktop 侧栏更是原生的。
> 见 `research/notes/survey-existing-tools.md`。剩下的差异点只有一条：**跨 agent（Claude+Codex+Pi）统一历史 → 投到指定 pane**，
> 以及服务于服务器 / SSH 这一群没有 GUI 可用的人。

## ⚠️ 未拍板的分岔口（下次讨论从这里开始）

**决策变量只有一个：你需不需要从别的地方（纯 SSH 进来、手机、另一台机器）接管同一批会话？**

| | 路线 A：tmux 当后端 | 路线 B：自写常驻 daemon |
|---|---|---|
| L2 | 免费 | 自己实现（PTY 归 daemon，GUI 只是 attach 的渲染器） |
| 布局自由度 | 受限于**二叉分割树**，做不到浮动/重叠 | **完全自由，浮动重叠都行** |
| 协议 | 控制模式 `tmux -CC`，纯文本行协议，man page 有定义 | 自己定，一个 stdio/WebSocket 就够 |
| 最脏的活 | **两份布局真相要同步**（tmux 拥有布局，你得镜像它的树）—— 这类项目 bug 最密集处 | 没有这个问题 |
| 解析器 | 控制模式状态机 300~500 行，琐碎 | 不需要 |
| 断线重连 / 换机器 SSH 接管 | 免费 | 做不到 |
| 十几年的边界打磨（resize 竞态 / SIGWINCH / terminfo） | 免费 | 丢掉 |

> **纠正一个常见误解**：推荐 tmux **不是因为工作量小**，恰恰相反 —— 前期工作量更大。
> 推荐理由只有一条：L2 + 跨端接管是自己写买不到的。不需要跨端接管的话，自写 daemon 总复杂度更低且布局自由。

**还有路线 C（最终采用）**：先别写 app。tmux 生态可能已经把布局吃掉了 ——

| 能力 | 谁提供 | 到什么程度 |
|---|---|---|
| 布局序列化 | 原生 `#{window_layout}` / `select-layout <串>` | 只能给**已存在的 pane** 重排，不能重建 pane |
| 重启后重建 session/window/pane/cwd | tmux-resurrect | 重建结构和目录，**是重新拉起进程，不是恢复进程状态**（即 L1+cwd，非 L2） |
| 自动定期存档 + 开机自动恢复 | tmux-continuum | 补上「不用手动存」 |
| 项目模板（打开项目 X 的标准布局） | tmuxinator / tmuxp（YAML 声明） | 现成 |

~~本机**这些一个都没装**（无 `.tmux.conf`、无 tpm、无 tmuxinator）。~~
**已过期(2026-08-12 当天就装了)**：`~/.tmux.conf` 已有，resurrect + continuum 已装并实测生效
（tmuxinator 仍未装）。结论是**布局这块 tmux 生态确实够用**，路线 C 成立。
于是整个构想缩成一个几百行的 tmux 侧栏（`display-popup -E` 浮层，`prefix + a` 唤出，选完消失，**完全不占布局**），
Windows GUI / 控制模式解析器 / 布局同步全部蒸发。2026-09-05 起 `atm install` 直接把 resurrect + continuum 装好。

## 已知的坑（查证过，别重新踩）

1. **控制模式 `%output` 的转义会放大流量。** man page 原文：`value escapes non-printable characters and backslash as octal \xxx`。
   Claude Code 是重 ANSI 重刷的 TUI，ESC(`\033`) 本身就是非打印字符 —— 几乎每个转义序列都膨胀成 4 字节。
   **实际膨胀多少没测过**，走 tmux 路线的第一件事就是做吞吐压测；扛不住就退回「每个 pane 一条独立 `tmux attach` 管道」。
2. **inotify 事件不会通过 9p 传到 Windows 侧的 `\\wsl.localhost\`** —— 文件监听必须在 WSL 内做。
3. **tmux 有两个完全不同的协议层**，别搞混：
   - 客户端↔服务端 `/tmp/tmux-<UID>/default` unix socket —— **二进制、内部、未文档化、版本间会变，绝不碰**。
   - 控制模式 `-CC` —— 走客户端进程的 stdin/stdout，纯文本行协议，公开接口，iTerm2 靠它多年。
   - 正确姿势是**把 tmux 客户端当子进程拉起来**（`spawn tmux -CC attach -t <session>`），让它替你说那个二进制协议。
4. **tmux-continuum 会静默关掉自动存档 —— 只要加载时机器上还有别的 tmux server。**
   源码 `continuum.tmux:main()`：

   ```bash
   if ! another_tmux_server_running; then
       add_resurrect_save_interpolation   # 把 #(continuum_save.sh) 塞进 status-right
   fi
   ```

   自动存档**完全靠状态栏刷新驱动**（`status-interval` 秒跑一次那个 `#()`），钩子没装上就永不存档，
   而且**没有任何提示**：`@continuum-restore` 仍然显示 `on`，插件目录也在，一切看着都正常。

   2026-08-12 实测踩中：跑实验留下游离 socket，主 server 恰好在那之后重启，
   于是 **9 小时 40 分钟一次档都没存**，直到手动核对 `status-right` 才发现。

   自检（唯一可靠的判据是**看 status-right 里有没有那个 `#()`**，不是看 `@continuum-*` 选项；`atm doctor` 就查这个）：

   ```bash
   tmux show-options -gv status-right | grep -q continuum_save.sh \
     && echo "自动存档正常" || echo "❌ 钩子没装上，永不存档"
   ls -lt ~/.local/share/tmux/resurrect/ | head -3      # 最新存档时间应当在 save-interval 之内
   ```

   修复：确保只剩一个 server（`ls /tmp/tmux-$UID/`，清掉游离 socket）后
   `tmux source-file ~/.tmux.conf`。注意重新加载会把「上次存档时间」重置成当下，
   所以要立刻补一次 `~/.tmux/plugins/tmux-resurrect/scripts/save.sh`，否则空窗一个 interval。

   **推论：本项目跑任何 tmux 实验都必须用 `-L` 独立 socket 并当场清理** ——
   残留 socket 不只是脏，它会让用户的自动存档静默失效。
5. **tmux-resurrect 的空存档会杀掉刚起的 server**（2026-09-05 实测）。restore 只检查 `last` 存在、不检查非空
   （`restore.sh:check_saved_session_exists`）。0 字节存档 → 判定「从零恢复」→ `handle_session_0` 杀掉唯一的 session 0
   → server 没会话就退出。空文件来自 server 已死后 `save.sh` 仍被调用（比如 systemd 单元的 `ExecStop`）。
   开机自启 tmux 的单元要在 `ExecStartPre` 里把空的 `last` 改指最新非空存档。
6. **tmux 3.4 把 `-F` 格式里的控制字符转义成字面 `\037` 打出来，3.6 输出原字节**（2026-09-05 实测）。
   用 `\x1f` 当字段分隔符的解析在 3.4 上会得到「一行一个字段」，而「格式对不上就跳过」的防御会把**每一行**都静默吞掉。
   `tmux.py:_split_fields` 两种都认，但只认死 `\037` 这一个序列、不做通用八进制反转义（pane 标题里的 `C:\123` 不能误伤）。
7. **tmux server 的环境是它启动那一刻的快照**。`run-shell` / `display-popup` 里的 PATH 不是你当前 shell 的 PATH；
   `atm` 装在 `~/.local/bin` 而 server 比那条 PATH 更早起来，`run-shell 'atm …'` 就是 127，报错只有一行。
   所以 `atm install` 写进 tmux.conf 的永远是绝对路径。
8. **两个刚好命中场景的 tmux 特性**：
   - `refresh-client -A %<pane>:off` —— 让 tmux 对指定 pane **停止读取输出**。同时挂 6 个 Claude Code 但只有 2 个在视野里时，其余直接关推送。这是不炸 CPU 的关键开关；配合 `pause-after` 可自动暂停，恢复时发 `%continue`。
   - `refresh-client -B <name>:<what>:<format>` —— 订阅格式串，变化时推 `%subscription-changed`。pane 标题、是否有活动、当前跑什么命令全部推送式拿到，不用轮询 —— 侧栏「这个格子正在忙」的指示器靠它。

## 待确认

1. **跨端接管要不要** → 决定 A / B 路线。**仍未回答**，但不再阻塞：路线 C 已经交付可用的东西了。
2. ~~先做路线 C 验证还是直接进 A/B~~ → 已定：**路线 C**，且已实现（`src/atm/`）。
3. ~~侧栏形态~~ → **两种并存**（2026-09-02）：`display-popup -E` 浮层管「查历史、投一次」（`prefix + a`）；
   **常驻通高左侧 pane** 管「切换正在跑的进程」（`prefix + b`，`atm sidebar`）。
   后者是新维度：原来 atm 只碰 L3（磁盘上的对话），侧栏碰的是 L2（tmux 里已经在跑的 pane），
   核心手势从 `send-keys` 变成 `swap-pane`。实测 tmux 3.6 `swap-pane` 跨 window / 跨 session 都行，
   不在视野里的进程停在 `bg` 窗口继续跑。端到端在 `research/experiments/2026-09-02-sidebar-swap/`。
4. ~~若做 GUI 的技术栈~~ → 不做 GUI。`src/atm/` 定为 **Python 3.11+，零运行时依赖**。
5. ~~「打开到指定分窗口」的交互~~ → 已定：**选中会话后进第二步选择器**，
   列出所有 pane（带忙闲状态）+ 「新分一个 pane」+「新开 window」+「只打印」。
   没用 `display-panes`：那需要焦点在 tmux 上，从浮层里调用会打断手势。

**下一步该做的**（按价值排）：

1. 在装了 pi 的机器上验证 Pi 适配器（现在是照文档写的）。
2. 会话文件的 watch（inotify）→ 索引增量更新。注意坑 #2：必须在 WSL 内做。
3. 标题质量：94% 靠截首条消息，长提问截出来的标题可读性一般。
   可以考虑本地跑个小模型补标题，或等 Claude 的 `ai-title` 覆盖率自然涨上来。
4. 跨 CLI 的会话接力（在 Codex 里续一段 Claude Code 的对话、反过来也行）——还没想清楚上下文怎么带。

## 本机环境事实（实测，2026-08-12）

- `tmux 3.6`、`node v24.19.0`、`python 3.13.13`；**无** zellij / wezterm；无 `.tmux.conf` / tpm / tmuxinator / tmuxp。
- WSL2：`systemd=true`，`Linger=no`，但 `KillUserProcesses=no`（默认）→ 退出登录不会连坐杀掉 tmux server。
- `.wslconfig`：`memory=6GB`、`autoMemoryReclaim=gradual`；
  **没设 `vmIdleTimeout`**，VM 已连续跑 23h → 日常不会自己回收。
- 因为 Mirrored + hostAddressLoopback，**Windows↔WSL 的 TCP localhost 是通的**
  （早前「别用 TCP 端口、走 `wsl.exe --exec` + stdio JSON-RPC」的建议因此不再是硬约束，但 stdio 仍更省事：无端口、无防火墙弹窗、无鉴权）。

## 实测性能（`research/experiments/2026-08-12-index-bench/`）

| 指标 | 实测值 |
|---|---|
| 语料 | 213 个会话，1.73 GB（最大单文件 **680 MB**） |
| 冷启动（全量解析） | **198ms** 中位数 |
| 热启动（缓存命中） | **5ms** 中位数 |
| 冷启动实际读取 | 47 MB / 1.73 GB = **2.7%** |

关键是**只读文件头部**（标题/cwd/branch 实测都在头部）+ 按 `(mtime_ns, size)` 缓存。

## 目录

见 `CLAUDE.md`。实际项目代码放 `src/atm/`。

## 日志

- 2026-08-12 建目录；从会话 `00000000-0000-4000-8000-000000000004` 提炼需求与架构讨论，详见 `research/notes/2026-08-12-design-session.md`。
- 2026-08-12 调研现成工具，推翻「没人做」的判断，见 `research/notes/survey-existing-tools.md`。
- 2026-08-12 **路线 C 拍板并实现**：`src/atm/` 里的 `atm`（当时 68 测试通过）。
  实现过程中实测推翻了本文件关于两个 jsonl 格式的三条描述（见上「侧栏数据源」节）。
  端到端验证 `research/experiments/2026-08-12-tmux-e2e/`，压测 `research/experiments/2026-08-12-index-bench/`。
- 2026-09-02 **常驻侧栏 + swap 换位**（分支 `sidebar-swap`）：`atm sidebar` 常驻在最左边的通高 pane 里，
  上半段列正在跑的格子（Claude Code 自己会把 pane 标题设成 `✳ <任务名>`，不用反查会话 id），
  下半段接历史；选中运行中的 → `swap-pane` 换进主格，选中历史 → 新 window 里 resume 再换进来。
  `prefix + b` 开/切/收，`prefix + B` 把当前格子收进 `bg`。推翻了「侧栏不做成 tmux pane」的旧结论（见上）。
- 2026-09-05 **两个 tmux 3.4 实机 bug**（PR #3 / #4）：绑定写裸名 `atm` 导致 server 里 127；`\x1f` 分隔符被 3.4 转义成字面量导致解析全空。
  **Pi 会话源**（PR #5）：第三家 CLI，适配器照上游文档写，未经真机验证；
  顺带修了 `cli.py` 里「不是 Claude 就当 Codex」的标记 bug，来源标记收进 `model.SOURCE_TAG` 并加守卫测试。
  **`atm install` 顺手装 resurrect + continuum**（PR #6），并把定位写进本 README：服务器 / SSH 上的多会话管理。

## 贡献

欢迎 issue / PR。开发环境与约定见 [CONTRIBUTING.md](CONTRIBUTING.md)；
安全问题不要开公开 issue，见 [SECURITY.md](SECURITY.md)。许可证 [MIT](LICENSE)。
