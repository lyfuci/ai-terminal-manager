#!/usr/bin/env bash
# 格子健康（atm health / 侧栏 ⚠ 标记）的依据：一个撞 MemoryHigh 的进程到底有多慢，
# 以及内核的哪个数能看出来。
#
# 做法：同一段「反复摸 200M 内存」的 python，分别在没有限制 / MemoryHigh=64M 的 scope 里跑，
# 记吞吐、PSI、memory.events(.local) 里 high 的增速、进程的 utime/stime。
# 然后在**隔离 socket**（-L + -f /dev/null）的 tmux 里开侧栏，看它能不能标出来。
#
# 只动 `systemd-run --user --scope` 临时建的 scope 和隔离 tmux server，退出时都清掉。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
WORK=$(mktemp -d)
SOCK="atm-health-$$"
T=(tmux -L "$SOCK" -f /dev/null)
cleanup() { "${T[@]}" kill-server 2>/dev/null || true; rm -rf "$WORK"; }
trap cleanup EXIT

cat > "$WORK/hog.py" <<'PY'
import sys, time
secs = float(sys.argv[1])
b = [bytearray(1 << 20) for _ in range(200)]
t = time.time(); n = 0
while time.time() - t < secs:
    for x in b:
        for i in range(0, 1 << 20, 4096):
            x[i] = 1
    n += 1
print(f"passes/s {n / secs:.2f}", flush=True)
PY

high() { awk '/^high/{print $2}' "$1" 2>/dev/null || echo 0; }
ticks() { awk '{print $14, $15}' "/proc/$1/stat"; }

echo "== 1. 吞吐：没有限制 =="
python3 "$WORK/hog.py" 5

echo "== 2. MemoryHigh=64M（工作集 200M）=="
systemd-run --user --scope -q -p MemoryHigh=64M -p MemoryMax=512M python3 "$WORK/hog.py" 16 &
HOG=$!
sleep 4
CG=/sys/fs/cgroup$(sed -n 's/^0:://p' "/proc/$HOG/cgroup")
PARENT=$(dirname "$CG")
l1=$(high "$CG/memory.events.local"); p1=$(high "$PARENT/memory.events"); pl1=$(high "$PARENT/memory.events.local")
read -r u1 s1 < <(ticks "$HOG")
sleep 5
l2=$(high "$CG/memory.events.local"); p2=$(high "$PARENT/memory.events"); pl2=$(high "$PARENT/memory.events.local")
read -r u2 s2 < <(ticks "$HOG")
echo "  scope memory.events.local high: $(( (l2 - l1) / 5 ))/s"
echo "  父层 $(basename "$PARENT") memory.events high: $(( (p2 - p1) / 5 ))/s（层级累计，含子 scope）"
echo "  父层 $(basename "$PARENT") memory.events.local high: $(( (pl2 - pl1) / 5 ))/s"
echo "  utime +$((u2 - u1)) / stime +$((s2 - s1)) ticks in 5s（$(getconf CLK_TCK)/s）"
sed 's/^/  memory.pressure: /' "$CG/memory.pressure" | head -1
sed 's/^/  io.pressure: /' "$CG/io.pressure" | head -1
wait "$HOG"

echo "== 3. 隔离 tmux 里的侧栏 =="
"${T[@]}" new-session -d -s t -x 140 -y 30 \
  "env XDG_STATE_HOME=$WORK/state ATM_LANG=zh $REPO/.venv/bin/atm sidebar"
SB=$("${T[@]}" display -p -t t '#{pane_id}')
"${T[@]}" split-window -h -t t \
  "systemd-run --user --scope -q -p MemoryHigh=64M -p MemoryMax=512M python3 $WORK/hog.py 16; sleep 60"
"${T[@]}" resize-pane -t "$SB" -x 34
sleep 12
echo "  -- 进行中（侧栏前 3 行）"
"${T[@]}" capture-pane -p -t "$SB" | sed -n '2,3p' | sed 's/^/  /'
sleep 12
echo "  -- 结束后"
"${T[@]}" capture-pane -p -t "$SB" | sed -n '2,3p' | sed 's/^/  /'
echo "  -- 统计日志（去掉 scope 名）"
sed -E 's/run-r[0-9a-f]+\.scope/run-*.scope/g' "$WORK/state/atm/health.jsonl" | sed 's/^/  /'
