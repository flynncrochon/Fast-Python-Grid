"""OpenGL/GPU renderer engine, toolkit-neutral.

Blits the core display list (paint()) onto an OpenGL 1.1 surface driven by a
C-ABI DLL (../csrc/glsurface.cpp) via ctypes.

Provides:
  * GpuEngine: owns model/geometry/controller, the surface, and all rendering,
    overlays (editor/dropdown/filter/find), scrollbars and input logic. Talks to
    a `host` adapter for toolkit bits, so Tk/Qt hosts are thin wrappers.
  * GpuCanvas: core.render.blit backend packing primitives into the wire buffer
    glsurface.cpp decodes.
  * TextField: custom text-input control (measures via a host callable).

Hosts: fastpygrid.render.tk / .qt; use their make_sheet() to launch.
Build: build-all.bat (compiles DLLs, copies package into dist\fastpygrid).
"""
import ctypes
import gc
import math
import os
import struct
import subprocess
import sys
import time

from . import theme as T
from .geometry import Geometry
from .gridcontroller import GridController
from .filter import FilterController
from .find import FindController
from .paint import (paint, edit_colors, DisplayList, _prelude, _chrome, _dropdowns,
                    SEL_WASH_A)
from .render import blit, blit_fast

# Overlay chrome colors, decoupled from the cell palette.
_UI_BG = "#ece7dd"          # find bar
# filter popup: dark panel, light text
_PANEL_BG = "#2b2a26"
_PANEL_FG = "#e8e5dc"
_PANEL_SUB = "#3a3934"      # input field / row backing
_PANEL_HI = "#4a463d"       # hovered row / button
# scrollbars (track + thumb)
_SB_TRACK = "#e3e0d6"
_SB_THUMB = "#b7b1a3"
_SB_TRACK_DK = "#232220"    # on the dark filter panel
_SB_THUMB_DK = "#5c574d"
_SB_THUMB_HOVER = T.ACCENT  # thumb brightens to the accent on hover/drag
_SB_ARROW_HOVER = "#9a968c"  # end arrows lighten on hover (accent while held)


def _sb_metrics(track_start, track_len, content, view, offset):
    """(thumb_start, thumb_len) for a scrollbar, or None if not scrollable."""
    if content <= view or track_len <= 4:
        return None
    tlen = min(track_len, max(24, int(track_len * view / content)))
    denom = max(1, content - view)
    tstart = track_start + int((track_len - tlen) * max(0, min(offset, denom)) / denom)
    return tstart, tlen


def _sb_offset(pos, grab, track_start, track_len, tlen, content, view):
    """New scroll offset from a thumb drag. grab = (press_pos - thumb_start)."""
    span = max(1, track_len - tlen)
    return int(round((pos - grab - track_start) / span * (content - view)))

_DLL_DIR = os.path.dirname(__file__)          # .dll/.so installs beside this file in core/


def _lib_path():
    ext = ".dll" if sys.platform == "win32" else ".so"
    return os.path.join(_DLL_DIR, "glsurface" + ext)


# Host width-measurement font. Must match what the surface rasterizes so
# ellipsis-trim/centering line up: Win GDI = Segoe UI, Linux FreeType = DejaVu Sans.
UI_FONT = "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"


def _load_lib():
    """Load the glsurface backend, or None if not built / can't load. make_sheet()
    raises a build hint on None."""
    # Vsync OFF by default: the vblank wait was idle time capping fps at refresh
    # rate. Backend reads this env once at context creation, before first gpu_attach.
    # FASTPYGRID_VSYNC=1 forces it back on.
    os.environ.setdefault("FASTPYGRID_VSYNC", "0")
    path = _lib_path()
    if not os.path.exists(path):
        return None
    try:
        lib = ctypes.CDLL(path)
    except OSError:
        return None
    P, I, C = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
    sig = {
        "gpu_probe_pixel": ([C, I, I, I, I, I], ctypes.c_uint32),
        "gpu_attach": ([P, I, I], P),
        "gpu_render": ([P, C, I, I, I], None),   # ..., clear_rgb, animating (zoom glide)
        "gpu_resize": ([P, I, I], None),
        "gpu_detach": ([P], None),
    }
    for name, (args, res) in sig.items():
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, res
    return lib


def build():
    """Build the dist (all .py + both DLLs) via build-all.bat. Dev-only: build-all.bat and
    the .cpp source aren't in the shipped package."""
    root = os.path.join(os.path.dirname(__file__), "..", "..", "..")
    bat = os.path.join(root, "build-all.bat")
    return subprocess.call([bat], cwd=root, shell=True) == 0


_COL_CACHE = {}
def _col(c):
    """'#rrggbb' -> 0xRRGGBB int, falsy -> -1 (no fill/outline). Memoized: ~6k
    calls/frame over few distinct colors."""
    if not c:
        return -1
    v = _COL_CACHE.get(c)
    if v is None:
        v = _COL_CACHE[c] = int(c[1:], 16)
    return v


def _box_union(a, b):
    """Union two [x0,y0,x1,y1] boxes (either may be None)."""
    if a is None:
        return b
    if b is None:
        return a
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def _enable_dpi_awareness():
    """Windows: make the process DPI-aware so the surface renders sharp, not
    bitmap-stretched on hi-DPI. No-op elsewhere.

    Only the Tk host calls this (before its root window) -- Tk doesn't opt in on
    its own. Qt gets it free: QApplication makes the process DPI-aware when created,
    and the Qt host converts logical<->physical per-event via devicePixelRatioF()."""
    try:
        import ctypes as _c
        try:
            _c.windll.shcore.SetProcessDpiAwareness(2)         # per-monitor v2
        except Exception:
            _c.windll.user32.SetProcessDPIAware()              # older Windows
    except Exception:
        pass


def _check_poly(ax, ay, bx, by, cx, cy, h):
    """Checkmark stroke (centerline a->b->c, half-thickness h) as a filled polygon
    with mitered joins (a two-line stroke leaves a notch at the bend)."""
    def nrm(dx, dy):
        L = math.hypot(dx, dy) or 1.0
        return -dy / L, dx / L                     # left normal, unit
    n1x, n1y = nrm(bx - ax, by - ay)
    n2x, n2y = nrm(cx - bx, cy - by)
    mx, my = n1x + n2x, n1y + n2y
    ml = math.hypot(mx, my) or 1.0
    mx, my = mx / ml, my / ml
    sc = h / max(0.35, mx * n1x + my * n1y)         # miter length, clamped vs spikes
    return [(ax + n1x * h, ay + n1y * h), (bx + mx * sc, by + my * sc),
            (cx + n2x * h, cy + n2y * h), (cx - n2x * h, cy - n2y * h),
            (bx - mx * sc, by - my * sc), (ax - n1x * h, ay - n1y * h)]


def _screen_scale(win):
    """DPI scale factor (1.0 at 96 DPI, 1.5 at 150%, …)."""
    try:
        import ctypes as _c
        return max(1.0, _c.windll.user32.GetDpiForSystem() / 96.0)   # Win10+
    except Exception:
        return 1.0


# Compiled wire packers (tag byte baked in as leading 'c' -> one pack emits
# tag+payload, no reparse/concat per primitive; ~4k packs/frame).
_PK_R = struct.Struct("<cffffiif").pack
_PK_T = struct.Struct("<cffffifBH").pack
_PK_X = struct.Struct("<cfffffifBH").pack
_PK_L = struct.Struct("<cffffif").pack
_PK_P = struct.Struct("<ciH").pack
_PK_FF = struct.Struct("<ff").pack


class GpuCanvas:
    """core.render.blit backend: buffers primitives into the packed wire format
    (glsurface.cpp header). Host ships bytes(canvas.buf) to the DLL once per frame,
    one native call, not one per primitive."""

    def __init__(self, fpx, scale=1.0):
        self.fpx = float(fpx)          # cell-text pixel size
        self.s = scale
        self.buf = bytearray()
        self._w1 = max(1, round(scale))   # 1px stroke in device px (for rect_fill)

    def rect(self, x, y, w, h, fill=None, outline=None, width=1):
        # whole-px stroke width -> consistent thickness on hi-DPI
        self.buf += _PK_R(b"R", x, y, w, h,
                          _col(fill), _col(outline), max(1, round(width * self.s)))

    def rect_fill(self, x, y, w, h, fill):
        # Fill-only rect, the hot path (~2900 cells/frame): skips the outline parse
        # and stroke-width round that `rect` redoes per call.
        self.buf += _PK_R(b"R", x, y, w, h, _col(fill), -1, self._w1)

    def _text(self, x, y, w, h, s, color, size, bold, center):
        u = s.encode("utf-16-le")
        flags = (1 if bold else 0) | (2 if center else 0)
        self.buf += _PK_T(b"T", x, y, w, h, _col(color), size, flags, len(u) // 2) + u

    def text(self, x, y, w, h, s, color, bold=False, center=False):
        self._text(x, y, w, h, s, color, self.fpx, bold, center)

    def text_scrolled(self, x, y, w, h, origin_x, s, color, bold=False):
        """Left-aligned text at an explicit origin_x, clipped to (x,y,w,h). Used by
        the custom text field for horizontal scroll (origin_x = x - xscroll)."""
        u = s.encode("utf-16-le")
        self.buf += _PK_X(b"X", x, y, w, h, origin_x, _col(color), self.fpx,
                          1 if bold else 0, len(u) // 2) + u

    def line(self, x1, y1, x2, y2, color, width):
        self.buf += _PK_L(b"L", x1, y1, x2, y2, _col(color), max(1, round(width * self.s)))

    def poly(self, points, color):
        self.buf += _PK_P(b"P", _col(color), len(points))
        for px, py in points:
            self.buf += _PK_FF(px, py)

    def barrier(self):
        """Layer break: GL batches cell fills ahead of text, so overlay widgets
        emitted after this force the batch out to stay on top."""
        self.buf += b"F"

    def glyph(self, cx, cy, s, color, px):
        # one char centered on (cx, cy) at its own px size
        self._text(cx - px, cy - px, 2 * px, 2 * px, s, color, float(px), False, True)

    def combo(self, x, y, w, h):
        """Drop button: box + chevron as rect+poly so the DLL only needs R/L/P/T."""
        sz = max(8, round(h) - 8)
        bx, by = x + w - sz - 3, y + (h - sz) / 2
        self.rect(bx, by, sz, sz, outline=T.BTN_BORDER)
        cx, cy = bx + sz / 2, by + sz / 2
        r = max(2.0, sz * 0.26)
        self.poly([(cx - r, cy - r * 0.5), (cx + r, cy - r * 0.5), (cx, cy + r * 0.7)],
                  T.ARROW_IDLE)


class TextField:
    """Single-line text input drawn with GpuCanvas primitives (no Tk widget). Owner
    feeds key/mouse events and calls draw() each frame. Owns text, caret, selection,
    horizontal scroll, clipboard. Widths via a host `measure` callable."""

    def __init__(self, measure, text="", clipboard=None):
        self.measure = measure                   # (text, bold) -> px width
        self.clip = clipboard                    # (get, set) callables, or None
        self.bold = False                        # match edited cell weight (caret/scroll widths)
        self.focused = True
        self.set_text(text)

    def set_text(self, s):
        self.text = s
        self.caret = self.anchor = len(s)
        self.xscroll = 0

    def _w(self, i):                             # px width of text[:i]
        return self.measure(self.text[:i], self.bold) if i else 0

    def _index_at(self, px):                     # caret index nearest text-space pixel px
        best, bestd = 0, 1e18
        for i in range(len(self.text) + 1):
            d = abs(self._w(i) - px)
            if d < bestd:
                best, bestd = i, d
            elif d > bestd:
                break                            # width monotonic in i -> past the min
        return best

    def _sel(self):
        return min(self.caret, self.anchor), max(self.caret, self.anchor)

    def _del_sel(self):
        a, b = self._sel()
        if a != b:
            self.text = self.text[:a] + self.text[b:]
            self.caret = self.anchor = a
            return True
        return False

    def insert(self, s):
        self._del_sel()
        self.text = self.text[:self.caret] + s + self.text[self.caret:]
        self.caret += len(s)
        self.anchor = self.caret

    def handle_key(self, keysym, char, shift, ctrl):
        """Mutate state. Return 'enter'/'esc'/'tab'/'shift-tab' for the owner, else None."""
        k = keysym
        if k in ("Return", "KP_Enter"):
            return "enter"
        if k == "Escape":
            return "esc"
        if k == "Tab":
            return "shift-tab" if shift else "tab"
        if ctrl and k in ("a", "A"):
            self.anchor, self.caret = 0, len(self.text); return None
        if ctrl and k in ("c", "C", "x", "X"):
            a, b = self._sel()
            if a != b and self.clip:
                self.clip[1](self.text[a:b])
            if k in ("x", "X"):
                self._del_sel()
            return None
        if ctrl and k in ("v", "V"):
            if self.clip:
                self.insert((self.clip[0]() or "").replace("\n", " ").replace("\r", ""))
            return None
        if k == "Left":
            self.caret = max(0, self.caret - 1)
        elif k == "Right":
            self.caret = min(len(self.text), self.caret + 1)
        elif k == "Home":
            self.caret = 0
        elif k == "End":
            self.caret = len(self.text)
        elif k == "BackSpace":
            if not self._del_sel() and self.caret > 0:
                self.text = self.text[:self.caret - 1] + self.text[self.caret:]
                self.caret -= 1
            self.anchor = self.caret; return None
        elif k == "Delete":
            if not self._del_sel() and self.caret < len(self.text):
                self.text = self.text[:self.caret] + self.text[self.caret + 1:]
            self.anchor = self.caret; return None
        elif char and char.isprintable() and not ctrl:
            self.insert(char); return None
        else:
            return None
        if not shift:                            # bare movement collapses the selection
            self.anchor = self.caret
        return None

    def click(self, px_rel, shift=False):
        """Position the caret from a click. px_rel = pixels from the text's left edge."""
        i = self._index_at(px_rel + self.xscroll)
        self.caret = i
        if not shift:
            self.anchor = i

    def select_word(self, px_rel):
        """Select the word (or whitespace run) under a click (double-click behavior)."""
        t = self.text
        if not t:
            self.caret = self.anchor = 0
            return
        i = min(self._index_at(px_rel + self.xscroll), len(t) - 1)
        isw = lambda ch: ch.isalnum() or ch == "_"
        cls = isw(t[i])
        a, b = i, i + 1
        while a > 0 and isw(t[a - 1]) == cls:
            a -= 1
        while b < len(t) and isw(t[b]) == cls:
            b += 1
        self.anchor, self.caret = a, b

    def draw(self, cv, x, y, w, h, bg, fg, accent, border=True, bold=False):
        pad = cv.fpx * 5 / 13                     # match cell-text left pad (glsurface.cpp 'T' op)
        avail = max(1, w - 2 * pad)
        cx = self._w(self.caret)                 # keep the caret in view
        if cx - self.xscroll > avail:
            self.xscroll = cx - avail
        if cx - self.xscroll < 0:
            self.xscroll = cx
        self.xscroll = max(0, self.xscroll)
        cv.rect(x, y, w, h, fill=bg, outline=accent if border else None)
        a, b = self._sel()
        if a != b and self.focused:              # selection highlight behind the text
            sx0 = max(x + pad, x + pad + self._w(a) - self.xscroll)
            sx1 = min(x + w - pad, x + pad + self._w(b) - self.xscroll)
            if sx1 > sx0:
                cv.rect(sx0, y + 2, sx1 - sx0, h - 4, fill=T.EDIT_SEL)
        if self.text:
            cv.text_scrolled(x + pad, y, avail, h, x + pad - self.xscroll, self.text, fg, bold)
        if self.focused:                         # caret
            cxp = x + pad + cx - self.xscroll
            cv.line(cxp, y + 3, cxp, y + h - 3, fg, 1)


class GridFacade:
    """Public convenience API mixed into the host widgets (Qt/Tk) so callers use
    grid.reset_view()/select()/open_find()/scroll_to()/export()/subscribe() instead
    of reaching into engine/ctl/geom internals. Requires self.engine + self.model,
    set by the host __init__."""
    def reset_view(self):
        """Zoom to 1.0 and scroll to the top-left origin."""
        self.engine.reset_view()

    def select(self, r, c):
        """Select grid cell (r, c) (header rows included), scrolling it into view."""
        self.engine.select(r, c)

    def scroll_to(self, r, c):
        """Scroll grid cell (r, c) into view without changing the selection."""
        self.engine.scroll_to(r, c)

    def open_find(self):
        """Open the find bar (same as Ctrl+F)."""
        self.engine.reveal_find()

    def export(self):
        """(headers, rows) in source order — stable read-back for autosave/export."""
        return self.model.export()

    def subscribe(self, fn):
        """Register fn() to fire on any change; returns unsubscribe(). Multiple
        listeners allowed, unlike assigning model.changed."""
        return self.model.subscribe(fn)


class GpuEngine:
    """Toolkit-neutral grid engine: owns model/geometry/controller, the surface, and
    all rendering, overlays (editor/dropdown/filter/find), scrollbar and input logic.
    Talks to a `host` adapter for toolkit bits, so Tk/Qt hosts are thin wrappers.

    Host adapter contract (see TkHost):
        host.measure(text, bold=False) -> int      text width in px
        host.after(ms, fn) -> handle, host.after_cancel(handle), host.after_idle(fn)
        host.clip_get() -> str, host.clip_set(text)
        host.focus(), host.set_cursor(on_edge), host.fullscreen_toggle()
        host.size() -> (w, h), host.hwnd() -> int
        host.set_zoom_px(px), host.context_menu(root_xy, editable, actions)
    Also the GridController's host surface. Input arrives normalized:
    press/motion/drag/release/wheel/key/configure/context."""

    def __init__(self, host, model, editable=True, frozen=0, col_w=None, scale=1.0, lib=None,
                 uncap_rows=False, uncap_cols=False, filters=True):
        self.host = host
        self.model = model
        self.editable = editable
        self._lib = lib          # make_sheet/make_qt load it and raise before here
        s = self._scale = scale
        self._base_fpx = 13 * s
        self._fpx = max(9.0, self._base_fpx)   # float: exact GPU text-op size
        widths = [max(24, round(w * s)) for w in (col_w or [120] * model.ncols)]
        self.geom = Geometry(widths, frozen, gutter_w=max(28, round(56 * s)),
                             row_h=max(14, round(22 * s)), hdr_rows=model.header_rows,
                             uncap_rows=uncap_rows, uncap_cols=uncap_cols, filters=filters)
        self.ctl = GridController(self, base_row_h=22 * s, base_gutter=56 * s, base_w=widths)
        self._measure = lambda t, b=False: self.host.measure(t, b)   # for TextField
        self._surf = None
        self._editor = None          # open in-cell editor state, or None
        self._dbl = (0.0, 0, 0)      # (time,x,y) of last word-select, for triple detect
        self._dropdown = None        # open dropdown state, or None
        self._find = None            # open find bar state, or None
        self._filter = None          # open filter popup state, or None
        self._sbw = max(12, round(14 * s))            # scrollbar thickness
        self._vsb = {"hover": False, "drag": False, "grab": 0, "thumb": None, "arrow_hover": None}
        self._hsb = {"hover": False, "drag": False, "grab": 0, "thumb": None, "arrow_hover": None}
        self._ptr = None             # last pointer (x,y): re-check hover when thumb slides under
        self._arrow = False          # pointer over a ▼ button (hand cursor)
        self._next = None
        self._paint_pending = False
        self._in_drag = False        # true during a drag gesture
        self._autoscroll_after = None   # edge-autoscroll timer handle (selection drag)
        self._autoscroll_acc = [0.0, 0.0]   # fractional px (v,h) carried between ticks
        self._autoscroll_t = 0.0        # perf_counter of last tick (real-dt velocity)
        self._drag_xy = None            # last grid-drag pointer pos (for the timer)
        # inertial wheel scroll: wheel adds to a float px target, animation eases
        # live pos toward it per frame. See _scroll_smooth / _scroll_anim_tick.
        self._scroll_after = None       # animation timer handle, or None when idle
        self._scroll_pos = None         # [x, y] float px the animation eases from
        self._scroll_to = None          # [x, y] float px target
        self._scroll_last = None        # (scroll_x, scroll_y) last written (detect external scroll)
        self._scroll_t = 0.0            # perf_counter of last wheel-scroll ease tick (real-dt)
        # track-click paging: hold on the track, view glides toward the pointer
        # (Excel-style), stops when the thumb reaches it. See _start_sbpage.
        self._sbpage_after = None       # repeat timer handle, or None when idle
        self._sbpage = None             # (axis, track_pos) currently being paged toward
        self._sbpage_pos = None         # float scroll pos for the page glide (sub-pixel)
        # smooth zoom: Ctrl+wheel multiplies a float target, animation eases live
        # zoom toward it (mirrors the scroll glide).
        self._zoom_after = None         # zoom animation timer handle, or None when idle
        self._zoom_to = None            # target zoom factor
        self._zoom_t = 0.0              # perf_counter of last zoom-ease tick (real-dt)
        self._zoomfps = os.environ.get("FASTPYGRID_ZOOMFPS") == "1"   # print real glide fps
        self._zoom_frames = []          # (ts, build_ms, upload_ms) for the current glide (fps probe)
        model.subscribe(lambda: self._coalesce(self.redraw))   # first listener; hosts add more

    # --- surface lifecycle (lazy: device created on first Configure, after the
    # window maps, so make_sheet() stays instant) ---
    def _attach(self):
        if self._surf is not None:
            return
        w, h = self.host.size()
        if w < 2 or h < 2:
            return
        self._surf = self._lib.gpu_attach(self.host.hwnd(), w, h) or None
        # The GL child is created hidden; gpu_render reveals it on the first present. Render
        # that frame synchronously here so the window's first visible pixels are the grid,
        # never a white flash. (WM_PAINT is a no-op; we rely on the present persisting via DWM.)
        if self._surf is not None:
            self.redraw()

    def redraw(self):
        # Drag fires mouse-move faster than paint+blit+upload completes, and the zoom
        # ease timer ticks ~165 Hz; both ask for a repaint per event/tick. Coalesce to
        # one render per idle so renders can't backlog. Zoom level advances on the
        # timer independent of paint, so a dropped frame just renders the latest level.
        if self._in_drag or self._zoom_to is not None:
            self._coalesce(self._paint_now); return
        self._paint_now()

    def paint_to(self, cv):
        """Draw one frame onto the wire-buffer canvas `cv`. False if the surface is too
        small. The single render sequence."""
        sw, sh = self.host.size()
        if sw < 2 or sh < 2:
            return False
        g = self.geom
        g.w, g.h = max(1, sw - self._sbw), max(1, sh - self._sbw)   # reserve scrollbar strips
        g.clamp(self.model.nrows())
        if not (isinstance(cv, GpuCanvas) and self._blit_fast(cv)):
            dl = paint(self.model, g, self.ctl.active, self.ctl.ranges(), self.ctl.corner_hover)
            blit(dl, cv)
        self._draw_scrollbars(cv, sw, sh)
        if self._editor or self._dropdown or self._filter or self._find:
            cv.barrier()                      # keep overlay fills above the batched grid (GL)
        if self._editor:                      # widgets drawn on top of the grid
            self._draw_editor(cv)
        if self._dropdown:
            self._draw_dropdown(cv)
        if self._filter:
            self._draw_filter(cv)
        if self._find:
            self._draw_find(cv)
        return True

    def _blit_fast(self, cv):
        """Native body path: the viewport data-cell loop runs in C++ (gc_paint_body),
        its wire bytes splice into the GpuCanvas buffer; only chrome stays in Python.
        True if it built the frame; False (fallback) only with no C++ core. Styles and
        find highlight are resolved in C++, so this runs during a find too."""
        m, g = self.model, self.geom
        if not hasattr(m, "gc_paint_body"):
            return False
        C = _prelude(m, g, self.ctl.ranges())
        fz = g.frozen
        # Scrollable and frozen cols emit as separate body wires: frozen splices
        # behind a barrier so scrolled cells can't bleed text over the frozen band.
        scr = [c for c in C.cols if c >= fz]
        frz = [c for c in C.cols if c < fz]
        grs = list(C.data_rows)
        rowy = [g.row_y(gr) for gr in grs]
        sel = [v for rng in C.norm for v in rng]
        scr_wire, scr_box = self._body_wire(m, C, scr, grs, rowy, sel)
        frz_wire, frz_box = self._body_wire(m, C, frz, grs, rowy, sel)
        body_box = _box_union(scr_box, frz_box)
        chrome = DisplayList()
        _dropdowns(m, C, chrome.drops, chrome.frozen_drops)
        _chrome(m, C, self.ctl.active, self.ctl.corner_hover,
                chrome.frozen, chrome.chrome, chrome.overlays)
        # chrome.frozen = L1 Python cells (gutter + header/letter, joins frz_wire);
        # chrome.chrome = L2 pins. scr_wire = L0, frz_wire = frozen body (L1).
        blit_fast(chrome.chrome, chrome.frozen, scr_wire, frz_wire, body_box,
                  chrome.drops, chrome.frozen_drops, chrome.overlays, cv)
        return True

    def _body_wire(self, m, C, cols, grs, rowy, sel):
        """(wire_bytes, box) for one column group's body cells via the C++ emitter, or
        (b"", None) when the group is empty. box = [x0,y0,x1,y1] the group covers."""
        if not cols or not grs:
            return b"", None
        g = self.geom
        colx = [g.col_x(c) for c in cols]
        colw = [g.col_width(c) for c in cols]
        # Styles live in the core (read per-cell by gc_paint_body); the selection wash over
        # a styled bg is blended there from sel_tint/SEL_WASH_A. Find highlight is native
        # too, so no per-cell Python work here at all.
        wire = m.gc_paint_body(cols, colx, colw, grs, rowy, g.row_h, C.H, self._fpx,
                               max(1, round(self._scale)), sel, C.single_cell,
                               _col(T.TXT), _col(T.ZEBRA), _col(T.BG),
                               _col(C.wash_even), _col(C.wash_odd),
                               _col(T.SEL_TINT), SEL_WASH_A,
                               _col(T.FIND_MATCH), _col(T.FIND_ACTIVE))
        box = [min(colx), min(rowy),
               max(colx[i] + colw[i] for i in range(len(colx))), max(rowy) + g.row_h]
        return wire, box

    def _paint_now(self):
        if self._surf is None:
            self._attach()
            if self._surf is None:
                return
        cv = GpuCanvas(self._fpx, self._scale)
        anim = 1 if self._zoom_to is not None else 0          # zoom glide: renderer scales cached glyphs
        if self._zoomfps and self._zoom_to is not None:       # FASTPYGRID_ZOOMFPS=1: split build vs upload
            t0 = time.perf_counter()
            ok = self.paint_to(cv)
            t1 = time.perf_counter()
            if ok:
                self._lib.gpu_render(self._surf, bytes(cv.buf), len(cv.buf), _col(T.LETTER_BG), anim)
                t2 = time.perf_counter()
                self._zoom_frames.append((t2, (t1 - t0) * 1000, (t2 - t1) * 1000))
            return
        if self.paint_to(cv):
            self._lib.gpu_render(self._surf, bytes(cv.buf), len(cv.buf), _col(T.LETTER_BG), anim)

    # --- GridController host surface (delegates toolkit bits to self.host) ---
    def after_scroll_change(self):   self._coalesce(self.redraw)
    def after_geometry_change(self): self._coalesce(self.redraw)
    def measure(self, text, bold):   return self.host.measure(text, bold)
    def set_edge_cursor(self, on):   # ctl.on_motion drives this, hand wins over default
        self._set_cursor("resize" if on else ("hand" if self._arrow else ""))

    def _set_cursor(self, kind):     # dedup to avoid re-setting every motion (flicker)
        if kind != getattr(self, "_cursor", None):
            self._cursor = kind
            self.host.set_cursor(kind)

    def _over_arrow(self, x, y):
        """True when (x, y) is over a clickable ▼: a header filter button or a
        choice cell's dropdown button (hand cursor)."""
        g, m = self.geom, self.model
        region, row, col = g.hit(x, y, m.nrows(), m.ncols)
        if region != "cell" or col is None:
            return False
        if row == g.hdr_rows - 1 and g.filter_btn_hit(x, y, col):   # header filter ▼
            return True
        return bool(self.editable and row is not None and row >= g.hdr_rows
                    and m.cell_choices(row, col) is not None
                    and g.dropdown_btn_hit(x, y, row, col))         # data-cell dropdown ▼
    def clipboard_get(self):
        # HTML <table> clipboard flavor -> flatten to TSV so both models paste it
        # with their existing tab/newline split.
        html = getattr(self.host, "clip_get_html", lambda: "")()   # table flavor, if any
        if html:
            from .model import _parse_html_table
            rows = _parse_html_table(html)
            if rows:
                flat = lambda s: s.replace("\t", " ").replace("\r", " ").replace("\n", " ")
                return "\n".join("\t".join(flat(c) for c in r) for r in rows)
        return self.host.clip_get()
    def clipboard_set(self, text):   self.host.clip_set(text)
    def open_filter_popup(self, col):
        self._open_filter(col)

    # --- find bar (top-right, no Tk widget). Keys route to the query field:
    # Enter=next, Shift+Enter=prev, Esc/click-off closes. Match highlights:
    # FindController -> model find-state -> paint(). ---
    # --- public convenience API (surfaced on the host widget via GridFacade) ---
    def reset_view(self):
        """Zoom to 1.0 and scroll back to the top-left origin."""
        self.ctl.zoom_to(1.0)
        self.geom.scroll_x = self.geom.scroll_y = 0
        self.geom.clamp(self.model.nrows())
        self.redraw()

    def select(self, r, c):
        """Select grid cell (r, c) (clamped) and scroll it into view."""
        self.ctl.select(r, c)

    def scroll_to(self, r, c):
        """Scroll grid cell (r, c) into view without changing the selection."""
        self.ctl.scroll_into_view(r, c)

    def reveal_find(self):
        if self._find is None:
            # base_measure (÷ zoom) so the caret matches the zoom-independent bar text
            base_measure = lambda t, b=False: round(self.host.measure(t, b) * self._base_fpx / max(1, self._fpx))
            tf = TextField(base_measure, "", clipboard=(self.host.clip_get, self.host.clip_set))
            f = self._find = {"tf": tf, "ctl": FindController(self.ctl), "count": "",
                              "case": False, "scope": False, "avail": False, "layout": None}
            f["ctl"].on_count = self._find_count
        f = self._find
        f["avail"] = f["ctl"].open(self.ctl.ranges())
        f["scope"] = f["avail"]
        self.host.focus()
        self.redraw()

    def _find_count(self, text):
        if self._find:
            self._find["count"] = text
            self.redraw()

    def _draw_find(self, cv):
        f, g, s = self._find, self.geom, self._scale
        # Size + text pinned to DPI scale s, not grid zoom, so the bar stays constant
        # while row_h/_fpx grow with Ctrl+wheel zoom (mirrors the filter popup).
        saved_fpx, cv.fpx = cv.fpx, max(9, round(13 * s))
        h = round(30 * s)
        pad, bw, cw, fw = round(6 * s), round(26 * s), round(64 * s), round(150 * s)
        w = pad + fw + pad + cw + 5 * bw + pad
        x, y = g.w - w - round(6 * s), round(4 * s)
        cv.rect(x, y, w, h, fill=_UI_BG, outline=T.ACCENT)
        fx, fy, fh = x + pad, y + round(4 * s), h - round(8 * s)
        f["tf"].focused = True
        f["tf"].draw(cv, fx, fy, fw, fh, "#fbf9f4", T.TXT, T.ACCENT)
        cx = fx + fw + pad
        cv.text(cx, y, cw, h, f["count"] or "", T.TXT)
        bx = cx + cw
        btns = {}
        for label, key in (("‹", "prev"), ("›", "next"), ("Aa", "case"), ("In", "scope"), ("✕", "close")):
            on = (key == "case" and f["case"]) or (key == "scope" and f["scope"])
            dim = key == "scope" and not f["avail"]
            cv.text(bx, y, bw, h, label, T.ACCENT if on else ("#999" if dim else T.TXT), center=True)
            btns[key] = (bx, y, bw, h)
            bx += bw
        f["layout"] = {"field": (fx, fy, fw, fh), "btns": btns, "bar": (x, y, w, h)}
        cv.fpx = saved_fpx                                   # restore for later overlays

    def _find_key(self, keysym, char, shift, ctrl):
        f = self._find
        if keysym == "Escape":
            self._close_find(); return
        if keysym in ("Return", "KP_Enter"):
            f["ctl"].step(-1 if shift else 1); return
        old = f["tf"].text
        f["tf"].handle_key(keysym, char, shift, ctrl)
        if f["tf"].text != old:
            f["ctl"].run(f["tf"].text, navigate=False)
        self.redraw()

    def _find_press(self, x, y):
        """Return True if the click hit the find bar (consumed)."""
        f = self._find
        lo = f["layout"]
        if not lo:
            return False
        bx, by, bw, bh = lo["bar"]
        if not (bx <= x <= bx + bw and by <= y <= by + bh):
            return False
        fx, fy, fw, fh = lo["field"]
        if fx <= x <= fx + fw:
            f["tf"].click(x - fx - 4); self.redraw(); return True
        for key, (rx, ry, rw, rh) in lo["btns"].items():
            if rx <= x <= rx + rw:
                self._find_btn(key); break
        return True

    def _find_btn(self, key):
        f = self._find
        if key == "prev":
            f["ctl"].step(-1)
        elif key == "next":
            f["ctl"].step(1)
        elif key == "case":
            f["case"] = not f["case"]; f["ctl"].set_case(f["case"]); self.redraw()
        elif key == "scope" and f["avail"]:
            f["scope"] = not f["scope"]; f["ctl"].set_scope(f["scope"]); self.redraw()
        elif key == "close":
            self._close_find()

    def _close_find(self):
        if self._find:
            self._find["ctl"].close()
            self._find = None
            self.host.focus()
            self.redraw()

    # --- filter popup (no Tk widget): sort buttons, search field, scrollable
    # checkbox list, OK/Cancel. Modal. Distinct scan deferred so opening is
    # instant on a huge column. ---
    def _open_filter(self, col):
        # Measure at base font size (÷ zoom) so the caret matches the zoom-independent
        # popup text; host.measure uses the zoomed font.
        base_measure = lambda t, b=False: round(self.host.measure(t, b) * self._base_fpx / max(1, self._fpx))
        tf = TextField(base_measure, "", clipboard=(self.host.clip_get, self.host.clip_set))
        self._filter = {"ctl": FilterController(self.model, col), "col": col, "tf": tf,
                        "top": 0, "loaded": False, "layout": None,
                        "flyout": None, "colors": {}, "flybounds": None, "flyrects": []}
        self.host.focus()
        self.host.after(1, self._filter_load)
        self.redraw()

    def _filter_load(self):
        if self._filter:
            f = self._filter
            f["ctl"].load()
            col = f["col"]
            f["colors"] = {w: self.model.distinct_colors(col, w) for w in ("bg", "fg")}
            self._filter["loaded"] = True
            self.redraw()

    def _draw_filter(self, cv):
        f, g, s = self._filter, self.geom, self._scale
        # Size pinned to DPI scale s, not grid zoom: the popup stays constant size
        # while row_h/_fpx/col_w grow with Ctrl+wheel zoom.
        rh, pad = max(14, round(22 * s)), round(5 * s)
        W = round(220 * s)
        saved_fpx, cv.fpx = cv.fpx, max(9, round(13 * s))   # popup text at base size
        x = max(pad, min(g.col_x(f["col"]), g.w - W - pad))
        y = g.header_h                                       # flush under the column's ▼ button
        ctl = f["ctl"]
        have_colors = bool(f["loaded"] and (f["colors"]["bg"] or f["colors"]["fg"]))
        rows = ctl.rows(f["tf"].text) if f["loaded"] else []
        items = (["\0all"] + list(rows)) if f["loaded"] else []
        capped_hint = f["loaded"] and ctl.capped and not f["tf"].text.strip()
        if capped_hint:
            items = items + ["\0capped"]  # trailing "too many" caption row
        avail = g.h - y - 2 * pad
        fixed = 7 * rh + 3 * pad          # sortA, sortZ, sortcolor, clearsort, clear, filtercolor, search
        nvis = max(1, min(len(items) or 1, (avail - fixed - rh - 2 * pad) // rh, 12))
        listh = nvis * rh
        H = fixed + listh + 2 * pad + rh                      # + OK/Cancel row
        cv.rect(x - 1, y - 1, W + 2, H + 2, fill="#000000")   # border
        cv.rect(x, y, W, H, fill=_PANEL_BG)                   # dark panel
        lay, cy = {}, y + pad

        def button(key, label, enabled=True, arrow=False):
            nonlocal cy
            hov = enabled and (f.get("hoverbtn") == key
                               or (arrow and f["flyout"] and key.startswith(f["flyout"])))  # lit while open
            cv.rect(x + pad, cy, W - 2 * pad, rh - 2, fill=_PANEL_HI if hov else _PANEL_SUB)
            cv.text(x + pad, cy, W - 2 * pad, rh, label, _PANEL_FG if enabled else "#7d786e")
            if arrow:                                          # submenu affordance
                cv.text(x + W - round(18 * s), cy, round(12 * s), rh,
                        "›", _PANEL_FG if enabled else "#7d786e", center=True)
            lay[key] = (x + pad, cy, W - 2 * pad, rh, enabled)
            cy += rh

        if self.model.is_column_numeric(f["col"]):
            button("sortA", "Sort Smallest → Largest")
            button("sortZ", "Sort Largest → Smallest")
        else:
            button("sortA", "Sort A → Z")
            button("sortZ", "Sort Z → A")
        button("sortcolor", "Sort by Color", have_colors, arrow=True)
        button("clearsort", "Clear Sort", self.model.has_sort(f["col"]))
        button("clear", "Clear Filter", self.model.has_filter(f["col"]))
        button("filtercolor", "Filter by Color", have_colors, arrow=True)
        f["tf"].focused = True                               # search field
        f["tf"].draw(cv, x + pad, cy + 1, W - 2 * pad, rh - 2, _PANEL_SUB, _PANEL_FG, T.ACCENT)
        lay["field"] = (x + pad, cy + 1, W - 2 * pad, rh - 2)
        cy += rh + pad
        # checklist
        n = len(items)
        f["top"] = max(0, min(f["top"], max(0, n - nvis)))
        sbw = round(10 * s) if n > nvis else 0
        lw = W - 2 * pad - sbw
        lay["list"] = (x + pad, cy, lw, rh, nvis, sbw)
        if not f["loaded"]:
            cv.text(x + pad, cy, lw, rh, "  loading…", "#8a8578")
        for i in range(nvis):
            ii = f["top"] + i
            if ii >= n:
                break
            v = items[ii]
            if v == "\0capped":                  # trailing caption row: no checkbox, not clickable
                ry = cy + i * rh
                cv.rect(x + pad, ry, lw, rh - 1, fill=_PANEL_BG)
                cv.text(x + pad, ry, lw, rh, "  Too many, type to search", "#8a8578")
                continue
            if v == "\0all":
                label, checked = "(Select all)", ctl.all_on(rows)
            else:
                label, checked = (v if v else "(blank)"), ctl.checked(v)
            ry = cy + i * rh
            hov = ii == f.get("hoverrow")
            cv.rect(x + pad, ry, lw, rh - 1, fill=_PANEL_HI if hov else _PANEL_BG)
            # checkbox as primitives (a ☑/☐ glyph font-fell-back to different sizes).
            # Box is constant, only the tick appears; label x fixed so it never shifts.
            bs = max(8, rh - round(9 * s))
            bx, by = x + pad + round(3 * s), ry + (rh - bs) // 2
            # checked: accent box + white tick; unchecked: dark box
            cv.rect(bx, by, bs, bs, fill=T.ACCENT if checked else _PANEL_SUB,
                    outline=T.ACCENT if checked else _PANEL_FG)
            if checked:                          # mitered tick as one filled poly
                cv.poly(_check_poly(bx + bs * 0.23, by + bs * 0.53,
                                    bx + bs * 0.42, by + bs * 0.71,
                                    bx + bs * 0.77, by + bs * 0.29, bs * 0.085), "#ffffff")
            tx = bx + bs + round(6 * s)
            cv.text(tx, ry, x + pad + lw - tx, rh, label, _PANEL_FG)
        lay["sbthumb"] = None
        if sbw:
            tx = x + pad + lw
            cv.rect(tx, cy, sbw, listh, fill=_SB_TRACK_DK)
            th = max(rh // 2, int(listh * nvis / n))
            ty = cy + int((listh - th) * f["top"] / max(1, n - nvis))
            hot = f.get("sbhover") or f.get("sbdrag")
            cv.rect(tx + 1, ty, sbw - 2, th, fill=_SB_THUMB_HOVER if hot else _SB_THUMB_DK)
            lay["sbthumb"] = (tx, ty, sbw, th)
        cy += listh + pad
        # OK / Cancel (brighten on hover)
        half = (W - 3 * pad) // 2
        hb = f.get("hoverbtn")
        cv.rect(x + pad, cy, half, rh, fill="#d0865c" if hb == "ok" else T.ACCENT)
        cv.text(x + pad, cy, half, rh, "OK", "#ffffff", center=True)
        lay["ok"] = (x + pad, cy, half, rh)
        cv.rect(x + 2 * pad + half, cy, half, rh, fill="#5a564b" if hb == "cancel" else _PANEL_HI)
        cv.text(x + 2 * pad + half, cy, half, rh, "Cancel", _PANEL_FG, center=True)
        lay["cancel"] = (x + 2 * pad + half, cy, half, rh)
        lay["items"] = items
        f["layout"] = lay
        if f["flyout"] and have_colors:
            self._draw_flyout(cv, x, y, W, H, rh, pad, s)
        else:
            f["flybounds"], f["flyrects"] = None, []
        cv.fpx = saved_fpx                                   # restore for later overlays

    def _draw_flyout(self, cv, x, y, W, H, rh, pad, s):
        """Submenu to the right of the popup: swatches grouped under
        'by Cell Color' / 'by Font Color', plus No Fill / Automatic (uncolored)."""
        f = self._filter
        kind = f["flyout"]                                    # 'sort' | 'filter'
        verb = "Sort" if kind == "sort" else "Filter"
        fw = round(140 * s)
        sections = []                                        # (header, which, [colors..., None])
        for which, name, empty in (("bg", "Cell", "No Fill"), ("fg", "Font", "Automatic")):
            cols = f["colors"][which]
            if cols:
                sections.append(("%s by %s Color" % (verb, name), which,
                                 list(cols) + [None], empty))
        nrows = sum(1 + len(items) for _h, _w, items, _e in sections)
        fh = nrows * rh + 2 * pad
        trig = f["layout"]["sortcolor" if kind == "sort" else "filtercolor"]
        ax = x + W                                           # touch the panel (no dead gap)
        if ax + fw > self.geom.w:                            # no room right -> flip left
            ax = x - fw
        ay = max(pad, min(trig[1], self.geom.h - fh - pad))
        cv.rect(ax - 1, ay - 1, fw + 2, fh + 2, fill="#000000")
        cv.rect(ax, ay, fw, fh, fill=_PANEL_BG)
        # hit region padded toward the panel so the cursor crosses the button->flyout
        # gap without the submenu blinking shut.
        f["flybounds"] = (ax - 2 * pad, ay - pad, fw + 4 * pad, fh + 2 * pad)
        active = (self.model._sort if kind == "sort"
                  else self.model._color_filters.get(f["col"]))
        sww = fw - round(40 * s)                              # swatch width, centered
        sxc = ax + (fw - sww) // 2
        hits, cy = [], ay + pad
        for header, which, items, empty in sections:
            cv.text(ax + pad, cy, fw - 2 * pad, rh, header, "#c9c4b8", bold=True)
            cy += rh
            for c in items:
                hov = f.get("hoverfly") == (which, c)
                cv.rect(ax + 2, cy, fw - 4, rh - 1, fill=_PANEL_HI if hov else _PANEL_BG)
                on = (active == (f["col"], True, which, c)) if kind == "sort" else (active == (which, c))
                if on:                                       # tick the current pick
                    bs = max(8, rh - round(9 * s))
                    ty = cy + (rh - bs) // 2
                    cv.poly(_check_poly(ax + pad + bs * 0.23, ty + bs * 0.53,
                                        ax + pad + bs * 0.42, ty + bs * 0.71,
                                        ax + pad + bs * 0.77, ty + bs * 0.29, bs * 0.085), T.ACCENT)
                if c is None:                                # 'No Fill' / 'Automatic', centered
                    cv.text(ax, cy, fw, rh, empty, _PANEL_FG, center=True)
                else:                                        # centered filled color chip
                    cv.rect(sxc, cy + round(4 * s), sww, rh - round(8 * s), fill=c, outline="#000000")
                hits.append(((ax + 2, cy, fw - 4, rh), which, c))
                cy += rh
        f["flyrects"] = hits

    def _apply_color(self, kind, which, color):
        f = self._filter
        if kind == "sort":
            self.model.set_color_sort(f["col"], which, color)
        else:
            self.model.set_color_filter(f["col"], which, color)
        self._close_filter()

    @staticmethod
    def _in(r, x, y):
        return r and r[0] <= x <= r[0] + r[2] and r[1] <= y <= r[1] + r[3]

    def _filter_press(self, x, y):
        f = self._filter
        lo = f["layout"]
        if not lo:
            return
        if f["flyout"]:                                      # submenu overlays everything
            for r, which, color in f["flyrects"]:
                if self._in(r, x, y):
                    self._apply_color(f["flyout"], which, color); return
            if self._in(f["flybounds"], x, y):
                return                                       # click on flyout chrome: ignore
        for key in ("sortA", "sortZ", "sortcolor", "clearsort", "clear", "filtercolor"):
            r = lo.get(key)
            if r and self._in(r, x, y):
                if r[4]:
                    self._filter_btn(key)
                return
        if self._in(lo["field"], x, y):
            f["tf"].click(x - lo["field"][0] - 4); self.redraw(); return
        if self._in(lo["ok"], x, y):
            self._filter_commit(); return
        if self._in(lo["cancel"], x, y):
            self._close_filter(); return
        lx, ly, lw, rh, nvis, sbw = lo["list"]
        thumb = lo.get("sbthumb")
        if sbw and lx + lw <= x <= lx + lw + sbw:            # scrollbar
            if self._in(thumb, x, y):                        # grab the thumb -> drag
                f["sbdrag"] = True
                f["sbgrab"] = y - thumb[1]
            elif thumb:                                      # hold -> glide toward the click
                self._start_fpage(y)
            return
        if lx <= x <= lx + lw and ly <= y < ly + nvis * rh:  # checklist row
            ii = f["top"] + int((y - ly) // rh)
            items = lo["items"]
            if 0 <= ii < len(items):
                rows = f["ctl"].rows(f["tf"].text)
                if items[ii] == "\0capped":
                    pass                             # caption row, ignore clicks
                elif items[ii] == "\0all":
                    f["ctl"].toggle_all(rows)
                else:
                    f["ctl"].toggle(items[ii])
                self.redraw()
            return
        self._close_filter()                                 # click outside the panel: cancel

    def _filter_hover(self, x, y):
        f = self._filter
        lo = f.get("layout")
        if not lo:
            return
        over = self._in(lo.get("sbthumb"), x, y)             # scrollbar-thumb hover
        btn = next((k for k in ("sortA", "sortZ", "sortcolor", "clearsort", "clear",
                                "filtercolor", "ok", "cancel")
                    if self._in(lo.get(k), x, y)), None)     # button hover
        # Color submenu: hovering a ▸ item opens/switches it; stays open over the
        # flyout, closes when the cursor moves onto any other section.
        fly, hoverfly = None, None
        if btn in ("sortcolor", "filtercolor") and lo[btn][4]:
            fly = "sort" if btn == "sortcolor" else "filter"
        elif f["flyout"] and btn is None and self._in(f.get("flybounds"), x, y):
            fly = f["flyout"]                                # keep the open one while hovering it
            hoverfly = next(((w, c) for r, w, c in f["flyrects"] if self._in(r, x, y)), None)
        row = None
        lx, ly, lw, rh, nvis, sbw = lo["list"]
        if lx <= x <= lx + lw and ly <= y < ly + nvis * rh:  # checklist-row hover
            row = f["top"] + int((y - ly) // rh)
        if (over != f.get("sbhover") or row != f.get("hoverrow") or btn != f.get("hoverbtn")
                or fly != f["flyout"] or hoverfly != f.get("hoverfly")):
            f["sbhover"], f["hoverrow"], f["hoverbtn"] = over, row, btn
            f["flyout"], f["hoverfly"] = fly, hoverfly
            self.redraw()

    def _filter_sbdrag(self, y):
        f, lo = self._filter, self._filter["layout"]
        lx, ly, lw, rh, nvis, sbw = lo["list"]
        n = len(lo["items"])
        th = lo["sbthumb"][3] if lo.get("sbthumb") else rh
        f["top"] = max(0, min(max(0, n - nvis),
                              _sb_offset(y, f.get("sbgrab", 0), ly, nvis * rh, th, n, nvis)))
        self.redraw()

    def _filter_btn(self, key):
        f = self._filter
        if key in ("sortcolor", "filtercolor"):  # open the color submenu
            f["flyout"] = "sort" if key == "sortcolor" else "filter"
            self.redraw()
        elif key == "sortA":
            self.model.set_sort(f["col"], True); self._close_filter()
        elif key == "sortZ":
            self.model.set_sort(f["col"], False); self._close_filter()
        elif key == "clearsort":
            self.model.clear_sort(); self._close_filter()
        elif key == "clear":
            self.model.clear_column_filter(f["col"]); self._close_filter()

    def _filter_scroll(self, dr):
        f = self._filter
        f["top"] = max(0, f["top"] + dr)
        self.redraw()

    def _filter_key(self, keysym, char, shift, ctrl):
        f = self._filter
        if keysym == "Escape":
            self._close_filter(); return
        if keysym in ("Return", "KP_Enter"):
            self._filter_commit(); return
        f["tf"].handle_key(keysym, char, shift, ctrl)
        f["top"] = 0                                         # new search -> back to top
        self.redraw()

    def _filter_commit(self):
        f = self._filter
        if f["loaded"]:
            f["ctl"].commit(f["tf"].text)
        self._close_filter()

    def _close_filter(self):
        self._filter = None
        self.host.focus()
        self.redraw()

    # --- in-cell editor: a TextField drawn on the surface (no Tk widget). ---
    def begin_edit(self, initial=None):
        if not self.editable:
            return
        self.commit_editor()
        r, c = self.ctl.active
        if self.model.col_readonly(c):
            return
        choices = self.model.cell_choices(r, c)
        if choices is not None and r >= self.geom.hdr_rows:  # dropdown cell (data rows only)
            self._open_dropdown(r, c, choices)
            return
        bg, fg = edit_colors(r, self.geom.hdr_rows)      # blend with the cell it edits
        st = self.model.cell_style(r, c)                 # keep the cell's own fg/bold while editing
        bold = r < self.geom.hdr_rows or bool(st and st.get("bold"))  # match bold header rows
        if st and st.get("fg"):
            fg = st["fg"]
        text = initial if initial is not None else self.model.cell(r, c)
        tf = TextField(self._measure, text, clipboard=(self.host.clip_get, self.host.clip_set))
        tf.bold = bold
        # caret at end (set_text default): open just begins editing, no select-all
        self._editor = {"tf": tf, "cell": (r, c), "bg": bg, "fg": fg, "bold": bold}
        self.host.focus()
        self.redraw()

    def _draw_editor(self, cv):
        ed = self._editor
        r, c = ed["cell"]
        g = self.geom
        if not g.cell_visible(r, c) or g.col_x(c) < g.gutter_w:
            return                                       # scrolled out of view
        ed["tf"].draw(cv, g.col_x(c), g.row_y(r), g.col_width(c) + 1, g.row_h_at(r) + 1,
                      ed["bg"], ed["fg"], T.ACCENT, bold=ed["bold"])

    def _editor_key(self, keysym, char, shift, ctrl):
        k = keysym
        if k == "Down":
            self.commit_editor(move=(1, 0)); return
        if k == "Up":
            self.commit_editor(move=(-1, 0)); return
        act = self._editor["tf"].handle_key(k, char, shift, ctrl)
        if act == "enter":
            self.commit_editor(move=(1, 0))
        elif act == "tab":
            self.commit_editor(move=(0, 1))
        elif act == "shift-tab":
            self.commit_editor(move=(0, -1))
        elif act == "esc":
            self._cancel_editor()
        else:
            self.redraw()

    def _editor_press(self, x, y):
        """Click while editing: reposition caret if inside the cell (return True), else
        commit and let the click select the new cell (return False)."""
        r, c = self._editor["cell"]
        g = self.geom
        x0, y0, w, h = g.col_x(c), g.row_y(r), g.col_width(c), g.row_h_at(r)
        if x0 <= x <= x0 + w and y0 <= y <= y0 + h:
            self._editor["tf"].click(x - x0 - self._edit_pad())
            self.redraw()
            return True
        self.commit_editor()
        return False

    _TRIPLE_MS = 0.5   # click within this of a word-select at same spot = triple

    def _is_triple(self, x, y):
        t, px, py = self._dbl
        return (time.monotonic() - t) < self._TRIPLE_MS and abs(x - px) < 4 and abs(y - py) < 4

    def _edit_pad(self):
        return self._fpx * 5 / 13                 # editor text left pad (matches TextField.draw)

    def _editor_hit(self, x, y):
        """Text-space x offset if (x,y) is inside the open editor cell, else None."""
        r, c = self._editor["cell"]
        g = self.geom
        x0, y0, w, h = g.col_x(c), g.row_y(r), g.col_width(c), g.row_h_at(r)
        return (x - x0 - self._edit_pad()) if (x0 <= x <= x0 + w and y0 <= y <= y0 + h) else None

    def _editor_double(self, x, y):
        """Double-click inside the open editor: select the word under the cursor."""
        px = self._editor_hit(x, y)
        if px is None:
            return False
        self._editor["tf"].select_word(px)
        self._dbl = (time.monotonic(), x, y)         # arm triple-click (3rd click = select all)
        self.redraw()
        return True

    def _editor_select_all(self, x, y):
        """Triple-click inside the open editor: select all text."""
        if self._editor_hit(x, y) is None:
            return False
        tf = self._editor["tf"]
        tf.anchor, tf.caret = 0, len(tf.text)
        self._dbl = (0.0, 0, 0)
        self.redraw()
        return True

    def triple(self, x, y):                          # Tk fires a native <Triple-Button-1>
        if self._editor:
            self._editor_select_all(x, y)

    # --- dropdown list drawn on the surface (no Tk widget), own scrollbar (wheel +
    # thumb drag). Opens below the cell, flips above near the bottom. ---
    def _open_dropdown(self, r, c, choices):
        opts = list(choices)
        cur = self.model.cell(r, c)
        sel = opts.index(cur) if cur in opts else 0
        longest = max((self._measure(o) for o in opts), default=0)  # once, not per-frame
        self._dropdown = {"r": r, "c": c, "opts": opts, "sel": sel, "longest": longest,
                          "top": 0, "type": "", "ta": None, "layout": None, "drag": None}
        self._dropdown_ensure_visible()
        self.host.focus()
        self.redraw()

    def _dropdown_geom(self):
        """(x, y, w, row_h, nvis, sbw) of the open list. Widens to the longest option
        (+ a scrollbar gutter when it overflows) and flips above the cell if needed."""
        d, g = self._dropdown, self.geom
        rh = g.row_h
        n = len(d["opts"])
        cell_y = g.row_y(d["r"])
        below, above = g.h - (cell_y + rh), cell_y
        nvis = min(n, max(1, max(below, above) // rh), 14)
        sbw = max(8, round(10 * self._scale)) if n > nvis else 0
        w = min(g.w, max(g.col_w[d["c"]], d["longest"] + 16) + sbw)
        x = max(0, min(g.col_x(d["c"]), g.w - w))
        y = cell_y + rh if below >= nvis * rh or below >= above else cell_y - nvis * rh
        return x, y, w, rh, nvis, sbw

    def _dropdown_ensure_visible(self):
        d = self._dropdown
        _x, _y, _w, _rh, nvis, _sb = self._dropdown_geom()
        if d["sel"] < d["top"]:
            d["top"] = d["sel"]
        elif d["sel"] >= d["top"] + nvis:
            d["top"] = d["sel"] - nvis + 1

    def _draw_dropdown(self, cv):
        d = self._dropdown
        x, y, w, rh, nvis, sbw = self._dropdown_geom()
        n = len(d["opts"])
        listh = nvis * rh
        d["top"] = max(0, min(d["top"], n - nvis))
        tw = w - sbw                                       # option-text column width
        cv.rect(x - 1, y - 1, w + 2, listh + 2, fill="#000000")       # border
        for i in range(nvis):
            oi = d["top"] + i
            if oi >= n:
                break
            sel = oi == d["sel"]                          # dark panel, matches filter popup
            cv.rect(x, y + i * rh, tw - 1, rh - 1, fill=T.ACCENT if sel else _PANEL_BG)
            cv.text(x, y + i * rh, tw, rh, d["opts"][oi], "#ffffff" if sel else _PANEL_FG)
        thumb = None
        if sbw:                                            # scrollbar track + thumb (dark)
            cv.rect(x + tw, y, sbw, listh, fill=_SB_TRACK_DK)
            th = max(rh // 2, int(listh * nvis / n))
            ty = y + int((listh - th) * d["top"] / max(1, n - nvis))
            hot = d.get("sbhover") or d.get("drag")
            cv.rect(x + tw + 1, ty, sbw - 2, th, fill=_SB_THUMB_HOVER if hot else _SB_THUMB_DK)
            thumb = (x + tw, ty, sbw, th)
        d["layout"] = {"x": x, "y": y, "w": w, "rh": rh, "nvis": nvis,
                       "tw": tw, "sbw": sbw, "listh": listh, "thumb": thumb}

    def _dropdown_hover(self, x, y):
        d = self._dropdown
        lo = d["layout"]
        if lo and lo["thumb"]:                             # scrollbar-thumb hover highlight
            over = self._in(lo["thumb"], x, y)
            if over != d.get("sbhover"):
                d["sbhover"] = over
                self.redraw()
            if over:
                return
        oi = self._dropdown_at(x, y)                       # else hover-highlight the option
        if oi is not None and oi != d["sel"]:
            d["sel"] = oi
            self.redraw()

    def _dropdown_move(self, delta):
        d = self._dropdown
        d["sel"] = max(0, min(len(d["opts"]) - 1, d["sel"] + delta))
        self._dropdown_ensure_visible()
        self.redraw()

    def _dropdown_scroll(self, dr):
        d = self._dropdown
        d["top"] = max(0, min(len(d["opts"]) - 1, d["top"] + dr))
        self.redraw()

    def _dropdown_typeahead(self, ch):
        d = self._dropdown
        d["type"] += ch.lower()
        s, low = d["type"], [o.lower() for o in d["opts"]]
        idx = next((i for i, o in enumerate(low) if o.startswith(s)), None)
        if idx is None:
            idx = next((i for i, o in enumerate(low) if s in o), None)
        if idx is not None:
            d["sel"] = idx
            self._dropdown_ensure_visible()
            self.redraw()
        if d["ta"]:
            self.host.after_cancel(d["ta"])
        d["ta"] = self.host.after(900, lambda: d.update(type="") if self._dropdown is d else None)

    def _dropdown_at(self, x, y):
        """Option index under (x, y) in the LIST column, or None if outside it."""
        lo = self._dropdown and self._dropdown["layout"]
        if not lo:
            return None
        if lo["x"] <= x <= lo["x"] + lo["tw"] and lo["y"] <= y < lo["y"] + lo["listh"]:
            oi = self._dropdown["top"] + int((y - lo["y"]) // lo["rh"])
            if 0 <= oi < len(self._dropdown["opts"]):
                return oi
        return None

    def _dropdown_press(self, x, y):
        """Route a click while the dropdown is open: scrollbar drag, pick, or close."""
        d, lo = self._dropdown, self._dropdown["layout"]
        if lo and lo["sbw"] and lo["x"] + lo["tw"] <= x <= lo["x"] + lo["w"]:
            d["drag"] = True                               # grabbed the scrollbar
            self._dropdown_drag(y)
            return
        oi = self._dropdown_at(x, y)
        if oi is not None:
            d["sel"] = oi
            self._dropdown_commit()
        else:
            self._close_dropdown()

    def _dropdown_drag(self, y):
        d, lo = self._dropdown, self._dropdown["layout"]
        n, nvis = len(d["opts"]), lo["nvis"]
        th = lo["thumb"][3] if lo["thumb"] else lo["rh"]
        frac = (y - lo["y"] - th / 2) / max(1, lo["listh"] - th)
        d["top"] = max(0, min(n - nvis, round(frac * (n - nvis))))
        self.redraw()

    def _dropdown_commit(self):
        d = self._dropdown
        r, c, val = d["r"], d["c"], d["opts"][d["sel"]]
        self._close_dropdown(redraw=False)
        self.model.set_cell(r, c, val)                   # repaints via model.changed

    def _close_dropdown(self, redraw=True):
        d = self._dropdown
        if d and d["ta"]:
            self.host.after_cancel(d["ta"])      # no-op on stale ids
        self._dropdown = None
        if redraw:
            self.redraw()

    def _dropdown_key(self, keysym, char):
        k = keysym
        if k in ("Down", "Right"):
            self._dropdown_move(1)
        elif k in ("Up", "Left"):
            self._dropdown_move(-1)
        elif k == "Next":
            self._dropdown_move(10)
        elif k == "Prior":
            self._dropdown_move(-10)
        elif k in ("Return", "KP_Enter", "Tab"):
            self._dropdown_commit()
        elif k == "Escape":
            self._close_dropdown()
        elif char and char.isprintable():
            self._dropdown_typeahead(char)

    def commit_editor(self, move=None):
        ed = self._editor
        if not ed:
            return
        r, c = ed["cell"]
        text = ed["tf"].text
        if text.strip():                 # typing past the last column grows the sheet
            self.ctl.ensure_col(c)       # (an empty commit adds nothing)
        self.model.set_cell(r, c, text)
        self._editor = None
        self.host.focus()                         # next keystroke edits again
        if move:
            self.ctl.active = self.ctl.anchor = (r, c)
            self.ctl.move(move)
        else:
            self.redraw()

    def _cancel_editor(self):
        if self._editor:
            self._editor = None
            self.host.focus()
            self.redraw()

    def set_zoom_fonts(self, z):
        # No 9px floor / int rounding: col_w/row_h scale linearly with z, so the GPU
        # font must too exactly or text ellipsis-trims. Keep _fpx float; only the
        # host fonts (measure/edit) need an int.
        self._fpx = max(1.0, self._base_fpx * z)
        self.host.set_zoom_px(max(1, round(self._fpx)))

    # --- normalized input: host translates native events into these calls; routing
    # (overlays first, then grid) is identical under any toolkit. ---
    def press(self, x, y, ctrl, shift):
        if self._dropdown:                             # scrollbar drag / pick / close
            self._dropdown_press(x, y); return
        if self._filter:                               # modal: click routes inside the popup
            self._filter_press(x, y); return
        if self._sb_press(x, y):                       # grid scrollbar drag / page
            return
        if self._editor and self._is_triple(x, y) and self._editor_select_all(x, y):
            return                                     # Qt: 3rd rapid click (no native triple event)
        if self._editor and self._editor_press(x, y):
            return                                     # caret repositioned inside the editor
        if self._find:
            if self._find_press(x, y):
                return
            self._close_find()                         # click off the bar closes find
        self.host.focus()
        self.ctl.on_press(x, y, ctrl, shift)

    def release(self):
        self._stop_autoscroll()
        self._stop_sbpage()
        if self._dropdown:
            self._dropdown["drag"] = None; return
        if self._filter and self._filter.get("sbdrag"):
            self._filter["sbdrag"] = False; return
        if self._vsb["drag"] or self._hsb["drag"]:
            self._vsb["drag"] = self._hsb["drag"] = False; return
        self.ctl.on_release()

    def motion(self, x, y):
        self._ptr = (x, y)
        if self._dropdown:                     # trigger ▼ left a hand cursor; popup is default
            self._set_cursor(""); self._dropdown_hover(x, y); return
        if self._filter:
            self._set_cursor(""); self._filter_hover(x, y); return
        if self._sb_hover(x, y):
            self._set_cursor("")                   # plain arrow over scrollbars, never resize/hand
            return
        if self._editor:                       # I-beam over the open editor's text box
            self._set_cursor("text" if self._editor_hit(x, y) is not None else "")
            return
        self._arrow = self._over_arrow(x, y)   # read by set_edge_cursor during on_motion
        self.ctl.on_motion(x, y)

    def drag(self, x, y):
        if self._dropdown:
            if self._dropdown.get("drag"):
                self._dropdown_drag(y)
            return
        if self._filter:
            if self._filter.get("sbdrag"):
                self._filter_sbdrag(y)
            elif self._sbpage and self._sbpage[0] == "fpage":   # drag along track re-aims the glide
                self._sbpage = ("fpage", None, y)
            return
        if self._sb_drag(x, y):
            return
        if self._editor:
            r, c = self._editor["cell"]
            self._editor["tf"].click(x - self.geom.col_x(c) - self._edit_pad(), shift=True)
            self.redraw(); return
        # At an edge the timer owns scrolling, so the event must not also scroll
        # (follow=False), else the two compound and race the pointer.
        at_edge = self.ctl.drag_region is not None and self._edge_scroll(x, y) != (0, 0)
        self._in_drag = True
        try:
            self.ctl.on_drag(x, y, follow=not at_edge)
        finally:
            self._in_drag = False
        self._drag_xy = (x, y)
        if at_edge:
            self._start_autoscroll()
        else:
            self._stop_autoscroll()

    # --- edge autoscroll during a selection drag: re-arms immediately so it paces on
    # the paint (vsync'd render = frame clock). Moves at a velocity (cells/sec) that
    # grows with how far past the edge the pointer is, using real dt (perf_counter) so
    # speed is frame-time independent and uncapped. A dt clamp stops a stall from
    # teleporting the view; fractional accumulation carries the sub-step remainder. ---
    _AUTOSCROLL_MS = 1               # re-arm ASAP, no fixed wait on top of the paint
    _AS_BASE = 16.0                  # cells/sec at the edge (gentle, precise selection)
    _AS_GAIN = 42.0                  # + cells/sec per row_h past the edge; no cap
    _AS_DT_CAP = 0.05               # s: clamp elapsed dt so a hitch can't fling the view

    def _edge_scroll(self, x, y):
        """Autoscroll velocity (rows/sec, px/sec) while the pointer is past a body edge,
        else (0.0, 0.0). Velocity not per-tick step, so speed is timer-rate independent."""
        g = self.geom
        rh = max(1, g.row_h)
        def vel(past):                                  # past = px beyond the body edge
            return self._AS_BASE + self._AS_GAIN * max(0.0, past) / rh
        vr = 0.0
        if y > g.h - rh:                                # bottom band / past the bottom
            vr = vel(y - g.h)
        elif y < g.header_h:                            # above the first data row
            vr = -vel(g.header_h - y)
        vpx = 0.0
        if x > g.w - rh:                                # right band / past the right edge
            vpx = vel(x - g.w) * rh                     # px/sec, same feel as vertical
        elif x < g.gutter_w:                            # over the gutter / past the left edge
            vpx = -vel(g.gutter_w - x) * rh
        return vr, vpx

    def _start_autoscroll(self):
        if self._autoscroll_after is None:
            self._autoscroll_acc = [0.0, 0.0]           # fresh remainder, no lurch on start
            self._autoscroll_t = time.perf_counter()    # dt baseline for the first tick
            self._autoscroll_after = self.host.after(self._AUTOSCROLL_MS, self._autoscroll_tick)

    def _stop_autoscroll(self):
        if self._autoscroll_after is not None:
            self.host.after_cancel(self._autoscroll_after)   # no-op on stale ids
            self._autoscroll_after = None

    def _autoscroll_tick(self):
        self._autoscroll_after = None
        if self.ctl.drag_region is None or self._drag_xy is None:
            return
        x, y = self._drag_xy
        vr, vpx = self._edge_scroll(x, y)
        if (vr, vpx) == (0.0, 0.0):                      # pointer back inside: stop
            return
        now = time.perf_counter()
        dt = min(self._AS_DT_CAP, now - self._autoscroll_t)   # real elapsed -> timer-independent speed
        self._autoscroll_t = now
        g = self.geom
        vpy = vr * g.row_h                              # rows/sec -> px/sec, glides like horizontal
        apy, apx = self._autoscroll_acc
        apy += vpy * dt; apx += vpx * dt
        dpy, dpx = int(apy), int(apx)                   # whole-px steps this tick; keep the remainder
        self._autoscroll_acc = [apy - dpy, apx - dpx]
        before = (g.scroll_y, g.scroll_x)
        if dpy:
            g.scroll_y = max(0, g.scroll_y + dpy)       # sub-row pixel scroll, no whole-row lurch
        if dpx:
            g.scroll_x = max(0, g.scroll_x + dpx)
        g.clamp(self.model.nrows())
        applied = (g.scroll_y, g.scroll_x) != before
        if (dpy or dpx) and not applied:                # clamped at the data edge: stop
            return
        if applied:
            self.ctl.on_drag(x, y, follow=False)         # extend selection into revealed cells
        self._autoscroll_after = self.host.after(self._AUTOSCROLL_MS, self._autoscroll_tick)

    def wheel(self, notches):                          # notches > 0 = scroll up (may be fractional)
        if self._dropdown or self._filter:
            step = int(notches) or (1 if notches > 0 else -1)   # discrete list: non-zero int
            (self._dropdown_scroll if self._dropdown else self._filter_scroll)(-step)
            return
        self._scroll_smooth(dy=-notches * self._WHEEL_ROWS * self.geom.row_h)

    def _overlay_open(self):
        return self._dropdown or self._filter or self._editor or self._find

    def double(self, x, y):
        if self._editor and self._editor_double(x, y):  # dbl-click in the open editor = select word
            return
        if self._overlay_open() or x >= self.geom.w or y >= self.geom.h:
            self.press(x, y, False, False); return     # modal / scrollbar: no cell-edit fall-through
        self.ctl.on_double(x, y)

    def leave(self):            self.ctl.set_corner_hover(False)
    def toggle_fullscreen(self): self.host.fullscreen_toggle()

    # --- smooth zoom (Ctrl+wheel) -----------------------------------------
    # Time-based ease-out, not a per-frame fraction: SwapBuffers is vsync-blocked so
    # the timer fires at refresh rate (~60 Hz), not _ZOOM_MS's 165 Hz; a per-frame
    # fraction would settle too slowly on a 60 Hz panel. Decaying by real dt makes the
    # glide a constant ~130 ms at any refresh.
    _ZOOM_TAU = 0.045        # ease time constant (s): ~95% of the gap closed in ~3*TAU
    _ZOOM_MS = 1             # timer period: tight so render cadence is vsync-bound, not timer-gated
    _ZOOM_SNAP = 0.004       # within this of target -> land exactly and stop

    def zoom(self, factor):
        # Accumulate onto the live target so a burst of notches glides to the final
        # level in one motion.
        base = self._zoom_to if self._zoom_to is not None else self.ctl._zoom
        self._zoom_to = max(0.4, min(4.0, base * factor))
        if self._zoom_after is None:
            # Glide frames allocate short-lived objects; a gen-0 collection mid-glide
            # is a ~10 ms stutter. Freeze GC for the glide, sweep once at settle.
            gc.disable()
            self._zoom_t = time.perf_counter()          # glide clock start (real-dt ease)
            self._zoom_frames = []                       # fps probe: fresh glide
            self._zoom_after = self.host.after(self._ZOOM_MS, self._zoom_anim_tick)

    def _zoom_anim_tick(self):
        self._zoom_after = None
        if self._zoom_to is None:
            return
        now = time.perf_counter()
        dt, self._zoom_t = now - self._zoom_t, now
        cur = self.ctl._zoom
        gap = self._zoom_to - cur
        if abs(gap) <= self._ZOOM_SNAP:
            self.ctl.zoom_to(self._zoom_to)
            self._zoom_to = None
            # Resume GC now, but defer the one-time sweep to idle: a synchronous
            # gc.collect() here lands a full gen-2 collection on the settle frame (the
            # same frame that re-rasters glyphs at exact px), a ~20ms hitch at the end
            # of every zoom. after_idle runs it once the crisp frame has presented.
            gc.enable()
            self.host.after_idle(gc.collect)
            if self._zoomfps and len(self._zoom_frames) > 1:
                ts = [f[0] for f in self._zoom_frames]
                span = ts[-1] - ts[0]
                n = len(self._zoom_frames)
                build = sum(f[1] for f in self._zoom_frames) / n
                upload = sum(f[2] for f in self._zoom_frames) / n
                print("zoom glide: %d frames in %.0f ms = %.0f fps  |  avg build %.2f ms  "
                      "gpu_render+vsync %.2f ms" %
                      (n, span * 1000, (n - 1) / span if span else 0, build, upload))
            return
        frac = 1.0 - math.exp(-dt / self._ZOOM_TAU)      # real-dt exponential decay (frame-rate independent)
        self.ctl.zoom_to(cur + gap * frac)
        self._zoom_after = self.host.after(self._ZOOM_MS, self._zoom_anim_tick)

    def key(self, keysym, char, shift, ctrl):
        """Return True if consumed (host should swallow it)."""
        if self._dropdown:
            self._dropdown_key(keysym, char); return True
        if self._filter:
            self._filter_key(keysym, char, shift, ctrl); return True
        if self._editor:
            self._editor_key(keysym, char, shift, ctrl); return True
        if self._find:
            self._find_key(keysym, char, shift, ctrl); return True
        k = {"BackSpace": "Delete", "KP_Enter": "Return"}.get(keysym, keysym)
        if len(k) == 1:
            k = k.lower()
        return bool(self.ctl.on_key(k, shift, ctrl, char))

    def context(self, x, y, root):
        if self._overlay_open():                       # modal: no grid menu behind overlay
            return
        self.host.focus()
        self.ctl.context_select(x, y)
        st = self.editable
        self.host.context_menu(root, [("Copy", self.ctl.copy, True),
                                      ("Cut", self.ctl.cut, st),
                                      ("Paste", self.ctl.paste, st),
                                      ("Delete", self.ctl.delete, st)])

    def configure_size(self, w, h):
        """Host reports its surface resized (or first-mapped)."""
        if w < 2 or h < 2:
            return
        if self._surf is None:
            self._attach()
        else:
            self._lib.gpu_resize(self._surf, w, h)
        self.geom.w, self.geom.h = w - self._sbw, h - self._sbw
        self.redraw()

    # --- smooth (inertial) wheel scroll -----------------------------------
    # A notch nudges the target a couple rows, easing eats a fraction of the gap per
    # ~90fps frame; target lead and per-frame move are capped so a fast spin can't
    # fling or lag.
    _WHEEL_ROWS = 2.0        # vertical rows per notch (px/notch = _WHEEL_ROWS * row_h,
                             # DPI/zoom-independent)
    _HWHEEL_ROWS = 4.0       # horizontal notch in row_h units: columns are far wider than
                             # row_h, so matching the vertical step felt sluggish. 4*22≈one col.
    _SCROLL_EASE = 0.30      # per-frame ease fraction (scrollbar trough-glide, _sbpage_tick)
    _SCROLL_MS = 11          # scrollbar trough-glide timer period (~90 Hz)
    _SCROLL_SNAP = 1.0       # px: within this of target -> land exactly and stop
    _SCROLL_MAX_FRAC = 0.22  # per-frame move cap, fraction of viewport (scrollbar trough-glide)
    _SCROLL_LEAD_FRAC = 1.1  # target may lead the live position by at most this * viewport
    # Wheel-scroll glide: real-dt ease (like zoom), so feel is cadence-independent and the
    # timer can run tight (vsync/refresh-bound) instead of capping fps at ~90 Hz.
    _SMOOTH_MS = 1           # wheel glide timer period: tight, render cadence-bound not timer-gated
    _SMOOTH_TAU = 0.05       # ease time constant (s): ~95% of the gap closed in ~3*TAU (~150 ms)
    _SMOOTH_MAX_VEL = 20.0   # top-speed cap: viewport-fractions/sec (dt-scaled, frame-rate-independent)

    def hwheel(self, notches):     # shift-wheel / trackpad-x
        self._scroll_smooth(dx=-notches * self._HWHEEL_ROWS * self.geom.row_h)

    def _scroll_smooth(self, dx=0.0, dy=0.0):
        if self._dropdown:
            self._close_dropdown(redraw=False)
        g = self.geom
        g.clamp(self.model.nrows())
        live = (g.scroll_x, g.scroll_y)
        # (Re)seed from the live position on the first notch or after a non-wheel
        # scroll moved the view, so the glide starts where we are. Stamp _scroll_last
        # so notches in a burst accumulate onto the target instead of re-seeding.
        if self._scroll_to is None or self._scroll_last != live:
            self._scroll_pos = [float(g.scroll_x), float(g.scroll_y)]
            self._scroll_to = [float(g.scroll_x), float(g.scroll_y)]
            self._scroll_last = live
        view_w = max(1, g.w - g.freeze_x())
        view_h = max(1, g.h - g.header_h)
        t = self._scroll_to
        t[0] = min(max(0.0, t[0] + dx), g.max_scroll_x())
        t[1] = min(max(0.0, t[1] + dy), g.max_scroll_y(self.model.nrows()))
        # cap how far the target runs ahead of live: kills the "keeps scrolling after
        # I stopped" backlog that reads as lag on a fast spin.
        lead_x, lead_y = self._SCROLL_LEAD_FRAC * view_w, self._SCROLL_LEAD_FRAC * view_h
        t[0] = min(max(t[0], g.scroll_x - lead_x), g.scroll_x + lead_x)
        t[1] = min(max(t[1], g.scroll_y - lead_y), g.scroll_y + lead_y)
        if self._scroll_after is None:
            self._scroll_t = time.perf_counter()    # glide clock start (real-dt ease)
            self._scroll_after = self.host.after(self._SMOOTH_MS, self._scroll_anim_tick)

    def _scroll_anim_tick(self):
        self._scroll_after = None
        if self._scroll_to is None:
            return
        g = self.geom
        if self._scroll_last is not None and (g.scroll_x, g.scroll_y) != self._scroll_last:
            self._scroll_to = None; return          # external scroll took over -> yield
        now = time.perf_counter()
        dt, self._scroll_t = now - self._scroll_t, now
        frac = 1.0 - math.exp(-dt / self._SMOOTH_TAU)   # real-dt ease: same feel at any cadence
        view_w = max(1, g.w - g.freeze_x())
        view_h = max(1, g.h - g.header_h)
        pos, tgt = self._scroll_pos, self._scroll_to

        def ease(cur, target, view):
            gap = target - cur
            if abs(gap) <= self._SCROLL_SNAP:
                return target, True
            step = gap * frac
            # scroll_x/y render as ints; a sub-pixel step rounds to the same pixel for
            # several frames then jumps 1px = visible judder in the ease tail. Floor the
            # step to a whole pixel so every frame advances exactly 1px near the target.
            if abs(step) < 1.0:
                step = 1.0 if gap > 0 else -1.0
            cap = self._SMOOTH_MAX_VEL * view * dt  # velocity cap (per-sec, dt-scaled): far spins don't lurch
            return cur + max(-cap, min(cap, step)), False

        pos[0], dx_done = ease(pos[0], tgt[0], view_w)
        pos[1], dy_done = ease(pos[1], tgt[1], view_h)
        g.scroll_x = max(0, int(round(pos[0])))
        g.scroll_y = max(0, int(round(pos[1])))
        g.clamp(self.model.nrows())
        self._scroll_last = (g.scroll_x, g.scroll_y)
        self.redraw()                               # one frame per tick (not coalesced), smoother
        if dx_done and dy_done:
            self._scroll_to = self._scroll_pos = None
            return
        self._scroll_after = self.host.after(self._SMOOTH_MS, self._scroll_anim_tick)

    # --- scrollbars: drawn in the reserved right/bottom strips, thumb brightens to
    # accent on hover/drag. ---
    def _sb_arrow(self, cv, rect, d, color):
        """A scrollbar end button: a filled triangle pointing in direction `d`."""
        bx, by, w, h = rect
        cx, cy, r = bx + w / 2, by + h / 2, w * 0.22
        cv.poly({"up":    [(cx, cy - r), (cx - r, cy + r), (cx + r, cy + r)],
                 "down":  [(cx, cy + r), (cx - r, cy - r), (cx + r, cy - r)],
                 "left":  [(cx - r, cy), (cx + r, cy - r), (cx + r, cy + r)],
                 "right": [(cx + r, cy), (cx - r, cy - r), (cx - r, cy + r)]}[d], color)

    def _arrow_color(self, sb, axis, sign, name):
        """Held arrow -> accent (like the thumb); hovered -> lighter; else idle."""
        p = self._sbpage
        if p and p[0] == "step" and p[1] == axis and p[2] == sign:
            return _SB_THUMB_HOVER
        return _SB_ARROW_HOVER if sb.get("arrow_hover") == name else T.ARROW_IDLE

    def _draw_scrollbars(self, cv, sw, sh):
        g, sbw = self.geom, self._sbw
        g.used_rows, g.used_cols = self.model.used_extent()   # trim blank overscroll from the thumb
        # vertical bar in px (matches sub-row scroll_y) so the thumb glides; track
        # inset by one button (sbw) at each end for the arrows.
        n, view = g.row_extent(self.model.nrows()) * g.row_h, g.full_rows() * g.row_h
        cv.rect(g.w, 0, sbw, g.h, fill=_SB_TRACK)                     # vertical track
        vm = _sb_metrics(sbw, g.h - 2 * sbw, n, view, g.scroll_y)
        if vm:
            ts, tl = vm
            hot = self._vsb["hover"] or self._vsb["drag"]
            cv.rect(g.w + 2, ts, sbw - 4, tl, fill=_SB_THUMB_HOVER if hot else _SB_THUMB)
            self._vsb["thumb"] = (g.w, ts, sbw, tl)
        else:
            self._vsb["thumb"] = None
        self._vsb["up"], self._vsb["down"] = (g.w, 0, sbw, sbw), (g.w, g.h - sbw, sbw, sbw)
        self._sb_arrow(cv, self._vsb["up"], "up", self._arrow_color(self._vsb, "v", -1, "up"))
        self._sb_arrow(cv, self._vsb["down"], "down", self._arrow_color(self._vsb, "v", 1, "down"))
        cv.rect(0, g.h, g.w, sbw, fill=_SB_TRACK)                     # horizontal track
        total, avail = g.col_extent(), g.w - g.freeze_x()
        hm = _sb_metrics(sbw, g.w - 2 * sbw, max(1, total), avail, g.scroll_x)
        if hm:
            ts, tl = hm
            hot = self._hsb["hover"] or self._hsb["drag"]
            cv.rect(ts, g.h + 2, tl, sbw - 4, fill=_SB_THUMB_HOVER if hot else _SB_THUMB)
            self._hsb["thumb"] = (ts, g.h, tl, sbw)
        else:
            self._hsb["thumb"] = None
        self._hsb["left"], self._hsb["right"] = (0, g.h, sbw, sbw), (g.w - sbw, g.h, sbw, sbw)
        self._sb_arrow(cv, self._hsb["left"], "left", self._arrow_color(self._hsb, "h", -1, "left"))
        self._sb_arrow(cv, self._hsb["right"], "right", self._arrow_color(self._hsb, "h", 1, "right"))
        cv.rect(g.w, g.h, sbw, sbw, fill=_SB_TRACK)                   # corner
        # A thumb may have slid under a stationary cursor; re-test hover against the
        # new rects (_sb_hover redraws only if a flag flips).
        if self._ptr and not (self._vsb["drag"] or self._hsb["drag"]):
            self._sb_hover(*self._ptr)

    def _sb_press(self, x, y):
        """Route a scrollbar press: end-arrow step, thumb drag, or track paging."""
        g = self.geom
        vt, ht = self._vsb["thumb"], self._hsb["thumb"]
        if x >= g.w and y < g.h:                                      # vertical strip
            if self._in(self._vsb.get("up"), x, y):
                self._start_sbstep("v", -1)
            elif self._in(self._vsb.get("down"), x, y):
                self._start_sbstep("v", 1)
            elif self._in(vt, x, y):
                self._vsb.update(drag=True, grab=y - vt[1])
            elif vt:                                                  # hold -> glide toward the click
                self._start_sbpage("v", y)
            return True
        if y >= g.h and x < g.w:                                      # horizontal strip
            if self._in(self._hsb.get("left"), x, y):
                self._start_sbstep("h", -1)
            elif self._in(self._hsb.get("right"), x, y):
                self._start_sbstep("h", 1)
            elif self._in(ht, x, y):
                self._hsb.update(drag=True, grab=x - ht[0])
            elif ht:
                self._start_sbpage("h", x)
            return True
        return x >= g.w or y >= g.h                                   # corner -> swallow

    # Held scrollbar actions share one repeat timer: "page" eases toward the pointer
    # and stops (Excel track-click); "step" nudges a fixed amount per tick while an
    # arrow is held. "page" reuses the wheel glide's constants and float accumulator
    # so track-paging is as smooth as the wheel.
    _SBPAGE_MS = 16                                                  # filter-list page-ease cadence
    _ARROW_MS = 40                                                   # arrow-repeat cadence
    _ARROW_PX = 48                                                   # horizontal arrow step (px)

    def _sbpage_target(self, axis, pos):
        """Scroll offset that lands the thumb centered on track coordinate `pos`."""
        g, sbw = self.geom, self._sbw
        if axis == "v":
            tl = self._vsb["thumb"][3]
            off = _sb_offset(pos, tl / 2, sbw, g.h - 2 * sbw, tl,
                             g.row_extent(self.model.nrows()) * g.row_h, g.full_rows() * g.row_h)
            return min(max(0, off), int(g.max_scroll_y(self.model.nrows())))
        tl = self._hsb["thumb"][2]
        off = _sb_offset(pos, tl / 2, sbw, g.w - 2 * sbw, tl, max(1, g.col_extent()), g.w - g.freeze_x())
        return min(max(0, off), int(g.max_scroll_x()))

    def _start_sbpage(self, axis, pos):
        self._sbpage = ("page", axis, pos)
        self._sbpage_pos = float(self.geom.scroll_y if axis == "v" else self.geom.scroll_x)
        if self._sbpage_after is None:
            self._sbpage_tick()

    def _start_sbstep(self, axis, sign):
        self._sbpage = ("step", axis, sign)
        if self._sbpage_after is None:
            self._sbpage_tick()

    def _stop_sbpage(self):
        if self._sbpage_after is not None:
            self.host.after_cancel(self._sbpage_after)   # no-op on stale ids
            self._sbpage_after = None
        was_step = self._sbpage and self._sbpage[0] == "step"
        self._sbpage = None
        self._sbpage_pos = None
        if was_step:
            self.redraw()                                # drop the arrow's held (accent) tint

    def _start_fpage(self, y):
        """Same hold-and-glide as the grid track, but easing the filter list's row top."""
        self._sbpage = ("fpage", None, y)
        if self._sbpage_after is None:
            self._sbpage_tick()

    def _fpage_tick(self):
        if not (self._filter and self._filter.get("layout")):
            self._sbpage = None; return
        f, lo = self._filter, self._filter["layout"]
        lx, ly, lw, rh, nvis, sbw = lo["list"]
        n = len(lo["items"])
        th = lo["sbthumb"][3] if lo.get("sbthumb") else rh
        target = max(0, min(_sb_offset(self._sbpage[2], th / 2, ly, nvis * rh, th, n, nvis),
                            max(0, n - nvis)))
        gap = target - f["top"]
        if gap == 0:
            self._sbpage = None; return
        step = max(-nvis, min(nvis, int(gap * self._SCROLL_EASE))) or (1 if gap > 0 else -1)
        f["top"] = max(0, min(f["top"] + step, max(0, n - nvis)))
        self.redraw()
        self._sbpage_after = self.host.after(self._SBPAGE_MS, self._sbpage_tick)

    def _sbpage_tick(self):
        self._sbpage_after = None
        if not self._sbpage:
            return
        if self._sbpage[0] == "fpage":
            self._fpage_tick(); return
        g, (kind, axis, val) = self.geom, self._sbpage
        view = g.full_rows() * g.row_h if axis == "v" else (g.w - g.freeze_x())
        cur = g.scroll_y if axis == "v" else g.scroll_x
        if kind == "page":                               # float glide toward the held pointer
            if self._sbpage_pos is None or abs(self._sbpage_pos - cur) > 1:
                self._sbpage_pos = float(cur)            # (re)seed if the view moved
            gap = self._sbpage_target(axis, val) - self._sbpage_pos
            if abs(gap) <= self._SCROLL_SNAP:            # land exactly on the pointer and stop
                newv = self._sbpage_target(axis, val)
                self._sbpage = self._sbpage_pos = None
            else:
                cap = self._SCROLL_MAX_FRAC * view       # cap top speed so far clicks don't lurch
                self._sbpage_pos += max(-cap, min(cap, gap * self._SCROLL_EASE))
                newv = max(0, int(round(self._sbpage_pos)))
        else:                                            # end arrow: fixed step, repeat while held
            newv = max(0, cur + val * (g.row_h if axis == "v" else self._ARROW_PX))
        if axis == "v":
            g.scroll_y = newv
        else:
            g.scroll_x = newv
        g.clamp(self.model.nrows())
        self.redraw()                                # paint this tick directly, like wheel glide
        if self._sbpage is None:                     # page glide landed -> no more ticks
            return
        self._sbpage_after = self.host.after(self._SCROLL_MS if kind == "page" else self._ARROW_MS,
                                             self._sbpage_tick)

    def _sb_drag(self, x, y):
        g, sbw = self.geom, self._sbw
        if self._sbpage and self._sbpage[0] == "page":   # drag along track re-aims the glide
            self._sbpage = ("page", self._sbpage[1], y if self._sbpage[1] == "v" else x)
            return True
        if self._vsb["drag"] and self._vsb["thumb"]:
            tl = self._vsb["thumb"][3]
            self.geom.scroll_y = max(0, _sb_offset(y, self._vsb["grab"], sbw, g.h - 2 * sbw, tl,
                                                   g.row_extent(self.model.nrows()) * g.row_h,
                                                   g.full_rows() * g.row_h))
            self.geom.clamp(self.model.nrows()); self._coalesce(self._paint_now); return True
        if self._hsb["drag"] and self._hsb["thumb"]:
            tl = self._hsb["thumb"][2]
            total = g.col_extent()
            self.geom.scroll_x = max(0, _sb_offset(x, self._hsb["grab"], sbw, g.w - 2 * sbw, tl,
                                                   max(1, total), g.w - g.freeze_x()))
            self.geom.clamp(self.model.nrows()); self._coalesce(self._paint_now); return True
        return False

    def _which_arrow(self, sb, names, x, y):
        for name in names:
            if self._in(sb.get(name), x, y):
                return name
        return None

    def _sb_hover(self, x, y):
        """Update thumb + arrow hover flags. Return True if over a scrollbar."""
        vh = self._in(self._vsb["thumb"], x, y)
        hh = self._in(self._hsb["thumb"], x, y)
        va = self._which_arrow(self._vsb, ("up", "down"), x, y)
        ha = self._which_arrow(self._hsb, ("left", "right"), x, y)
        if (vh != self._vsb["hover"] or hh != self._hsb["hover"]
                or va != self._vsb["arrow_hover"] or ha != self._hsb["arrow_hover"]):
            self._vsb["hover"], self._hsb["hover"] = vh, hh
            self._vsb["arrow_hover"], self._hsb["arrow_hover"] = va, ha
            self.redraw()
        return x >= self.geom.w or y >= self.geom.h

    def _coalesce(self, fn):
        self._next = fn
        if not self._paint_pending:
            self._paint_pending = True
            self.host.after_idle(self._flush)

    def _flush(self):
        self._paint_pending = False
        fn, self._next = self._next, None
        if fn:
            fn()

    def close(self):
        if self._zoom_to is not None:      # closed mid-glide: don't leave GC frozen
            gc.enable()
        if self._surf is not None:
            self._lib.gpu_detach(self._surf)
            self._surf = None


def _selftest():
    from ..core.geometry import Geometry
    from .coremodel import make_model
    from ..core.paint import paint
    from ..core.render import blit
    from types import SimpleNamespace

    # Autoscroll velocity curve (pure: reads only self.geom). Zero inside, faster
    # past an edge, correctly signed.
    stub = SimpleNamespace(geom=SimpleNamespace(row_h=20, h=400, header_h=40, w=600, gutter_w=56),
                           _AS_BASE=GpuEngine._AS_BASE, _AS_GAIN=GpuEngine._AS_GAIN)
    assert GpuEngine._edge_scroll(stub, 300, 200) == (0.0, 0.0)          # inside -> no scroll
    near = GpuEngine._edge_scroll(stub, 300, 401)[0]                     # just past the bottom
    far = GpuEngine._edge_scroll(stub, 300, 401 + 20 * 6)[0]            # 6 rows past
    assert 0 < near < far, "autoscroll vel: %r then %r" % (near, far)    # accelerates, uncapped
    assert GpuEngine._edge_scroll(stub, 300, 20)[0] < 0                  # above top -> scroll up
    assert GpuEngine._edge_scroll(stub, 590, 200)[1] > 0                 # right band -> pan right

    lib = _load_lib()
    if lib is None:
        print("%s not built. Run build-all.bat, then import from dist\\fastpygrid"
              % os.path.basename(_lib_path()))
        return 1

    # A: color/decode correctness: one opaque rect, read the pixel back exactly.
    cv = GpuCanvas(13)
    cv.rect(0, 0, 60, 40, fill="#3366cc")
    got = lib.gpu_probe_pixel(bytes(cv.buf), len(cv.buf), 64, 48, 10, 10)
    assert got == 0xFF3366CC, "rect color: got %08X want FF3366CC" % got

    # A2: filled polygon -> probe the interior (GL stencil concave-fill path).
    cv = GpuCanvas(13)
    cv.poly([(10, 10), (50, 10), (30, 50)], "#22cc44")     # triangle
    got = lib.gpu_probe_pixel(bytes(cv.buf), len(cv.buf), 64, 64, 30, 25)   # strictly inside
    assert got == 0xFF22CC44, "poly fill: got %08X want FF22CC44" % got

    # A3: text path. Solid block glyph (U+2588) white on black -> a point near the
    # cell center must read near-white. Scans a few points to not hinge on exact
    # glyph metrics.
    def _near_white(buf, w, h, px, py):
        v = lib.gpu_probe_pixel(buf, len(buf), w, h, px, py)
        return ((v >> 16) & 0xff) > 0x80 and ((v >> 8) & 0xff) > 0x80 and (v & 0xff) > 0x80
    cv = GpuCanvas(44)
    cv.rect(0, 0, 64, 64, fill="#000000")
    cv.text(0, 0, 64, 64, "█", "#ffffff", center=True)
    tb = bytes(cv.buf)
    assert any(_near_white(tb, 64, 64, px, py) for px in (26, 32, 38) for py in (26, 32, 38)), \
        "text glyph did not render near-white near center"

    # A4: glyph-atlas overflow recovery (the "text vanishes" zoom bug). Overflow the
    # atlas in one frame (many large distinct glyphs), then draw a block last; it
    # lands in the rebuilt atlas and must still render, else this probes black.
    cv = GpuCanvas(200)
    for k in range(80):
        cv.text(0, 0, 40, 40, chr(0x21 + k), "#ffffff")     # 80 big distinct glyphs -> overflow
    cv.rect(0, 0, 320, 320, fill="#000000")                 # cover fillers: probe tests the block alone
    cv.text(40, 40, 240, 240, "█", "#ffffff", center=True)  # wide cell (no ellipsis-trim), drawn last
    ob = bytes(cv.buf)
    assert any(_near_white(ob, 320, 320, px, py) for px in (150, 160, 170) for py in (150, 160, 170)), \
        "glyph vanished after atlas overflow/rebuild"

    # B: full pipeline: real model through paint()->blit()->GpuCanvas. Probe a
    # data-cell interior, it must be painted (not the black clear sentinel).
    model = make_model(["A", "B", "C"], [["x", "y", "z"], ["1", "2", "3"]])
    g = Geometry([120, 120, 120], 0, hdr_rows=model.header_rows)
    g.w, g.h = 400, 200
    cv = GpuCanvas(13)
    blit(paint(model, g, (g.hdr_rows, 0), []), cv)
    # center of the first data cell (col 0, first data row)
    px = g.gutter_w + g.col_w[0] // 2
    py = g.row_y(g.hdr_rows) + g.row_h // 2
    got = lib.gpu_probe_pixel(bytes(cv.buf), len(cv.buf), g.w, g.h, px, py)
    assert (got >> 24) == 0xFF and got != 0xFF000000, \
        "data cell not painted: got %08X at (%d,%d)" % (got, px, py)

    print("ok  (glsurface: rect color exact, full paint->blit->Gpu pipeline renders)")
    return 0


if __name__ == "__main__":
    if "--build" in sys.argv:
        sys.exit(0 if build() else 1)
    sys.exit(_selftest())
