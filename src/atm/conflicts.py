"""找出 `~/.tmux.conf` 里**块外**同样在设 atm 管着的那几个 tmux 选项的行。

## 为什么需要这个模块

atm 把自己的 tmux 选项写在一对 marker 之间。用户如果在 `atm config` 里**明确开启**了某一项，
他要的就是 atm 的值生效，这时块外的同名行不是「用户的覆盖」而是陈年重复，会让 atm 说假话：

    用户的 ~/.tmux.conf：set -g mouse on     （早于 atm）
    $ atm config tmux.mouse false
    → atm 删掉自己那行，报告「已写入」，而用户那行还在，鼠标依然是开的。

## 为什么只报告、不代劳

两条更主动的路实测都不成立：

1. **把 atm 的块挪到文件末尾，靠「后来者胜」赢。** 实测 tmux 3.4 会**同时加载**
   `~/.tmux.conf` 和 `~/.config/tmux/tmux.conf`，后者在**后面**。
2. **把冲突行注释掉由 atm 接管。** 除了违反「不碰用户自己的内容」，还会误伤
   `bind-key R { ... set -g history-limit 50000 ... }` 里的行。

更根本的是：tpm 用 `run-shell -b` 加载插件，实测那个后台任务在整份配置读完**之后**才跑，
它设的选项压过配置里的任何一行。**没有任何行位置能保证赢**，所以不承诺赢。

## 为什么要做词法分析，不能逐行正则

第一版是逐行正则，评审当场找出会**误报**的输入 —— 而误报比漏报糟得多，它会让用户去删一行
其实属于别的命令的配置：

    bind-key m \\
        set -g mouse on        ← 第 2 行单看就是顶层设置，实际是 bind-key 的参数
    bind-key R {
        display-message "}"    ← 引号里的 } 被当成闭合，后面的行就被误判成顶层
        set -g mouse on
    }
    set -t mouse status off    ← `-t mouse` 是**目标**，选项其实是 status

所以这里按 tmux 的语法走一遍：拼接反斜杠续行 → 逐字符跟踪引号/转义/注释 →
按未被引号包住的 `;` `{` `}` 切命令 → 解析命令名和标志。宁可漏报，绝不误报。

## 识别的边界

- 命令名认 `set` / `setw` 以及 `set-option` / `set-window-option` 的无歧义前缀（tmux 自己允许）
- `-t` 会吃掉下一个 token（那是目标，不是选项名）
- `-u`（取消设置）单独标出来：它清掉值，但不等于「把这项打开了」
- `-o`（已设过就不覆盖）标为不确定：生效与否取决于此前有没有设过
- 花括号命令块和 `%if` 里的记为不确定 —— 它们未必在当下生效
- atm 自己三个块里的行跳过
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# atm 自己的三个块。写在这里而不是 import 三个模块：这个模块被 doctor 和 tmuxopts 共用，
# 不该为了两个常量把 install/persist 拖进 import 链。test_conflicts 有对齐检查。
KEYS_BLOCK = ("# >>> atm (ai-terminal-manager) >>>", "# <<< atm <<<")
PERSIST_BLOCK = (
    "# >>> atm persist (tmux-resurrect + tmux-continuum) >>>",
    "# <<< atm persist <<<",
)
OPTIONS_BLOCK = ("# >>> atm tmux options (atm config tmux.*) >>>", "# <<< atm tmux options <<<")
ATM_BLOCKS: tuple[tuple[str, str], ...] = (KEYS_BLOCK, PERSIST_BLOCK, OPTIONS_BLOCK)

# set-option 的标志里只有 -t 带参数（目标）。其余是字母簇，可以连写（-gq、-wg…）。
_FLAG_WITH_ARG = "t"


@dataclass(frozen=True, slots=True)
class Conflict:
    """块外一条命令，设的是 atm 当前管着的某个选项。"""

    line_no: int  # 命令**起始**的物理行，从 1 开始，和编辑器一致
    text: str  # 原样的那一行（续行只显示第一行），去掉首尾空白
    option: str  # tmux 的选项名
    key: str  # 对应 atm config 的键
    certain: bool  # False = 在花括号块 / %if / 带 -o，未必生效
    unsets: bool  # True = 带 -u，是「取消设置」而不是「设成某值」
    after_atm_block: bool  # 在 atm 的选项块之后（tmux 后来者胜，所以它压过 atm）


def scan(text: str, options: dict[str, str]) -> tuple[Conflict, ...]:
    """扫描配置文本。`options`：tmux 选项名 → atm 配置键，只报这些。"""
    if not options:
        return ()
    block_end = _options_block_end(text)
    found: list[Conflict] = []
    for cmd in _commands(text):
        parsed = _parse_set(cmd.words, options)
        if parsed is None:
            continue
        option, flags = parsed
        found.append(
            Conflict(
                line_no=cmd.line_no,
                text=cmd.text,
                option=option,
                key=options[option],
                certain=cmd.depth == 0 and cmd.conditional == 0 and "o" not in flags,
                unsets="u" in flags,
                after_atm_block=block_end is None or cmd.line_no > block_end,
            )
        )
    return tuple(found)


# ---------------------------------------------------------------- 词法


@dataclass(frozen=True, slots=True)
class _Command:
    line_no: int
    text: str
    words: tuple[str, ...]
    depth: int  # 命令开始时的花括号深度
    conditional: int  # 命令开始时的 %if 深度


def _commands(text: str) -> list[_Command]:
    """把配置切成一条条命令，跳过 atm 自己的块。

    做三件逐行正则做不到的事：拼接反斜杠续行、认引号、按未被引号包住的 `;` `{` `}` 切分。
    """
    out: list[_Command] = []
    depth = 0
    conditional = 0
    in_block = False
    end_marker = ""

    for line_no, logical, display in _logical_lines(text):
        stripped = logical.strip()

        if in_block:
            if stripped == end_marker:
                in_block = False
            continue
        opened = next((end for begin, end in ATM_BLOCKS if stripped == begin), None)
        if opened is not None:
            in_block, end_marker = True, opened
            continue

        if stripped.startswith("%if"):
            conditional += 1
            continue
        if stripped.startswith("%endif"):
            conditional = max(0, conditional - 1)
            continue
        if stripped.startswith(("%else", "%elif")):
            continue  # 深度不变，仍在条件里

        commands, depth = _split_commands(logical, depth)
        for words, depth_at_start in commands:
            out.append(
                _Command(
                    line_no=line_no,
                    text=display,
                    words=tuple(words),
                    depth=depth_at_start,
                    conditional=conditional,
                )
            )
    return out


def _logical_lines(text: str):
    """(起始行号, 拼接后的逻辑行, 用于显示的原始首行)。

    行尾的 `\\` 是续行 —— tmux 会把下一行接上去。第一版没做这件事，于是
    `bind-key m \\` 换行接 `set -g mouse on` 里的第二行被当成了顶层设置。
    """
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        start = i
        parts = [lines[i]]
        while _ends_with_continuation(lines[i]) and i + 1 < len(lines):
            i += 1
            parts.append(lines[i])
        # 拼接时把续行符换成空格，保持 token 边界
        joined = " ".join(p[:-1] if _ends_with_continuation(p) else p for p in parts)
        yield start + 1, joined, lines[start].strip()
        i += 1


def _ends_with_continuation(line: str) -> bool:
    """行尾是不是一个**未被转义**的反斜杠。"""
    if not line.endswith("\\"):
        return False
    trailing = len(line) - len(line.rstrip("\\"))
    return trailing % 2 == 1


def _split_commands(logical: str, depth: int) -> tuple[list[tuple[list[str], int]], int]:
    """把一条逻辑行切成若干命令。返回 ([(words, 命令开始时的深度)], 这行结束后的深度)。

    引号内的 `;` `{` `}` `#` 一律不算 —— `display-message "}"` 那个误判就出在这里。
    """
    commands: list[tuple[list[str], int]] = []
    words: list[str] = []
    current = ""
    has_current = False
    start_depth = depth
    single = double = False
    escaped = False
    at_word_start = True

    def end_word() -> None:
        nonlocal current, has_current, at_word_start
        if has_current:
            words.append(current)
            current, has_current = "", False
        at_word_start = True

    def end_command() -> None:
        nonlocal words, start_depth
        end_word()
        if words:
            commands.append((words, start_depth))
            words = []
        start_depth = depth

    for ch in logical:
        if escaped:
            current += ch
            has_current, at_word_start, escaped = True, False, False
            continue
        if ch == "\\" and not single:
            escaped = True
            continue
        if single:
            if ch == "'":
                single = False
            else:
                current += ch
            continue
        if double:
            if ch == '"':
                double = False
            else:
                current += ch
            continue
        if ch in "'\"":
            single, double = ch == "'", ch == '"'
            has_current, at_word_start = True, False
            continue
        if ch == "#" and at_word_start:
            break  # 注释到逻辑行末尾
        if ch in " \t":
            end_word()
            continue
        if ch == ";":
            end_command()
            continue
        if ch in "{}":
            end_command()
            depth = max(0, depth + (1 if ch == "{" else -1))
            start_depth = depth
            continue
        current += ch
        has_current, at_word_start = True, False

    end_command()
    return commands, depth


# ---------------------------------------------------------------- 命令解析


def _is_set_command(word: str) -> bool:
    """tmux 允许无歧义前缀：`set-o` `set-op` … 都是 set-option。"""
    if word in ("set", "setw"):
        return True
    for full in ("set-option", "set-window-option"):
        if len(word) >= 5 and full.startswith(word):
            return True
    return False


def _parse_set(words: tuple[str, ...], options: dict[str, str]) -> tuple[str, set[str]] | None:
    """→ (选项名, 标志字母集合)，不是我们关心的 set 命令就返回 None。"""
    if not words or not _is_set_command(words[0]):
        return None
    flags: set[str] = set()
    i = 1
    while i < len(words) and words[i].startswith("-") and words[i] != "-":
        letters = words[i][1:]
        flags.update(letters)
        i += 1
        if _FLAG_WITH_ARG in letters:
            i += 1  # -t 后面那个是目标，不是选项名
    if i >= len(words):
        return None
    name = words[i]
    return (name, flags) if name in options else None


# ---------------------------------------------------------------- 块位置 / 其它来源


def _options_block_end(text: str) -> int | None:
    """atm 选项块结束在第几行（1 起）。没有这个块就返回 None。"""
    begin, end = OPTIONS_BLOCK
    seen = False
    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped == begin:
            seen = True
        elif seen and stripped == end:
            return line_no
    return None


def other_config_files(conf_path: Path) -> tuple[Path, ...]:
    """tmux 还会读、但 atm 不管的配置文件（存在的才返回）。

    实测 tmux 3.4：`~/.tmux.conf` 和 `~/.config/tmux/tmux.conf` **两个都读**，
    且后者在后面 —— 它里面的同名设置会盖掉 atm 写的那份。
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    candidates = [root / "tmux" / "tmux.conf", Path("/etc/tmux.conf")]
    return tuple(p for p in candidates if p != conf_path and p.is_file())


def sourced_files(text: str) -> tuple[str, ...]:
    """配置里 `source-file` 引进来的路径。atm 不跟进去读，但要说「那里面我没看」。"""
    out: list[str] = []
    for cmd in _commands(text):
        head = cmd.words[0]
        if head == "source" or (len(head) >= 6 and "source-file".startswith(head)):
            args = [w for w in cmd.words[1:] if not w.startswith("-")]
            if args:
                out.append(args[0])
    return tuple(dict.fromkeys(out))
