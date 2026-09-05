"""索引 + 缓存。

缓存是「popup 瞬开」的全部依赖，所以命中/失效/损坏三条路径都要有测试。
"""

from __future__ import annotations

import json
import os
from datetime import UTC
from pathlib import Path

from atm import cache as cache_mod
from atm import index as index_mod
from atm.cache import CachedEntry, IndexCache
from atm.index import SourceRoots
from atm.model import Source

from .conftest import write_jsonl


def _roots(claude_root: Path, codex_root: Path, tmp_path: Path) -> SourceRoots:
    return SourceRoots(
        claude_root=claude_root,
        codex_root=codex_root,
        codex_legacy_index=tmp_path / "no-legacy.jsonl",
    )


def test_build_merges_both_sources_newest_first(
    claude_session: Path, claude_root: Path, codex_root: Path, tmp_path: Path
) -> None:
    codex_file = write_jsonl(
        codex_root
        / "2026/08/12"
        / "rollout-2026-08-12T01-46-32-abcdabcd-1111-2222-3333-444444444444.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {"session_id": "abcdabcd-1111-2222-3333-444444444444", "cwd": "/tmp/x"},
            }
        ],
    )
    # 让 codex 那条明确更新
    os.utime(claude_session, (1_700_000_000, 1_700_000_000))
    os.utime(codex_file, (1_800_000_000, 1_800_000_000))

    result = index_mod.build(
        roots=_roots(claude_root, codex_root, tmp_path),
        cache_path=tmp_path / "cache.json",
    )

    assert result.stats.total == 2
    assert [e.source for e in result.entries] == [Source.CODEX, Source.CLAUDE]
    assert result.stats.parsed == 2
    assert result.stats.from_cache == 0


def test_second_build_hits_cache(
    claude_session: Path, claude_root: Path, codex_root: Path, tmp_path: Path
) -> None:
    cache_path = tmp_path / "cache.json"
    roots = _roots(claude_root, codex_root, tmp_path)

    first = index_mod.build(roots=roots, cache_path=cache_path)
    second = index_mod.build(roots=roots, cache_path=cache_path)

    assert first.stats.parsed == 1
    assert second.stats.parsed == 0
    assert second.stats.from_cache == 1
    assert first.entries[0].title == second.entries[0].title


def test_cache_invalidated_when_file_changes(
    claude_session: Path, claude_root: Path, codex_root: Path, tmp_path: Path
) -> None:
    cache_path = tmp_path / "cache.json"
    roots = _roots(claude_root, codex_root, tmp_path)
    index_mod.build(roots=roots, cache_path=cache_path)

    write_jsonl(
        claude_session,
        [{"type": "user", "cwd": "/home/sean/IdeaProjects/demo", "message": {"content": "改过了"}}],
    )

    result = index_mod.build(roots=roots, cache_path=cache_path)
    assert result.stats.parsed == 1
    assert result.stats.from_cache == 0
    assert result.entries[0].title == "改过了"


def test_no_cache_forces_reparse(
    claude_session: Path, claude_root: Path, codex_root: Path, tmp_path: Path
) -> None:
    cache_path = tmp_path / "cache.json"
    roots = _roots(claude_root, codex_root, tmp_path)
    index_mod.build(roots=roots, cache_path=cache_path)

    result = index_mod.build(roots=roots, cache_path=cache_path, use_cache=False)
    assert result.stats.from_cache == 0
    assert result.stats.parsed == 1


def test_deleted_sessions_drop_out_of_cache(
    claude_session: Path, claude_root: Path, codex_root: Path, tmp_path: Path
) -> None:
    """删掉的会话不能永远留在缓存里。"""
    cache_path = tmp_path / "cache.json"
    roots = _roots(claude_root, codex_root, tmp_path)
    index_mod.build(roots=roots, cache_path=cache_path)

    claude_session.unlink()
    result = index_mod.build(roots=roots, cache_path=cache_path)

    assert result.stats.total == 0
    assert cache_mod.load(cache_path).entries == {}


def test_corrupt_cache_degrades_to_empty(tmp_path: Path) -> None:
    """缓存坏了最多是慢一次，不该报错。"""
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("{ this is not json", encoding="utf-8")
    assert cache_mod.load(cache_path).entries == {}


def test_cache_version_mismatch_ignored(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({"version": 999, "entries": {}}), encoding="utf-8")
    assert cache_mod.load(cache_path).entries == {}


def test_cache_roundtrip_preserves_entry(
    claude_session: Path, claude_root: Path, codex_root: Path, tmp_path: Path
) -> None:
    cache_path = tmp_path / "cache.json"
    roots = _roots(claude_root, codex_root, tmp_path)
    built = index_mod.build(roots=roots, cache_path=cache_path)

    loaded = cache_mod.load(cache_path)
    original = built.entries[0]
    restored = loaded.entries[original.path].entry
    assert restored == original


def test_cache_save_is_atomic_and_leaves_no_temp(tmp_path: Path) -> None:
    from datetime import datetime

    from atm.model import SessionEntry

    cache_path = tmp_path / "sub" / "cache.json"
    entry = SessionEntry(
        id="x",
        title="t",
        source=Source.CLAUDE,
        cwd="/tmp",
        git_branch=None,
        updated_at=datetime.now(tz=UTC),
        path="/tmp/x.jsonl",
        size_bytes=1,
    )
    cache = IndexCache(entries={"/tmp/x.jsonl": CachedEntry("1:1", entry)})

    assert cache_mod.save(cache, cache_path) is True
    assert cache_path.exists()
    assert list(cache_path.parent.glob("*.tmp")) == []


def test_filter_entries_by_cwd_prefix_respects_boundaries(
    claude_root: Path, codex_root: Path, tmp_path: Path
) -> None:
    """/home/sean/demo 不该匹配到 /home/sean/demo-other。"""
    write_jsonl(
        claude_root / "-a" / "1111.jsonl",
        [{"type": "user", "cwd": "/home/sean/demo", "message": {"content": "in"}}],
    )
    write_jsonl(
        claude_root / "-b" / "2222.jsonl",
        [{"type": "user", "cwd": "/home/sean/demo-other", "message": {"content": "out"}}],
    )
    write_jsonl(
        claude_root / "-c" / "3333.jsonl",
        [{"type": "user", "cwd": "/home/sean/demo/sub", "message": {"content": "nested"}}],
    )

    result = index_mod.build(
        roots=_roots(claude_root, codex_root, tmp_path), cache_path=tmp_path / "c.json"
    )
    filtered = index_mod.filter_entries(result.entries, cwd_prefix="/home/sean/demo")
    titles = {e.title for e in filtered}
    assert titles == {"in", "nested"}


def test_filter_entries_by_source(
    claude_session: Path, claude_root: Path, codex_root: Path, tmp_path: Path
) -> None:
    result = index_mod.build(
        roots=_roots(claude_root, codex_root, tmp_path), cache_path=tmp_path / "c.json"
    )
    assert len(index_mod.filter_entries(result.entries, source=Source.CODEX)) == 0
    assert len(index_mod.filter_entries(result.entries, source=Source.CLAUDE)) == 1


def test_build_survives_unreadable_file(
    claude_root: Path, codex_root: Path, tmp_path: Path
) -> None:
    """一个文件读不了不能让整个索引挂掉。"""
    good = write_jsonl(
        claude_root / "-a" / "1111.jsonl",
        [{"type": "user", "cwd": "/home/sean/a", "message": {"content": "ok"}}],
    )
    bad = write_jsonl(claude_root / "-b" / "2222.jsonl", [{"type": "user"}])
    bad.chmod(0o000)
    try:
        result = index_mod.build(
            roots=_roots(claude_root, codex_root, tmp_path), cache_path=tmp_path / "c.json"
        )
        assert any(e.path == str(good) for e in result.entries)
    finally:
        bad.chmod(0o644)


# ------------------------------------------------- 噪音会话过滤（实测回归）


def _entry(title: str, name: str | None = None):
    from datetime import datetime

    from atm.model import SessionEntry

    return SessionEntry(
        id=title,
        title=title,
        source=Source.CLAUDE,
        cwd="/tmp",
        git_branch=None,
        updated_at=datetime.now(tz=UTC),
        path=f"/tmp/{title}.jsonl",
        size_bytes=1,
        name=name,
    )


def test_machine_generated_calls_are_noise() -> None:
    """实测本机 201 条里 91 条是这种机器生成的一次性调用。"""
    assert index_mod.is_noise(_entry("Below is a conversation log from a Claude Code session."))


def test_agents_md_injection_is_not_noise() -> None:
    """曾经把 `AGENTS.md instructions for …` 当噪音 —— 那是误杀，本测试锁住不许回退。

    它不是机器生成的调用，是 Codex 在**真人对话**前注入的 AGENTS.md 包装；
    标题提取没剥掉它才浮成标题。实测被它藏掉的一条 625KB 会话里有 4 条真人消息。
    真正的修法在 text.py 的 _HEADING_BEFORE_TAG。
    """
    assert not index_mod.is_noise(_entry("AGENTS.md instructions for /home/sean/x"))


def test_real_prompts_are_not_noise() -> None:
    """用户重复问过的真提问也会重复出现，绝不能被当噪音杀掉。"""
    assert not index_mod.is_noise(_entry("请对 sample-service 的「任务列表梳理」模块做代码评审"))
    assert not index_mod.is_noise(_entry("你是独立评审者。这是一个中文项目"))
    assert not index_mod.is_noise(_entry("(untitled)"))


def test_named_session_is_never_noise() -> None:
    """取过名字就说明用户在乎它 —— 这条兜底优先于任何前缀规则。"""
    assert not index_mod.is_noise(
        _entry("Below is a conversation log from a Claude Code session.", name="重要")
    )


def test_noise_hidden_by_default_and_counted() -> None:
    entries = [_entry("真会话"), _entry("Below is a conversation log from a Claude Code x")]
    result = index_mod.filter_with_stats(entries)
    assert [e.title for e in result.entries] == ["真会话"]
    assert result.hidden_noise == 1


def test_include_noise_shows_everything() -> None:
    entries = [_entry("真会话"), _entry("Below is a conversation log from a Claude Code x")]
    result = index_mod.filter_with_stats(entries, include_noise=True)
    assert len(result.entries) == 2
    assert result.hidden_noise == 0


# ------------------------------------------------- 缓存一致性回归


def test_source_filtered_build_preserves_other_source_cache(
    claude_session: Path, claude_root: Path, codex_root: Path, tmp_path: Path
) -> None:
    """`--source claude` 不能把 Codex 那半边的缓存抹掉。

    实测（修复前）：全量 202 条 → 跑一次 --source claude → 缓存只剩 127 条，
    下次全量要重新解析 75 个文件。
    """
    write_jsonl(
        codex_root / "2026/08/12" / "rollout-2026-08-12T01-00-00-cccccccc-1-2-3-4.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "cccccccc-0000-0000-0000-000000000003",
                    "thread_source": "user",
                    "cwd": "/tmp/x",
                },
            }
        ],
    )
    cache_path = tmp_path / "cache.json"
    roots = _roots(claude_root, codex_root, tmp_path)

    index_mod.build(roots=roots, cache_path=cache_path)
    both = len(cache_mod.load(cache_path).entries)
    assert both == 2

    index_mod.build(roots=roots, cache_path=cache_path, sources={Source.CLAUDE})
    after = cache_mod.load(cache_path).entries
    assert len(after) == 2, "Codex 那条缓存被抹掉了"
    assert any(c.entry.source is Source.CODEX for c in after.values())

    # 再全量：应当全部命中，不需要重新解析
    again = index_mod.build(roots=roots, cache_path=cache_path)
    assert again.stats.parsed == 0
    assert again.stats.from_cache == 2


def test_cache_not_rewritten_when_nothing_changed(
    claude_session: Path, claude_root: Path, codex_root: Path, tmp_path: Path
) -> None:
    """100% 命中时不该重写盘 —— 那是白白的 140KB 写入 + fsync。"""
    cache_path = tmp_path / "cache.json"
    roots = _roots(claude_root, codex_root, tmp_path)
    index_mod.build(roots=roots, cache_path=cache_path)

    before = cache_path.stat().st_mtime_ns
    index_mod.build(roots=roots, cache_path=cache_path)
    assert cache_path.stat().st_mtime_ns == before, "内容没变却重写了缓存"


def test_cache_still_written_when_content_changes(
    claude_session: Path, claude_root: Path, codex_root: Path, tmp_path: Path
) -> None:
    cache_path = tmp_path / "cache.json"
    roots = _roots(claude_root, codex_root, tmp_path)
    index_mod.build(roots=roots, cache_path=cache_path)

    write_jsonl(
        claude_session,
        [{"type": "user", "cwd": "/home/sean/x", "message": {"content": "改了"}}],
    )
    index_mod.build(roots=roots, cache_path=cache_path)
    assert cache_mod.load(cache_path).entries[str(claude_session)].entry.title == "改了"


# ------------------------------------------------- 负结果缓存 / 去重 / 统计（实测回归）


def test_negative_parse_result_is_cached(
    claude_root: Path, codex_root: Path, tmp_path: Path
) -> None:
    """「这个文件解析不出会话」也要记住，否则每次新进程都重读它。

    实测：本机 14 个 codex 子 agent 文件之前每次热启动都被重新读一遍。
    """
    write_jsonl(
        codex_root / "2026/08/12" / "rollout-2026-08-12T01-00-00-dddd-1-2-3-4.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "dddddddd-0000-0000-0000-000000000004",
                    "thread_source": "subagent",  # 会让 parse 返回 None
                    "parent_thread_id": "ffff",
                    "cwd": "/tmp",
                },
            }
        ],
    )
    cache_path = tmp_path / "cache.json"
    roots = _roots(claude_root, codex_root, tmp_path)

    first = index_mod.build(roots=roots, cache_path=cache_path)
    assert first.stats.skipped == 1

    second = index_mod.build(roots=roots, cache_path=cache_path)
    assert second.stats.skipped == 1
    assert second.stats.parsed == 0, "负结果没被缓存，又重新解析了一遍"
    assert second.stats.from_cache >= 1


def test_entries_deduped_by_source_and_id(
    claude_root: Path, codex_root: Path, tmp_path: Path
) -> None:
    """同 id 出现两次会让 `atm resume <前缀>` 卡在无解的「请给更长的前缀」。"""
    for d in ("-a", "-b"):
        write_jsonl(
            claude_root / d / "same-0000-0000-0000-000000000000.jsonl",
            [{"type": "user", "sessionId": "same-id", "cwd": "/tmp", "message": {"content": "x"}}],
        )
    result = index_mod.build(
        roots=_roots(claude_root, codex_root, tmp_path), cache_path=tmp_path / "c.json"
    )
    ids = [e.id for e in result.entries]
    assert len(ids) == len(set(ids)), f"存在重复 id: {ids}"


def test_hidden_noise_counted_within_scope(
    claude_root: Path, codex_root: Path, tmp_path: Path
) -> None:
    """`--cwd X` 时不该报全量的噪音数 —— 之前作用域内 0 条噪音却报「隐藏 92 条」。"""
    write_jsonl(
        claude_root / "-a" / "1111.jsonl",
        [{"type": "user", "cwd": "/tmp/scope-a", "message": {"content": "真会话"}}],
    )
    write_jsonl(
        claude_root / "-b" / "2222.jsonl",
        [
            {
                "type": "user",
                "cwd": "/tmp/scope-b",
                "message": {"content": "Below is a conversation log from a Claude Code x"},
            }
        ],
    )
    result = index_mod.build(
        roots=_roots(claude_root, codex_root, tmp_path), cache_path=tmp_path / "c.json"
    )
    scoped = index_mod.filter_with_stats(result.entries, cwd_prefix="/tmp/scope-a")
    assert len(scoped.entries) == 1
    assert scoped.hidden_noise == 0, "作用域里没有噪音，不该报隐藏条数"


def test_cache_write_failure_is_visible(
    claude_session: Path, claude_root: Path, codex_root: Path, tmp_path: Path
) -> None:
    """缓存写不进去必须让用户看得见，否则只表现为「怎么一直这么慢」。"""
    ro = tmp_path / "readonly"
    ro.mkdir()
    ro.chmod(0o555)
    try:
        result = index_mod.build(
            roots=_roots(claude_root, codex_root, tmp_path), cache_path=ro / "c.json"
        )
        assert result.stats.cache_written is False
        assert "缓存写入失败" in result.stats.describe()
    finally:
        ro.chmod(0o755)
