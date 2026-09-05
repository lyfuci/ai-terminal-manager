"""`atm config`：用户级配置，目前只管一件事 —— 启动 AI CLI 时套什么内存闸门。

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
}

_HELP: dict[str, str] = {
    "memory.enabled": "要不要套闸门（true/false）",
    "memory.high": "软上限：超了节流 + 回收，不杀（如 4G）",
    "memory.max": "硬上限：回收压不住才杀，杀的是整个会话及其子进程（如 8G）",
    "memory.swap-max": "swap 上限（WSL 上 swap 在宿主 SSD，建议小）",
    "memory.slice": "所有会话共同归入的 systemd slice，兜总量",
    "memory.user": "systemd-run 是否走 --user（一般 true）",
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
    return cfg


def save(cfg: Config, path: Path | None = None) -> Path:
    """原子写：先写临时文件再 rename，中途断电不会留下半个 TOML。"""
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
        f"配置文件：{config_path()}{'' if config_path().exists() else '（不存在，全部默认）'}",
        f"优先级：命令行参数 > 环境变量（{ENV_PREFIX}MEMORY_HIGH 这类）> 文件 > 默认",
        "",
    ]
    for key, field in KEYS.items():
        value = getattr(cfg, field)
        shown = str(value).lower() if isinstance(value, bool) else value
        src = sources.get(key, "default")
        tag = "" if src == "default" else f"  ← {src}"
        lines.append(f"  {key:<16} {shown:<12}{tag}")
        lines.append(f"  {'':<16} {_(_HELP[key])}")
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
    """扁平 TOML：一个 [memory] 表，字符串和布尔。不支持别的类型，也不需要。"""
    lines = [
        "# atm 的用户配置。改完立即生效，不用重启。",
        "# `atm config` 查看，`atm config <key> <value>` 修改。",
        "",
        "[memory]",
    ]
    for key, field in KEYS.items():
        name = key.split(".", 1)[1].replace("-", "_")
        value = getattr(cfg, field)
        if isinstance(value, bool):
            lines.append(f"{name} = {'true' if value else 'false'}")
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
    s = str(value).strip()
    if key == "memory.slice":
        if not re.match(r"^[\w.-]+\.slice$", s):
            raise ConfigError(
                _("{key} 要形如 name.slice，收到 {value!r}").format(key=key, value=value)
            )
        return s
    return validate_size(key, s)


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
