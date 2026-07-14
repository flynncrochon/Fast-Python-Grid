"""Toolkit-neutral grid controller: owns the selection state machine and turns
normalized input events into core calls. Shared by the Tk and Qt renderers
(selection, click+drag incl. frozen-pane crossing, keyboard nav/copy/paste/undo,
column resize + autofit, Ctrl+wheel zoom).

The renderer owns only per-toolkit bits (drawing, widgets, native events) and
delegates the logic here through this ``host`` surface:

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
        # Metrics recomputed from these base * _zoom (never ratio-chained) so it never
        # drifts. Manual resizes write back to _base_w so zoom keeps them proportional.
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
        # Uncapped: let selection/arrow-nav reach on-screen phantom cells. The bound
        # grows with the view, so each scroll reveals more reachable cells.
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

    def select(self, r, c):
        """Set the single-cell selection to grid cell (r, c), clamped to the sheet,
        and scroll it into view. Mirrors where an arrow key lands."""
        r = max(0, min(self.model.nrows() - 1, r))
        c = max(0, min(self.model.ncols - 1, c))
        self.active = self.anchor = (r, c)
        self.sel, self.extra = (r, c, r, c), []
        self.scroll_into_view(r, c)

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
            self._pending_dropdown = True    # open in on_release so this release doesn't
            self.scroll_into_view(*self.active)   # land on the fresh popup and dismiss it
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
        """Resolve a drag-extend. ``follow`` scrolls to keep the pressed-to cell
        visible; the engine passes follow=False while its edge-autoscroll timer owns
        scrolling, else the two compound and fly thousands of rows past the edge."""
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
        if x < g.gutter_w:
            col = 0                                    # past the left edge -> select down to col 0; the frozen cells
        else:                                          # select naturally while autoscroll reveals the hidden gap.
            if col is None:
                col = ncols - 1                        # past the right edge / phantom
            # Over the frozen block: reveal the hidden scrollable columns one per
            # motion; once scrolled home this returns the frozen column under the
            # pointer so the selection lands on the pinned cells.
            col = S.edge_reveal_col(col, anchor_col=self.anchor[1], frozen_cols=g.frozen,
                                    scroll_x=g.scroll_x, ncols=ncols, pointer_x=x,
                                    gutter_w=g.gutter_w, frozen_w=g.frozen_w(),
                                    body_w=g.w, leaf_x=g.col_x)
        self.sel, self.active = S.resolve_drag(
            self.drag_region, row, col, anchor=self.anchor, **self.bounds())
        if follow:
            self.scroll_into_view(*self.active)
        g.clamp(nrows)
        self.host.redraw()

    def on_release(self):
        self.drag_region = None
        self.resize_col = None
        if self._pending_dropdown:          # ▼ button click, open the list now
            self._pending_dropdown = False
            self.host.begin_edit()
        self.host.redraw()      # full repaint so chrome reflects the final selection

    def on_double(self, x, y):
        ec = self.geom.col_edge_hit(x, y, self.model.ncols)
        if ec is not None:                                  # dbl-click border = autofit
            self.autofit(ec)
            return
        region, row, col = self.geom.hit(x, y, self.model.nrows(), self.model.ncols)
        if region == "cell" and self.editable:
            self.active = (row, col)
            if self.model.cell_choices(row, col) is not None:
                self._pending_dropdown = True    # open on the trailing release, else it
            else:                                # lands on and dismisses the popup
                self.host.begin_edit()

    def ensure_col(self, c):
        """Grow the sheet so column `c` exists (editing a phantom column, uncapped);
        no-op for a real column. Widens model AND geometry at the phantom width so new
        columns render/hit/zoom like the originals."""
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
        """Set a column width and record it as the new zoom base so a later zoom keeps
        it proportional instead of snapping back."""
        self.geom.set_col_w(c, w)
        self._base_w[c] = self.geom.col_w[c] / self._zoom

    def autofit(self, c, rows=None):
        # Default: fit headers + visible rows only (scanning 1M rows would stall, and
        # visible-fit matches what the user sees). Callers that know the grid is small
        # can pass explicit `rows` (grid-row indices) instead.
        H = self.geom.hdr_rows
        sel = self._selected_cols()
        cols = sorted(sel) if c in sel and len(sel) > 1 else [c]   # Ctrl+A -> fit all
        if rows is None:
            rows = list(range(H)) + self.geom.visible_data_rows(self.model.nrows())
        btn = self.geom.row_h - 8 + 8                # filter button + gap (bottom header row)
        arrow = self.geom.row_h                      # dropdown ▼ zone (data cells)
        drop = self.model.cell_choices
        # Reserve the cell's L+R text pad; the GPU renderer sizes it fpx*9/13, which
        # grows with zoom, so a fixed margin trimmed columns when zoomed in.
        pad = round(self.host._fpx * 9 / 13) + 3     # padL+padR at the current zoom + slack
        for cc in cols:
            w = max(self.host.measure(self.model.cell(r, cc), r < H)
                    + (btn if r == H - 1 else arrow if r >= H and drop(r, cc) is not None else 0)
                    for r in rows)
            self.resize_to(cc, w + pad)
        self.host.after_geometry_change()
        self.host.redraw()

    def _selected_cols(self):
        cols = set()
        for (r1, c1, r2, c2) in self.ranges():
            cols.update(range(min(c1, c2), max(c1, c2) + 1))
        return cols

    # --- zoom (Ctrl + wheel) ---------------------------------------------
    def zoom_to(self, z):
        """Apply an absolute zoom factor (clamped). The engine eases toward a target
        by calling this each animation frame (a wheel notch multiplies target by 1.1)."""
        z = max(0.4, min(4.0, z))
        if z == self._zoom:
            return
        self._zoom = z
        g = self.geom
        # scroll_x/scroll_y are PIXEL offsets, so after sizes change the same offset
        # lands on a different cell. Rescale by how much the sizes grew so the cell
        # under the top-left corner stays put (anchor the zoom there).
        old_row_h, old_w = g.row_h, g.content_w()
        self.host.set_zoom_fonts(z)
        g.set_metrics(max(10, round(self._base_row_h * z)),
                      max(24, round(self._base_gutter * z)),
                      [max(20, round(w * z)) for w in self._base_w])
        g.scroll_y = round(g.scroll_y * g.row_h / old_row_h) if old_row_h else g.scroll_y
        new_w = g.content_w()
        g.scroll_x = round(g.scroll_x * new_w / old_w) if old_w else g.scroll_x
        g.clamp(self.model.nrows())
        # One repaint per frame: eased zoom calls this ~90x/sec; an extra
        # after_geometry_change() here doubled GPU work per frame and read as lag.
        self.host.redraw()

    # --- keyboard ---------------------------------------------------------
    def on_key(self, key, shift, ctrl, text):
        """Handle a normalized key (lowercase letter or ARROWS/Return/Tab/Delete/F2).
        Returns True if consumed."""
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
                self.sel, self.extra = (0, 0, lr, lc), []    # header + data (active 0,0 pastes in place)
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
        """Reselect the cells an undo/redo touched (None = view-only change like a
        filter/sort undo, leave the selection put)."""
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
        """Copy then clear. delete_selection is a no-op on a read-only grid, so this
        is safe even if a caller doesn't grey the menu item out."""
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
        """Right-click: select the cell under the cursor unless it's already inside the
        selection (keeps a multi-cell selection when you right-click inside it)."""
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
