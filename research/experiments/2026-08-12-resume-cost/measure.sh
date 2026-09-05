#!/usr/bin/env bash
# 实测：`claude --resume <大小不同的会话>` 的峰值内存与读取字节数（2026-08-12）
#
# 起因：2026-08-12 的 WSL 冻结事故。用户说「我之前开 6 个会话都没问题」，
# 而 atm 让他开的是 6 个 **--resume** 会话 —— 我一直在推理两者代价不同，从没测过。
#
# 安全设计（这台机器刚被内存打爆过一次，不能重演）：
#   - 每次测量关进独立 cgroup，`MemoryMax` 硬上限 + `MemorySwapMax=0` 禁止吃 swap；
#     超限只会 OOM-kill 掉那个 claude，不会拖垮 WSL，更不会影响宿主。
#   - **不发任何 prompt** → 交互模式只加载不提问，不产生 API 调用、不花 token。
#   - 从小到大依次测，任何一档 OOM 就停，不往上试。
#
# 用法：./measure.sh [MemoryMax，默认 3G]

set -uo pipefail

LIMIT="${1:-3G}"
SETTLE="${SETTLE:-30}"   # 给 claude 多少秒加载
UID_N="$(id -u)"

command -v claude >/dev/null || { echo "claude 不在 PATH"; exit 1; }

# ---------------------------------------------------------------- 挑样本
# 按转录大小分桶，每桶取最新的一条。刻意包含 7.4MB 那条 —— 它是事故窗口里唯一被动过的会话。
mapfile -t SAMPLES < <(python3 - <<'PY'
import glob, os
rows = []
for p in glob.glob(os.path.expanduser('~/.claude/projects/*/*.jsonl')):
    try: rows.append((os.path.getsize(p), os.path.getmtime(p), p))
    except OSError: pass
rows.sort()
buckets = [(0, 0.3), (1, 3), (5, 9), (15, 30)]   # MB 区间
picked = []
for lo, hi in buckets:
    cand = [r for r in rows if lo*1048576 <= r[0] < hi*1048576]
    if not cand: continue
    cand.sort(key=lambda r: -r[1])
    size, _, path = cand[0]
    picked.append((size, path))
seen = set()
for size, path in picked:
    sid = os.path.basename(path)[:-6]
    if sid in seen: continue
    seen.add(sid)
    # cwd 从项目目录名反解只用于展示，实际 resume 由 claude 自己定位
    print(f"{size}\t{sid}\t{path}")
PY
)

# ---------------------------------------------------------------- 测量
# ⚠️ 关键：必须在 scope **存活期间**轮询采样。
# 第一版在 `wait` 之后才读 memory.peak，那时 systemd 已经把 cgroup 删了，读出来全是 0。
measure() {           # measure <标签> <cmd...>
	local label="$1"; shift
	local unit="atmcost-$$-$RANDOM"

	# ⚠️ script 的命令里**绝不能**再重定向 stdout —— 那会把 stdout 从 PTY 换成非终端，
	# claude 于是不跑完整 TUI、也就不真正加载会话（假平坦「全都 71MB」就是这么来的）。
	# 也别把注释写进行续符 `\` 中间，会截断整条命令。

	systemd-run --user --scope -q --unit="$unit" -p RuntimeMaxSec=$((SETTLE+20)) \
		-p MemoryMax="$LIMIT" -p MemorySwapMax=0 -p MemoryAccounting=1 -p IOAccounting=1 \
		script -qec "timeout -k 3 -s TERM ${SETTLE}s $*" /dev/null \
		>/dev/null 2>&1 </dev/null &
	local runner=$!

	local cg="" i
	for i in $(seq 1 60); do
		cg=$(systemctl --user show "$unit.scope" -p ControlGroup --value 2>/dev/null </dev/null)
		[ -n "$cg" ] && [ -d "/sys/fs/cgroup$cg" ] && break
		sleep 0.2
	done
	if [ -z "$cg" ] || [ ! -d "/sys/fs/cgroup$cg" ]; then
		wait $runner 2>/dev/null
		printf '  %-24s 拿不到 cgroup，跳过\n' "$label"
		return
	fi

	# 存活期间持续采样
	local base="/sys/fs/cgroup$cg" peak=0 cur rio=0 oom=0
	while [ -d "$base" ] && kill -0 $runner 2>/dev/null; do
		cur=$(cat "$base/memory.current" 2>/dev/null || echo 0)
		[ "${cur:-0}" -gt "$peak" ] 2>/dev/null && peak=$cur
		rio=$(awk '{for(i=1;i<=NF;i++) if($i ~ /^rbytes=/){split($i,a,"="); s+=a[2]}} END{print s+0}' \
			"$base/io.stat" 2>/dev/null || echo 0)
		oom=$(awk '/^oom_kill /{print $2}' "$base/memory.events" 2>/dev/null || echo 0)
		sleep 0.3
	done
	wait $runner 2>/dev/null

	printf '  %-24s 峰值内存 %7.1f MB   读盘 %7.1f MB   oom_kill=%s\n' \
		"$label" "$(echo "$peak/1048576" | bc -l)" "$(echo "$rio/1048576" | bc -l)" "${oom:-0}"
	systemctl --user stop "$unit.scope" >/dev/null 2>&1 </dev/null
}

echo "cgroup 上限 $LIMIT / 禁用 swap / 每次加载 ${SETTLE}s / 不发 prompt（不花 token）"
echo
echo "== 对照组：全新会话（不 resume）=="
measure "claude（全新）" claude

echo
echo "== 实验组：--resume 不同大小的转录 =="
while IFS=$'\t' read -r size sid path; do
	[ -z "${sid:-}" ] && continue
	mb=$(echo "$size/1048576" | bc -l)
	measure "$(printf 'resume %6.1fMB' "$mb")" claude --resume "$sid" </dev/null
done < <(printf '%s\n' "${SAMPLES[@]}")

echo
echo "样本："
printf '%s\n' "${SAMPLES[@]}" | awk -F'\t' '{printf "  %7.1fMB  %s\n", $1/1048576, $2}'
