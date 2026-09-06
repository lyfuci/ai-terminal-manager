"""`atm update`：让 atm 自己升级自己。

难点不在"跑一条升级命令"，在于**判断该跑哪一条**——同一个 `atm` 可能是 uv tool / pipx / pip 装的，
装的来源又可能是 PyPI、git URL 或本地 editable。判断错了会出现两份 atm 打架
（比如 `pip install -U` 装进了另一个解释器，PATH 上的 `atm` 还是旧的）。

判断依据两处，都是标准的：
1. `sys.executable` 的路径：uv tool 的 venv 在 `…/uv/tools/<name>/…`，
   pipx 的在 `…/pipx/venvs/<name>/…`。
2. PEP 610 的 `direct_url.json`（`importlib.metadata` 能读）：有 `vcs_info` 就是 git 装的，
   `dir_info.editable` 就是 editable；都没有就是从索引（PyPI / 镜像）装的。

版本比较只是提示，不是闸门：查不到 PyPI（离线 / 镜像滞后）照样可以升级。
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from importlib import metadata

from .i18n import _

DIST_NAME = "ai-terminal-manager"
PYPI_JSON = f"https://pypi.org/pypi/{DIST_NAME}/json"
PYPI_SIMPLE = "https://pypi.org/simple"
GIT_URL = "https://github.com/lyfuci/ai-terminal-manager"


class Installer(StrEnum):
    UV_TOOL = "uv tool"
    PIPX = "pipx"
    PIP = "pip"
    UNKNOWN = "unknown"


class Origin(StrEnum):
    INDEX = "index"  # PyPI 或镜像
    GIT = "git"
    EDITABLE = "editable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class InstallInfo:
    installer: Installer
    origin: Origin
    detail: str = ""  # git 的 commit / editable 的目录，只用于展示

    def describe(self) -> str:
        parts = [self.installer.value, self.origin.value]
        if self.detail:
            parts.append(self.detail)
        return " · ".join(parts)


def current_version() -> str:
    from . import __version__

    return __version__


_READ_ENV = object()


def detect(
    executable: str | None = None, direct_url: str | object | None = _READ_ENV
) -> InstallInfo:
    """两个参数都可注入，方便测试；默认看真实环境。

    `direct_url=None` 表示「确实没有这个文件」，和「没传」是两回事。
    """
    exe = executable if executable is not None else sys.executable
    if direct_url is _READ_ENV:
        direct_url = _read_direct_url()
    assert direct_url is None or isinstance(direct_url, str)

    if "/uv/tools/" in exe or "\\uv\\tools\\" in exe:
        installer = Installer.UV_TOOL
    elif "/pipx/venvs/" in exe or "\\pipx\\venvs\\" in exe:
        installer = Installer.PIPX
    elif direct_url is not None or _dist_installed():
        installer = Installer.PIP
    else:
        installer = Installer.UNKNOWN

    origin, detail = Origin.INDEX, ""
    if direct_url:
        try:
            data = json.loads(direct_url)
        except json.JSONDecodeError:
            data = {}
        if isinstance(data.get("dir_info"), dict) and data["dir_info"].get("editable"):
            origin, detail = Origin.EDITABLE, str(data.get("url", "")).removeprefix("file://")
        elif isinstance(data.get("vcs_info"), dict):
            origin = Origin.GIT
            detail = str(data["vcs_info"].get("commit_id", ""))[:7]
        elif data.get("url"):
            origin, detail = Origin.GIT, ""
    if installer is Installer.UNKNOWN:
        origin = Origin.UNKNOWN
    return InstallInfo(installer, origin, detail)


def upgrade_command(info: InstallInfo) -> list[str] | None:
    """None = 我们不该替用户升级（editable 或认不出来），调用方给人话。"""
    if info.origin is Origin.EDITABLE:
        return None
    if info.installer is Installer.UV_TOOL:
        # git 装的：uv 会重新解析同一个 ref，拿到最新 commit；PyPI 装的：拿最新发行版。
        return ["uv", "tool", "upgrade", DIST_NAME]
    if info.installer is Installer.PIPX:
        return ["pipx", "upgrade", DIST_NAME]
    if info.installer is Installer.PIP:
        target = f"git+{GIT_URL}" if info.origin is Origin.GIT else DIST_NAME
        return [sys.executable, "-m", "pip", "install", "--upgrade", target]
    return None


def direct_index_command(info: InstallInfo) -> list[str] | None:
    """镜像滞后时的兜底：绕过用户配置的索引，直连 PyPI 装最新版。

    实测场景：国内镜像同步 PyPI 有几分钟到一小时的延迟。`atm update` 问 PyPI 本尊知道有 0.6.0，
    但 `uv tool upgrade` 走镜像只看得到 0.4.0，于是"Nothing to upgrade"、版本原地不动。
    只对从索引装的有意义；git 装的不经过索引。
    """
    if info.origin is not Origin.INDEX:
        return None
    if info.installer is Installer.UV_TOOL:
        return ["uv", "tool", "install", "--reinstall", "--default-index", PYPI_SIMPLE, DIST_NAME]
    if info.installer is Installer.PIPX:
        return ["pipx", "install", "--force", "--index-url", PYPI_SIMPLE, DIST_NAME]
    if info.installer is Installer.PIP:
        return [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--index-url",
            PYPI_SIMPLE,
            DIST_NAME,
        ]
    return None


def installed_version_from(output: str) -> str | None:
    """`atm --version` 的输出（`atm 0.6.0`）→ `0.6.0`；认不出返回 None。"""
    m = re.search(r"(\d+\.\d+\.\d+)", output)
    return m.group(1) if m else None


def manual_hint(info: InstallInfo) -> str:
    if info.origin is Origin.EDITABLE:
        where = info.detail or _("源码目录")
        return _(
            "这是 editable 安装（{where}），升级就是 `git pull`；改了依赖再 `uv sync --frozen`。"
        ).format(where=where)
    return _(
        "认不出这份 atm 是怎么装的。手动升级：\n  uv tool upgrade {DIST_NAME} "
        "       # uv tool 装的\n  pipx upgrade {DIST_NAME} "
        "          # pipx 装的\n  pip install -U {DIST_NAME} "
        "        # pip 装的（注意 PyPI 上的 `atm` 是别人的包）"
    ).format(DIST_NAME=DIST_NAME)


def latest_version(timeout: float = 5.0) -> str | None:
    """问 PyPI 最新版本。拿不到返回 None，绝不抛——它只是提示。"""
    try:
        req = urllib.request.Request(PYPI_JSON, headers={"User-Agent": f"atm/{current_version()}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        version = data.get("info", {}).get("version")
        return str(version) if version else None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def version_tuple(v: str) -> tuple[int, ...]:
    """够用的版本比较：只认 `1.2.3` 这种，别的段当 0。"""
    out = []
    for part in v.split("."):
        m = re.match(r"\d+", part)  # 只取开头的数字：`0rc1` 是 0，不是 01
        out.append(int(m.group()) if m else 0)
    return tuple(out)


def _read_direct_url() -> str | None:
    try:
        return metadata.distribution(DIST_NAME).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None


def _dist_installed() -> bool:
    try:
        metadata.distribution(DIST_NAME)
        return True
    except metadata.PackageNotFoundError:
        return False
