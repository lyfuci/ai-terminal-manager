# 贡献指南

欢迎给 atm 提 PR。这份指南只有两条是硬约束（见下「铁律」），其余都是让评审更快的建议。

## 开发环境

Linux 或 WSL2 + tmux（3.0+，开发基准 3.6）+ Python 3.11+。依赖管理统一用 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/lyfuci/ai-terminal-manager
cd ai-terminal-manager/app
uv sync                      # 建虚拟环境并安装 dev 依赖
uv run pytest                # 全量测试
uv run ruff check src tests  # lint
uv run ruff format src tests # 格式化
```

装成本机命令：`uv tool install --editable .`（或 `uv tool install 'atm @ git+https://github.com/lyfuci/ai-terminal-manager#subdirectory=app'`）。

## 铁律（不遵守的 PR 会被要求改）

1. **会话数据只读**。atm 解析 `~/.claude/projects/` 和 `~/.codex/` 下的文件，这两个目录由
   CLI 自己管理 —— 任何代码路径都不得写入、移动、删除或“整理”它们。测试只能用自建的
   tmp_path fixture，绝不读取贡献者机器上的真实会话数据。
2. **tmux 只走公开 CLI**。绝不读写 `/tmp/tmux-<uid>/*` 的 unix socket，也绝不解析控制模式
   (`-CC`) 之外的上游私有协议。需要 tmux 的测试用临时 fixture，不用真 server。
3. **索引层不依赖 tmux**。`src/atm/index.py` 及其下游（model / sources / cache / jsonl）刻意
   保持可以在 tmux 之外运行，为将来的其他后端留路。别在这里 import `tmux.py`。
4. **格式假设要防御**。两个 CLI 的 JSONL 是逆向观察出来的，不是公开契约。解析必须容忍
   未知 type / 缺字段 / 脏行，绝不允许一条坏记录弄挂整个索引。

## 写测试

- 新功能先写测试（RED → GREEN → REFACTOR）；修 bug 先写一个能复现的失败测试再修。
- tmux 输出解析类测试（如 `parse_panes`）是格式变更的第一道防线，改动输出格式时同步更新。
- 涉及 tmux 真实行为（如 `swap-pane` 跨窗口、`split-window -f`）的结论，优先补一个
  `experiments/<yyyy-mm-dd>-<tag>/` 下的隔离 socket 验证脚本，再在代码注释里引用它。

## tmux 实验规则（在真机器上验证时）

- 一律用隔离 socket：`tmux -L <名字> -f /dev/null`。**只 `-L` 不 `-f` 不够** —— 新 server
  仍会 source `~/.tmux.conf`，带 `@continuum-restore on` 的环境会把用户整个存档恢复到你的
  测试 server 里（实测发生过）。
- 用完 `kill-server` 并删掉 socket 文件；不要用 `pkill -f tmux` 这类模式匹配清理。
- 绝不碰用户的默认 server 或默认 socket。

## 提交

- 从最新 `main` 切分支；commit message 用 `feat:` / `fix:` / `docs:` / `test:` / `chore:` 前缀。
- PR 请附：动机、改动摘要、验证方式（测试输出或实验记录）。
- 新依赖需要理由 —— atm 刻意保持零运行时依赖（要在 `display-popup` 里反复冷启动），
  标准库能解决就不加。

## 想贡献但不知道从哪开始

看看 issue 里的 `good first issue`；或者这些方向永远欢迎：

- 新 CLI 的会话数据源适配（`src/atm/sources/` 加一个适配器）。
- 现有解析对 CLI 新版本的兼容修复（尤其标题提取，见 `text.py` 的注入包装剥离）。
- TUI 体验、终端宽度/东亚字符处理（`text.py`）。

## 暂不接受的方向

- 自写常驻 daemon / GUI（架构分岔口 A/B 未拍板，见根目录 README「未拍板的分岔口」）。
- 读写 tmux 私有 wire protocol。
- 任何把用户会话数据上传到网络的功能 —— atm 的边界是本机工具。