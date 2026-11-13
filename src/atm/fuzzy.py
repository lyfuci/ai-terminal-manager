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


def _origin_map(text: str) -> tuple[int, ...]:
    """小写化之后，每个下标对应**原串**的哪个下标。

    `str.lower()` **不保证长度不变**：U+0130（İ，土耳其大写 I 带点）小写化成两个码点
    （'i' + U+0307）。整个 Unicode 范围里只有这一个码点会这样，但它出现在土耳其语
    人名、路径、会话标题里并不罕见。

    两串长度一旦不同，小写串的下标就不能拿去索引原串：命中位置会整体错位
    （高亮到别的字上），词首判定读错字符，两个这样的字符之后还会直接越界。

    绝大多数文本小写化后长度不变，那种情况根本用不到这张表（见 `score`）。
    """
    origin: list[int] = []
    for index, ch in enumerate(text):
        origin.extend([index] * len(ch.lower()))
    return tuple(origin)


def score(needle: str, haystack: str) -> Match | None:
    """子序列匹配。匹配不上返回 None。空查询算满分匹配（列表全显示）。"""
    if not needle:
        return Match(score=0, positions=())
    if not haystack:
        return None

    case_sensitive = any(ch.isupper() for ch in needle)
    if case_sensitive:
        hay = haystack
        origin: tuple[int, ...] | None = None  # 折叠前后一一对应，下标天然对齐
        pin = needle
    else:
        hay = haystack.lower()
        # 长度一致时下标天然对齐，不用建映射表（绝大多数情况）。
        origin = None if len(hay) == len(haystack) else _origin_map(haystack)
        pin = needle.lower()

    positions: list[int] = []
    total = 0
    hay_index = 0
    previous_hit = -2

    for ch in pin:
        found = hay.find(ch, hay_index)
        if found == -1:
            return None
        # 命中位置换算回**原串**的下标：折叠可能改变长度，两串的下标不再一一对应。
        at = found if origin is None else origin[found]

        if at == previous_hit + 1:
            total += _BONUS_CONSECUTIVE
        elif previous_hit >= 0:
            gap = at - previous_hit - 1
            total -= min(_MAX_GAP_PENALTY_PER_HOLE, gap * _PENALTY_GAP)

        if at == 0 or haystack[at - 1] in _WORD_BOUNDARY:
            total += _BONUS_WORD_START
        if not case_sensitive and haystack[at] == ch:
            total += _BONUS_EXACT_CASE

        positions.append(at)
        previous_hit = at
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
