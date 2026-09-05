"""Claude Code 会话源：`~/.claude/projects/<cwd 把 '/' 换成 '-'>/<sessionId>.jsonl`。

实测事实（本机 2026-08-12，Claude Code 2.1.228，1459 个文件 / 2.1GB）：

- **没有 `type:"summary"` 记录。** 抽样 120 个文件的尾部，一条都没有。
  项目 README 早前写的「从 summary 行提标题」在当前版本上不成立。
- **`type:"ai-title"` 存在但覆盖率只有 2%**（150 个抽样里 3 个）—— 是 2.1.x 的新功能，老会话没有。
  它出现得很早（首次出现行号中位数 11，p90 14），所以只读头部就能拿到。
- **真正的标题主力是「首条非 isMeta 的 user 消息」，覆盖 94%**（150 个抽样里 141 个）。
- `cwd` / `gitBranch` 挂在普通消息记录上，覆盖率同样 94%。
- 剩下 6% 是空的/中断的会话，给 UNTITLED 兜底。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from ..jsonl import DEFAULT_HEAD_BYTES, iter_head_records, iter_tail_records
from ..model import UNTITLED, FileRef, SessionEntry, Source
from ..text import clean_title, is_junk_prompt

DEFAULT_ROOT = Path.home() / ".claude" / "projects"

# 头部扫这么多条就停 —— 再往后翻不会有新信息，只会拖慢冷启动。
_MAX_RECORDS = 400


def discover(root: Path | None = None) -> Iterator[FileRef]:
    """列出所有**可以 resume 的**会话文件。目录不存在就安静返回空。

    ⚠️ 这里的 `*/*.jsonl` 只扫一层，是**故意的，别改成递归**。
    实测本机 1459 个 jsonl 的分布：

    - 127 个在 `<project>/<sessionId>.jsonl` —— 真正能 `claude --resume` 的会话；
    - 1332 个在 `<project>/<sessionId>/{subagents,workflows,tool-results}/...` ——
      是某个会话的子 agent / workflow / 工具结果**产物**，没有独立的 sessionId，
      resume 不了。（127 + 1332 = 1459，实测无遗漏。）

    改成 `**/*.jsonl` 会让侧栏里多出 1332 条点了没反应的假条目。
    """
    base = root or DEFAULT_ROOT
    if not base.is_dir():
        return
    for path in base.glob("*/*.jsonl"):
        ref = FileRef.from_path(path)
        if ref is not None and ref.size_bytes > 0:
            yield ref


def decode_project_dir(name: str) -> str:
    """把 `-home-user-IdeaProjects-foo` 还原成 `/home/user/IdeaProjects/foo`。

    这是**有损**的 —— 目录名里本来就有的 '-' 和路径分隔符的 '-' 无法区分。
    所以只当 cwd 的兜底，记录里读到真的 cwd 一律优先。
    """
    if not name.startswith("-"):
        return name
    return "/" + name[1:].replace("-", "/")


def parse(ref: FileRef) -> SessionEntry | None:
    """解析一个会话文件的头部，拿不到有用信息就返回 None（调用方跳过它）。"""
    session_id = ""
    title = ""
    name = ""
    cwd = ""
    git_branch: str | None = None
    saw_any = False

    # 文件小到头窗口已经覆盖全文时，就不必再读一次尾部了。
    # 实测 127 个 Claude 会话里 102 个 ≤256KB —— 之前对这 80% 的文件
    # 白白多 open 一次、把整个文件重新 json.loads 一遍。
    head_covers_all = ref.size_bytes <= DEFAULT_HEAD_BYTES

    for index, record in enumerate(iter_head_records(ref.path)):
        # 头窗口覆盖全文时不设记录数上限：既然字节数已经有界，
        # 再卡 400 条只会让「改名发生在第 401 条」这种情况漏掉。
        if not head_covers_all and index >= _MAX_RECORDS:
            break
        saw_any = True

        if not session_id:
            candidate = record.get("sessionId")
            if isinstance(candidate, str) and candidate:
                session_id = candidate
        if not cwd:
            candidate = record.get("cwd")
            if isinstance(candidate, str) and candidate:
                cwd = candidate
        if git_branch is None:
            candidate = record.get("gitBranch")
            if isinstance(candidate, str) and candidate:
                git_branch = candidate

        record_type = record.get("type")

        # custom-title 是**用户手动起的名字**（`/rename` 之类），最高优先级的身份标识。
        # 实测字段：{"type":"custom-title","customTitle":"sample-project",...}
        if record_type == "custom-title":
            candidate = record.get("customTitle")
            if isinstance(candidate, str) and candidate.strip():
                name = clean_title(candidate, limit=40)  # 后写的覆盖先写的 = 取最后一次改名

        # ai-title 是 CLI 自己生成的标题，质量最高（后写的比先写的更贴合会话最终内容）。
        elif record_type == "ai-title":
            candidate = record.get("aiTitle")
            if isinstance(candidate, str) and candidate.strip():
                title = clean_title(candidate)

        elif record_type == "user" and not title and not record.get("isMeta"):
            text = _user_text(record)
            if text and not is_junk_prompt(text):
                title = clean_title(text)

        # 该拿的都拿到了就早退 —— 大文件上这一步省掉大量解析。
        #
        # ⚠️ **不要把 name 加进这个条件**：custom-title 只有改过名的会话才有，
        # 实测 127 个文件里带它的只有 10 个 —— 要求 name 会让另外 117 个
        # 每次都白扫满 400 条记录。改名最终由尾部扫描兜底（见下），这里不需要它。
        # 头窗口覆盖全文时也不早退，因为要取最后一条 title/name。
        if not head_covers_all and title and cwd and session_id and git_branch is not None:
            break

    # 头窗口一条记录都解析不出来（整窗是一条超长行）时**不能直接 return None** ——
    # 那会让一条正常会话从列表里彻底消失。文件非空就仍然产出条目，
    # 靠文件名/目录名兜底，至少还能被看到和 resume。
    # （当前语料实测 0 个触发，属于防御性处理。）
    if not saw_any and ref.size_bytes <= 0:
        return None

    # 头部扫完再扫尾部。有两类信息是「会话进行中才产生、且会反复重写」的，
    # 只读头部要么完全漏掉、要么拿到过期版本：
    #   - custom-title：用户改名。实测 14 个命名会话里 4 个的改名在头部窗口之外
    #     （包括一条叫 ⟨wsl⟩ 的，用户在列表里怎么都找不到）；
    #     还有 1 个改过两次名，头部拿到的是旧名字。
    #   - ai-title：CLI 自己生成的标题，后写的比先写的更贴合会话最终内容。
    # 两者都取**最后一条**。
    # 头窗口没覆盖全文时才需要额外读尾部。
    if not head_covers_all:
        tail_name, tail_title = _scan_tail(ref.path)
        name = tail_name or name
        title = tail_title or title

    path = Path(ref.path)
    if not session_id:
        session_id = path.stem
    if not cwd:
        cwd = decode_project_dir(path.parent.name)

    return SessionEntry(
        id=session_id,
        title=title or UNTITLED,
        source=Source.CLAUDE,
        cwd=cwd,
        git_branch=git_branch,
        updated_at=ref.updated_at,
        path=ref.path,
        size_bytes=ref.size_bytes,
        name=name or None,
    )


def _scan_tail(path: str) -> tuple[str, str]:
    """扫文件尾部，返回 (最后的 custom-title, 最后的 ai-title)，没有就是空串。"""
    name = ""
    title = ""
    for record in iter_tail_records(path):
        record_type = record.get("type")
        if record_type == "custom-title":
            candidate = record.get("customTitle")
            if isinstance(candidate, str) and candidate.strip():
                name = clean_title(candidate, limit=40)
        elif record_type == "ai-title":
            candidate = record.get("aiTitle")
            if isinstance(candidate, str) and candidate.strip():
                title = clean_title(candidate)
    return name, title


def _user_text(record: dict) -> str:
    """从一条 user 记录里抽出纯文本。

    content 可能是字符串，也可能是 block 数组；数组里混着 tool_result 之类的非文本块，
    只取 text 块。取不到就返回空串。
    """
    message = record.get("message")
    if not isinstance(message, dict):
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
