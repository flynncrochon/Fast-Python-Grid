"""OpenGL/GPU renderer engine, TOOLKIT-NEUTRAL.

Blits the core display list (paint()) onto an OpenGL 1.1 surface driven by a small
C-ABI DLL (see ../csrc/glsurface.cpp), loaded via ctypes. The GPU compositor keeps the
zoomed-out full-rebuild cheap. Scrolling just repaints the viewport and Presents.

This module imports NO GUI toolkit. It provides:
  * GpuEngine: owns model/geometry/controller, the Gpu surface, and ALL
    rendering, overlays (editor/dropdown/filter/find), scrollbars and normalized
    input logic. Talks to a `host` adapter for toolkit bits (see the host contract
    on GpuEngine). This makes a Tk host and a Qt host thin, interchangeable wrappers.
  * GpuCanvas: the core.render.blit backend that packs each primitive into the
    wire buffer glsurface.cpp decodes.
  * TextField: a custom text-input control (measures via a host callable).

Host adapters live in their own files: fastpygrid.render.tk (Tk) and
fastpygrid.render.qt (Qt). Use those modules' make_sheet() to launch.

Build once:  build.bat   (compiles the DLLs and copies the package into dist\fastpygrid)
"""
import ctypes
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
                    _blend, SEL_WASH_A)
from .render import blit, blit_fast

# Chrome colors for the custom overlays, decoupled from the cell palette.
_UI_BG = "#ece7dd"          # find bar (light)
# filter popup: a dark panel (darker than the white grid body) with light text
_PANEL_BG = "#2b2a26"
_PANEL_FG = "#e8e5dc"
_PANEL_SUB = "#3a3934"      # input field / row backing on the dark panel
_PANEL_HI = "#4a463d"       # hovered row / button
# scrollbars (track + thumb, thumb brightens to the accent on hover/drag)
_SB_TRACK = "#e3e0d6"
_SB_THUMB = "#b7b1a3"
_SB_TRACK_DK = "#232220"    # on the dark filter panel
_SB_THUMB_DK = "#5c574d"
_SB_THUMB_HOVER = T.ACCENT  # thumb brightens to the accent on hover/drag


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

_DLL_DIR = os.path.dirname(__file__)          # the .dll/.so installs into core/, beside this file


def _lib_path():
    ext = ".dll" if sys.platform == "win32" else ".so"
    return os.path.join(_DLL_DIR, "glsurface" + ext)


# UI font name for the toolkit hosts' width MEASUREMENT. It must match the font the
# surface actually rasterizes so ellipsis-trim / centering line up: Windows GDI uses
# Segoe UI (glsurface.cpp), the Linux FreeType backend uses DejaVu Sans.
UI_FONT = "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"


def _load_lib():
    """Load the glsurface render backend (OpenGL 1.1, cross-platform), or None if it
    isn't built / can't load. Exports the C-ABI below. make_sheet() raises a build
    hint on None."""
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
        "gpu_render": ([P, C, I, I], None),
        "gpu_resize": ([P, I, I], None),
        "gpu_detach": ([P], None),
    }
    for name, (args, res) in sig.items():
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, res
    return lib


def build():
    """Build the whole dist (all .py + both DLLs) via the repo build.bat.
    Dev-only: build.bat and the .cpp source aren't in the shipped package."""
    root = os.path.join(os.path.dirname(__file__), "..", "..", "..")
    bat = os.path.join(root, "build.bat")
    return subprocess.call([bat], cwd=root, shell=True) == 0


_COL_CACHE = {}
def _col(c):
    """'#rrggbb' -> 0xRRGGBB int, falsy -> -1 (means 'no fill/outline'). Memoized:
    called ~6k times/frame over a handful of distinct theme colors, so the hex parse
    is a dict hit after the first."""
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
    """Windows: make the process DPI-aware so the surface renders sharp instead of
    being bitmap-stretched by the OS on a hi-DPI display. No-op elsewhere.

    Also disables Qt's own HighDPI scaling: the engine works in PHYSICAL px, so Qt
    must report physical coords/sizes (dpr==1) or every click maps toward the origin
    and the surface renders into a 1/dpr corner. These env vars only take effect if
    set BEFORE QApplication. Both Qt entry points call this right before creating
    it, so this is the reliable place (module-import timing in render.qt is too late when
    the app is built first, e.g. the demo). No-op for Tk."""
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "0")
    try:
        import ctypes as _c
        try:
            _c.windll.shcore.SetProcessDpiAwareness(2)         # per-monitor v2
        except Exception:
            _c.windll.user32.SetProcessDPIAware()              # older Windows
    except Exception:
        pass


def _check_poly(ax, ay, bx, by, cx, cy, h):
    """Outline of a checkmark stroke (centerline a->b->c, half-thickness h) as a
    filled polygon (mitered joins, so the bend is clean, since a two-line stroke
    leaves a butt-cap notch at the vertex)."""
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
        return max(1.0, _c.windll.user32.GetDpiForSystem() / 96.0)   # Win10+; Tk/Qt hosts both
    except Exception:
        return 1.0


# Compiled wire packers (tag byte baked in as the leading 'c', so one pack call emits
# tag+payload -- no format-string reparse and no `b"R" +` concat per primitive; hot at
# ~4k packs/frame).
_PK_R = struct.Struct("<cffffiif").pack
_PK_T = struct.Struct("<cffffifBH").pack
_PK_X = struct.Struct("<cfffffifH").pack
_PK_L = struct.Struct("<cffffif").pack
_PK_P = struct.Struct("<ciH").pack
_PK_FF = struct.Struct("<ff").pack


class GpuCanvas:
    """core.render.blit backend: buffers primitives into the packed wire format
    (see glsurface.cpp header) instead of drawing directly. The host ships
    bytes(canvas.buf) to the DLL once per frame: one native call, not one per
    primitive (the Python->native boundary is the cost worth avoiding)."""

    def __init__(self, fpx, scale=1.0):
        self.fpx = float(fpx)          # cell-text pixel size, glyph() carries its own
        self.s = scale
        self.buf = bytearray()

    def rect(self, x, y, w, h, fill=None, outline=None, width=1):
        # round the stroke width to whole px so outlines/rings/dividers land at a
        # consistent thickness on a hi-DPI display
        self.buf += _PK_R(b"R", x, y, w, h,
                          _col(fill), _col(outline), max(1, round(width * self.s)))

    def _text(self, x, y, w, h, s, color, size, bold, center):
        u = s.encode("utf-16-le")
        flags = (1 if bold else 0) | (2 if center else 0)
        self.buf += _PK_T(b"T", x, y, w, h, _col(color), size, flags, len(u) // 2) + u

    def text(self, x, y, w, h, s, color, bold=False, center=False):
        self._text(x, y, w, h, s, color, self.fpx, bold, center)

    def text_scrolled(self, x, y, w, h, origin_x, s, color):
        """Left-aligned text at an explicit origin_x, clipped to (x,y,w,h). Used by
        the custom text field for horizontal scroll (origin_x = x - xscroll)."""
        u = s.encode("utf-16-le")
        self.buf += _PK_X(b"X", x, y, w, h, origin_x, _col(color), self.fpx, len(u) // 2) + u

    def line(self, x1, y1, x2, y2, color, width):
        self.buf += _PK_L(b"L", x1, y1, x2, y2, _col(color), max(1, round(width * self.s)))

    def poly(self, points, color):
        self.buf += _PK_P(b"P", _col(color), len(points))
        for px, py in points:
            self.buf += _PK_FF(px, py)

    def barrier(self):
        """Layer break. The GL backend batches cell fills ahead of text for speed;
        anything emitted after this (overlay widgets) must stay on top, so this forces
        the batch out first."""
        self.buf += b"F"

    def glyph(self, cx, cy, s, color, px):
        # one char centered on (cx, cy) at its own pixel size -> a centered text op
        self._text(cx - px, cy - px, 2 * px, 2 * px, s, color, float(px), False, True)

    def combo(self, x, y, w, h):
        """Drop button: thin box + filled chevron, decomposed to rect+poly here so
        the DLL only needs R/L/P/T (mirrors the Tk/Qt combo drawing)."""
        sz = max(8, round(h) - 8)
        bx, by = x + w - sz - 3, y + (h - sz) / 2
        self.rect(bx, by, sz, sz, outline=T.BTN_BORDER)
        cx, cy = bx + sz / 2, by + sz / 2
        r = max(2.0, sz * 0.26)
        self.poly([(cx - r, cy - r * 0.5), (cx + r, cy - r * 0.5), (cx, cy + r * 0.7)],
                  T.ARROW_IDLE)


class TextField:
    """Custom single-line text input rendered with GpuCanvas primitives, no Tk
    widget. The owner feeds it key/mouse events and calls draw() each frame. It
    owns text, caret, selection, horizontal scroll, and clipboard. Measures widths
    with a `measure` callable the host provides (toolkit-neutral)."""

    def __init__(self, measure, text="", clipboard=None):
        self.measure = measure                   # callable: text -> px width (toolkit-neutral)
        self.clip = clipboard                    # (get, set) callables, or None
        self.focused = True
        self.set_text(text)

    def set_text(self, s):
        self.text = s
        self.caret = self.anchor = len(s)
        self.xscroll = 0

    def _w(self, i):                             # px width of text[:i]
        return self.measure(self.text[:i]) if i else 0

    def _index_at(self, px):                     # caret index nearest text-space pixel px
        best, bestd = 0, 1e18
        for i in range(len(self.text) + 1):
            d = abs(self._w(i) - px)
            if d < bestd:
                best, bestd = i, d
            elif d > bestd:
                break                            # width is monotonic in i -> past the min
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
        if not shift:                            # a bare movement collapses the selection
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

    def draw(self, cv, x, y, w, h, bg, fg, accent, border=True):
        pad = cv.fpx * 5 / 13                     # match the cell-text left pad (glsurface.cpp 'T' op)
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
            cv.text_scrolled(x + pad, y, avail, h, x + pad - self.xscroll, self.text, fg)
        if self.focused:                         # caret
            cxp = x + pad + cx - self.xscroll
            cv.line(cxp, y + 3, cxp, y + h - 3, fg, 1)


class GpuEngine:
    """Toolkit-NEUTRAL grid engine: owns the model/geometry/controller, the Gpu
    surface, and ALL rendering, overlay (editor/dropdown/filter/find), scrollbar
    and input LOGIC. Knows nothing about Tk or Qt: it talks to a `host` adapter
    for the toolkit-specific bits, so a Tk host and a Qt host are thin wrappers.

    Host adapter contract (see TkHost):
        host.measure(text, bold=False) -> int      text width in px
        host.after(ms, fn) -> handle, host.after_cancel(handle), host.after_idle(fn)
        host.clip_get() -> str, host.clip_set(text)
        host.focus(), host.set_cursor(on_edge), host.fullscreen_toggle()
        host.size() -> (w, h), host.hwnd() -> int
        host.set_zoom_px(px), host.context_menu(root_xy, editable, actions)
    The engine is also the GridController's host surface. Input arrives already
    normalized: press/motion/drag/release/wheel/key/configure/context."""

    def __init__(self, host, model, editable=True, frozen=0, col_w=None, scale=1.0, lib=None,
                 uncap_rows=False, uncap_cols=False, filters=True):
        self.host = host
        self.model = model
        self.editable = editable
        self._lib = lib          # make_sheet/make_qt load it and raise before we get here
        s = self._scale = scale
        self._base_fpx = 13 * s
        self._fpx = max(9.0, self._base_fpx)   # float: exact size for the GPU text op
        widths = [max(24, round(w * s)) for w in (col_w or [120] * model.ncols)]
        self.geom = Geometry(widths, frozen, gutter_w=max(28, round(56 * s)),
                             row_h=max(14, round(22 * s)), hdr_rows=model.header_rows,
                             uncap_rows=uncap_rows, uncap_cols=uncap_cols, filters=filters)
        self.ctl = GridController(self, base_row_h=22 * s, base_gutter=56 * s, base_w=widths)
        self._measure = lambda t: self.host.measure(t, False)   # for TextField
        self._surf = None
        self._editor = None          # open custom in-cell editor state, or None
        self._dbl = (0.0, 0, 0)      # (time, x, y) of the last editor word-select, for triple detect
        self._dropdown = None        # open custom dropdown state, or None
        self._find = None            # open custom find bar state, or None
        self._filter = None          # open custom filter popup state, or None
        self._sbw = max(12, round(14 * s))            # scrollbar thickness
        self._vsb = {"hover": False, "drag": False, "grab": 0, "thumb": None}
        self._hsb = {"hover": False, "drag": False, "grab": 0, "thumb": None}
        self._arrow = False          # pointer is over a ▼ dropdown button (hand cursor)
        self._next = None
        self._paint_pending = False
        self._in_drag = False        # true while a mouse drag gesture is in flight
        self._autoscroll_after = None   # edge-autoscroll timer handle (selection drag)
        self._autoscroll_acc = [0.0, 0.0]   # fractional (row, px) carried between autoscroll ticks
        self._autoscroll_t = 0.0        # perf_counter of the last autoscroll tick (for real-dt velocity)
        self._drag_xy = None            # last grid-drag pointer pos (for the timer)
        # smooth (inertial) wheel scrolling: wheel adds to a float pixel TARGET and an
        # animation eases the live position toward it a fraction per frame, so panning
        # glides instead of snapping whole rows. See _scroll_smooth / _scroll_anim_tick.
        self._scroll_after = None       # animation timer handle, or None when idle
        self._scroll_pos = None         # [x, y] float px the animation is easing from
        self._scroll_to = None          # [x, y] float px target
        self._scroll_last = None        # (scroll_x, scroll_y) we last wrote (detect external scroll)
        # smooth zoom: a Ctrl+wheel notch multiplies a float target, an animation eases
        # the live zoom toward it a fraction per frame (mirrors the scroll glide above).
        self._zoom_after = None         # zoom animation timer handle, or None when idle
        self._zoom_to = None            # target zoom factor
        model.changed = lambda: self._coalesce(self.redraw)

    # --- surface lifecycle (lazy: device is created on the first Configure, AFTER
    # the window maps, so make_sheet() stays instant, load time is unchanged) --
    def _attach(self):
        if self._surf is not None:
            return
        w, h = self.host.size()
        if w < 2 or h < 2:
            return
        self._surf = self._lib.gpu_attach(self.host.hwnd(), w, h) or None
        # The child HWND is freshly mapped and our WM_PAINT is a no-op (we rely on the
        # last gpu_render present persisting via DWM). On first show there's no prior
        # present, so the first render can race the window becoming visible and leave it
        # white until a click/resize. Force one more render once the loop settles.
        if self._surf is not None:
            self.host.after_idle(self.redraw)

    def redraw(self):
        # During a drag, Windows fires mouse-move faster than a full paint+blit+GPU
        # upload completes, and on_drag asks for a repaint per event. Coalesce to one
        # render per idle so the selection keeps up with the cursor instead of a
        # backlog of synchronous renders queueing. (Tk/Qt hosts coalesce natively.)
        if self._in_drag:
            self._coalesce(self._paint_now); return
        self._paint_now()

    def paint_to(self, cv):
        """Draw one frame onto the GL wire-buffer canvas `cv`. Returns False if the
        surface is too small to draw. The single render sequence."""
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
        if self._editor:                      # custom widgets drawn on top of the grid
            self._draw_editor(cv)
        if self._dropdown:
            self._draw_dropdown(cv)
        if self._filter:
            self._draw_filter(cv)
        if self._find:
            self._draw_find(cv)
        return True

    def _blit_fast(self, cv):
        """Native body path: the ~viewport-sized data-cell loop runs in C++
        (gc_paint_body) and its wire bytes splice straight into the GpuCanvas buffer;
        only the cheap chrome (gutter/headers/letter/overlays) stays in Python. Returns
        True if it built the frame. Falls back (returns False) for models without the
        C++ core or when a find is active (per-cell needle match kept in Python)."""
        m, g = self.model, self.geom
        if getattr(m, "_find_needle", "") or not hasattr(m, "gc_paint_body"):
            return False
        C = _prelude(m, g, self.ctl.ranges())
        fz = g.frozen
        # Scrollable cols and frozen cols emitted as SEPARATE body wires (layers A and
        # B, see DisplayList / blit_fast): frozen splices behind a barrier so a
        # scrollable cell scrolled under the frozen band can't bleed text over it.
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
        # chrome.frozen = L1 Python cells (gutter + scrollable header/letter, joins
        # frz_wire); chrome.chrome = L2 pins. scr_wire = L0, frz_wire = frozen body (L1).
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
        # Style overrides gathered VIEWPORT-BOUNDED: look up only the visible cells'
        # styles, never iterate the whole (possibly 100k-entry) style map. Key is
        # (source_row, col) == C++ combined(gr-H), matching _style_key for data.
        styles = []
        sd = m._styles
        if sd:
            get = sd.get
            srcs = [m._src_data(gr - C.H) for gr in grs]   # visible data rows -> source, once
            for col in cols:
                for src in srcs:
                    sty = get((src, col))
                    if not sty:
                        continue
                    bg = sty.get("bg")
                    base = _col(bg) if bg else -1
                    basew = _col(_blend(bg, T.SEL_TINT, SEL_WASH_A)) if bg else -1
                    styles += [src, col, _col(sty.get("fg", T.TXT)), base, basew,
                               1 if sty.get("bold") else 0]
        wire = m.gc_paint_body(cols, colx, colw, grs, rowy, g.row_h, C.H, self._fpx,
                               max(1, round(self._scale)), sel, C.single_cell,
                               _col(T.TXT), _col(T.ZEBRA), _col(T.BG),
                               _col(C.wash_even), _col(C.wash_odd), styles)
        box = [min(colx), min(rowy),
               max(colx[i] + colw[i] for i in range(len(colx))), max(rowy) + g.row_h]
        return wire, box

    def _paint_now(self):
        if self._surf is None:
            self._attach()
            if self._surf is None:
                return
        cv = GpuCanvas(self._fpx, self._scale)
        if self.paint_to(cv):
            self._lib.gpu_render(self._surf, bytes(cv.buf), len(cv.buf), _col(T.BG))

    # --- GridController host surface (delegates toolkit bits to self.host) ---
    def after_scroll_change(self):   self._coalesce(self.redraw)
    def after_geometry_change(self): self._coalesce(self.redraw)
    def measure(self, text, bold):   return self.host.measure(text, bold)
    def set_edge_cursor(self, on):   # ctl.on_motion drives this, hand wins over default
        self._set_cursor("resize" if on else ("hand" if self._arrow else ""))

    def _set_cursor(self, kind):     # dedup so we don't re-set every motion (flicker)
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
        # Jira/browser tables ship as an HTML <table>, flatten it here to plain TSV so
        # both the Python and C++ models paste it with their existing tab/newline split.
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

    # --- custom find bar drawn top-right on the surface (no Tk widget). Modal-ish:
    # keys route to the query field. Enter=next, Shift+Enter=prev, Esc closes,
    # clicking off the bar closes it. Match highlights come from FindController -> the
    # model find-state -> paint(). ---
    def reveal_find(self):
        if self._find is None:
            tf = TextField(self._measure, "", clipboard=(self.host.clip_get, self.host.clip_set))
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
        h = g.row_h + round(8 * s)
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

    # --- custom filter popup drawn on the surface (no Tk widget): sort buttons, a
    # search field, a scrollable checkbox list, OK/Cancel. Modal while open. The
    # distinct scan runs deferred so opening is instant even on a huge column. ---
    def _open_filter(self, col):
        # Measure at base font size (÷ current zoom), so the search caret matches the
        # zoom-independent text the popup draws. host.measure uses the zoomed font.
        base_measure = lambda t: round(self.host.measure(t, False) * self._base_fpx / max(1, self._fpx))
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
        # Size is pinned to the DPI scale s, NOT the grid zoom: row_h / _fpx / col_w
        # all grow with Ctrl+wheel zoom, but the popup should stay one constant size.
        rh, pad = max(14, round(22 * s)), round(5 * s)
        W = round(220 * s)
        saved_fpx, cv.fpx = cv.fpx, max(9, round(13 * s))   # popup text at base size too
        x = max(pad, min(g.col_x(f["col"]), g.w - W - pad))
        y = g.header_h                                       # flush under the column's ▼ button
        ctl = f["ctl"]
        have_colors = bool(f["loaded"] and (f["colors"]["bg"] or f["colors"]["fg"]))
        rows = ctl.rows(f["tf"].text) if f["loaded"] else []
        items = (["\0all"] + list(rows)) if f["loaded"] else []
        avail = g.h - y - 2 * pad
        fixed = 7 * rh + 3 * pad          # sortA, sortZ, sortcolor, clearsort, clear, filtercolor, search
        nvis = max(1, min(len(items) or 1, (avail - fixed - rh - 2 * pad) // rh, 12))
        listh = nvis * rh
        H = fixed + listh + 2 * pad + rh                      # + OK/Cancel row
        cv.rect(x - 1, y - 1, W + 2, H + 2, fill="#000000")   # border
        cv.rect(x, y, W, H, fill=_PANEL_BG)                   # dark panel (darker than the grid)
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

        button("sortA", "Sort A → Z")
        button("sortZ", "Sort Z → A")
        button("sortcolor", "Sort by Color", have_colors, arrow=True)
        button("clearsort", "Clear Sort", self.model.has_sort(f["col"]))
        button("clear", "Clear Filter", self.model.has_filter(f["col"]))
        button("filtercolor", "Filter by Color", have_colors, arrow=True)
        f["tf"].focused = True                               # search field (dark input)
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
            if v == "\0all":
                label, checked = "(Select all)", ctl.all_on(rows)
            else:
                label, checked = (v if v else "(blank)"), ctl.checked(v)
            ry = cy + i * rh
            hov = ii == f.get("hoverrow")
            cv.rect(x + pad, ry, lw, rh - 1, fill=_PANEL_HI if hov else _PANEL_BG)
            # checkbox drawn as primitives (not a ☑/☐ glyph: font fallback rendered
            # the two states at different sizes). Box is always identical, only the
            # tick appears. Label x is fixed, so it never shifts on (un)check.
            bs = max(8, rh - round(9 * s))
            bx, by = x + pad + round(3 * s), ry + (rh - bs) // 2
            # checked: filled accent box + white tick (high contrast). unchecked: dark box
            cv.rect(bx, by, bs, bs, fill=T.ACCENT if checked else _PANEL_SUB,
                    outline=T.ACCENT if checked else _PANEL_FG)
            if checked:                          # crisp mitered tick as one filled poly
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
        # hit region padded toward the panel so the cursor crosses the small
        # button->flyout gap without the submenu blinking shut.
        f["flybounds"] = (ax - 2 * pad, ay - pad, fw + 4 * pad, fh + 2 * pad)
        active = (self.model._sort if kind == "sort"
                  else self.model._color_filters.get(f["col"]))
        sww = fw - round(40 * s)                              # swatch width, centered in the row
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
            elif thumb:                                      # page toward the click
                f["top"] += nvis if y > thumb[1] else -nvis
                self.redraw()
            return
        if lx <= x <= lx + lw and ly <= y < ly + nvis * rh:  # checklist row
            ii = f["top"] + int((y - ly) // rh)
            items = lo["items"]
            if 0 <= ii < len(items):
                rows = f["ctl"].rows(f["tf"].text)
                if items[ii] == "\0all":
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
        # Color submenu: hovering a ▸ item opens (or switches) it. It stays open while
        # the cursor is over the flyout itself, and closes the moment it moves onto any
        # other section of the popup.
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
        if key in ("sortcolor", "filtercolor"):  # open the color submenu (also opens on hover)
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

    # --- in-cell editor: a custom TextField drawn on the surface (no Tk widget). ---
    def begin_edit(self, initial=None):
        if not self.editable:
            return
        self.commit_editor()
        r, c = self.ctl.active
        if self.model.col_readonly(c):
            return
        choices = self.model.cell_choices(r, c)
        if choices is not None and r >= self.geom.hdr_rows:  # dropdown cell (data rows only) -> custom Gpu list
            self._open_dropdown(r, c, choices)
            return
        bg, fg = edit_colors(r, self.geom.hdr_rows)      # blend with the cell it edits
        text = initial if initial is not None else self.model.cell(r, c)
        tf = TextField(self._measure, text, clipboard=(self.host.clip_get, self.host.clip_set))
        # caret at end (set_text default): open just begins editing, no select-all
        self._editor = {"tf": tf, "cell": (r, c), "bg": bg, "fg": fg}
        self.host.focus()
        self.redraw()

    def _draw_editor(self, cv):
        ed = self._editor
        r, c = ed["cell"]
        g = self.geom
        if not g.cell_visible(r, c) or g.col_x(c) < g.gutter_w:
            return                                       # scrolled out of view
        ed["tf"].draw(cv, g.col_x(c), g.row_y(r), g.col_width(c) + 1, g.row_h_at(r) + 1,
                      ed["bg"], ed["fg"], T.ACCENT)

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

    _TRIPLE_MS = 0.5   # click within this of a word-select, at the same spot, counts as a triple

    def _is_triple(self, x, y):
        t, px, py = self._dbl
        return (time.monotonic() - t) < self._TRIPLE_MS and abs(x - px) < 4 and abs(y - py) < 4

    def _edit_pad(self):
        return self._fpx * 5 / 13                 # editor's text left pad (matches TextField.draw)

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

    # --- custom Gpu dropdown: a list drawn on the surface (no Tk widget), with its
    # own scrollbar (wheel + thumb drag). Input runs through the grid's Tk bindings
    # while _dropdown is open. Opens below the cell, flips above near the bottom. ---
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
        cv.rect(x - 1, y - 1, w + 2, listh + 2, fill="#000000")       # border frame
        for i in range(nvis):
            oi = d["top"] + i
            if oi >= n:
                break
            sel = oi == d["sel"]                          # dark panel, matches the filter popup
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
            over = self._in_rect(lo["thumb"], x, y)
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
        self.model.set_cell(r, c, val)                   # triggers a repaint via model.changed

    def _close_dropdown(self, redraw=True):
        d = self._dropdown
        if d and d["ta"]:
            self.host.after_cancel(d["ta"])      # host after_cancel is a no-op on stale ids
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
        # No 9px floor and no int rounding here: col_w/row_h scale linearly with z,
        # so the GPU cell font must too, exactly, or it's a hair too big for the cell
        # and the text gets ellipsis-trimmed to "…". Keep _fpx a float for the GL text
        # op, only the Qt/Tk host fonts (used for measuring/editing) need an int.
        self._fpx = max(1.0, self._base_fpx * z)
        self.host.set_zoom_px(max(1, round(self._fpx)))

    # --- normalized input: the host translates native events into these calls, so
    # the routing (overlays first, then grid) is identical under any toolkit. ---
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
        if self._dropdown:
            self._dropdown["drag"] = None; return
        if self._filter and self._filter.get("sbdrag"):
            self._filter["sbdrag"] = False; return
        if self._vsb["drag"] or self._hsb["drag"]:
            self._vsb["drag"] = self._hsb["drag"] = False; return
        self.ctl.on_release()

    def motion(self, x, y):
        if self._dropdown:                     # reset: the trigger ▼ left a hand cursor,
            self._set_cursor(""); self._dropdown_hover(x, y); return   # the popup itself is default
        if self._filter:
            self._set_cursor(""); self._filter_hover(x, y); return
        if self._sb_hover(x, y):
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
            return
        if self._sb_drag(x, y):
            return
        if self._editor:
            r, c = self._editor["cell"]
            self._editor["tf"].click(x - self.geom.col_x(c) - self._edit_pad(), shift=True)
            self.redraw(); return
        # At an edge the timer owns the scrolling, so the event itself must NOT also
        # scroll (follow=False), else the two compound and race the pointer.
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

    # --- edge autoscroll during a selection drag: re-arms near-immediately so it
    # paces on the paint itself (the vsync'd render is the frame clock), and moves at
    # a VELOCITY (cells/sec) that grows with how far PAST the edge the pointer is held.
    # dt is the REAL elapsed time between ticks (via perf_counter), so the speed is
    # correct no matter how long a frame took -- and it's UNCAPPED: push further, go
    # faster, no ceiling. The only guard is a dt clamp so a stall can't teleport the
    # view. Fractional accumulation carries the sub-step remainder for smoothness. ---
    _AUTOSCROLL_MS = 1               # re-arm ASAP: don't add a fixed timer wait on top of the paint
    _AS_BASE = 16.0                  # cells/sec right at the edge (gentle, precise selection)
    _AS_GAIN = 42.0                  # + cells/sec per row_h past the edge; no cap -> push harder, faster
    _AS_DT_CAP = 0.05               # s: clamp elapsed dt so a hitch can't fling the view

    def _edge_scroll(self, x, y):
        """Autoscroll VELOCITY (rows/sec, px/sec) while the pointer sits past a body
        edge, or (0.0, 0.0) if it's inside. Velocity (not a per-tick step) so speed is
        timer-rate independent; uncapped so a hard push scrolls fast."""
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
            vpx = vel(x - g.w) * rh                     # px/sec, same cells/sec feel as vertical
        elif x < g.gutter_w:                            # over the gutter / past the left edge
            vpx = -vel(g.gutter_w - x) * rh
        return vr, vpx

    def _start_autoscroll(self):
        if self._autoscroll_after is None:
            self._autoscroll_acc = [0.0, 0.0]           # fresh remainder so it doesn't lurch on start
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
        dt = min(self._AS_DT_CAP, now - self._autoscroll_t)   # real elapsed -> speed is timer-independent
        self._autoscroll_t = now
        ar, apx = self._autoscroll_acc
        ar += vr * dt; apx += vpx * dt
        dr, dpx = int(ar), int(apx)                     # whole steps this tick; keep the remainder
        self._autoscroll_acc = [ar - dr, apx - dpx]
        g = self.geom
        before = (g.top_row, g.scroll_x)
        if dr:
            g.top_row = max(g.hdr_rows, g.top_row + dr)
        if dpx:
            g.scroll_x = max(0, g.scroll_x + dpx)
        g.clamp(self.model.nrows())
        applied = (g.top_row, g.scroll_x) != before
        if (dr or dpx) and not applied:                 # tried to move but clamped at the data edge: stop
            return
        if applied:
            self.ctl.on_drag(x, y, follow=False)         # extend the selection into the revealed cells
        self._autoscroll_after = self.host.after(self._AUTOSCROLL_MS, self._autoscroll_tick)

    def wheel(self, notches):                          # notches > 0 = scroll up (may be fractional)
        if self._dropdown or self._filter:
            step = int(notches) or (1 if notches > 0 else -1)   # discrete list: non-zero int rows
            (self._dropdown_scroll if self._dropdown else self._filter_scroll)(-step)
            return
        self._scroll_smooth(dy=-notches * self._WHEEL_ROWS * self.geom.row_h)

    def _overlay_open(self):
        return self._dropdown or self._filter or self._editor or self._find

    def double(self, x, y):
        if self._editor and self._editor_double(x, y):  # dbl-click in the open editor = select word
            return
        if self._overlay_open():                       # modal: don't fall through to the grid
            self.press(x, y, False, False); return
        self.ctl.on_double(x, y)

    def leave(self):            self.ctl.set_corner_hover(False)
    def toggle_fullscreen(self): self.host.fullscreen_toggle()

    # --- smooth zoom (Ctrl+wheel) -----------------------------------------
    _ZOOM_EASE = 0.75        # fraction of the remaining gap consumed each frame (high = snappy)
    _ZOOM_MS = 6             # animation timer period (~165 Hz) for a tight, fast glide
    _ZOOM_SNAP = 0.004       # within this of target -> land exactly and stop

    def zoom(self, factor):
        # Accumulate onto the live target (successive notches compound) instead of
        # snapping ctl._zoom, so a burst glides to the final level in one motion.
        base = self._zoom_to if self._zoom_to is not None else self.ctl._zoom
        self._zoom_to = max(0.4, min(4.0, base * factor))
        if self._zoom_after is None:
            self._zoom_after = self.host.after(self._ZOOM_MS, self._zoom_anim_tick)

    def _zoom_anim_tick(self):
        self._zoom_after = None
        if self._zoom_to is None:
            return
        cur = self.ctl._zoom
        gap = self._zoom_to - cur
        if abs(gap) <= self._ZOOM_SNAP:
            self.ctl.zoom_to(self._zoom_to)
            self._zoom_to = None
            return
        self.ctl.zoom_to(cur + gap * self._ZOOM_EASE)
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
        if self._overlay_open():                       # modal: no grid menu behind the overlay
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
    # Tuned for "slower top speed, but glassy": a notch nudges the target a couple
    # rows, easing eats a fraction of the gap per ~90fps frame, and both the target
    # lead and the per-frame move are capped so a fast spin can't fling or lag.
    _WHEEL_ROWS = 2.0        # rows per wheel notch added to the vertical target (was 3, hard-snap)
    _HWHEEL_PX = 48.0        # px per notch for shift-wheel / trackpad-x
    _SCROLL_EASE = 0.30      # fraction of the remaining gap consumed each animation frame
    _SCROLL_MS = 11          # animation timer period (~90 Hz)
    _SCROLL_SNAP = 0.5       # px: within this of target -> land exactly and stop
    _SCROLL_MAX_FRAC = 0.22  # per-frame move cap, as a fraction of the viewport (caps top speed)
    _SCROLL_LEAD_FRAC = 1.1  # target may lead the live position by at most this * viewport

    def _scroll_px(self, dx):                          # shift-wheel / trackpad horizontal
        self._scroll_smooth(dx=float(dx) / 40.0 * self._HWHEEL_PX)

    def _scroll_smooth(self, dx=0.0, dy=0.0):
        if self._dropdown:
            self._close_dropdown(redraw=False)
        g = self.geom
        g.clamp(self.model.nrows())
        live = (g.scroll_x, g.scroll_y)
        # (Re)seed from the live position on the first notch or after any non-wheel
        # scroll moved the view out from under us, so the glide starts where we are.
        # Stamp _scroll_last here too, so successive notches in the same burst ACCUMULATE
        # onto the target instead of each re-seeding back to the (unmoved) live position.
        if self._scroll_to is None or self._scroll_last != live:
            self._scroll_pos = [float(g.scroll_x), float(g.scroll_y)]
            self._scroll_to = [float(g.scroll_x), float(g.scroll_y)]
            self._scroll_last = live
        view_w = max(1, g.w - g.freeze_x())
        view_h = max(1, g.h - g.header_h)
        t = self._scroll_to
        t[0] = min(max(0.0, t[0] + dx), g.max_scroll_x())
        t[1] = min(max(0.0, t[1] + dy), g.max_scroll_y(self.model.nrows()))
        # cap how far the target can run ahead of the live position: kills the "keeps
        # scrolling long after I stopped" backlog that reads as lag on a fast spin.
        lead_x, lead_y = self._SCROLL_LEAD_FRAC * view_w, self._SCROLL_LEAD_FRAC * view_h
        t[0] = min(max(t[0], g.scroll_x - lead_x), g.scroll_x + lead_x)
        t[1] = min(max(t[1], g.scroll_y - lead_y), g.scroll_y + lead_y)
        if self._scroll_after is None:
            self._scroll_after = self.host.after(self._SCROLL_MS, self._scroll_anim_tick)

    def _scroll_anim_tick(self):
        self._scroll_after = None
        if self._scroll_to is None:
            return
        g = self.geom
        if self._scroll_last is not None and (g.scroll_x, g.scroll_y) != self._scroll_last:
            self._scroll_to = None; return          # external scroll took over -> yield
        view_w = max(1, g.w - g.freeze_x())
        view_h = max(1, g.h - g.header_h)
        pos, tgt = self._scroll_pos, self._scroll_to

        def ease(cur, target, view):
            gap = target - cur
            if abs(gap) <= self._SCROLL_SNAP:
                return target, True
            step = gap * self._SCROLL_EASE
            cap = self._SCROLL_MAX_FRAC * view
            return cur + max(-cap, min(cap, step)), False

        pos[0], dx_done = ease(pos[0], tgt[0], view_w)
        pos[1], dy_done = ease(pos[1], tgt[1], view_h)
        g.scroll_x = max(0, int(round(pos[0])))
        g.scroll_y = max(0, int(round(pos[1])))
        g.clamp(self.model.nrows())
        self._scroll_last = (g.scroll_x, g.scroll_y)
        self.redraw()                               # one frame per tick (not coalesced) for smoothness
        if dx_done and dy_done:
            self._scroll_to = self._scroll_pos = None
            return
        self._scroll_after = self.host.after(self._SCROLL_MS, self._scroll_anim_tick)

    # --- custom scrollbars: drawn in the reserved right/bottom strips, thumb
    # brightens to the accent on hover/drag (grid vertical + horizontal). ---
    @staticmethod
    def _in_rect(r, x, y):
        return r is not None and r[0] <= x <= r[0] + r[2] and r[1] <= y <= r[1] + r[3]

    def _draw_scrollbars(self, cv, sw, sh):
        g, sbw = self.geom, self._sbw
        g.used_rows, g.used_cols = self.model.used_extent()   # trim blank overscroll from the thumb
        # vertical bar in PIXELS (matches the sub-row scroll_y) so the thumb glides
        n, view = g.row_extent(self.model.nrows()) * g.row_h, g.full_rows() * g.row_h
        cv.rect(g.w, 0, sbw, g.h, fill=_SB_TRACK)                     # vertical track
        vm = _sb_metrics(0, g.h, n, view, g.scroll_y)
        if vm:
            ts, tl = vm
            hot = self._vsb["hover"] or self._vsb["drag"]
            cv.rect(g.w + 2, ts, sbw - 4, tl, fill=_SB_THUMB_HOVER if hot else _SB_THUMB)
            self._vsb["thumb"] = (g.w, ts, sbw, tl)
        else:
            self._vsb["thumb"] = None
        cv.rect(0, g.h, g.w, sbw, fill=_SB_TRACK)                     # horizontal track
        total, avail = g.col_extent(), g.w - g.freeze_x()
        hm = _sb_metrics(0, g.w, max(1, total), avail, g.scroll_x)
        if hm:
            ts, tl = hm
            hot = self._hsb["hover"] or self._hsb["drag"]
            cv.rect(ts, g.h + 2, tl, sbw - 4, fill=_SB_THUMB_HOVER if hot else _SB_THUMB)
            self._hsb["thumb"] = (ts, g.h, tl, sbw)
        else:
            self._hsb["thumb"] = None
        cv.rect(g.w, g.h, sbw, sbw, fill=_SB_TRACK)                   # corner

    def _sb_press(self, x, y):
        """Grab a thumb (drag) or page on a track click. True if the click was on a bar."""
        g = self.geom
        vt, ht = self._vsb["thumb"], self._hsb["thumb"]
        if x >= g.w and y < g.h:                                      # vertical strip
            if self._in_rect(vt, x, y):
                self._vsb.update(drag=True, grab=y - vt[1])
            elif vt:                                                  # page by ~one screen
                self.geom.scroll_y += (g.full_rows() - 1) * g.row_h * (1 if y > vt[1] else -1)
                self.geom.clamp(self.model.nrows()); self.redraw()
            return True
        if y >= g.h and x < g.w:                                      # horizontal strip
            if self._in_rect(ht, x, y):
                self._hsb.update(drag=True, grab=x - ht[0])
            elif ht:
                self.geom.scroll_x += (g.w - g.freeze_x()) * (1 if x > ht[0] else -1)
                self.geom.clamp(self.model.nrows()); self.redraw()
            return True
        return x >= g.w or y >= g.h                                   # corner -> swallow

    def _sb_drag(self, x, y):
        g = self.geom
        if self._vsb["drag"] and self._vsb["thumb"]:
            tl = self._vsb["thumb"][3]
            self.geom.scroll_y = max(0, _sb_offset(y, self._vsb["grab"], 0, g.h, tl,
                                                   g.row_extent(self.model.nrows()) * g.row_h,
                                                   g.full_rows() * g.row_h))
            self.geom.clamp(self.model.nrows()); self.redraw(); return True
        if self._hsb["drag"] and self._hsb["thumb"]:
            tl = self._hsb["thumb"][2]
            total = g.col_extent()
            self.geom.scroll_x = max(0, _sb_offset(x, self._hsb["grab"], 0, g.w, tl,
                                                   max(1, total), g.w - g.freeze_x()))
            self.geom.clamp(self.model.nrows()); self.redraw(); return True
        return False

    def _sb_hover(self, x, y):
        """Update thumb hover flags. Return True if the pointer is over a scrollbar."""
        vh = self._in_rect(self._vsb["thumb"], x, y)
        hh = self._in_rect(self._hsb["thumb"], x, y)
        if vh != self._vsb["hover"] or hh != self._hsb["hover"]:
            self._vsb["hover"], self._hsb["hover"] = vh, hh
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
        if self._surf is not None:
            self._lib.gpu_detach(self._surf)
            self._surf = None


def _selftest():
    from ..core.geometry import Geometry
    from .model import GridModel
    from ..core.paint import paint
    from ..core.render import blit
    from types import SimpleNamespace

    # Autoscroll velocity curve (pure: reads only self.geom). Zero inside the body,
    # accelerates the further past an edge, capped, and correctly signed.
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
        print("%s not built. Run build.bat, then import from dist\\fastpygrid"
              % os.path.basename(_lib_path()))
        return 1

    # A: color/decode correctness: one opaque rect, read the pixel back exactly.
    cv = GpuCanvas(13)
    cv.rect(0, 0, 60, 40, fill="#3366cc")
    got = lib.gpu_probe_pixel(bytes(cv.buf), len(cv.buf), 64, 48, 10, 10)
    assert got == 0xFF3366CC, "rect color: got %08X want FF3366CC" % got

    # A2: filled polygon -> probe the interior. Exercises the GL stencil concave-fill
    # path (a 'VERIFY' spot).
    cv = GpuCanvas(13)
    cv.poly([(10, 10), (50, 10), (30, 50)], "#22cc44")     # triangle
    got = lib.gpu_probe_pixel(bytes(cv.buf), len(cv.buf), 64, 64, 30, 25)   # strictly inside
    assert got == 0xFF22CC44, "poly fill: got %08X want FF22CC44" % got

    # A3: text path. Solid block glyph (U+2588) white on black -- a point near the
    # cell center must read back near-white. Exercises the GL glyph atlas (pack +
    # batched textured quads). Scan a few points so the check doesn't hinge on exact
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

    # A4: glyph-atlas overflow recovery (the GL zoom-in "text vanishes" bug). Force the
    # atlas to overflow within ONE frame -- many large distinct glyphs -- then draw a
    # solid block LAST. It lands in the freshly-rebuilt atlas, so it must still render;
    # if overflow silently dropped glyphs, this probes black.
    cv = GpuCanvas(200)
    for k in range(80):
        cv.text(0, 0, 40, 40, chr(0x21 + k), "#ffffff")     # 80 big distinct glyphs -> overflow
    cv.rect(0, 0, 320, 320, fill="#000000")                 # cover the fillers: probe tests the block alone
    cv.text(40, 40, 240, 240, "█", "#ffffff", center=True)  # wide cell (no ellipsis-trim); drawn last
    ob = bytes(cv.buf)
    assert any(_near_white(ob, 320, 320, px, py) for px in (150, 160, 170) for py in (150, 160, 170)), \
        "glyph vanished after atlas overflow/rebuild"

    # B: full pipeline: real model through paint()->blit()->GpuCanvas. Probe a
    # data-cell interior, it must be painted (not the black clear sentinel).
    model = GridModel(["A", "B", "C"], [["x", "y", "z"], ["1", "2", "3"]])
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
