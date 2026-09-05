# ai-terminal-manager

本目录是 `research/` 工作区下的一个单次研究项目。工作区整体约定见 `../CLAUDE.md`，全局规则见 `~/.claude/CLAUDE.md` + `~/.claude/rules/common/`。

## 主题

面向 AI CLI（Claude Code / Codex）的终端管理器：多格终端 + 可收纳的左侧「最近 AI 会话」侧栏，
能把一条历史对话恢复到指定的分窗口。核心价值不在布局，在**把 AI 会话历史当一等公民**。

## 目标

_(TODO: 分岔口拍板后填。当前候选的第一步：装 tpm + tmux-resurrect + tmux-continuum 用两天，
判断布局这块 tmux 生态是否已经够用；同时把侧栏最小实现跑起来验证核心手势。)_

> ⚠️ **架构分岔口未拍板**（tmux 后端 / 自写 daemon / 先不写 app）。
> 动手前先读 `README-cn.md` 的「未拍板的分岔口」和「已知的坑」两节（`README.md` 是英文版，内容同步；另有 `README-ja.md`）——
> 那里的结论是查证过的，别重新推导、也别推翻不看。改 README 时三个语言版本要一起改。

## 本目录结构

产品代码在**仓库根目录**（标准 src 布局，`uv tool install git+…` 不带 `#subdirectory` 就能装，也发 PyPI：`ai-terminal-manager`）；
研究材料整体收在 `research/`，可以整块搬走。

```
ai-terminal-manager/
├── CLAUDE.md            # 本文件 — Claude Code 的研究上下文
├── README.md            # 英文主 README；README-cn.md / README-ja.md 是中文 / 日文版，内容同步
│                        # 结构：为什么做 → 装 → 用 → 怎么工作 → 研究记录（需求、已确认结论、分岔口、坑、环境事实）
├── pyproject.toml       # ★ 包 ai-terminal-manager，命令 atm；零运行时依赖；uv 管理
├── src/atm/             # ★ 产品代码
├── tests/               # ★ pytest（只用 tmp_path，绝不碰真实会话数据）
├── atm.tmux             # tpm 兼容的插件入口（给不想 uv install 的人）
├── docs/reference.md    # 完整选项、实测性能、三个 JSONL 的格式细节、内存闸门
├── .github/workflows/   # ci（ruff + pytest）、publish（打 v* tag 发 PyPI，trusted publishing）
└── research/            # 研究过程，不进包
    ├── notes/           # 讨论纪要、阅读笔记、决策记录、事故复盘
    ├── experiments/     # 验证性实验：吞吐压测、原型脚本、实测数据
    └── writeup/         # 报告 / 设计文档草稿
```

## 产品代码 vs 研究材料

**研究笔记与产品代码分开**：`research/` 是研究过程，根目录的 `src/` `tests/` 是真要长期维护的东西。

- 技术栈已定：**Python 3.11+，零运行时依赖**，`uv` 管理，hatchling 构建。sdist / wheel 只打包 `src/`、`tests/`、`atm.tmux` 和几个顶层文档，`research/` 不进包。
- 一次性的验证脚本、压测、探针**不要**写进 `src/`，放 `research/experiments/<yyyy-mm-dd-tag>/`，再在代码注释里引用。
- 代码注释里引用研究材料用相对仓库根的路径（`research/notes/…`），别用 `../../`。

## 环境约定

- 依赖管理用 `uv`（不要 conda / 裸 pip / poetry），版本由 `requires-python` 固定。在本仓库跑命令用 `uv run --frozen`，别让 `uv run` 顺手重写 `uv.lock` 里的镜像地址。
- 秘密走 `.env`（gitignored），模板放 `.env.example`。
- 平台：Windows 10 + WSL2（Ubuntu，systemd=true）。**跨 Windows/WSL 边界的行为都要实测，不要凭直觉**（9p、inotify、localhost、路径）。

## 领域约定（本研究特有）

- **区分 L1/L2/L3**（定义见 `README.md`）。讨论「记住状态」时一律说清是哪一层 —— 混着说是这个项目最容易犯的错。
- **tmux 的两个协议层不要混**：unix socket 的 wire format 是内部实现，**绝不碰**；只用控制模式 `-CC` 的文本协议。
- **读会话数据是只读的**：`~/.claude/projects/` 和 `~/.codex/` 里的文件由 CLI 自己管理，**只读不写**，别原地改、别整理、别删。
- **格式假设要防御**：那两个 jsonl 的字段是逆向观察出来的，不是公开契约，CLI 升级可能变。解析要能容忍未知 type / 缺字段，**不要因为一条脏行整个侧栏挂掉**。
- **结构化返回**：会话条目用具名类型（`SessionEntry(id, title, source, cwd, gitBranch, updatedAt)`），不要传裸 dict。
- **性能声明要有实测**：`%output` 膨胀率、同时挂 N 个会话的 CPU —— 这类数字必须来自 `research/experiments/` 里的压测，不能估。

## Claude 协作须知

- 进入本研究先读 `README.md` → `research/notes/idea-log.md` → `research/notes/2026-08-12-design-session.md`。
- 不要修改 `../reference/` 里的任何文件。
- 涉及外部服务调用前，按全局规则先跟用户确认。
- 装 tmux 插件 / 改 `~/.tmux.conf` 这类**动用户全局环境**的操作，先说清楚要改什么再动手。
