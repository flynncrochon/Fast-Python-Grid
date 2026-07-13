"""Headless self-check of the GridController state machine."""
from fastpygrid.core.gridcontroller import GridController
from fastpygrid.core.coremodel import make_model     # the real (C++-backed) model
from fastpygrid.core.geometry import Geometry


class _Host:                          # records redraws, no toolkit
    def __init__(self):
        self.model = make_model(["A", "B"], [["a1", "b1"], ["a2", "b2"]])
        self.geom = Geometry([80, 80]); self.geom.w, self.geom.h = 400, 300
        self.editable = True
        self.clip = ""
    def redraw(self): pass
    def commit_editor(self): pass
    def after_scroll_change(self): pass
    def after_geometry_change(self): pass
    def set_zoom_fonts(self, z): pass
    def clipboard_set(self, t): self.clip = t
    def clipboard_get(self): return self.clip


def test_state_machine():
    h = _Host()
    ctl = GridController(h, 22, 56, [80, 80])
    assert ctl.active == (1, 0)
    ctl.move((1, 0)); assert ctl.active == (2, 0), ctl.active   # Enter -> down
    ctl.on_key("Right", False, False, ""); assert ctl.active == (2, 1), ctl.active
    ctl.on_key("a", False, True, "")                            # Ctrl+A selects header+data
    assert ctl.sel == (0, 0, h.model.data_extent()[0], 1), ctl.sel
    assert ctl.zoom_to(ctl._zoom * 1.1) is None and ctl._zoom != 1.0   # zoom took
    ctl.sel, ctl.extra, ctl.active = (1, 0, 1, 0), [], (1, 0)   # cut clears the cell, fills clipboard
    ctl.cut(); assert h.clip == "a1" and h.model.cell(1, 0) == "", (h.clip, h.model.cell(1, 0))
    # edit elsewhere, move away, then Ctrl+Z jumps the selection back to the edit
    h.model.set_cell(2, 1, "X"); ctl.active = ctl.anchor = (1, 0)
    ctl.on_key("z", False, True, ""); assert ctl.active == (2, 1), ctl.active
    ctl.on_key("z", True, True, ""); assert ctl.active == (2, 1), ctl.active   # Ctrl+Shift+Z redo, same cell
