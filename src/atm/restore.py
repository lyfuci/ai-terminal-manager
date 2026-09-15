"""`atm restore`：把上次那些会话一键填回**已经恢复出来的空格子**里。

## 它解决什么

tmux-resurrect 重启后能把布局搭回来（窗口、分格、每格 cwd），但格子里是空 shell。
把 AI CLI 加进 `@resurrect-processes` 能让 resurrect 自己拉起它们，可那是**开机时全砸下去**：
2026-08-12 就是这么把机器冻死的（四个会话合计吃掉 87% 内存）。所以 atm 一直不用那条路。

代价是用户得一个一个手动 resume。窗口一多就很烦 —— 这个命令就是补这个缺口。

## 为什么 atm 做比 resurrect 做安全

- **只填空闲的格子。** 格子里在跑东西就跳过，绝不覆盖你正在用的会话。
- **串行，不并发。** 三个 claude 同时读各自 20MB+ 的转录会造成尖峰；一条一条来最坏只是慢。
- **走 atm 正常的投递路径**，所以每条都套上 cgroup 内存闸门，也会做 cwd 存在性检查和体积警告。
- **默认先给你看计划**，确认了才动手。

## 数据从哪来

复用 resurrect 已经在写的存档（默认每 10 分钟一次），不再造第二份状态。
格式是**逆向观察**的（仓库硬规则第 4 条），所以解析全程「拿不到就跳过这一行」，
字段数变了、列错位了都只是少恢复几条，不会让整条命令炸掉。

实测的 pane 行（制表符分隔）：

    pane  main  1  1  :*  1  ✳ github  :/home/sean  1  claude  :/home/…/claude --resume <uuid>
     0     1    2  3   4  5      6           7      8     9                  10

第 10 列记的是**用户实际敲的**命令行，所以两种写法都得认：atm 自己投递的 `claude --resume <uuid>`，
和手敲的 `claude -r github`（短参数 + 会话名）。名字在规划时回查索引（`resolve`）。
2026-09-15 真机上的 claude 格子全是后一种（或没带恢复参数的 `claude agents`），atm 只认前一种，
于是每份存档都解析出 0 条 —— 用户第三次重启后没能恢复。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import config as config_mod
from . import tmux
from .dispatch import RESUME_PROGRAMS, DispatchError, DispatchTarget, MemoryLimit, dispatch
from .i18n import _
from .model import SessionEntry, Source
from .sources import claude as claude_source
from .sources import pi as pi_source
from .tmux import Pane

_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
_MEMINFO = Path("/proc/meminfo")

# 存档里 pane 行至少要有这么多列才敢解析
_MIN_FIELDS = 11
_SESSION_FIELD, _WINDOW_FIELD, _PANE_FIELD = 1, 2, 5
_COMMAND_FIELD = 10
_ACTIVE_VALUES = ("0", "1")

# 恢复参数的短写法。RESUME_PROGRAMS 记的是 atm 投递时用的长写法，存档里却是用户手敲的原样。
# 实测 `claude --help`：`-r, --resume [value]`；`gemini --help`：`-r, --resume`；
# `opencode --help`：`-s, --session`。codex 是子命令 `resume`，没有短写法；pi 本机没装，不猜。
_SHORT_FLAGS: dict[Source, str] = {
    Source.CLAUDE: "-r",
    Source.GEMINI: "-r",
    Source.OPENCODE: "-s",
}


@dataclass(frozen=True, slots=True)
class SavedPane:
    """存档里一行，且能从中认出一条可恢复的会话。"""

    target: str  # "main:1.2"，和 tmux 的目标写法一致
    session: str
    window: str
    pane: str
    title: str
    cwd: str
    source: Source
    # 恢复参数后面那个词：atm 投递的是 id，手敲的常常是会话名（`claude -r github`）
    session_id: str
    # 恢复参数后面的**整段**。存档里引号已经丢了（`claude -r "my project"` 记成 `-r my project`），
    # 所以只凭第一个词判断不了名字到哪为止 —— `name_reference` 靠它判断后面还有没有别的词。
    ref_tail: str = ""


@dataclass(frozen=True, slots=True)
class Item:
    """一条恢复计划。`state` 决定它会不会被执行。"""

    saved: SavedPane
    pane_id: str | None  # 对应的活格子；None = 布局里已经没有这一格
    entry: SessionEntry | None  # 认准的那条会话；None = 没找到或认不准
    state: str  # ready / occupied / no-pane / no-session / ambiguous / unclear
    # 同名会话不止一条时的全部候选（state == "ambiguous"），列给人自己挑
    candidates: tuple[SessionEntry, ...] = ()

    @property
    def ready(self) -> bool:
        return self.state == "ready"


def save_path() -> Path:
    from . import persist

    return persist.save_dir() / "last"


def _pane_columns(fields: list[str]) -> tuple[str, str, str] | None:
    """一行 pane 的 (标题, cwd, 当时的程序名)。只认下面两种结构，别的返回 None。

    正常行：      `… 5:pane  6:标题  7::cwd  8:active  9:程序  10::命令行`
    标题为空的行：`… 5:pane  6::cwd  7:active  8:程序  9:pid  10::命令行`

    后一种是真机语料（resurrect 3.4）：空标题那列没了，程序后面多出一列 pid，总列数不变。
    平时没有标题的空 shell 格子就是这样；2026-09-15 server 退出途中写的存档里连 claude 格子也是
    （推测是进程正在退、标题已被清空，没有单独验证）。按整组结构判断，而不是只看某一列的前缀。
    """
    if len(fields) < _MIN_FIELDS or not fields[_COMMAND_FIELD].startswith(":"):
        return None
    if fields[7].startswith(":") and fields[8] in _ACTIVE_VALUES:
        return fields[6], fields[7][1:], fields[9]
    if fields[6].startswith(":") and fields[7] in _ACTIVE_VALUES and fields[9].isdigit():
        return "", fields[6][1:], fields[8]
    return None


def parse_save(text: str) -> tuple[SavedPane, ...]:
    """从存档文本里挑出「能恢复的会话」。认不出的行安静跳过。"""
    out: list[SavedPane] = []
    for line in text.splitlines():
        fields = line.split("\t")
        if fields[0] != "pane":
            continue
        columns = _pane_columns(fields)
        if columns is None:
            continue
        found = _session_of(fields[_COMMAND_FIELD].lstrip(":"))
        if found is None:
            continue
        source, session_id, ref_tail = found
        session = fields[_SESSION_FIELD]
        window = fields[_WINDOW_FIELD]
        pane = fields[_PANE_FIELD]
        if not (session and window and pane):
            continue
        title, cwd, _program = columns
        out.append(
            SavedPane(
                target=f"{session}:{window}.{pane}",
                session=session,
                window=window,
                pane=pane,
                title=title,
                cwd=cwd,
                source=source,
                session_id=session_id,
                ref_tail=ref_tail,
            )
        )
    return tuple(out)


def _session_of(command: str) -> tuple[Source, str, str] | None:
    """命令行 → (来源, 恢复参数后第一个词, 恢复参数后整段)。

    `/path/to/claude --resume <id>` → (Source.CLAUDE, id, id)。
    手敲的也认：短写法 `claude -r my project` → (Source.CLAUDE, "my", "my project")，
    以及 `--resume=<值>`。遇到 `--` 就停：之后是传给程序的位置参数，不是恢复参数。

    恢复参数各家不一样（`--resume` / `resume` / `--session`），统一从 RESUME_PROGRAMS 反查，
    这样加了新来源这里不用改；短写法见 `_SHORT_FLAGS`。
    """
    words = command.split()
    if len(words) < 2:
        return None
    program = PurePosixPath(words[0]).name
    for source, argv in RESUME_PROGRAMS.items():
        if argv[0] != program:
            continue
        long_flag = argv[1]
        flags = {long_flag, _SHORT_FLAGS.get(source, long_flag)}
        for index, word in enumerate(words[1:], start=1):
            if word == "--":
                break
            rest = words[index + 1 :]
            if long_flag.startswith("--") and word.startswith(f"{long_flag}="):
                value = word[len(long_flag) + 1 :]
                if value:
                    return source, value, " ".join([value, *rest])
                continue
            # `claude -r --verbose`：-r 没带值（打开选择器），后面是别的参数，不是会话
            if word in flags and rest and not rest[0].startswith("-"):
                return source, rest[0], " ".join(rest)
    return None


def count_ai_panes(text: str) -> int:
    """存档里在跑 AI CLI 的格子数，不管认不认得出会话。doctor 拿它和 `parse_save` 的条数对比。

    命令行列和程序列**任一**是 AI CLI 就算：server 退出途中写的存档里，有的格子命令行已经丢了
    （被挤成了单独一行），程序列却还是 claude —— 只看命令行会漏掉它，doctor 就会误报「都认得出」。
    """
    programs = {argv[0] for argv in RESUME_PROGRAMS.values()}
    count = 0
    for line in text.splitlines():
        fields = line.split("\t")
        if fields[0] != "pane" or len(fields) < _MIN_FIELDS:
            continue
        words = fields[_COMMAND_FIELD].lstrip(":").split()
        names = {PurePosixPath(words[0]).name} if words else set()
        columns = _pane_columns(fields)
        if columns is not None:
            names.add(columns[2])
        if names & programs:
            count += 1
    return count


def newest_restorable_save(directory: Path, *, skip: Path | None = None) -> Path | None:
    """目录里最新的一份「解析得出会话」的历史存档。没有就是 None。

    `last` 会被每次自动存档替换：重启后格子还空着时再存一次，新的 `last` 里就没有 AI 会话了，
    `atm restore` 读它只能得到 0 条 —— 而重启前那份还原封不动地躺在目录里。
    只用来**提示**，不自动换：哪份存档对应用户想要的那一刻，只有用户知道。
    """
    try:
        files = sorted(directory.glob("tmux_resurrect_*.txt"), reverse=True)  # 文件名里是时间戳
    except OSError:
        return None
    skipped = skip.resolve() if skip is not None else None
    for path in files:
        try:
            if path.resolve() == skipped:
                continue
            # 坏字节替换掉而不是抛 UnicodeDecodeError：一份写坏的旧存档不能让后面的都不检查
            if parse_save(path.read_text(encoding="utf-8", errors="replace")):
                return path
        except OSError:
            continue
    return None


def matches(saved: SavedPane, target: str | None) -> bool:
    """`-t` 过滤。None = 全收；`main` = 整个会话；`main:1` = 那个窗口。"""
    if not target:
        return True
    session, _, window = target.partition(":")
    if session and saved.session != session:
        return False
    return not window or saved.window == window


def latest_raw_name(entry: SessionEntry) -> str | None:
    """整份会话文件里**最后一次**改名的原文。这个来源没有会话名、或文件读不到，就是 None。

    索引只读头 256KB 和尾部，中间发生的改名它看不见，`raw_name` 可能是过期的旧名字 ——
    两个会话互换过名字时，拿过期名字认身份会恢复错会话（2026-09-15 codex 复核第四轮实测）。
    所以按名字认会话时，同一来源、同一 cwd 的会话都用它整份重扫一遍；
    没有会话名的来源（codex / gemini / opencode）直接返回 None，不读文件。
    """
    if entry.source is Source.CLAUDE:
        return claude_source.latest_raw_name(entry.path)
    if entry.source is Source.PI:
        return pi_source.latest_raw_name(entry.path)
    return None


def resolve(
    saved: SavedPane,
    entries: Collection[SessionEntry],
    *,
    latest_names: dict[tuple[Source, str], str | None] | None = None,
) -> tuple[SessionEntry, ...]:
    """存档里的会话引用 → 索引里的会话。空 = 没找到；一条 = 认准了；多条 = 同名认不准。

    1. **当 id 查**，来源必须一致：claude 的引用不能落到恰好同 id 的别家条目上。
    2. **当会话名查**（`/rename`、`claude -n` 起的）：引用见 `name_reference`，
       只在同一来源、**同一个 cwd** 里找（`claude -r <名字>` 自己就只找当前项目目录）。
       比的是每个会话文件里**最后一次**改名的原文（`latest_raw_name`，整份重扫），
       **不用索引里的名字**：索引只读头尾，中间改过名就过期了。过期名字既会把
       已经改名的会话错认成它，也会漏掉改成这个名字的会话，让同名冲突看上去唯一
       （2026-09-15 codex 复核第四、五轮）。
    3. **同名不止一条就全部交回**，由调用方报「认不准」。不按更新时间挑：那比的是**现在**的
       更新时间，存档之后才动过的另一个同名会话会被错选（2026-09-15 codex 复核指出）。

    这里交回一条就会被当成 ready 直接投递（开机模式没人确认），所以宁可认不出，不能认错。
    `claude -r <搜索词>` 在 claude 里是「带搜索词开选择器」，不是精确名字 —— 查不到就是查不到。
    """
    for entry in entries:
        if entry.id == saved.session_id and entry.source is saved.source:
            return (entry,)
    name = name_reference(saved)
    if name is None or not saved.cwd:
        return ()
    cwd = os.path.normpath(saved.cwd)
    # 同一次规划里多个格子共用这张表：每个会话文件最多扫一遍
    latest = {} if latest_names is None else latest_names
    matched: list[SessionEntry] = []
    for entry in entries:
        if entry.source is not saved.source or os.path.normpath(entry.cwd) != cwd:
            continue
        key = (entry.source, entry.id)
        if key not in latest:
            latest[key] = latest_raw_name(entry)
        if latest[key] == name:
            matched.append(entry)
    return tuple(matched)


def name_reference(saved: SavedPane) -> str | None:
    """能拿去当会话名比对的引用：恢复参数后面**只有这一个词**时才算，否则 None。

    存档里的命令行已经丢了参数边界（引号没了，连续空格也被合并），所以 `-r` 后面只要还跟着
    别的东西，就分不清名字到哪为止（2026-09-15 codex 复核，前两版修法都被它举出反例）：

    - `claude -r my project`：可能是名字 `my` 加 prompt `project`，也可能是名字 `my project`；
    - `claude -r github --verbose`：`--verbose` 可能是选项，
      也可能是名字 `"github --verbose"` 的一部分。

    认错的代价是把别的会话投进格子（开机模式没人确认），认不出的代价只是让人手动指定一次。
    所以带空格的名字、后面还跟着参数的名字一律不自动认，由调用方报 `unclear`。
    """
    words = saved.ref_tail.split() if saved.ref_tail else [saved.session_id]
    return words[0] if len(words) == 1 else None


def build_plan(
    saved: tuple[SavedPane, ...],
    panes: tuple[Pane, ...],
    entries: Collection[SessionEntry],
    *,
    target: str | None = None,
) -> tuple[Item, ...]:
    """把存档、活着的格子、会话索引三者对上，得出每一条的状态。

    `entries` 是索引里的全部条目，**不要先按 id 建字典**：不同来源可能有同一个 id，
    字典会让后一个覆盖前一个 —— 被覆盖的那条要是正好同名，同名冲突就被藏成了「唯一」，
    恢复错会话（2026-09-15 codex 复核第六轮）。条目身份一律是 (来源, id)。
    """
    live = {f"{p.session}:{p.window_index}.{p.pane_index}": p for p in panes}
    latest_names: dict[tuple[Source, str], str | None] = {}
    items: list[Item] = []
    for entry_saved in saved:
        if not matches(entry_saved, target):
            continue
        pane = live.get(entry_saved.target)
        found = resolve(entry_saved, entries, latest_names=latest_names)
        session = found[0] if len(found) == 1 else None
        if pane is None:
            state = "no-pane"
        elif len(found) > 1:
            state = "ambiguous"  # 同名不止一条：不替用户挑
        elif session is None and name_reference(entry_saved) is None:
            state = "unclear"  # 参数边界丢了，分不清会话名到哪为止
        elif session is None:
            state = "no-session"
        elif not pane.is_idle_shell:
            state = "occupied"  # 里面在跑东西，绝不覆盖
        else:
            state = "ready"
        items.append(
            Item(
                saved=entry_saved,
                pane_id=pane.id if pane else None,
                entry=session,
                state=state,
                candidates=found if len(found) > 1 else (),
            )
        )
    return tuple(items)


def describe(items: tuple[Item, ...]) -> str:
    """给人看的计划。每一条都说清楚为什么做或不做。"""
    if not items:
        return _("存档里没有可以恢复的会话。")
    reasons = {
        "occupied": _("跳过：这个格子里已经在跑东西了"),
        "no-pane": _("跳过：布局里没有这一格了"),
        "no-session": _("跳过：索引里找不到这条会话（按 id 和会话名都查过）"),
        "ambiguous": _("跳过：同名会话不止一条，认不准是哪条 —— 用 atm resume <id> 指定："),
        "unclear": _(
            "跳过：恢复参数后面还跟着别的词，存档里的引号已经丢了，分不清会话名到哪为止 —— "
            "用 atm resume <id> 指定"
        ),
    }
    ready = [i for i in items if i.ready]
    lines = [_("将恢复 {n} 条会话：").format(n=len(ready))] if ready else []
    for item in ready:
        title = item.entry.title if item.entry else item.saved.title
        lines.append(f"  {item.saved.target}  {item.saved.source.value:9} {title}")
    skipped = [i for i in items if not i.ready]
    if skipped:
        lines.append("")
        lines.append(_("以下 {n} 条不动：").format(n=len(skipped)))
        for item in skipped:
            label = item.saved.title or item.saved.ref_tail
            lines.append(f"  {item.saved.target}  {label}  —— {reasons[item.state]}")
            for entry in item.candidates:
                # 纯占位符，没有可翻译的自然语言，所以不过 _()
                lines.append(f"      {entry.id}  {entry.updated_at:%m-%d %H:%M}  {entry.title}")
    return "\n".join(lines)


def execute(
    items: tuple[Item, ...],
    *,
    memory: MemoryLimit | None = None,
    floor: int | None = None,
    meminfo: Path = _MEMINFO,
    on_done: Callable[[int], None] | None = None,
) -> list[str]:
    """按计划逐条投递。**串行**，一条失败不影响后面的。返回每条的结果说明。

    `floor` 是可用内存下限（字节）：每投一条之前看一眼 MemAvailable，掉到下限以下就停手，
    剩下的留给用户自己挑。开机恢复靠这条把「一次性全拉起来」压成「拉到内存吃紧为止」。
    `on_done` 每投完一条调一次（传已投条数），开机模式用它把进度写进状态文件。
    """
    notes: list[str] = []
    done = 0
    for item in items:
        if not item.ready or item.entry is None or item.pane_id is None:
            continue
        if floor is not None:
            have = mem_available(meminfo)
            if have is not None and have < floor:
                notes.append(
                    _("  可用内存只剩 {have}，剩下的先不恢复了。").format(have=_human(have))
                )
                break
        try:
            dispatch(
                item.entry,
                DispatchTarget.existing(item.pane_id),
                focus=False,  # 别把光标抢走：恢复完用户还在原来那格
                memory=memory,
            )
        except DispatchError as exc:
            notes.append(
                _("  {target} 失败：{err}").format(
                    target=item.saved.target, err=str(exc).splitlines()[0]
                )
            )
        else:
            # 纯占位符 + 箭头，没有可翻译的自然语言，所以不过 _()
            notes.append(f"  {item.saved.target} ← {item.entry.title}")
        # 失败的也算「处理过」：状态文件记的是「走到哪了」，不是「成功了几条」。
        done += 1
        if on_done is not None:
            on_done(done)
    return notes


def current_session() -> str | None:
    """当前 attach 的会话名。不在 tmux 里就是 None。"""
    if not tmux.inside_tmux():
        return None
    try:
        return tmux.run(["display-message", "-p", "#{session_name}"]).strip() or None
    except tmux.TmuxError:
        return None


# ---------------------------------------------------------------- 开机自动恢复的闸门
#
# `restore.on-boot` 打开后，resurrect 的 post-restore-all 钩子会调 `atm restore --boot`。
# 这是仓库「不让 resurrect 在开机时批量拉起 AI CLI」那条禁令的边上走 —— 所以必须比它安全，
# 而不是换个地方犯同一个错（2026-08-12：四个会话同时起来吃掉 87% 内存，冻死两次）。
#
# 已经有的三层保护：串行投递、每条套 cgroup 闸门、所有会话共用一个总量 slice。
# 这里再加两层，都是**不用 root、跨重启也成立**的：
#
# 1. **上一次开机恢复有没有跑完。** 投递前先把「计划恢复 N 条」写进状态文件，每投一条 +1。
#    机器被压死时进程是被杀掉的，计数停在半路。下次开机看到 done < planned 就不自动跑了 ——
#    这条专门用来打断「恢复→冻死→重启→再恢复」的循环。
# 2. **可用内存够不够。** 开始前和每条投递前都读一次 MemAvailable，低于 restore.min-available
#    就停手。不管上一次是怎么死的，内存已经紧张时就不该再往里塞。
#
# 为什么不查 journal 里的 OOM 记录：普通用户不在 adm / systemd-journal 组时，
# `journalctl` **只看得到自己的日志**，内核的 oom-kill 是系统消息，查出来永远是「没有」。
# 一个只会返回「一切正常」的检查比没有检查更糟，所以不做。
# 同理 `systemctl --user show tmux.service -p Result` 只在同一次开机内有意义 ——
# user manager 每次登录重建，跨重启读到的是新一轮的结果。


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / "atm"


def state_path() -> Path:
    return state_dir() / "boot-restore.json"


def log_path() -> Path:
    """开机恢复没有终端，做了什么只能写进文件。`atm doctor` 会指这里。"""
    return state_dir() / "restore.log"


def boot_id(path: Path = _BOOT_ID) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def mem_available(meminfo: Path = _MEMINFO) -> int | None:
    """MemAvailable，字节。读不到就是 None —— 拿不到数就不拿它当拒绝的理由。"""
    try:
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


@dataclass(frozen=True, slots=True)
class Attempt:
    """一次开机恢复的痕迹。`done < planned` = 上次没跑完。"""

    boot_id: str
    at: str
    planned: int
    done: int

    @property
    def finished(self) -> bool:
        return self.done >= self.planned


def read_attempt(path: Path | None = None) -> Attempt | None:
    p = path or state_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return Attempt(
            boot_id=str(raw["boot_id"]),
            at=str(raw["at"]),
            planned=int(raw["planned"]),
            done=int(raw["done"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None  # 没有痕迹 / 文件坏了 = 当作没跑过，闸门的其它几条还在


def write_attempt(attempt: Attempt, path: Path | None = None) -> None:
    p = path or state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "boot_id": attempt.boot_id,
                    "at": attempt.at,
                    "planned": attempt.planned,
                    "done": attempt.done,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass  # 写不下痕迹不该挡住恢复本身；代价只是下次少一层保护


@dataclass(frozen=True, slots=True)
class Gate:
    ok: bool
    reason: str


def boot_gate(
    cfg,
    *,
    state: Path | None = None,
    meminfo: Path = _MEMINFO,
    limits_available: bool | None = None,
) -> Gate:
    """开机自动恢复到底跑不跑。每种拒绝都给一句能照着做的理由。"""
    if not cfg.restore_on_boot:
        return Gate(False, _("restore.on-boot 是关的（`atm config restore.on-boot true` 打开）。"))

    from . import dispatch as dispatch_mod

    available = (
        dispatch_mod.memory_limits_available() if limits_available is None else limits_available
    )
    if not available:
        return Gate(
            False,
            _("这台机器拿不到 cgroup 内存闸门，开机批量恢复没有兜底，不自动跑。"),
        )

    last = read_attempt(state)
    if last is not None and not last.finished:
        return Gate(
            False,
            _(
                "上次开机恢复只跑完 {done}/{planned} 条就中断了（多半是内存不够被杀）。"
                "这次不自动恢复，手动跑一次 `atm restore` 确认没问题后，"
                "删掉 {path} 就会重新自动恢复。"
            ).format(done=last.done, planned=last.planned, path=state or state_path()),
        )

    floor = config_mod.size_to_bytes(cfg.restore_min_available)
    have = mem_available(meminfo)
    if floor is not None and have is not None and have < floor:
        return Gate(
            False,
            _("可用内存只剩 {have}，低于 restore.min-available={want}，这次不自动恢复。").format(
                have=_human(have), want=cfg.restore_min_available
            ),
        )
    return Gate(True, _("闸门通过。"))


def _human(n: int) -> str:
    gib = 1 << 30
    return f"{n / gib:.1f}G" if n >= gib else f"{n / (1 << 20):.0f}M"


def append_log(lines: list[str], path: Path | None = None) -> None:
    """开机恢复的流水账。没有终端的时候这是唯一的现场。"""
    p = path or log_path()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(f"{stamp}  {line}\n")
    except OSError:
        pass
