# Fast Python Grid

A fast, GPU-painted spreadsheet grid for tens of thousands of rows. Scroll,
select, filter and find stay instant because only the visible cells are ever
built. The grid logic lives in a GUI-free core; a single toolkit-neutral
Direct2D engine draws it, hosted by a thin Tk or Qt adapter.

```
src/                   fastpygrid .py sources (no DLLs live here; becomes dist/fastpygrid)
  core/      model, geometry, selection, paint() -> display list, gpu.py (Direct2D engine)
    _gpu/        surface.cpp
    _gridstore/  gridcore.cpp
  render/    gpu_tk.py (tkinter host) · gpu_qt.py (PySide6 host)
CMakeLists.txt  builds the DLLs + installs the .py into dist/fastpygrid/
build.bat    runs CMake (configure/build/install) -> dist/fastpygrid/ (the runnable library)
demos/       demo_gpu_tk.py · demo_gpu_qt.py · _data.py · setup.bat (stages demos/fastpygrid + demos/.venv)
scripts/
  tests/       check_select.py · fuzz_coremodel.py       (import dist/fastpygrid)
  benchmarks/  bench_geometry.py                          (import dist/fastpygrid)
```

`core.paint()` returns a display list (plain-data draw ops for the visible
cells); the engine just blits it. The host only owns the window and translates
events, so the Tk and Qt hosts behave identically:

```
dl.cells    = [(x, y, w, h, text, bg, fg, flags), ...]      # ~visible cells, back-to-front
dl.overlays = [("line"|"vline"|"hline"|"ring"|"tri", ...)]  # chrome drawn after cells
```

`core.paint` decides every colour, position and z-order; the engine is ~"for
each cell fill a rect + draw text; for each overlay draw a line/rect/triangle".

![fastpygrid sample grid: headers, frozen columns, per-column filters](docs/screenshot.png)

## Requirements

| Requirement | Details |
|---|---|
| **Windows only** | The renderer is Direct2D (`_gpu/surface.dll`), so it needs Windows and a Direct3D 11-capable GPU (falls back to the WARP software device if there's no GPU). There is no macOS/Linux backend. |
| **Python 3.8+** | |
| **Tk host** | Standard library only (`tkinter`, ships with Python). |
| **Qt host** | Needs `PySide6` (`pip install PySide6`), the only dependency, and only if you use the Qt host. |
| **Native DLLs** | Not committed -- `build.bat` runs CMake (which finds MSVC itself) to compile them into `dist/fastpygrid/`. `surface.dll` draws the Direct2D surface and `gridcore.dll` is an optional C++ data core (the model falls back to pure Python if it's missing). Needs CMake + MSVC on the machine. Run `build.bat` once (and after any `.cpp` or `.py` change), then run the demos. |

## Example

```python
from fastpygrid.render.gpu_tk import make_sheet         # tkinter host (stdlib only)
win = make_sheet(
    ["Ticker", "Company", "Sector", "Price"],
    [["AAPL", "Apple Inc.", "Technology", "189.20"],
     ["XOM",  "Exxon Mobil", "Energy",   "104.10"]],
    frozen_columns=2,   # pin the first 2 columns against horizontal scroll
)
win.mainloop()
```

Double-click or type to edit. Enter/Tab commit, Ctrl+Z/Y undo/redo, Ctrl+C/V
copy/paste, Ctrl+A select-all, Ctrl+F find, ▼ on a header to filter/sort.

```python
from fastpygrid.render.gpu_qt import make_sheet          # PySide6 host
win = make_sheet(headers, rows, frozen_columns=2)
win.mainloop()                                          # aliases app.exec()
```

## Build

```bash
build.bat
```

Runs CMake to compile the DLLs and assemble the runnable library into
`dist/fastpygrid/` (`.py` + `.dll` only). Needs CMake and MSVC. Re-run after any
`.cpp` or `.py` change, then `demos/setup.bat` to stage `demos/fastpygrid` + `demos/.venv`.

## Run

```bash
python demos/demo_gpu_tk.py                 # tkinter host, 100k rows
python demos/demo_gpu_qt.py                 # Qt host, same data
python demos/demo_gpu_tk.py --rows 500000   # stress it
python scripts/tests/check_select.py        # selection-state-machine check
python scripts/tests/fuzz_coremodel.py      # C++ data core vs pure-Python oracle
```

## Performance

Only the visible cells are built, so the row count barely matters. 10k rows
and 1M rows do the same work per frame.

```
rows         core paint()
10,000       0.13 ms
100,000      0.13 ms
1,000,000    0.13 ms
```

The Direct2D surface redraws that frame on the GPU, vsync-capped (~60 fps), and
stays smooth while scrolling millions of rows.

## Features

The engine handles all of this, so both the Tk and Qt hosts get it for free.

| Feature | Details |
|---|---|
| Frozen columns | Pin leading columns against horizontal scroll. |
| Pinned selectable header rows | Pass a list of lists as `headers` for a multi-row header; adjacent same-label cells in the upper rows merge into spanning group bands, e.g. `[["", "FY 2023", "FY 2023"], ["Ticker", "Q1", "Q2"]]`. |
| spreadsheet-style selection | Click/drag/Ctrl/Shift, whole row/column, select-all, Ctrl+arrow block jumps. |
| In-cell editing | Double-click or type to edit; copy/paste/delete, undo/redo. |
| Filter & sort | Per-column value and text filters, A→Z/Z→A sort. |
| Find | Ctrl+F with prev/next, case, and selection scope. |

Per-cell styling and dropdowns, and thick black section dividers, are set on
the model (display only, positional):

```python
model.set_cell_style(gr, col, fg="#c0392b", bold=True)   # gr/col are GRID coords
model.set_cell_choices(gr, col, ["Buy", "Hold", "Sell"]) # editing offers a dropdown
model.set_vline(col)          # thick rule on the RIGHT edge of a column
model.set_hline(gr)           # thick rule on the BOTTOM edge of a grid row
model.set_readonly_col(col)   # block edits/paste/delete in a column (still selectable)
```
