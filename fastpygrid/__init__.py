"""fastpygrid: a fast grid split into a GUI-free core and thin toolkit hosts.

    core/    model + geometry + selection + paint() + OpenGL engine (gpu.py)
    render/  tk.py (tkinter) · qt.py (PySide6); both drive the SAME engine

    from fastpygrid.render.tk import make_sheet
    win = make_sheet(headers, rows, frozen_columns=2)
    win.mainloop()
"""
from .core import selection, theme
from .core.coremodel import make_model
from .frame import dataframe_to_grid

__all__ = ["make_model", "dataframe_to_grid", "selection", "theme"]
