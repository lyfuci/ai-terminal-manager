"""配置改了之后，让机器跟上：这是「值进 config、动作留 install」这条边界的另一半。

`atm config` 的编辑器和命令行改完保存后都调 `apply_changes(old, new)`。它只做和改动相关的那部分：
- `tmux.*` 变了 → tmuxopts：重写 ~/.tmux.conf 最前面的选项块 + 对活着的 server `set -g`
- `keys.*` 变了 → install：键位块已装的话重写 + 重绑，旧键解绑；没装就提示先 `atm install`
- `memory.slice*` 变了 → guard：单元文件是 atm 写的就按新数重写 + daemon-reload

返回一组人话，调用方决定打到 stdout 还是编辑器状态行。任何一步失败都不抛 —— 配置文件已经保存成功了，
失败的只是「让 tmux / systemd 跟上」这一步，把原因说出来就行。
"""

from __future__ import annotations

from pathlib import Path

from . import config as config_mod
from . import dispatch
from .i18n import _

_TMUX_FIELDS = ("tmux_mouse", "tmux_focus_events", "tmux_history_limit", "tmux_base_index")
_TMUX_FIELDS += ("tmux_renumber_windows",)
_KEY_FIELDS = ("keys_pick", "keys_sidebar", "keys_popup_width", "keys_popup_height")
_SLICE_FIELDS = ("memory_slice", "memory_slice_high", "memory_slice_max")


def _changed(old: config_mod.Config, new: config_mod.Config, names: tuple[str, ...]) -> bool:
    return any(getattr(old, n) != getattr(new, n) for n in names)


def apply_changes(
    old: config_mod.Config, new: config_mod.Config, *, conf_path: Path | None = None
) -> list[str]:
    notes: list[str] = []
    if _changed(old, new, _TMUX_FIELDS):
        notes += _sync_tmux_options(new, conf_path)
    if _changed(old, new, _KEY_FIELDS):
        notes += _sync_keys(old, new, conf_path)
    if _changed(old, new, _SLICE_FIELDS):
        notes += _sync_slice(new)
    return notes


def _sync_tmux_options(cfg: config_mod.Config, conf_path: Path | None) -> list[str]:
    from . import tmuxopts

    try:
        result = tmuxopts.sync(cfg, conf_path=conf_path)
    except (OSError, RuntimeError) as exc:  # ConfUnreadable / BackupFailed 都是 RuntimeError
        return [_("tmux 选项没写进 ~/.tmux.conf：{exc}").format(exc=exc)]
    notes: list[str] = []
    if result.written:
        notes.append(_("tmux 选项已写进 {path}").format(path=result.conf_path))
    if result.applied_live:
        notes.append(_("tmux 选项已对运行中的 server 生效"))
    elif result.live_error:
        notes.append(_("tmux 选项对运行中的 server 生效失败：{err}").format(err=result.live_error))
    return notes


def _sync_keys(old: config_mod.Config, new: config_mod.Config, conf_path: Path | None) -> list[str]:
    from . import install

    path = conf_path or Path.home() / ".tmux.conf"
    try:
        if not install._has_marker(install._read(path)):
            return [
                _("键位还没装进 tmux；跑一次 `atm install` 才会绑 prefix + {k}").format(
                    k=new.keys_pick
                )
            ]
        plan = install.build_plan(cfg=new, conf_path=path)
        gone = tuple(
            k
            for k in (old.keys_pick, old.keys_sidebar)
            if k not in (new.keys_pick, new.keys_sidebar)
        )
        install.unbind_live(gone)
        result = install.apply(plan)
    except (OSError, RuntimeError, ValueError) as exc:
        return [_("键位块没更新：{exc}").format(exc=exc)]
    notes = [_("键位块已重写 → {path}").format(path=result.conf_path)]
    if result.applied_live:
        notes.append(
            _("运行中的 server 已重绑：prefix + {pick} 选择器，prefix + {sidebar} 侧栏").format(
                pick=new.keys_pick, sidebar=new.keys_sidebar
            )
        )
    elif result.live_error:
        notes.append(_("对运行中的 server 重绑失败：{err}").format(err=result.live_error))
    return notes


def _sync_slice(cfg: config_mod.Config) -> list[str]:
    from . import guard

    if not dispatch.memory_limits_available():
        return [_("这台机器拿不到 cgroup，总量 slice 的数只记在配置里")]
    try:
        path, action = guard.sync(cfg)
    except OSError as exc:
        return [_("总量闸门单元没写：{exc}").format(exc=exc)]
    high, max_ = guard.resolve_totals(cfg)
    if action in ("written", "updated"):
        return [
            _("总量闸门 {path} 已{verb}：MemoryHigh={high} / MemoryMax={max_}").format(
                path=path,
                verb=_("写入") if action == "written" else _("更新"),
                high=high,
                max_=max_,
            )
        ]
    if action == "kept-user":
        return [
            _("总量闸门 {path} 是你自己写的，没动；配置里的数只对 atm 写的单元生效").format(
                path=path
            )
        ]
    return []
