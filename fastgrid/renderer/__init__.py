"""Thin per-toolkit renderers over the shared core display list.

Import the one you need directly so an unused toolkit is never imported:
    from fastgrid.renderer.tk import make_sheet    # tkinter (stdlib + nothing)
    from fastgrid.renderer.qt import make_sheet    # PySide6
"""
