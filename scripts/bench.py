"""Paint-cost micro-benchmarks for fastgrid.

Shows the core claim: cost is bounded by the VIEWPORT, not the row count --
paint() and a Tk redraw stay flat whether the grid holds 10k or 1M rows,
because only the ~visible cells are ever built.

    python scripts/bench.py                # core paint() + Tk redraw
    python scripts/bench.py --qt           # also time a Qt viewport repaint
    python scripts/bench.py --rows 10000,100000,1000000

Headless note: the Tk bench needs a display; the Qt bench runs offscreen.
"""
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _data import HEADERS, COL_W, gen_rows                      # noqa: E402
from fastgrid.core.geometry import Geometry                     # noqa: E402
from fastgrid.core.model import GridModel                       # noqa: E402
from fastgrid.core.paint import paint                           # noqa: E402

W, H = 1000, 600


def _median_ms(fn, frames=40):
    ts = []
    for i in range(frames):
        t = time.perf_counter()
        fn(i)
        ts.append((time.perf_counter() - t) * 1000)
    return statistics.median(ts)


def bench_core(model, geom):
    """Pure display-list build — no toolkit, no window."""
    nrows = model.nrows()

    def one(i):
        geom.top_row = int(i / 40 * max(1, nrows - geom.full_rows()))
        paint(model, geom, active=(geom.top_row, 0),
              ranges=[(geom.top_row, 0, geom.top_row + 5, 3)])
    return _median_ms(one)


def bench_tk(model):
    import tkinter as tk
    from fastgrid.renderer.tk import TkGrid
    root = tk.Tk()
    root.geometry("%dx%d" % (W, H))
    g = TkGrid(root, model, frozen=2, col_w=COL_W)
    g.pack(fill="both", expand=True)
    root.update_idletasks(); root.update()
    nrows = model.nrows()

    def one(i):
        g.geom.top_row = int(i / 40 * max(1, nrows - g.geom.full_rows()))
        g.redraw(); root.update()
    ms = _median_ms(one)
    items = len(g.canvas.find_all())
    root.destroy()
    return ms, items


def _pick_cols(model):
    """A representative high- and low-cardinality column for the filter bench."""
    ncols, hi, lo = model.ncols, None, None
    for c in range(ncols):
        card = len({r[c] for r in model._rows[:2000]})    # cheap cardinality probe
        if card >= 1000 and hi is None:
            hi = c
        if card < 50 and lo is None:
            lo = c
    return hi if hi is not None else 0, lo if lo is not None else ncols - 1


def bench_filter(model):
    """Filter-select cost as the popup ACTUALLY pays it: distinct_capped() on
    open + every search keystroke -- NOT the uncapped distinct_values(), which
    the popup never calls. A high-cardinality column early-exits at the cap (a
    value checklist is useless there, so we never build the giant set); a
    low-cardinality column scans fully but dedups into a tiny set. warm = cached
    re-ask (per keystroke); apply = set_filter + full view rebuild."""
    hi, lo = _pick_cols(model)

    def cold(col):
        model._distinct.clear()
        t = time.perf_counter(); model.distinct_capped(col)
        return (time.perf_counter() - t) * 1000
    cold_hi, cold_lo = cold(hi), cold(lo)
    warm = _median_ms(lambda i: model.distinct_capped(lo), 200)
    vals, _ = model.distinct_capped(lo)
    target = {vals[0]} if vals else set()

    def apply(i):
        model.set_filter(lo, target if i % 2 else None)      # apply / clear, both rebuild
    ap = _median_ms(apply, 20)
    model.set_filter(lo, None)
    return cold_hi, cold_lo, warm, ap


def bench_qt(model):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from fastgrid.renderer.qt import QtGrid
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    g = QtGrid(model, frozen=2, col_w=COL_W)
    g.resize(W, H); g.show(); app.processEvents()
    nrows = model.nrows()

    def one(i):
        g.geom.top_row = int(i / 40 * max(1, nrows - g.geom.full_rows()))
        g.viewport().repaint(); app.processEvents()
    return _median_ms(one)


def main():
    counts = [10_000, 100_000, 1_000_000]
    if "--rows" in sys.argv:
        counts = [int(x) for x in sys.argv[sys.argv.index("--rows") + 1].split(",")]
    do_qt = "--qt" in sys.argv

    print("%-12s %-14s %-22s%s" % ("rows", "core paint()", "tk redraw (items)",
                                   "   qt repaint" if do_qt else ""))
    print("-" * (60 if do_qt else 48))
    for n in counts:
        model = GridModel(HEADERS, gen_rows(n))
        geom = Geometry(COL_W, frozen=2); geom.w, geom.h = W, H
        core = bench_core(model, geom)
        line = "%-12s %-14s" % (f"{n:,}", "%.2f ms" % core)
        try:
            tk_ms, items = bench_tk(model)
            line += " %-22s" % ("%.1f ms (%d)" % (tk_ms, items))
        except Exception as e:                      # headless / no display
            line += " %-22s" % ("n/a (%s)" % type(e).__name__)
        if do_qt:
            try:
                line += "   %.1f ms" % bench_qt(model)
            except Exception as e:
                line += "   n/a (%s)" % type(e).__name__
        print(line)
    print("\nFlat across row count = viewport-virtualized (only visible cells drawn).")

    print("\n%-12s %-18s %-18s %-16s %s"
          % ("rows", "open hi-card", "open lo-card", "keystroke (warm)", "filter apply"))
    print("-" * 78)
    for n in counts:
        model = GridModel(HEADERS, gen_rows(n))
        cold_hi, cold_lo, warm, ap = bench_filter(model)
        print("%-12s %-18s %-18s %-16s %.2f ms"
              % (f"{n:,}", "%.2f ms" % cold_hi, "%.2f ms" % cold_lo,
                 "%.3f ms" % warm, ap))
    print("\nopen = distinct_capped() the popup runs on open. hi-card early-exits at the")
    print("cap; lo-card scans fully into a tiny set. warm = cached re-ask per keystroke.")


if __name__ == "__main__":
    main()
