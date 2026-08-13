# 现成工具调研（2026-08-12）

> 结论先行：**这块已经很拥挤了，而且官方自己做了。**
> 上一轮讨论里那句「Warp / Wave / Zellij / WezTerm 都在解决终端布局，**没人把 AI 会话历史当一等公民**」
> —— **这个判断是错的**，当时没搜就下了结论。以下是实际情况。

## 一、最致命的：官方 Claude Code Desktop 已经是这个形态

来源：[官方文档 code.claude.com/docs/en/desktop](https://code.claude.com/docs/en/desktop)（已核对原文）

原始需求里的每一条，几乎都被它覆盖：

| 你的需求 | 官方 Desktop 的对应实现（文档原文要点） |
|---|---|
| 可收纳的左侧会话侧栏 | 「The sidebar lists your sessions and lets you run several in parallel」；侧栏顶部可按**状态 / 项目 / 环境**过滤、按项目分组 |
| 自由布局 | 「built around panes you can arrange in any layout: chat, diff, browser, terminal, file, plan, tasks, subagent」；**拖 pane header 换位、拖边缘调大小**，`Ctrl+\` 关闭焦点 pane（需 v1.2581.0+） |
| 从侧栏打开对话到指定分窗口 | 「hold **Ctrl** and click a session in the sidebar → 在第二个 pane 里打开」；分屏状态下点侧栏会替换**有焦点的**那个 pane |
| 恢复历史会话 | CLI↔Desktop 对照表里写得很直白：`--resume` / `--continue` ⇒ **「Click a session in the sidebar」** |
| 多开不好管 | 每个 session 自带 Git worktree 隔离；`Ctrl+Tab` 循环切换 |
| WSL | Windows 上环境可选 **WSL distribution**（`/docs/en/desktop-wsl`），另有 Local / Cloud / SSH |
| L2（进程活过 UI） | **Cloud session**「continues after you close the app」；本地 session 没这个保证 |
| 终端 | 内置 terminal pane（`Ctrl+\``），共享 session 的 cwd 和环境，**仅 local session 可用** |

官方文档甚至直接写了选型建议：
> 「use Desktop when you want to manage parallel sessions in one window, arrange panes side by side, or review changes visually」

## 二、tmux 生态：AI agent 侧栏已经有一堆

| 项目 | 是什么 | 覆盖的 agent |
|---|---|---|
| [tmux-agent-sidebar](https://hiroppy.github.io/tmux-agent-sidebar/) | 一个 tmux 侧栏，跟踪所有 window/session 里的 agent pane；显示 prompt、tool call、回复预览、后台 shell 状态、等待原因、任务进度、subagent 树 | Claude Code / Codex / OpenCode |
| [tmux-agent-status](https://github.com/samleeney/tmux-agent-status) | sidebar-first 的 agent session 管理器；每个 session 一条常驻状态栏 + status line 摘要 + 分层 fzf 跳转器。**状态来自 agent 的 hook 生命周期事件，不是进程轮询** | Claude Code / Codex |
| [opensessions](https://github.com/Ataraxy-Labs/opensessions) | tmux 侧栏（Rust，走 tpm 装）；per-thread 的 `done`/`error`/`interrupted` 未读标记、本地 HTTP API、session 重排/隐藏/新建/kill | Amp / Claude Code / Codex / OpenCode |
| [mulmoterminal](https://github.com/receptron/mulmoterminal) | 浏览器里的终端网格，tmux 后端；哪个 agent 需要你就标色；git worktree 隔离；手机推送 | Claude Code / Codex |
| [unitmux](https://dev.to/ugo/unitmux-a-floating-desktop-app-for-claude-code-and-codex-in-tmux-54gn) | 常驻置顶的浮动桌面小窗，自动识别 tmux pane，直接往里发指令 | Claude Code / Codex |
| Claude Squad | 把多个 AI terminal agent 当 tmux session 管理 | Claude Code / Codex / OpenCode / Amp |

> 注意这批工具的共同点：**它们看的是「活着的」agent 状态**（谁在忙、谁报错、谁在等你），
> 而不是「翻历史对话再恢复」。opensessions 明确只做 live state，不做历史 resume。这是个可以撬的缝，见第四节。

## 三、历史会话浏览/恢复：也有现成的

| 项目 | 做法 |
|---|---|
| [clauhist](https://dev.to/lef237/clauhist-browse-full-claude-code-history-and-resume-sessions-across-projects-1c1o) | 读 `~/.claude/history.jsonl`，按 session 分组喂给 fzf，选中 → 在项目目录里 `claude --resume` |
| [claude-sessions](https://github.com/greeun/claude-sessions) | 单文件 Python CLI，索引 `~/.claude/projects/` 下**所有**转录；fzf 式选择器，带实时关键词过滤、多选、原地 resume |
| 官方 CLI | `claude --continue`（当前项目最近一条）、`claude --resume`（交互式选择器） |
| 官方 Cookbook | 甚至有一篇 [Building a session browser](https://platform.claude.com/cookbook/claude-agent-sdk-05-building-a-session-browser) 教你自己写 |

上一轮讨论里「Claude 侧没有现成标题、要从 jsonl 里提」这个技术判断是对的，
但「得自己做」这个结论不成立 —— 已经有人做完了。

社区里也有对应的 issue：[「/resume should search sessions globally across all project directories」](https://github.com/anthropics/claude-code/issues/59941) —— 说明官方也在被推着补这块。

## 四、那还剩什么缝？（下次讨论的实际议题）

按证据强弱排：

1. **跨 agent 的统一历史检索 + 恢复到指定 pane。**
   官方 Desktop 只管 Claude；tmux 那批侧栏只看 live 状态；clauhist / claude-sessions 只管 Claude 的历史、且是「原地 resume」不是「投到指定 pane」。
   **「Claude + Codex 的历史统一列表 → 选中 → 投到我指定的那个格子」目前没看到有人做全。** 这是原始想法里唯一还站得住的差异点。
2. **官方 Desktop 的侧栏只管它自己跑的 session。** 文档原文：Claude「doesn't see cloud sessions, or sessions you started from the terminal CLI or the VS Code extension」——
   这句是描述模型可见的跨 session 面，**侧栏列表是否也排除 CLI 起的 session，文档没直说，需要实测**。
   如果确实排除，那「我平时在终端里跑的会话，在 Desktop 侧栏里根本看不到」就是个真实的洞。
3. **Desktop 的 pane 是 Claude 自己的功能面板**（chat / diff / browser / terminal / file / plan / tasks / subagent），
   **不是任意 shell 的自由网格**。你原始诉求里「一堆命令行格子」它并不满足 —— 内置终端只有一个 pane，且仅 local session 可用。
4. Windows + WSL 这条链路，第三方 tmux 工具基本没人认真管（都默认 macOS/Linux）。

## 五、对项目的影响

- **「做个 terminal 管理程序」这个大目标基本不用做了** —— 官方 Desktop + tmux 生态已经吃掉 90%。
- 上一轮结论里的**路线 C（先别写 app）应该直接升级为默认路线**，而且验证清单要改：
  不只是装 tpm + resurrect + continuum，而是**先把官方 Desktop 和上面 2~3 个 tmux 侧栏实际用两天**，
  用完再看第四节那几条缝是不是真的疼。
- 如果最后只剩第 1 条（跨 agent 历史 → 投到指定 pane），那就是个**几百行的脚本**，不是一个产品。

---

**方法论备注**：上一轮在没搜索的情况下断言「没人做」，这正是 `~/.claude/rules/common/investigation.md`
里「Search Before Asserting」要防的错误。这条记进 `idea-log.md`。
