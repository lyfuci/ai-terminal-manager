"""i18n：翻译表完整、占位符一致、语言探测正确、真实命令能切语言。"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from atm import cli, config, i18n
from atm.i18n_catalog import CATALOG, EN, JA

SRC = Path(__file__).resolve().parents[1] / "src" / "atm"
PLACEHOLDER = re.compile(r"\{[^{}]*\}")


def _source_keys() -> set[str]:
    """源码里所有 `_()` 的字符串参数 + config._HELP 的值。"""
    keys: set[str] = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                keys.add(node.args[0].value)
    keys.update(config._HELP.values())
    return keys


@pytest.fixture(autouse=True)
def _reset_lang(monkeypatch):
    for var in ("ATM_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(var, raising=False)
    i18n.reset_lang()
    yield
    i18n.reset_lang()


@pytest.mark.parametrize("table_name", ["EN", "JA"])
def test_every_source_string_is_translated(table_name: str) -> None:
    table = {"EN": EN, "JA": JA}[table_name]
    missing = sorted(_source_keys() - set(table))
    assert not missing, f"{table_name} 缺 {len(missing)} 条：\n" + "\n".join(missing[:10])


@pytest.mark.parametrize("table_name", ["EN", "JA"])
def test_no_stale_keys_in_catalog(table_name: str) -> None:
    table = {"EN": EN, "JA": JA}[table_name]
    stale = sorted(set(table) - _source_keys())
    assert not stale, f"{table_name} 里有源码已不存在的键：\n" + "\n".join(stale[:10])


@pytest.mark.parametrize("table_name", ["EN", "JA"])
def test_placeholders_match_source(table_name: str) -> None:
    """翻译漏掉一个 {name} 就是运行时 KeyError；多一个也是。"""
    table = {"EN": EN, "JA": JA}[table_name]
    bad = []
    for key, value in table.items():
        if sorted(PLACEHOLDER.findall(key)) != sorted(PLACEHOLDER.findall(value)):
            bad.append(key)
    assert not bad, f"{table_name} 占位符不一致：\n" + "\n".join(bad[:10])


def test_translations_are_not_identical_to_source() -> None:
    """翻译表里全等于原文的条目多半是忘了翻。允许极少数本来就是符号的。"""
    same = [k for k, v in EN.items() if k == v]
    assert len(same) <= 2, same


def test_lang_detection(monkeypatch) -> None:
    assert i18n._detect() in ("en", "zh", "ja")  # 没设任何变量 → 走 locale 兜底，不崩
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    assert i18n._detect() == "zh"
    monkeypatch.setenv("LANG", "ja_JP.UTF-8")
    assert i18n._detect() == "ja"
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    assert i18n._detect() == "en"
    monkeypatch.setenv("LANG", "C.UTF-8")
    assert i18n._detect() == "en"
    monkeypatch.setenv("LC_ALL", "zh_TW.UTF-8")  # LC_ALL 压过 LANG
    assert i18n._detect() == "zh"
    monkeypatch.setenv("ATM_LANG", "en")  # ATM_LANG 压过一切
    assert i18n._detect() == "en"
    monkeypatch.setenv("ATM_LANG", "klingon")
    assert i18n._detect() == "en"


def test_tr_falls_back_to_source_when_missing(monkeypatch) -> None:
    monkeypatch.setenv("ATM_LANG", "en")
    i18n.reset_lang()
    assert i18n.tr("这条消息不在表里") == "这条消息不在表里"
    assert i18n.tr("== 数据源 ==") == "== Data sources =="


def test_zh_returns_source(monkeypatch) -> None:
    monkeypatch.setenv("ATM_LANG", "zh")
    i18n.reset_lang()
    assert i18n.tr("== 数据源 ==") == "== 数据源 =="


def test_help_switches_language(monkeypatch, capsys) -> None:
    monkeypatch.setenv("ATM_LANG", "en")
    i18n.reset_lang()
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "list sessions" in out and "列出会话" not in out

    monkeypatch.setenv("ATM_LANG", "ja")
    i18n.reset_lang()
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "セッションを一覧" in capsys.readouterr().out


def test_formatted_message_in_english(monkeypatch, capsys, tmp_path: Path) -> None:
    """带占位符的翻译真的能 format 出来（不是只在表里对得上）。"""
    monkeypatch.setenv("ATM_LANG", "en")
    i18n.reset_lang()
    monkeypatch.setenv("ATM_CONFIG", str(tmp_path / "c.toml"))
    assert cli.main(["config", "memory.high", "lots"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "must look like 4G / 512M / infinity" in err and "'lots'" in err


def test_all_catalog_entries_format_without_error() -> None:
    """用假参数把每条带占位符的翻译都 format 一遍，抓格式串本身的语法错。"""

    class Any:
        def __format__(self, spec):
            return "x"

        def __repr__(self):
            return "x"

    for lang_table in CATALOG.values():
        for key, value in lang_table.items():
            names = {m.strip("{}").split("!")[0].split(":")[0] for m in PLACEHOLDER.findall(key)}
            kwargs = {n: Any() for n in names if n}
            value.format(**kwargs)
