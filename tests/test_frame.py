"""Duck-typed check of dataframe_to_grid: no pandas needed to exercise the logic."""
from fastpygrid.frame import dataframe_to_grid


class _Cols(list):
    def __init__(self, items, nlevels=1):
        super().__init__(items); self.nlevels = nlevels


class _DF:
    def __init__(self, cols, rows):
        self.columns, self._rows = cols, rows
    def itertuples(self, index=False, name=None):
        return iter(self._rows)


def test_single_header_and_blanks():
    nan = float("nan")
    single = _DF(_Cols(["A", "B"]), [(1, nan), (None, "y")])
    headers, rows = dataframe_to_grid(single)
    assert headers == [["A", "B"]]
    assert rows == [["1", ""], ["", "y"]]          # NaN and None -> blank


def test_multiindex_header():
    multi = _DF(_Cols([("g1", "a"), ("g1", "b"), ("g2", "c")], nlevels=2),
                [(1, 2, 3)])
    headers, rows = dataframe_to_grid(multi)
    assert headers == [["g1", "g1", "g2"], ["a", "b", "c"]]   # one header row per level
    assert rows == [["1", "2", "3"]]
