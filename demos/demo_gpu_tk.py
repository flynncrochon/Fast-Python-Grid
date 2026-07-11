"""Direct2D/GPU demo: the fastpygrid Gpu renderer (Windows only).

    python demos/demo_gpu_tk.py                 # 100k rows on the GPU surface
    python demos/demo_gpu_tk.py --rows 500000   # stress it

Tabs across the top open separate whole sheets, each with different options, so you
can compare them live. The "Uncapped" tabs scroll past the last row/column into
empty space (spreadsheet-style): the scrollbar thumb shrinks as you overscroll and snaps
back when you scroll in again, unless you typed out there, which grows the sheet.

Each sheet is a full editable grid: scroll (wheel + scrollbars), click/drag select,
keyboard nav, Ctrl+wheel zoom, F11 fullscreen (Esc exits), in-cell editing, dropdown
cells, read-only columns, right-click menu, ▼ column filter/sort, and Ctrl+F find.
Build the DLL once with:  python -m fastpygrid.core.gpu --build
"""
import sys

# _data.py lives next to this file; fastpygrid is installed into demos/.venv by setup.bat.
from _data import (HEADERS, COL_W, gen_rows, rows_arg, stream_styles, choices_demo,
                   lines_demo, readonly_demo)

# Each tab = a whole separate sheet with its own scroll-cap options.
SHEETS = [
    ("Capped (default)", {}),
    ("Uncapped rows", dict(uncap_rows=True)),
    ("Uncapped rows + cols", dict(uncap_rows=True, uncap_cols=True)),
]


def _add_sheet(nb, title, headers, rows, col_w, scale, lib, **opts):
    """Build one grid (its own model + engine) into a fresh notebook tab."""
    import tkinter as tk
    from fastpygrid.render.tk import GpuGrid
    from fastpygrid.core.coremodel import make_model
    frame = tk.Frame(nb)
    nb.add(frame, text=title)
    model = make_model(headers, rows, editable=True)
    grid = GpuGrid(frame, model, frozen=2, col_w=col_w, scale=scale, lib=lib, **opts)
    grid.pack(fill="both", expand=True)
    frame.model, frame.grid_view = model, grid   # stream_styles() reads these
    choices_demo(model)            # Sector/Rating dropdowns (O(1), whole-column)
    lines_demo(model)              # thick section dividers
    readonly_demo(model)           # locked Ticker + Price columns
    model.changed()                # first frame paints instantly with data + dropdowns
    stream_styles(frame)           # per-cell fg/bold/bg streams in after the first frame


def main():
    import tkinter as tk
    from tkinter import ttk
    from fastpygrid.core.gpu import _load_lib, _enable_dpi_awareness, _screen_scale
    lib = _load_lib()
    if lib is None:
        raise SystemExit("Gpu surface unavailable. Build it with "
                         "`python -m fastpygrid.core.gpu --build`.")
    _enable_dpi_awareness()
    n = rows_arg(sys.argv)
    data = gen_rows(n)             # generate once, each sheet's model copies it
    root = tk.Tk()
    root.title("fastpygrid (gpu): sheet options, %s rows" % f"{n:,}")
    scale = _screen_scale(root)
    root.geometry("%dx%d" % (round(980 * scale), round(620 * scale)))
    # uncapped tabs use a SMALL sheet so the empty repeatable cells are right past
    # the data (with 100k rows you'd never scroll to the phantom region to see them).
    small = data if n <= 30 else gen_rows(30)
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)
    for title, opts in SHEETS:
        _add_sheet(nb, title, HEADERS, (small if opts else data), COL_W, scale, lib, **opts)
    root.mainloop()


if __name__ == "__main__":
    main()
