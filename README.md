# Fast Python Grid

A GPU-painted spreadsheet grid for tens of thousands of rows. Only visible cells
are built, so scroll, select, filter and find stay instant. A GUI-free core
holds the logic; one Direct2D engine draws it, under a thin Tk or Qt host.

## Layout

```
Fast-Python-Grid/
|-- src/                     # fastpygrid .py sources (no DLLs live here)
|   |-- core/               # model, geometry, selection, paint() -> display list, gpu.py (Direct2D engine)
|   |   |-- _gpu/           # surface.cpp
|   |   `-- _gridstore/     # gridcore.cpp
|   `-- render/             # tk.py (tkinter host), qt.py (PySide6 host)
|-- CMakeLists.txt          # compiles the DLLs (scikit-build-core)
|-- build.bat               # python -m build -> dist/*.whl + *.tar.gz (same as CI/PyPI)
|-- demos/                  # demo_gpu_tk.py, demo_gpu_qt.py, _data.py, setup.bat (wheel into demos/.venv)
`-- scripts/
    |-- tests/              # check_select.py, fuzz_coremodel.py (need fastpygrid installed)
    `-- benchmarks/         # bench_geometry.py (need fastpygrid installed)
```

![fastpygrid sample grid: headers, frozen columns, per-column filters](docs/screenshot.png)

## Requirements

| Requirement | Details |
|---|---|
| **Windows only** | The renderer is Direct2D (`_gpu/surface.dll`): needs Windows and a Direct3D 11 GPU (falls back to the WARP software device if there's none). No macOS/Linux backend. |
| **Python 3.8+** | |
| **Tk host** | Standard library only (`tkinter`). |
| **Qt host** | Needs `PySide6` (`pip install PySide6`), the only dependency, and only for the Qt host. |
| **Native DLLs** | Not committed; built into the wheel by `build.bat` (CMake via scikit-build-core, which finds MSVC). `surface.dll` draws the Direct2D surface; `gridcore.dll` is an optional C++ data core (falls back to pure Python if missing). Building needs CMake + MSVC; end users just `pip install fastpygrid`. |

## Example

### Tk

```python
from fastpygrid.render.tk import make_sheet         # tkinter host (stdlib only)
win = make_sheet(
    ["Ticker", "Company", "Sector", "Price"],
    [["AAPL", "Apple Inc.", "Technology", "189.20"],
     ["XOM",  "Exxon Mobil", "Energy",   "104.10"]],
    frozen_columns=2,   # pin the first 2 columns against horizontal scroll
)
win.mainloop()
```

### Qt

```python
from fastpygrid.render.qt import make_sheet          # PySide6 host
win = make_sheet(headers, rows, frozen_columns=2)
win.mainloop()                                          # aliases app.exec()
```

## Build

```bash
build.bat
```

Runs `python -m build`, which drives CMake to compile the DLLs and produce
`dist/fastpygrid-*.whl` + `.tar.gz` (same artifacts as CI/PyPI). Needs CMake,
MSVC, and Python's `build`. Re-run after any `.cpp`/`.py` change.

## Run demo

`demos/setup.bat` creates `demos/.venv` and installs the freshly built wheel.
Run it after `build.bat`, then:

```bash
demos\demo.bat tk                                        # tkinter host, 100k rows
demos\demo.bat qt                                        # Qt host, same data
demos\demo.bat tk --rows 500000                          # stress it
demos\.venv\Scripts\python scripts/tests/check_select.py # selection-state-machine check
demos\.venv\Scripts\python scripts/tests/fuzz_coremodel.py  # C++ data core vs oracle
```

Any env with `pip install .` runs the scripts; the demo venv is just handy.

## API

The library is two things: `make_sheet()`, which opens a window, and the
`GridModel` it returns, which you call to style cells, add dropdowns or draw
dividers. Both hosts (`fastpygrid.render.tk` and `fastpygrid.render.qt`) expose
the same `make_sheet`.

Coordinates: `col` is a 0-based column index. `gr` is a **grid row**, where rows
`0 .. header_rows-1` are the header and everything from `header_rows` on is data.
With one header row, the first data row is `gr=1`. You never touch pixels.

### `make_sheet(headers, rows, ...)`

Builds the model, opens the window, returns it. Raises `RuntimeError` if the
Direct2D surface can't be created (DLL missing or no D3D device).

| Argument | Type | Default | What it does |
|---|---|---|---|
| `headers` | `list[str]` or `list[list[str]]` | required | Column titles. A list of lists gives stacked headers; adjacent equal labels in the upper rows merge into group bands. |
| `rows` | `list[list]` | required | The data, row by row. Values are stringified. |
| `frozen_columns` | `int` | `0` | Pin this many leading columns against horizontal scroll. |
| `view_only` | `bool` | `False` | Read-only sheet: no edit, paste or delete. |
| `master` | widget | `None` | Parent window. Given one, opens as a child instead of a new top-level app. |
| `col_w` | `list[int]` | `None` | Per-column pixel widths. `None` auto-sizes. |
| `title` | `str` | host default | Window title. |
| `uncap_rows` | `bool` | `False` | Lift the built-in row-count cap. |
| `uncap_cols` | `bool` | `False` | Lift the built-in column-count cap. |

Returns the host window: a `tk.Tk` (or `Toplevel`) for Tk, a `QWidget` for Qt.
Both carry `.mainloop()` (Qt aliases `app.exec()`), `.model` (the `GridModel`),
and `.grid_view` (the grid widget).

```python
from fastpygrid.render.tk import make_sheet
win = make_sheet(
    ["Ticker", "Company", "Sector", "Price"],
    [["AAPL", "Apple Inc.", "Technology", "189.20"],
     ["XOM",  "Exxon Mobil", "Energy",    "104.10"]],
    frozen_columns=2,
)
win.mainloop()
```

### Reading and writing cells

Reach the model through `win.model`.

| Call | Returns | What it does |
|---|---|---|
| `cell(gr, col)` | `str` | Text at that grid row/column (`""` if out of range). |
| `nrows()` | `int` | Total grid rows: header rows plus data. |
| `ncols` | `int` | Column count (a property, no parens). |
| `set_cell(gr, col, text)` | `bool` | Write a cell. Goes through undo. Returns `False` if unchanged or read-only. |
| `set_data(headers, rows)` | `None` | Swap in a new sheet, resetting filters, sort and undo. |

```python
m = win.model
m.cell(1, 0)                 # 'AAPL'
m.set_cell(1, 3, "191.55")   # bump Apple's price; True
m.ncols                      # 4
```

### Styling and dropdowns

Styles and choices are keyed to the data, so they follow a row through sort and
filter.

| Call | What it does |
|---|---|
| `set_cell_style(gr, col, fg=None, bg=None, bold=None)` | Style one cell. `fg`/`bg` are `#rrggbb`; `None` leaves an attribute as-is. |
| `set_cell_choices(gr, col, choices)` | Turn one cell into a dropdown offering `choices`. `None` clears it. |
| `set_col_choices(col, choices)` | Same for a whole column, one O(1) call. A per-cell choice overrides it. |

```python
m.set_cell_style(1, 3, fg="#c0392b", bold=True)      # Apple's price in bold red
m.set_col_choices(2, ["Technology", "Energy", "Healthcare"])   # Sector is a dropdown
```

### Dividers and locked cells

Dividers are positional (keyed by index), so they stay put when data moves.
Read-only rows are keyed to the data and follow it.

| Call | What it does |
|---|---|
| `set_vline(col, on=True)` | Thick black rule on the right edge of a column. |
| `set_hline(gr, on=True)` | Thick black rule on the bottom edge of a grid row. |
| `set_readonly_col(col, on=True)` | Block edit/paste/delete in a column (still selectable and copyable). |
| `set_readonly_row(gr, on=True)` | Same, for a row. |

```python
m.set_vline(1)              # rule after the 'Company' column
m.set_hline(0)              # rule under the header
m.set_readonly_col(0)       # Ticker can't be edited
```
