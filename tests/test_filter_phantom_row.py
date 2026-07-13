"""A filtered view shrinks the data; phantom overscroll rows past the view must
not crash _src_data (regression: IndexError on redraw after filtering)."""
from fastpygrid.core.model import GridModel


def test_phantom_row_past_filtered_view():
    m = GridModel(["A"], [["x"], ["y"], ["x"], ["z"]])
    m.set_filter(0, ["x"])                     # view = 2 rows
    assert len(m._view) == 2
    di = 5                                      # phantom row well past the view
    assert m._src_data(di) == -1               # no source, sentinel
    assert m.cell(m.header_rows + di, 0) == ""  # renders blank, no crash
