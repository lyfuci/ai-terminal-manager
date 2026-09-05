"""Codex 会话源：`~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl`。

实测事实（本机 2026-08-12，codex-cli 0.147.0）：

- 项目 README 里写的 `~/.codex/session_index.jsonl`（带现成的 `thread_name` 中文标题）
  **已经不是真相**：那个文件停在 8/3、只剩 5 条，而 sessions/ 下有 86 个 rollout 文件（120MB）。
  它仍然被当作**标题的优先来源**（那 5 条的人类标题质量最好），但不能当会话列表用。
- `~/.codex/thread_history_1.sqlite` 是**单线程的投影缓存**（实测只有 1 个 thread / 3 个 turn），
  不是全局索引，不能当数据源。
- rollout 文件第 1 行是 `type:"session_meta"`，payload 里有
  `session_id` / `id` / `cwd` / `timestamp` / `cli_version` / `originator` / `git`（可能是 null）。
- 标题来自 `type:"event_msg"` 且 `payload.type == "user_message"` 的 `payload.message`；
  也兼容 `response_item` 里 role=user 的 `input_text` 块（不同版本写法不同）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from pathlib import Path

from ..jsonl import iter_all_records, iter_head_records
from ..model import UNTITLED, FileRef, SessionEntry, Source
from ..text import clean_title, is_junk_prompt

DEFAULT_ROOT = Path.home() / ".codex" / "sessions"
LEGACY_INDEX = Path.home() / ".codex" / "session_index.jsonl"

# Codex 的 base_instructions 会把整段 system prompt 塞进第 1 行，单行就有几十 KB，
# 所以头部预算要比 Claude 大一些，才能翻到第一条 user_message。
_HEAD_BYTES = 512 * 1024
# ⚠️ 单行上限必须跟着一起提。第 1 行的 `session_meta` 里塞着整段 base_instructions
# （实测 17,921 字），超过 jsonl.py 默认的 64KB 就会被整行丢掉 ——
# 于是 cwd 拿不到、静默退化成 $HOME，投递时 `cd $HOME` 完全是错的目录。
_MAX_LINE_BYTES = 1024 * 1024
_MAX_RECORDS = 400


def discover(root: Path | None = None) -> Iterator[FileRef]:
    base = root or DEFAULT_ROOT
    if not base.is_dir():
        return
    for path in base.glob("**/rollout-*.jsonl"):
        ref = FileRef.from_path(path)
        if ref is not None and ref.size_bytes > 0:
            yield ref


def load_legacy_titles(index_path: Path | None = None) -> dict[str, str]:
    """读老的 session_index.jsonl 拿 `thread_name`。

    这个文件已经不再更新，但存量的那几条标题是 Codex 自己起的、质量比截首条消息好，
    所以留着当标题的第一优先级。文件不在就返回空 dict —— 它是可选增强，不是依赖。
    """
    path = index_path or LEGACY_INDEX
    titles: dict[str, str] = {}
    for record in iter_all_records(path):
        session_id = record.get("id")
        name = record.get("thread_name")
        if isinstance(session_id, str) and isinstance(name, str) and name.strip():
            titles[session_id] = clean_title(name)
    return titles


def parse(ref: FileRef, legacy_titles: dict[str, str] | None = None) -> SessionEntry | None:
    session_id = ""
    cwd = ""
    git_branch: str | None = None
    saw_any = False

    # 两个候选标题分开收：`event_msg`/`user_message` 才是**用户真正敲的那一轮**，
    # `response_item` 里 role=user 的记录可能是 CLI 注入的上下文
    # （实测 Codex 0.147 会在真实提问前塞一条 10865 字的 `<recommended_plugins>…`，
    # 而且它排在更前面）。所以不能「谁先出现用谁」，必须按来源定优先级。
    event_title = ""
    fallback_title = ""

    for index, record in enumerate(
        iter_head_records(ref.path, max_bytes=_HEAD_BYTES, max_line_bytes=_MAX_LINE_BYTES)
    ):
        if index >= _MAX_RECORDS:
            break
        saw_any = True
        payload = record.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        record_type = record.get("type")

        if record_type == "session_meta":
            # ⚠️ 子 agent 的 rollout 文件里 `session_id` 记的是**父线程**的 id，
            # 只有 `id` 才是它自己的。先读 `id`，否则一个父会话下的 N 个子 agent
            # 会全部塌成同一条 —— 实测 sample-crawler 下就有 7 个文件变成同一个 id。
            session_id = _first_str(payload.get("id"), payload.get("session_id")) or session_id
            if _is_subagent(payload):
                return None  # 子 agent 不能独立 resume，和 Claude 侧的 subagents/ 一样排除
            cwd = _first_str(payload.get("cwd")) or cwd
            git_branch = git_branch or _git_branch(payload.get("git"))

        elif not event_title and _is_event_user_message(record_type, payload):
            event_title = _title_from(payload)

        elif not fallback_title and _is_response_user_message(record_type, payload):
            fallback_title = _title_from(payload)

        if event_title and cwd and session_id:
            break

    title = event_title or fallback_title

    if not saw_any:
        return None

    if not session_id:
        session_id = _id_from_filename(Path(ref.path).stem)
    if not session_id:
        return None

    # 老索引里的人类标题优先级最高。
    if legacy_titles:
        title = legacy_titles.get(session_id) or title

    return SessionEntry(
        id=session_id,
        title=title or UNTITLED,
        source=Source.CODEX,
        cwd=cwd or str(Path.home()),
        git_branch=git_branch,
        updated_at=ref.updated_at,
        path=ref.path,
        size_bytes=ref.size_bytes,
    )


def _is_subagent(payload: dict) -> bool:
    """这个 rollout 是不是子 agent 产生的。

    实测判据（codex 0.144+，`spawn_agent` 多 agent 模式）：
      thread_source == "subagent"，且带 parent_thread_id / forked_from_id / agent_nickname。
    父线程则是 thread_source == "user"。

    子 agent **不是独立可 resume 的会话** —— 它没有自己的可恢复入口，
    而且它的 session_id 指向父线程，留在列表里既是重复项、resume 时还会撞车。
    """
    if payload.get("thread_source") == "subagent":
        return True
    return bool(payload.get("parent_thread_id") or payload.get("forked_from_id"))


def _is_event_user_message(record_type: object, payload: dict) -> bool:
    """用户真正敲的那一轮。"""
    return record_type == "event_msg" and payload.get("type") == "user_message"


def _is_response_user_message(record_type: object, payload: dict) -> bool:
    """老版本 / 另一种写法的 user 消息，只当兜底用。"""
    return (
        record_type == "response_item"
        and payload.get("type") == "message"
        and payload.get("role") == "user"
    )


def _title_from(payload: dict) -> str:
    text = _message_text(payload)
    if not text or is_junk_prompt(text):
        return ""
    return clean_title(text)


def _message_text(payload: dict) -> str:
    message = payload.get("message")
    if isinstance(message, str):
        return message

    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, Iterable) or isinstance(content, (bytes, dict)):
        return ""

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") not in ("input_text", "text"):
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n".join(parts)


def _git_branch(git: object) -> str | None:
    """session_meta.git 实测可能是 null；有值时它是个 dict，字段名各版本不一，都试一遍。"""
    if not isinstance(git, dict):
        return None
    for key in ("branch", "current_branch", "head", "ref"):
        value = git.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


_UUID_RE = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


def _id_from_filename(stem: str) -> str:
    """`rollout-2026-08-12T01-46-32-00000000-0000-7000-8000-000000000002` → 尾部的 UUID。

    **必须校验形状**：之前只检查「至少 5 段」就把末 5 段拼起来，于是
    `rollout-2026-08-12T01-46-32` 会拼出 `2026-08-12T01-46-32` 这种假 id，
    喂给 `codex resume` 只会报找不到会话。拼不出合法 UUID 就返回空，让调用方丢弃这条。
    """
    parts = stem.split("-")
    if len(parts) < 5:
        return ""
    candidate = "-".join(parts[-5:])
    return candidate if _UUID_RE.match(candidate) else ""


def _first_str(*candidates: object) -> str:
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return ""
