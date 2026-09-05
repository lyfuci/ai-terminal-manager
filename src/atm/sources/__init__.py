"""会话源适配器。

每个源只做两件事：`discover()` 列出文件，`parse()` 把一个文件变成 SessionEntry。
两者都必须能容忍格式变化 —— 这两个 jsonl 的字段是逆向观察出来的，不是公开契约。
"""

from . import claude, codex, pi

__all__ = ["claude", "codex", "pi"]
