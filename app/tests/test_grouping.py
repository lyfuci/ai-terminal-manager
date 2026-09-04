"""会话选择器的按目录聚合。

组标题不可选中，所以光标移动必须跳过它 —— 这是分组功能最容易出错的地方。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from atm.model import SessionEntry, Source
from atm.tui import GroupHeader, GroupMode, _SessionPicker


def entry(title: str, cwd: str, days_ago: float = 0) -> SessionEntry:
    return SessionEntry(
        id=title,
        title=title,
        source=Source.CLAUDE,
        cwd=cwd,
        git_branch=None,
        updated_at=datetime.now(tz=UTC) - timedelta(days=days_ago),
        path=f"/tmp/{title}.jsonl",
        size_bytes=1000,
    )


def picker(entries, query="", mode=GroupMode.CWD) -> _SessionPicker:
    p = _SessionPicker(entries, query, mode)
    p._recompute()
    return p


def test_groups_by_cwd() -> None:
    p = picker([entry("a", "/p/one"), entry("b", "/p/two"), entry("c", "/p/one")])
    headers = [r for r in p._rows if isinstance(r, GroupHeader)]
    assert {h.key for h in headers} == {"/p/one", "/p/two"}
    assert {h.key: h.count for h in headers} == {"/p/one": 2, "/p/two": 1}


def test_group_order_follows_best_entry_not_group_size() -> None:
    """会话多的目录不该因为条数多就被顶到最上面。"""
    entries = [
        entry("recent", "/p/small", days_ago=0),
        entry("old1", "/p/big", days_ago=10),
        entry("old2", "/p/big", days_ago=11),
        entry("old3", "/p/big", days_ago=12),
    ]
    p = picker(entries)
    first_header = next(r for r in p._rows if isinstance(r, GroupHeader))
    assert first_header.key == "/p/small"


def test_cursor_starts_on_a_selectable_row() -> None:
    p = picker([entry("a", "/p/one")])
    assert isinstance(p._rows[0], GroupHeader)
    assert p._cursor == 1
    assert p._selected() is not None


def test_cursor_skips_group_headers_going_down() -> None:
    p = picker([entry("a", "/p/one"), entry("b", "/p/two")])
    # rows: [H(one), a, H(two), b]
    assert p._selected().title == "a"
    p._move(1)
    assert p._selected().title == "b"  # 跳过了 H(two)


def test_cursor_skips_group_headers_going_up() -> None:
    p = picker([entry("a", "/p/one"), entry("b", "/p/two")])
    p._move(1)
    p._move(-1)
    assert p._selected().title == "a"


def test_move_past_end_stops_on_last_selectable() -> None:
    p = picker([entry("a", "/p/one"), entry("b", "/p/two")])
    p._move(999)
    assert p._selected() is not None
    assert p._selected().title == "b"


def test_move_past_start_stops_on_first_selectable() -> None:
    p = picker([entry("a", "/p/one"), entry("b", "/p/two")])
    p._move(999)
    p._move(-999)
    assert p._selected().title == "a"


def test_cycle_grouping_goes_cwd_agent_flat() -> None:
    p = picker([entry("a", "/p/one"), entry("b", "/p/two")])
    assert p._mode is GroupMode.CWD
    p._cycle_grouping()
    assert p._mode is GroupMode.AGENT
    p._cycle_grouping()
    assert p._mode is GroupMode.NONE
    assert not any(isinstance(r, GroupHeader) for r in p._rows)
    p._cycle_grouping()
    assert p._mode is GroupMode.CWD


def test_flat_mode_selects_first_row() -> None:
    p = picker([entry("a", "/p/one")], mode=GroupMode.NONE)
    assert p._cursor == 0
    assert p._selected().title == "a"


def test_query_filters_within_groups() -> None:
    p = picker([entry("alpha", "/p/one"), entry("beta", "/p/two")], query="alpha")
    headers = [r for r in p._rows if isinstance(r, GroupHeader)]
    assert [h.key for h in headers] == ["/p/one"]  # 空组不显示
    assert p._matches == 1


def test_empty_result_has_no_selection() -> None:
    p = picker([entry("alpha", "/p/one")], query="zzzznomatch")
    assert p._rows == ()
    assert p._selected() is None


def test_group_label_shortens_home() -> None:
    from pathlib import Path

    h = GroupHeader(
        mode=GroupMode.CWD, key=str(Path.home() / "proj"), count=3, newest=datetime.now(tz=UTC)
    )
    assert h.label().startswith("~/proj")
    assert "(3)" in h.label()


# ------------------------------------------------- TUI 边界（workflow 发现，逐条实测复现过）


def test_option_picker_scrolls_with_cursor() -> None:
    """选项多于可见行时必须跟着滚。

    之前从 index 0 直画到画不下为止，而光标能移到 len-1 ——
    pane 多于 height-3 时光标停在**画不出来的选项**上，Enter 就投到看不见的 pane。
    """
    from atm.tui import PickerOption, _OptionPicker

    opts = [PickerOption(key=f"%{i}", label=f"pane {i}") for i in range(20)]
    p = _OptionPicker(opts, "t")
    p._move(19)
    rows = 7  # 终端高 10 → height-3
    p._offset = max(0, min(p._offset, len(opts) - rows))
    if p._cursor >= p._offset + rows:
        p._offset = p._cursor - rows + 1
    assert p._offset <= p._cursor < p._offset + rows, "光标跑出了可见窗口"


def test_option_picker_digit_is_relative_to_window() -> None:
    """数字键选的必须是**画出来的**那几项，不是绝对下标。"""
    from atm.tui import PickerOption, _OptionPicker

    opts = [PickerOption(key=f"%{i}", label=f"p{i}") for i in range(20)]
    p = _OptionPicker(opts, "t")
    p._offset = 10
    assert p._by_shortcut("1").key == "%10"
    assert p._by_shortcut("3").key == "%12"


def test_sync_offset_clamped_when_terminal_grows() -> None:
    """终端变大后 offset 要夹回来，否则上半屏看不到、下半屏全是空白。"""
    p = picker([entry(f"s{i}", "/p/one") for i in range(60)], mode=GroupMode.NONE)
    p._move(59)
    p._sync_offset(7)
    p._sync_offset(50)
    assert p._offset + 50 <= len(p._rows) or p._offset == 0
    assert p._offset <= max(0, len(p._rows) - 50)


def test_page_moves_by_visual_rows_not_selectable_rows() -> None:
    """分组模式下 PgDn 不能跨过整屏 —— 组标题也占显示行。

    实测（修复前）：20 个单会话目录组、屏幕 7 行，一次 PgDn 移动了 14 个显示行。
    """
    entries = [entry(f"s{i}", f"/p/{i}") for i in range(20)]
    p = picker(entries, mode=GroupMode.CWD)
    before = p._cursor
    p._page(7)
    assert p._cursor - before <= 9, f"翻了 {p._cursor - before} 个显示行，超出一屏"
    assert p._is_selectable(p._cursor)


def test_page_backwards_lands_on_selectable() -> None:
    entries = [entry(f"s{i}", f"/p/{i}") for i in range(20)]
    p = picker(entries, mode=GroupMode.CWD)
    p._page(14)
    p._page(-7)
    assert p._is_selectable(p._cursor)


def test_control_chars_stripped_before_drawing() -> None:
    """display_width 把控制符算 0 列，ncurses 却渲染成 ^X 占 2 列 —— 必须先剥掉。"""
    from atm.tui import _Screen

    assert _Screen._sanitize("A\x1b[31mRED\x07B") == "A[31mREDB"
    assert _Screen._sanitize("正常标题") == "正常标题"
    assert _Screen._sanitize("tab\there") == "tab\there"


def test_every_source_has_a_display_tag() -> None:
    """新加 Source 忘了配标记的话，列表里会出现一列 `??`。"""
    from atm.model import SOURCE_TAG, Source

    for source in Source:
        assert SOURCE_TAG.get(source), source
        assert len(SOURCE_TAG[source]) == 2, source
