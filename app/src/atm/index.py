"""统一索引：把 Claude 和 Codex 的会话合成一份按时间倒序的列表。

这一层是整个项目里唯一「别人没做全」的部分（见 ../../notes/survey-existing-tools.md 第四节），
也是唯一在 tmux / daemon / GUI 任何一条路线下都能复用的部分。
所以它不 import 任何 tmux 相关的东西 —— 保持可以单独搬走。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from . import cache as cache_mod
from .cache import CachedEntry, IndexCache
from .model import FileRef, IndexStats, SessionEntry, SessionIndex, Source
from .sources import claude, codex, pi

# ---------------------------------------------------------------- 噪音会话
#
# 有些「会话」不是人跟 AI 的对话，而是**某个脚本/插件把内容喂给 CLI** 产生的一次性调用。
# 它们在列表里毫无意义，但数量能压过真实会话 —— 实测本机 201 条里有 95 条是这种（47%）。
#
# 判据只用**标题前缀**，因为这些提示词是固定文本；刻意不用「体积/时间/目录」之类的启发式，
# 那些会误杀真实会话。每一条都附实测条数，将来失效时能看出来。
_NOISE_TITLE_PREFIXES = (
    # 91 条。ECC 插件时期把会话日志喂给 claude 做分析产生的，全部无名字、78~234KB。
    "Below is a conversation log from a Claude Code",
    # ⚠️ 曾经在这里挡过 "AGENTS.md instructions for " —— **已删除，那是误杀**。
    # 它根本不是「机器生成的调用」，而是 Codex 在真人对话前注入的 AGENTS.md 包装，
    # 因为标题提取没剥掉它才浮成了标题。实测被它藏掉的一条 625KB 会话里
    # 有 4 条真人消息。正确的修法在 text.py 的 _HEADING_BEFORE_TAG：
    # 剥掉「Markdown 标题行 + <INSTRUCTIONS> 块」这种形状，让标题落到真正的首条提问上。
    #
    # 教训：**标题字符串可以用来展示，不能用来判断会话来源**。
    # 标题是推断出来的，推断错了就会把真实对话判成噪音 —— 而噪音规则是静默的，
    # 用户根本不知道自己少了一条。
)


def is_noise(entry: SessionEntry) -> bool:
    """这条是不是「机器生成的一次性调用」而不是真实对话。

    **用户手动命名过的会话永远不算噪音** —— 取过名字就说明他在乎它，
    这条兜底比任何前缀规则都可靠。
    """
    if entry.name:
        return False
    return entry.title.startswith(_NOISE_TITLE_PREFIXES)


@dataclass(frozen=True, slots=True)
class SourceRoots:
    """数据源根目录。测试里可以整组替换掉，不碰真实的 ~/.claude 和 ~/.codex。"""

    claude_root: Path | None = None
    codex_root: Path | None = None
    codex_legacy_index: Path | None = None
    pi_root: Path | None = None


def build(
    *,
    roots: SourceRoots | None = None,
    use_cache: bool = True,
    cache_path: Path | None = None,
    sources: Iterable[Source] | None = None,
) -> SessionIndex:
    """扫描 + 解析 + 排序，返回不可变的 SessionIndex。

    `use_cache=False` 表示强制重新解析（`atm index --rebuild`），但仍然会写回缓存。
    """
    started = time.perf_counter()
    roots = roots or SourceRoots()
    wanted = set(sources) if sources is not None else set(Source)

    # 无论用不用缓存都要读它：`use_cache=False` 只表示「本轮不吃命中」，
    # 不表示「可以把别的来源的缓存也扔掉」。
    on_disk = cache_mod.load(cache_path)
    old_cache = on_disk if use_cache else IndexCache.empty()

    legacy_titles = (
        codex.load_legacy_titles(roots.codex_legacy_index) if Source.CODEX in wanted else {}
    )

    entries: list[SessionEntry] = []

    # ⚠️ 只清理**本轮真正扫过的来源**，没扫的来源原样留着。
    # 之前是从空 dict 开始、扫完直接整体覆盖缓存，于是
    # `atm list --source claude` 会把 Codex 那 75 条缓存全抹掉（实测），
    # 下一次全量命令又得重新解析 75 个文件。
    fresh: dict[str, CachedEntry] = {
        path: cached
        for path, cached in on_disk.entries.items()
        # entry 可能是 None（负结果：这个文件解析不出会话）。
        # 负结果没有 source，用路径归属判断它属于哪个来源。
        if _cached_source(path, cached) not in wanted
    }

    from_cache = 0
    parsed = 0
    skipped = 0

    def ingest(refs: Iterable[FileRef], parse: Callable[[FileRef], SessionEntry | None]) -> None:
        nonlocal from_cache, parsed, skipped
        for ref in refs:
            hit = old_cache.lookup(ref.path, ref.fingerprint)
            if hit is not None:
                fresh[ref.path] = hit
                from_cache += 1
                if hit.entry is not None:
                    entries.append(hit.entry)
                else:
                    skipped += 1  # 负结果也是命中，不用重读
                continue

            entry = _safe_parse(ref, parse)
            # ⚠️ 负结果**也要写进缓存**。之前 `entry is None` 直接 continue，
            # 于是「这个文件解析不出会话」这个结论永远不被记住，
            # 每次新进程都要把它重读重解析 —— 实测 14 个 codex 子 agent 文件每次都白读。
            fresh[ref.path] = CachedEntry(fingerprint=ref.fingerprint, entry=entry)
            if entry is None:
                skipped += 1
                continue
            entries.append(entry)
            parsed += 1

    if Source.CLAUDE in wanted:
        ingest(claude.discover(roots.claude_root), claude.parse)
    if Source.CODEX in wanted:
        ingest(codex.discover(roots.codex_root), lambda ref: codex.parse(ref, legacy_titles))
    if Source.PI in wanted:
        ingest(pi.discover(roots.pi_root), pi.parse)

    # 内容没变就别写盘。热路径下 100% 命中是常态，无脑重写会白白产生
    # 140KB 写入 + 一次 fsync —— 在 WSL 上这些字节最终要落到宿主的虚拟磁盘。
    cache_written = True
    if fresh != on_disk.entries:
        # 写盘失败要能看见。之前返回值被直接丢掉，缓存目录只读时每次都退化成
        # 全量解析而**没有任何提示**，用户只会觉得「怎么一直这么慢」。
        cache_written = cache_mod.save(IndexCache(entries=fresh), cache_path)

    entries = _dedupe(entries)
    entries.sort(key=lambda e: e.updated_at, reverse=True)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    return SessionIndex(
        entries=tuple(entries),
        stats=IndexStats(
            total=len(entries),
            from_cache=from_cache,
            parsed=parsed,
            skipped=skipped,
            elapsed_ms=elapsed_ms,
            cache_written=cache_written,
        ),
    )


def _cached_source(path: str, cached: CachedEntry) -> Source | None:
    """一条缓存记录属于哪个来源。负结果没有 entry，只能靠路径判断。"""
    if cached.entry is not None:
        return cached.entry.source
    if "/.codex/" in path:
        return Source.CODEX
    if "/.pi/" in path:
        return Source.PI
    if "/.claude/" in path:
        return Source.CLAUDE
    return None


def _dedupe(entries: list[SessionEntry]) -> list[SessionEntry]:
    """按 (source, id) 去重，保留最新的那条。

    同一个 id 出现两次会让 `atm resume <前缀>` 走进「匹配到多条，请给更长的前缀」——
    而前缀再长也分不开，因为两条的 id 完全一样，用户被卡死在一个无解的分支里。
    成因：备份/复制过项目目录，或同一会话被记录在两个路径下。
    """
    best: dict[tuple[Source, str], SessionEntry] = {}
    for entry in entries:
        key = (entry.source, entry.id)
        current = best.get(key)
        if current is None or entry.updated_at > current.updated_at:
            best[key] = entry
    return list(best.values())


def _safe_parse(
    ref: FileRef, parse: Callable[[FileRef], SessionEntry | None]
) -> SessionEntry | None:
    """一个文件解析炸了不能让整个索引挂掉 —— 格式假设本来就是逆向出来的。"""
    try:
        return parse(ref)
    except Exception:
        return None


@dataclass(frozen=True, slots=True)
class FilterResult:
    """过滤结果。**带上被隐藏的条数** —— 默默丢掉一半列表会让人以为「就这么多」。"""

    entries: tuple[SessionEntry, ...]
    hidden_noise: int


def filter_entries(
    entries: Iterable[SessionEntry],
    *,
    source: Source | None = None,
    cwd_prefix: str | None = None,
    limit: int | None = None,
    include_noise: bool = False,
) -> tuple[SessionEntry, ...]:
    """按来源 / 工作目录前缀过滤，默认剔除噪音会话。返回新元组，不改入参。"""
    return filter_with_stats(
        entries,
        source=source,
        cwd_prefix=cwd_prefix,
        limit=limit,
        include_noise=include_noise,
    ).entries


def filter_with_stats(
    entries: Iterable[SessionEntry],
    *,
    source: Source | None = None,
    cwd_prefix: str | None = None,
    limit: int | None = None,
    include_noise: bool = False,
) -> FilterResult:
    """同 `filter_entries`，但把「隐藏了多少条噪音」也返回出来。"""
    result = list(entries)
    # ⚠️ 先做 source / cwd 过滤，最后才统计噪音 ——
    # 否则 `atm list --here` 会报「已隐藏 92 条」，而当前作用域内一条噪音都没有。
    if source is not None:
        result = [e for e in result if e.source is source]
    if cwd_prefix:
        prefix = str(Path(cwd_prefix).expanduser())
        result = [e for e in result if e.cwd == prefix or e.cwd.startswith(prefix + "/")]
    hidden = 0
    if not include_noise:
        before = len(result)
        result = [e for e in result if not is_noise(e)]
        hidden = before - len(result)
    if limit is not None and limit >= 0:
        result = result[:limit]
    return FilterResult(entries=tuple(result), hidden_noise=hidden)
