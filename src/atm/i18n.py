"""CLI 消息的国际化：源码里的字符串是中文（键），按语言查表翻译。

为什么键是中文而不是英文 id：这个项目的所有消息一开始就是中文写的，300 多条。给每条起一个
`MSG_DOCTOR_NO_TMUX` 这种 id 再回填，等于把每个调用点都重写一遍，且以后每加一条消息要改两处。
用源字符串当键，加消息只需要写一次；翻译漏了就原样显示中文，永远不会崩、不会显示 id。

语言选择（第一个命中的生效）：
1. `ATM_LANG`（`en` / `zh` / `ja`）
2. `LC_ALL` → `LC_MESSAGES` → `LANG`（取前两个字母；`C` / `POSIX` → en）
3. 都没设：`locale.getlocale()`；还是认不出 → en

只翻 CLI 输出、`--help`、TUI 提示、错误信息。`--json` 的字段名本来就是英文，日志（`-v`）不翻。
"""

from __future__ import annotations

import os

SUPPORTED = ("en", "zh", "ja")
_lang_cache: str | None = None


def lang() -> str:
    global _lang_cache
    if _lang_cache is None:
        _lang_cache = _detect()
    return _lang_cache


def reset_lang() -> None:
    """测试用：环境变量改了之后重新探测。"""
    global _lang_cache
    _lang_cache = None


def _detect() -> str:
    forced = os.environ.get("ATM_LANG", "").strip().lower()
    if forced:
        return forced if forced in SUPPORTED else "en"
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "").strip()
        if not value:
            continue
        if value in ("C", "POSIX") or value.startswith(("C.", "POSIX.")):
            return "en"
        code = value[:2].lower()
        return code if code in SUPPORTED else "en"
    # 环境变量全空（某些 Windows 终端 / 服务里起的进程）：问一下 Python 的 locale 兜底
    try:
        import locale

        sys_locale = (locale.getlocale()[0] or "")[:2].lower()
    except (ValueError, TypeError):
        sys_locale = ""
    return sys_locale if sys_locale in SUPPORTED else "en"


def tr(message: str) -> str:
    """翻译一条消息。没有翻译就原样返回（中文源串）。"""
    current = lang()
    if current == "zh":
        return message
    from .i18n_catalog import CATALOG

    return CATALOG.get(current, {}).get(message, message)


_ = tr
