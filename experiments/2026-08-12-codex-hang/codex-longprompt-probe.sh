#!/usr/bin/env bash
# 长 prompt + </dev/null 到底会不会挂？跑 3 轮，用 --json 看它在等什么。
set -u
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT; cd "$WORK" || exit 1

CX=(codex exec --ephemeral --skip-git-repo-check -s read-only -C "$WORK" --json)
FILLER="$(head -c 40000 /dev/zero | tr '\0' 'x')"
LONG="Ignore the following filler entirely: ${FILLER}

Reply with exactly the single word PONG and nothing else."

for i in 1 2 3; do
  t0=$(date +%s.%N)
  timeout 240 "${CX[@]}" "$LONG" < /dev/null > "$WORK/r$i.jsonl" 2>&1
  rc=$?
  t1=$(date +%s.%N)
  printf '轮%d  rc=%-4s %6.1fs\n' "$i" "$rc" "$(echo "$t1 - $t0" | bc)"
  # 事件类型分布 + 任何 error/retry 字样
  grep -o '"type":"[a-z_.]*"' "$WORK/r$i.jsonl" 2>/dev/null | sort | uniq -c | sed 's/^/      /'
  grep -io 'retry\|rate.limit\|at capacity\|429\|503\|timeout' "$WORK/r$i.jsonl" 2>/dev/null \
    | sort | uniq -c | sed 's/^/      ⚠ /'
done
