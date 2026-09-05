#!/usr/bin/env bash
# atm 的 tmux 插件入口（tpm 兼容）。
#
# 装法二选一：
#   1) tpm —— 在 ~/.tmux.conf 里加 `set -g @plugin 'path/to/ai-terminal-manager'`，再按 prefix + I
#   2) 手动 —— 在 ~/.tmux.conf 里加 `run-shell /绝对路径/atm.tmux`
#
# 默认绑定 prefix + a（a = agent），在 display-popup 浮层里唤出选择器。
# 浮层是「查历史、投一次」的手势；常驻左侧 pane（prefix + b）是 2026-09-02 加的第二个维度：
# 列出**正在跑**的格子，选中就 swap-pane 换进主格 —— 两种入口共用一份代码。
#
# 想改键位就在 ~/.tmux.conf 里设：
#   set -g @atm-key 'a'
#   set -g @atm-popup-width '80%'
#   set -g @atm-popup-height '70%'
#   set -g @atm-sidebar-key 'b'

set -euo pipefail

CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

tmux_option() {
	local value
	value="$(tmux show-option -gqv "$1")"
	if [ -n "$value" ]; then printf '%s' "$value"; else printf '%s' "$2"; fi
}

KEY="$(tmux_option '@atm-key' 'a')"
WIDTH="$(tmux_option '@atm-popup-width' '80%')"
HEIGHT="$(tmux_option '@atm-popup-height' '70%')"

# 优先用 PATH 里已安装的 atm；没装就退回仓库内的源码，免得非要先 uv install 才能试。
if command -v atm >/dev/null 2>&1; then
	ATM_CMD='atm'
elif command -v uv >/dev/null 2>&1; then
	ATM_CMD="uv run --project '$CURRENT_DIR' atm"
else
	ATM_CMD="PYTHONPATH='$CURRENT_DIR/src' python3 -m atm"
fi

tmux bind-key "$KEY" display-popup -E -w "$WIDTH" -h "$HEIGHT" "$ATM_CMD pick"

# prefix + A（大写）：只看当前目录的会话，省一步筛选
tmux bind-key "$(printf '%s' "$KEY" | tr '[:lower:]' '[:upper:]')" \
	display-popup -E -w "$WIDTH" -h "$HEIGHT" "$ATM_CMD pick --here"

# ── 常驻侧栏（2026-09-02）──────────────────────────────────────────────
# prefix + b：开 / 切到 / 收起当前 window 最左边的侧栏；prefix + B：把当前格子收进后台窗口 bg。
# `#{pane_id}` 由 tmux 在执行前展开 —— run-shell 里的 $TMUX_PANE 实测不可靠。
SIDEBAR_KEY="$(tmux_option '@atm-sidebar-key' 'b')"
tmux bind-key "$SIDEBAR_KEY" run-shell -b "$ATM_CMD sidebar --toggle --pane '#{pane_id}'"
tmux bind-key "$(printf '%s' "$SIDEBAR_KEY" | tr '[:lower:]' '[:upper:]')" \
	run-shell -b "$ATM_CMD park '#{pane_id}'"
