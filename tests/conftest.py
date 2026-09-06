"""测试用的假语料。

每个 fixture 的记录形状都照着**实测的真实文件**写（2026-08-12 本机语料），
不是照文档猜的 —— 项目 README 里关于这两个格式的描述有三处已经过时。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def write_jsonl(path: Path, records: list[dict | str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for record in records:
        lines.append(record if isinstance(record, str) else json.dumps(record, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def claude_root(tmp_path: Path) -> Path:
    return tmp_path / "claude" / "projects"


@pytest.fixture
def codex_root(tmp_path: Path) -> Path:
    return tmp_path / "codex" / "sessions"


@pytest.fixture
def claude_session(claude_root: Path) -> Path:
    """一个典型的 Claude 会话：首条 user 是 isMeta 的 caveat，真标题在第二条。"""
    return write_jsonl(
        claude_root / "-home-user-IdeaProjects-demo" / "aaaaaaaa-1111-2222-3333-444444444444.jsonl",
        [
            {
                "type": "last-prompt",
                "leafUuid": "x",
                "sessionId": "aaaaaaaa-1111-2222-3333-444444444444",
            },
            {
                "type": "user",
                "isMeta": True,
                "sessionId": "aaaaaaaa-1111-2222-3333-444444444444",
                "message": {
                    "role": "user",
                    "content": "<local-command-caveat>Caveat: ...</local-command-caveat>",
                },
            },
            {
                "type": "user",
                "sessionId": "aaaaaaaa-1111-2222-3333-444444444444",
                "cwd": "/home/user/IdeaProjects/demo",
                "gitBranch": "feature/atm",
                "message": {"role": "user", "content": "帮我把索引层的缓存加上\n第二行会被折叠"},
            },
            {"type": "assistant", "message": {"role": "assistant", "content": "好的"}},
        ],
    )


@pytest.fixture
def pi_root(tmp_path: Path) -> Path:
    return tmp_path / "pi" / "agent" / "sessions"


@pytest.fixture
def opencode_root(tmp_path: Path) -> Path:
    """opencode 的数据目录替身。库文件由 `write_opencode_db` 建。"""
    return tmp_path / "opencode"


@pytest.fixture
def gemini_root(tmp_path: Path) -> Path:
    """`~/.gemini/tmp` 的替身。projects.json 是它的**同级**文件，所以要多一层 gemini/。"""
    return tmp_path / "gemini" / "tmp"


@pytest.fixture(autouse=True)
def _isolate_source_roots(tmp_path: Path, monkeypatch):
    """把所有数据源的 DEFAULT_ROOT 指到一个不存在的临时目录。

    为什么必须 autouse：`DEFAULT_ROOT` 是 **import 时**用 `Path.home()` 算好的常量，
    测试里再 monkeypatch `Path.home` 已经晚了 —— 那些不显式传 root 的调用
    （`atm doctor` 走的 `mod.discover()`）会去读开发者**真实的** ~/.claude / ~/.gemini。
    仓库硬规则第 1 条是「会话数据只读、测试只用自己的语料」，这里从根上堵死。
    要用真语料形状的测试自己传 root，不受影响。
    """
    from atm.sources import claude, codex, gemini, opencode, pi

    isolated = tmp_path / "no-real-sessions"
    for module in (claude, codex, gemini, opencode, pi):
        monkeypatch.setattr(module, "DEFAULT_ROOT", isolated / module.__name__.rsplit(".", 1)[-1])
    monkeypatch.setattr(codex, "LEGACY_INDEX", isolated / "no-legacy.jsonl")
    return isolated


@pytest.fixture(autouse=True)
def _pin_cli_language(monkeypatch):
    """测试里的断言写的是中文原文；把界面语言钉死在 zh，不受开发者机器 LANG 影响。
    test_i18n.py 自己会覆盖这个设置。"""
    from atm import i18n

    monkeypatch.setenv("ATM_LANG", "zh")
    i18n.reset_lang()
    yield
    i18n.reset_lang()


def write_opencode_db(root: Path, sessions: list[dict], parts: list[dict] | None = None) -> Path:
    """造一个 opencode 形状的 SQLite 库。

    只建 atm 真正读的三张表和它读的列 —— 上游 session 表有 28 列，
    照抄全部只会让「上游加了一列」变成测试要改的事。
    """
    import json as _json
    import sqlite3

    root.mkdir(parents=True, exist_ok=True)
    db = root / "opencode.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, title TEXT,"
        " time_created INTEGER, time_updated INTEGER, parent_id TEXT);"
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT);"
        "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,"
        " time_created INTEGER, data TEXT);"
    )
    for s in sessions:
        conn.execute(
            "INSERT INTO session (id, directory, title, time_created, time_updated, parent_id)"
            " VALUES (:id, :directory, :title, :time_created, :time_updated, :parent_id)",
            {
                "directory": "/home/user/demo",
                "title": None,
                "time_created": 1788714639122,
                "time_updated": 1788714639780,
                "parent_id": None,
                **s,
            },
        )
    for i, p in enumerate(parts or []):
        message_id = f"msg{i}"
        conn.execute(
            "INSERT INTO message (id, session_id, data) VALUES (?, ?, ?)",
            (message_id, p["session_id"], _json.dumps({"role": p.get("role", "user")})),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, data)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                f"prt{i}",
                message_id,
                p["session_id"],
                p.get("time_created", i),
                _json.dumps({"type": p.get("type", "text"), "text": p.get("text", "")}),
            ),
        )
    conn.commit()
    conn.close()
    return db
