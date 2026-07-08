# Fast Python Grid

A fast, direct-painted spreadsheet grid for high-volume data (tens of thousands
of rows — instant scroll / select / filter / find). The logic is written **once**
in a GUI-free core; each toolkit is a thin renderer over it.

```
fastgrid/
  core/        model · geometry · selection · paint() -> display list   (no GUI)
  renderer/    tk.py (tkinter, stdlib — no Pillow) · qt.py (PySide6)
scripts/       demo_tk.py · demo_qt.py · bench.py · check_select.py
```

`core.paint()` returns a **display list** — pure-data draw ops for the ~visible
cells — and the renderers just blit it. Same list under both toolkits, so they
look and behave identically. See [fastgrid/README.md](fastgrid/README.md) for the
architecture.

## Quick start

```python
from fastgrid.renderer.tk import make_sheet          # tkinter (stdlib only)
win = make_sheet(
    ["Ticker", "Company", "Sector", "Price"],
    [["AAPL", "Apple Inc.", "Technology", "189.20"],
     ["XOM",  "Exxon Mobil", "Energy",   "104.10"]],
    frozen=2,           # pin the first 2 columns against horizontal scroll
)
win.mainloop()
```

Double-click or type to edit; Enter/Tab commit, Ctrl+Z/Y undo/redo, Ctrl+C/V
copy/paste, Ctrl+A select-all, Ctrl+F find, ▼ on a header to filter/sort.

```python
from fastgrid.renderer.qt import make_sheet           # PySide6
app, win = make_sheet(headers, rows, frozen=2); win.show(); app.exec()
```

## Run

```bash
python scripts/demo_tk.py         # tkinter demo, 100k rows
python scripts/demo_qt.py         # Qt renderer, same data
python scripts/demo_tk.py --smoke # headless self-check (model + paint)
python scripts/bench.py --qt      # paint-cost benchmark
python scripts/check_select.py    # selection correctness
```

## Performance

Viewport-virtualized: only the ~visible cells are ever built, so cost is flat in
the row count.

```
rows         core paint()   tk redraw (items)     qt repaint
10,000       0.12 ms        15.1 ms (424)         3.4 ms
100,000      0.13 ms        15.7 ms (424)         4.1 ms
1,000,000    0.13 ms        15.6 ms (423)         3.5 ms
```

## Dependencies

- **Tk renderer**: standard library only (tkinter). No Pillow, no Qt.
- **Qt renderer**: PySide6.

## Features

Frozen columns, a pinned selectable header row, spreadsheet-style selection
(click/drag/Ctrl/Shift, whole row/column, select-all, Ctrl+arrow block jumps),
in-cell editing, copy/paste/delete, undo/redo, per-column value + text filters,
A→Z/Z→A sort, and Ctrl+F find with prev/next, case, and selection scope. **Both
renderers wire all of it** — the selection state machine and the filter/find
navigation live in `core`, so Tk and Qt behave identically.
