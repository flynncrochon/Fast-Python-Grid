"""fastgrid — a fast grid split into a GUI-free core and thin renderers.

    core/        model + geometry + selection + paint() -> display list  (no GUI)
    renderer/    tk.py (stdlib, no Pillow) · qt.py (PySide6)

Both renderers draw the SAME core display list, so behaviour and looks match.

    from fastgrid.renderer.tk import make_sheet     # tkinter
    win = make_sheet(headers, rows, frozen=2); win.mainloop()
"""
from .core import GridModel, Geometry, paint, selection, theme

__all__ = ["GridModel", "Geometry", "paint", "selection", "theme"]
