"""fastpygrid — a fast grid split into a GUI-free core and thin toolkit hosts.

    core/        model + geometry + selection + paint() + the Direct2D engine (gpu.py)
    render/      tk.py (tkinter host) · qt.py (PySide6 host)

Both hosts drive the SAME Direct2D engine, so behaviour and looks match.

    from fastpygrid.render.tk import make_sheet   # tkinter
    win = make_sheet(headers, rows, frozen_columns=2); win.mainloop()
"""
from .core import GridModel, selection, theme

__all__ = ["GridModel", "selection", "theme"]
