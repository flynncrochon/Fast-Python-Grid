"""Toolkit-neutral grid controller: owns the selection state machine and turns
normalized input events into core calls. Shared by the Tk and Qt renderers --
selection/anchor, click+drag selection (incl. spreadsheet frozen-pane crossing),
keyboard nav/copy/paste/undo, column drag-resize + dbl-click autofit, and
Ctrl+wheel zoom.

A renderer owns only what genuinely differs per toolkit -- drawing the display
list, building widgets (editor, filter popup, find bar), and translating native
events -- then delegates the logic here through this small ``host`` surface:

    host.model, host.geom, host.editable
    host.redraw()                     repaint the viewport
    host.after_scroll_change()        push top_row/scroll_x to the scrollbars
    host.after_geometry_change()      push scrollbar ranges (metrics changed)
    host.clipboard_get() -> str       host.clipboard_set(text)
    host.begin_edit(initial=None)     open the in-cell editor on the active cell
    host.commit_editor()              flush any open editor before an action
    host.measure(text, bold) -> int   text width in px (autofit)
    host.set_zoom_fonts(zoom)         resize the cell fonts for a zoom factor
    host.set_edge_cursor(on_edge)     show/hide the column-resize cursor
    host.open_filter_popup(col, at)   pop the column filter menu (``at`` = toolkit)
    host.reveal_find()                show the find bar
"""
from . import selection as S

ARROWS = ("Up", "Down", "Left", "Right", "Home", "End", "Prior", "Next")


class GridController:
    def __init__(self, host, base_row_h, base_gutter, base_w):
        self.host = host
        self.model = host.model
        self.geom = host.geom
        self.editable = host.editable
        self.active = (1, 0)               # start on the first DATA cell (row 0 = header)
        self.anchor = (1, 0)
        self.sel = (1, 0, 1, 0)
        self.extra = []
        self.drag_region = None
        self.resize_col = None             # column being drag-resized, else None
        self.corner_hover = False
        # Zoom: metrics are recomputed from these base values * _zoom (never
        # ratio-chained), so it stays crisp and never drifts. Manual column
        # resizes write back to _base_w so a later zoom keeps them proportional.
        self._zoom = 1.0
        self._base_row_h = base_row_h
        self._base_gutter = base_gutter
        self._base_w = list(base_w)

    # --- selection surface (also the contract FindController drives) ------
    def ranges(self):
        return list(self.extra) + [self.sel]

    def bounds(self):
        return dict(top_hrow=0, last_row=self.model.nrows() - 1,
                    last_col=self.model.ncols - 1)

    def scroll_into_view(self, r, c):
        self.geom.scroll_into_view(r, c)
        self.geom.clamp(self.model.nrows())
        self.host.after_scroll_change()

    # --- mouse ------------------------------------------------------------
    def on_press(self, x, y, ctrl, shift, at):
        self.host.commit_editor()
        g, m = self.geom, self.model
        region, row, col = g.hit(x, y, m.nrows(), m.ncols)
        if row == 0 and region == "cell" and col is not None \
                and g.filter_btn_hit(x, y, col):            # header filter button
            self.drag_region = None
            self.host.open_filter_popup(col, at)
            return
        ec = g.col_edge_hit(x, y, m.ncols)
        if ec is not None:                                  # grab a column border
            self.resize_col = ec
            self._resize_x0, self._resize_w0 = x, g.col_w[ec]
            self.drag_region = None
            return
        self.sel, self.extra, self.active, self.anchor = S.resolve_click(
            region, row, col, anchor=self.anchor, sel=self.sel, extra=self.extra,
            ctrl=ctrl, shift=shift, **self.bounds())
        self.drag_region = region
        self.scroll_into_view(*self.active)
        self.host.redraw()

    def on_motion(self, x, y):
        """Pointer moved with no button down: corner hover + resize cursor."""
        over = self.geom.in_corner(x, y)
        if over != self.corner_hover:
            self.corner_hover = over
            self.host.redraw()
        self.host.set_edge_cursor(self.geom.col_edge_hit(x, y, self.model.ncols) is not None)

    def set_corner_hover(self, over):
        if over != self.corner_hover:
            self.corner_hover = over
            self.host.redraw()

    def on_drag(self, x, y):
        g, m = self.geom, self.model
        if self.resize_col is not None:                     # live column resize
            self.resize_to(self.resize_col, self._resize_w0 + (x - self._resize_x0))
            self.host.after_geometry_change()
            self.host.redraw()
            return
        if self.drag_region is None:
            return
        nrows, ncols = m.nrows(), m.ncols
        # autoscroll: down past the bottom edge; up only when data exists above
        # (so dragging into the pinned header selects it instead of scrolling)
        if y > g.h - g.row_h:
            g.top_row += 1
        elif y < g.header_h and g.top_row > 1:
            g.top_row -= 1
        row = g.drag_row(y, nrows)
        col = g.x_to_col(x, ncols)
        if col is None:
            col = g.frozen if x < g.gutter_w else ncols - 1
        # spreadsheet frozen-pane crossing: dragging left past the freeze line targets the
        # scrollable column hidden under the frozen block (scroll-into-view reveals
        # it, one per motion) instead of snapping onto a pinned frozen column.
        col = S.edge_reveal_col(col, anchor_col=self.anchor[1], frozen_cols=g.frozen,
                                scroll_x=g.scroll_x, ncols=ncols, pointer_x=x,
                                gutter_w=g.gutter_w, frozen_w=g.frozen_w(),
                                body_w=g.w, leaf_x=g.col_x)
        self.sel, self.active = S.resolve_drag(
            self.drag_region, row, col, anchor=self.anchor, **self.bounds())
        self.scroll_into_view(*self.active)                 # push the view to the pressed-to cell
        g.clamp(nrows)
        self.host.redraw()

    def on_release(self):
        self.drag_region = None
        self.resize_col = None
        self.host.redraw()      # full repaint so chrome (corner tri) reflects the final selection

    def on_double(self, x, y):
        ec = self.geom.col_edge_hit(x, y, self.model.ncols)
        if ec is not None:                                  # dbl-click border = autofit
            self.autofit(ec)
            return
        region, row, col = self.geom.hit(x, y, self.model.nrows(), self.model.ncols)
        if region == "cell" and self.editable:
            self.active = (row, col)
            self.host.begin_edit()

    # --- column sizing ----------------------------------------------------
    def resize_to(self, c, w):
        """Set a column width (drag-resize / autofit) and record it as the new
        zoom base so a later zoom keeps it proportional instead of snapping back."""
        self.geom.set_col_w(c, w)
        self._base_w[c] = self.geom.col_w[c] / self._zoom

    def autofit(self, c):
        # fit header + currently-visible rows only. Scanning 1M rows for
        # the widest cell would stall; visible-fit matches what the user sees.
        sel = self._selected_cols()
        cols = sorted(sel) if c in sel and len(sel) > 1 else [c]   # Ctrl+A -> fit all
        rows = [0] + self.geom.visible_data_rows(self.model.nrows())
        btn = self.geom.row_h - 8 + 8                        # filter button + gap (header only)
        for cc in cols:
            w = max(self.host.measure(self.model.cell(r, cc), r == 0)
                    + (btn if r == 0 else 0) for r in rows)
            self.resize_to(cc, w + 12)                       # 5px text inset + margin
        self.host.after_geometry_change()
        self.host.redraw()

    def _selected_cols(self):
        cols = set()
        for (r1, c1, r2, c2) in self.ranges():
            cols.update(range(min(c1, c2), max(c1, c2) + 1))
        return cols

    # --- zoom (Ctrl + wheel) ---------------------------------------------
    def zoom_by(self, factor):
        z = max(0.4, min(4.0, self._zoom * factor))
        if z == self._zoom:
            return
        self._zoom = z
        self.host.set_zoom_fonts(z)
        self.geom.set_metrics(max(10, round(self._base_row_h * z)),
                              max(24, round(self._base_gutter * z)),
                              [max(20, round(w * z)) for w in self._base_w])
        self.geom.clamp(self.model.nrows())
        self.host.after_geometry_change()
        self.host.redraw()

    # --- keyboard ---------------------------------------------------------
    def on_key(self, key, shift, ctrl, text):
        """Handle a normalized key. ``key`` is a lowercase letter or one of the
        ARROWS / "Return"/"Tab"/"Delete"/"F2". Returns True if consumed."""
        m = self.model
        if ctrl:
            if key == "f":
                self.host.reveal_find(); return True
            if key == "c":
                self.copy(); return True
            if key == "x":
                self.cut(); return True
            if key == "v":
                self.paste(); return True
            if key == "z":
                self._jump(m.redo() if shift else m.undo()); return True  # Ctrl+Shift+Z = redo
            if key == "y":
                self._jump(m.redo()); return True
            if key == "a":
                lr, lc = m.data_extent()
                self.sel, self.extra = (0, 0, lr, lc), []    # header + data (active 0,0 -> paste in place)
                self.active = self.anchor = (0, 0)
                self.host.redraw(); return True
        if key in ARROWS:
            self.arrow(key, shift, ctrl); return True
        if key == "Return":
            self.move((1, 0)); return True
        if key == "Tab":
            self.move((0, 1)); return True
        if key == "Delete":
            self.delete(); return True
        if key == "F2":
            self.host.begin_edit(); return True
        if text and len(text) == 1 and text.isprintable() and not ctrl and self.editable:
            self.host.begin_edit(initial=text); return True
        return False

    def _jump(self, target):
        """Land the selection on the cell an undo/redo reports (None = nothing to do)."""
        if target is not None:
            r, c = target
            self.sel, self.extra = (r, c, r, c), []
            self.active = self.anchor = (r, c)
            self.scroll_into_view(r, c)
        self.host.redraw()

    # --- clipboard (keyboard + right-click menu share these) --------------
    def copy(self):
        self.host.commit_editor()
        self.host.clipboard_set(self.model.selection_text(self.ranges()))

    def cut(self):
        """Copy then clear -- model.delete_selection is a no-op on a read-only grid,
        so this is safe even if a caller doesn't grey the menu item out."""
        self.copy()
        self.model.delete_selection(self.ranges())

    def paste(self):
        box = self.model.paste_text(self.host.clipboard_get(), self.ranges(), self.active)
        if box:
            self.sel, self.extra = box, []
        self.host.redraw()

    def delete(self):
        self.model.delete_selection(self.ranges())   # no-op on a read-only grid

    def context_select(self, x, y):
        """Right-click: select the cell under the cursor unless it's already inside
        the selection (spreadsheet keeps a multi-cell selection when you right-click in it)."""
        region, row, col = self.geom.hit(x, y, self.model.nrows(), self.model.ncols)
        if region != "cell" or row is None or col is None or self._in_selection(row, col):
            return
        self.sel, self.extra, self.active, self.anchor = S.resolve_click(
            region, row, col, anchor=self.anchor, sel=self.sel, extra=self.extra,
            ctrl=False, shift=False, **self.bounds())
        self.host.redraw()

    def _in_selection(self, r, c):
        return any(r1 <= r <= r2 and c1 <= c <= c2
                   for (r1, c1, r2, c2) in S.normalize(self.ranges()))

    def arrow(self, name, shift, ctrl):
        self.sel, self.extra, self.active, self.anchor = S.resolve_arrow(
            name, active=self.active, anchor=self.anchor, shift=shift, ctrl=ctrl,
            page_rows=self.geom.full_rows(),
            occupied_row=self.model.occupied_row,
            occupied_col=(lambda c: self.model.occupied_col_at(self.active[0], c)),
            **self.bounds())
        self.scroll_into_view(*self.active)
        self.host.redraw()

    def move(self, d):
        r = max(0, min(self.model.nrows() - 1, self.active[0] + d[0]))
        c = max(0, min(self.model.ncols - 1, self.active[1] + d[1]))
        self.active = self.anchor = (r, c)
        self.sel, self.extra = (r, c, r, c), []
        self.scroll_into_view(r, c)
        self.host.redraw()


if __name__ == "__main__":   # headless self-check of the state machine
    from .model import GridModel
    from .geometry import Geometry

    class _Host:                          # records redraws; no toolkit
        def __init__(self):
            self.model = GridModel(["A", "B"], [["a1", "b1"], ["a2", "b2"]])
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

    h = _Host()
    ctl = GridController(h, 22, 56, [80, 80])
    assert ctl.active == (1, 0)
    ctl.move((1, 0)); assert ctl.active == (2, 0), ctl.active   # Enter -> down
    ctl.on_key("Right", False, False, ""); assert ctl.active == (2, 1), ctl.active
    ctl.on_key("a", False, True, "")                            # Ctrl+A selects header+data
    assert ctl.sel == (0, 0, h.model.data_extent()[0], 1), ctl.sel
    assert ctl.zoom_by(1.1) is None and ctl._zoom != 1.0        # zoom took
    ctl.sel, ctl.extra, ctl.active = (1, 0, 1, 0), [], (1, 0)   # cut clears the cell, fills clipboard
    ctl.cut(); assert h.clip == "a1" and h.model.cell(1, 0) == "", (h.clip, h.model.cell(1, 0))
    # edit elsewhere, move away, then Ctrl+Z jumps the selection back to the edit
    h.model.set_cell(2, 1, "X"); ctl.active = ctl.anchor = (1, 0)
    ctl.on_key("z", False, True, ""); assert ctl.active == (2, 1), ctl.active
    ctl.on_key("z", True, True, ""); assert ctl.active == (2, 1), ctl.active   # Ctrl+Shift+Z redo, same cell
    print("gridcontroller self-check ok")
