"""Live zoom-fps probe: opens the REAL GL surface, auto-runs a few Ctrl+wheel zoom
glides, and prints the true rendered fps (past vsync + GPU upload) for each. This is
the number bench_zoom.py can't get -- it needs a window. Closes itself after ~6 s.

    python benchmarks/bench_zoom_live.py
"""
import os
import sys

os.environ["FASTPYGRID_ZOOMFPS"] = "1"          # engine prints "zoom glide: N frames ... = F fps"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk
from fastpygrid.render.tk import GpuGrid
from fastpygrid.core.coremodel import make_model
from fastpygrid.core.gpu import _load_lib, _enable_dpi_awareness, _screen_scale

HEADERS = ["Ticker", "Name", "Sector", "Price", "Chg%", "Volume", "Rating"]
COL_W = [90, 200, 130, 90, 80, 110, 90]


def main():
    lib = _load_lib()
    if lib is None:
        raise SystemExit("OpenGL surface unavailable -- build it with "
                         "`python -m fastpygrid.core.gpu --build`.")
    _enable_dpi_awareness()
    rows = [[f"R{r}C{c}" for c in range(len(HEADERS))] for r in range(100_000)]
    root = tk.Tk()
    root.title("zoom fps probe -- auto-zooming, closes itself")
    scale = _screen_scale(root)
    root.geometry("%dx%d" % (round(1000 * scale), round(700 * scale)))
    model = make_model(HEADERS, rows, editable=True)
    grid = GpuGrid(root, model, frozen=2, col_w=COL_W, scale=scale, lib=lib)
    grid.pack(fill="both", expand=True)
    model.changed()
    E = grid.engine

    burst = lambda f, n: [E.zoom(f) for _ in range(n)]   # n notches -> one glide to the compounded target
    root.after(1000, lambda: burst(1.1, 6))              # zoom IN  (settles ~1.5 s)
    root.after(2200, lambda: burst(1 / 1.1, 6))          # zoom OUT
    root.after(3400, lambda: burst(1.1, 8))              # zoom IN further
    root.after(4600, lambda: burst(1 / 1.1, 10))         # zoom OUT
    root.after(6000, root.destroy)
    root.mainloop()
    print("(probe window closed)")


if __name__ == "__main__":
    main()
