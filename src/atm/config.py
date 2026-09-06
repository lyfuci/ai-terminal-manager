"""`atm config`：用户级配置。所有「值」都在这：内存闸门、总量 slice 的数、tmux 键位、tmux 常用选项。

边界：**值进 config，动作留 install**。`atm install` 只做写键位块 / 克隆持久化插件这类一次性动作，
然后按这里的值把 slice、tmux 选项落地；改这里的任何值、保存，sync.py 立刻让 tmux / systemd 跟上。

为什么要有这个文件而不是一堆命令行参数：闸门参数（MemoryHigh / MemoryMax / slice）是**这台机器**
的属性，不是某一次投递的属性。48G 的机器和 8G 的机器该给的数字不一样，但每次 `--mem-high` 手敲
没人会做。设一次，之后 `atm claude` / `atm pick` / 侧栏恢复全部沿用。

刻意保留一条**不受限**的路：直接敲 `claude` 就是原生的，不套任何东西。
`atm claude` 才进闸门 —— 前缀即选择，不劫持用户的命令。

文件在 `~/.config/atm/config.toml`（尊重 `$XDG_CONFIG_HOME`，测试用 `$ATM_CONFIG` 指到别处）。
只读用 stdlib `tomllib`；写用一个几十行的扁平 TOML 写手 —— 这里只有字符串和布尔，
不值得为此引入依赖（零运行时依赖是本项目的硬约束）。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path

from . import dispatch
from .i18n import _

log = logging.getLogger(__name__)

# systemd 认的内存大小：纯数字（字节）或带 K/M/G/T 后缀；`infinity` 表示不限。
_SIZE_RE = re.compile(r"^(\d+(\.\d+)?[KMGT]?|infinity)$", re.IGNORECASE)
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def config_path() -> Path:
    override = os.environ.get("ATM_CONFIG")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "atm" / "config.toml"


@dataclass(frozen=True, slots=True)
class Config:
    """所有字段都有默认值，缺文件 = 全默认。默认值和 dispatch 里的常量保持同一来源。"""

    memory_enabled: bool = True
    memory_high: str = dispatch.DEFAULT_MEMORY_HIGH
    memory_max: str = dispatch.DEFAULT_MEMORY_MAX
    memory_swap_max: str = dispatch.DEFAULT_MEMORY_SWAP_MAX
    memory_slice: str = dispatch.DEFAULT_SLICE
    # systemd-run 走 `--user`（当前用户的 user manager）。关掉就是系统级 scope，需要 root，
    # 一般人用不到，留着是因为有人在容器 / 服务器上确实是 root 跑的。
    memory_user: bool = True
    # 总量 slice 的两个数。"auto" = 物理内存的 50% / 65%（guard.py 算），也可以写死 24G。
    memory_slice_high: str = "auto"
    memory_slice_max: str = "auto"
    # tmux 键位：prefix + pick 唤出选择器（大写 = 只看当前目录），prefix + sidebar 开关侧栏
    # （大写 = 把当前格子收进后台）。浮层尺寸给 display-popup 的 -w / -h。
    keys_pick: str = "a"
    keys_sidebar: str = "b"
    keys_popup_width: str = "80%"
    keys_popup_height: str = "70%"
    # tmux 常用选项 —— 就是大多数人手写在 ~/.tmux.conf 顶上的那几行。默认全是「不写」：
    # 0 / false 表示不动用户的 tmux 配置；开了才由 tmuxopts.py 写进 ~/.tmux.conf **最前面**
    # 的独立 marker 块（用户后面自己写的任何一行都能盖掉它），并对活着的 server 立即生效。
    tmux_mouse: bool = False
    tmux_focus_events: bool = False
    tmux_history_limit: int = 0  # 0 = 不写；常用 50000（tmux 默认 2000）
    tmux_base_index: int = 0  # 0 = 不写；1 = window / pane 都从 1 开始编号
    tmux_renumber_windows: bool = False

    def memory_limit(self) -> dispatch.MemoryLimit | None:
        """按配置生成闸门；关了或机器不支持就是 None（= 不套）。"""
        if not self.memory_enabled:
            log.debug("memory.enabled=false，不套闸门")
            return None
        if not dispatch.memory_limits_available():
            log.info("这台机器拿不到 cgroup（systemd-run 或 memory 控制器缺），本次不套内存闸门")
            return None
        return dispatch.MemoryLimit(
            high=self.memory_high,
            max=self.memory_max,
            swap_max=self.memory_swap_max,
            slice_name=self.memory_slice,
            user=self.memory_user,
        )


# 命令行上的 key（点号分段） → dataclass 字段 → 校验器
KEYS: dict[str, str] = {
    "memory.enabled": "memory_enabled",
    "memory.high": "memory_high",
    "memory.max": "memory_max",
    "memory.swap-max": "memory_swap_max",
    "memory.slice": "memory_slice",
    "memory.user": "memory_user",
    "memory.slice-high": "memory_slice_high",
    "memory.slice-max": "memory_slice_max",
    "keys.pick": "keys_pick",
    "keys.sidebar": "keys_sidebar",
    "keys.popup-width": "keys_popup_width",
    "keys.popup-height": "keys_popup_height",
    "tmux.mouse": "tmux_mouse",
    "tmux.focus-events": "tmux_focus_events",
    "tmux.history-limit": "tmux_history_limit",
    "tmux.base-index": "tmux_base_index",
    "tmux.renumber-windows": "tmux_renumber_windows",
}

_HELP: dict[str, str] = {
    "memory.enabled": "要不要套闸门（true/false）",
    "memory.high": "软上限：超了节流 + 回收，不杀（如 4G）",
    "memory.max": "硬上限：回收压不住才杀，杀的是整个会话及其子进程（如 8G）",
    "memory.swap-max": "swap 上限（WSL 上 swap 在宿主 SSD，建议小）",
    "memory.slice": "所有会话共同归入的 systemd slice，兜总量",
    "memory.user": "systemd-run 是否走 --user（一般 true）",
    "memory.slice-high": "总量软限（所有 atm 会话合计）：auto = 物理内存 50%，或写死如 24G",
    "memory.slice-max": "总量硬限：auto = 物理内存 65%，或写死如 31G",
    "keys.pick": "prefix + 这个键唤出选择器（大写 = 只看当前目录）。单个小写字母",
    "keys.sidebar": "prefix + 这个键开关侧栏（大写 = 把当前格子收进后台）。单个小写字母",
    "keys.popup-width": "选择器浮层宽度，给 display-popup -w（如 80% 或 120）",
    "keys.popup-height": "选择器浮层高度，给 display-popup -h（如 70% 或 40）",
    "tmux.mouse": "tmux 鼠标：点格子切焦点、滚轮翻滚动缓冲、拖边框调大小（写进 ~/.tmux.conf）",
    "tmux.focus-events": "tmux 把终端的焦点进出转给程序（编辑器自动重载、AI CLI 感知切窗要它）",
    "tmux.history-limit": "每格滚动缓冲行数；0 = 不写（tmux 默认 2000），常用 50000",
    "tmux.base-index": "window / pane 编号起点；0 = 不写（tmux 从 0 数），1 = 从 1 开始",
    "tmux.renumber-windows": "关掉一个 window 后剩下的自动重新编号，不留空洞",
}


class ConfigError(ValueError):
    pass


ENV_PREFIX = "ATM_"


def env_var_for(key: str) -> str:
    """memory.swap-max → ATM_MEMORY_SWAP_MAX"""
    return ENV_PREFIX + key.replace(".", "_").replace("-", "_").upper()


def load(path: Path | None = None) -> Config:
    """读配置。优先级：环境变量 > 文件 > 默认（命令行参数由调用方再盖一层）。

    文件不存在 → 默认；文件坏了 → 抛 ConfigError 而不是静默用默认值，
    否则用户以为限制生效了其实没有。
    """
    return load_with_sources(path)[0]


def load_with_sources(path: Path | None = None) -> tuple[Config, dict[str, str]]:
    """同 load()，外加每个 key 的值来自哪里：default / file / env。`atm config` 用它标来源。"""
    p = path or config_path()
    sources = dict.fromkeys(KEYS, "default")
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(_("{p} 读不了：{exc}").format(p=p, exc=exc)) from exc
    cfg = _from_raw(raw, p)
    for key, field in KEYS.items():
        if getattr(cfg, field) != getattr(Config(), field):
            sources[key] = "file"
    for key, field in KEYS.items():
        env_value = os.environ.get(env_var_for(key))
        if env_value is not None and env_value != "":
            cfg = replace(cfg, **{field: _coerce(key, env_value)})
            sources[key] = f"env {env_var_for(key)}"
    return cfg, sources


def _from_raw(raw: dict, p: Path) -> Config:
    """把 TOML 字典变成 Config。结构错 / 键拼错都报错——静默忽略会让人以为设置生效了。"""
    known_sections = {k.split(".", 1)[0] for k in KEYS}
    for section in raw:
        if section not in known_sections:
            raise ConfigError(
                _("{p}：不认识的段 [{section}]，只有 {v0}").format(
                    p=p, section=section, v0=", ".join(sorted(known_sections))
                )
            )
    cfg = Config()
    for section in known_sections:
        table = raw.get(section)
        if table is None:
            continue
        if not isinstance(table, dict):
            raise ConfigError(
                _("{p}：[{section}] 应该是一个表，收到 {v0}").format(
                    p=p, section=section, v0=type(table).__name__
                )
            )
        valid = {
            k.split(".", 1)[1].replace("-", "_"): k for k in KEYS if k.startswith(section + ".")
        }
        for name, value in table.items():
            key = valid.get(name)
            if key is None:
                raise ConfigError(
                    _("{p}：[{section}] 里不认识 {name!r}，可用：{v0}").format(
                        p=p, section=section, name=name, v0=", ".join(sorted(valid))
                    )
                )
            cfg = replace(cfg, **{KEYS[key]: _coerce(key, value)})
    validate(cfg)
    return cfg


def save(cfg: Config, path: Path | None = None) -> Path:
    """原子写：先写临时文件再 rename，中途断电不会留下半个 TOML。"""
    validate(cfg)
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(dumps(cfg), encoding="utf-8")
    tmp.replace(p)
    return p


def validate_size(label: str, value: str) -> str:
    """给命令行参数用的同一套校验：`--mem-high lots` 在这里就报，不等 systemd-run。"""
    s = str(value).strip()
    if not _SIZE_RE.match(s):
        raise ConfigError(
            _("{label} 要形如 4G / 512M / infinity，收到 {value!r}").format(
                label=label, value=value
            )
        )
    return s.upper() if s.lower() != "infinity" else "infinity"


def set_value(cfg: Config, key: str, value: str) -> Config:
    """校验并返回新配置。key 不认识 / 值不合法都抛 ConfigError，带可读原因。"""
    field = KEYS.get(key)
    if field is None:
        raise ConfigError(
            _("不认识的配置项 {key!r}，可用：{v0}").format(key=key, v0=", ".join(KEYS))
        )
    return replace(cfg, **{field: _coerce(key, value)})


def unset_value(cfg: Config, key: str) -> Config:
    field = KEYS.get(key)
    if field is None:
        raise ConfigError(
            _("不认识的配置项 {key!r}，可用：{v0}").format(key=key, v0=", ".join(KEYS))
        )
    return replace(cfg, **{field: getattr(Config(), field)})


def describe(cfg: Config, sources: dict[str, str] | None = None) -> str:
    """`atm config` 不带参数时的输出：值 + 来源 + 一句说明。"""
    sources = sources or dict.fromkeys(KEYS, "default")
    lines = [
        _("配置文件：{v0}{v1}").format(
            v0=config_path(), v1="" if config_path().exists() else _("（不存在，全部默认）")
        ),
        _("优先级：命令行参数 > 环境变量（{ENV_PREFIX}MEMORY_HIGH 这类）> 文件 > 默认").format(
            ENV_PREFIX=ENV_PREFIX
        ),
        "",
    ]
    for key, field in KEYS.items():
        value = getattr(cfg, field)
        shown = str(value).lower() if isinstance(value, bool) else str(value)
        src = sources.get(key, "default")
        tag = "" if src == "default" else f"  ← {src}"
        lines.append(f"  {key:<22} {shown:<12}{tag}")
        lines.append(f"  {'':<22} {_(_HELP[key])}")
    lines.append("")
    if not dispatch.memory_limits_available():
        lines.append(
            _("⚠ 这台机器拿不到 cgroup（无 systemd user manager 或 memory 控制器未 delegate），")
        )
        lines.append(_("  以上设置会被静默忽略，atm claude 等价于直接跑 claude。"))
    return "\n".join(lines)


def to_json(cfg: Config, sources: dict[str, str]) -> dict:
    """`atm config --json`：机器可读，字段名与语言无关。"""
    return {
        "path": str(config_path()),
        "exists": config_path().exists(),
        "values": {
            key: {"value": getattr(cfg, field), "source": sources.get(key, "default")}
            for key, field in KEYS.items()
        },
    }


def dumps(cfg: Config) -> str:
    """扁平 TOML：每个段一个表（[memory] / [tmux]），字符串和布尔。不支持别的类型，也不需要。"""
    lines = [
        _("# atm 的用户配置。改完立即生效，不用重启。"),
        _("# `atm config` 查看，`atm config <key> <value>` 修改。"),
    ]
    sections = dict.fromkeys(k.split(".", 1)[0] for k in KEYS)  # 保持 KEYS 里的出现顺序
    for section in sections:
        lines += ["", f"[{section}]"]
        for key, field in KEYS.items():
            if not key.startswith(section + "."):
                continue
            name = key.split(".", 1)[1].replace("-", "_")
            value = getattr(cfg, field)
            if isinstance(value, bool):
                lines.append(f"{name} = {'true' if value else 'false'}")
            elif isinstance(value, int):
                lines.append(f"{name} = {value}")
            else:
                lines.append(f'{name} = "{value}"')
    return "\n".join(lines) + "\n"


def _coerce(key: str, value: object):
    field = KEYS[key]
    kind = next(f.type for f in fields(Config) if f.name == field)
    if kind == "bool":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        raise ConfigError(_("{key} 要 true/false，收到 {value!r}").format(key=key, value=value))
    if kind == "int":
        if isinstance(value, bool) or not re.match(r"^\d+$", str(value).strip()):
            raise ConfigError(_("{key} 要非负整数，收到 {value!r}").format(key=key, value=value))
        n = int(str(value).strip())
        if key == "tmux.base-index" and n > 1:
            raise ConfigError(
                _("{key} 只能是 0（不写）或 1，收到 {value!r}").format(key=key, value=value)
            )
        return n
    s = str(value).strip()
    if key == "memory.slice":
        if not re.match(r"^[\w.-]+\.slice$", s):
            raise ConfigError(
                _("{key} 要形如 name.slice，收到 {value!r}").format(key=key, value=value)
            )
        return s
    if key in ("memory.slice-high", "memory.slice-max"):
        return "auto" if s.lower() == "auto" else validate_size(key, s)
    if key in ("keys.pick", "keys.sidebar"):
        if not re.match(r"^[a-z]$", s):
            raise ConfigError(
                _("{key} 只能是单个小写字母，收到 {value!r}（大写留给配套的第二条绑定）").format(
                    key=key, value=value
                )
            )
        return s
    if key in ("keys.popup-width", "keys.popup-height"):
        if not re.match(r"^[1-9]\d*%?$", s):
            raise ConfigError(
                _("{key} 要形如 80% 或 120，收到 {value!r}").format(key=key, value=value)
            )
        return s
    return validate_size(key, s)


def validate(cfg: Config) -> None:
    """跨字段的约束。单字段的在 _coerce 里。"""
    if cfg.keys_pick == cfg.keys_sidebar:
        raise ConfigError(
            _("keys.pick 和 keys.sidebar 不能相同（都是 {key!r}）").format(key=cfg.keys_pick)
        )


# ---------------------------------------------------------------- 启动包装


def launch_argv(program: str, args: list[str], cfg: Config) -> list[str]:
    """`atm claude …` 真正 exec 的 argv。有闸门就裹 systemd-run，没有就是原样。

    纯函数，不查 PATH、不 exec，好测。
    """
    limit = cfg.memory_limit()
    if limit is None:
        return [program, *args]
    return [*limit.systemd_args(f"atm: {program}"), program, *args]


def resolve_program(program: str) -> str | None:
    """PATH 上找可执行文件。找不到返回 None，让调用方给人话。"""
    return shutil.which(program)
