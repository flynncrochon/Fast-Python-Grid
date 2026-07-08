"""Tk demo — the fastgrid Tk renderer (stdlib only, no Pillow).

    python scripts/demo_tk.py                 # tabs: Editable + Read-only, 100k rows
    python scripts/demo_tk.py --rows 500000   # stress it
    python scripts/demo_tk.py --view-only     # both tabs read-only
    python scripts/demo_tk.py --smoke         # no window: assert core model + paint()
"""
import sys

from _data import HEADERS, COL_W, gen_rows, rows_arg


def smoke():
    from fastgrid.core.model import GridModel
    from fastgrid.core.geometry import Geometry
    from fastgrid.core.paint import paint
    m = GridModel(HEADERS, gen_rows(1000))
    assert m.nrows() > 1000
    assert m.cell(0, 0) == "Ticker"        # grid row 0 = header (field names)
    assert m.cell(1, 0) == "TIK00000"      # grid row 1 = first data row
    m.set_filter(2, {"Energy"})            # data rows are grid 1..; header stays row 0
    assert all(m.cell(gr, 2) == "Energy" for gr in range(1, m._real_rows()))
    m.clear_filters()
    m.set_sort(0, False); assert m.cell(1, 0) == "TIK00999"; m.clear_filters()
    cells, capped = m.find_matches("Energy")
    assert cells and not capped
    m.set_cell(1, 1, "EDITED"); assert m.cell(1, 1) == "EDITED"    # edit a data cell
    m.undo(); assert m.cell(1, 1) == "Company 0 Inc."
    assert m.cell(0, 1) == "Company"       # header row is selectable/addressable
    g = Geometry(COL_W, frozen=2); g.w, g.h = 1000, 560
    m.set_find("Energy", False, None, (7, 2))
    dl = paint(m, g, active=(2, 1), ranges=[(0, 1, 6, 3)])   # selection spans the header row
    assert dl.cells and dl.overlays and any(fl & 1 for *_r, fl in dl.cells)
    print("smoke OK — model + core paint (%d cells, %d overlays)"
          % (len(dl.cells), len(dl.overlays)))


def _tab(nb, rows, editable, scale):
    """One notebook tab: a toolbar (clear filters + status) over a TkGrid."""
    import tkinter as tk
    from fastgrid.core import theme as T
    from fastgrid.core.model import GridModel
    from fastgrid.renderer.tk import TkGrid, _UI_BG
    frame = tk.Frame(nb)
    model = GridModel(HEADERS, gen_rows(rows), editable=editable)
    bar = tk.Frame(frame, bg=_UI_BG); bar.pack(fill="x")
    grid = TkGrid(frame, model, editable=editable, frozen=2, col_w=COL_W, scale=scale)
    grid.pack(fill="both", expand=True)
    tk.Button(bar, text="Clear filters", relief="flat", bg=_UI_BG, fg=T.TXT,
              command=model.clear_filters).pack(side="left", padx=6, pady=3)
    status = tk.Label(bar, bg=_UI_BG, fg=T.TXT, anchor="e")
    status.pack(side="right", padx=8)
    prev = model.changed
    model.changed = lambda: (prev(), status.configure(
        text="%d rows%s   ·   Ctrl+F find · ▼ filter" % (
            model._real_rows() - 1, "  ·  filtered" if model.any_filters() else "")))
    model.changed()
    return frame


def main():
    import tkinter as tk
    from tkinter import ttk
    from fastgrid.renderer.tk import _enable_dpi_awareness, _screen_scale
    rows = rows_arg(sys.argv)
    editable = "--view-only" not in sys.argv
    _enable_dpi_awareness()
    win = tk.Tk()
    win.title("fastgrid (tk) — %s rows" % f"{rows:,}")
    scale = _screen_scale(win)
    try:
        win.tk.call("tk", "scaling", scale * 96 / 72)
    except tk.TclError:
        pass
    win.geometry("%dx%d" % (round(980 * scale), round(620 * scale)))
    nb = ttk.Notebook(win)
    nb.pack(fill="both", expand=True)
    nb.add(_tab(nb, rows, editable, scale), text="Editable" if editable else "Read-only")
    nb.add(_tab(nb, rows, False, scale), text="Read-only")
    win.mainloop()


if __name__ == "__main__":
    smoke() if "--smoke" in sys.argv else main()
