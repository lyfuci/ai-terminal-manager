# ------------------------------------------------- install 的两个静默失败


def test_install_rejects_non_letter_key() -> None:
    """第二条绑定靠 key.upper() 区分；数字的 upper() 是恒等 → 两条撞成一个键。"""
    import pathlib

    import pytest

    from atm import install

    for bad in ("1", "F1", "", "ab", "-"):
        with pytest.raises(ValueError, match="小写字母"):
            install.build_plan(conf_path=pathlib.Path("/tmp/atm-nonexistent.conf"), key=bad)


def test_install_letter_key_makes_two_distinct_bindings() -> None:
    import pathlib

    from atm import install

    plan = install.build_plan(conf_path=pathlib.Path("/tmp/atm-nonexistent.conf"), key="s")
    keys = [b.key for b in plan.bindings]
    assert keys == ["s", "S"]


def test_marker_detection_matches_stripping() -> None:
    """检测用子串、剥离用整行 → uninstall 报成功却一行都没删（实测复现过）。"""
    from atm import install

    # 用户在 marker 行尾加了自己的注释
    text = (
        f"{install.MARKER_BEGIN} (我自己加的)\n"
        f"bind-key a x\n{install.MARKER_END}\nset -g mouse on\n"
    )
    assert install._has_marker(text) is False, "整行对不上就不该算已安装"
    assert install._strip_block(text) == text, "认不出来就必须原样返回，不能乱删"

    clean = f"{install.MARKER_BEGIN}\nbind-key a x\n{install.MARKER_END}\nset -g mouse on\n"
    assert install._has_marker(clean) is True
    assert install._has_marker(install._strip_block(clean)) is False


def test_cli_reports_bad_key_without_traceback() -> None:
    """参数级的 ValueError 要变成一行人话，不是一屏 traceback。"""
    from atm import cli

    assert cli.main(["install", "--key", "1", "--print"]) == cli.EXIT_ERROR
