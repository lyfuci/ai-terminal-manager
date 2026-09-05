"""防御式 jsonl 读取。

这里的每一条约束都对应一个实测事实（2026-08-12，本机语料）：

- `~/.claude/projects/` 有 1459 个文件 / 2.1GB，**单个文件最大 648MB** → 绝不整文件读。
- Claude 的 `attachment` 记录可以非常大（一个文件里 1149 条）→ 单行也要设上限。
- 这两个 jsonl 的字段是逆向观察出来的，不是公开契约 → 单条脏行只能跳过，不能炸整个扫描。
- 需要的信息（标题 / cwd / gitBranch）实测都在**头部**：Claude 的 `ai-title` 中位数在第 11 行，
  Codex 的 `session_meta` 在第 1 行。所以只读头部就够。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

# 头部读多少字节。实测 Claude 文件大小中位数 193KB，ai-title 落在前 14 行内，
# 256KB 足够覆盖到第一条真实 user 消息，同时对 648MB 的文件也只是一次 256KB 读。
DEFAULT_HEAD_BYTES = 256 * 1024

# 超过这个长度的单行直接丢弃 —— 那必然是 attachment / 大 tool_result，
# 里面不会有标题或 cwd，解析它只会浪费 CPU。
DEFAULT_MAX_LINE_BYTES = 64 * 1024

# 尾部读多少字节。实测：最后一条 `custom-title`（会话改名）距文件尾最远 18KB，
# 64KB 已能覆盖本机全部 14 个命名会话，这里取 128KB 留一倍余量。
DEFAULT_TAIL_BYTES = 128 * 1024


def read_head_bytes(path: Path | str, max_bytes: int = DEFAULT_HEAD_BYTES) -> bytes:
    """读文件头部至多 max_bytes 字节。读不了就返回空，不抛。"""
    try:
        with open(path, "rb") as fh:  # noqa: PTH123 — 冷启动要开 1500+ 个文件，不为风格多建 Path
            return fh.read(max_bytes)
    except OSError:
        return b""


def head_yielded_nothing(path: Path | str, max_bytes: int = DEFAULT_HEAD_BYTES) -> bool:
    """头部窗口一条记录都解析不出来（整窗是一条超长行）。

    实测触发条件：用户在会话开头粘了一大段日志/attachment，第 1 行就超过 64KB 单行上限。
    这时 iter_head_records 一条都不 yield，上游若据此 `return None`，
    **这个会话就从列表里彻底消失了** —— 它明明是条正常会话。
    """
    return next(iter_head_records(path, max_bytes=max_bytes), None) is None


def iter_head_records(
    path: Path | str,
    *,
    max_bytes: int = DEFAULT_HEAD_BYTES,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
) -> Iterator[dict]:
    """逐条 yield 头部的 JSON 对象。

    只 yield dict —— jsonl 里出现裸数组/字符串/数字都当脏数据跳过，
    这样调用方可以直接 `.get()` 而不用每次判类型。
    """
    raw = read_head_bytes(path, max_bytes)
    if not raw:
        return

    lines = raw.split(b"\n")
    # 最后一段几乎必然被 max_bytes 截断成半行；只有当整个文件都读完了才保留它。
    if len(raw) >= max_bytes and len(lines) > 1:
        lines = lines[:-1]

    for line in lines:
        if not line or len(line) > max_line_bytes:
            continue
        record = loads_or_none(line)
        if isinstance(record, dict):
            yield record


def iter_tail_records(
    path: Path | str,
    *,
    max_bytes: int = DEFAULT_TAIL_BYTES,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
) -> Iterator[dict]:
    """逐条 yield **尾部**的 JSON 对象。

    为什么需要尾部：有些信息是「会话进行中才产生」的，只读头部必然漏掉。
    典型的是 `custom-title`（用户改名）—— 实测 14 个命名会话里有 4 个
    改名发生在头部窗口之外，其中就包括用户找不到的那条 ⟨wsl⟩。

    窗口大小的依据（实测）：最后一条 `custom-title` **距文件尾最远 18KB**，
    64KB 就能覆盖全部 14 个；这里取 128KB 留一倍余量。
    """
    seek_landed_on_line_start = False
    try:
        size = os.path.getsize(path)  # noqa: PTH202 - 热路径，见 model.py 同样的取舍
        with open(path, "rb") as fh:  # noqa: PTH123
            if size > max_bytes:
                offset = size - max_bytes
                # 多读 1 字节来判断 offset 是不是正好压在行首。
                # 不判的话「offset 前一字节是 \n」这种情况下 lines[0] 是**完整**记录，
                # 却会被当成半行砍掉 —— 实测能因此丢掉一条 custom-title。
                fh.seek(max(0, offset - 1))
                probe = fh.read(1)
                seek_landed_on_line_start = probe == b"\n" or offset == 0
                raw = fh.read(max_bytes)
            else:
                raw = fh.read()
    except OSError:
        return

    lines = raw.split(b"\n")
    if size > max_bytes and not seek_landed_on_line_start and len(lines) > 1:
        lines = lines[1:]  # 只有确实切在行中间时才丢首段

    for line in lines:
        if not line or len(line) > max_line_bytes:
            continue
        record = loads_or_none(line)
        if isinstance(record, dict):
            yield record


def loads_or_none(line: bytes | str) -> object | None:
    """一条脏行不该让整个侧栏挂掉 —— 解析失败返回 None。"""
    try:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        return json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None


def iter_all_records(
    path: Path | str,
    *,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
) -> Iterator[dict]:
    """流式读整个文件（不做字节上限），给小文件用 —— 例如 Codex 的 session_index.jsonl。

    仍然带单行上限，且逐行流式读，不会把整个文件塞进内存。
    """
    try:
        with open(path, "rb") as fh:  # noqa: PTH123 — 冷启动要开 1500+ 个文件，不为风格多建 Path
            for line in fh:
                line = line.rstrip(b"\n")
                if not line or len(line) > max_line_bytes:
                    continue
                record = loads_or_none(line)
                if isinstance(record, dict):
                    yield record
    except OSError:
        return
