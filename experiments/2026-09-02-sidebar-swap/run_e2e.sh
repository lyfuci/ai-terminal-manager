#!/usr/bin/env bash
# 常驻侧栏端到端：在**隔离 socket** 上验证 atm sidebar --toggle / swap / park 三条路径。
#
# 两条硬约束（README「已知的坑」第 4 条 + 2026-08-12 事故）：
#   1. 必须 `-L <name>` **并且** `-f /dev/null` —— 只 -L 不 -f 的话新 server 照样 source
#      ~/.tmux.conf，@continuum-restore 会把用户的整个存档恢复到测试 server 里（实测拉起过 5 个 claude）。
#   2. 退出时 kill-server + 删 socket 文件。
set -euo pipefail

SOCK="atm-e2e-sidebar-$$"
T=(tmux -L "$SOCK" -f /dev/null)
cleanup() { "${T[@]}" kill-server 2>/dev/null || true; rm -f "/tmp/tmux-$(id -u)/$SOCK"; }
trap cleanup EXIT

# atm 子进程要连同一个 server：通过 TMUX 环境变量把 socket 路径带进去，
# tmux 客户端会从 $TMUX 的第一段取 socket。
export TMUX="/tmp/tmux-$(id -u)/$SOCK,0,0"
export TMUX_PANE=

pass=0; fail=0
check() { if eval "$2"; then echo "  ✅ $1"; pass=$((pass+1)); else echo "  ❌ $1"; fail=$((fail+1)); fi; }
panes() { "${T[@]}" list-panes -a -F '#{session_name}:#{window_name}.#{pane_index} #{pane_id} w=#{pane_width} h=#{pane_height} cmd=#{pane_current_command} sb=[#{@atm_sidebar}]'; }

"${T[@]}" new-session -d -s A -n w0 -x 140 -y 40 'sleep 900'
"${T[@]}" split-window -t A:w0 -v 'sleep 900'                 # 主区上下两格
MAIN=$("${T[@]}" display -p -t A:w0.0 '#{pane_id}')
OTHER=$("${T[@]}" display -p -t A:w0.1 '#{pane_id}')
"${T[@]}" new-window -t A -d -n bg 'sleep 900'                # 停车场里已有一个后台进程
BG=$("${T[@]}" display -p -t A:bg.0 '#{pane_id}')
"${T[@]}" select-pane -t "$MAIN"

echo "== 1. toggle：打开通高侧栏 =="
atm sidebar --toggle --pane "$MAIN" --width 30 2>&1 | sed 's/^/  /'
SB=$("${T[@]}" list-panes -t A:w0 -F '#{pane_id} #{@atm_sidebar} #{pane_height} #{pane_left}' | awk '$2=="1"{print $1}')
check "侧栏 pane 存在且带 @atm_sidebar 标记" '[ -n "$SB" ]'
check "侧栏通高（=窗口高 40）且在最左" '[ "$("${T[@]}" display -p -t "$SB" "#{pane_height}:#{pane_left}")" = "40:0" ]'
check "侧栏宽 30" '[ "$("${T[@]}" display -p -t "$SB" "#{pane_width}")" = "30" ]'
sleep 1.5   # 让 atm sidebar 进程起来
check "侧栏 pane 里跑的是 atm（python）" '"${T[@]}" display -p -t "$SB" "#{pane_current_command}" | grep -qE "atm|python"'
check "侧栏画出了「运行中」段" '"${T[@]}" capture-pane -p -t "$SB" | grep -q "运行中"'

echo "== 2. toggle：焦点不在侧栏 → 切过去 =="
"${T[@]}" select-pane -t "$MAIN"
atm sidebar --toggle --pane "$MAIN" 2>&1 | sed 's/^/  /'
check "活动 pane 变成侧栏" '[ "$("${T[@]}" display -p -t A:w0 "#{pane_id}")" = "$SB" ]'
check "没有开出第二个侧栏" '[ "$("${T[@]}" list-panes -t A:w0 -F "#{@atm_sidebar}" | grep -c 1)" = "1" ]'

echo "== 3. swap：把 bg 里的 pane 换进主格 =="
# 侧栏拿焦点前用户在看 MAIN，所以 MAIN 是 pane_last → 主格
atm swap "$BG" --pane "$SB" 2>&1 | sed 's/^/  /'
check "BG 现在在 w0 里" '"${T[@]}" list-panes -t A:w0 -F "#{pane_id}" | grep -qx "$BG"'
check "MAIN 被换去了 bg 窗口" '"${T[@]}" list-panes -t A:bg -F "#{pane_id}" | grep -qx "$MAIN"'
check "焦点在换进来的 BG 上" '[ "$("${T[@]}" display -p -t A:w0 "#{pane_id}")" = "$BG" ]'
check "侧栏没动（仍通高 30 列）" '[ "$("${T[@]}" display -p -t "$SB" "#{pane_width}:#{pane_height}")" = "30:40" ]'
check "swap 后进程没断（BG 的 sleep 还在）" '[ "$("${T[@]}" display -p -t "$BG" "#{pane_current_command}")" = "sleep" ]'

echo "== 4. swap：目标已在本窗口 → 只切焦点 =="
atm swap "$OTHER" --pane "$SB" 2>&1 | sed 's/^/  /'
check "焦点切到 OTHER" '[ "$("${T[@]}" display -p -t A:w0 "#{pane_id}")" = "$OTHER" ]'
check "布局没变（w0 仍是 3 格）" '[ "$("${T[@]}" display -p -t A:w0 "#{window_panes}")" = "3" ]'

echo "== 5. park：把 OTHER 收进已有的 bg =="
atm park "$OTHER" 2>&1 | sed 's/^/  /'
check "OTHER 进了 bg" '"${T[@]}" list-panes -t A:bg -F "#{pane_id}" | grep -qx "$OTHER"'
check "w0 剩 侧栏+BG 两格" '[ "$("${T[@]}" display -p -t A:w0 "#{window_panes}")" = "2" ]'
check "焦点仍留在 w0（-d 不切过去）" '[ "$("${T[@]}" display -p -t A "#{window_name}")" = "w0" ]'

echo "== 6. park 拒绝：侧栏自己 / 独占窗口的 pane =="
check "拒绝收侧栏" '! atm park "$SB" 2>/dev/null'
"${T[@]}" new-window -t A -d -n solo 'sleep 900'; SOLO=$("${T[@]}" display -p -t A:solo '#{pane_id}')
check "拒绝收独占窗口的 pane（否则窗口会被改名成 bg）" '! atm park "$SOLO" 2>/dev/null'
check "solo 窗口还在" '"${T[@]}" list-windows -t A -F "#{window_name}" | grep -qx solo'

echo "== 7. toggle：焦点在侧栏 → 收起，主区占满 =="
"${T[@]}" select-pane -t "$SB"
atm sidebar --toggle --pane "$SB" 2>&1 | sed 's/^/  /'
check "侧栏 pane 没了" '! "${T[@]}" list-panes -a -F "#{pane_id}" | grep -qx "$SB"'
check "BG 占满整行（宽 140）" '[ "$("${T[@]}" display -p -t "$BG" "#{pane_width}")" = "140" ]'

echo; panes | sed 's/^/  /'
echo; echo "通过 $pass，失败 $fail"
[ "$fail" -eq 0 ]
