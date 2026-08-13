#!/usr/bin/env bash
# 端到端验证：resurrect 的 `->` 映射能不能让**恢复出来的**进程带上 cgroup 限制。
#
# ⚠️ 两条硬约束（都是踩过才知道的）：
#   1. 必须 -L 独立 socket，绝不碰用户的 default
#   2. 必须 -f /dev/null —— 独立 server **照样会 source ~/.tmux.conf**，
#      而用户配置里 @continuum-restore=on，会让这个测试 server 把用户的存档
#      整个恢复一遍（实测真的发生了，一次性拉起 5 个 claude）。
# 用 sleep 当 claude 的替身：机制完全同构，但不吃内存。
set -u
SOCK="atmslice$$"
DIR="$(mktemp -d)"
cleanup() {
  tmux -L "$SOCK" kill-server 2>/dev/null   # -L 必须有
  rm -f "/tmp/tmux-$(id -u)/$SOCK"
  rm -rf "$DIR"
  echo "[清理] socket=$SOCK 已杀并删除，临时目录已删"
}
trap cleanup EXIT

R=~/.tmux/plugins/tmux-resurrect/scripts
T=(tmux -L "$SOCK" -f /dev/null)

"${T[@]}" new-session -d -s t -x 80 -y 24
"${T[@]}" set-option -g @resurrect-dir "$DIR"
"${T[@]}" set-option -g @resurrect-processes "\"~sleep->$HOME/.local/bin/ai-scope sleep *\""
"${T[@]}" send-keys -t t "sleep 4321" Enter
sleep 2

echo "== 0. 确认这是干净的隔离环境（不该有 main/console）=="
"${T[@]}" list-sessions -F '  session: #{session_name}'
echo "  @resurrect-processes = $("${T[@]}" show-options -gv @resurrect-processes)"

echo "== 1. 存档前 pane 里跑的 =="
"${T[@]}" list-panes -a -F '  #{pane_current_command} (pid #{pane_pid})'

"${T[@]}" run-shell "$R/save.sh"; sleep 2
echo "== 2. 存档里记的命令行（应是**裸** sleep）=="
grep -o 'sleep 4321' "$DIR/last" | head -1 | sed 's/^/  /'

# ⚠️ 顺序不能反：杀掉**唯一**的会话会连 server 一起销毁，
# 我设在 server 上的 @resurrect-dir 跟着消失，restore 就回落到默认目录
# （= 用户的真实存档）。必须先建占位会话，让 server 活着。
"${T[@]}" new-session -d -s placeholder -x 80 -y 24
"${T[@]}" kill-session -t t
echo "  恢复前 @resurrect-dir = $("${T[@]}" show-options -gv @resurrect-dir)"
"${T[@]}" run-shell "$R/restore.sh"; sleep 5

echo "== 3. 恢复后实际跑起来的进程 =="
pid=$(pgrep -f "sleep 4321" | head -1)
[ -z "$pid" ] && { echo "  ❌ 没恢复出来"; "${T[@]}" list-panes -a -F '  #{session_name}:#{pane_current_command}'; exit 1; }
ps -o pid,args -p "$pid" | tail -1 | sed 's/^/  /'

echo "== 4. 落在哪个 cgroup（关键判据）=="
cg=$(cut -d: -f3 /proc/"$pid"/cgroup)
echo "  $cg"
case "$cg" in
  *atm-ai.slice*) echo "  ✅ 在 atm-ai.slice 里 —— 恢复出来的进程带上了限制" ;;
  *) echo "  ❌ 不在 atm-ai.slice 里 —— 映射没生效" ;;
esac

echo "== 5. 单进程那层的限制 =="
for f in memory.high memory.max; do
  printf '  %-14s %s\n' "$f" "$(cat "/sys/fs/cgroup$cg/$f" 2>/dev/null)"
done
kill "$pid" 2>/dev/null
