"""Headless check of GridModel view/style state (editing lives in CoreModel now)."""
from fastpygrid.core.model import GridModel
from fastpygrid.core.coremodel import make_model


def test_on_edit_fires_only_on_data():
    """on_edit is the data-mutation sink (autosave hook), distinct from changed()
    which also fires on style/filter/find. Grid row 1 = data row 0 (one header row)."""
    m = make_model(["A", "B"], [["x", "p"], ["y", "q"], ["z", "r"]])
    edits = []
    m.on_edit = lambda: edits.append(1)

    # data mutations -> fire
    m.set_cell(1, 0, "X"); assert len(edits) == 1
    m.paste_text("a\tb", [(1, 0, 1, 1)], (1, 0)); assert len(edits) == 2
    m.delete_selection([(1, 0, 1, 0)]); assert len(edits) == 3
    m.undo(); assert len(edits) == 4          # undo of a cell edit IS a data change
    m.redo(); assert len(edits) == 5

    # no-op / non-data -> do NOT fire
    n = len(edits)
    m.set_cell(1, 0, m.cell(1, 0))            # same value, engine reports unchanged
    m.set_cell_style(1, 0, bold=True)         # style
    m.set_filter(0, {"x", "y"})               # view
    m.find_matches("x")                       # find
    assert len(edits) == n


def test_export_roundtrip_and_subscribe():
    """export() is source-ordered + round-trips; subscribe() is multi-listener."""
    m = make_model(["A", "B"], [["x", "p"], ["y", "q"], ["z", "r"]])
    m.set_sort(0, ascending=False)                 # view is now z,y,x...
    headers, rows = m.export()                     # ...export ignores the sort
    assert headers == ["A", "B"]
    assert rows == [["x", "p"], ["y", "q"], ["z", "r"]]
    m.set_data(*m.export()); assert m.export()[1] == rows   # round-trips

    hits = [0, 0]
    off0 = m.subscribe(lambda: hits.__setitem__(0, hits[0] + 1))
    m.subscribe(lambda: hits.__setitem__(1, hits[1] + 1))
    m.changed(); assert hits == [1, 1]             # both listeners fire
    off0(); m.changed(); assert hits == [1, 2]     # unsubscribed one stops


def test_core_cell_styles():
    """CoreModel styles live in the C++ core (gc_set_style/gc_get_style/...)."""
    m = make_model(["A", "B"], [["x", "p"], ["y", "q"], ["z", "r"]])
    # partial-update mask: set fg, then bold; each leaves the other attrs intact.
    m.set_cell_style(1, 0, fg="#ff0000")
    m.set_cell_style(1, 0, bold=True)
    assert m.cell_style(1, 0) == {"fg": "#ff0000", "bold": True}
    m.set_cell_style(1, 0, bg="#00ff00")
    assert m.cell_style(1, 0) == {"fg": "#ff0000", "bg": "#00ff00", "bold": True}
    assert m.cell_style(2, 0) is None
    # style is keyed by SOURCE row -> follows the cell through a sort. desc col0 (z,y,x)
    # orders source [2,1,0], so styled source row0 ([x,p]) lands at grid row 3.
    m.set_sort(0, ascending=False)
    assert m.cell_style(3, 0) == {"fg": "#ff0000", "bg": "#00ff00", "bold": True}
    assert m.cell_style(1, 0) is None                       # grid row1 is now source row2
    m.clear_sort()
    # bulk set + distinct_colors (both native)
    m.set_cell_styles([(1, 1, None, "#0000ff", None), (2, 1, None, "#0000ff", None)])
    assert m.distinct_colors(1, "bg") == ["#0000ff"]
    # header cells key negatively and store too
    m.set_cell_style(0, 0, bg="#abcdef")
    assert m.cell_style(0, 0) == {"bg": "#abcdef"}


def test_core_color_filter_sort():
    """Color filter/sort compose with value ops, natively (gc_style_filter/sort)."""
    m = make_model(["A"], [["a"], ["b"], ["c"], ["d"]])
    m.set_cell_style(1, 0, bg="#ff0000")                    # source row0 = a
    m.set_cell_style(3, 0, bg="#ff0000")                    # source row2 = c
    m.set_color_filter(0, "bg", "#ff0000")
    assert [m.cell(gr, 0) for gr in range(1, m.nrows())] == ["a", "c"]
    m.set_color_filter(0, None, None)
    m.set_color_sort(0, "bg", "#ff0000", ascending=True)    # matches first, order kept
    assert [m.cell(gr, 0) for gr in range(1, m.nrows())] == ["a", "c", "b", "d"]


def test_filter_sort_undo():
    m = make_model(["A", "B"], [["x", "p"], ["y", "q"], ["x", "r"], ["z", "s"]])
    # filter/sort commit as undoable "view" entries (no cell diff)
    m.set_filter(0, {"x"}); assert m.has_filter(0)
    assert m.undo() is None and not m.has_filter(0)      # reverted, no cell jump
    assert m.redo() is None and m.has_filter(0)
    m.clear_filters()
    m.set_sort(0, ascending=True); assert m.has_sort(0)
    m.undo(); assert not m.has_sort(0)


def test_numeric_sort():
    # numeric sort: "10" > "9" numerically, but a->z would order it first
    n = make_model(["N"], [["10"], ["9"], ["100"], [""], ["2"]])
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
    m = make_model(["A", "B"], [["x", "p"], ["y", "q"], ["x", "r"], ["z", "s"]])
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
