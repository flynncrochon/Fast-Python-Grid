"""Toolkit-neutral grid controller: owns the selection state machine and turns
normalized input events into core calls. Shared by the Tk and Qt renderers:
selection/anchor, click+drag selection (incl. frozen-pane crossing),
keyboard nav/copy/paste/undo, column drag-resize + dbl-click autofit, and
Ctrl+wheel zoom.

A renderer owns only what differs per toolkit (drawing the display
list, building widgets (editor, filter popup, find bar), and translating native
events), then delegates the logic here through this small ``host`` surface:

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
    host.open_filter_popup(col)       pop the column filter menu
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
        first = (host.geom.hdr_rows, 0)    # first DATA cell (header rows pinned above)
        self.active = first
        self.anchor = first
        self.sel = first + first
        self.extra = []
        self.drag_region = None
        self.resize_col = None             # column being drag-resized, else None
        self.corner_hover = False
        self._pending_dropdown = False     # ▼ button pressed, open the list on release
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
        g, m = self.geom, self.model
        last_row, last_col = m.nrows() - 1, m.ncols - 1
        # Uncapped: let selection/arrow-nav reach the phantom cells that are on
        # screen. The bound grows with the view, so it stays "infinite": each
        # scroll reveals more reachable cells (spreadsheet-style).
        if g.uncap_rows:
            last_row = max(last_row, g.top_row + g.vis_rows())
        if g.uncap_cols:
            vis = g.visible_cols(m.ncols)
            if vis:
                last_col = max(last_col, vis[-1] + 1)
        return dict(top_hrow=0, last_row=last_row, last_col=last_col)

    def scroll_into_view(self, r, c):
        self.geom.scroll_into_view(r, c)
        self.geom.clamp(self.model.nrows())
        self.host.after_scroll_change()

    # --- mouse ------------------------------------------------------------
    def on_press(self, x, y, ctrl, shift):
        self.host.commit_editor()
        g, m = self.geom, self.model
        region, row, col = g.hit(x, y, m.nrows(), m.ncols)
        if row == g.hdr_rows - 1 and region == "cell" and col is not None \
                and g.filter_btn_hit(x, y, col):            # header filter button
            self.drag_region = None
            self.host.open_filter_popup(col)
            return
        if region == "cell" and row >= g.hdr_rows and col is not None and self.editable \
                and m.cell_choices(row, col) is not None \
                and g.dropdown_btn_hit(x, y, row, col):      # ▼ button: select now, open on release
            self.drag_region = None
            self.sel, self.extra, self.active, self.anchor = S.resolve_click(
                region, row, col, anchor=self.anchor, sel=self.sel, extra=self.extra,
                ctrl=False, shift=False, **self.bounds())
            self._pending_dropdown = True    # open in on_release so this click's release
            self.scroll_into_view(*self.active)   # doesn't land on the fresh popup and dismiss it
            self.host.redraw()
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
        self.set_corner_hover(self.geom.in_corner(x, y))
        self.host.set_edge_cursor(self.geom.col_edge_hit(x, y, self.model.ncols) is not None)

    def set_corner_hover(self, over):
        if over != self.corner_hover:
            self.corner_hover = over
            self.host.redraw()

    def on_drag(self, x, y, follow=True):
        """Resolve a drag-extend from a pointer pos. ``follow`` scrolls the view to
        keep the pressed-to cell visible. The engine passes follow=False while its
        edge-autoscroll timer owns the scrolling, so the two don't compound (that
        double-scroll raced the pointer and flew thousands of rows past the edge)."""
        g, m = self.geom, self.model
        if self.resize_col is not None:                     # live column resize
            self.resize_to(self.resize_col, self._resize_w0 + (x - self._resize_x0))
            self.host.after_geometry_change()
            self.host.redraw()
            return
        if self.drag_region is None:
            return
        nrows, ncols = m.nrows(), m.ncols
        row = g.drag_row(y, nrows)
        col = g.x_to_col(x, ncols)
        if col is None:
            col = g.frozen if x < g.gutter_w else ncols - 1
        # frozen-pane crossing: dragging left past the freeze line targets the
        # scrollable column hidden under the frozen block (scroll-into-view reveals
        # it, one per motion) instead of snapping onto a pinned frozen column.
        col = S.edge_reveal_col(col, anchor_col=self.anchor[1], frozen_cols=g.frozen,
                                scroll_x=g.scroll_x, ncols=ncols, pointer_x=x,
                                gutter_w=g.gutter_w, frozen_w=g.frozen_w(),
                                body_w=g.w, leaf_x=g.col_x)
        self.sel, self.active = S.resolve_drag(
            self.drag_region, row, col, anchor=self.anchor, **self.bounds())
        if follow:
            self.scroll_into_view(*self.active)             # push the view to the pressed-to cell
        g.clamp(nrows)
        self.host.redraw()

    def on_release(self):
        self.drag_region = None
        self.resize_col = None
        if self._pending_dropdown:          # ▼ button click, open the list now (after the release)
            self._pending_dropdown = False
            self.host.begin_edit()
        self.host.redraw()      # full repaint so chrome (corner tri) reflects the final selection

    def on_double(self, x, y):
        ec = self.geom.col_edge_hit(x, y, self.model.ncols)
        if ec is not None:                                  # dbl-click border = autofit
            self.autofit(ec)
            return
        region, row, col = self.geom.hit(x, y, self.model.nrows(), self.model.ncols)
        if region == "cell" and self.editable:
            self.active = (row, col)
            if self.model.cell_choices(row, col) is not None:
                self._pending_dropdown = True    # open on the dbl-click's trailing release,
            else:                                # else the release lands on and dismisses the popup
                self.host.begin_edit()

    def ensure_col(self, c):
        """Grow the sheet so column `c` exists (editing a phantom column past the
        last one, uncapped). No-op for a real column. Widens the model AND geometry
        (at the phantom width the empty columns were shown), so the new columns store
        text and render/hit/zoom exactly like the originals."""
        if c < self.model.ncols:
            return
        new_w = c + 1
        pw = self.geom._phantom_w()
        self.model.grow_cols(new_w)
        self.geom.set_cols(self.geom.col_w + [pw] * (new_w - len(self.geom.col_w)))
        self._base_w += [pw / self._zoom] * (new_w - len(self._base_w))
        self.host.after_geometry_change()

    # --- column sizing ----------------------------------------------------
    def resize_to(self, c, w):
        """Set a column width (drag-resize / autofit) and record it as the new
        zoom base so a later zoom keeps it proportional instead of snapping back."""
        self.geom.set_col_w(c, w)
        self._base_w[c] = self.geom.col_w[c] / self._zoom

    def autofit(self, c, rows=None):
        # fit headers + currently-visible rows only by default. Scanning 1M rows for the
        # widest cell would stall. Visible-fit matches what the user sees. Callers that KNOW
        # the grid is small (or must fit content below the fold on first load) can pass an
        # explicit `rows` list (grid-row indices) to fit against those instead.
        H = self.geom.hdr_rows
        sel = self._selected_cols()
        cols = sorted(sel) if c in sel and len(sel) > 1 else [c]   # Ctrl+A -> fit all
        if rows is None:
            rows = list(range(H)) + self.geom.visible_data_rows(self.model.nrows())
        btn = self.geom.row_h - 8 + 8                # filter button + gap (bottom header row)
        arrow = self.geom.row_h                      # dropdown ▼ zone (data cells)
        drop = self.model.cell_choices
        for cc in cols:
            w = max(self.host.measure(self.model.cell(r, cc), r < H)
                    + (btn if r == H - 1 else arrow if r >= H and drop(r, cc) is not None else 0)
                    for r in rows)
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
        self.zoom_to(self._zoom * factor)

    def zoom_to(self, z):
        """Apply an absolute zoom factor (clamped). The engine eases toward a target
        by calling this each animation frame; a notch is factor 1.1 via zoom_by."""
        z = max(0.4, min(4.0, z))
        if z == self._zoom:
            return
        self._zoom = z
        self.host.set_zoom_fonts(z)
        self.geom.set_metrics(max(10, round(self._base_row_h * z)),
                              max(24, round(self._base_gutter * z)),
                              [max(20, round(w * z)) for w in self._base_w])
        self.geom.clamp(self.model.nrows())
        # One repaint per frame: the eased zoom calls this ~90x/sec, and the extra
        # after_geometry_change() present (redundant with redraw() in this engine)
        # doubled the GPU work per frame and read as lag. redraw() alone is enough.
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

    def _jump(self, rng):
        """Reselect the cells an undo/redo touched (None = a view-only change such
        as a filter/sort undo, leave the selection where it is)."""
        if rng is not None:
            r1, c1, r2, c2 = rng
            self.sel, self.extra = (r1, c1, r2, c2), []
            self.active = self.anchor = (r1, c1)
            self.scroll_into_view(r1, c1)
        self.host.redraw()

    # --- clipboard (keyboard + right-click menu share these) --------------
    def copy(self):
        self.host.commit_editor()
        self.host.clipboard_set(self.model.selection_text(self.ranges()))

    def cut(self):
        """Copy then clear. model.delete_selection is a no-op on a read-only grid,
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
        the selection (a multi-cell selection is kept when you right-click inside it)."""
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
