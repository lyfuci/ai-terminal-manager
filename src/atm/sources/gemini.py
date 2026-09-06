"""Gemini CLI 会话源：`~/.gemini/tmp/<项目标识>/chats/session-<时间>-<id 前8位>.jsonl`。

**逆向观察自本机真实语料**（gemini-cli 0.58.0，15 个会话文件），不是照文档写的。
观察到的事实，每一条都影响解析：

1. **两种文件格式并存**。旧版写单个 JSON 对象（`.json`），0.58 起写追加式的 `.jsonl`。
   `getAllSessionFiles` 两种都收，所以我们也两种都认。

2. **`.jsonl` 里消息有两种形态**（这是文档看不出来、只有真数据才有的）：
   第 1 行是元数据；之后既有 `{"$set": {"messages": [...]}}` 这样的批量写入，
   也有**裸消息记录** `{"id","timestamp","type","content"}` 直接追加。
   只认 `$set` 会漏掉用户后面所有的话 —— 本机那个会话里真正的第一句就在裸记录里。

3. **首条 user 消息是注入的 `<session_context>`**（带闭合标签，几 KB 的目录树），
   不是用户说的话。拿它当标题会让整列都是同一段环境说明。

4. **`content` 可能是字符串（旧）也可能是 parts 数组（新）**：`[{"text": "..."}]`。

5. **cwd 不在会话文件里**。目录名是 `~/.gemini/projects.json` 注册表里的**可读标识**
   （`/home/sean` → `sean`），不是哈希；老版本用的是 sha256 目录名，注册表里没有。
   所以 cwd 按两条路找：注册表反查目录名，再退回 `<session_context>` 里的
   `Workspace Directories:`。两条都不中就**丢弃这条会话** —— gemini 的 resume 是按项目
   隔离的（`listSessions` 只看当前项目的 chats 目录），cwd 不对根本恢复不了，
   与其列出来让用户白按一次，不如不列。实测本机 14 条旧会话正是这种：
   它们的目录名是 Windows 路径的哈希（用户从 Windows 迁过来的），在 Linux 上无从恢复。

6. **`projectHash` 是 cwd 绝对路径的 sha256**（实测 `sha256("/home/sean")` 与文件里的值一致）。
   拿它当校验：注册表给出的 cwd 与之对不上就不信任，退回上下文那条路。

7. **resume 必须用完整 UUID**。`findSession` 是 `session.id === identifier` 精确比对
   （或纯数字当序号），不做前缀匹配。所以元数据里没有 sessionId 时宁可丢弃，
   也不要拿文件名里那 8 位十六进制拼一个 —— 那个 id 一定 resume 不了。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path

from ..jsonl import iter_head_records, read_head_bytes
from ..model import UNTITLED, FileRef, SessionEntry, Source
from ..text import clean_title, is_junk_prompt

DEFAULT_ROOT = Path.home() / ".gemini" / "tmp"

# 注册表和 tmp 同级：~/.gemini/projects.json
REGISTRY_NAME = "projects.json"

_MAX_RECORDS = 400

# 完整 UUID 才能 resume（见模块文档第 7 条）。
_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# `<session_context>` 里的工作目录：`- **Workspace Directories:**` 之后第一条缩进路径。
_WORKSPACE_RE = re.compile(r"Workspace Directories:\*\*\s*\n\s*-\s*(?P<path>\S+)")

# 旧格式是单个 JSON 对象，必须整读才能解析。超过这个大小就只当它有元数据、不取标题 ——
# 本机最大的旧会话 1.5MB，8MB 留了五倍余量，同时挡住病态大文件（Claude 那边见过 648MB）。
_MAX_WHOLE_JSON_BYTES = 8 * 1024 * 1024


def load_registry(root: Path | None = None) -> dict[str, str]:
    """`projects.json` 反查表：目录标识 → 项目绝对路径。读不了就返回空表。

    正表是 `{"/home/sean": "sean"}`，我们要的是反过来。同一个标识理论上只对一个路径。
    """
    base = root or DEFAULT_ROOT
    path = base.parent / REGISTRY_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    projects = raw.get("projects") if isinstance(raw, dict) else None
    if not isinstance(projects, dict):
        return {}
    return {
        identifier: cwd
        for cwd, identifier in projects.items()
        if isinstance(cwd, str) and isinstance(identifier, str)
    }


def discover(root: Path | None = None) -> Iterator[FileRef]:
    """列出所有会话文件。目录不存在就安静返回空。

    固定两层 `*/chats/session-*`：`tmp/<项目>/` 下面还有 `logs/` 和 `tool-outputs/`
    （上游 `RESERVED_SESSION_DIR_NAMES`），递归下去只会捞进不是会话的东西。
    """
    base = root or DEFAULT_ROOT
    if not base.is_dir():
        return
    for pattern in ("*/chats/session-*.jsonl", "*/chats/session-*.json"):
        for path in base.glob(pattern):
            ref = FileRef.from_path(path)
            if ref is not None and ref.size_bytes > 0:
                yield ref


def parse(ref: FileRef, registry: dict[str, str] | None = None) -> SessionEntry | None:
    path = Path(ref.path)
    meta, messages = _read(path, ref.size_bytes)
    if meta is None:
        return None

    session_id = _text_of(meta.get("sessionId"))
    if not _ID_RE.match(session_id):
        return None  # 见模块文档第 7 条：宁可不列，也不要给一个 resume 不了的 id

    title = ""
    context = ""
    for message in messages:
        text = _message_text(message)
        if not text:
            continue
        if not context and text.lstrip().startswith("<session_context>"):
            context = text
            continue
        if not title and not is_junk_prompt(text):
            title = clean_title(text)
        if title and context:
            break

    cwd = _resolve_cwd(
        identifier=path.parent.parent.name,
        registry=registry if registry is not None else load_registry(),
        project_hash=_text_of(meta.get("projectHash")),
        context=context,
    )
    if not cwd:
        return None  # 见模块文档第 5 条

    return SessionEntry(
        id=session_id,
        title=title or UNTITLED,
        source=Source.GEMINI,
        cwd=cwd,
        git_branch=None,  # 会话文件里没有 git 信息
        updated_at=ref.updated_at,
        path=ref.path,
        size_bytes=ref.size_bytes,
        name=None,  # gemini 的 /chat save 标签存在别处，不在会话文件里
    )


def _resolve_cwd(
    *, identifier: str, registry: dict[str, str], project_hash: str, context: str
) -> str:
    """两条路找 cwd，都不中返回空串。

    注册表优先，但要过 `projectHash` 这一关：注册表是可变的（用户换过目录、标识撞名），
    而 projectHash 是写会话时算的 sha256，两者矛盾时信文件里的那个。
    """
    from_registry = registry.get(identifier, "")
    if from_registry and (not project_hash or _sha256(from_registry) == project_hash):
        return from_registry

    match = _WORKSPACE_RE.search(context)
    if match is not None:
        candidate = match.group("path")
        if not project_hash or _sha256(candidate) == project_hash:
            return candidate
    return ""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(path: Path, size_bytes: int) -> tuple[dict | None, list[dict]]:
    """(元数据, 消息列表)。读不出元数据就返回 (None, [])。"""
    if path.suffix == ".jsonl":
        return _read_jsonl(path)
    return _read_json(path, size_bytes)


def _read_jsonl(path: Path) -> tuple[dict | None, list[dict]]:
    meta: dict | None = None
    messages: list[dict] = []
    for index, record in enumerate(iter_head_records(path)):
        if index >= _MAX_RECORDS:
            break
        if meta is None:
            meta = record
            continue
        # 见模块文档第 2 条：批量写入和裸消息记录两种形态都要收
        batch = record.get("$set")
        if isinstance(batch, dict):
            found = batch.get("messages")
            if isinstance(found, list):
                messages.extend(m for m in found if isinstance(m, dict))
        elif record.get("type") is not None:
            messages.append(record)
    return meta, messages


def _read_json(path: Path, size_bytes: int) -> tuple[dict | None, list[dict]]:
    """旧格式：整个文件一个 JSON 对象。

    太大就只从头部抠元数据 —— `sessionId` / `projectHash` 实测都排在 `messages` 前面，
    头部窗口足够。标题拿不到就让它显示未命名，总比整条会话消失强。
    """
    if size_bytes <= _MAX_WHOLE_JSON_BYTES:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        if isinstance(raw, dict):
            found = raw.get("messages")
            return raw, [m for m in found if isinstance(m, dict)] if isinstance(found, list) else []

    head = read_head_bytes(path).decode("utf-8", errors="replace")
    meta = {
        key: match.group(1)
        for key in ("sessionId", "projectHash")
        if (match := re.search(rf'"{key}"\s*:\s*"([^"]+)"', head))
    }
    return (meta or None), []


def _message_text(message: dict) -> str:
    """只认 `type == "user"` 的消息。gemini / info / error 三类不是用户说的话。"""
    if message.get("type") != "user":
        return ""
    return _content_to_text(message.get("content"))


def _content_to_text(content: object) -> str:
    """见模块文档第 4 条：字符串（旧）或 parts 数组（新）。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n".join(parts)


def _text_of(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""
