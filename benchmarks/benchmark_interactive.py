"""Interactive performance test: a FULLSCREEN grid with thousands of visible cells,
driven through the engine's real scroll and selection input, timing every frame.

This exercises the paths a user actually feels: vertical scroll, horizontal scroll,
and a selection drag that grows to cover the whole screen. Each phase mutates engine
state exactly as a mouse wheel / press+drag would, then renders one real frame and
records its time. Smoothness is a worst-case property, so we report p50 / p95 / max
per phase (a single 40 ms hitch matters more than a great average).

Run:  python benchmarks/benchmark_interactive.py
Needs the DLLs built (build.bat) into fastpygrid/core/.
"""
import gc
import sys
import time

from fastpygrid.core.gpu import _load_lib
from fastpygrid.render.tk import make_sheet

FRAMES = 400          # frames timed per scroll phase
DRAG_STEPS = 300      # selection-drag frames
NCOLS = 100           # more columns than fit, so horizontal scroll has somewhere to go
COL_W = 28            # narrow (pre-DPI) so thousands of cells fit on a hi-DPI screen
NROWS = 50_000        # deep enough that vertical scroll never hits the bottom


def _dataset():
    headers = ["c%02d" % i for i in range(NCOLS)]
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


def _visible_cells(eng):
    g = eng.geom
    return g.vis_rows() * len(g.visible_cols(eng.model.ncols))


def _prepare(headers, rows):
    win = make_sheet(headers, rows, col_w=[COL_W] * NCOLS, title="interactive bench")
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
    # We render every frame by hand, so silence the engine's coalesced after_idle
    # repaints; otherwise a stale _flush fires after teardown ("invalid command").
    eng._coalesce = lambda fn: None
    return win, eng


def _reset(eng):
    eng.geom.top_row = eng.geom.hdr_rows
    eng.geom.scroll_x = 0
    eng.ctl.on_release()               # drop any in-flight drag/selection


def _phase_vscroll(eng):
    # Drive scroll_y directly, not via wheel(): wheel() now defers the move to an eased
    # animation timer (smooth inertial scrolling), which this hand-rolled loop doesn't
    # pump. Moving geom straight measures the real per-frame render at each position.
    _reset(eng)
    g = eng.geom
    step = int(round(2 * g.row_h))     # ~one wheel notch of downward pan per frame
    times = []
    for _ in range(FRAMES):
        g.scroll_y += step; g.clamp(eng.model.nrows())
        t0 = time.perf_counter(); eng._paint_now(); times.append(time.perf_counter() - t0)
    return times


def _phase_hscroll(eng):
    _reset(eng)
    g = eng.geom
    times = []
    for _ in range(FRAMES):
        g.scroll_x += 40; g.clamp(eng.model.nrows())   # pan right 40 px/frame
        t0 = time.perf_counter(); eng._paint_now(); times.append(time.perf_counter() - t0)
    return times


def _phase_select(eng):
    """Press top-left, then drag the far corner outward so the selection grows to
    fill the viewport, the worst-case selection-highlight render."""
    _reset(eng)
    g, (w, h) = eng.geom, eng.host.size()
    x0, y0 = g.gutter_w + 8, g.header_h + 8
    eng.press(x0, y0, False, False)
    xmax, ymax = w - eng._sbw - 40, h - eng._sbw - 40    # stay inside the edge-autoscroll band
    times = []
    for i in range(DRAG_STEPS):
        f = (i + 1) / DRAG_STEPS
        eng.drag(int(x0 + (xmax - x0) * f), int(y0 + (ymax - y0) * f))
        t0 = time.perf_counter(); eng._paint_now(); times.append(time.perf_counter() - t0)
    eng.release()
    return times


def _phase_fast_select(eng):
    """LARGE, QUICK, FAST-MOVING selection: anchor top-left, then fling the drag
    corner across the whole viewport in big jumps every frame (coprime triangle
    waves so it doesn't settle into a cycle). Each frame the selection rectangle
    is large AND changes a lot, the stress case the request asked for."""
    _reset(eng)
    g, (w, h) = eng.geom, eng.host.size()
    x0, y0 = g.gutter_w + 8, g.header_h + 8
    eng.press(x0, y0, False, False)
    xspan = w - eng._sbw - 40 - x0
    yspan = h - eng._sbw - 40 - y0
    times = []
    for i in range(DRAG_STEPS):
        fx = abs(((i * 37) % 200) - 100) / 100.0          # fast triangle wave 0..1
        fy = abs(((i * 53) % 200) - 100) / 100.0          # different period -> 2D swing
        eng.drag(int(x0 + xspan * fx), int(y0 + yspan * fy))
        t0 = time.perf_counter(); eng._paint_now(); times.append(time.perf_counter() - t0)
    eng.release()
    return times


def bench(headers, rows):
    win, eng = _prepare(headers, rows)
    cells = _visible_cells(eng)
    w, h = eng.host.size()
    gc.collect(); gc.disable()         # keep GC pauses out of the frame-time tails
    try:
        phases = {"v-scroll": _phase_vscroll(eng),
                  "h-scroll": _phase_hscroll(eng),
                  "select-drag": _phase_select(eng),
                  "fast-select": _phase_fast_select(eng)}
    finally:
        gc.enable()
    win.destroy(); gc.collect()
    return {"w": w, "h": h, "cells": cells,
            "phases": {k: _stats(v) for k, v in phases.items()}}


def main():
    if _load_lib() is None:
        print("OpenGL backend (glsurface) not built, run build.bat"); return 1
    headers, rows = _dataset()
    r = bench(headers, rows)
    print("interactive benchmark  (%dx%d fullscreen, %d cells on screen, %d rows x %d cols)\n"
          % (r["w"], r["h"], r["cells"], NROWS, NCOLS))
    print("  %-13s  %8s %8s %8s   %s" %
          ("phase", "p50 ms", "p95 ms", "max ms", "fps@p50"))
    for phase, s in r["phases"].items():
        print("  %-13s  %8.3f %8.3f %8.3f   %6.0f" %
              (phase, s["p50"], s["p95"], s["max"], s["fps"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
