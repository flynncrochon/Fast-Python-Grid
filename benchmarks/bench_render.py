"""GL render-path benchmark: the half bench_zoom.py can't measure.

bench_zoom times the CPU display-list build (paint_to) and stops before the GPU
upload. This times the OTHER side: gpu_render (wire upload + draw_ops + SwapBuffers)
on a real GL context, plus the full per-frame cost (build + upload). It scrolls a
row each frame so the wire buffer actually changes (no static-frame caching wins).

    python benchmarks/bench_render.py                    # vsync off (default), 100k rows
    set FASTPYGRID_VSYNC=1 && python benchmarks/bench_render.py    # vsync on, to see the refresh cap

vsync off = the raw GL ceiling. vsync on = SwapBuffers blocks on the vblank, so
upload ms pins to the monitor's refresh period (that's the cap, not a cost).
Needs a display (opens a real 1600x900 window briefly).
"""
import argparse
import gc
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk
from fastpygrid.core.coremodel import make_model
from fastpygrid.core.gpu import GpuCanvas, _load_lib, _col
from fastpygrid.core import theme as T
from fastpygrid.render.tk import GpuGrid

HEADERS = ["Ticker", "Name", "Sector", "Price", "Chg%", "Volume", "Rating"]
COL_W = [90, 200, 130, 90, 80, 110, 90]


def pct(vals, p):
    return sorted(vals)[min(len(vals) - 1, int(len(vals) * p))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=100_000)
    ap.add_argument("--frames", type=int, default=600)
    ap.add_argument("--size", default="1600x900")
    args = ap.parse_args()

    lib = _load_lib()
    if lib is None:
        sys.exit("glsurface not built: python -m fastpygrid.core.gpu --build")

    w, h = (int(v) for v in args.size.split("x"))
    rows = [[f"R{r}C{c}" for c in range(len(HEADERS))] for r in range(args.rows)]
    model = make_model(HEADERS, rows, editable=True)

    root = tk.Tk()
    root.title("bench_render")
    root.geometry(f"{w}x{h}")
    grid = GpuGrid(root, model, frozen=2, col_w=COL_W, lib=lib)
    grid.pack(fill="both", expand=True)
    root.update()                                   # map window, size surface, attach GL
    eng = grid.engine
    eng._paint_now()                                # force attach + first frame
    root.update()
    if eng._surf is None:
        sys.exit("GL attach failed (no surface)")

    vsync = os.environ.get("FASTPYGRID_VSYNC", "0")
    render = eng._lib.gpu_render
    surf, clear = eng._surf, _col(T.LETTER_BG)
    maxsy = eng.geom.max_scroll_y(model.nrows())

    build_ms, upload_ms, total_ms = [], [], []
    gc.disable()
    for i in range(args.frames):
        eng.geom.scroll_y = (i * eng.geom.row_h) % max(1, maxsy)   # a fresh viewport each frame
        cv = GpuCanvas(eng._fpx, eng._scale)
        t0 = time.perf_counter()
        eng.paint_to(cv)
        t1 = time.perf_counter()
        buf = bytes(cv.buf)
        render(surf, buf, len(buf), clear, 0)
        t2 = time.perf_counter()
        build_ms.append((t1 - t0) * 1000)
        upload_ms.append((t2 - t1) * 1000)
        total_ms.append((t2 - t0) * 1000)
    gc.enable()

    # drop warmup
    build_ms, upload_ms, total_ms = build_ms[5:], upload_ms[5:], total_ms[5:]
    print(f"rows={args.rows:,}  viewport={w}x{h}  frames={len(total_ms)}  vsync={vsync}")
    for name, v in (("build (CPU)", build_ms), ("upload (GL)", upload_ms), ("TOTAL", total_ms)):
        print(f"  {name:12s} median {pct(v,.5):6.3f} ms  p99 {pct(v,.99):6.3f} ms  "
              f"worst {max(v):6.3f} ms")
    med = pct(total_ms, .5)
    print(f"\nfull-frame fps: median {1000/med:6.0f}  sustained(worst) {1000/max(total_ms):6.0f}")
    if vsync != "0":
        print("(vsync on: upload ms is SwapBuffers blocking on the vblank = your refresh rate, not cost)")
    root.destroy()


if __name__ == "__main__":
    main()
