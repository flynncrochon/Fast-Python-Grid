"""Big-selection stress: drag PAST the fullscreen edges so edge-autoscroll kicks in
and the selection balloons far beyond the visible viewport -- thousands of rows down,
hundreds of columns right, and both at once from the corner.

This drives the REAL path a user's mouse triggers (press -> drag outside the body ->
_autoscroll_tick scrolls the view and extends the selection into the revealed cells),
timing one real frame per autoscroll step. Unlike benchmark_interactive's fast-select
(which stays inside the viewport), here the drag corner is pinned OUTSIDE the surface,
so every frame both scrolls and grows the selection. We report p50/p95/max per phase
and the final selection extent, to prove it really went off-screen huge.

Run:  python demos/benchmark_bigselect.py       (add FASTPYGRID_VSYNC=0 for uncapped)
Needs the DLLs built (build.bat) into fastpygrid/core/.
"""
import gc
import sys
import time

from fastpygrid.core.gpu import _load_lib
from fastpygrid.render.tk import make_sheet

STEPS = 400           # autoscroll frames timed per phase
NCOLS = 250           # WIDER than the screen (250 * 34 px) so column-extend has ~160 cols off-screen
COL_W = 34
NROWS = 30_000        # deep enough that a fast downward fling (ROW_STEP/frame) never bottoms out
ROW_STEP = 40         # rows swept per frame (a fast downward fling)
COL_STEP = 3          # columns swept per frame (a fast rightward fling)


def _dataset():
    headers = ["c%03d" % i for i in range(NCOLS)]
    rows = [["r%dc%d" % (r, c) for c in range(NCOLS)] for r in range(NROWS)]
    return headers, rows


def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def _stats(times):
    ms = [t * 1e3 for t in times]
    p50 = _pct(ms, 0.50)
    return {"p50": p50, "p95": _pct(ms, 0.95), "max": max(ms),
            "fps": 1e3 / p50 if p50 else 0.0}


def _prepare(headers, rows):
    win = make_sheet(headers, rows, col_w=[COL_W] * NCOLS, title="bigselect bench")
    win.attributes("-fullscreen", True)
    eng = win.grid_view.engine
    for _ in range(200):
        win.update()
        if min(eng.host.size()) > 200:
            break
    for _ in range(8):
        eng._paint_now(); win.update()
    if eng._surf is None:
        raise RuntimeError("surface never attached")
    eng._coalesce = lambda fn: None            # we render by hand; silence coalesced repaints
    eng.host.after = lambda ms, fn: None        # don't let autoscroll self-schedule; we pump it
    eng.host.after_cancel = lambda h: None
    return win, eng


def _sel_extent(eng):
    r1, c1, r2, c2 = eng.ctl.sel
    return abs(r2 - r1) + 1, abs(c2 - c1) + 1


def _fling(eng, past_x, past_y):
    """Press top-left, pin the drag corner `past` px OUTSIDE the body on each axis, then
    pump autoscroll: each frame advances the view at a high (fixed, big) step and extends
    the selection into the revealed cells -- exactly _autoscroll_tick's work minus its
    real-time dt (we force a big step so it sweeps off-screen fast). Times each paint."""
    g = eng.geom
    g.top_row = g.hdr_rows; g.scroll_x = 0
    eng.ctl.on_release()
    x0, y0 = g.gutter_w + 8, g.header_h + 8
    eng.press(x0, y0, False, False)
    # pointer held outside the surface: past the right edge and/or below the bottom
    px = (g.w + past_x) if past_x else (x0 + 40)
    py = (g.h + past_y) if past_y else (y0 + 40)
    times = []
    for _ in range(STEPS):
        if past_y:
            g.top_row = max(g.hdr_rows, g.top_row + ROW_STEP)
        if past_x:
            g.scroll_x = min(g.max_scroll_x(), g.scroll_x + COL_STEP * COL_W)
        g.clamp(eng.model.nrows())
        eng.ctl.on_drag(px, py, follow=False)             # extend selection into revealed cells
        t0 = time.perf_counter(); eng._paint_now(); times.append(time.perf_counter() - t0)
    eng.release()
    return times, _sel_extent(eng)


def bench(headers, rows):
    win, eng = _prepare(headers, rows)
    w, h = eng.host.size()
    gc.collect(); gc.disable()
    try:
        phases = {
            "fling-down  (rows)":   _fling(eng, 0, 200),      # past bottom -> sweep rows
            "fling-right (cols)":   _fling(eng, 200, 0),      # past right  -> sweep cols
            "fling-corner(both)":   _fling(eng, 200, 200),    # past corner -> both axes
        }
    finally:
        gc.enable()
    win.destroy(); gc.collect()
    return w, h, {k: (_stats(t), ext) for k, (t, ext) in phases.items()}


def main():
    if _load_lib() is None:
        print("OpenGL backend (glsurface) not built -- run build.bat"); return 1
    headers, rows = _dataset()
    w, h, phases = bench(headers, rows)
    print("big-selection benchmark  (%dx%d fullscreen, %d rows x %d cols dataset)\n"
          % (w, h, NROWS, NCOLS))
    print("  %-20s  %8s %8s %8s   %8s   %s" %
          ("phase", "p50 ms", "p95 ms", "max ms", "fps@p50", "final sel (rows x cols)"))
    for phase, (s, ext) in phases.items():
        print("  %-20s  %8.3f %8.3f %8.3f   %8.0f   %d x %d" %
              (phase, s["p50"], s["p95"], s["max"], s["fps"], ext[0], ext[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
