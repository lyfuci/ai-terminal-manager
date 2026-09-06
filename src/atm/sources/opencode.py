"""opencode 会话源：`~/.local/share/opencode/opencode.db`（SQLite）。

**逆向观察自本机真实数据**（opencode 1.18.29）。和另外四家最大的不同：
**它把所有会话放在一个 SQLite 库里**，不是一个会话一个文件。

这带来两个设计决定：

1. **合成 FileRef**。atm 的管线是「一个 FileRef → 一条会话」，而这里只有一个 .db 文件。
   所以给每条会话造一个 `<db 路径>#<session_id>` 的路径，指纹用**该行自己的** `time_updated`。
   好处是缓存粒度比文件源还细：改一条会话只让那一条失效，而不是整个库。
   坏处是这个路径不能 stat —— 所以 FileRef 是手工构造的，不走 `from_path`。

2. **不解析对话内容**。`session` 表里直接有 `id` / `directory`（就是 cwd）/ `title` /
   `time_created` / `time_updated`，是这五个源里唯一不用从消息里猜标题的。
   只有 opencode 自己没来得及生成标题时（会话报错或中断）才退回去看第一条消息。

其它观察到的事实：

- `parent_id` 非空 = 子 agent 会话。和 codex 的子会话一样排除掉：它们不是用户开的对话。
- 标题占位符形如 `New session - 2026-09-06T17:10:39.122Z`，是「还没生成标题」而不是真标题。
- 时间是**毫秒**的 epoch，不是秒。
- 恢复：`opencode --session <id>`（TUI）实测存在；`opencode run -s <id>` 是非交互那条路。
- 数据目录可以用 `OPENCODE_DATA_DIR` 改（上游支持逗号分隔多个，这里只认第一个）。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from ..model import UNTITLED, FileRef, SessionEntry, Source
from ..text import clean_title, is_junk_prompt

DB_NAME = "opencode.db"

# opencode 还没给会话起名字时写的占位符，不是真标题。
_PLACEHOLDER_TITLE = re.compile(r"^New session - \d{4}-\d{2}-\d{2}T")

# 只在标题是占位符时才去翻消息，且最多翻这么多条 —— 用户的第一句必然在最前面。
_MAX_PARTS_SCANNED = 8

# 别在库被写的时候干等。读不到就当没有，扫描不能因为一条锁卡住。
_BUSY_TIMEOUT_MS = 500


def default_root() -> Path:
    """`OPENCODE_DATA_DIR` 优先。上游允许逗号分隔多个目录，这里只认第一个。"""
    override = os.environ.get("OPENCODE_DATA_DIR", "").split(",")[0].strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "opencode"


DEFAULT_ROOT = default_root()


def load_sessions(root: Path | None = None) -> dict[str, dict]:
    """一次把所有会话查出来：{session_id: 行}。库不在 / 读不了 / 表不对都返回空表。

    体积单独一条聚合查询算出来 —— atm 要靠它警告「这条会话大到 resume 会卡」。
    """
    db = (root or DEFAULT_ROOT) / DB_NAME
    if not db.is_file():
        return {}
    try:
        with _connect(db) as conn:
            rows = conn.execute(
                "SELECT id, directory, title, time_created, time_updated, parent_id FROM session"
            ).fetchall()
            sizes = dict(
                conn.execute(
                    "SELECT session_id, SUM(LENGTH(data)) FROM part GROUP BY session_id"
                ).fetchall()
            )
    except sqlite3.Error:
        # 库被锁 / 版本对不上 / 表被改名：当作没有这个源，不让整次扫描挂掉
        return {}

    sessions: dict[str, dict] = {}
    for row in rows:
        session = dict(row)
        session_id = session.get("id")
        if not isinstance(session_id, str) or not session_id:
            continue
        if session.get("parent_id"):
            continue  # 子 agent 会话，不是用户开的对话
        session["size_bytes"] = int(sizes.get(session_id) or 0)
        sessions[session_id] = session
    return sessions


def discover(
    root: Path | None = None, sessions: dict[str, dict] | None = None
) -> Iterator[FileRef]:
    """每条会话一个合成 FileRef。路径是 `<db>#<session_id>`，不能 stat。"""
    base = root or DEFAULT_ROOT
    db = base / DB_NAME
    for session_id, session in (sessions if sessions is not None else load_sessions(base)).items():
        updated_ms = _as_int(session.get("time_updated")) or _as_int(session.get("time_created"))
        if updated_ms is None:
            continue
        yield FileRef(
            path=f"{db}#{session_id}",
            mtime_ns=updated_ms * 1_000_000,  # 库里是毫秒
            size_bytes=session.get("size_bytes", 0),
        )


def parse(ref: FileRef, sessions: dict[str, dict] | None = None) -> SessionEntry | None:
    db_path, _, session_id = ref.path.rpartition("#")
    if not session_id:
        return None
    known = sessions if sessions is not None else load_sessions(Path(db_path).parent)
    session = known.get(session_id)
    if session is None:
        return None

    cwd = _as_str(session.get("directory"))
    if not cwd:
        return None  # 恢复必须先 cd 回去，没有 cwd 这条会话没法用

    title = _as_str(session.get("title"))
    if not title or _PLACEHOLDER_TITLE.match(title):
        title = _first_user_text(Path(db_path), session_id)

    return SessionEntry(
        id=session_id,
        title=clean_title(title) if title else UNTITLED,
        source=Source.OPENCODE,
        cwd=cwd,
        git_branch=None,  # session 表里没有分支，project.vcs 只说用了哪种版本控制
        updated_at=ref.updated_at,
        path=ref.path,
        size_bytes=ref.size_bytes,
        name=None,  # opencode 的 --title 也写进 title 列，和自动标题分不开
    )


def _first_user_text(db: Path, session_id: str) -> str:
    """opencode 还没起标题时的退路：这条会话里第一段用户文本。

    role 在 `message.data` 的 JSON 里，所以只能取回来在 Python 里判断。
    只有占位符标题才会走到这里，量很小。
    """
    try:
        with _connect(db) as conn:
            rows = conn.execute(
                "SELECT m.data AS message_data, p.data AS part_data "
                "FROM part p JOIN message m ON m.id = p.message_id "
                "WHERE p.session_id = ? ORDER BY p.time_created LIMIT ?",
                (session_id, _MAX_PARTS_SCANNED),
            ).fetchall()
    except sqlite3.Error:
        return ""

    for row in rows:
        message = _loads(row["message_data"])
        part = _loads(row["part_data"])
        if message.get("role") != "user" or part.get("type") != "text":
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip() and not is_junk_prompt(text):
            return text
    return ""


def _connect(db: Path) -> sqlite3.Connection:
    """只读打开。**绝不**写别人的库 —— 会话数据只读是本项目的硬规则第 1 条。"""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    return conn


def _loads(raw: object) -> dict:
    if not isinstance(raw, (str, bytes)):
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _as_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
