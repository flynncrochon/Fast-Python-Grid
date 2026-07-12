"""fastpygrid: a fast grid split into a GUI-free core and thin toolkit hosts.

    core/        model + geometry + selection + paint() + the OpenGL engine (gpu.py)
    render/      tk.py (tkinter host) · qt.py (PySide6 host)

Both hosts drive the SAME OpenGL engine, so behaviour and looks match.

    from fastpygrid.render.tk import make_sheet   # tkinter
    win = make_sheet(headers, rows, frozen_columns=2)
    win.mainloop()
"""
from .core import selection, theme
from .core.coremodel import make_model
from .frame import dataframe_to_grid

__all__ = ["make_model", "dataframe_to_grid", "selection", "theme"]
