"""标题清洗 + 终端宽度计算。

标题的来源是「用户的第一条消息」，那里面混着大量不是标题的东西
（slash command 的包装、系统注入的 caveat、tool_result 回灌）。
这里负责把它们挡掉 —— 实测下来这是标题质量的决定因素。
"""

from __future__ import annotations

import re
import unicodedata

# 这些开头的内容不是用户真正打的话，是 CLI / 系统注入的包装。
# 实测来源：`<local-command-caveat>` 出现在几乎每个带 slash command 的 Claude 会话首条 user 记录里
# （那条虽然带 isMeta:true，但不是每个版本都带，所以这里再挡一层）。
_JUNK_PREFIX = re.compile(
    r"^\s*<(?:"
    r"local-command-caveat|local-command-stdout|local-command-stderr"
    r"|command-name|command-message|command-args"
    r"|system-reminder|user_instructions|environment_context|editor_context"
    r"|task-notification|function_results|bash-input|bash-stdout|bash-stderr"
    r")\b",
    re.IGNORECASE,
)

_CAVEAT_PREFIX = re.compile(r"^\s*Caveat:\s*The messages below were generated", re.IGNORECASE)

# Claude 会把 hook 输出、粘贴的大块内容包成这种形式，同样不是标题。
_PASTED_PREFIX = re.compile(r"^\s*\[(?:Image|Pasted text|Request interrupted)", re.IGNORECASE)

_WHITESPACE = re.compile(r"\s+")

# 清洗标题前先把输入截到这个长度。标题最终只要 120 列，没必要让正则啃完整条消息。
# 取 32KB 是为了不破坏「剥离开头的注入包装」—— 实测 Codex 塞的那块是 10,865 字，
# 32KB 留了三倍余量；超过这个长度的开头包装本来也不该当标题。
_MAX_TITLE_SCAN = 32 * 1024

# 开头的 XML 式标签。上面那张名单是「已知的」注入包装，但两边的 CLI 都在不停加新的
# （实测 Codex 0.147 会在真正的用户消息前塞一个 10865 字的
# `<recommended_plugins>…</environment_context>`），所以不能只靠枚举。
_LEADING_TAG = re.compile(r"^\s*<([A-Za-z][\w:.-]*)(?:\s[^>]*)?>")

# 注入包装前面**带一行 Markdown 标题**的形状。实测 Codex 注入 AGENTS.md 时是：
#
#     # AGENTS.md instructions for /home/sean/IdeaProjects/sample-backend
#
#     <INSTRUCTIONS>… 8645 字 …</INSTRUCTIONS>
#
# 标题行挡在前面，`_LEADING_TAG` 匹配不上，整块剥不掉，于是这条注入本身成了会话标题
# —— 再被噪音前缀规则当成机器调用隐藏掉。实测有一条 625KB 的会话就是这么消失的，
# 它里面有 4 条真人消息（「现在设置的默认模型是 gpt-5.6-sol 对吗」）。
#
# 只在标题行**紧跟着**一个包装标签时才吃掉它 —— 单独的标题行是正常内容，不能动。
_HEADING_BEFORE_TAG = re.compile(r"^\s*#[^\n]*\n\s*(?=<[A-Za-z][\w:.-]*(?:\s[^>]*)?>)")
_MAX_STRIP_ROUNDS = 8


def strip_wrapper_blocks(text: str) -> str:
    """反复剥掉开头被注入的 `<tag>…</tag>` 块，返回剩下的真实内容。

    比枚举标签名更耐用：CLI 加了新的包装标签也不用改代码。
    只剥**开头**的块 —— 用户正文里出现的尖括号不受影响。
    """
    current = text
    for _ in range(_MAX_STRIP_ROUNDS):
        heading = _HEADING_BEFORE_TAG.match(current)
        if heading is not None:
            current = current[heading.end() :]
        match = _LEADING_TAG.match(current)
        if match is None:
            break
        closing = f"</{match.group(1)}>"
        end = current.find(closing, match.end())
        if end == -1:
            # 没有闭合标签 —— **保留原文**，不能当包装丢掉。
            # 之前这里 return ""，于是任何以尖括号 token 开头的正常提问
            # （`<div class="x"> 这个标签为什么…`、`<T> 泛型怎么写`）整条被判成噪音、
            # 从列表里消失。真正的注入包装有固定标签名，交给 _JUNK_PREFIX 名单去挡。
            break
        current = current[end + len(closing) :].lstrip()
    return current


def is_junk_prompt(text: str) -> bool:
    """这段文本是不是「系统注入的包装」而不是用户真正说的话。"""
    if not text or not text.strip():
        return True
    if _CAVEAT_PREFIX.match(text) or _PASTED_PREFIX.match(text) or _JUNK_PREFIX.match(text):
        return True
    # 剥完包装什么都不剩，说明整条消息都是注入的。
    return not strip_wrapper_blocks(text).strip()


def clean_title(text: str, *, limit: int = 120) -> str:
    """把一段多行的用户消息压成一行标题。

    limit 按**显示宽度**截断而不是字符数 —— 中文标题占双宽，
    按字符截会让列表右边参差不齐。
    """
    if not text:
        return ""
    # 先截到有界长度再做正则 —— 实测输入最长 60,749 字符，而产出只要 120 列。
    text = text[:_MAX_TITLE_SCAN]
    # 再剥包装：`<recommended_plugins>…</environment_context>\n\n真正的问题` 这种，
    # 要的是后半截而不是前面那一大坨。
    stripped = strip_wrapper_blocks(text)
    collapsed = _WHITESPACE.sub(" ", (stripped or text).replace("　", " ")).strip()
    # 去掉 markdown 标题的前导 # —— 「# 项目背景」当标题时那个 # 是噪音
    collapsed = re.sub(r"^#{1,6}\s+", "", collapsed)
    return truncate_display(collapsed, limit)


def char_width(ch: str) -> int:
    """单个字符占几列。

    ⚠️ `unicodedata.combining()` **拦不住零宽字符**：它返回的是规范组合类，
    对 U+200D(ZWJ) / U+200B(ZWSP) / U+FE0F(VS16) / U+1F3FB-FF(肤色修饰符) 都是 0。
    实测 `display_width('👨‍👩‍👧‍👦')` 曾算出 11 列（终端实际渲染 2 列），
    导致标题被提前截断、pad_display 补位全错、整个列表右边参差不齐。
    所以这里改用 category 判定。
    """
    if ch == "\t":
        return 8
    code = ord(ch)
    if code < 32 or code == 0x7F:
        # C0 控制符。ncurses 会渲染成 `^X`（2 列），但那种内容进标题本身就是脏数据，
        # 上游 clean_title 已经折叠掉了；这里按 0 算并在 _put 侧统一剥离。
        return 0
    category = unicodedata.category(ch)
    if category in ("Mn", "Me", "Cf"):
        # Mn/Me：组合记号；Cf：格式字符（ZWJ / ZWSP / VS16 / 方向控制）—— 都不占列
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def display_width(text: str) -> int:
    """终端里这段文本占几列。"""
    return sum(char_width(ch) for ch in text)


def truncate_display(text: str, limit: int, *, ellipsis: str = "…") -> str:
    """按显示宽度截断，超出时补省略号（省略号本身也占宽度）。

    **单趟扫描、超出即停** —— 之前先 `display_width(text)` 扫完整串只为回答
    「超没超 limit」，再对每个字符重扫一遍。输入是**未截断的整条用户消息**
    （实测最长 60,749 字符），而输出只要 120 列，于是 99.8% 的扫描是白做的。
    实测这一处占冷启动的 38~41%。
    """
    if limit <= 0:
        return ""

    ell_w = display_width(ellipsis)
    budget = max(0, limit - ell_w)

    width = 0
    cut = None  # 达到 budget 时的切点
    for index, ch in enumerate(text):
        width += char_width(ch)
        if cut is None and width > budget:
            cut = index
        if width > limit:
            # 确认真的超了才截断；只到 budget 还不够，因为可能刚好等于 limit
            return text[:cut] + ellipsis
    return text


def pad_display(text: str, width: int) -> str:
    """右侧补空格到指定显示宽度（已超宽则先截断）。"""
    truncated = truncate_display(text, width)
    return truncated + " " * max(0, width - display_width(truncated))


def humanize_age_ago(seconds: float) -> str:
    """Claude Code 自己的 resume 选择器用的格式：`just now` / `5m ago` / `2h ago`。

    格式来源是从 claude 二进制里 `strings` 出来的 UI 文案（`just now` / `s ago` / `d ago`），
    保持一致是为了让两个列表看起来是同一个东西。
    """
    if seconds < 60:
        return "just now"
    return f"{humanize_age(seconds)} ago"


def humanize_age(seconds: float) -> str:
    """把「多久以前」压成 4 字符以内的相对时间，列表里比绝对时间戳好扫。"""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return "now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h"
    days = hours / 24
    if days < 7:
        return f"{int(days)}d"
    weeks = days / 7
    if weeks < 5:
        return f"{int(weeks)}w"
    months = days / 30
    if months < 12:
        return f"{int(months)}mo"
    return f"{int(days / 365)}y"


def tail_display(text: str, limit: int, marker: str = "…") -> str:
    """保留**末尾** limit 列，头部截掉（和 truncate_display 反过来）。

    用在输入行上：光标永远在末尾，被截掉的必须是已经打完的前半段，
    否则用户看到的是「开头几个字 + 一片截断」，正在敲的字反而不可见。
    """
    if limit <= 0:
        return ""
    width = 0
    kept: list[str] = []
    for ch in reversed(text):
        w = char_width(ch)
        if width + w > limit:
            room = limit - display_width(marker)
            if room <= 0:
                return marker[:limit]
            # 重新从尾部收，给 marker 腾位置
            width, kept = 0, []
            for c in reversed(text):
                cw = char_width(c)
                if width + cw > room:
                    break
                width += cw
                kept.append(c)
            return marker + "".join(reversed(kept))
        width += w
        kept.append(ch)
    return text
