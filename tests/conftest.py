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
        claude_root / "-home-sean-IdeaProjects-demo" / "aaaaaaaa-1111-2222-3333-444444444444.jsonl",
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
                "cwd": "/home/sean/IdeaProjects/demo",
                "gitBranch": "feature/atm",
                "message": {"role": "user", "content": "帮我把索引层的缓存加上\n第二行会被折叠"},
            },
            {"type": "assistant", "message": {"role": "assistant", "content": "好的"}},
        ],
    )


@pytest.fixture
def pi_root(tmp_path: Path) -> Path:
    return tmp_path / "pi" / "agent" / "sessions"
