"""atm 的核心数据类型。

跨模块边界传递的一律是这里的具名类型，不传裸 dict。
全部 frozen —— 要改就 `dataclasses.replace` 生成新对象，不原地改。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

UNTITLED = "(untitled)"


class Source(StrEnum):
    """会话来自哪个 AI CLI。"""

    CLAUDE = "claude"
    CODEX = "codex"
    PI = "pi"


# 列表里那两个字符的来源标记。放在 model 里是因为 cli / tui / sidebar_tui 三处都要用，
# 而 cli.py 刻意不 import tui（tui 会在 import 时拉 curses）。
SOURCE_TAG: dict[Source, str] = {
    Source.CLAUDE: "CC",
    Source.CODEX: "CX",
    Source.PI: "PI",
}


@dataclass(frozen=True, slots=True)
class FileRef:
    """一个待解析的会话文件，以及用来判断缓存是否失效的指纹。

    用 (mtime_ns, size) 而不是内容 hash：2.1GB 的语料每次算 hash 是不可接受的，
    而 CLI 只会往 jsonl 尾部追加，mtime+size 足以识别「变了没」。
    """

    path: str
    mtime_ns: int
    size_bytes: int

    @classmethod
    def from_path(cls, path: Path | str) -> FileRef | None:
        """stat 失败（文件刚被删/权限不足）返回 None，不抛 —— 扫描不能因为一个文件挂掉。"""
        try:
            # 刻意用 os.stat 而不是 Path.stat()：这行在冷启动时要跑 1500+ 次，
            # 而调用方大多已经拿着 str 路径，不必为了风格再构造一个 Path 对象。
            st = os.stat(path)  # noqa: PTH116
        except OSError:
            return None
        return cls(path=str(path), mtime_ns=st.st_mtime_ns, size_bytes=st.st_size)

    @property
    def fingerprint(self) -> str:
        return f"{self.mtime_ns}:{self.size_bytes}"

    @property
    def updated_at(self) -> datetime:
        return datetime.fromtimestamp(self.mtime_ns / 1e9, tz=UTC)


@dataclass(frozen=True, slots=True)
class SessionEntry:
    """侧栏里的一条会话。

    `id` 是可以直接喂给 `claude --resume` / `codex resume` 的那个 id。
    `cwd` 是会话原本的工作目录 —— 恢复时必须先 cd 回去，两个 CLI 的会话都是项目作用域的。
    """

    id: str
    title: str
    source: Source
    cwd: str
    git_branch: str | None
    updated_at: datetime
    path: str
    size_bytes: int
    # 用户手动给会话起的名字（Claude 的 `custom-title` 记录）。
    # 与 title 的区别：title 是**推断**出来的（ai-title / 首条消息），name 是**人取**的，
    # 所以在列表里要单独、显眼地展示，不能和推断标题混在一起。
    name: str | None = None

    @property
    def project_name(self) -> str:
        """cwd 的最后一段，用来在列表里做分组显示。"""
        return Path(self.cwd).name or self.cwd

    def haystack(self) -> str:
        """模糊搜索的匹配文本 —— 标题 + 项目 + 分支 + 来源都能搜到。"""
        parts = [self.title, self.cwd, self.source.value]
        if self.name:
            parts.append(self.name)
        if self.git_branch:
            parts.append(self.git_branch)
        return " ".join(parts)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "source": self.source.value,
            "cwd": self.cwd,
            "gitBranch": self.git_branch,
            "updatedAt": self.updated_at.isoformat(),
            "path": self.path,
            "sizeBytes": self.size_bytes,
            "name": self.name,
        }

    @classmethod
    def from_json(cls, data: dict) -> SessionEntry:
        return cls(
            id=data["id"],
            title=data["title"],
            source=Source(data["source"]),
            cwd=data["cwd"],
            git_branch=data.get("gitBranch"),
            updated_at=datetime.fromisoformat(data["updatedAt"]),
            path=data["path"],
            size_bytes=int(data.get("sizeBytes", 0)),
            name=data.get("name"),
        )


@dataclass(frozen=True, slots=True)
class IndexStats:
    """一次索引构建的统计 —— `atm index --stats` 直接打这个，性能声明要有实测数字。"""

    total: int
    from_cache: int
    parsed: int
    skipped: int
    elapsed_ms: int
    # 缓存有没有成功写回。False 说明下次还是全量解析 —— 必须让用户看得见，
    # 否则缓存目录只读时只会表现为「怎么一直这么慢」而无从诊断。
    cache_written: bool = True

    def describe(self) -> str:
        base = (
            f"{self.total} sessions "
            f"({self.from_cache} cached, {self.parsed} parsed, {self.skipped} skipped) "
            f"in {self.elapsed_ms}ms"
        )
        return base if self.cache_written else base + "  ⚠️ 缓存写入失败，下次仍是冷启动"


@dataclass(frozen=True, slots=True)
class SessionIndex:
    """构建好的索引。entries 按 updated_at 倒序（最近的在前）。"""

    entries: tuple[SessionEntry, ...]
    stats: IndexStats
