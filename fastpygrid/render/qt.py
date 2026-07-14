"""Qt (PySide6) host for the toolkit-neutral GpuEngine (fastpygrid.core.gpu).

Same shape as tk.py: the thin Qt adapter owns the window, a native surface widget
(the Gpu child HWND parents to its winId), fonts, event translation, clipboard and
context menu, and implements the host-adapter methods the engine calls. All chrome
is Gpu-drawn, so the widget IS the surface: no sibling Qt widgets.
"""
# The engine works in PHYSICAL pixels; Qt events/sizes are LOGICAL. This host
# converts at the boundary via devicePixelRatioF(), so it's correct whether or not
# the host QApplication has HighDPI scaling on -- no global env hacks needed.
from PySide6 import QtCore, QtGui, QtWidgets

from ..core import theme as T
from ..core.coremodel import make_model
from ..core.gpu import GpuEngine, GridFacade, _load_lib, _screen_scale, UI_FONT

# Qt key -> Tk-style keysym the engine checks (grid nav + TextField overlays).
_KEYS = {
    QtCore.Qt.Key_Up: "Up", QtCore.Qt.Key_Down: "Down", QtCore.Qt.Key_Left: "Left",
    QtCore.Qt.Key_Right: "Right", QtCore.Qt.Key_Home: "Home", QtCore.Qt.Key_End: "End",
    QtCore.Qt.Key_PageUp: "Prior", QtCore.Qt.Key_PageDown: "Next",
    QtCore.Qt.Key_Return: "Return", QtCore.Qt.Key_Enter: "KP_Enter",
    QtCore.Qt.Key_Tab: "Tab", QtCore.Qt.Key_Backtab: "Tab",
    QtCore.Qt.Key_Delete: "Delete", QtCore.Qt.Key_Backspace: "BackSpace",
    QtCore.Qt.Key_Escape: "Escape", QtCore.Qt.Key_F2: "F2",
}


def _keysym(e):
    """Tk-style keysym for a QKeyEvent: named specials, else the letter (for the
    engine's Ctrl+A/C/V/X + single-char checks), else the char itself."""
    k = e.key()
    if k in _KEYS:
        return _KEYS[k]
    if QtCore.Qt.Key_A <= k <= QtCore.Qt.Key_Z:
        return chr(k)                      # "A".."Z", engine lowercases where needed
    t = e.text()
    return t if t else ""


class GpuQtGrid(GridFacade, QtWidgets.QWidget):
    """Thin Qt host. Native self-painted surface: the Gpu child HWND parents to
    winId() and does all drawing; Qt just forwards events."""

    def __init__(self, parent, model, editable=True, frozen=0, col_w=None, scale=1.0, lib=None,
                 uncap_rows=False, uncap_cols=False, filters=True):
        super().__init__(parent)
        # Native, self-painted: Qt won't draw over the Gpu child, and winId() is a real HWND.
        self.setAttribute(QtCore.Qt.WA_NativeWindow, True)
        self.setAttribute(QtCore.Qt.WA_PaintOnScreen, True)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WA_OpaquePaintEvent, True)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setMouseTracking(True)                 # deliver hover moves (no button held)

        self._fpx = max(9, round(13 * scale))
        lp = max(1, round(self._fpx / (self.devicePixelRatioF() or 1.0)))  # logical px -> _fpx physical
        self.font = QtGui.QFont(UI_FONT); self.font.setPixelSize(lp)
        self.hfont = QtGui.QFont(UI_FONT); self.hfont.setPixelSize(lp)
        self.hfont.setBold(True)
        self._fm = {False: QtGui.QFontMetrics(self.font), True: QtGui.QFontMetrics(self.hfont)}

        self.model = model
        self.engine = GpuEngine(self, model, editable=editable, frozen=frozen,
                                col_w=col_w, scale=scale, lib=lib,
                                uncap_rows=uncap_rows, uncap_cols=uncap_cols, filters=filters)

    def paintEngine(self):
        return None                                 # foreign HWND owns the pixels

    # --- native events -> engine's normalized input ---
    @staticmethod
    def _mods(e):
        m = e.modifiers()
        return bool(m & QtCore.Qt.ControlModifier), bool(m & QtCore.Qt.ShiftModifier)

    def _dpr(self):
        return self.devicePixelRatioF() or 1.0

    def _px(self, p):
        """Logical widget position -> physical px the engine works in."""
        d = self._dpr()
        return int(p.x() * d), int(p.y() * d)

    def _root(self, e):
        g = e.globalPosition().toPoint()             # logical global, for QMenu.exec
        return (g.x(), g.y())

    def mousePressEvent(self, e):
        x, y = self._px(e.position())
        if e.button() == QtCore.Qt.RightButton:
            self.engine.context(x, y, self._root(e)); return
        if e.button() != QtCore.Qt.LeftButton:
            return
        ctrl, shift = self._mods(e)
        self.engine.press(x, y, ctrl, shift)

    def mouseMoveEvent(self, e):
        x, y = self._px(e.position())
        if e.buttons() & QtCore.Qt.LeftButton:
            self.engine.drag(x, y)
        else:
            self.engine.motion(x, y)

    def mouseReleaseEvent(self, e):
        if e.button() == QtCore.Qt.LeftButton:
            self.engine.release()

    def mouseDoubleClickEvent(self, e):
        if e.button() == QtCore.Qt.LeftButton:
            self.engine.double(*self._px(e.position()))

    def leaveEvent(self, e):
        self.engine.leave()

    def wheelEvent(self, e):
        dx, dy = e.angleDelta().x(), e.angleDelta().y()
        m = e.modifiers()
        # Float division, not // : high-res wheels deliver sub-120 deltas; floor
        # division truncates asymmetrically (up->0, down->-1) so up feels dead. Float
        # scrolls fractional notches proportionally.
        if m & QtCore.Qt.ControlModifier:
            self.engine.zoom(1.1 if dy > 0 else 1 / 1.1)
        elif m & QtCore.Qt.ShiftModifier:
            self.engine.hwheel(dy / 120.0)
        elif abs(dx) > abs(dy):                       # trackpad horizontal swipe
            self.engine.hwheel(dx / 120.0)
        else:
            self.engine.wheel(dy / 120.0)

    def keyPressEvent(self, e):
        if e.key() == QtCore.Qt.Key_F11:
            self.engine.toggle_fullscreen(); return
        ctrl, shift = self._mods(e)
        consumed = self.engine.key(_keysym(e), e.text(), shift, ctrl)
        if consumed:
            return
        if e.key() == QtCore.Qt.Key_Escape and self.window().isFullScreen():
            self.window().showNormal(); return
        super().keyPressEvent(e)

    def focusNextPrevChild(self, nxt):
        return False                                # let Tab reach keyPressEvent

    def resizeEvent(self, e):
        self.engine.configure_size(*self.size())     # physical

    def showEvent(self, e):
        super().showEvent(e)
        self.engine.configure_size(*self.size())     # physical

    # --- host-adapter API the engine calls (mirrors render.tk.GpuGrid) ---
    def measure(self, text, bold=False):
        return round(self._fm[bool(bold)].horizontalAdvance(text) * self._dpr())

    def size(self):
        d = self._dpr()
        return round(self.width() * d), round(self.height() * d)

    def hwnd(self):
        return int(self.winId())

    def focus(self):
        self.setFocus()

    def set_cursor(self, kind):
        c = {"resize": QtCore.Qt.SizeHorCursor,
             "hand": QtCore.Qt.PointingHandCursor,
             "text": QtCore.Qt.IBeamCursor}.get(kind, QtCore.Qt.ArrowCursor)
        self.setCursor(c)

    def set_zoom_px(self, px):
        lp = max(1, round(px / self._dpr()))         # px is physical; QFont wants logical
        self.font.setPixelSize(lp); self.hfont.setPixelSize(lp)
        self._fm = {False: QtGui.QFontMetrics(self.font), True: QtGui.QFontMetrics(self.hfont)}

    def clip_get(self):
        return QtWidgets.QApplication.clipboard().text() or ""

    def clip_get_html(self):
        md = QtWidgets.QApplication.clipboard().mimeData()
        if md.hasHtml():
            h = md.html()
            if "<table" in h.lower():
                return h
        return ""

    def clip_set(self, text):
        QtWidgets.QApplication.clipboard().setText(text)

    def fullscreen_toggle(self):
        w = self.window()
        w.showNormal() if w.isFullScreen() else w.showFullScreen()

    def context_menu(self, root, actions):
        m = QtWidgets.QMenu(self)
        # no icons/checks on any action, so kill QMenu's left icon gutter
        # that otherwise indents every label vs the tight Tk menu.
        m.setStyleSheet(
            "QMenu::item { padding: 4px 24px 4px 12px; }"
            f"QMenu::item:selected {{ background: {T.SEL_RING}; color: white; }}"
        )
        for label, cmd, enabled in actions:
            a = m.addAction(label); a.setEnabled(enabled); a.triggered.connect(cmd)
        m.exec(QtCore.QPoint(int(root[0]), int(root[1])))

    # Qt timer shims for the engine's coalesced repaint / typeahead timeouts.
    def after(self, ms, fn):
        t = QtCore.QTimer(self); t.setSingleShot(True); t.timeout.connect(fn); t.start(ms)
        return t

    def after_cancel(self, handle):
        handle.stop()          # QTimer.stop() is safe on an already-stopped timer

    def after_idle(self, fn):
        QtCore.QTimer.singleShot(0, fn)

    def closeEvent(self, e):
        self.engine.close()
        super().closeEvent(e)


def make_sheet(headers, rows, frozen_columns=0, view_only=False, master=None,
               col_w=None, title="fastpygrid (gpu-qt)", uncap_rows=False, uncap_cols=False,
               filters=True):
    """One-call sheet under a Qt host (OpenGL 1.1 backend). Creates a QApplication if
    none exists. Returns the window (.model, .grid_view, .mainloop()). Raises if the
    surface lib isn't built."""
    lib = _load_lib()
    if lib is None:
        raise RuntimeError(
            "OpenGL surface unavailable, build it with `python -m fastpygrid.core.gpu --build`.")
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])  # QApplication makes the process DPI-aware itself
    scale = _screen_scale(None)
    model = make_model(headers, rows, editable=not view_only)
    win = QtWidgets.QWidget(master)
    win.setWindowTitle(title)
    dpr = win.devicePixelRatioF() or 1.0  # resize() is logical; Qt already applies dpr when scaling
    win.resize(round(980 * scale / dpr), round(620 * scale / dpr))
    grid = GpuQtGrid(win, model, editable=not view_only, frozen=frozen_columns,
                     col_w=col_w, scale=scale, lib=lib,
                     uncap_rows=uncap_rows, uncap_cols=uncap_cols, filters=filters)
    lay = QtWidgets.QVBoxLayout(win)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(grid)
    win.model, win.grid_view = model, grid
    win.mainloop = app.exec                          # parity with the Tk demo
    win.show()
    return win
