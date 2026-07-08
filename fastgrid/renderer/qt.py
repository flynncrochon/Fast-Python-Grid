"""Qt renderer: blit the SAME core display list with QPainter, on a
QAbstractScrollArea, and wire Qt events into the shared GridController. Proves
the core is toolkit-neutral -- identical geometry, colours, layout and behaviour
as the Tk renderer, different draw calls. This file is "draw the display list,
build the widgets (editor, filter popup, find bar), translate Qt events".
"""
from PySide6 import QtCore, QtGui, QtWidgets

from ..core import theme as T
from ..core.filter import FilterController
from ..core.find import FindController
from ..core.geometry import Geometry
from ..core.gridcontroller import GridController
from ..core.paint import paint, edit_colors
from ..core.render import blit

# Qt key -> the normalized token GridController.on_key expects.
_KEYS = {QtCore.Qt.Key_Up: "Up", QtCore.Qt.Key_Down: "Down", QtCore.Qt.Key_Left: "Left",
         QtCore.Qt.Key_Right: "Right", QtCore.Qt.Key_Home: "Home", QtCore.Qt.Key_End: "End",
         QtCore.Qt.Key_PageUp: "Prior", QtCore.Qt.Key_PageDown: "Next",
         QtCore.Qt.Key_Return: "Return", QtCore.Qt.Key_Enter: "Return",
         QtCore.Qt.Key_Tab: "Tab", QtCore.Qt.Key_Delete: "Delete",
         QtCore.Qt.Key_Backspace: "Delete", QtCore.Qt.Key_F2: "F2"}

_ELIDE = {}   # (bold, text, avail_px) -> elided string; shared across frames


class QtCanvas:
    """Qt backend for core.render.blit -- QPainter calls. Per-frame (holds the
    painter); the QColor cache is shared across frames. Keeps the per-cell hot
    path cheap: cached colours/pens/font-metrics, setFont only on a font change."""

    def __init__(self, p, font, hfont, qcolor):
        self.p, self.font, self.hfont, self._qcolor = p, font, hfont, qcolor
        self.fm = {False: QtGui.QFontMetrics(font), True: QtGui.QFontMetrics(hfont)}
        self._pens = {}
        self.cur_font = None
        self.A_L = int(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft)
        self.A_C = int(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignHCenter)
        self.elide = QtCore.Qt.ElideRight

    def _c(self, s):
        c = self._qcolor.get(s)
        if c is None:
            c = self._qcolor[s] = QtGui.QColor(s)
        return c

    def _pen(self, s, w):
        pen = self._pens.get((s, w))
        if pen is None:
            pen = self._pens[(s, w)] = QtGui.QPen(self._c(s), w)
        return pen

    def rect(self, x, y, w, h, fill=None, outline=None, width=1):
        if fill is not None:
            self.p.fillRect(x, y, w, h, self._c(fill))
        if outline is not None:
            self.p.setPen(self._pen(outline, width))
            self.p.setBrush(QtCore.Qt.NoBrush)
            self.p.drawRect(x, y, w, h)

    def text(self, x, y, w, h, s, color, bold=False, center=False):
        f = self.hfont if bold else self.font
        if f is not self.cur_font:                   # setFont is not free; only on change
            self.p.setFont(f); self.cur_font = f
        self.p.setPen(self._c(color))
        if center:
            self.p.drawText(QtCore.QRect(x, y, w, h), self.A_C, self._clip(bold, s, w - 4))
        else:
            self.p.drawText(QtCore.QRect(x + 5, y, w - 9, h), self.A_L, self._clip(bold, s, w - 9))

    def _clip(self, bold, s, avail):
        """Elide to `avail`px, memoized. elidedText measures the string every call
        and the same (text, width) recurs constantly while scrolling repeated data
        -- the same reason the Tk backend memoizes _fit."""
        key = (bold, s, avail)
        r = _ELIDE.get(key)
        if r is None:
            r = _ELIDE[key] = self.fm[bold].elidedText(s, self.elide, avail)
            if len(_ELIDE) > 16384:                  # bounded; scrolling reuses a hot set
                _ELIDE.clear()
        return r

    def line(self, x1, y1, x2, y2, color, width):
        self.p.setPen(self._pen(color, width))
        self.p.drawLine(x1, y1, x2, y2)

    def poly(self, points, color):
        self.p.setPen(QtCore.Qt.NoPen)
        self.p.setBrush(self._c(color))
        self.p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(px, py) for px, py in points]))

    def glyph(self, cx, cy, s, color, px):
        f = QtGui.QFont(self.font)
        f.setPixelSize(int(px))
        self.p.setFont(f); self.cur_font = None
        self.p.setPen(self._c(color))
        self.p.drawText(QtCore.QRectF(cx - px, cy - px, px * 2, px * 2),
                        int(QtCore.Qt.AlignCenter), s)


class QtGrid(QtWidgets.QAbstractScrollArea):
    def __init__(self, model, editable=True, frozen=0, col_w=None, parent=None):
        super().__init__(parent)
        self.model = model
        self.editable = editable
        self.geom = Geometry(col_w or [120] * model.ncols, frozen)
        self.font = QtGui.QFont("Segoe UI", 10)
        self.hfont = QtGui.QFont("Segoe UI", 10, QtGui.QFont.Bold)
        self._base_pt = 10.0               # zoom scales the point size off this
        self.ctl = GridController(self, base_row_h=self.geom.row_h,
                                  base_gutter=self.geom.gutter_w, base_w=list(self.geom.col_w))
        self._qcolor = {}                  # str -> QColor cache (only ~5 recur per frame)
        self._editor = None

        vp = self.viewport()
        vp.setMouseTracking(True)
        vp.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.verticalScrollBar().valueChanged.connect(self._v_changed)
        self.horizontalScrollBar().valueChanged.connect(self._h_changed)
        model.changed = self._on_change
        self.find = QtFindBar(self)

    # --- scrollbars ---------------------------------------------------
    def after_geometry_change(self):
        g = self.geom
        v, h = self.verticalScrollBar(), self.horizontalScrollBar()
        v.setRange(1, g.max_top(self.model.nrows()))       # top_row is a DATA row (>=1)
        v.setPageStep(max(1, g.full_rows()))
        h.setRange(0, g.max_scroll_x())
        h.setPageStep(max(1, g.w - g.freeze_x()))
        h.setSingleStep(40)

    def after_scroll_change(self):
        self.verticalScrollBar().setValue(self.geom.top_row)
        self.horizontalScrollBar().setValue(self.geom.scroll_x)

    def redraw(self):
        self.viewport().update()

    def _v_changed(self, val):
        self.geom.top_row = val
        self.viewport().update()

    def _h_changed(self, val):
        self.geom.scroll_x = val
        self.viewport().update()

    def _on_change(self):
        self.after_geometry_change()
        self.viewport().update()

    # --- GridController host surface (widgets / fonts / cursor) --------
    def set_edge_cursor(self, on_edge):
        self.viewport().setCursor(QtCore.Qt.SplitHCursor if on_edge else QtCore.Qt.ArrowCursor)

    def reveal_find(self):
        self.find.reveal()

    def open_filter_popup(self, col, at):
        QtFilterPopup(self, col, at)

    def measure(self, text, bold):
        return QtGui.QFontMetrics(self.hfont if bold else self.font).horizontalAdvance(text)

    def set_zoom_fonts(self, z):
        self.font.setPointSizeF(max(6.0, self._base_pt * z))
        self.hfont.setPointSizeF(max(6.0, self._base_pt * z))

    def clipboard_set(self, text):
        QtWidgets.QApplication.clipboard().setText(text)

    def clipboard_get(self):
        return QtWidgets.QApplication.clipboard().text()

    # --- zoom / resize ------------------------------------------------
    def wheelEvent(self, e):
        if e.modifiers() & QtCore.Qt.ControlModifier:
            d = e.angleDelta().y()
            if d:
                self.ctl.zoom_by(1.1 if d > 0 else 1 / 1.1)
            e.accept()
            return
        super().wheelEvent(e)                                   # normal scroll

    def resizeEvent(self, e):
        self.geom.w = self.viewport().width()
        self.geom.h = self.viewport().height()
        self.after_geometry_change()
        if self.find.isVisible():
            self.find.reposition()
        super().resizeEvent(e)

    # --- paint --------------------------------------------------------
    def paintEvent(self, e):
        g = self.geom
        g.w, g.h = self.viewport().width(), self.viewport().height()
        dl = paint(self.model, g, self.ctl.active, self.ctl.ranges(), self.ctl.corner_hover)
        p = QtGui.QPainter(self.viewport())
        blit(dl, QtCanvas(p, self.font, self.hfont, self._qcolor))
        p.end()
        self._place_editor()

    # --- events -> controller (viewport events are delivered here) ----
    @staticmethod
    def _pos(e):
        pos = e.position().toPoint() if hasattr(e, "position") else e.pos()
        return pos.x(), pos.y()

    def mousePressEvent(self, e):
        self.viewport().setFocus()
        x, y = self._pos(e)
        mods = e.modifiers()
        gp = e.globalPosition().toPoint() if hasattr(e, "globalPosition") else e.globalPos()
        self.ctl.on_press(x, y, bool(mods & QtCore.Qt.ControlModifier),
                          bool(mods & QtCore.Qt.ShiftModifier), gp)

    def mouseMoveEvent(self, e):
        x, y = self._pos(e)
        if self.ctl.resize_col is not None or self.ctl.drag_region is not None:
            self.ctl.on_drag(x, y)
        else:
            self.ctl.on_motion(x, y)

    def mouseReleaseEvent(self, e):
        self.ctl.on_release()

    def contextMenuEvent(self, e):
        vp = self.viewport().mapFromGlobal(e.globalPos())
        self.ctl.context_select(vp.x(), vp.y())
        m = QtWidgets.QMenu(self)
        m.addAction("Copy", self.ctl.copy)
        m.addAction("Cut", self.ctl.cut).setEnabled(self.editable)   # no-op on read-only
        m.addAction("Paste", self.ctl.paste).setEnabled(self.editable)
        m.addAction("Delete", self.ctl.delete).setEnabled(self.editable)
        m.exec(e.globalPos())

    def mouseDoubleClickEvent(self, e):
        self.ctl.on_double(*self._pos(e))

    def leaveEvent(self, e):
        self.ctl.set_corner_hover(False)

    def keyPressEvent(self, e):
        key = e.key()
        if QtCore.Qt.Key_A <= key <= QtCore.Qt.Key_Z:
            token = chr(key).lower()
        else:
            token = _KEYS.get(key, "")
        mods = e.modifiers()
        if not self.ctl.on_key(token, bool(mods & QtCore.Qt.ShiftModifier),
                               bool(mods & QtCore.Qt.ControlModifier), e.text()):
            super().keyPressEvent(e)

    # --- editor -------------------------------------------------------
    def begin_edit(self, initial=None):
        if not self.editable:
            return
        self.commit_editor()
        r, c = self.ctl.active
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

    def commit_editor(self, move=None):
        ed = self._editor
        if not ed:
            return
        r, c = ed._cell
        self.model.set_cell(r, c, ed.text())
        self._editor = None
        ed.removeEventFilter(self)
        ed.deleteLater()
        if move:
            self.ctl.active = self.ctl.anchor = (r, c)
            self.ctl.move(move)
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
            if k in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter, QtCore.Qt.Key_Down):
                self.commit_editor(move=(1, 0)); return True
            if k == QtCore.Qt.Key_Up:
                self.commit_editor(move=(-1, 0)); return True
            if k == QtCore.Qt.Key_Tab:
                self.commit_editor(move=(0, 1)); return True
            if k == QtCore.Qt.Key_Backtab:
                self.commit_editor(move=(0, -1)); return True
            if k == QtCore.Qt.Key_Escape:
                self._cancel_editor(); return True
        return super().eventFilter(obj, ev)


class QtFilterPopup(QtWidgets.QFrame):
    """spreadsheet-style column filter/sort popup -- the Qt widget over the shared
    FilterController (identical behaviour to the Tk popup)."""

    def __init__(self, grid, col, global_pos):
        super().__init__(grid, QtCore.Qt.Popup)
        self.g, self.model = grid, grid.model
        self.ctl = FilterController(grid.model, col)
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
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
        self.ctl.load()
        self._repopulate()

    def _repopulate(self):
        if self.ctl.state is None:         # still loading the distinct scan
            return
        self.lst.blockSignals(True)
        self.lst.clear()
        rows = self.ctl.rows(self.search.text())
        # Checkbox is display-only (no ItemIsUserCheckable) so the indicator click
        # doesn't toggle natively AND via itemClicked -- _on_click owns toggling.
        head = QtWidgets.QListWidgetItem("(Select all)")
        head.setCheckState(QtCore.Qt.Checked if self.ctl.all_on(rows) else QtCore.Qt.Unchecked)
        head.setData(QtCore.Qt.UserRole, "\0all")
        self.lst.addItem(head)
        for v in rows:
            it = QtWidgets.QListWidgetItem(v if v else "(blank)")
            it.setCheckState(QtCore.Qt.Checked if self.ctl.checked(v) else QtCore.Qt.Unchecked)
            it.setData(QtCore.Qt.UserRole, v)
            self.lst.addItem(it)
        if self.ctl.truncated(rows):       # list truncated -> tell the user to narrow
            note = QtWidgets.QListWidgetItem("… too many to list — type to search")
            note.setFlags(QtCore.Qt.NoItemFlags)
            note.setForeground(QtGui.QColor("#8a8578"))
            self.lst.addItem(note)
        self.lst.blockSignals(False)

    def _on_click(self, item):
        if self.ctl.state is None:
            return
        key = item.data(QtCore.Qt.UserRole)
        if key is None:                    # the "(too many…)" note line
            return
        if key == "\0all":
            self.ctl.toggle_all(self.ctl.rows(self.search.text()))
        else:
            self.ctl.toggle(key)
        self._repopulate()

    def _sort(self, asc):
        self.model.set_sort(self.ctl.col, asc); self.close()

    def _clear(self):
        self.model.clear_column_filter(self.ctl.col); self.close()

    def _text(self, op):
        self.close()
        val, ok = QtWidgets.QInputDialog.getText(self.g, "Text filter", op.capitalize() + ":")
        if ok:
            self.model.set_text_filter(self.ctl.col, op, val)

    def _apply(self):
        if self.ctl.state is None:         # OK before the list loaded = no-op
            self.close(); return
        self.ctl.commit(self.search.text())
        self.close()


class QtFindBar(QtWidgets.QFrame):
    """Qt find bar -- a thin widget over the shared core FindController (identical
    logic to the Tk find bar)."""

    def __init__(self, grid):
        super().__init__(grid.viewport())
        self.g = grid
        self.ctl = FindController(grid.ctl)
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
        self._timer.setInterval(120)         # keystroke debounce
        self._timer.timeout.connect(lambda: self.ctl.run(self.entry.text(), navigate=False))
        self.hide()

    def reveal(self):
        has_scope = self.ctl.open(self.g.ctl.ranges())
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
