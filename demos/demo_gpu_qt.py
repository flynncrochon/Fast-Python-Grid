"""Direct2D/GPU demo under a Qt (PySide6) host -- same engine as demo_gpu_tk.py.

    python demos/demo_gpu_qt.py                 # 100k rows on the GPU surface
    python demos/demo_gpu_qt.py --rows 500000   # stress it

Tabs across the top open separate whole sheets, each with different options, so you
can compare them live. The "Uncapped" tabs scroll past the last row/column into
empty space (spreadsheet-style): the scrollbar thumb shrinks as you overscroll and snaps
back when you scroll in again -- unless you typed out there, which grows the sheet.

Proves the toolkit-neutral GpuEngine runs unchanged under Qt. Build the DLL once:
    python -m fastgrid.core.gpu --build
"""
import sys

# _data.py and fastgrid/ both live next to this file (fastgrid staged by setup.bat).
from _data import (HEADERS, COL_W, gen_rows, rows_arg, stream_styles, choices_demo,
                   lines_demo, readonly_demo)

# Each tab = a whole separate sheet with its own scroll-cap options.
SHEETS = [
    ("Capped (default)", {}),
    ("Uncapped rows", dict(uncap_rows=True)),
    ("Uncapped rows + cols", dict(uncap_rows=True, uncap_cols=True)),
]


def _add_sheet(tabs, title, headers, rows, col_w, scale, lib, **opts):
    """Build one grid (its own model + engine) into a fresh Qt tab page."""
    from PySide6 import QtWidgets
    from fastgrid.render.gpu_qt import GpuQtGrid
    from fastgrid.core.coremodel import make_model
    page = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(page)
    lay.setContentsMargins(0, 0, 0, 0)
    model = make_model(headers, rows, editable=True)
    grid = GpuQtGrid(page, model, frozen=2, col_w=col_w, scale=scale, lib=lib, **opts)
    lay.addWidget(grid)
    page.model, page.grid_view = model, grid   # stream_styles() reads these
    tabs.addTab(page, title)
    choices_demo(model)            # Sector/Rating dropdowns (O(1), whole-column)
    lines_demo(model)              # thick section dividers
    readonly_demo(model)           # locked Ticker + Price columns
    model.changed()                # first frame paints instantly with data + dropdowns
    stream_styles(page)            # per-cell fg/bold/bg streams in after the first frame


def main():
    from PySide6 import QtWidgets
    from fastgrid.core.gpu import _load_lib, _enable_dpi_awareness, _screen_scale
    lib = _load_lib()
    if lib is None:
        raise SystemExit("Gpu surface unavailable -- build it with "
                         "`python -m fastgrid.core.gpu --build`.")
    app = QtWidgets.QApplication.instance()
    if app is None:
        _enable_dpi_awareness()
        app = QtWidgets.QApplication([])
    n = rows_arg(sys.argv)
    data = gen_rows(n)             # generate once; each sheet's model copies it
    scale = _screen_scale(None)
    # uncapped tabs use a SMALL sheet so the empty repeatable cells are right past
    # the data (with 100k rows you'd never scroll to the phantom region to see them).
    small = data if n <= 30 else gen_rows(30)
    win = QtWidgets.QTabWidget()
    win.setWindowTitle("fastgrid (gpu-qt) — sheet options — %s rows" % f"{n:,}")
    win.resize(round(980 * scale), round(620 * scale))
    for title, opts in SHEETS:
        _add_sheet(win, title, HEADERS, (small if opts else data), COL_W, scale, lib, **opts)
    win.show()
    app.exec()


if __name__ == "__main__":
    main()
