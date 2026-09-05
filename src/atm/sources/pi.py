"""Pi 会话源：`~/.pi/agent/sessions/--<cwd 把 '/' 换成 '-'>--/<ts>_<uuid>.jsonl`。

⚠️ **这个适配器是按上游公开文档写的，不是像 claude.py / codex.py 那样逆向观察出来的**
（本机没装 pi，无真实语料可测）。依据是 earendil-works/pi 的
`packages/coding-agent/docs/session-format.md`（schema `version: 3`）：

- 第 1 行是 SessionHeader：`{"type":"session","version":3,"id":…,"timestamp":…,"cwd":…}`
  —— **只有它带 `cwd`**，后续记录都没有，所以 cwd 必须从头部拿，拿不到才退回目录名。
- 会话显示名走独立记录：`{"type":"session_info","name":"Refactor auth module"}`，
  由 `/name` 或 `--name` 产生。**会重复出现**（可以改名多次），取最后一条。
- 消息记录是 `{"type":"message","message":{"role":…,"content":…}}`，
  注意 role 不止 user/assistant，还有 `toolResult` / `bashExecution` /
  `compactionSummary` / `branchSummary` —— 只认 `user` 那一类当标题来源。
- `content` 可能是裸字符串（user）也可能是 block 数组（assistant），和另外两家一样。

因为是文档推导，解析全程走「拿不到就降级」而不是抛错：
字段缺失退回文件名 / 目录名，整个目录不存在就安静返回空。
真机跑起来后如果字段对不上，第一时间红的应该是 tests/test_sources.py 里的 pi 用例。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from pathlib import Path

from ..jsonl import iter_head_records, iter_tail_records
from ..model import UNTITLED, FileRef, SessionEntry, Source
from ..text import clean_title, is_junk_prompt

DEFAULT_ROOT = Path.home() / ".pi" / "agent" / "sessions"

_MAX_RECORDS = 400

# `--home-user-myapp--`：前后各两个连字符包住，中间把 '/' 换成 '-'。
_PROJECT_DIR_RE = re.compile(r"^--(?P<body>.*)--$")
# 文件名 `2024-12-03T14_00_00_abc123.jsonl` 的尾段就是 session id。
_ID_RE = re.compile(r"^[0-9A-Za-z-]+$")


def discover(root: Path | None = None) -> Iterator[FileRef]:
    """列出所有会话文件。目录不存在就安静返回空。

    只扫一层（`*/*.jsonl`），和 claude.py 同样的理由：会话文件直接躺在
    per-cwd 目录下，再往深处递归只会捞进将来可能出现的衍生产物。
    """
    base = root or DEFAULT_ROOT
    if not base.is_dir():
        return
    for path in base.glob("*/*.jsonl"):
        ref = FileRef.from_path(path)
        if ref is not None and ref.size_bytes > 0:
            yield ref


def decode_project_dir(name: str) -> str:
    """`--home-user-myapp--` → `/home/user/myapp`。

    和 claude 那边一样是**有损**的（目录名里本来就有的 '-' 分不出来），
    所以只当 cwd 的兜底 —— SessionHeader 里读到真的 cwd 一律优先。
    """
    match = _PROJECT_DIR_RE.match(name)
    if match is None:
        return name
    body = match.group("body")
    return "/" + body.replace("-", "/") if body else "/"


def parse(ref: FileRef) -> SessionEntry | None:
    session_id = ""
    cwd = ""
    title = ""
    name = ""
    saw_any = False

    for index, record in enumerate(iter_head_records(ref.path)):
        if index >= _MAX_RECORDS:
            break
        saw_any = True
        record_type = record.get("type")

        if record_type == "session":
            # 分叉出来的会话带 `parentSession`，但它**仍然可以独立 resume**
            # （`--fork` 产生的是一个完整的新会话文件），所以不像 codex 的子 agent 那样排除。
            session_id = _first_str(record.get("id")) or session_id
            cwd = _first_str(record.get("cwd")) or cwd

        elif record_type == "session_info":
            candidate = record.get("name")
            if isinstance(candidate, str) and candidate.strip():
                name = clean_title(candidate, limit=40)  # 后写的覆盖先写的 = 最后一次改名

        elif not title and record_type == "message":
            text = _user_text(record)
            if text and not is_junk_prompt(text):
                title = clean_title(text)

        if title and cwd and session_id:
            break

    if not saw_any:
        return None

    # 改名可以发生在会话任何时刻，头部窗口未必覆盖 —— 尾部再扫一遍取最后一个 name。
    # （claude.py 踩过这个坑：14 个命名会话里 4 个的改名在头部窗口之外。）
    tail_name = _scan_tail_name(ref.path)
    name = tail_name or name

    path = Path(ref.path)
    if not session_id:
        session_id = _id_from_filename(path.stem)
    if not session_id:
        return None
    if not cwd:
        cwd = decode_project_dir(path.parent.name)

    return SessionEntry(
        id=session_id,
        title=title or UNTITLED,
        source=Source.PI,
        cwd=cwd or str(Path.home()),
        git_branch=None,  # session-format.md 的 schema v3 里没有 git 字段
        updated_at=ref.updated_at,
        path=ref.path,
        size_bytes=ref.size_bytes,
        name=name or None,
    )


def _scan_tail_name(path: str) -> str:
    name = ""
    for record in iter_tail_records(path):
        if record.get("type") != "session_info":
            continue
        candidate = record.get("name")
        if isinstance(candidate, str) and candidate.strip():
            name = clean_title(candidate, limit=40)
    return name


def _user_text(record: dict) -> str:
    """只认 role == "user" 的消息。

    ⚠️ pi 的 role 枚举比另外两家宽：`user` / `assistant` / `toolResult` /
    `bashExecution` / `custom` / `branchSummary` / `compactionSummary`。
    不做这层过滤的话，一条 compaction 摘要或者 bash 输出会冒充标题。
    """
    message = record.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return ""
    return _blocks_to_text(message.get("content"))


def _blocks_to_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, Iterable) or isinstance(content, (bytes, dict)):
        return ""

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n".join(parts)


def _id_from_filename(stem: str) -> str:
    """`2024-12-03T14_00_00_abc123` → `abc123`。

    按 '_' 拆取最后一段。拆不出合法形状就返回空串让调用方丢弃 ——
    codex.py 踩过这个坑：拼出假 id 喂给 CLI 只会报「找不到会话」，
    比列表里少一条更难查。
    """
    candidate = stem.rsplit("_", 1)[-1]
    return candidate if candidate and _ID_RE.match(candidate) else ""


def _first_str(*candidates: object) -> str:
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return ""
