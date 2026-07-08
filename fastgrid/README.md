# fastgrid

One fast grid, split so the **logic** is written once and each GUI toolkit is a
thin renderer over it. `core/` imports no toolkit — no Qt, no Tk, no Pillow — and
its `paint()` returns a **display list** (pure data). Renderers just blit it.

```
fastgrid/
  core/                  # zero GUI imports
    model.py             #   rows/cols/values + filter · sort · find · edit · undo
    geometry.py          #   scroll math, col_x, row_at_y, col_at_x, visible ranges, hit-test
    selection.py         #   spreadsheet click/drag/arrow state machine (shared verbatim)
    find.py              #   Ctrl+F search/navigation controller (both find bars drive it)
    theme.py             #   the one palette both renderers use
    paint.py             #   paint(model, geom, active, ranges) -> display list
  renderer/              # thin — one file per toolkit
    tk.py                #   tkinter + native Canvas items (stdlib only, NO Pillow)
    qt.py                #   PySide6 + QPainter
scripts/                 # runnable entry points (outside the library)
  demo_tk.py             #   Tk live demo (+ --smoke self-check)
  demo_qt.py             #   Qt live demo
  bench.py               #   paint-cost benchmark (flat across row count)
  check_select.py        #   selection state-machine correctness
  _data.py               #   shared sample data
```

The display list:

```
dl.cells    = [(x, y, w, h, text, bg, fg, flags), ...]   # ~visible cells, back-to-front
dl.overlays = [("line"|"rect"|"funnel", ...), ...]       # chrome drawn after cells
```

`core.paint` decides every colour, position and z-order; a renderer is ~"for each
cell fill a rect + draw text; for each overlay draw a line/rect/triangle". Both
renderers produce a pixel-identical layout because they consume the same list.

## Run

```bash
python scripts/demo_tk.py          # tkinter — stdlib, no Pillow
python scripts/demo_qt.py          # PySide6, same data
python scripts/demo_tk.py --smoke  # headless: assert core model + paint()
python scripts/demo_tk.py --rows 500000
python scripts/bench.py --qt       # paint-cost benchmark across row counts
python scripts/check_select.py     # selection correctness
```

## Use

```python
from fastgrid.renderer.tk import make_sheet          # tkinter
win = make_sheet(headers, rows, frozen=2); win.mainloop()

from fastgrid.renderer.qt import make_sheet           # PySide6
app, win = make_sheet(headers, rows, frozen=2); win.show(); app.exec()
```

## Performance

Both renderers are **viewport-virtualized**: only the ~visible cells are ever
drawn (Tk: ~390 Canvas items; Qt: ~200 `QPainter` calls) regardless of row
count. A 100k-row viewport repaints in ~16ms (Tk) — item count is bounded by the
window, not the data. No per-cell widgets, no Pillow.

## Renderer parity

Both renderers are at full parity: selection, in-cell edit, copy/paste/undo,
filter popup, and Ctrl+F find. The shared logic lives in `core` — the selection
state machine (`selection.py`), filter state (`model.py`), and find navigation
(`find.py`, driven by both find bars) — so Tk and Qt behave identically; each
renderer is just the toolkit-specific widgets over it.
