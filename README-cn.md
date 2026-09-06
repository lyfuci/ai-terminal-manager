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

- **轻。** 没有 Electron、没有常驻 daemon。`atm` 只在你按键的那一刻跑一下（热启动 5ms，实测见[研究记录](research/README-cn.md)），
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

> 这个仓库同时是**研究记录**：怎么用在 [docs/usage-cn.md](docs/usage-cn.md)，「为什么这么设计 / 实测踩过哪些坑」在
> [研究记录](research/README-cn.md) 和 `research/notes/`。所有被推翻的旧结论都用删除线留着，不抹掉。

---

## 装

### 要求

- Linux 或 WSL2 + **tmux ≥ 3.2**（`display-popup` 是 3.2 才有的；开发基准 3.6，3.4 实测兼容）
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

没有 uv 用 `pipx install ai-terminal-manager` 也一样。升级：**`atm update`**——它会识别 atm 是怎么装的（uv tool / pipx / pip，PyPI 还是 git）并跑对应的升级命令；`atm update --check` 只看不升。

### 体检、键位、持久化

```bash
atm doctor      # 数据源在不在、tmux 通不通、能扫到多少条会话、自动存档钩子有没有真的装上
atm install     # 往 ~/.tmux.conf 写键位 + 装 resurrect/continuum。终端里先问四个问题（键位 / 持久化 / 总量闸门），再把要写的内容打出来确认；-y 全跳过
```

`atm install` 做两件事，各自写成一个 marker 包起来的块，改前自动备份：

- **键位块**：四个绑定——`prefix + a/A` 浮层、`prefix + b/B` 侧栏（详见 [docs/usage-cn.md](docs/usage-cn.md)）。对正在跑的 tmux server 立即生效。键位可换：`atm install --key s --sidebar-key g`。
- **持久化块**：经 tpm 装 **tmux-resurrect + tmux-continuum**（克隆到 `~/.tmux/plugins/`），
  开 `@continuum-restore`、10 分钟自动存档。重启后 session / window / pane / cwd 自动搭回来。
  刻意**不**让它重新拉起 claude / codex ——开机批量拉起会瞬间吃光内存（`research/notes/2026-08-12-incident.md` 附三）；
  会话由你在对应格子里按需 resume。不想要：`--no-persist`。你自己已经在用 tpm 会自动跳过，不重复写。

界面语言跟系统 locale（中 / 英 / 日），`ATM_LANG=zh|en|ja` 可强制。

可选：`atm config memory.high 4G` 设一次，之后 `atm claude` / `atm codex` / `atm pi` 就在 cgroup 内存闸门里启动；
直接敲 `claude` 仍然不受限——前缀即选择。见 [docs/usage-cn.md](docs/usage-cn.md)。

tmux 没装的话 `atm install` 给出对应包管理器的安装命令，不替你跑 sudo。
卸载：`atm uninstall && uv tool uninstall ai-terminal-manager`——只删这两个块，你自己的配置一个字不动，克隆下来的插件也不动。

---

## 文档

- [用法](docs/usage-cn.md) —— 四个键、浮层和侧栏、命令行、它怎么工作
- [参考](docs/reference.md) —— 完整选项、实测性能、会话文件格式、内存闸门
- [研究记录](research/README-cn.md) —— 为什么这么设计、实测了什么、踩过的每个坑（被推翻的结论用删除线留着）
- [贡献](CONTRIBUTING.md)

## 贡献

欢迎 issue / PR。开发环境与约定见 [CONTRIBUTING.md](CONTRIBUTING.md)；
安全问题不要开公开 issue，见 [SECURITY.md](SECURITY.md)。许可证 [MIT](LICENSE)。
