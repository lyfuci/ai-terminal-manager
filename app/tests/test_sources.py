"""两个会话源的解析。

重点覆盖「格式假设会变」这件事：脏行、缺字段、超大行、空文件都不能让扫描挂掉。
"""

from __future__ import annotations

import json
from pathlib import Path

from atm.model import UNTITLED, FileRef, Source
from atm.sources import claude, codex

from .conftest import write_jsonl

# ---------------------------------------------------------------- claude


def test_claude_parse_extracts_title_cwd_branch(claude_session: Path) -> None:
    ref = FileRef.from_path(claude_session)
    assert ref is not None
    entry = claude.parse(ref)

    assert entry is not None
    assert entry.id == "aaaaaaaa-1111-2222-3333-444444444444"
    assert entry.title == "帮我把索引层的缓存加上 第二行会被折叠"  # 多行折叠成一行
    assert entry.cwd == "/home/sean/IdeaProjects/demo"
    assert entry.git_branch == "feature/atm"
    assert entry.source is Source.CLAUDE


def test_claude_ai_title_wins_over_first_user_message(claude_root: Path) -> None:
    """ai-title 是 CLI 自己生成的，质量比截首条消息高，优先级要更高。"""
    path = write_jsonl(
        claude_root / "-home-sean-demo" / "bbbbbbbb-0000-0000-0000-000000000000.jsonl",
        [
            {"type": "user", "cwd": "/home/sean/demo", "message": {"content": "随便问一句"}},
            {"type": "ai-title", "aiTitle": "Add cache layer to index", "sessionId": "bbbbbbbb"},
        ],
    )
    entry = claude.parse(FileRef.from_path(path))
    assert entry is not None
    assert entry.title == "Add cache layer to index"


def test_claude_skips_junk_prompts(claude_root: Path) -> None:
    """slash command 的包装和 system-reminder 都不该当标题。"""
    path = write_jsonl(
        claude_root / "-home-sean-demo" / "cccccccc-0000-0000-0000-000000000000.jsonl",
        [
            {"type": "user", "message": {"content": "<command-name>/effort</command-name>"}},
            {
                "type": "user",
                "message": {"content": "<system-reminder>ignore me</system-reminder>"},
            },
            {
                "type": "user",
                "cwd": "/home/sean/demo",
                "message": {"content": "真正的第一句话"},
            },
        ],
    )
    entry = claude.parse(FileRef.from_path(path))
    assert entry is not None
    assert entry.title == "真正的第一句话"


def test_claude_tolerates_corrupt_lines(claude_root: Path) -> None:
    """一条脏行不能让整个文件解析失败 —— 这是 CLAUDE.md 明确要求的。"""
    path = write_jsonl(
        claude_root / "-home-sean-demo" / "dddddddd-0000-0000-0000-000000000000.jsonl",
        [
            "{not json at all",
            "[1, 2, 3]",  # 合法 JSON 但不是 dict
            '{"type": "user", "cwd": "/home/sean/demo", "message": {"content": "活下来了"}}',
        ],
    )
    entry = claude.parse(FileRef.from_path(path))
    assert entry is not None
    assert entry.title == "活下来了"


def test_claude_falls_back_to_untitled_and_decoded_dir(claude_root: Path) -> None:
    """空会话也要出现在列表里 —— 有 6% 的文件就是这样。"""
    path = write_jsonl(
        claude_root / "-home-sean-IdeaProjects-demo" / "eeeeeeee-0000-0000-0000-000000000000.jsonl",
        [{"type": "assistant", "message": {"content": "no user message here"}}],
    )
    entry = claude.parse(FileRef.from_path(path))
    assert entry is not None
    assert entry.title == UNTITLED
    assert entry.cwd == "/home/sean/IdeaProjects/demo"  # 从目录名反解
    assert entry.id == "eeeeeeee-0000-0000-0000-000000000000"  # 从文件名兜底


def test_claude_ignores_oversized_lines(claude_root: Path) -> None:
    """attachment 之类的巨行要跳过，否则 648MB 的文件会拖死冷启动。"""
    huge = json.dumps({"type": "user", "message": {"content": "x" * 200_000}}, ensure_ascii=False)
    path = write_jsonl(
        claude_root / "-home-sean-demo" / "ffffffff-0000-0000-0000-000000000000.jsonl",
        [huge, {"type": "user", "cwd": "/home/sean/demo", "message": {"content": "小行赢"}}],
    )
    entry = claude.parse(FileRef.from_path(path))
    assert entry is not None
    assert entry.title == "小行赢"


def test_claude_discover_finds_sessions(claude_session: Path, claude_root: Path) -> None:
    refs = list(claude.discover(claude_root))
    assert [r.path for r in refs] == [str(claude_session)]


def test_claude_discover_on_missing_root(tmp_path: Path) -> None:
    assert list(claude.discover(tmp_path / "nope")) == []


def test_decode_project_dir_is_lossy_but_documented() -> None:
    assert claude.decode_project_dir("-home-sean-demo") == "/home/sean/demo"
    assert claude.decode_project_dir("relative") == "relative"


# ---------------------------------------------------------------- codex


def _codex_file(root: Path, session_id: str, *, extra: list[dict] | None = None) -> Path:
    return write_jsonl(
        root / "2026" / "08" / "12" / f"rollout-2026-08-12T01-46-32-{session_id}.jsonl",
        [
            {
                "timestamp": "2026-08-12T05:46:32.499Z",
                "type": "session_meta",
                "payload": {
                    "session_id": session_id,
                    "id": session_id,
                    "cwd": "/home/sean/IdeaProjects/sample-project",
                    "cli_version": "0.147.0",
                    "git": {"branch": "main"},
                },
            },
            *(extra or []),
        ],
    )


def test_codex_parse_reads_session_meta_and_first_user_message(codex_root: Path) -> None:
    path = _codex_file(
        codex_root,
        "00000000-0000-7000-8000-000000000002",
        extra=[
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "修复 recordId 入库重复"},
            }
        ],
    )
    entry = codex.parse(FileRef.from_path(path))
    assert entry is not None
    assert entry.source is Source.CODEX
    assert entry.id == "00000000-0000-7000-8000-000000000002"
    assert entry.title == "修复 recordId 入库重复"
    assert entry.cwd == "/home/sean/IdeaProjects/sample-project"
    assert entry.git_branch == "main"


def test_codex_handles_null_git(codex_root: Path) -> None:
    """实测：codex_exec 起的会话 session_meta.git 就是 null。"""
    path = write_jsonl(
        codex_root
        / "2026/08/12"
        / "rollout-2026-08-12T01-46-32-abcdabcd-1111-2222-3333-444444444444.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {
                    "session_id": "abcdabcd-1111-2222-3333-444444444444",
                    "cwd": "/tmp/x",
                    "git": None,
                },
            }
        ],
    )
    entry = codex.parse(FileRef.from_path(path))
    assert entry is not None
    assert entry.git_branch is None
    assert entry.title == UNTITLED


def test_codex_accepts_response_item_user_messages(codex_root: Path) -> None:
    """不同 codex 版本对 user 消息有两种写法，两种都要能认。"""
    path = _codex_file(
        codex_root,
        "11111111-1111-1111-1111-111111111111",
        extra=[
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "第二种写法"}],
                },
            }
        ],
    )
    entry = codex.parse(FileRef.from_path(path))
    assert entry is not None
    assert entry.title == "第二种写法"


def test_codex_legacy_index_title_wins(codex_root: Path, tmp_path: Path) -> None:
    """老 session_index.jsonl 的 thread_name 是人类可读标题，优先级最高。"""
    session_id = "00000000-0000-7000-8000-000000000001"
    path = _codex_file(
        codex_root,
        session_id,
        extra=[
            {"type": "event_msg", "payload": {"type": "user_message", "message": "很长的原始提问"}}
        ],
    )
    legacy = write_jsonl(
        tmp_path / "session_index.jsonl",
        [
            {
                "id": session_id,
                "thread_name": "修复文件下载校验",
                "updated_at": "2026-08-03T04:25:09Z",
            }
        ],
    )
    titles = codex.load_legacy_titles(legacy)
    entry = codex.parse(FileRef.from_path(path), titles)
    assert entry is not None
    assert entry.title == "修复文件下载校验"


def test_codex_legacy_index_missing_is_fine(tmp_path: Path) -> None:
    """老索引是可选增强，不是依赖。"""
    assert codex.load_legacy_titles(tmp_path / "nope.jsonl") == {}


def test_codex_id_from_filename_when_meta_missing(codex_root: Path) -> None:
    path = write_jsonl(
        codex_root
        / "2026/08/12"
        / "rollout-2026-08-12T01-46-32-99999999-8888-7777-6666-555555555555.jsonl",
        [{"type": "event_msg", "payload": {"type": "user_message", "message": "没有 meta"}}],
    )
    entry = codex.parse(FileRef.from_path(path))
    assert entry is not None
    assert entry.id == "99999999-8888-7777-6666-555555555555"


def test_codex_discover_walks_date_tree(codex_root: Path) -> None:
    _codex_file(codex_root, "22222222-2222-2222-2222-222222222222")
    refs = list(codex.discover(codex_root))
    assert len(refs) == 1


# ------------------------------------------------- 子 agent 排除 + 会话命名（实测回归）


def test_codex_skips_subagent_rollouts(codex_root: Path) -> None:
    """子 agent 的 rollout 里 session_id 记的是**父线程**的 id。

    实测：sample-crawler 下 7 个不同的文件因此塌成同一个 id，列表里出现 7 条一样的条目，
    resume 时还会撞车。子 agent 不能独立 resume，必须排除。
    """
    path = write_jsonl(
        codex_root / "2026/07/23" / "rollout-2026-07-23T22-42-23-aaaa-1-2-3-4.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "aaaaaaaa-0000-0000-0000-000000000001",
                    "session_id": "ffffffff-9999-9999-9999-999999999999",  # 父线程的
                    "parent_thread_id": "ffffffff-9999-9999-9999-999999999999",
                    "forked_from_id": "ffffffff-9999-9999-9999-999999999999",
                    "thread_source": "subagent",
                    "agent_nickname": "Kepler",
                    "cwd": "/home/sean/demo",
                },
            }
        ],
    )
    assert codex.parse(FileRef.from_path(path)) is None


def test_codex_parent_thread_uses_its_own_id(codex_root: Path) -> None:
    """父线程 thread_source=user，正常收录，且用自己的 id。"""
    path = write_jsonl(
        codex_root / "2026/07/23" / "rollout-2026-07-23T22-41-20-bbbb-1-2-3-4.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "bbbbbbbb-0000-0000-0000-000000000002",
                    "session_id": "bbbbbbbb-0000-0000-0000-000000000002",
                    "thread_source": "user",
                    "cwd": "/home/sean/demo",
                },
            },
            {"type": "event_msg", "payload": {"type": "user_message", "message": "父线程的提问"}},
        ],
    )
    entry = codex.parse(FileRef.from_path(path))
    assert entry is not None
    assert entry.id == "bbbbbbbb-0000-0000-0000-000000000002"
    assert entry.title == "父线程的提问"


def test_claude_reads_custom_title_as_name(claude_root: Path) -> None:
    """`custom-title` 是**用户手取的名字**，要和推断出来的 title 分开存。"""
    path = write_jsonl(
        claude_root / "-home-sean-demo" / "11112222-0000-0000-0000-000000000000.jsonl",
        [
            {"type": "custom-title", "customTitle": "sample-project", "sessionId": "11112222"},
            {"type": "user", "cwd": "/home/sean/demo", "message": {"content": "推断出来的标题"}},
        ],
    )
    entry = claude.parse(FileRef.from_path(path))
    assert entry is not None
    assert entry.name == "sample-project"
    assert entry.title == "推断出来的标题"  # 名字不覆盖标题，两者并存


def test_claude_name_is_none_when_unnamed(claude_session: Path) -> None:
    entry = claude.parse(FileRef.from_path(claude_session))
    assert entry is not None
    assert entry.name is None


def test_named_session_is_searchable_by_name(claude_root: Path) -> None:
    from atm.fuzzy import rank

    path = write_jsonl(
        claude_root / "-home-sean-demo" / "33334444-0000-0000-0000-000000000000.jsonl",
        [
            {"type": "custom-title", "customTitle": "sample-project"},
            {"type": "user", "cwd": "/home/sean/demo", "message": {"content": "完全无关的标题"}},
        ],
    )
    entry = claude.parse(FileRef.from_path(path))
    assert rank([entry], "sample-project"), "按名字应该能搜到"


def test_claude_finds_rename_beyond_head_window(claude_root: Path) -> None:
    """`custom-title` 是用户**改名那一刻**写的，可能远在头部窗口之外。

    实测：14 个命名会话里 4 个的改名在头部 256KB 之外，
    其中一条名叫 ⟨wsl⟩ 的用户在列表里怎么都找不到。
    """
    filler = [{"type": "assistant", "message": {"content": "x" * 900}} for _ in range(500)]
    path = write_jsonl(
        claude_root / "-home-sean-demo" / "77778888-0000-0000-0000-000000000000.jsonl",
        [
            {"type": "user", "cwd": "/home/sean/demo", "message": {"content": "起初没有名字"}},
            *filler,
            {"type": "custom-title", "customTitle": "wsl"},
        ],
    )
    assert path.stat().st_size > 300_000, "填充不够，没超出头部窗口"
    entry = claude.parse(FileRef.from_path(path))
    assert entry is not None
    assert entry.name == "wsl"


def test_claude_takes_latest_rename_not_first(claude_root: Path) -> None:
    """改过多次名要用**最后一次**。实测有一条头部读到旧名、最终是新名。"""
    path = write_jsonl(
        claude_root / "-home-sean-demo" / "99990000-0000-0000-0000-000000000000.jsonl",
        [
            {"type": "custom-title", "customTitle": "旧名字"},
            {"type": "user", "cwd": "/home/sean/demo", "message": {"content": "正文"}},
            {"type": "custom-title", "customTitle": "新名字"},
        ],
    )
    entry = claude.parse(FileRef.from_path(path))
    assert entry is not None
    assert entry.name == "新名字"


def test_claude_takes_latest_ai_title(claude_root: Path) -> None:
    """ai-title 也会反复重写，后写的更贴合会话最终内容。"""
    path = write_jsonl(
        claude_root / "-home-sean-demo" / "aaaa1111-0000-0000-0000-000000000000.jsonl",
        [
            {"type": "ai-title", "aiTitle": "早期猜的标题"},
            {"type": "user", "cwd": "/home/sean/demo", "message": {"content": "正文"}},
            {"type": "ai-title", "aiTitle": "最终的标题"},
        ],
    )
    entry = claude.parse(FileRef.from_path(path))
    assert entry is not None
    assert entry.title == "最终的标题"
