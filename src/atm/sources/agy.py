"""Antigravity CLI (`agy`) 会话源：`~/.gemini/antigravity-cli/conversation_summaries.db`。

**逆向观察自本机真实数据**。和 `opencode` 一样，主索引在一个 SQLite 库里，
同时每条会话在同级目录下还有按会话拆开的状态库与转录文件：

- 主索引：`conversation_summaries.db` 的 `conversation_summaries` 表，直接有
  `conversation_id`、`title`、`preview`、`last_modified_time`、`workspace_uris`、
  `parent_conversation_id`、`nesting_depth`、`last_user_input_time`。
- 单会话库：`conversations/<id>.db`（用来统计会话体积，时间缺失时作兜底）。
- 紧凑转录：`brain/<id>/.system_generated/logs/transcript.jsonl`（`title` 与 `preview`
  皆空时扫 `USER_INPUT` 记录提首句标题）。

几个实测踩出来的陷阱：

1. **子代理排除**：子代理会话同样写进 `conversation_summaries`，靠
   `bool(parent_conversation_id)` 或 `(nesting_depth or 0) > 0` 排除。
2. **9 位小数纳秒与零值时间**：`last_modified_time` 形如
   `2026-11-13 04:20:22.902731278+00:00`（9 位纳秒）或未初始化零值
   `0001-01-01 00:00:00+00:00`。整数运算保留全部 9 位纳秒做缓存指纹，拒绝 `year <= 1970`。
3. **`<USER_REQUEST>` 标签陷阱**：`transcript.jsonl` 里的用户输入被包成
   `<USER_REQUEST>\\n…\\n</USER_REQUEST>\\n<ADDITIONAL_METADATA>…</ADDITIONAL_METADATA>`。
   直接喂给 `strip_wrapper_blocks` 会把 `<USER_REQUEST>` 当注入包装整段剥空，
   必须先把 `<USER_REQUEST>` 内部正文取出来再做 `is_junk_prompt` / `clean_title`。
4. **恢复命令**：`agy --conversation <id>`（无短选项；`-c` 只恢复最近一条，不吃 ID）。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import urllib.parse
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from ..jsonl import iter_head_records
from ..model import UNTITLED, FileRef, SessionEntry, Source
from ..text import clean_title, is_junk_prompt

DB_NAME = "conversation_summaries.db"
DEFAULT_ROOT = Path.home() / ".gemini" / "antigravity-cli"

_BUSY_TIMEOUT_MS = 500
_MAX_TRANSCRIPT_RECORDS = 200
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_ISO_TS_RE = re.compile(
    r"^\s*"
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"[T ]"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.(?P<frac>\d+))?"
    r"(?:\s*(?P<tz>Z|z|[+-]\d{2}:?\d{2}))?"
    r"\s*$"
)

_USER_REQUEST_RE = re.compile(
    r"<USER_REQUEST(?:\s[^>]*)?>(.*?)</USER_REQUEST>",
    re.DOTALL | re.IGNORECASE,
)


def load_sessions(root: Path | None = None) -> dict[str, dict]:
    """一次读出全部顶层会话摘要：{conversation_id: 行}。库不在 / 损坏 / 被锁均返回空表。"""
    base = root or DEFAULT_ROOT
    db = base / DB_NAME
    if not db.is_file():
        return {}
    try:
        with contextlib.closing(_connect(db)) as conn:
            rows = conn.execute(
                "SELECT conversation_id, title, preview, last_modified_time,"
                " workspace_uris, parent_conversation_id, nesting_depth,"
                " last_user_input_time"
                " FROM conversation_summaries"
            ).fetchall()
    except (sqlite3.Error, OSError):
        return {}

    sessions: dict[str, dict] = {}
    for row in rows:
        session = dict(row)
        session_id = session.get("conversation_id")
        if not _valid_id(session_id):
            continue
        if _is_subagent(session):
            continue

        conv_st = _stat(base / "conversations" / f"{session_id}.db")
        transcript_st: os.stat_result | None = None
        if conv_st is None:
            transcript_st = _stat(_transcript_path(base, session_id))

        size_bytes = (
            conv_st.st_size
            if conv_st is not None
            else (transcript_st.st_size if transcript_st is not None else 0)
        )
        mtime_ns = _parse_iso_ns(session.get("last_modified_time")) or _parse_iso_ns(
            session.get("last_user_input_time")
        )
        if mtime_ns is None:
            if conv_st is not None:
                mtime_ns = conv_st.st_mtime_ns
            else:
                if transcript_st is None:
                    transcript_st = _stat(_transcript_path(base, session_id))
                if transcript_st is not None:
                    mtime_ns = transcript_st.st_mtime_ns

        session["size_bytes"] = int(size_bytes)
        session["mtime_ns"] = mtime_ns
        sessions[session_id] = session
    return sessions


def discover(
    root: Path | None = None, sessions: dict[str, dict] | None = None
) -> Iterator[FileRef]:
    """每条顶层会话生成一个 `<db>#<conversation_id>` 合成 FileRef，实现单会话粒度缓存失效。"""
    base = root or DEFAULT_ROOT
    db = base / DB_NAME
    known = sessions if sessions is not None else load_sessions(base)
    for session_id, session in known.items():
        updated_ns = session.get("mtime_ns")
        if not isinstance(updated_ns, int) or updated_ns <= 0:
            continue
        yield FileRef(
            path=f"{db}#{session_id}",
            mtime_ns=updated_ns,
            size_bytes=int(session.get("size_bytes") or 0),
        )


def parse(ref: FileRef, sessions: dict[str, dict] | None = None) -> SessionEntry | None:
    db_path, _, session_id = ref.path.rpartition("#")
    if not db_path or not _valid_id(session_id):
        return None
    root = Path(db_path).parent
    known = sessions if sessions is not None else load_sessions(root)
    session = known.get(session_id)
    if session is None:
        return None

    cwd = _extract_cwd(session.get("workspace_uris"))
    if not cwd:
        return None  # 没有工作目录无法 cd 回原项目恢复

    title = (
        _usable_summary_text(session.get("title"), check_junk=False)
        or _usable_summary_text(session.get("preview"), check_junk=True)
        or _first_transcript_title(root, session_id)
        or UNTITLED
    )

    sec, nsec = divmod(ref.mtime_ns, 1_000_000_000)
    updated_at = datetime.fromtimestamp(sec, tz=UTC).replace(microsecond=nsec // 1000)

    return SessionEntry(
        id=session_id,
        title=title,
        source=Source.AGY,
        cwd=cwd,
        git_branch=None,
        updated_at=updated_at,
        path=ref.path,
        size_bytes=ref.size_bytes,
        name=None,
    )


def _connect(db: Path) -> sqlite3.Connection:
    """只读打开。**绝不**写别人的库 —— 会话数据只读是本项目的硬规则第 1 条。"""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    return conn


def _transcript_path(root: Path, session_id: str) -> Path:
    return root / "brain" / session_id / ".system_generated" / "logs" / "transcript.jsonl"


def _first_transcript_title(root: Path, session_id: str) -> str:
    """`title` 与 `preview` 都空时的退路：扫头部 `USER_INPUT` 记录。"""
    path = _transcript_path(root, session_id)
    for index, record in enumerate(iter_head_records(path)):
        if index >= _MAX_TRANSCRIPT_RECORDS:
            break
        if record.get("type") != "USER_INPUT":
            continue
        candidate = _usable_summary_text(record.get("content"), check_junk=True)
        if candidate:
            return candidate
    return ""


def _usable_summary_text(raw: object, *, check_junk: bool) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return ""
    unwrapped = _unwrap_user_request(raw)
    if not unwrapped.strip():
        return ""
    if check_junk and is_junk_prompt(unwrapped):
        return ""
    return clean_title(unwrapped)


def _unwrap_user_request(text: str) -> str:
    match = _USER_REQUEST_RE.search(text)
    return match.group(1).strip() if match is not None else text


def _extract_cwd(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        uris = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(uris, list):
        return ""
    for item in uris:
        if not isinstance(item, str) or not item.startswith("file://"):
            continue
        parsed = urllib.parse.urlparse(item)
        if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
            continue
        path = urllib.parse.unquote(parsed.path).strip()
        if path:
            return path
    return ""


def _parse_iso_ns(raw: object) -> int | None:
    """解析 SQLite 里的 ISO 时间戳（兼容 0..9 位小数纳秒与零值），返回 epoch 纳秒。"""
    if not isinstance(raw, str):
        return None
    match = _ISO_TS_RE.match(raw)
    if match is None:
        return None
    year = int(match.group("year"))
    if year <= 1970:
        return None
    tz_raw = match.group("tz")
    if not tz_raw or tz_raw in ("Z", "z"):
        tz = UTC
    else:
        sign = 1 if tz_raw[0] == "+" else -1
        digits = tz_raw[1:].replace(":", "")
        tz_h, tz_m = int(digits[:2]), int(digits[2:4])
        if tz_h > 23 or tz_m > 59:
            return None
        tz = timezone(sign * timedelta(hours=tz_h, minutes=tz_m))
    try:
        dt = datetime(
            year,
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
            tzinfo=tz,
        )
    except ValueError:
        return None

    delta = dt.astimezone(UTC) - _EPOCH
    whole_sec = delta.days * 86_400 + delta.seconds
    if whole_sec <= 0:
        return None
    frac = (match.group("frac") or "")[:9].ljust(9, "0")
    return whole_sec * 1_000_000_000 + int(frac)


def _valid_id(value: object) -> bool:
    if not isinstance(value, str) or not value or "#" in value:
        return False
    return not any(ch.isspace() or ord(ch) < 32 or ord(ch) == 0x7F for ch in value)


def _is_subagent(session: dict) -> bool:
    parent = session.get("parent_conversation_id")
    if isinstance(parent, str):
        if parent.strip():
            return True
    elif parent:
        return True
    depth = session.get("nesting_depth")
    if isinstance(depth, bool):
        return False
    if isinstance(depth, (int, float)):
        return depth > 0
    if isinstance(depth, str):
        try:
            return int(depth) > 0
        except ValueError:
            return False
    return False


def _stat(path: Path) -> os.stat_result | None:
    try:
        return os.stat(path)  # noqa: PTH116
    except OSError:
        return None
