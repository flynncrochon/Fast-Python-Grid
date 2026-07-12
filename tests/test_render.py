"""grid-gap invariant: a backing rect covers the cell bbox, and each cell fill is
inset 1px on the right/bottom so the backing shows through as a grid line."""
from fastpygrid.core.render import blit, T


class _Rec:
    def __init__(self): self.rects = []
    def rect(self, x, y, w, h, fill=None, outline=None, width=1):
        self.rects.append((x, y, w, h, fill))
    def text(self, *a, **k): pass
    def barrier(self): pass


class _DL:
    cells = [(0, 0, 10, 5, "a", "#111", "#fff", 0), (10, 0, 20, 5, "b", "#222", "#fff", 0)]
    overlays = []
    frozen = []
    chrome = []
    drops = []
    frozen_drops = []


def test_grid_gap_inset():
    rec = _Rec(); blit(_DL(), rec)
    assert rec.rects[0] == (0, 0, 30, 5, T.GRID), rec.rects[0]      # backing = bbox
    assert rec.rects[1] == (0, 0, 9, 4, "#111"), rec.rects[1]       # cell inset by 1px
    assert rec.rects[2] == (10, 0, 19, 4, "#222"), rec.rects[2]
