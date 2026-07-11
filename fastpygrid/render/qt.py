"""Qt (PySide6) host for the toolkit-neutral GpuEngine (fastpygrid.core.gpu).

Same shape as tk.py: the engine owns all rendering, overlays, scrollbars and
input LOGIC and imports NO GUI toolkit. This file is the thin Qt adapter -- it
owns the window, a native surface widget (the Gpu child HWND parents to its
winId), fonts, event translation, clipboard and context menu, and implements the
~dozen host-adapter methods the engine calls. The engine is reused UNCHANGED.

All chrome is custom Gpu-drawn, so the widget IS the surface -- no sibling Qt
widgets. Windows-only (Direct2D); raises if the DLL/GPU surface can't build.
"""
import os

# The engine works in PHYSICAL pixels (HWND is physical, Gpu RT forced to 96 DPI),
# so Qt must not scale -- else it reports logical px and the surface renders into a
# 1/dpr corner with every mouse coord offset. AA_DisableHighDpiScaling is a no-op in
# Qt6; these env vars are the real switch and must be set before QApplication.
# ponytail: assumes dpr==1. Embedding in a QApplication that forces scaling => coords
# offset; set QT_ENABLE_HIGHDPI_SCALING=0 in that app's env too.
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")
os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "0")

from PySide6 import QtCore, QtGui, QtWidgets

from ..core import theme as T
from ..core.coremodel import make_model
from ..core.gpu import GpuEngine, _load_lib, _enable_dpi_awareness, _screen_scale

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
    """Tk-style keysym for a QKeyEvent: named specials, else the letter (so the
    engine's Ctrl+A/C/V/X and single-char checks work), else the char itself."""
    k = e.key()
    if k in _KEYS:
        return _KEYS[k]
    if QtCore.Qt.Key_A <= k <= QtCore.Qt.Key_Z:
        return chr(k)                      # "A".."Z"; engine lowercases where needed
    t = e.text()
    return t if t else ""


class GpuQtGrid(QtWidgets.QWidget):
    """Thin Qt host for GpuEngine. A native, self-painted surface widget: the Gpu
    child HWND parents to winId() and does all drawing; Qt just forwards events."""

    def __init__(self, parent, model, editable=True, frozen=0, col_w=None, scale=1.0, lib=None,
                 uncap_rows=False, uncap_cols=False):
        super().__init__(parent)
        # Native, self-painted: Qt won't draw over the Gpu child, and winId() is a real HWND.
        self.setAttribute(QtCore.Qt.WA_NativeWindow, True)
        self.setAttribute(QtCore.Qt.WA_PaintOnScreen, True)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WA_OpaquePaintEvent, True)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setMouseTracking(True)                 # deliver hover moves (no button held)

        self._fpx = max(9, round(13 * scale))
        self.font = QtGui.QFont("Segoe UI"); self.font.setPixelSize(self._fpx)
        self.hfont = QtGui.QFont("Segoe UI"); self.hfont.setPixelSize(self._fpx)
        self.hfont.setBold(True)
        self._fm = {False: QtGui.QFontMetrics(self.font), True: QtGui.QFontMetrics(self.hfont)}

        self.model = model
        self.engine = GpuEngine(self, model, editable=editable, frozen=frozen,
                                col_w=col_w, scale=scale, lib=lib,
                                uncap_rows=uncap_rows, uncap_cols=uncap_cols)

    def paintEngine(self):
        return None                                 # foreign HWND owns the pixels

    # --- native events -> engine's normalized input ---
    @staticmethod
    def _mods(e):
        m = e.modifiers()
        return bool(m & QtCore.Qt.ControlModifier), bool(m & QtCore.Qt.ShiftModifier)

    def _root(self, e):
        g = e.globalPosition().toPoint()
        return (g.x(), g.y())

    def mousePressEvent(self, e):
        p = e.position()
        if e.button() == QtCore.Qt.RightButton:
            self.engine.context(int(p.x()), int(p.y()), self._root(e)); return
        if e.button() != QtCore.Qt.LeftButton:
            return
        ctrl, shift = self._mods(e)
        self.engine.press(int(p.x()), int(p.y()), ctrl, shift)

    def mouseMoveEvent(self, e):
        p = e.position()
        if e.buttons() & QtCore.Qt.LeftButton:
            self.engine.drag(int(p.x()), int(p.y()))
        else:
            self.engine.motion(int(p.x()), int(p.y()))

    def mouseReleaseEvent(self, e):
        if e.button() == QtCore.Qt.LeftButton:
            self.engine.release()

    def mouseDoubleClickEvent(self, e):
        if e.button() == QtCore.Qt.LeftButton:
            p = e.position()
            self.engine.double(int(p.x()), int(p.y()))

    def leaveEvent(self, e):
        self.engine.leave()

    def wheelEvent(self, e):
        dx, dy = e.angleDelta().x(), e.angleDelta().y()
        m = e.modifiers()
        if m & QtCore.Qt.ControlModifier:
            self.engine.zoom(1.1 if dy > 0 else 1 / 1.1)
        elif m & QtCore.Qt.ShiftModifier:
            self.engine._scroll_px(-(dy // 120) * 40)
        elif abs(dx) > abs(dy):                       # trackpad horizontal swipe
            self.engine._scroll_px(-(dx // 120) * 40)
        else:
            self.engine.wheel(dy // 120)

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
        s = e.size()
        self.engine.configure_size(s.width(), s.height())

    def showEvent(self, e):
        super().showEvent(e)
        self.engine.configure_size(self.width(), self.height())

    # --- host-adapter API the engine calls (mirrors render.tk.GpuGrid) ---
    def measure(self, text, bold=False):
        return self._fm[bool(bold)].horizontalAdvance(text)

    def size(self):
        return self.width(), self.height()

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
        self.font.setPixelSize(px); self.hfont.setPixelSize(px)
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
        for label, cmd, enabled in actions:
            a = m.addAction(label); a.setEnabled(enabled); a.triggered.connect(cmd)
        m.exec(QtCore.QPoint(int(root[0]), int(root[1])))

    # Qt timer shims for the engine's coalesced repaint / typeahead timeouts.
    def after(self, ms, fn):
        t = QtCore.QTimer(self); t.setSingleShot(True); t.timeout.connect(fn); t.start(ms)
        return t

    def after_cancel(self, handle):
        try:
            handle.stop()
        except Exception:
            pass

    def after_idle(self, fn):
        QtCore.QTimer.singleShot(0, fn)

    def closeEvent(self, e):
        self.engine.close()
        super().closeEvent(e)


def make_sheet(headers, rows, frozen_columns=0, view_only=False, master=None,
               col_w=None, title="fastpygrid (gpu-qt)", uncap_rows=False, uncap_cols=False):
    """One-call sheet, Direct2D renderer under a Qt host. Creates a QApplication if
    none exists. Returns the window (with .model, .grid_view, .mainloop()). Raises
    if the GPU surface can't be built (DLL missing / no D3D device)."""
    lib = _load_lib()
    if lib is None:
        raise RuntimeError(
            "Gpu surface unavailable -- build it with "
            "`python -m fastpygrid.core.gpu --build`.")
    app = QtWidgets.QApplication.instance()
    if app is None:
        _enable_dpi_awareness()          # process DPI-aware; Qt scaling off via env (top of file)
        app = QtWidgets.QApplication([])
    scale = _screen_scale(None)
    model = make_model(headers, rows, editable=not view_only)
    win = QtWidgets.QWidget(master)
    win.setWindowTitle(title)
    win.resize(round(980 * scale), round(620 * scale))
    grid = GpuQtGrid(win, model, editable=not view_only, frozen=frozen_columns,
                     col_w=col_w, scale=scale, lib=lib,
                     uncap_rows=uncap_rows, uncap_cols=uncap_cols)
    lay = QtWidgets.QVBoxLayout(win)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(grid)
    win.model, win.grid_view = model, grid
    win.mainloop = app.exec                          # parity with the Tk demo
    win.show()
    return win
