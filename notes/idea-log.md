# idea log

## 2026-08-12 建目录
- 需求一句话：面向 AI CLI 的终端管理器 —— 多格终端 + 可收纳的「最近 AI 会话」侧栏，能把历史对话恢复到指定分窗口。
- 需求与架构讨论提炼自会话 00000000-0000-4000-8000-000000000004，详见 2026-08-12-design-session.md。
- 状态：架构分岔口未拍板（A tmux 后端 / B 自写 daemon / C 先不写 app 只做侧栏）。
- 决策变量：需不需要从别的地方（纯 SSH、另一台机器）接管同一批会话。
- 待办：调研现成工具（见 survey-existing-tools.md）。

## 2026-08-12 现成工具调研（见 survey-existing-tools.md）
- 结论：官方 Claude Code Desktop 已经覆盖侧栏 + 自由 pane 布局 + WSL + 点侧栏 resume；tmux 生态另有一批 agent 侧栏。
- 上一轮「没人把 AI 会话历史当一等公民」的判断被推翻 —— 当时没搜就断言。
- 教训（对应 ~/.claude/rules/common/investigation.md 的 Search Before Asserting）：
  「市面上没人做 X」是断言，不是观察，必须先搜再说。这条错误的差异化判断，
  差点让整个项目按「填补空白」的前提往下走。
- 剩下的候选缝：跨 agent（Claude+Codex）统一历史 → 投到指定 pane；Windows+WSL 链路第三方基本没管。

## 2026-08-12 路线 C 落地（app/ = atm）

- **名字定为 `atm`**（AI Terminal Manager），包名 / CLI 同名。
- 范围收敛到 survey 里唯一还站得住的那条缝：**跨 agent 统一历史 → 投到指定 tmux pane**。
  不做 GUI、不做控制模式解析器、不做布局同步。
- 技术栈：Python 3.11+，**零运行时依赖**（stdlib 的 json/curses 够用）。
  不依赖 fzf —— 本机根本没装，而「唤出来就能搜」是这工具的全部价值，不能压在可能不存在的二进制上。
- 侧栏形态定为 `display-popup -E` 浮层：**收纳不该扰动布局树**。

### 实现时被实测推翻的判断（又一次「别把文档当契约」）

写 README 时那三条关于 jsonl 格式的描述，实现时一查全错或半错：

| README 原话 | 实测 |
|---|---|
| Codex 用 `session_index.jsonl`，自带 `thread_name` | 该文件**停更于 8/3、只剩 5 条**；真实源是 `sessions/**/rollout-*.jsonl`（86 个） |
| Claude 标题从 `type:"summary"` 行提 | 抽样 120 个文件尾部，**一条 summary 都没有**；主力是首条非 isMeta user 消息（94%） |
| （未提及） | `ai-title` 确实有，但覆盖率只有 **2%** —— 是 2.1.228 的新功能，老会话没有 |
| （未提及） | 1459 个 jsonl 里**只有 127 个是可 resume 的会话**，其余 1332 个是 subagent/workflow 产物 |

教训延续上一条：**逆向观察出来的格式，写进文档那一刻就开始过期**。
所以解析层的设计原则是「容忍未知」而不是「匹配已知」——
标题抽取用的是「剥掉开头的 XML 包装块」这种通用规则，而不是枚举标签名，
这样 CLI 加了新包装（实测 Codex 会塞 10865 字的 `<recommended_plugins>`）也不用改代码。

### 性能

冷启动 198ms / 热启动 5ms，1.73GB 语料只读 2.7%（47MB）。
靠的是「只读头部 + (mtime_ns,size) 指纹缓存」。压测在 `experiments/2026-08-12-index-bench/`。

### 还没做

- 实机用两天验证手势（`display-popup` 里 `TMUX_PANE` 是什么 headless 验不了，代码对两种情况都成立）。
- inotify watch → 增量更新（注意坑 #2：必须在 WSL 内做）。
- 路线 A/B 仍未否掉，只是没做；`index.py` 那层在任何后端下都能复用。

## 2026-09-02 常驻侧栏：第二个维度

用户的原话：「tmux 的左边能不能弄出来一个类似选择列表之类的东西，能让我超越当前屏幕的范围，
快速替换到对应的后台 claude code 进程？」

这跟 8-12 做的东西不是一回事：`atm pick` 是 L3（历史对话 → resume），这个是 **L2**（已经在跑的 pane）。
拍板：默认 **swap**（保持布局，换进主格），侧栏**常驻**。

- 推翻了 8-12「侧栏不做成 tmux pane」的结论。当时的理由是「收纳不该扰动布局树」，
  实际做法是通高 pane + kill 收起，扰动只有宽度，可接受。
- 实测确认的机制：`swap-pane` 跨 window/session；`split-window -f -h -b -l 32` 通高左栏；
  pane 用户选项 `@atm_sidebar` 能在 `list-panes -F` 里读到；`pane_last` = 侧栏拿焦点前用户在看的格子。
- 不用反查会话 id：Claude Code 自己把 pane 标题设成 `✳ <任务名>`。
- `run-shell` 里 `$TMUX_PANE` 不可靠（指向别的 pane），键位绑定必须用 `#{pane_id}` 展开传参。
- `break-pane` 对单 pane 窗口不报错，只是把窗口改名 —— `park` 要拦。
