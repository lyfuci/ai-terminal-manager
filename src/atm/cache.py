"""索引缓存。

冷启动要扫 1459 个 Claude 文件 + 86 个 Codex 文件；就算只读头部也要秒级。
tmux popup 里的选择器必须是「瞬开」的，所以解析结果按 (path → fingerprint + entry) 缓存，
下次只解析 fingerprint 变了的文件。

写入必须是原子的：popup 可能被随手 Esc 掉，半截的缓存文件会让下一次启动直接报错。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .model import SessionEntry

CACHE_VERSION = 3  # v3: 缓存可以记住「解析不出会话」的负结果


def default_cache_path() -> Path:
    """遵守 XDG；没设就用 ~/.cache。"""
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "atm" / f"index-v{CACHE_VERSION}.json"


@dataclass(frozen=True, slots=True)
class CachedEntry:
    """一个文件的解析结果。

    `entry is None` 表示**这个文件解析不出会话**（例如 Codex 的子 agent rollout）。
    这个「负结果」必须也缓存起来 —— 否则每次新进程都要把它重读重解析一遍。
    实测本机有 14 个这样的文件，每次热启动白读它们的头部。
    """

    fingerprint: str
    entry: SessionEntry | None


@dataclass(frozen=True, slots=True)
class IndexCache:
    """path → (fingerprint, entry)。不可变：更新用 `with_entries` 生成新对象。"""

    entries: dict[str, CachedEntry]

    @classmethod
    def empty(cls) -> IndexCache:
        return cls(entries={})

    def lookup(self, path: str, fingerprint: str) -> CachedEntry | None:
        """返回缓存条目本身（可能是负结果），指纹不匹配就返回 None。

        刻意不返回 `SessionEntry | None` —— 那样「没缓存」和「缓存了负结果」
        会长得一模一样，负结果就永远存不住。
        """
        cached = self.entries.get(path)
        if cached is None or cached.fingerprint != fingerprint:
            return None
        return cached

    def get(self, path: str, fingerprint: str) -> SessionEntry | None:
        cached = self.lookup(path, fingerprint)
        return cached.entry if cached else None

    def with_entries(self, updates: dict[str, CachedEntry]) -> IndexCache:
        merged = dict(self.entries)
        merged.update(updates)
        return IndexCache(entries=merged)


def load(path: Path | None = None) -> IndexCache:
    """读缓存。任何异常都退化成空缓存 —— 缓存坏了最多是慢一次，不该是报错。"""
    cache_path = path or default_cache_path()
    try:
        raw = cache_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return IndexCache.empty()

    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return IndexCache.empty()

    records = data.get("entries")
    if not isinstance(records, dict):
        return IndexCache.empty()

    entries: dict[str, CachedEntry] = {}
    for file_path, record in records.items():
        if not isinstance(record, dict):
            continue
        fingerprint = record.get("fingerprint")
        payload = record.get("entry")
        if not isinstance(fingerprint, str):
            continue
        if payload is None:  # 负结果：解析不出会话，但这个结论本身要记住
            entries[file_path] = CachedEntry(fingerprint=fingerprint, entry=None)
            continue
        if not isinstance(payload, dict):
            continue
        try:
            entries[file_path] = CachedEntry(
                fingerprint=fingerprint, entry=SessionEntry.from_json(payload)
            )
        except Exception:
            # 这里刻意兜底一切：docstring 承诺「缓存坏了最多是慢一次，不该是报错」。
            # 之前只 catch (KeyError, ValueError)，而 SessionEntry.from_json 会抛 TypeError
            # （datetime.fromisoformat(None) / int(None)），坏缓存会让每次启动直接崩。
            continue  # 单条坏了就丢这一条，重新解析它对应的文件
    return IndexCache(entries=entries)


def save(cache: IndexCache, path: Path | None = None) -> bool:
    """原子写。失败返回 False —— 缓存写不进去不该让命令失败。"""
    cache_path = path or default_cache_path()
    payload = {
        "version": CACHE_VERSION,
        "entries": {
            file_path: {
                "fingerprint": cached.fingerprint,
                "entry": cached.entry.to_json() if cached.entry is not None else None,
            }
            for file_path, cached in cache.entries.items()
        },
    }

    temp_name: str | None = None
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=cache_path.parent,
            prefix=cache_path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            # ⚠️ 先绑定名字再写。之前 temp_name 是 with 块最后一行，
            # 而真正会失败的动作（json.dump 遇 ENOSPC、fsync 遇 EIO）全在它前面 ——
            # 一失败 temp_name 根本没绑定，清理逻辑拿不到路径，.tmp 就在缓存目录里无限堆积。
            temp_name = handle.name
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temp_name).replace(cache_path)
        return True
    except Exception:
        # 不能只 catch OSError：会话标题里一个孤立代理字符（jsonl 里合法的 \udXXX 转义）
        # 就会让 json.dump 抛 UnicodeEncodeError（ValueError 子类）逃出去，
        # 整个 atm 调用直接崩掉。缓存写不进去只该降级，不该是致命错误。
        _cleanup(temp_name)
        return False


def clear(path: Path | None = None) -> bool:
    cache_path = path or default_cache_path()
    try:
        cache_path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _cleanup(temp_name: str | None) -> None:
    if isinstance(temp_name, str):
        with contextlib.suppress(OSError):
            Path(temp_name).unlink()
