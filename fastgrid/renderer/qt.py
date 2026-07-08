"""Qt renderer: blit the SAME core display list with QPainter, on a
QAbstractScrollArea. Proves the core is toolkit-neutral -- identical geometry,
colours and layout as the Tk renderer, different draw calls.

Covers scroll / select / keyboard-nav / in-cell edit / copy-paste. The filter
popup and find bar are Tk-specific widgets in tk.py; the *logic* they drive
(model.set_filter / find_matches) lives in core, so a Qt popup would reuse it --
left out here to keep this renderer thin.
"""
from PySide6 import QtCore, QtGui, QtWidgets

from ..core import selection as S, theme as T
from ..core.find import FindController
from ..core.geometry import Geometry
from ..core.paint import paint, edit_colors


class QtGrid(QtWidgets.QAbstractScrollArea):
    def __init__(self, model, editable=True, frozen=0, col_w=None, parent=None):
        super().__init__(parent)
        self.model = model
        self.editable = editable
        self.geom = Geometry(col_w or [120] * model.ncols, frozen)
        self.font = QtGui.QFont("Segoe UI", 10)
        self.hfont = QtGui.QFont("Segoe UI", 10, QtGui.QFont.Bold)
        # Zoom (Ctrl+wheel): metrics recomputed from these base values * _zoom,
        # never ratio-chained, so it stays crisp and never drifts. Manual column
        # resizes write back to _base_w so a later zoom keeps them proportional.
        self._zoom = 1.0
        self._base_pt = 10.0
        self._base_row_h, self._base_gutter = self.geom.row_h, self.geom.gutter_w
        self._base_w = list(self.geom.col_w)
        self._qcolor = {}                  # str -> QColor cache (only ~5 recur per frame)
        self.active = (1, 0)               # start on the first DATA cell (row 0 = header)
        self.anchor = (1, 0)
        self.sel = (1, 0, 1, 0)
        self.extra = []
        self._drag_region = None
        self._editor = None
        self._corner_hover = False
        self._resize_col = None            # column being drag-resized, else None

        vp = self.viewport()
        vp.setMouseTracking(True)
        vp.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.verticalScrollBar().valueChanged.connect(self._v_changed)
        self.horizontalScrollBar().valueChanged.connect(self._h_changed)
        model.changed = self._on_change
        self.find = QtFindBar(self)

    def _ranges(self):
        return list(self.extra) + [self.sel]

    def _col(self, s):
        c = self._qcolor.get(s)
        if c is None:
            c = self._qcolor[s] = QtGui.QColor(s)
        return c

    def _bounds(self):
        return dict(top_hrow=0, last_row=self.model.nrows() - 1,
                    last_col=self.model.ncols - 1)

    # --- scrollbars ---------------------------------------------------
    def _update_bars(self):
        g = self.geom
        v, h = self.verticalScrollBar(), self.horizontalScrollBar()
        v.setRange(1, g.max_top(self.model.nrows()))       # top_row is a DATA row (>=1)
        v.setPageStep(max(1, g.full_rows()))
        h.setRange(0, g.max_scroll_x())
        h.setPageStep(max(1, g.w - g.freeze_x()))
        h.setSingleStep(40)

    def _v_changed(self, val):
        self.geom.top_row = val
        self.viewport().update()

    def _h_changed(self, val):
        self.geom.scroll_x = val
        self.viewport().update()

    def _on_change(self):
        self._update_bars()
        self.viewport().update()

    # --- zoom (Ctrl + wheel) ------------------------------------------
    def wheelEvent(self, e):
        if e.modifiers() & QtCore.Qt.ControlModifier:
            d = e.angleDelta().y()
            if d:
                self._set_zoom(self._zoom * (1.1 if d > 0 else 1 / 1.1))
            e.accept()
            return
        super().wheelEvent(e)                                   # normal scroll

    def _set_zoom(self, z):
        z = max(0.4, min(4.0, z))
        if abs(z - self._zoom) < 1e-9:
            return
        self._zoom = z
        self.font.setPointSizeF(max(6.0, self._base_pt * z))
        self.hfont.setPointSizeF(max(6.0, self._base_pt * z))
        self.geom.set_metrics(max(10, round(self._base_row_h * z)),
                              max(24, round(self._base_gutter * z)),
                              [max(20, round(w * z)) for w in self._base_w])
        self.geom.clamp(self.model.nrows())
        self._update_bars()
        self.viewport().update()

    def _resize_to(self, c, w):
        """Set a column width and record it as the new zoom base (see _set_zoom)."""
        self.geom.set_col_w(c, w)
        self._base_w[c] = self.geom.col_w[c] / self._zoom

    def _sync_bars_to_geom(self):
        self.verticalScrollBar().setValue(self.geom.top_row)
        self.horizontalScrollBar().setValue(self.geom.scroll_x)

    def resizeEvent(self, e):
        self.geom.w = self.viewport().width()
        self.geom.h = self.viewport().height()
        self._update_bars()
        if self.find.isVisible():
            self.find.reposition()
        super().resizeEvent(e)

    # --- paint --------------------------------------------------------
    def paintEvent(self, e):
        g = self.geom
        g.w, g.h = self.viewport().width(), self.viewport().height()
        dl = paint(self.model, g, self.active, self._ranges(), self._corner_hover)
        p = QtGui.QPainter(self.viewport())
        col = self._col                                  # str -> cached QColor
        grid_pen = QtGui.QPen(col(T.GRID))
        # Per-frame constants hoisted out of the per-cell loop (were rebuilt each cell).
        fm = {0: QtGui.QFontMetrics(self.font), T.FLAG_BOLD: QtGui.QFontMetrics(self.hfont)}
        A_L = int(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft)
        A_C = int(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignHCenter)
        elide = QtCore.Qt.ElideRight
        cur_font = None
        for (x, y, w, h, text, bg, fg, flags) in dl.cells:
            p.fillRect(x, y, w, h, col(bg))
            p.setPen(grid_pen)
            p.drawRect(x, y, w, h)
            if text:
                bold = flags & T.FLAG_BOLD
                f = self.hfont if bold else self.font
                if f is not cur_font:                    # setFont is not free; only on change
                    p.setFont(f); cur_font = f
                p.setPen(col(fg))
                m = fm[bold]
                if flags & T.FLAG_CENTER:
                    p.drawText(QtCore.QRect(x, y, w, h), A_C,
                               m.elidedText(text, elide, w - 4))
                else:
                    p.drawText(QtCore.QRect(x + 5, y, w - 9, h), A_L,
                               m.elidedText(text, elide, w - 9))
        for ov in dl.overlays:
            k = ov[0]
            if k == "line":
                p.setPen(QtGui.QPen(QtGui.QColor(ov[5]), ov[6]))
                p.drawLine(ov[1], ov[2], ov[3], ov[4])
            elif k == "rect":
                p.setPen(QtGui.QPen(QtGui.QColor(ov[5]), ov[6]))
                p.setBrush(QtCore.Qt.NoBrush)
                p.drawRect(ov[1], ov[2], ov[3], ov[4])
            elif k == "filterbtn":
                self._draw_filter_btn(p, *ov[1:])
            elif k == "tri":
                x1, y1, sz, col = ov[1], ov[2], ov[3], QtGui.QColor(ov[4])
                p.setPen(QtCore.Qt.NoPen)
                p.setBrush(col)
                p.drawPolygon(QtGui.QPolygon([QtCore.QPoint(x1 - sz, y1),
                              QtCore.QPoint(x1, y1 - sz), QtCore.QPoint(x1, y1)]))
        p.end()
        self._place_editor()

    def _draw_filter_btn(self, p, bx, by, sz, state):
        p.setPen(QtGui.QPen(QtGui.QColor(T.BTN_BORDER), 1))
        p.setBrush(QtGui.QColor(T.BTN_BG))
        p.drawRect(bx, by, sz, sz)
        if state == "funnel":
            mx, fw, sw = bx + sz / 2, sz * 0.22, sz * 0.05
            t, n, b = by + sz * 0.24, by + sz * 0.49, by + sz * 0.72
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(QtGui.QColor(T.FUNNEL))
            p.drawPolygon(QtGui.QPolygonF([
                QtCore.QPointF(mx - fw, t), QtCore.QPointF(mx + fw, t),
                QtCore.QPointF(mx + sw, n), QtCore.QPointF(mx + sw, b),
                QtCore.QPointF(mx - sw, b), QtCore.QPointF(mx - sw, n)]))
        else:
            glyph = "▲" if state == "asc" else "▼"
            col = T.ARROW_IDLE if state == "idle" else T.ARROW_SORT
            f = QtGui.QFont(self.font)
            f.setPixelSize(max(6, int(sz * 0.7)))
            p.setFont(f)
            p.setPen(QtGui.QColor(col))
            p.drawText(QtCore.QRect(bx, by, sz, sz), int(QtCore.Qt.AlignCenter), glyph)

    # --- scroll-into-view ---------------------------------------------
    def _scroll_into_view(self, r, c):
        self.geom.scroll_into_view(r, c)
        self.geom.clamp(self.model.nrows())
        self._sync_bars_to_geom()

    # --- mouse (viewport events are delivered here) -------------------
    def mousePressEvent(self, e):
        self.viewport().setFocus()
        self._commit_editor()
        pos = e.position().toPoint() if hasattr(e, "position") else e.pos()
        region, row, col = self.geom.hit(pos.x(), pos.y(), self.model.nrows(), self.model.ncols)
        if row == 0 and region == "cell" and col is not None \
                and self.geom.filter_btn_hit(pos.x(), pos.y(), col):   # header filter button
            self._drag_region = None
            gp = e.globalPosition().toPoint() if hasattr(e, "globalPosition") else e.globalPos()
            QtFilterPopup(self, col, gp)
            return
        ec = self.geom.col_edge_hit(pos.x(), pos.y(), self.model.ncols)
        if ec is not None:                                         # grab a column border
            self._resize_col = ec
            self._resize_x0, self._resize_w0 = pos.x(), self.geom.col_w[ec]
            self._drag_region = None
            return
        mods = e.modifiers()
        ctrl = bool(mods & QtCore.Qt.ControlModifier)
        shift = bool(mods & QtCore.Qt.ShiftModifier)
        self.sel, self.extra, self.active, self.anchor = S.resolve_click(
            region, row, col, anchor=self.anchor, sel=self.sel, extra=self.extra,
            ctrl=ctrl, shift=shift, **self._bounds())
        self._drag_region = region
        self._scroll_into_view(*self.active)
        self.viewport().update()

    def leaveEvent(self, e):
        if self._corner_hover:
            self._corner_hover = False
            self.viewport().update()

    def mouseMoveEvent(self, e):
        g = self.geom
        nrows, ncols = self.model.nrows(), self.model.ncols
        pos = e.position().toPoint() if hasattr(e, "position") else e.pos()
        x, y = pos.x(), pos.y()
        if self._resize_col is not None:               # live column resize
            self._resize_to(self._resize_col, self._resize_w0 + (x - self._resize_x0))
            self._update_bars()
            self.viewport().update()
            return
        if self._drag_region is None:                  # hover: split-cursor over a border
            on_edge = g.col_edge_hit(x, y, ncols) is not None
            self.viewport().setCursor(QtCore.Qt.SplitHCursor if on_edge
                                      else QtCore.Qt.ArrowCursor)
        over = g.in_corner(x, y)
        if over != self._corner_hover:                 # hover-highlight the corner
            self._corner_hover = over
            self.viewport().update()
        if self._drag_region is None:
            return
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
            self._drag_region, row, col, anchor=self.anchor, **self._bounds())
        self._scroll_into_view(*self.active)           # push the view to the pressed-to cell
        self.viewport().update()

    def mouseReleaseEvent(self, e):
        self._drag_region = None
        self._resize_col = None

    def mouseDoubleClickEvent(self, e):
        pos = e.position().toPoint() if hasattr(e, "position") else e.pos()
        ec = self.geom.col_edge_hit(pos.x(), pos.y(), self.model.ncols)
        if ec is not None:                             # dbl-click border = autofit
            self._autofit_col(ec)
            return
        region, row, col = self.geom.hit(pos.x(), pos.y(), self.model.nrows(), self.model.ncols)
        if region == "cell" and self.editable:
            self.active = (row, col)
            self._begin_edit()

    def _autofit_col(self, c):
        # ponytail: fit header + currently-visible rows only. Scanning 1M rows for
        # the widest cell would stall; visible-fit matches what the user sees.
        sel = self._selected_cols()
        cols = sorted(sel) if c in sel and len(sel) > 1 else [c]   # Ctrl+A -> fit all
        fm = {0: QtGui.QFontMetrics(self.hfont), 1: QtGui.QFontMetrics(self.font)}
        rows = [0] + self.geom.visible_data_rows(self.model.nrows())
        btn = self.geom.row_h - 8 + 8                  # filter button + gap (header only)
        for cc in cols:
            w = max(fm[0 if r == 0 else 1].horizontalAdvance(self.model.cell(r, cc))
                    + (btn if r == 0 else 0) for r in rows)
            self._resize_to(cc, w + 12)                # 5px text inset + margin
        self._update_bars()
        self.viewport().update()

    def _selected_cols(self):
        cols = set()
        for (r1, c1, r2, c2) in self._ranges():
            cols.update(range(min(c1, c2), max(c1, c2) + 1))
        return cols

    # --- keyboard -----------------------------------------------------
    def keyPressEvent(self, e):
        key = e.key()
        mods = e.modifiers()
        ctrl = bool(mods & QtCore.Qt.ControlModifier)
        shift = bool(mods & QtCore.Qt.ShiftModifier)
        K = QtCore.Qt
        if ctrl and key == K.Key_F:
            self.find.reveal(); return
        if ctrl and key == K.Key_C:
            QtWidgets.QApplication.clipboard().setText(
                self.model.selection_text(self._ranges())); return
        if ctrl and key == K.Key_V:
            box = self.model.paste_text(QtWidgets.QApplication.clipboard().text(),
                                        self._ranges(), self.active)
            if box:
                self.sel, self.extra = box, []
            self.viewport().update(); return
        if ctrl and key == K.Key_Z:
            self.model.undo(); return
        if ctrl and key == K.Key_Y:
            self.model.redo(); return
        if ctrl and key == K.Key_A:
            lr, lc = self.model.data_extent()
            self.sel, self.extra = (0, 0, lr, lc), []   # header + data (active at 0,0 -> paste in place)
            self.active = self.anchor = (0, 0)
            self.viewport().update(); return
        names = {K.Key_Up: "Up", K.Key_Down: "Down", K.Key_Left: "Left",
                 K.Key_Right: "Right", K.Key_Home: "Home", K.Key_End: "End",
                 K.Key_PageUp: "Prior", K.Key_PageDown: "Next"}
        if key in names:
            self._arrow(names[key], shift, ctrl); return
        if key in (K.Key_Return, K.Key_Enter):
            self._move((1, 0)); return
        if key == K.Key_Tab:
            self._move((0, 1)); return
        if key in (K.Key_Delete, K.Key_Backspace):
            self.model.delete_selection(self._ranges()); return
        if key == K.Key_F2:
            self._begin_edit(); return
        if e.text() and e.text().isprintable() and not ctrl and self.editable:
            self._begin_edit(initial=e.text()); return
        super().keyPressEvent(e)

    def _arrow(self, name, shift, ctrl):
        self.sel, self.extra, self.active, self.anchor = S.resolve_arrow(
            name, active=self.active, anchor=self.anchor, shift=shift, ctrl=ctrl,
            page_rows=self.geom.full_rows(),
            occupied_row=self.model.occupied_row,
            occupied_col=(lambda c: self.model.occupied_col_at(self.active[0], c)),
            **self._bounds())
        self._scroll_into_view(*self.active)
        self.viewport().update()

    def _move(self, d):
        r = max(0, min(self.model.nrows() - 1, self.active[0] + d[0]))
        c = max(0, min(self.model.ncols - 1, self.active[1] + d[1]))
        self.active = self.anchor = (r, c)
        self.sel, self.extra = (r, c, r, c), []
        self._scroll_into_view(r, c)
        self.viewport().update()

    # --- editor -------------------------------------------------------
    def _begin_edit(self, initial=None):
        if not self.editable:
            return
        self._commit_editor()
        r, c = self.active
        bg, fg = edit_colors(r)              # keep the cell's own background + text colour
        ed = QtWidgets.QLineEdit(self.viewport())
        ed.setFont(self.font)                # match the cell font (don't grow the text)
        ed.setStyleSheet("QLineEdit{background:%s;color:%s;border:1px solid %s;padding:0 4px;}"
                         % (bg, fg, T.ACCENT))
        ed.setText(initial if initial is not None else self.model.cell(r, c))
        ed._cell = (r, c)
        ed.installEventFilter(self)          # commit/cancel keys, consumed (not to the grid)
        self._editor = ed
        self._place_editor()
        ed.show()
        ed.setFocus(QtCore.Qt.OtherFocusReason)
        ed.setCursorPosition(len(ed.text()))

    def _place_editor(self):
        ed = self._editor
        if not ed:
            return
        r, c = ed._cell
        if not self.geom.cell_visible(r, c):
            ed.hide(); return
        x = max(self.geom.gutter_w, self.geom.col_x(c))
        ed.setGeometry(x, self.geom.row_y(r), self.geom.col_w[c] + 1,
                       self.geom.row_h_at(r) + 1)
        ed.show()

    def _commit_editor(self, move=None):
        ed = self._editor
        if not ed:
            return
        r, c = ed._cell
        self.model.set_cell(r, c, ed.text())
        self._editor = None
        ed.removeEventFilter(self)
        ed.deleteLater()
        if move:
            self.active = self.anchor = (r, c)
            self._move(move)
        else:
            self.viewport().update()
        self.viewport().setFocus()     # return focus so the next keystroke edits again

    def _cancel_editor(self):
        ed = self._editor
        if ed:
            self._editor = None
            ed.removeEventFilter(self)
            ed.deleteLater()
            self.viewport().update()
            self.viewport().setFocus()

    def eventFilter(self, obj, ev):
        # Commit/cancel keys for the in-cell editor, CONSUMED so they never bubble
        # up to the grid's keyPressEvent (which would move the cell a second time).
        if obj is self._editor and ev.type() == QtCore.QEvent.KeyPress:
            k = ev.key()
            if k in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
                self._commit_editor(move=(1, 0)); return True
            if k == QtCore.Qt.Key_Tab:
                self._commit_editor(move=(0, 1)); return True
            if k == QtCore.Qt.Key_Backtab:
                self._commit_editor(move=(0, -1)); return True
            if k == QtCore.Qt.Key_Escape:
                self._cancel_editor(); return True
        return super().eventFilter(obj, ev)


class QtFilterPopup(QtWidgets.QFrame):
    """spreadsheet-style column filter/sort popup for the Qt renderer. Drives the SAME
    core logic the Tk popup does (model.set_filter / set_sort / set_text_filter)."""

    def __init__(self, grid, col, global_pos):
        super().__init__(grid, QtCore.Qt.Popup)
        self.g, self.model, self.col = grid, grid.model, col
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self._state = None                 # distinct scan is deferred (see _load)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(3)

        def btn(text, slot, enabled=True):
            b = QtWidgets.QPushButton(text)
            b.setFlat(True)
            b.setStyleSheet("text-align:left;padding:4px;")
            b.clicked.connect(slot)
            b.setEnabled(enabled)
            lay.addWidget(b)
            return b

        btn("Sort A → Z", lambda: self._sort(True))
        btn("Sort Z → A", lambda: self._sort(False))
        btn("Clear Filter", self._clear, self.model.has_filter(col))
        btn("Contains…", lambda: self._text("contains"))
        btn("Equals…", lambda: self._text("equals"))
        self.search = QtWidgets.QLineEdit(placeholderText="Search…")
        self.search.textChanged.connect(self._repopulate)
        self.search.returnPressed.connect(self._apply)     # Enter = OK
        lay.addWidget(self.search)
        self.lst = QtWidgets.QListWidget()
        self.lst.setMaximumHeight(240)
        self.lst.itemClicked.connect(self._on_click)   # toggle on a click anywhere in the row
        self.lst.addItem("Loading…")
        lay.addWidget(self.lst)
        foot = QtWidgets.QHBoxLayout()
        foot.addStretch(1)
        ok = QtWidgets.QPushButton("OK"); ok.clicked.connect(self._apply)
        cancel = QtWidgets.QPushButton("Cancel"); cancel.clicked.connect(self.close)
        foot.addWidget(ok); foot.addWidget(cancel)
        lay.addLayout(foot)

        self.adjustSize()
        self.move(global_pos)
        self.show()
        self.search.setFocus()
        # Pop now; run the (possibly heavy) distinct scan on the next event-loop
        # tick so opening the popup is instant even on a 1M-row column.
        QtCore.QTimer.singleShot(0, self._load)

    def _load(self):
        self._active = self.model._filters.get(self.col)
        self._preloaded, self._capped = self.model.distinct_capped(self.col)
        self._state = {v: self._checked(v) for v in self._preloaded}
        self._repopulate()

    def _checked(self, v):
        """Checked state -- an explicit user toggle, else the default from the
        active filter (all allowed when there's no filter)."""
        if self._state and v in self._state:
            return self._state[v]
        return self._active is None or v in self._active

    def _rows(self):
        q = self.search.text().strip().lower()
        if not q:
            return self._preloaded
        if self._capped:                   # search the whole column, not just the preview
            return self.model.distinct_matching(self.col, q)
        return [v for v in self._preloaded if q in v.lower()]

    def _repopulate(self):
        if self._state is None:            # still loading the distinct scan
            return
        self.lst.blockSignals(True)
        self.lst.clear()
        rows = self._rows()
        all_on = bool(rows) and all(self._checked(v) for v in rows)
        # Checkbox is display-only (no ItemIsUserCheckable) so the indicator click
        # doesn't toggle natively AND via itemClicked -- _on_click owns toggling.
        head = QtWidgets.QListWidgetItem("(Select all)")
        head.setCheckState(QtCore.Qt.Checked if all_on else QtCore.Qt.Unchecked)
        head.setData(QtCore.Qt.UserRole, "\0all")
        self.lst.addItem(head)
        for v in rows:
            it = QtWidgets.QListWidgetItem(v if v else "(blank)")
            it.setCheckState(QtCore.Qt.Checked if self._checked(v) else QtCore.Qt.Unchecked)
            it.setData(QtCore.Qt.UserRole, v)
            self.lst.addItem(it)
        if len(rows) >= self.model.DISTINCT_CAP:     # list truncated -> tell the user to narrow
            note = QtWidgets.QListWidgetItem("… too many to list — type to search")
            note.setFlags(QtCore.Qt.NoItemFlags)
            note.setForeground(QtGui.QColor("#8a8578"))
            self.lst.addItem(note)
        self.lst.blockSignals(False)

    def _on_click(self, item):
        if self._state is None:
            return
        key = item.data(QtCore.Qt.UserRole)
        if key is None:                    # the "(too many…)" note line
            return
        if key == "\0all":
            rows = self._rows()
            target = not all(self._checked(v) for v in rows)
            for v in rows:
                self._state[v] = target
        else:
            self._state[key] = not self._checked(key)
        self._repopulate()

    def _sort(self, asc):
        self.model.set_sort(self.col, asc); self.close()

    def _clear(self):
        self.model.clear_column_filter(self.col); self.close()

    def _text(self, op):
        self.close()
        val, ok = QtWidgets.QInputDialog.getText(self.g, "Text filter", op.capitalize() + ":")
        if ok:
            self.model.set_text_filter(self.col, op, val)

    def _apply(self):
        if self._state is None:            # OK before the list loaded = no-op
            self.close(); return
        query = self.search.text().strip()
        if query:
            rows = self._rows()
            if len(rows) >= self.model.DISTINCT_CAP:       # still too many -> "contains"
                self.model.set_text_filter(self.col, "contains", query)
            else:                                          # filter TO the checked matches
                keep = {v for v in rows if self._checked(v)}
                self.model.set_filter(self.col, keep or None)
            self.close(); return
        known = set(self._preloaded) | set(self._state)
        checked = {v for v in known if self._checked(v)}
        # Clear when everything's checked and we truly know it's everything: no
        # active filter, or the full distinct set fits (not capped). Otherwise
        # keep exactly the checked members (inclusion).
        if len(checked) == len(known) and (self._active is None or not self._capped):
            self.model.set_filter(self.col, None)
        else:
            self.model.set_filter(self.col, checked)
        self.close()


class QtFindBar(QtWidgets.QFrame):
    """Qt find bar -- a thin widget over the shared core FindController (identical
    logic to the Tk find bar)."""

    def __init__(self, grid):
        super().__init__(grid.viewport())
        self.g = grid
        self.ctl = FindController(grid)
        self.ctl.on_count = lambda t: self.count.setText(t)
        self._case = self._scope_on = False
        self.setStyleSheet(
            "QFrame{background:#ece7dd;border:2px solid %s;}"
            "QLineEdit{border:0;background:#fbf9f4;padding:2px;}"
            "QToolButton{border:0;padding:2px 6px;}"
            "QToolButton:checked{color:%s;font-weight:bold;}"
            "QLabel{color:#20201e;}" % (T.ACCENT, T.ACCENT))
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(2)
        self.entry = QtWidgets.QLineEdit()
        self.entry.setFixedWidth(150)
        self.entry.textEdited.connect(lambda _t: self._timer.start())
        self.entry.installEventFilter(self)
        lay.addWidget(self.entry)
        self.count = QtWidgets.QLabel("")
        self.count.setMinimumWidth(62)
        lay.addWidget(self.count)

        def tb(text, slot, checkable=False):
            b = QtWidgets.QToolButton()
            b.setText(text)
            b.setCheckable(checkable)
            b.clicked.connect(slot)
            lay.addWidget(b)
            return b

        tb("‹", lambda: self.ctl.step(-1))
        tb("›", lambda: self.ctl.step(1))
        self.case_btn = tb("Aa", self._toggle_case, True)
        self.scope_btn = tb("In", self._toggle_scope, True)
        tb("✕", self.close_bar)
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)         # debounce, matches the example
        self._timer.timeout.connect(lambda: self.ctl.run(self.entry.text(), navigate=False))
        self.hide()

    def reveal(self):
        has_scope = self.ctl.open(self.g._ranges())
        self._scope_on = has_scope
        self.scope_btn.setEnabled(has_scope)
        self.scope_btn.setChecked(has_scope)
        self.case_btn.setChecked(self._case)
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        self.entry.setFocus()
        self.entry.selectAll()

    def reposition(self):
        self.move(max(0, self.g.viewport().width() - self.width() - 6), 4)

    def _toggle_case(self):
        self._case = self.case_btn.isChecked()
        self.ctl.set_case(self._case)

    def _toggle_scope(self):
        if self.ctl.scope_range is None:
            self.scope_btn.setChecked(False)
            return
        self._scope_on = self.scope_btn.isChecked()
        self.ctl.set_scope(self._scope_on)

    def close_bar(self):
        self._timer.stop()
        self.hide()
        self.ctl.close()
        self.g.viewport().setFocus()

    def eventFilter(self, obj, ev):
        # Consume Return/Enter/Escape on the entry so they DRIVE find navigation
        # and never propagate up to the grid (whose Return = "move the cell down").
        if obj is self.entry and ev.type() == QtCore.QEvent.KeyPress:
            k = ev.key()
            if k == QtCore.Qt.Key_Escape:
                self.close_bar()
                return True
            if k in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
                self.ctl.step(-1 if (ev.modifiers() & QtCore.Qt.ShiftModifier) else 1)
                return True
        return super().eventFilter(obj, ev)


def make_sheet(headers, rows, frozen=0, view_only=False, col_w=None,
               title="fastgrid (qt)"):
    """One-call sheet, Qt renderer. Returns (app, window) -- call app.exec()."""
    from ..core.model import GridModel
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    model = GridModel(headers, rows, editable=not view_only)
    win = QtWidgets.QMainWindow()
    win.setWindowTitle(title)
    win.resize(980, 620)
    grid = QtGrid(model, editable=not view_only, frozen=frozen, col_w=col_w)
    win.setCentralWidget(grid)
    win.model, win.grid_view = model, grid
    return app, win
