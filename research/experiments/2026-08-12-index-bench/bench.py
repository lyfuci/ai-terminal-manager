"""索引构建的吞吐压测（2026-08-12）。

CLAUDE.md 要求「性能声明必须来自 research/experiments/ 里的压测，不能估」，这就是那份实测。

跑法：`uv run --project ../../app python bench.py`
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

ROUNDS = 5


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
    from atm import cache as cache_mod
    from atm import index as index_mod
    from atm.sources import claude, codex

    bench_cache = Path("/tmp/atm-bench-cache.json")

    print("== 语料规模 ==")
    claude_refs = list(claude.discover())
    codex_refs = list(codex.discover())
    claude_bytes = sum(r.size_bytes for r in claude_refs)
    codex_bytes = sum(r.size_bytes for r in codex_refs)
    print(f"  Claude: {len(claude_refs)} 个会话文件, {claude_bytes / 1e9:.2f} GB")
    print(f"  Codex : {len(codex_refs)} 个会话文件, {codex_bytes / 1e6:.0f} MB")
    print(f"  最大单文件: {max(r.size_bytes for r in claude_refs) / 1e6:.0f} MB")

    print("\n== 冷启动（全量解析，无缓存）==")
    cold: list[float] = []
    for i in range(ROUNDS):
        bench_cache.unlink(missing_ok=True)
        start = time.perf_counter()
        result = index_mod.build(use_cache=False, cache_path=bench_cache)
        elapsed = (time.perf_counter() - start) * 1000
        cold.append(elapsed)
        if i == 0:
            print(f"  {result.stats.describe()}")
    print(f"  中位数 {statistics.median(cold):.0f}ms  最快 {min(cold):.0f}ms  最慢 {max(cold):.0f}ms")

    print("\n== 热启动（缓存全命中）==")
    index_mod.build(use_cache=True, cache_path=bench_cache)  # 预热
    warm: list[float] = []
    for _ in range(ROUNDS):
        start = time.perf_counter()
        result = index_mod.build(use_cache=True, cache_path=bench_cache)
        warm.append((time.perf_counter() - start) * 1000)
    print(f"  {result.stats.describe()}")
    print(f"  中位数 {statistics.median(warm):.0f}ms  最快 {min(warm):.0f}ms  最慢 {max(warm):.0f}ms")

    print("\n== 读取量 ==")
    total_files = len(claude_refs) + len(codex_refs)
    total_bytes = claude_bytes + codex_bytes
    # 每个文件最多读 head 上限，Claude 256KB / Codex 512KB
    capped = sum(min(r.size_bytes, 256 * 1024) for r in claude_refs) + sum(
        min(r.size_bytes, 512 * 1024) for r in codex_refs
    )
    print(f"  语料总大小        {total_bytes / 1e9:.2f} GB")
    print(f"  冷启动实际读取    {capped / 1e6:.0f} MB ({capped / total_bytes * 100:.1f}%)")
    print(f"  —— 只读头部省掉了 {(total_bytes - capped) / 1e9:.2f} GB 的 IO")

    cache_size = bench_cache.stat().st_size
    print(f"\n  缓存文件 {cache_size / 1024:.0f} KB，覆盖 {total_files} 个文件")

    bench_cache.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
