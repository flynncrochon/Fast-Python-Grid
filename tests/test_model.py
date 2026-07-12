"""Headless check of GridModel view/style state (editing lives in CoreModel now)."""
from fastpygrid.core.model import GridModel


def test_filter_sort_undo():
    m = GridModel(["A", "B"], [["x", "p"], ["y", "q"], ["x", "r"], ["z", "s"]])
    # filter/sort commit as undoable "view" entries (no cell diff)
    m.set_filter(0, {"x"}); assert m.has_filter(0)
    assert m.undo() is None and not m.has_filter(0)      # reverted, no cell jump
    assert m.redo() is None and m.has_filter(0)
    m.clear_filters()
    m.set_sort(0, ascending=True); assert m.has_sort(0)
    m.undo(); assert not m.has_sort(0)


def test_numeric_sort():
    # numeric sort: "10" > "9" numerically, but a->z would order it first
    n = GridModel(["N"], [["10"], ["9"], ["100"], [""], ["2"]])
    n.set_column_numeric(0); n.set_sort(0, ascending=True)
    assert [n._rows[r][0] for r in n._view] == ["2", "9", "10", "100", ""]
    n.set_column_numeric(0, False)
    assert [n._rows[r][0] for r in n._view] == ["10", "100", "2", "9", ""]


def test_cell_choices_interned():
    m = GridModel(["A", "B"], [["x", "p"], ["y", "q"], ["x", "r"], ["z", "s"]])
    # per-cell dropdown choices (identical option lists are interned)
    assert m.cell_choices(1, 0) is None
    m.set_cell_choices(1, 0, ["x", "y", "z"])
    assert m.cell_choices(1, 0) == ("x", "y", "z") and m.dropdown_cols() == {0}
    m.set_cell_choices(1, 1, ["x", "y", "z"])
    assert m.cell_choices(1, 1) is m.cell_choices(1, 0)  # shared object
    m.set_cell_choices(1, 0, None)
    assert m.cell_choices(1, 0) is None and m.dropdown_cols() == {1}


def test_lines_and_readonly():
    m = GridModel(["A", "B"], [["x", "p"], ["y", "q"], ["x", "r"], ["z", "s"]])
    # grid lines + readonly flags
    assert m.vlines() == {} and m.hlines() == {}
    m.set_vline(0); m.set_hline(1, width=4)
    assert m.vlines() == {0: None} and m.hlines() == {0: 4}   # hline keyed by SOURCE row; None = theme default
    assert m.hline_width(1) == 4 and m.hline_width(2) is m._NO_HLINE
    # divider follows its row through a sort: source row 0 ([x,p]) moves to the
    # bottom under a descending sort of col 0 (z, y, x, x) -> grid row 3.
    m.set_sort(0, ascending=False)
    assert m.hline_width(3) == 4 and m.hline_width(1) is m._NO_HLINE
    m.clear_sort()
    m.set_vline(0, on=False); assert m.vlines() == {}
    m.set_readonly_col(0); assert m.col_readonly(0)
    m.set_readonly_col(0, on=False); assert not m.col_readonly(0)
