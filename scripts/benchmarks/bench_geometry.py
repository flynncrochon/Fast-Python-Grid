"""Hot-path bench for wide sheets: the per-frame column ops (visible_cols,
x_to_col, col_edge_hit) and a full paint() display-list build, at growing column
counts. Compares the bisect implementations against a linear reference so the
speedup is explicit. Run: python scripts/benchmarks/bench_geometry.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "dist"))
from fastgrid.core.geometry import Geometry
from fastgrid.core.model import GridModel
from fastgrid.core.paint import paint


def _timeit(fn, iters):
    t0 = time.perf_counter()
    for _ in range(iters):
        fn()
    return (time.perf_counter() - t0) / iters * 1e6   # microseconds/call


def _linear_visible_cols(g, ncols):
    """The pre-bisect implementation, for a before/after ratio."""
    fx = g.freeze_x()
    out = []
    for c in range(ncols):
        x = g.col_x(c)
        if c < g.frozen or (x + g.col_w[c] > fx and x < g.w):
            out.append(c)
    return out


def _linear_x_to_col(g, x, ncols):
    if x < g.gutter_w:
        return None
    cx = (x - g.gutter_w) if x < g.freeze_x() else (x - g.gutter_w + g.scroll_x)
    for c in range(ncols):
        if g._cum[c] <= cx < g._cum[c + 1]:
            return c
    return None


def bench(ncols, rows=2000, iters=2000):
    col_w = [90] * ncols
    g = Geometry(col_w, frozen=1)
    g.w, g.h = 1400, 900
    g.scroll_x = g._cum[ncols // 2]              # scrolled to the middle (worst case for linear)
    xs = [g.gutter_w + (i * 137 % (g.w - g.gutter_w)) for i in range(64)]

    bis = _timeit(lambda: g.visible_cols(ncols), iters)
    lin = _timeit(lambda: _linear_visible_cols(g, ncols), max(1, iters // (1 + ncols // 200)))
    xbis = _timeit(lambda: [g.x_to_col(x, ncols) for x in xs], iters) / len(xs)
    xlin = _timeit(lambda: [_linear_x_to_col(g, x, ncols) for x in xs],
                   max(1, iters // (1 + ncols // 200))) / len(xs)
    edge = _timeit(lambda: [g.col_edge_hit(x, 10, ncols) for x in xs], iters) / len(xs)

    # full paint() build (display list for the visible viewport)
    hdr = ["C%d" % c for c in range(ncols)]
    m = GridModel(hdr, [[""] * ncols for _ in range(rows)])
    pnt = _timeit(lambda: paint(m, g, (1, 1), [(1, 1, 1, 1)]), 200)

    assert sorted(set(g.visible_cols(ncols)) & set(range(ncols))) == \
        [c for c in _linear_visible_cols(g, ncols) if c < ncols], "visible_cols mismatch"
    print(f"ncols={ncols:>6}  visible_cols {bis:7.1f}us (linear {lin:8.1f}us, "
          f"{lin/bis:5.0f}x)  x_to_col {xbis:5.2f}us ({xlin/xbis:4.0f}x)  "
          f"edge_hit {edge:5.2f}us  paint {pnt:6.1f}us")


if __name__ == "__main__":
    for n in (100, 1000, 5000, 16384):
        bench(n)
