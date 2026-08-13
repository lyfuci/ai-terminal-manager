"""模糊匹配。

本机没装 fzf（实测），而 atm 的价值就在「唤出来就能搜」，
所以不能把 fzf 当硬依赖 —— 这里自带一个够用的子序列匹配器。

打分偏好（从高到低）：连续命中 > 词首命中 > 大小写完全一致 > 靠前命中。
smart case：查询里有大写就大小写敏感，全小写就不敏感。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .model import SessionEntry

_BONUS_CONSECUTIVE = 8
_BONUS_WORD_START = 10
_BONUS_EXACT_CASE = 2
_PENALTY_LEADING = 1  # 首个命中位置每往后一格扣一点
_MAX_LEADING_PENALTY = 20

# 命中之间的空隙要扣分。没有这一项的话 "c a c h e" 会赢过 "cache"：
# 前者每个字母都在词首、能拿满 5 次词首奖励，反而盖过真正连续的匹配。
_PENALTY_GAP = 3
_MAX_GAP_PENALTY_PER_HOLE = 12

_WORD_BOUNDARY = set(" \t/\\-_.:,()[]{}")


@dataclass(frozen=True, slots=True)
class Match:
    """一次匹配的结果。positions 是命中字符在 haystack 里的下标，用来在 TUI 里高亮。"""

    score: int
    positions: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ScoredEntry:
    entry: SessionEntry
    match: Match


def score(needle: str, haystack: str) -> Match | None:
    """子序列匹配。匹配不上返回 None。空查询算满分匹配（列表全显示）。"""
    if not needle:
        return Match(score=0, positions=())
    if not haystack:
        return None

    case_sensitive = any(ch.isupper() for ch in needle)
    hay = haystack if case_sensitive else haystack.lower()
    pin = needle if case_sensitive else needle.lower()

    positions: list[int] = []
    total = 0
    hay_index = 0
    previous_hit = -2

    for ch in pin:
        found = hay.find(ch, hay_index)
        if found == -1:
            return None

        if found == previous_hit + 1:
            total += _BONUS_CONSECUTIVE
        elif previous_hit >= 0:
            gap = found - previous_hit - 1
            total -= min(_MAX_GAP_PENALTY_PER_HOLE, gap * _PENALTY_GAP)

        if found == 0 or haystack[found - 1] in _WORD_BOUNDARY:
            total += _BONUS_WORD_START
        if not case_sensitive and found < len(haystack) and haystack[found] == ch:
            total += _BONUS_EXACT_CASE

        positions.append(found)
        previous_hit = found
        hay_index = found + 1

    total -= min(_MAX_LEADING_PENALTY, positions[0] * _PENALTY_LEADING)
    return Match(score=total, positions=tuple(positions))


def rank(entries: Iterable[SessionEntry], query: str) -> tuple[ScoredEntry, ...]:
    """过滤 + 排序。

    空查询保持索引原本的时间倒序；有查询时按分数降序，
    同分再按 updated_at 降序 —— 「同样匹配的，最近用过的排前面」。
    """
    query = query.strip()
    if not query:
        return tuple(ScoredEntry(entry=e, match=Match(0, ())) for e in entries)

    scored: list[ScoredEntry] = []
    for entry in entries:
        match = score(query, entry.haystack())
        if match is not None:
            scored.append(ScoredEntry(entry=entry, match=match))

    scored.sort(key=lambda s: (-s.match.score, -s.entry.updated_at.timestamp()))
    return tuple(scored)


def highlight_positions(entry: SessionEntry, match: Match) -> frozenset[int]:
    """haystack 的命中下标里，落在标题范围内的那些（TUI 只高亮标题列）。"""
    title_len = len(entry.title)
    return frozenset(p for p in match.positions if p < title_len)
