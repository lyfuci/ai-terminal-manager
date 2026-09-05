"""atm — AI Terminal Manager。

把 Claude Code 和 Codex 的历史会话合成一份统一列表，选一条投递到指定的 tmux pane。

范围刻意收得很窄（见 research/notes/survey-existing-tools.md）：
布局这块 tmux 生态和官方 Claude Code Desktop 已经吃掉了，不重复造。
唯一没人做全的是「跨 agent 统一历史 → 投到我指定的那个格子」，atm 只做这一件事。
"""

from .model import IndexStats, SessionEntry, SessionIndex, Source

__version__ = "0.3.0"

__all__ = ["IndexStats", "SessionEntry", "SessionIndex", "Source", "__version__"]
