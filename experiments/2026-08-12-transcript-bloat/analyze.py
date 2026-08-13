"""巨型会话转录的成分分析（2026-08-12）。

起因：一条会话的 jsonl 长到 649MB，resume 它把机器搞死了。要搞清楚这些字节到底是什么。

**内存安全是硬约束**：本机 WSL 只有 6GB，且刚因为内存被打爆冻结过一次。
所以这个脚本：
- 按固定大小的块流式读，绝不整文件读进来；
- 单行超过上限就**只统计长度、丢弃内容**，不在内存里保留；
- 全程只累加计数器，内存占用与文件大小无关。

跑法：`uv run --project ../../app python analyze.py [文件路径...]`
"""

from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

CHUNK = 1 << 20  # 1MB
# 超过这个长度的行不解析、不保留内容，只记长度和用正则从头部猜类型
MAX_KEEP = 2 << 20  # 2MB
TYPE_RE = re.compile(rb'"type"\s*:\s*"([^"]{1,60})"')
# 多媒体/编码数据的特征
B64_RE = re.compile(rb'"(?:data|source|base64|image_url)"\s*:\s*"(?:data:)?[A-Za-z0-9+/]{200,}')
MEDIA_RE = re.compile(rb'"(?:media_type|mimeType|mime_type)"\s*:\s*"([^"]{1,40})"')


def iter_lines(path: Path):
    """流式产出行。超长行只产出 (长度, 头部片段)，不把整行留在内存里。"""
    buf = bytearray()
    truncated_len = 0
    truncating = False
    head = b""

    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            start = 0
            while True:
                nl = chunk.find(b"\n", start)
                if nl == -1:
                    rest = chunk[start:]
                    if truncating:
                        truncated_len += len(rest)
                    else:
                        buf += rest
                        if len(buf) > MAX_KEEP:
                            truncating = True
                            head = bytes(buf[:4096])
                            truncated_len = len(buf)
                            buf = bytearray()
                    break

                piece = chunk[start:nl]
                if truncating:
                    truncated_len += len(piece)
                    yield truncated_len, head, True
                    truncating = False
                    truncated_len = 0
                    head = b""
                else:
                    buf += piece
                    yield len(buf), bytes(buf), False
                    buf.clear()
                start = nl + 1

    if truncating:
        yield truncated_len, head, True
    elif buf:
        yield len(buf), bytes(buf), False


def analyze(path: Path) -> None:
    size = path.stat().st_size
    print(f"\n{'=' * 78}\n{path.name}  ({size / 1048576:.0f} MB)\n{'=' * 78}")

    by_type_bytes: collections.Counter = collections.Counter()
    by_type_count: collections.Counter = collections.Counter()
    media_types: collections.Counter = collections.Counter()
    b64_lines = 0
    b64_bytes = 0
    oversized = 0
    biggest: list[tuple[int, str]] = []

    for length, payload, was_truncated in iter_lines(path):
        match = TYPE_RE.search(payload[:4096])
        rtype = match.group(1).decode("utf-8", "replace") if match else "?"

        if was_truncated:
            oversized += 1
            rtype = f"{rtype} (超大行)"
        else:
            if B64_RE.search(payload):
                b64_lines += 1
                b64_bytes += length
            for m in MEDIA_RE.finditer(payload[:8192]):
                media_types[m.group(1).decode("utf-8", "replace")] += 1

        by_type_bytes[rtype] += length
        by_type_count[rtype] += 1

        biggest.append((length, rtype))
        if len(biggest) > 200:
            biggest.sort(reverse=True)
            del biggest[20:]

    total = sum(by_type_bytes.values())
    print(f"\n总行数 {sum(by_type_count.values()):,}   超大行(>2MB) {oversized}")
    print(f"\n{'记录类型':<28} {'条数':>9} {'占用':>12} {'占比':>7}  {'平均/条':>10}")
    print("-" * 74)
    for rtype, nbytes in by_type_bytes.most_common(14):
        n = by_type_count[rtype]
        print(
            f"{rtype:<28} {n:>9,} {nbytes / 1048576:>10.1f}MB "
            f"{nbytes / total * 100:>6.1f}%  {nbytes / n / 1024:>8.1f}KB"
        )

    print(f"\n疑似 base64 大块数据的行：{b64_lines:,} 行，{b64_bytes / 1048576:.1f}MB "
          f"({b64_bytes / total * 100:.1f}%)")
    if media_types:
        print("媒体类型：", dict(media_types.most_common(8)))
    else:
        print("媒体类型：无 —— 没有发现 image/audio 之类的 media_type 字段")

    biggest.sort(reverse=True)
    print("\n最大的 5 行：")
    for length, rtype in biggest[:5]:
        print(f"  {length / 1048576:>7.2f}MB  {rtype}")


def main() -> int:
    targets = [Path(p) for p in sys.argv[1:]]
    if not targets:
        root = Path.home() / ".claude" / "projects"
        found = sorted(
            (p for p in root.glob("*/*.jsonl")), key=lambda p: p.stat().st_size, reverse=True
        )
        targets = found[:3]
    for path in targets:
        analyze(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
