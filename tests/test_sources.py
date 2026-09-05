"""两个会话源的解析。

重点覆盖「格式假设会变」这件事：脏行、缺字段、超大行、空文件都不能让扫描挂掉。
"""

from __future__ import annotations

import json
from pathlib import Path

from atm.model import UNTITLED, FileRef, Source
from atm.sources import claude, codex, pi

from .conftest import write_jsonl

# ---------------------------------------------------------------- claude


def test_claude_parse_extracts_title_cwd_branch(claude_session: Path) -> None:
    ref = FileRef.from_path(claude_session)
    assert ref is not None
    entry = claude.parse(ref)

    assert entry is not None
    assert entry.id == "aaaaaaaa-1111-2222-3333-444444444444"
    assert entry.title == "帮我把索引层的缓存加上 第二行会被折叠"  # 多行折叠成一行
    assert entry.cwd == "/home/user/IdeaProjects/demo"
    assert entry.git_branch == "feature/atm"
    assert entry.source is Source.CLAUDE


def test_claude_ai_title_wins_over_first_user_message(claude_root: Path) -> None:
    """ai-title 是 CLI 自己生成的，质量比截首条消息高，优先级要更高。"""
    path = write_jsonl(
        claude_root / "-home-user-demo" / "bbbbbbbb-0000-0000-0000-000000000000.jsonl",
        [
            {"type": "user", "cwd": "/home/user/demo", "message": {"content": "随便问一句"}},
            {"type": "ai-title", "aiTitle": "Add cache layer to index", "sessionId": "bbbbbbbb"},
        ],
    )
    entry = claude.parse(FileRef.from_path(path))
    assert entry is not None
    assert entry.title == "Add cache layer to index"


def test_claude_skips_junk_prompts(claude_root: Path) -> None:
    """slash command 的包装和 system-reminder 都不该当标题。"""
    path = write_jsonl(
        claude_root / "-home-user-demo" / "cccccccc-0000-0000-0000-000000000000.jsonl",
        [
            {"type": "user", "message": {"content": "<command-name>/effort</command-name>"}},
            {
                "type": "user",
                "message": {"content": "<system-reminder>ignore me</system-reminder>"},
            },
            {
                "type": "user",
                "cwd": "/home/user/demo",
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
        claude_root / "-home-user-demo" / "dddddddd-0000-0000-0000-000000000000.jsonl",
        [
            "{not json at all",
            "[1, 2, 3]",  # 合法 JSON 但不是 dict
            '{"type": "user", "cwd": "/home/user/demo", "message": {"content": "活下来了"}}',
        ],
    )
    entry = claude.parse(FileRef.from_path(path))
    assert entry is not None
    assert entry.title == "活下来了"


def test_claude_falls_back_to_untitled_and_decoded_dir(claude_root: Path) -> None:
    """空会话也要出现在列表里 —— 有 6% 的文件就是这样。"""
    path = write_jsonl(
        claude_root / "-home-user-IdeaProjects-demo" / "eeeeeeee-0000-0000-0000-000000000000.jsonl",
        [{"type": "assistant", "message": {"content": "no user message here"}}],
    )
    entry = claude.parse(FileRef.from_path(path))
    assert entry is not None
    assert entry.title == UNTITLED
    assert entry.cwd == "/home/user/IdeaProjects/demo"  # 从目录名反解
    assert entry.id == "eeeeeeee-0000-0000-0000-000000000000"  # 从文件名兜底


def test_claude_ignores_oversized_lines(claude_root: Path) -> None:
    """attachment 之类的巨行要跳过，否则 648MB 的文件会拖死冷启动。"""
    huge = json.dumps({"type": "user", "message": {"content": "x" * 200_000}}, ensure_ascii=False)
    path = write_jsonl(
        claude_root / "-home-user-demo" / "ffffffff-0000-0000-0000-000000000000.jsonl",
        [huge, {"type": "user", "cwd": "/home/user/demo", "message": {"content": "小行赢"}}],
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
    assert claude.decode_project_dir("-home-user-demo") == "/home/user/demo"
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
                    "cwd": "/home/user/IdeaProjects/sample-project",
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
    assert entry.cwd == "/home/user/IdeaProjects/sample-project"
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
                    "cwd": "/home/user/demo",
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
                    "cwd": "/home/user/demo",
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
        claude_root / "-home-user-demo" / "11112222-0000-0000-0000-000000000000.jsonl",
        [
            {"type": "custom-title", "customTitle": "sample-project", "sessionId": "11112222"},
            {"type": "user", "cwd": "/home/user/demo", "message": {"content": "推断出来的标题"}},
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
        claude_root / "-home-user-demo" / "33334444-0000-0000-0000-000000000000.jsonl",
        [
            {"type": "custom-title", "customTitle": "sample-project"},
            {"type": "user", "cwd": "/home/user/demo", "message": {"content": "完全无关的标题"}},
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
        claude_root / "-home-user-demo" / "77778888-0000-0000-0000-000000000000.jsonl",
        [
            {"type": "user", "cwd": "/home/user/demo", "message": {"content": "起初没有名字"}},
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
        claude_root / "-home-user-demo" / "99990000-0000-0000-0000-000000000000.jsonl",
        [
            {"type": "custom-title", "customTitle": "旧名字"},
            {"type": "user", "cwd": "/home/user/demo", "message": {"content": "正文"}},
            {"type": "custom-title", "customTitle": "新名字"},
        ],
    )
    entry = claude.parse(FileRef.from_path(path))
    assert entry is not None
    assert entry.name == "新名字"


def test_claude_takes_latest_ai_title(claude_root: Path) -> None:
    """ai-title 也会反复重写，后写的更贴合会话最终内容。"""
    path = write_jsonl(
        claude_root / "-home-user-demo" / "aaaa1111-0000-0000-0000-000000000000.jsonl",
        [
            {"type": "ai-title", "aiTitle": "早期猜的标题"},
            {"type": "user", "cwd": "/home/user/demo", "message": {"content": "正文"}},
            {"type": "ai-title", "aiTitle": "最终的标题"},
        ],
    )
    entry = claude.parse(FileRef.from_path(path))
    assert entry is not None
    assert entry.title == "最终的标题"


# ---------------------------------------------------------------- pi
#
# ⚠️ 和上面两组不同：pi 的记录形状**照上游文档写的，不是实测语料**
# （见 sources/pi.py 的模块 docstring）。真机跑起来后如果字段对不上，
# 这一组会第一个红 —— 那正是它存在的意义。


def _pi_file(root: Path, session_id: str, *, extra: list[dict | str] | None = None) -> Path:
    return write_jsonl(
        root / "--home-user-IdeaProjects-demo--" / f"2026-09-05T01_20_00_{session_id}.jsonl",
        [
            {
                "type": "session",
                "version": 3,
                "id": session_id,
                "timestamp": "2026-09-05T01:20:00.000Z",
                "cwd": "/home/user/IdeaProjects/demo",
            },
            *(extra or []),
        ],
    )


def _pi_user(text: str, entry_id: str = "a1b2c3d4") -> dict:
    return {
        "type": "message",
        "id": entry_id,
        "parentId": None,
        "timestamp": "2026-09-05T01:20:01.000Z",
        "message": {"role": "user", "content": text},
    }


def test_pi_parse_reads_header_and_first_user_message(pi_root: Path) -> None:
    path = _pi_file(pi_root, "abc12345", extra=[_pi_user("把索引层的缓存加上")])

    entry = pi.parse(FileRef.from_path(path))

    assert entry is not None
    assert entry.source is Source.PI
    assert entry.id == "abc12345"
    assert entry.title == "把索引层的缓存加上"
    assert entry.cwd == "/home/user/IdeaProjects/demo"
    assert entry.git_branch is None  # schema v3 没有 git 字段


def test_pi_ignores_non_user_roles_as_title(pi_root: Path) -> None:
    """role 枚举比另外两家宽 —— toolResult / bashExecution / compactionSummary
    都不能冒充标题，否则列表里会出现一堆 bash 输出。"""
    noise = [
        {"type": "message", "message": {"role": "toolResult", "content": "exit 0"}},
        {"type": "message", "message": {"role": "bashExecution", "content": "ls -la"}},
        {"type": "message", "message": {"role": "compactionSummary", "content": "用户讨论了 X"}},
        {
            "type": "message",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "好的"}]},
        },
    ]
    path = _pi_file(pi_root, "abc12345", extra=[*noise, _pi_user("真正的第一句")])

    entry = pi.parse(FileRef.from_path(path))

    assert entry is not None
    assert entry.title == "真正的第一句"


def test_pi_session_info_becomes_name(pi_root: Path) -> None:
    path = _pi_file(
        pi_root,
        "abc12345",
        extra=[
            _pi_user("随便问点什么"),
            {"type": "session_info", "id": "k1l2m3n4", "name": "Refactor auth module"},
        ],
    )

    entry = pi.parse(FileRef.from_path(path))

    assert entry is not None
    assert entry.name == "Refactor auth module"


def test_pi_takes_latest_rename(pi_root: Path) -> None:
    path = _pi_file(
        pi_root,
        "abc12345",
        extra=[
            _pi_user("随便问点什么"),
            {"type": "session_info", "name": "旧名字"},
            {"type": "session_info", "name": "新名字"},
        ],
    )

    entry = pi.parse(FileRef.from_path(path))

    assert entry is not None
    assert entry.name == "新名字"


def test_pi_tolerates_corrupt_lines(pi_root: Path) -> None:
    path = _pi_file(pi_root, "abc12345", extra=["{ 不是 json", _pi_user("脏行后面的正常消息")])

    entry = pi.parse(FileRef.from_path(path))

    assert entry is not None
    assert entry.title == "脏行后面的正常消息"


def test_pi_falls_back_to_filename_id_and_decoded_dir(pi_root: Path) -> None:
    """header 整行坏掉时也不能让这条会话从列表里消失。"""
    path = write_jsonl(
        pi_root / "--home-user-IdeaProjects-demo--" / "2026-09-05T01_20_00_deadbeef.jsonl",
        [_pi_user("没有 header 的会话")],
    )

    entry = pi.parse(FileRef.from_path(path))

    assert entry is not None
    assert entry.id == "deadbeef"
    assert entry.cwd == "/home/user/IdeaProjects/demo"
    assert entry.title == "没有 header 的会话"


def test_pi_untitled_when_no_user_message(pi_root: Path) -> None:
    entry = pi.parse(FileRef.from_path(_pi_file(pi_root, "abc12345")))
    assert entry is not None
    assert entry.title == UNTITLED


def test_pi_discover_finds_sessions(pi_root: Path) -> None:
    _pi_file(pi_root, "abc12345")
    assert len(list(pi.discover(pi_root))) == 1


def test_pi_discover_on_missing_root(tmp_path: Path) -> None:
    assert list(pi.discover(tmp_path / "nope")) == []


def test_pi_decode_project_dir_strips_the_double_dashes() -> None:
    assert pi.decode_project_dir("--home-user-myapp--") == "/home/user/myapp"
    # 形状对不上就原样返回，别拼出一个假路径
    assert pi.decode_project_dir("weird") == "weird"


def test_pi_id_from_filename_rejects_garbage() -> None:
    assert pi.parse.__module__.endswith("pi")  # 防止 import 写错指到别的模块
    assert pi._id_from_filename("2026-09-05T01_20_00_abc123") == "abc123"
    assert pi._id_from_filename("") == ""
