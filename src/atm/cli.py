"""atm 命令行入口。

设计原则：**每个子命令在不在 tmux 里都能用**。
不在 tmux 里时 `pick` 退化成打印命令（可以 `eval "$(atm pick --print)"`），
而不是直接报错 —— 研究阶段大量时间是在裸终端里。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from . import cache as cache_mod
from . import dispatch as dispatch_mod
from . import index as index_mod
from . import tmux
from .dispatch import DispatchError, DispatchTarget, TargetKind
from .i18n import _
from .model import SOURCE_TAG, SessionEntry, SessionIndex, Source
from .text import humanize_age, pad_display, strip_control, truncate_display
from .tmux import Pane, SplitDirection

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CANCELLED = 130


LAUNCH_PROGRAMS = ("claude", "codex", "pi")


_VERBOSE_FLAGS = {"-v": 1, "-vv": 2, "--verbose": 1}


def _launch_args(argv: Sequence[str]) -> argparse.Namespace | None:
    """`atm claude …` 不走 argparse。

    REMAINDER 有个老毛病：第一个位置参数之前的 `--xxx` 仍会被当成 atm 自己的选项解析，
    `atm claude --resume x` 会报 unrecognized arguments。这里在 parse 之前把它截下来，
    程序名后面的每一个字节都原样归 claude / codex / pi。前面允许带 atm 自己的 -v。
    """
    verbose = 0
    i = 0
    while i < len(argv) and argv[i] in _VERBOSE_FLAGS:
        verbose += _VERBOSE_FLAGS[argv[i]]
        i += 1
    rest = argv[i:]
    if rest and rest[0] in LAUNCH_PROGRAMS:
        return argparse.Namespace(
            handler=_cmd_launch, program=rest[0], args=list(rest[1:]), verbose=verbose
        )
    return None


def _setup_logging(verbose: int) -> None:
    """-v → INFO，-vv → DEBUG，ATM_DEBUG=1 → DEBUG。全部走 stderr，不污染 --json 的 stdout。"""
    if os.environ.get("ATM_DEBUG"):
        verbose = max(verbose, 2)
    level = logging.WARNING if verbose == 0 else logging.INFO if verbose == 1 else logging.DEBUG
    logging.basicConfig(
        level=level, stream=sys.stderr, format="atm: %(levelname)s %(name)s: %(message)s"
    )


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    args = _launch_args(raw)
    if args is None:
        parser = _build_parser()
        args = parser.parse_args(raw)
    _setup_logging(getattr(args, "verbose", 0))
    try:
        return args.handler(args)
    except KeyboardInterrupt:
        return EXIT_CANCELLED
    except BrokenPipeError:
        # `atm list | head`：下游先关了管道，不是错。
        # 把 stdout 换成 devnull，免得解释器退出时再报一次。
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_OK
    except (DispatchError, ValueError, RuntimeError, OSError) as exc:
        # 预期内的失败（参数不合法 / 配置读不了 / tmux 没起 / 文件写不进去）只给一行人话。
        # RuntimeError 覆盖 ConfUnreadable / BackupFailed / TmuxError。要堆栈：ATM_DEBUG=1。
        if os.environ.get("ATM_DEBUG"):
            raise
        print(f"atm: {exc}", file=sys.stderr)
        return EXIT_ERROR


# ---------------------------------------------------------------- parser


def _tui():
    """延迟 import：curses 只有真正进 TUI 才需要。"""
    from . import tui

    return tui


def _install_mod():
    """延迟 import：只有 install/uninstall 两个子命令用得到。"""
    from . import install

    return install


def _persist_mod():
    from . import persist

    return persist


def _config_mod():
    from . import config

    return config


def _guard_mod():
    from . import guard

    return guard


def _update_mod():
    from . import update

    return update


def _sidebar_mod():
    from . import sidebar

    return sidebar


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atm",
        description=(
            _("AI Terminal Manager — Claude Code / Codex / Pi 的统一会话历史，投到指定 tmux pane")
        ),
    )
    from . import __version__

    parser.add_argument("--version", action="version", version=f"atm {__version__}")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help=_("多打点过程信息到 stderr（-v 概要，-vv 逐文件/逐条 tmux 命令；ATM_DEBUG=1 同 -vv）"),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_filters(p: argparse.ArgumentParser) -> None:
        p.add_argument("--source", choices=[s.value for s in Source], help=_("只看某个 CLI 的会话"))
        p.add_argument("--cwd", help=_("只看这个目录（含子目录）下的会话"))
        p.add_argument("--here", action="store_true", help=_("等价于 --cwd $PWD"))
        p.add_argument("--no-cache", action="store_true", help=_("强制重新解析，不读缓存"))
        p.add_argument(
            "--all",
            action="store_true",
            help=_("连机器生成的一次性调用也列出来（默认隐藏，如 `Below is a conversation log…`）"),
        )

    p_list = sub.add_parser("list", help=_("列出会话"))
    add_filters(p_list)
    p_list.add_argument("-n", "--limit", type=int, default=30, help=_("最多列几条（默认 30）"))
    p_list.add_argument("--json", action="store_true", help=_("输出 JSON"))
    p_list.set_defaults(handler=_cmd_list)

    p_pick = sub.add_parser("pick", help=_("交互选一条会话并投递（主命令）"))
    add_filters(p_pick)
    p_pick.add_argument("-q", "--query", default="", help=_("预填搜索词"))
    p_pick.add_argument(
        "--group",
        choices=[m.name.lower() for m in _tui().GroupMode],
        default="cwd",
        help=_("列表怎么聚合：cwd(目录，默认) / agent / none(平铺)。TUI 里 Ctrl-G 可循环切换"),
    )
    p_pick.add_argument("--pane", help=_("直接投到这个 pane（跳过选目标那一步）"))
    p_pick.add_argument("--split", action="store_true", help=_("投到新分出来的 pane"))
    p_pick.add_argument("--window", action="store_true", help=_("投到新 window"))
    p_pick.add_argument("--vertical", action="store_true", help=_("--split 时上下分而不是左右分"))
    p_pick.add_argument("--print", action="store_true", help=_("只打印命令，不投递"))
    p_pick.add_argument("--force", action="store_true", help=_("目标 pane 正忙也照投"))
    p_pick.add_argument(
        "--no-mem-limit",
        action="store_true",
        help=_("不给会话套 cgroup 内存闸门（默认套）"),
    )
    p_pick.add_argument("--mem-high", help=_("软上限，超了节流+回收不杀（默认取 atm config）"))
    p_pick.add_argument("--mem-max", help=_("硬上限，回收压不住才杀（默认取 atm config）"))
    p_pick.set_defaults(handler=_cmd_pick)

    p_resume = sub.add_parser("resume", help=_("按 id 直接投递（不进 TUI）"))
    p_resume.add_argument("session_id", help=_("会话 id（可以只写前缀）"))
    target = p_resume.add_mutually_exclusive_group()
    target.add_argument("--pane", help=_("投到这个 pane"))
    target.add_argument("--split", action="store_true", help=_("从当前 pane 分一格出来投"))
    target.add_argument("--window", action="store_true", help=_("新开 window 投"))
    target.add_argument("--print", action="store_true", help=_("只打印命令，不投"))
    p_resume.add_argument(
        "--vertical", action="store_true", help=_("配合 --split：上下分而不是左右分")
    )
    p_resume.add_argument("--force", action="store_true")
    p_resume.add_argument("--no-cache", action="store_true")
    p_resume.add_argument(
        "--no-mem-limit",
        action="store_true",
        help=_("不给会话套 cgroup 内存闸门（默认套）"),
    )
    p_resume.add_argument("--mem-high", help=_("软上限，超了节流+回收不杀（默认取 atm config）"))
    p_resume.add_argument("--mem-max", help=_("硬上限，回收压不住才杀（默认取 atm config）"))
    p_resume.set_defaults(handler=_cmd_resume, source=None, cwd=None, here=False)

    p_index = sub.add_parser("index", help=_("构建/查看索引"))
    p_index.add_argument("--rebuild", action="store_true", help=_("清缓存后全量重建"))
    p_index.add_argument("--json", action="store_true")
    p_index.set_defaults(handler=_cmd_index)

    p_panes = sub.add_parser("panes", help=_("列出 tmux pane"))
    p_panes.add_argument("--json", action="store_true")
    p_panes.set_defaults(handler=_cmd_panes)

    # ---- 常驻侧栏（2026-09-02）：运行中的格子 + 历史，选中就换进主格
    p_sidebar = sub.add_parser("sidebar", help=_("常驻侧栏：列出运行中的格子和历史，选中换进主格"))
    p_sidebar.add_argument(
        "--toggle",
        action="store_true",
        help=_("开/切/收侧栏：没开就在当前 window 最左边开一个；开了就切过去；焦点在侧栏上就收起"),
    )
    p_sidebar.add_argument(
        "--pane", help=_("--toggle 时「当前 pane」是哪个（键位绑定里传 #{pane_id}）")
    )
    p_sidebar.add_argument(
        "--width", type=int, default=_sidebar_mod().SIDEBAR_WIDTH, help=_("侧栏宽度（列）")
    )
    p_sidebar.add_argument(
        "--no-mem-limit", action="store_true", help=_("从侧栏恢复历史时不套内存闸门")
    )
    p_sidebar.add_argument("--mem-high", help=_("默认取 atm config"))
    p_sidebar.add_argument("--mem-max", help=_("默认取 atm config"))
    p_sidebar.set_defaults(handler=_cmd_sidebar)

    p_swap = sub.add_parser("swap", help=_("把某个 pane 换进当前 window 的主格（不进 TUI）"))
    p_swap.add_argument("pane_id", help=_("要换进来的 pane，形如 %%7"))
    p_swap.add_argument("--pane", help=_("「当前 pane」是哪个（默认 $TMUX_PANE）"))
    p_swap.add_argument(
        "--into",
        help=_("换进哪一格。默认：从普通格子里跑就是那格自己；从侧栏里跑就是主格（pane_last）"),
    )
    keep = p_swap.add_mutually_exclusive_group()
    keep.add_argument(
        "--discard",
        action="store_true",
        help=_("换完把旧格子关掉（默认只有空闲 shell 才自动关；这个会连跑着的进程一起杀）"),
    )
    keep.add_argument(
        "--keep", action="store_true", help=_("旧格子一律留在后台，空闲 shell 也不关")
    )
    p_swap.set_defaults(handler=_cmd_swap)

    p_park = sub.add_parser("park", help=_("把 pane 收进后台窗口 bg（进程继续跑）"))
    p_park.add_argument("pane_id", nargs="?", help=_("默认收当前 pane（$TMUX_PANE）"))
    p_park.set_defaults(handler=_cmd_park)

    p_prune = sub.add_parser(
        "prune", help=_("关掉后台窗口 bg 里空闲的 shell 格子（跑着东西的不动）")
    )
    p_prune.add_argument("-n", "--dry-run", action="store_true", help=_("只列出来，不关"))
    p_prune.set_defaults(handler=_cmd_prune)

    p_doctor = sub.add_parser("doctor", help=_("体检：数据源在不在、tmux 通不通"))
    p_doctor.add_argument("--json", action="store_true", help=_("机器可读输出"))
    p_doctor.set_defaults(handler=_cmd_doctor)

    p_install = sub.add_parser("install", help=_("装 tmux 键位绑定（会改 ~/.tmux.conf）"))
    p_install.add_argument("-y", "--yes", action="store_true", help=_("不问直接装"))
    # 这四个的 default 故意是 None：裸 `atm install` 在终端里跑会进问答向导，
    # 给了任何选项就按选项来、跳过向导。真正的默认值在 _resolve_install_args 里补。
    p_install.add_argument("--key", help=_("绑哪个键（默认 a；大写 = 只看当前目录的会话）"))
    p_install.add_argument(
        "--sidebar-key", help=_("侧栏开关键（默认 b；大写 = 把当前格子收进后台）")
    )
    p_install.add_argument("--width", help=_("浮层宽度（默认 80%%）"))
    p_install.add_argument("--height", help=_("浮层高度（默认 70%%）"))
    p_install.add_argument("--print", action="store_true", help=_("只看会写什么，不动文件"))
    p_install.add_argument("--conf", type=Path, help=_("tmux 配置文件路径（默认 ~/.tmux.conf）"))
    p_install.add_argument(
        "--no-persist",
        action="store_true",
        help=_("不装 tmux-resurrect / tmux-continuum（默认一起装，重启后能恢复窗口布局）"),
    )
    p_install.add_argument(
        "--no-slice",
        action="store_true",
        help=_("不写总量闸门的 systemd slice（默认写 ~/.config/systemd/user/atm-ai.slice）"),
    )
    p_install.set_defaults(handler=_cmd_install)

    p_update = sub.add_parser(
        "update", help=_("升级 atm 自己（按安装方式选 uv tool / pipx / pip）")
    )
    p_update.add_argument("--check", action="store_true", help=_("只看有没有新版本，不升级"))
    p_update.add_argument("--json", action="store_true", help=_("配合 --check：机器可读输出"))
    p_update.add_argument("-y", "--yes", action="store_true", help=_("不问直接升"))
    p_update.set_defaults(handler=_cmd_update)

    p_completion = sub.add_parser(
        "completion",
        help=_('打印 shell 补全脚本：eval "$(atm completion bash)"'),
    )
    p_completion.add_argument("shell", choices=("bash", "zsh", "fish"))
    p_completion.set_defaults(handler=_cmd_completion)

    p_config = sub.add_parser(
        "config",
        help=_("查看 / 修改用户配置（内存闸门参数）。不带参数在终端里进交互式编辑器"),
        description=(
            _(
                "atm config                       查看\n"
                "atm config memory.high 4G        设置\n"
                "atm config --unset memory.high   恢复默认\n"
                "atm config --reset               全部恢复默认"
            )
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_config.add_argument("key", nargs="?", help=_("配置项，如 memory.high"))
    p_config.add_argument("value", nargs="?", help=_("新值"))
    p_config.add_argument("--unset", metavar="KEY", help=_("把某项恢复默认"))
    p_config.add_argument("--reset", action="store_true", help=_("全部恢复默认（删掉配置文件）"))
    p_config.add_argument("--path", action="store_true", help=_("只打印配置文件路径"))
    p_config.add_argument("--json", action="store_true", help=_("机器可读：每项的值和来源"))
    p_config.add_argument(
        "--show", action="store_true", help=_("只打印当前配置（不进交互式编辑器）")
    )
    p_config.set_defaults(handler=_cmd_config)

    # `atm claude …` / `atm codex …` / `atm pi …`：带闸门启动。参数原样透传，
    # 所以这里不接管 -h —— `atm claude --help` 看到的是 claude 自己的帮助。
    for program in LAUNCH_PROGRAMS:
        # 只为了出现在 `atm --help` 里；真正的分发在 main() 的 _launch_args 里。
        p_launch = sub.add_parser(
            program,
            add_help=False,
            help=(
                _(
                    "按 atm config 的内存闸门启动 "
                    "{program}（参数原样透传；直接敲 {program} 则不受限）"
                ).format(program=program)
            ),
        )
        p_launch.add_argument("args", nargs=argparse.REMAINDER)
        p_launch.set_defaults(handler=_cmd_launch, program=program)

    p_uninstall = sub.add_parser("uninstall", help=_("从 ~/.tmux.conf 里移除 atm 的绑定"))
    p_uninstall.add_argument("-y", "--yes", action="store_true")
    p_uninstall.add_argument("--conf", type=Path, help=_("tmux 配置文件路径（默认 ~/.tmux.conf）"))
    p_uninstall.set_defaults(handler=_cmd_uninstall)

    return parser


# ---------------------------------------------------------------- commands


def _cmd_list(args: argparse.Namespace) -> int:
    entries = _load_entries(args)
    limited = entries[: args.limit] if args.limit and args.limit > 0 else entries
    if args.json:
        # 空也要是合法 JSON（`[]`），否则 `atm list --json | jq` 在没有会话的机器上直接炸。
        print(json.dumps([e.to_json() for e in limited], ensure_ascii=False, indent=2))
        return EXIT_OK
    if not entries:
        print(_("没有找到会话。跑 `atm doctor` 看看数据源在不在。"), file=sys.stderr)
        return EXIT_OK

    now = datetime.now(tz=UTC)
    for entry in limited:
        age = humanize_age((now - entry.updated_at).total_seconds())
        tag = SOURCE_TAG.get(entry.source, "??")
        branch = f" ({entry.git_branch})" if entry.git_branch else ""
        named = f"⟨{entry.name}⟩ " if entry.name else ""
        print(
            f"{pad_display(age, 4)} {tag} "
            f"{pad_display(strip_control(entry.project_name + branch), 26)} "
            f"{truncate_display(strip_control(named + entry.title), 80)}"
        )
        print(f"     {entry.id}")
    if len(limited) < len(entries):
        print(
            _("... 还有 {v0} 条（-n 调整）").format(v0=len(entries) - len(limited)), file=sys.stderr
        )
    return EXIT_OK


def _cmd_pick(args: argparse.Namespace) -> int:
    entries = _load_entries(args)
    if not entries:
        print(_("没有找到会话。跑 `atm doctor` 看看数据源在不在。"), file=sys.stderr)
        return EXIT_ERROR

    if not sys.stdin.isatty():
        print(_("atm pick 需要一个交互终端；非交互场景用 `atm list --json`。"), file=sys.stderr)
        return EXIT_ERROR

    def choose():
        return _tui().pick_session(
            entries,
            initial_query=args.query,
            mode=_tui().GroupMode[args.group.upper()],
            missing_cwds=dispatch_mod.missing_cwds(entries),
        )

    entry = _on_tty(choose)
    if entry is None:
        return EXIT_CANCELLED

    target = _resolve_target(args, entry)
    if target is None:
        return EXIT_CANCELLED
    return _do_dispatch(entry, target, force=args.force, memory=_memory_limit(args))


def _cmd_resume(args: argparse.Namespace) -> int:
    entries = _load_entries(args)
    matches = [e for e in entries if e.id == args.session_id]
    if not matches:
        matches = [e for e in entries if e.id.startswith(args.session_id)]

    if not matches:
        print(
            _("atm: 找不到会话 {args_session_id}").format(args_session_id=args.session_id),
            file=sys.stderr,
        )
        return EXIT_ERROR
    if len(matches) > 1:
        print(
            _("atm: {args_session_id} 匹配到 {v0} 条，请给更长的前缀：").format(
                args_session_id=args.session_id, v0=len(matches)
            ),
            file=sys.stderr,
        )
        for entry in matches[:10]:
            print(f"  {entry.id}  {truncate_display(entry.title, 60)}", file=sys.stderr)
        return EXIT_ERROR

    entry = matches[0]
    target = _resolve_target(args, entry, interactive=False)
    if target is None:
        return EXIT_CANCELLED
    return _do_dispatch(entry, target, force=args.force, memory=_memory_limit(args))


def _cmd_index(args: argparse.Namespace) -> int:
    if args.rebuild:
        cache_mod.clear()
    result = index_mod.build(use_cache=not args.rebuild)
    if args.json:
        stats = result.stats
        print(
            json.dumps(
                {
                    "total": stats.total,
                    "fromCache": stats.from_cache,
                    "parsed": stats.parsed,
                    "skipped": stats.skipped,
                    "elapsedMs": stats.elapsed_ms,
                    "cacheWritten": stats.cache_written,
                    "cachePath": str(cache_mod.default_cache_path()),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(result.stats.describe())
        print(f"cache: {cache_mod.default_cache_path()}")
        _print_source_breakdown(result)
    return EXIT_OK


def _on_tty(fn):
    """stdout 被 `$(…)` 或管道接走时，把 TUI 画到 /dev/tty，完事再把 stdout 换回来。

    这就是文档里 `eval "$(atm pick --print)"` 能成立的原因：curses 只认 fd 1，
    所以临时把 fd 1 指到控制终端，选完再还给调用方去接那行命令。
    """
    if sys.stdout.isatty():
        return fn()
    try:
        tty_fd = os.open("/dev/tty", os.O_RDWR)
    except OSError as exc:
        raise RuntimeError(
            _(
                "atm pick 需要一个终端来画选择器（/dev/tty "
                "打不开）；非交互场景用 `atm list --json`。"
            )
        ) from exc
    saved = os.dup(1)
    sys.stdout.flush()
    try:
        os.dup2(tty_fd, 1)
        return fn()
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)
        os.close(tty_fd)


def _cmd_panes(args: argparse.Namespace) -> int:
    if not tmux.has_server():
        print(_("没有正在运行的 tmux server。"), file=sys.stderr)
        return EXIT_ERROR
    panes = tmux.list_panes()
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "id": p.id,
                        "label": p.label,
                        "command": p.current_command,
                        "path": p.current_path,
                        "active": p.active,
                        "idleShell": p.is_idle_shell,
                    }
                    for p in panes
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_OK

    for pane in panes:
        marker = "*" if pane.active else " "
        state = "idle" if pane.is_idle_shell else f"busy:{pane.current_command}"
        print(
            f"{marker} {pad_display(pane.id, 5)} {pad_display(strip_control(pane.label), 18)} "
            f"{pad_display(strip_control(state), 16)} {strip_control(pane.current_path)}"
        )
    return EXIT_OK


def _cmd_sidebar(args: argparse.Namespace) -> int:
    sb = _sidebar_mod()
    if args.toggle:
        current = args.pane or tmux.current_pane_id()
        if not current:
            print(
                _("atm sidebar --toggle 需要知道当前 pane：在 tmux 里跑，或加 --pane"),
                file=sys.stderr,
            )
            return EXIT_ERROR
        if not tmux.has_server():
            print(_("没有正在运行的 tmux server。"), file=sys.stderr)
            return EXIT_ERROR
        try:
            plan = sb.plan_toggle(tmux.list_panes(), current_pane=current)
            command = f"{_install_mod().resolve_atm_command()} sidebar"
            sb.execute_toggle(plan, command=command, width=args.width)
        except (sb.SidebarError, tmux.TmuxError) as exc:
            print(f"atm: {exc}", file=sys.stderr)
            return EXIT_ERROR
        print(plan.describe(), file=sys.stderr)
        return EXIT_OK

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(_("atm sidebar 是常驻 TUI，需要交互终端；要开侧栏用 --toggle。"), file=sys.stderr)
        return EXIT_ERROR
    from . import sidebar_tui

    try:
        return sidebar_tui.run_sidebar(memory=_memory_limit(args))
    except sb.SidebarError as exc:
        print(f"atm: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _cmd_swap(args: argparse.Namespace) -> int:
    sb = _sidebar_mod()
    current = args.pane or tmux.current_pane_id()
    if not current:
        print(_("atm swap 需要知道当前 pane：在 tmux 里跑，或加 --pane"), file=sys.stderr)
        return EXIT_ERROR
    try:
        panes = tmux.list_panes()
        me = sb.pane_by_id(panes, current)
        if me is None:
            raise sb.SidebarError(_("找不到当前 pane {current}").format(current=current))
        # 从普通格子里跑：换进这格自己。只有从侧栏里跑才需要去猜主格。
        slot = args.into or (None if me.is_sidebar else me.id)
        discard = True if args.discard else (False if args.keep else None)
        plan = sb.plan_swap(
            panes, window_id=me.window_id, target_id=args.pane_id, slot_id=slot, discard=discard
        )
        sb.execute_swap(plan)
    except (sb.SidebarError, tmux.TmuxError) as exc:
        print(f"atm: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(plan.describe(), file=sys.stderr)
    return EXIT_OK


def _cmd_park(args: argparse.Namespace) -> int:
    sb = _sidebar_mod()
    target = args.pane_id or tmux.current_pane_id()
    if not target:
        print(_("atm park 需要一个 pane id：在 tmux 里跑，或显式传"), file=sys.stderr)
        return EXIT_ERROR
    try:
        panes = tmux.list_panes()
        pane = sb.pane_by_id(panes, target)
        if pane is None:
            raise sb.SidebarError(_("找不到 pane {target}").format(target=target))
        bg = tmux.find_window(pane.session, sb.PARK_WINDOW)
        plan = sb.plan_park(panes, pane_id=target, bg_window=bg)
        sb.execute_park(plan)
    except (sb.SidebarError, tmux.TmuxError) as exc:
        print(f"atm: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(plan.describe(), file=sys.stderr)
    return EXIT_OK


def _cmd_prune(args: argparse.Namespace) -> int:
    sb = _sidebar_mod()
    if not tmux.has_server():
        print(_("没有正在运行的 tmux server。"), file=sys.stderr)
        return EXIT_ERROR
    try:
        targets = sb.prunable(tmux.list_panes())
    except tmux.TmuxError as exc:
        print(f"atm: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if not targets:
        print(
            _("{sb_PARK_WINDOW} 里没有空闲的 shell 格子。").format(sb_PARK_WINDOW=sb.PARK_WINDOW),
            file=sys.stderr,
        )
        return EXIT_OK
    for pane in targets:
        print(
            f"{'将关掉' if args.dry_run else '关掉'} {pane.id} {pane.label} "
            f"{pane.current_command} {pane.current_path}"
        )
    if args.dry_run:
        return EXIT_OK
    killed = sb.execute_prune(targets)
    print(
        _("已关掉 {v0} 个（{sb_PARK_WINDOW} 空了的话窗口也一起消失）").format(
            v0=len(killed), sb_PARK_WINDOW=sb.PARK_WINDOW
        ),
        file=sys.stderr,
    )
    return EXIT_OK


def _doctor_report() -> dict:
    """体检数据，供文本和 --json 两种输出。字段名与语言无关。"""
    import shutil

    from .sources import claude as claude_src
    from .sources import codex as codex_src
    from .sources import pi as pi_src

    config = _config_mod()
    guard = _guard_mod()

    sources = {}
    for name, mod in (("claude", claude_src), ("codex", codex_src), ("pi", pi_src)):
        root = mod.DEFAULT_ROOT
        sources[name] = {
            "root": str(root),
            "exists": root.is_dir(),
            "files": len(list(mod.discover())),
        }
    legacy = codex_src.LEGACY_INDEX
    sources["codex"]["legacyIndex"] = {
        "path": str(legacy),
        "exists": legacy.exists(),
        "titles": len(codex_src.load_legacy_titles()),
    }

    clis = {program: shutil.which(program) for program in LAUNCH_PROGRAMS}

    tmux_info = {
        "installed": tmux.is_installed(),
        "serverRunning": tmux.is_installed() and tmux.has_server(),
        "insideTmux": tmux.inside_tmux(),
    }

    st = _persist_mod().status()
    persist = {
        "blockInstalled": st.block_installed,
        "pluginsPresent": list(st.plugins_present),
        "pluginsMissing": list(st.plugins_missing),
        "autosaveHooked": st.autosave_hooked,
        "lastSave": str(st.last_save) if st.last_save else None,
    }

    memory: dict = {"cgroupAvailable": dispatch_mod.memory_limits_available()}
    config_error = None
    try:
        cfg = config.load()
        memory.update(
            {
                "enabled": cfg.memory_enabled,
                "high": cfg.memory_high,
                "max": cfg.memory_max,
                "slice": cfg.memory_slice,
            }
        )
        sl = guard.status(cfg.memory_slice)
        memory["sliceUnit"] = {
            "path": str(sl.path),
            "exists": sl.exists,
            "ours": sl.ours,
            "high": sl.high,
            "max": sl.max,
        }
    except config.ConfigError as exc:
        config_error = str(exc)

    result = index_mod.build()
    stats = result.stats
    per_source = {}
    for source in Source:
        n = sum(1 for e in result.entries if e.source is source)
        per_source[source.value] = n

    return {
        "version": __import__("atm").__version__,
        "sources": sources,
        "clis": clis,
        "tmux": tmux_info,
        "persistence": persist,
        "memory": memory,
        "configError": config_error,
        "indexDescribe": stats.describe(),
        "index": {
            "total": stats.total,
            "fromCache": stats.from_cache,
            "parsed": stats.parsed,
            "skipped": stats.skipped,
            "elapsedMs": stats.elapsed_ms,
            "perSource": per_source,
        },
        "ok": config_error is None,
    }


def _cmd_doctor(args: argparse.Namespace) -> int:
    report = _doctor_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return EXIT_OK if report["ok"] else EXIT_ERROR

    print(_("== 数据源 =="))
    for name, info in report["sources"].items():
        status = _("存在") if info["exists"] else _("不存在")
        print(
            _("  {v0} {v1}: {status}，{v2} 个会话文件").format(
                v0=name.capitalize(), v1=info["root"], status=status, v2=info["files"]
            )
        )
    legacy = report["sources"]["codex"]["legacyIndex"]
    print(
        _("  Codex 老索引 {v0}: {v1}（{v2} 条标题）").format(
            v0=legacy["path"], v1=_("有") if legacy["exists"] else _("无"), v2=legacy["titles"]
        )
    )

    print("\n== CLI ==")
    for program, found in report["clis"].items():
        print(f"  {program}: {found or _('不在 PATH 里 —— 投递出去会失败')}")

    print("\n== tmux ==")
    t = report["tmux"]
    if not t["installed"]:
        print(_("  tmux: 没装 —— 只能用 --print 模式"))
    else:
        print(
            _("  tmux: 已装，server {v0}").format(
                v0=_("在跑") if t["serverRunning"] else _("没起来")
            )
        )
        print(_("  当前在 tmux 里: {v0}").format(v0=_("是") if t["insideTmux"] else _("否")))

    print(_("\n== 持久化（resurrect + continuum）=="))
    _report_persist(_persist_mod().status())

    print(_("\n== 内存闸门 =="))
    guard_ok = _report_guard()

    print(_("\n== 索引 =="))
    idx = report["index"]
    print(f"  {report['indexDescribe']}")
    for source, n in idx["perSource"].items():
        print(f"    {SOURCE_TAG.get(Source(source), '??')} {source}: {n}")
    # 没装某家 CLI 不算故障；配置文件读不了才算 —— 那意味着用户以为的限制根本没生效。
    return EXIT_OK if guard_ok else EXIT_ERROR


def _interactive() -> bool:
    """两端都是终端才问问题；管道 / CI 里一律走默认值。"""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _wizard_wanted(args: argparse.Namespace) -> bool:
    """裸 `atm install`（只允许带 --conf）+ 两端都是终端 → 进问答向导。"""
    if not _interactive():
        return False
    if args.yes or args.print or args.no_persist or args.no_slice:
        return False
    return all(v is None for v in (args.key, args.sidebar_key, args.width, args.height))


def _install_wizard(args: argparse.Namespace) -> bool:
    """问四个问题填 install 的选项（借 `gh auth login` 的形式）。回车 = 默认。

    返回 False = 用户 Ctrl-C / Ctrl-D 中断。只填 args，不动任何文件 ——
    后面照旧打印计划、再确认一次才写。
    """
    install = _install_mod()
    print(_("atm install 向导：回车用默认值，Ctrl-C 退出。（带任意选项运行可跳过向导，例如 -y）"))
    print()
    try:
        while True:
            prompt = _("唤出选择器的键  prefix + ?（大写 = 只看当前目录的会话）")
            key = _ask(prompt, install.DEFAULT_KEY)
            try:
                install.validate_key(_("这个键"), key)
            except ValueError as exc:
                print(f"  {exc}")
                continue
            break
        while True:
            prompt = _("侧栏开关键  prefix + ?（大写 = 把当前格子收进后台）")
            sidebar_key = _ask(prompt, install.DEFAULT_SIDEBAR_KEY)
            try:
                install.validate_key(_("这个键"), sidebar_key)
            except ValueError as exc:
                print(f"  {exc}")
                continue
            if sidebar_key == key:
                print(_("  和上一个键相同了，换一个。"))
                continue
            break
        persist = _ask_yes_no(_("装 tmux-resurrect / tmux-continuum，重启后恢复窗口布局？"))
        use_slice = _ask_yes_no(_("写总量闸门 systemd slice，限制所有 AI 会话合计的内存？"))
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    args.key = key
    args.sidebar_key = sidebar_key
    args.no_persist = not persist
    args.no_slice = not use_slice
    print()
    return True


def _resolve_install_args(args: argparse.Namespace) -> None:
    install = _install_mod()
    args.key = args.key or install.DEFAULT_KEY
    args.sidebar_key = args.sidebar_key or install.DEFAULT_SIDEBAR_KEY
    args.width = args.width or install.DEFAULT_WIDTH
    args.height = args.height or install.DEFAULT_HEIGHT


def _ask(prompt: str, default: str) -> str:
    answer = input(f"{prompt} [{default}] ").strip()
    return answer or default


def _ask_yes_no(prompt: str, *, default: bool = True) -> bool:
    answer = input(f"{prompt} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _cmd_install(args: argparse.Namespace) -> int:
    persist = _persist_mod()
    if not tmux.is_installed():
        print(_("tmux 没装。先装：{v0}").format(v0=persist.tmux_install_hint()))
        print(_("（配置照样可以先写好，装完 tmux 直接生效。）\n"))

    if _wizard_wanted(args) and not _install_wizard(args):
        print(_("已取消，没有改动任何文件。"))
        return EXIT_CANCELLED
    _resolve_install_args(args)

    plan = _install_mod().build_plan(
        conf_path=args.conf,
        key=args.key,
        sidebar_key=args.sidebar_key,
        width=args.width,
        height=args.height,
    )
    persist_plan = None if args.no_persist else persist.build_plan(conf_path=args.conf)

    print(plan.describe())
    # resolve_atm_command 永远返回绝对路径，所以不能拿 != "atm" 判断「没装成命令」——
    # 那样 PATH 里明明有 atm 也会误报。只有退回到 `python -m atm` 才值得提醒。
    if "-m atm" in plan.atm_command:
        print(
            _(
                "\n注意：PATH 里没找到 `atm`，绑定会用 `{plan_atm_command}`。\n    "
                " 装成 PATH 命令会更快（仓库根目录 `uv tool install --editable .`）。"
            ).format(plan_atm_command=plan.atm_command)
        )
    if persist_plan is not None:
        print()
        print(persist_plan.describe())
    if not args.no_slice:
        print()
        print(_guard_mod().describe_plan(_config_mod().load().memory_slice))

    if args.print:
        return EXIT_OK

    if not args.yes:
        print(_("\n这会修改你的全局 tmux 配置（改前会自动备份）。"))
        if not _confirm(_("继续吗？")):
            print(_("已取消，没有改动任何文件。"))
            return EXIT_CANCELLED

    result = _install_mod().apply(plan)

    print(_("\n已写入 {result_conf_path}").format(result_conf_path=result.conf_path))
    if result.backup_path:
        print(_("备份    {result_backup_path}").format(result_backup_path=result.backup_path))
    if result.applied_live:
        print(
            _(
                "已对正在运行的 tmux server 立即生效 —— 不用 reload，直接按 prefix + {args_key} 试"
            ).format(args_key=args.key)
        )
    elif result.live_error:
        print(
            _("对运行中的 server 生效失败：{result_live_error}").format(
                result_live_error=result.live_error
            )
        )
        print(_("（配置已写好，下次起 tmux 或 `prefix + :source-file ~/.tmux.conf` 后生效）"))
    else:
        print(_("当前没有运行中的 tmux server；下次 `tmux new` 时自动生效。"))

    if persist_plan is not None:
        _apply_persist(persist_plan)
    if not args.no_slice:
        _apply_slice()
    return EXIT_OK


def _apply_slice() -> None:
    guard = _guard_mod()
    cfg = _config_mod().load()
    if not dispatch_mod.memory_limits_available():
        print(_("\n这台机器拿不到 cgroup，跳过总量闸门 slice。"))
        return
    path, written = guard.install(cfg.memory_slice)
    st = guard.status(cfg.memory_slice)
    if written:
        print(
            _(
                "\n已写总量闸门 {path}（MemoryHigh={st_high} "
                "/ MemoryMax={st_max}，按物理内存 50% / 65%）"
            ).format(path=path, st_high=st.high, st_max=st.max)
        )
        print(_("所有 atm 启动 / 投递的会话共同受这个总量约束；单个会话的上限看 `atm config`。"))
    else:
        print(
            _(
                "\n总量闸门 {path} 已存在（MemoryHigh={st_high} / MemoryMax={st_max}），不动。"
            ).format(path=path, st_high=st.high, st_max=st.max)
        )


def _apply_persist(plan) -> None:
    result = _persist_mod().apply(plan)
    print()
    if result.cloned:
        print(_("已克隆插件：{v0}").format(v0=", ".join(result.cloned)))
    for name, err in result.clone_errors.items():
        print(_("克隆 {name} 失败：{err}").format(name=name, err=err))
    if result.clone_errors:
        print(_("（配置已写好；网络好了在 tmux 里按 prefix + I 让 tpm 补装）"))
    if not result.block_written:
        return
    print(_("已写入持久化块 → {result_conf_path}").format(result_conf_path=result.conf_path))
    if result.backup_path:
        print(_("备份    {result_backup_path}").format(result_backup_path=result.backup_path))
    if tmux.has_server():
        print(_("持久化在下次起 tmux server 时生效；想现在就要：`tmux source-file ~/.tmux.conf`"))
        print(_("（刻意不自动 source —— 那会重跑你整份配置里带副作用的行）"))
    print(_("手动存/恢复：prefix + Ctrl-s / prefix + Ctrl-r；"))
    print(_("自动存档每 10 分钟，只在有客户端 attach 时触发"))


def _cmd_update(args: argparse.Namespace) -> int:
    """升级 atm 自己。先说清楚「你是怎么装的、我准备跑什么」，再动手。"""
    import subprocess

    update = _update_mod()
    info = update.detect()
    current = update.current_version()
    latest = update.latest_version()
    command = update.upgrade_command(info)

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "current": current,
                    "latest": latest,
                    "updateAvailable": bool(
                        latest and update.version_tuple(latest) > update.version_tuple(current)
                    ),
                    "installer": info.installer.value,
                    "origin": info.origin.value,
                    "detail": info.detail,
                    "command": command,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_OK

    print(_("当前 {current}（{v0}）").format(current=current, v0=info.describe()))
    if latest is None:
        print(_("PyPI 最新：查不到（离线或镜像滞后），不影响升级"))
    elif update.version_tuple(latest) > update.version_tuple(current):
        print(_("PyPI 最新：{latest}  ← 有新版本").format(latest=latest))
    else:
        print(_("PyPI 最新：{latest}  已是最新").format(latest=latest))
        if info.origin is update.Origin.GIT:
            print(_("（git 装的会跟 main 最新 commit，可能比 PyPI 发行版还新）"))

    if command is None:
        print()
        print(update.manual_hint(info))
        return EXIT_OK if info.origin is update.Origin.EDITABLE else EXIT_ERROR
    if args.check:
        print(_("\n升级命令：{v0}").format(v0=" ".join(command)))
        return EXIT_OK

    print(_("\n将执行：{v0}").format(v0=" ".join(command)))
    if not args.yes and not _confirm(_("继续吗？")):
        print(_("已取消。"))
        return EXIT_CANCELLED
    try:
        result = subprocess.run(command, check=False)
    except FileNotFoundError:
        print(_("atm: 找不到 {v0}，请手动升级").format(v0=command[0]), file=sys.stderr)
        return EXIT_ERROR
    if result.returncode != 0:
        print(
            _("atm: 升级命令退出码 {result_returncode}").format(
                result_returncode=result.returncode
            ),
            file=sys.stderr,
        )
        return EXIT_ERROR
    # 新版本在新进程里才看得到；这里的 __version__ 还是老的
    shown = subprocess.run(["atm", "--version"], capture_output=True, text=True, check=False)
    print(_("完成：{v0}").format(v0=shown.stdout.strip() or _("（跑 atm --version 看版本）")))
    return EXIT_OK


def _cmd_completion(args: argparse.Namespace) -> int:
    from . import completion

    config = _config_mod()
    print(
        completion.render(
            args.shell,
            _build_parser(),
            config_keys=tuple(config.KEYS),
            sources=tuple(s.value for s in Source),
        ),
        end="",
    )
    return EXIT_OK


def _cmd_config(args: argparse.Namespace) -> int:
    config = _config_mod()
    if args.path:
        print(config.config_path())
        return EXIT_OK
    if args.reset:
        path = config.config_path()
        if path.exists():
            path.unlink()
            print(_("已删 {path}，全部恢复默认").format(path=path))
        else:
            print(_("本来就是默认，没有配置文件"))
        return EXIT_OK
    try:
        cfg, sources = config.load_with_sources()
        if args.json and not (args.key or args.unset):
            print(json.dumps(config.to_json(cfg, sources), ensure_ascii=False, indent=2))
            return EXIT_OK
        if args.unset:
            cfg = config.unset_value(cfg, args.unset)
            path = config.save(cfg)
            print(_("{args_unset} 已恢复默认 → {path}").format(args_unset=args.unset, path=path))
            return EXIT_OK
        if args.key and args.value is None:
            raise config.ConfigError(
                _("要给 {args_key} 一个值，比如 `atm config {args_key} 4G`").format(
                    args_key=args.key
                )
            )
        if args.key:
            cfg = config.set_value(cfg, args.key, args.value)
            path = config.save(cfg)
            print(f"{args.key} = {args.value} → {path}")
            return EXIT_OK
    except config.ConfigError as exc:
        print(f"atm: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if not args.show and sys.stdin.isatty() and sys.stdout.isatty():
        from . import config_tui

        return config_tui.run_config_editor()
    print(config.describe(cfg, sources))
    return EXIT_OK


def _cmd_launch(args: argparse.Namespace) -> int:
    """`atm claude …`：套上闸门后 exec 过去。进程被替换，退出码和 tty 都是 claude 自己的。"""
    config = _config_mod()
    program = args.program
    found = config.resolve_program(program)
    if found is None:
        print(_("atm: PATH 里没有 {program}").format(program=program), file=sys.stderr)
        return EXIT_ERROR
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        print(f"atm: {exc}", file=sys.stderr)
        return EXIT_ERROR
    argv = config.launch_argv(found, list(args.args), cfg)
    if argv[0] != found and config.resolve_program(argv[0]) is None:
        # systemd-run 不在 PATH 上（非 systemd 环境）：退回裸跑，但要说一声，别让人以为限了。
        print(_("atm: 找不到 {v0}，本次不套内存闸门").format(v0=argv[0]), file=sys.stderr)
        argv = [found, *args.args]
    os.execvp(argv[0], argv)
    return EXIT_ERROR  # execvp 成功不会回来


def _cmd_uninstall(args: argparse.Namespace) -> int:
    conf_path = args.conf or Path.home() / ".tmux.conf"
    if not args.yes:
        print(
            _("将从 {conf_path} 移除 atm 的绑定块（marker 之外的内容一律不动）。").format(
                conf_path=conf_path
            )
        )
        if not _confirm(_("继续吗？")):
            print(_("已取消。"))
            return EXIT_CANCELLED

    removed, backup = _install_mod().remove(conf_path)
    removed_persist, backup_persist = _persist_mod().remove(conf_path)
    slice_name = _config_mod().load().memory_slice
    removed_slice = _guard_mod().remove(slice_name)
    if not (removed or removed_persist or removed_slice):
        print(_("没找到 atm 写的任何东西（键位块 / 持久化块 / slice），无需移除。"))
        return EXIT_OK
    if removed:
        print(_("已从 {conf_path} 移除键位块").format(conf_path=conf_path))
        if backup:
            print(_("备份 {backup}").format(backup=backup))
        print(_("已解绑的键位在下次启动 tmux 时消失（当前 server 可手动 `unbind-key`）。"))
    if removed_persist:
        print(
            _(
                "已从 {conf_path} 移除持久化块（~/.tmux/plugins 里已克隆的插件没动，不要了自己 rm）"
            ).format(conf_path=conf_path)
        )
        if backup_persist:
            print(_("备份 {backup_persist}").format(backup_persist=backup_persist))
    if removed_slice:
        print(_("已删总量闸门 {slice_name}（只删 atm 自己写的那份）").format(slice_name=slice_name))
    return EXIT_OK


def _confirm(prompt: str) -> bool:
    """非交互环境（管道 / CI）一律当「否」—— 不能默默改用户的全局配置。"""
    if not sys.stdin.isatty():
        print(_("{prompt} 非交互环境，默认取消。要无人值守请加 --yes。").format(prompt=prompt))
        return False
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


# ---------------------------------------------------------------- helpers


def _report_guard() -> bool:
    """返回 False 表示配置本身有错（doctor 该非零退出）；机器不支持 cgroup 不算错。"""
    config = _config_mod()
    guard = _guard_mod()
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        print(f"  ❌ {exc}")
        return False
    if not dispatch_mod.memory_limits_available():
        print(
            _(
                "  cgroup: 不可用（无 systemd user manager 或 memory 控制器未 delegate）"
                "—— 一切限制被忽略"
            )
        )
        return True
    if not cfg.memory_enabled:
        print(_("  已关闭（memory.enabled=false）"))
        return True
    print(
        _(
            "  单会话: MemoryHigh={cfg_memory_high} "
            "MemoryMax={cfg_memory_max}（atm claude / 投递时套）"
        ).format(cfg_memory_high=cfg.memory_high, cfg_memory_max=cfg.memory_max)
    )
    st = guard.status(cfg.memory_slice)
    if st.exists:
        who = _("atm 写的") if st.ours else _("用户自己的")
        print(
            _("  总量 {st_name}: MemoryHigh={st_high} MemoryMax={st_max}（{who}）").format(
                st_name=st.name, st_high=st.high, st_max=st.max, who=who
            )
        )
    else:
        print(
            _(
                "  总量 {st_name}: ❌ 单元不存在 —— systemd "
                "会建一个无限制的同名 slice，总量闸门形同虚设。跑 atm install 补上"
            ).format(st_name=st.name)
        )
    print(_("  直接敲 claude / codex 不受以上任何限制；要限就用 atm claude / atm codex"))
    return True


def _report_persist(st) -> None:
    print(
        _("  配置块: {v0}").format(
            v0=_("已装") if st.block_installed else _("未装（atm install 会一起装）")
        )
    )
    if st.plugins_missing:
        missing = ", ".join(st.plugins_missing)
        print(
            _("  插件缺: {missing}（在 tmux 里按 prefix + I，或重跑 atm install）").format(
                missing=missing
            )
        )
    else:
        print(_("  插件: {v0} 都在").format(v0=", ".join(st.plugins_present)))
    if st.autosave_hooked is None:
        print(_("  自动存档钩子: server 没起来，查不了"))
    elif st.autosave_hooked:
        print(_("  自动存档钩子: 在 status-right 里，正常"))
    else:
        print(
            _(
                "  自动存档钩子: ❌ 不在 status-right 里 —— "
                "continuum 加载时机器上还有别的 tmux server 就会静默跳过。"
                " 关掉多余的 server（含 -L 隔离 socket）后重起主 server"
            )
        )
    if st.last_save:
        import datetime as _dt

        when = _dt.datetime.fromtimestamp(st.last_save.resolve().stat().st_mtime)
        print(_("  最近存档: {when:%Y-%m-%d %H:%M}").format(when=when))
    else:
        print(_("  最近存档: 还没有"))


def _report_root(name: str, root: Path, count: int) -> None:
    status = _("存在") if root.is_dir() else _("不存在")
    print(
        _("  {name} {root}: {status}，{count} 个会话文件").format(
            name=name, root=root, status=status, count=count
        )
    )


def _print_source_breakdown(result: SessionIndex) -> None:
    for source in Source:
        count = sum(1 for e in result.entries if e.source is source)
        print(f"  {source.value}: {count}")


def _load_entries(args: argparse.Namespace) -> tuple[SessionEntry, ...]:
    source = Source(args.source) if getattr(args, "source", None) else None
    cwd = getattr(args, "cwd", None)
    if getattr(args, "here", False):
        cwd = str(Path.cwd())

    result = index_mod.build(
        use_cache=not getattr(args, "no_cache", False),
        sources={source} if source else None,
    )
    filtered = index_mod.filter_with_stats(
        result.entries,
        source=source,
        cwd_prefix=cwd,
        include_noise=getattr(args, "all", False),
    )
    if filtered.hidden_noise:
        print(
            _("（已隐藏 {filtered_hidden_noise} 条机器生成的一次性调用，--all 可显示）").format(
                filtered_hidden_noise=filtered.hidden_noise
            ),
            file=sys.stderr,
        )
    return filtered.entries


def _resolve_target(
    args: argparse.Namespace, entry: SessionEntry, *, interactive: bool = True
) -> DispatchTarget | None:
    """把命令行参数（或交互选择）翻译成投递目标。"""
    direction = (
        SplitDirection.VERTICAL if getattr(args, "vertical", False) else SplitDirection.HORIZONTAL
    )

    if getattr(args, "print", False):
        return DispatchTarget.print_only()
    if getattr(args, "pane", None):
        return DispatchTarget.existing(args.pane)
    if getattr(args, "window", False):
        return DispatchTarget.window()
    if getattr(args, "split", False):
        return DispatchTarget.split(tmux.current_pane_id(), direction)

    if not tmux.has_server():
        # 不在 tmux 里就退化成打印，别硬报错。
        return DispatchTarget.print_only()

    if not interactive:
        return DispatchTarget.split(tmux.current_pane_id(), direction)

    return _pick_target(entry, direction)


def _pick_target(entry: SessionEntry, direction: SplitDirection) -> DispatchTarget | None:
    """第二步：选投到哪个 pane。"""
    try:
        panes = tmux.list_panes()
    except tmux.TmuxError:
        panes = ()

    current = tmux.current_pane_id()
    options: list[_tui().PickerOption] = []
    for pane in panes:
        # 当前 pane 也留在列表里、只是标出来，不排除掉：
        # 从 display-popup 里跑时 TMUX_PANE 指向哪个 pane 没有实测结论，
        # 排除它可能恰好把用户最想投的那个格子给藏了。
        marker = _(" ←当前") if pane.id == current else ""
        options.append(
            _tui().PickerOption(
                key=pane.id,
                label=f"{pad_display(pane.id, 5)} {pad_display(pane.label + marker, 22)}",
                detail=_pane_detail(pane),
            )
        )

    options.append(
        _tui().PickerOption(
            key="__split_h__", label=_("新分一格：左右分 ┃"), detail="split-window -h"
        )
    )
    options.append(
        _tui().PickerOption(
            key="__split_v__", label=_("新分一格：上下分 ━"), detail="split-window -v"
        )
    )
    options.append(
        _tui().PickerOption(key="__window__", label=_("新开一个 window"), detail="new-window")
    )
    options.append(_tui().PickerOption(key="__print__", label=_("只打印命令"), detail=_("不投递")))

    size_note = dispatch_mod.size_label(entry)
    suffix = f"  [{size_note} ⚠]" if size_note else ""
    title = _("把「{v0}」{suffix} 投到哪里？").format(
        v0=truncate_display(entry.title, 44), suffix=suffix
    )
    choice = _tui().pick_option(options, title=title)
    if choice is None:
        return None
    if choice.key == "__split_h__":
        return DispatchTarget.split(current, SplitDirection.HORIZONTAL)
    if choice.key == "__split_v__":
        return DispatchTarget.split(current, SplitDirection.VERTICAL)
    if choice.key == "__window__":
        return DispatchTarget.window()
    if choice.key == "__print__":
        return DispatchTarget.print_only()
    return DispatchTarget.existing(choice.key)


def _pane_detail(pane: Pane) -> str:
    state = "idle" if pane.is_idle_shell else f"busy:{pane.current_command}"
    return f"[{state}] {truncate_display(pane.current_path, 40)}"


def _memory_limit(args: argparse.Namespace) -> dispatch_mod.MemoryLimit | None:
    """决定这次投递要不要套内存闸门。

    默认套。拿不到 cgroup 支持时**静默降级为不套** —— 没闸门也比投不出去强。
    """
    if getattr(args, "no_mem_limit", False):
        return None
    cfg = _config_mod().load()
    base = cfg.memory_limit()  # 已经考虑了 memory.enabled 和机器支持
    if base is None:
        return None
    config = _config_mod()
    high = getattr(args, "mem_high", None)
    max_ = getattr(args, "mem_max", None)
    # 命令行给的值走和 atm config 同一套校验：`--mem-high lots` 不该等到 systemd-run 才报错
    high = config.validate_size("--mem-high", high) if high else base.high
    max_ = config.validate_size("--mem-max", max_) if max_ else base.max
    return dispatch_mod.MemoryLimit(
        high=high, max=max_, swap_max=base.swap_max, slice_name=base.slice_name, user=base.user
    )


def _do_dispatch(
    entry: SessionEntry,
    target: DispatchTarget,
    *,
    force: bool,
    memory: dispatch_mod.MemoryLimit | None = None,
) -> int:
    notice = dispatch_mod.size_notice(entry)
    if notice:
        print(notice, file=sys.stderr)
    result = dispatch_mod.dispatch(entry, target, force=force, memory=memory)
    if target.kind is TargetKind.PRINT:
        print(result.command.shell_line())
    else:
        print(result.describe(), file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
