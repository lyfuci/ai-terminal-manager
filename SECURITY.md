# 安全政策

## 报告漏洞

**不要为安全问题开公开 issue。** 用 GitHub 的
[Private vulnerability reporting](https://github.com/lyfuci/ai-terminal-manager/security/advisories/new)，
或在 issue 里只描述“有一类安全问题，希望私下沟通”。

## 已知的安全边界

- atm **只读**本机的 `~/.claude/projects/` 和 `~/.codex/`，从不写入、不联网。
- 会话标题可能包含你对话里的文字，列表和 TUI 会原样显示 —— 注意别在共享屏幕时露出敏感标题。
- 投递走 `tmux send-keys` + `shlex.quote`，但**忙闲检查挡不住 readline 缓冲区里已打了一半的
  命令**（会先发 `C-e C-u` 清行，但这不是原子操作）。
- 内存闸门依赖 systemd user slice；不可用时静默降级为不限内存。

## 报告时请不要附上

- 真实的会话转录或 JSONL 片段（标题和路径就可能含敏感信息）。
- 会话 UUID、公司 / 内部项目名。