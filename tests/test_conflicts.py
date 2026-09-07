"""块外同名 tmux 设置的识别与报告。

这一组的立场是：**宁可漏报，不可误报**。误报会让用户去删一行其实无关的配置，
比不报更糟。所以每个「不该命中」的用例都和「该命中」的一样重要。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atm import config, conflicts, sync, tmux, tmuxopts

MOUSE = {"mouse": "tmux.mouse"}


def keys(text: str, options=MOUSE):
    return [(c.line_no, c.option, c.certain) for c in conflicts.scan(text, options)]


def marks(text: str, options=MOUSE):
    return [
        (c.line_no, c.option, c.certain, c.unsets, c.after_atm_block)
        for c in conflicts.scan(text, options)
    ]


# ---------------------------------------------------------------- 认得出来


@pytest.mark.parametrize(
    "line",
    [
        "set -g mouse on",
        "set-option -g mouse on",
        "set -gq mouse on",
        "set -g -q mouse on",
        "set mouse on",  # 没有 -g 也是设置
        "  set   -g   mouse   off  ",  # 空白随便
        "setw -g mouse on",
        "set-window-option -g mouse on",
    ],
)
def test_recognises_every_spelling(line: str) -> None:
    assert keys(line) == [(1, "mouse", True)]


def test_reports_line_numbers_users_can_act_on() -> None:
    text = "# 注释\n\nset -g status off\nset -g mouse on\n"
    assert keys(text) == [(4, "mouse", True)]


def test_base_index_maps_two_tmux_options_to_one_atm_key() -> None:
    text = "set -g base-index 1\nsetw -g pane-base-index 1\n"
    options = {"base-index": "tmux.base-index", "pane-base-index": "tmux.base-index"}
    found = conflicts.scan(text, options)
    assert [c.option for c in found] == ["base-index", "pane-base-index"]
    assert {c.key for c in found} == {"tmux.base-index"}


# ---------------------------------------------------------------- 不该误报


@pytest.mark.parametrize(
    "line",
    [
        "# set -g mouse on",  # 注释
        "bind-key m set -g mouse on",  # 是 bind-key 的参数，不是当下的设置
        "if-shell 'test x' 'set -g mouse on'",  # 同上
        "set -g status-right 'mouse'",  # 值里含 mouse，选项不是它
        "set -sg escape-time 0",  # 别的选项
        "source-file ~/other.conf",
        "",
    ],
)
def test_never_flags_a_line_that_is_not_a_top_level_setting(line: str) -> None:
    assert keys(line) == []


def test_only_reports_options_atm_currently_manages() -> None:
    text = "set -g mouse on\nset -g history-limit 50000\n"
    assert keys(text) == [(1, "mouse", True)]  # history-limit 没在 options 里


def test_empty_option_map_reports_nothing() -> None:
    assert conflicts.scan("set -g mouse on", {}) == ()


# ---------------------------------------------------------------- 不确定的算不确定


def test_line_inside_a_brace_block_is_uncertain() -> None:
    """`bind-key R { ... }` 里的一行，单看和顶层设置一模一样，但它只在按键时才跑。

    早期设计想把冲突行注释掉，正是这种情况会把用户的键位改坏 —— 所以这里必须能区分。
    """
    text = "bind-key R {\n  set -g mouse on\n}\nset -g mouse off\n"
    assert keys(text) == [(2, "mouse", False), (4, "mouse", True)]


def test_conditional_block_is_uncertain() -> None:
    text = "%if #{==:#{host},work}\nset -g mouse on\n%endif\nset -g mouse off\n"
    assert keys(text) == [(2, "mouse", False), (4, "mouse", True)]


def test_nested_braces_close_correctly() -> None:
    text = "bind-key R {\n  if -F x {\n    set -g mouse on\n  }\n}\nset -g mouse on\n"
    assert keys(text) == [(3, "mouse", False), (6, "mouse", True)]


# ---------------------------------------------------------------- atm 自己的块要跳过


def test_skips_all_three_atm_blocks() -> None:
    text = (
        f"{tmuxopts.MARKER_BEGIN}\nset -g mouse on\n{tmuxopts.MARKER_END}\n"
        "# >>> atm (ai-terminal-manager) >>>\nset -g mouse on\n# <<< atm <<<\n"
        "# >>> atm persist (tmux-resurrect + tmux-continuum) >>>\n"
        "set -g mouse on\n# <<< atm persist <<<\n"
        "set -g mouse on\n"
    )
    assert keys(text) == [(10, "mouse", True)]  # 只剩用户自己那行


def test_atm_block_constants_stay_in_sync_with_the_modules_that_write_them() -> None:
    """这个模块自己抄了三对 marker（不想把 install/persist 拖进 import 链），所以要对齐检查。"""
    from atm import install, persist

    assert set(conflicts.ATM_BLOCKS) == {
        (install.MARKER_BEGIN, install.MARKER_END),
        (persist.MARKER_BEGIN, persist.MARKER_END),
        (tmuxopts.MARKER_BEGIN, tmuxopts.MARKER_END),
    }  # 精确相等：多一对、少一对、改了字面量都要红


def test_unclosed_atm_block_does_not_swallow_the_rest_silently() -> None:
    """块只有开始没有结束时，剩下的行都算块内 —— 宁可漏报也不误报。"""
    text = f"{tmuxopts.MARKER_BEGIN}\nset -g mouse on\nset -g mouse off\n"
    assert keys(text) == []


# ---------------------------------------------------------------- 「我没看全」


def test_reports_other_config_files_tmux_also_reads(tmp_path: Path, monkeypatch) -> None:
    """实测 tmux 3.4 同时读 ~/.tmux.conf 和 ~/.config/tmux/tmux.conf，后者在后面赢。"""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    xdg = tmp_path / "tmux" / "tmux.conf"
    xdg.parent.mkdir(parents=True)
    xdg.write_text("set -g mouse off\n", encoding="utf-8")

    found = conflicts.other_config_files(tmp_path / ".tmux.conf")

    assert xdg in found


def test_does_not_list_the_file_it_is_already_scanning(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    conf = tmp_path / "tmux" / "tmux.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text("", encoding="utf-8")

    assert conf not in conflicts.other_config_files(conf)


def test_finds_sourced_files() -> None:
    text = (
        "source-file ~/a.conf\nsource ~/b.conf\n# source-file ~/skipped.conf\nsource-file '~/c'\n"
    )
    assert conflicts.sourced_files(text) == ("~/a.conf", "~/b.conf", "~/c")


# ---------------------------------------------------------------- 端到端：不再说假话


def test_turning_an_option_off_no_longer_claims_it_is_off(tmp_path: Path, monkeypatch) -> None:
    """原来的 bug：用户自己那行还在，atm 却报告成功，鼠标其实一直开着。"""
    monkeypatch.setattr(tmux, "has_server", lambda: False)
    conf = tmp_path / "tmux.conf"
    conf.write_text("set -g mouse on   # 我自己写的\n", encoding="utf-8")
    on = config.Config(tmux_mouse=True)
    tmuxopts.apply(tmuxopts.build_plan(on, conf_path=conf))

    notes = "\n".join(sync.apply_changes(on, config.Config(), conf_path=conf))

    assert "不再管" in notes
    assert "仍然在设" in notes
    assert f"{conf}:1" in notes  # 指到具体行，用户能直接去删
    assert conf.read_text(encoding="utf-8") == "set -g mouse on   # 我自己写的\n"


def test_enabling_an_option_warns_that_a_later_line_wins(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(tmux, "has_server", lambda: False)
    conf = tmp_path / "tmux.conf"
    conf.write_text("set -g history-limit 50000\n", encoding="utf-8")

    plan = tmuxopts.build_plan(config.Config(tmux_history_limit=10000), conf_path=conf)
    described = plan.describe()

    assert [c.line_no for c in plan.conflicts] == [1]
    assert "也设了同样的选项" in described and "history-limit" in described


def test_no_conflict_no_noise(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(tmux, "has_server", lambda: False)
    conf = tmp_path / "tmux.conf"
    conf.write_text("set -g status off\n", encoding="utf-8")

    plan = tmuxopts.build_plan(config.Config(tmux_mouse=True), conf_path=conf)

    assert plan.conflicts == ()
    assert "也设了同样的选项" not in plan.describe()


# ---------------------------------------------------------------- 词法：审查找出来的每一种误报
#
# 这一组全部来自一次外部审查。每一条当初都会让 atm 建议用户删掉一行**属于别的命令**的配置。


@pytest.mark.parametrize(
    "name,text",
    [
        ("反斜杠续行让下一行成为 bind-key 的参数", "bind-key m \\\n    set -g mouse on\n"),
        ("注释也会被续行接下去", "# example \\\nset -g mouse on\n"),
        ("bind-key 的行内参数", "bind-key m set -g mouse on\n"),
        ("if-shell 的引号参数", "if-shell 'test x' 'set -g mouse on'\n"),
    ],
)
def test_never_mistakes_another_commands_argument_for_a_setting(name: str, text: str) -> None:
    assert keys(text) == [], name


@pytest.mark.parametrize(
    "name,text",
    [
        ("引号里的花括号不闭合块", 'bind-key R {\n  display-message "}"\n  set -g mouse on\n}\n'),
        ("注释里的花括号不闭合块", "bind-key R {\n  # }\n  set -g mouse on\n}\n"),
    ],
)
def test_braces_in_quotes_or_comments_do_not_close_the_block(name: str, text: str) -> None:
    assert keys(text) == [(3, "mouse", False)], name


def test_target_flag_consumes_its_argument() -> None:
    """`set -t mouse status off`：mouse 是**目标**，选项是 status。认错会误报。"""
    options = {"mouse": "tmux.mouse", "status": "tmux.status"}
    assert keys("set -t mouse status off\n", options) == [(1, "status", True)]


def test_conditional_set_is_not_certain() -> None:
    """`-o` = 已经设过就不覆盖，所以它生效与否取决于之前有没有设过。"""
    assert keys("set -go mouse off\n") == [(1, "mouse", False)]


def test_unset_is_reported_but_marked_as_such() -> None:
    """`-u` 是清掉值，不等于「把它打开了」，措辞不能混为一谈。"""
    assert marks("set -gu mouse\n") == [(1, "mouse", True, True, True)]


@pytest.mark.parametrize(
    "name,text",
    [
        ("tmux 接受无歧义的命令前缀", "set-o -g mouse on\n"),
        ("选项名本身在续行的下一行", "set -g \\\n    mouse on\n"),
        ("分号连写的第二条", "set -g status off ; set -g mouse on\n"),
        ("花括号闭合后分号再接顶层", "bind-key R { display-message hi }; set -g mouse on\n"),
    ],
)
def test_does_not_miss_ordinary_spellings(name: str, text: str) -> None:
    assert any(c.option == "mouse" and c.certain for c in conflicts.scan(text, MOUSE)), name


def test_knows_whether_a_line_comes_after_atms_block() -> None:
    """「它压过 atm」这句话只在真的排在后面时才能说。"""
    before = f"set -g mouse on\n{tmuxopts.MARKER_BEGIN}\nset -g mouse off\n{tmuxopts.MARKER_END}\n"
    after = f"{tmuxopts.MARKER_BEGIN}\nset -g mouse off\n{tmuxopts.MARKER_END}\nset -g mouse on\n"
    assert [c.after_atm_block for c in conflicts.scan(before, MOUSE)] == [False]
    assert [c.after_atm_block for c in conflicts.scan(after, MOUSE)] == [True]


# ---------------------------------------------------------------- 报告本身


def test_reported_line_numbers_match_the_file_after_the_write(tmp_path: Path, monkeypatch) -> None:
    """写块会把用户的行往下挤。报告必须重新扫写完的文件，否则行号指到 atm 自己的 marker 上。"""
    monkeypatch.setattr(tmux, "has_server", lambda: False)
    conf = tmp_path / "tmux.conf"
    conf.write_text("set -g mouse off\n", encoding="utf-8")

    result = tmuxopts.apply(tmuxopts.build_plan(config.Config(tmux_mouse=True), conf_path=conf))

    lines = conf.read_text(encoding="utf-8").splitlines()
    assert [c.line_no for c in result.conflicts] == [
        i for i, line in enumerate(lines, 1) if line.strip() == "set -g mouse off"
    ]
    reported = result.conflicts[0].line_no
    assert lines[reported - 1].strip() == "set -g mouse off"  # 指到的确实是那一行


def test_report_never_claims_an_outcome_it_did_not_verify(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(tmux, "has_server", lambda: False)
    conf = tmp_path / "tmux.conf"
    conf.write_text("set -g mouse on\n", encoding="utf-8")

    result = tmuxopts.apply(tmuxopts.build_plan(config.Config(tmux_mouse=True), conf_path=conf))
    text = "\n".join(tmuxopts.report_lines(result))

    assert "说了算" not in text  # 不断言谁最终赢
    assert "atm 只看了" in text  # 明说范围有限
    assert "run-shell -b" in text  # 明说插件可以在之后再改


def test_scope_caveat_appears_even_with_no_other_files(tmp_path: Path, monkeypatch) -> None:
    """原来只有发现了别的配置文件才提「我没看全」，那让人以为没提就等于看全了。"""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    monkeypatch.setattr(tmux, "has_server", lambda: False)
    conf = tmp_path / "tmux.conf"
    conf.write_text("set -g mouse on\n", encoding="utf-8")

    result = tmuxopts.apply(tmuxopts.build_plan(config.Config(tmux_mouse=True), conf_path=conf))

    assert result.other_files == ()
    assert any("atm 只看了" in line for line in tmuxopts.report_lines(result))


def test_uncertain_released_conflicts_are_still_reported(tmp_path: Path, monkeypatch) -> None:
    """关掉一项时，%if 里的同名设置也要提 —— 漏掉它等于假装干净。"""
    monkeypatch.setattr(tmux, "has_server", lambda: False)
    conf = tmp_path / "tmux.conf"
    conf.write_text("%if 1\nset -g mouse on\n%endif\n", encoding="utf-8")
    on = config.Config(tmux_mouse=True)
    tmuxopts.apply(tmuxopts.build_plan(on, conf_path=conf))

    result = tmuxopts.apply(tmuxopts.build_plan(config.Config(), conf_path=conf))
    text = "\n".join(tmuxopts.report_lines(result))

    assert [c.certain for c in result.released] == [False]
    assert "未必生效" in text


def test_install_path_reports_released_conflicts_too(tmp_path: Path, monkeypatch, capsys) -> None:
    """`atm install` 走的是 cli._report_tmuxopts，它以前根本不查关掉那条路。"""
    from atm import cli

    monkeypatch.setattr(tmux, "has_server", lambda: False)
    conf = tmp_path / "tmux.conf"
    conf.write_text("set -g mouse on\n", encoding="utf-8")
    tmuxopts.apply(tmuxopts.build_plan(config.Config(tmux_mouse=True), conf_path=conf))

    result = tmuxopts.apply(tmuxopts.build_plan(config.Config(), conf_path=conf))
    cli._report_tmuxopts(result)

    out = capsys.readouterr().out
    assert "仍然在设" in out and f"{conf}:" in out
