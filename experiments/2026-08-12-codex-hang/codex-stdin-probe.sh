#!/usr/bin/env bash
# codex exec 假死机制实测。
# 每个场景都用 timeout 强制中止（124 = 被 timeout 杀掉 = 挂住了）。
# 全部 --ephemeral：不往 ~/.codex 写会话文件。
set -u

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK" || exit 1

CX=(codex exec --ephemeral --skip-git-repo-check -s read-only -C "$WORK")
ASK='Reply with exactly the single word PONG and nothing else.'

# ~40KB 的填充，逼近「长 prompt」的量级
FILLER="$(head -c 40000 /dev/zero | tr '\0' 'x')"
LONG="Ignore the following filler entirely: ${FILLER}

${ASK}"

run() {
  local name="$1" limit="$2"; shift 2
  local t0 t1 rc out
  t0=$(date +%s.%N)
  out=$("$@" 2>&1); rc=$?
  t1=$(date +%s.%N)
  printf '%-34s rc=%-4s %6.1fs  %s\n' \
    "$name" "$rc" "$(echo "$t1 - $t0" | bc)" \
    "$(printf '%s' "$out" | tr '\n' ' ' | tail -c 90)"
}

echo "=== codex $(codex --version) ==="
echo "argv 长度上限 ARG_MAX = $(getconf ARG_MAX)"
echo

# A: 基线 —— 短 prompt 走 argv，stdin 立即 EOF
run "A 短prompt + </dev/null" 120 \
  timeout 120 "${CX[@]}" "$ASK" < /dev/null

# B: 长 prompt 走 argv，stdin 立即 EOF  ← 我上次说「仍然挂」的那个
run "B 长prompt(40K)argv + </dev/null" 180 \
  timeout 180 "${CX[@]}" "$LONG" < /dev/null

# C: 长 prompt 走 stdin（管道会 EOF）
printf '%s' "$LONG" > "$WORK/long.txt"
t0=$(date +%s.%N)
out=$(timeout 180 "${CX[@]}" < "$WORK/long.txt" 2>&1); rc=$?
t1=$(date +%s.%N)
printf '%-34s rc=%-4s %6.1fs  %s\n' "C 长prompt走stdin(会EOF)" "$rc" \
  "$(echo "$t1 - $t0" | bc)" "$(printf '%s' "$out" | tr '\n' ' ' | tail -c 90)"

# D: 短 prompt 走 argv，但 stdin 是一个**永不 EOF** 的管道 —— 假设中的假死。
# 用 FIFO 而不是 `sleep | codex`：写端 PID 我自己拿着，收尾精确杀掉，
# 不用 pkill -f（本会话里它自匹配误杀过三次）。
FIFO="$WORK/never-eof"
mkfifo "$FIFO"
sleep 300 > "$FIFO" &
HOLDER=$!
t0=$(date +%s.%N)
out=$(timeout 45 "${CX[@]}" "$ASK" < "$FIFO" 2>&1); rc=$?
t1=$(date +%s.%N)
kill "$HOLDER" 2>/dev/null
printf '%-34s rc=%-4s %6.1fs  %s\n' "D argv + stdin永不EOF" "$rc" \
  "$(echo "$t1 - $t0" | bc)" "$(printf '%s' "$out" | tr '\n' ' ' | tail -c 90)"

echo
echo "rc=124 表示被 timeout 强杀（即挂住了）"
