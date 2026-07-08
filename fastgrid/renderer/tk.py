"""Tk renderer: blit the core display list with native Canvas items and wire
tkinter events into the shared GridController. NO Pillow -- only the ~visible
cells become Canvas items (virtualized), so 100k rows stay smooth. All
layout/colour/behaviour lives in core; this file is "draw these tuples, build
the widgets, translate tk events".
"""
import tkinter as tk
from tkinter import font as tkfont, simpledialog

from ..core import theme as T
from ..core.filter import FilterController
from ..core.find import FindController
from ..core.geometry import Geometry
from ..core.gridcontroller import GridController
from ..core.paint import paint, edit_colors
from ..core.render import blit

# Chrome for the floating Tk widgets (filter popup, find bar, toolbar) — kept
# light/readable and decoupled from the dark cell palette in core.theme.
_UI_BG = "#ece7dd"


_FIT = {}   # (font-name, text, px) -> clipped text. tkfont.Font is unhashable (3.8),
            # so key on its stable Tcl name; different fonts/DPI don't collide.


def _fit(font, txt, px):
    """Clip `txt` to `px` with an ellipsis. Memoized: Tk's font.measure()
    round-trips to Tcl (the slowest thing in the redraw loop) and the same
    (font, text, width) recurs constantly while scrolling."""
    key = (str(font), txt, px)
    r = _FIT.get(key)
    if r is None:
        if font.measure(txt) <= px:
            r = txt
        else:
            while txt and font.measure(txt + "…") > px:
                txt = txt[:-1]
            r = txt + "…"
        if len(_FIT) > 16384:              # bounded; scrolling reuses a small hot set
            _FIT.clear()
        _FIT[key] = r
    return r


def _enable_dpi_awareness():
    """Windows: make the process DPI-aware so Tk renders sharp instead of being
    bitmap-stretched (blurry) by the OS on a hi-DPI display. No-op elsewhere."""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)     # per-monitor v2
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()          # older Windows
    except Exception:
        pass


def _screen_scale(win):
    """DPI scale factor (1.0 at 96 DPI, 1.5 at 150%, …)."""
    try:
        import ctypes
        return max(1.0, ctypes.windll.user32.GetDpiForSystem() / 96.0)
    except Exception:
        try:
            return max(1.0, win.winfo_fpixels("1i") / 96.0)
        except Exception:
            return 1.0


class TkCanvas:
    """Tk backend for core.render.blit -- one Canvas item per primitive. Stroke
    widths scale with DPI so grid lines/rings stay crisp (identical at 100%).

    Native-scroll hooks (TkGrid drives them; default off = plain full-frame blit):
      _dy    -- pixels added to every y, so overlays land over the yview-scrolled
                body (world_y = screen_y + canvasy(0)).
      _tag   -- extra tag on every item (e.g. "ov" so a scroll can delete+redraw
                just the overlays without touching the blitted body band).
      _pin_h -- tag items lying wholly inside [0, _pin_h) (the header band) as
                "pinned" so the body can scroll under them while they stay put."""

    def __init__(self, canvas, font, hfont, scale):
        self.c, self.font, self.hfont, self.s = canvas, font, hfont, scale
        # Hot cell primitives (rect/text) call Tcl directly, bypassing tkinter's
        # per-item _options/_cnfmerge option-dict merge -- that Python overhead, not
        # the Tcl round-trip, dominates create_* (~70% of each call). Same item ids.
        self._call, self._getint, self._wname = canvas.tk.call, canvas.tk.getint, canvas._w
        self._family = font.actual("family")   # one Tcl round-trip, not one per glyph/frame
        self._glyph_fonts = {}                  # px -> (family, size) tuple, reused per frame
        self._dy = 0
        self._tag = None
        self._pin_h = None
        self._collect = None        # if a list, every rect id is appended (cell->item map)

    def _post(self, item, ytop, ybot):
        if self._tag:
            self.c.addtag_withtag(self._tag, item)
        if self._pin_h is not None and ytop >= 0 and ybot <= self._pin_h + 1:
            self.c.addtag_withtag("pinned", item)

    def rect(self, x, y, w, h, fill=None, outline=None, width=1):
        y += self._dy
        it = self._getint(self._call(self._wname, "create", "rectangle", x, y, x + w, y + h,
                                     "-fill", fill or "", "-outline", outline or "",
                                     "-width", max(1, round(width * self.s))))
        if self._collect is not None:
            self._collect.append(it)
        self._post(it, y, y + h)

    def text(self, x, y, w, h, s, color, bold=False, center=False):
        f = self.hfont if bold else self.font
        y += self._dy
        if center:
            cx, cy, anchor, txt = x + w / 2, y + h / 2, "center", _fit(f, s, w - 6)
        else:
            cx, cy, anchor, txt = x + 5, y + h / 2, "w", _fit(f, s, w - 9)
        it = self._getint(self._call(self._wname, "create", "text", cx, cy, "-anchor", anchor,
                                     "-fill", color, "-font", f.name, "-text", txt))
        self._post(it, y, y + h)

    def line(self, x1, y1, x2, y2, color, width):
        y1 += self._dy; y2 += self._dy
        it = self.c.create_line(x1, y1, x2, y2, fill=color, width=max(1, round(width * self.s)))
        self._post(it, min(y1, y2), max(y1, y2))

    def poly(self, points, color):
        pts = [(px, py + self._dy) for px, py in points]
        it = self.c.create_polygon(*[v for p in pts for v in p], fill=color, outline=color)
        ys = [py for _px, py in pts]
        self._post(it, min(ys), max(ys))

    def glyph(self, cx, cy, s, color, px):
        cy += self._dy
        key = -max(7, round(px))
        f = self._glyph_fonts.get(key)
        if f is None:
            f = self._glyph_fonts[key] = (self._family, key)
        it = self.c.create_text(cx, cy, anchor="center", text=s, fill=color, font=f)
        self._post(it, cy, cy)


class TkGrid(tk.Frame):
    def __init__(self, master, model, editable=True, frozen=0, col_w=None, scale=1.0, **kw):
        super().__init__(master, **kw)
        self.model = model
        self.editable = editable
        # Everything on the Canvas is in pixels, so scale the geometry + use
        # pixel-sized fonts (negative size) to stay crisp and physically sized on
        # a hi-DPI display (the process is made DPI-aware in make_sheet).
        s = self._scale = scale
        fpx = max(9, round(13 * s))
        self.font = tkfont.Font(family="Segoe UI", size=-fpx)
        self.hfont = tkfont.Font(family="Segoe UI", size=-fpx, weight="bold")
        widths = [max(24, round(w * s)) for w in (col_w or [120] * model.ncols)]
        self.geom = Geometry(widths, frozen, gutter_w=max(28, round(56 * s)),
                             row_h=max(14, round(22 * s)))
        self._base_fpx = 13 * s            # zoom scales pixel fonts off this
        self.ctl = GridController(self, base_row_h=22 * s, base_gutter=56 * s, base_w=widths)
        self._editor = None
        # Native-scroll cache: a materialised band of rows [B0, B1) is blitted once
        # in WORLD-Y coords; vertical scroll then moves through it with the canvas'
        # own yview (a C-level pixel blit -- the whole point) instead of repainting
        # every text item. _OVER rows of overscan each side; rebuild when scrolling
        # within _MARGIN of a band edge. Horizontal scroll / edits / selection go
        # through the full repaint (redraw), which just rebuilds the band.
        self._OVER, self._MARGIN = 24, 3
        self._band = None            # (B0, B1) currently materialised, or None
        self._pin_at = 0.0           # world-y the "pinned" header items were last moved to
        self._bandpix = 0            # scrollregion height of the current band
        # Incremental selection: dragging a selection changes only which cells are
        # washed, so we keep the cell rects and just re-fill the few that flipped
        # instead of repainting the whole viewport. Valid only while the viewport
        # (rows/cols/size) is unchanged since the last full paint.
        self._cell_rects = None      # rect item ids aligned with the last paint's cells
        self._cell_bgs = None        # their current fill colours
        self._paint_key = None       # viewport signature the map was built for
        # Scroll coalescing: the scrollbar/wheel fire a repaint per motion event
        # (~100/s), each far slower than the event rate, so painting every one
        # queues a backlog that lags the thumb. Instead we stash the latest paint
        # and run it once on after_idle -- always the newest position, never a queue.
        self._paint_pending = False
        self._next_paint = None

        self.canvas = tk.Canvas(self, highlightthickness=0, bg=T.BG)
        self.vbar = tk.Scrollbar(self, orient="vertical", command=self._on_vscroll)
        self.hbar = tk.Scrollbar(self, orient="horizontal", command=self._on_hscroll)
        self._cv = TkCanvas(self.canvas, self.font, self.hfont, self._scale)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vbar.grid(row=0, column=1, sticky="ns")
        self.hbar.grid(row=1, column=0, sticky="ew")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.find = FindBar(self)
        model.changed = self.redraw

        c = self.canvas
        c.bind("<Configure>", lambda e: self.redraw())
        c.bind("<Button-1>", self._on_press)
        c.bind("<B1-Motion>", lambda e: self.ctl.on_drag(e.x, e.y))
        c.bind("<ButtonRelease-1>", lambda e: self.ctl.on_release())
        c.bind("<Double-Button-1>", lambda e: self.ctl.on_double(e.x, e.y))
        c.bind("<Button-3>", self._on_context)
        c.bind("<MouseWheel>", lambda e: self._scroll_rows(-(e.delta // 120) * 3))
        c.bind("<Shift-MouseWheel>", lambda e: self._scroll_px(-(e.delta // 120) * 40))
        c.bind("<Control-MouseWheel>", lambda e: self.ctl.zoom_by(1.1 if e.delta > 0 else 1 / 1.1))
        c.bind("<Motion>", lambda e: self.ctl.on_motion(e.x, e.y))
        c.bind("<Leave>", lambda e: self.ctl.set_corner_hover(False))
        c.bind("<Key>", self._on_key)
        try:                                                   # X11 horizontal wheel
            c.bind("<Button-6>", lambda e: self._scroll_px(-40))
            c.bind("<Button-7>", lambda e: self._scroll_px(40))
        except tk.TclError:                                    # not a valid event on Windows
            pass
        c.configure(takefocus=1)
        c.focus_set()
        # No trackpad horizontal-swipe scroll -- Tk 8.6 drops WM_MOUSEHWHEEL and the
        # only way to catch it is a fragile Win32 hook. Shift+wheel, the scrollbar,
        # and X11 Button-6/7 cover horizontal scroll instead.

    # --- render -------------------------------------------------------
    def redraw(self):
        """Full repaint of the visible viewport in plain screen coords -- the path
        for everything that ISN'T a vertical wheel/scrollbar scroll (selection,
        drag, edit, filter, zoom, horizontal scroll). Light (no band / overscan /
        yview): paints just what's on screen so dragging a selection stays fast. It
        resets to yview=0 and invalidates the band, so the next vertical scroll rebuilds."""
        if not self.winfo_exists():
            return
        g = self.geom
        g.w, g.h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if g.w < 2 or g.h < 2:
            return
        g.clamp(self.model.nrows())
        # Mid-drag with an unchanged viewport: recolour only the flipped cells.
        if self.ctl.drag_region is not None and self._paint_key == self._vp_key():
            self._update_selection()
            return
        self.canvas.delete("all")
        self.canvas.yview_moveto(0)                     # unscrolled: screen == world
        self.canvas.configure(scrollregion=(0, 0, g.w, g.h))
        dl = paint(self.model, g, self.ctl.active, self.ctl.ranges(), self.ctl.corner_hover)
        rings = [ov for ov in dl.overlays if ov[0] == "ring"]     # selection outline edges
        chrome = [ov for ov in dl.overlays if ov[0] != "ring"]    # divider, filter btns, corner
        dl.overlays = []
        self._cv._collect = rects = []
        blit(dl, self._cv)                              # cells only; capture their rect ids
        self._cv._collect = None
        self._cell_rects = rects[1:1 + len(dl.cells)]   # rects[0] is the grid backing
        self._cell_bgs = [c[5] for c in dl.cells]
        self._paint_key = self._vp_key()
        dl.cells = []
        dl.overlays = chrome
        blit(dl, self._cv)                              # static chrome
        dl.overlays = rings
        self._cv._tag = "dragov"                        # rings tagged so a drag can refresh them
        blit(dl, self._cv)
        self._cv._tag = None
        self._band = None                               # invalidate the native-scroll band
        self._sync_bars()
        self._place_editor()

    def _vp_key(self):
        """Signature of everything that fixes cell positions/contents-layout; when
        it is unchanged, the cell->item map from the last paint is still valid."""
        g = self.geom
        return (g.top_row, g.scroll_x, g.row_h, g.gutter_w, g.w, g.h, tuple(g.col_w))

    def _update_selection(self):
        """Fast drag repaint: re-fill only the cells whose wash flipped and redraw
        the rings -- a tiny dirty area vs deleting+recreating the whole viewport."""
        dl = paint(self.model, self.geom, self.ctl.active, self.ctl.ranges(),
                   self.ctl.corner_hover)
        if len(dl.cells) != len(self._cell_rects):      # viewport drifted -> full repaint
            self._paint_key = None
            self.redraw()
            return
        cfg, rects, bgs = self.canvas.itemconfigure, self._cell_rects, self._cell_bgs
        for i, cl in enumerate(dl.cells):
            if cl[5] != bgs[i]:
                cfg(rects[i], fill=cl[5])
                bgs[i] = cl[5]
        self.canvas.delete("dragov")
        dl.cells = []
        dl.overlays = [ov for ov in dl.overlays if ov[0] == "ring"]
        self._cv._tag = "dragov"
        blit(dl, self._cv)
        self._cv._tag = None

    def _rebuild_band(self):
        """Blit viewport+overscan rows as one band in WORLD-Y coords (row B0 at
        header_h) so a native vertical scroll can yview-blit through it. Cells +
        static chrome (header, filter buttons, corner, frozen divider) go in the
        band; only the selection rings are redrawn per scroll (by _blit_overlays)
        since they must clip to the viewport. Only the scroll path calls this."""
        g, m = self.geom, self.model
        n = m.nrows()
        vis = g.vis_rows()
        B0 = max(1, g.top_row - self._OVER)
        B1 = min(n, g.top_row + vis + self._OVER)
        self.canvas.delete("all")
        self._paint_key = None                          # cell->item map is gone (world-Y now)
        real_top = g.top_row
        g.top_row = B0                                  # paint the band as if B0 were on top
        self._cv._pin_h = g.header_h                    # tag the header band -> "pinned"
        dl = paint(m, g, self.ctl.active, self.ctl.ranges(), self.ctl.corner_hover,
                   row_range=(B0, B1))
        # rings redraw per-scroll; the rest is static chrome painted once.
        dl.overlays = [ov for ov in dl.overlays if ov[0] in ("filterbtn", "tri")]
        blit(dl, self._cv)
        self._cv._pin_h = None
        g.top_row = real_top
        if g.frozen > 0:                                # frozen divider: pinned, full-viewport
            it = self.canvas.create_line(g.freeze_x(), 0, g.freeze_x(), g.h,
                                         fill=T.DIVIDER, width=max(1, round(self._scale)))
            self.canvas.addtag_withtag("pinned", it)
        self.canvas.tag_raise("pinned")                 # chrome stays above the scrolling body
        self._band = (B0, B1)
        self._pin_at = 0.0                              # painted at world[0, header_h)
        self._bandpix = g.header_h + (B1 - B0) * g.row_h

    def _blit_overlays(self):
        """Redraw just the selection/active rings at the current scroll position --
        two items, tiny dirty area. They ride +canvasy(0) so they land over the
        yview-scrolled body, and re-clip to the real viewport each frame."""
        c = self._cv
        self.canvas.delete("ov")
        dy = self.canvas.canvasy(0)                     # world-y currently at screen top
        dl = paint(self.model, self.geom, self.ctl.active, self.ctl.ranges(),
                   self.ctl.corner_hover)
        dl.cells = []
        dl.overlays = [ov for ov in dl.overlays if ov[0] == "ring"]
        c._dy, c._tag = dy, "ov"                        # offset rings onto the scrolled body
        blit(dl, c)
        c._dy, c._tag = 0, None

    def _apply_scroll(self):
        """Position the materialised band for the current top_row via native yview
        (the fast blit), re-pin the chrome, and refresh rings/scrollbars."""
        g = self.geom
        B0 = self._band[0]
        yoff = (g.top_row - B0) * g.row_h
        self.canvas.configure(scrollregion=(0, 0, g.w, self._bandpix))
        self.canvas.yview_moveto(yoff / max(1, self._bandpix))
        actual = self.canvas.canvasy(0)                 # exact landed offset (px-rounded)
        d = actual - self._pin_at
        if d:
            self.canvas.move("pinned", 0, d)            # keep chrome fixed as the body blits
            self._pin_at = actual
        self._blit_overlays()
        self._sync_bars()
        self._place_editor()

    def _need_rebuild(self):
        """True when the viewport is within _MARGIN rows of a band edge (and there
        are more rows that way) -- i.e. a native scroll would run off the band."""
        if self._band is None:
            return True
        B0, B1 = self._band
        g, n = self.geom, self.model.nrows()
        top, bot = g.top_row, g.top_row + g.vis_rows()
        if top - B0 < self._MARGIN and B0 > 1:
            return True
        if B1 - bot < self._MARGIN and B1 < n:
            return True
        return not (B0 <= top and bot <= B1)

    def _scroll_v(self):
        """Fast vertical-scroll path: reuse the band when possible (native yview
        blit), rebuild only when it runs out."""
        if not self.winfo_exists():
            return
        g = self.geom
        g.w, g.h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if g.w < 2 or g.h < 2:
            return
        g.clamp(self.model.nrows())
        if self._need_rebuild():
            self._rebuild_band()
        self._apply_scroll()

    def _coalesce(self, fn):
        """Run `fn` (a repaint) once on the next idle, collapsing a burst of
        scroll events into a single paint at the latest position."""
        self._next_paint = fn
        if not self._paint_pending:
            self._paint_pending = True
            self.after_idle(self._flush_paint)

    def _flush_paint(self):
        self._paint_pending = False
        fn, self._next_paint = self._next_paint, None
        if fn:
            fn()

    # --- GridController host surface ----------------------------------
    def after_scroll_change(self):   pass    # redraw() re-syncs the scrollbars
    def after_geometry_change(self): pass
    def set_edge_cursor(self, on_edge):
        self.canvas.configure(cursor="sb_h_double_arrow" if on_edge else "")
    def reveal_find(self):
        self.find.reveal()
    def open_filter_popup(self, col, at):
        FilterPopup(self, col, *at)
    def measure(self, text, bold):
        return (self.hfont if bold else self.font).measure(text)
    def set_zoom_fonts(self, z):
        fpx = max(9, round(self._base_fpx * z))
        self.font.configure(size=-fpx)
        self.hfont.configure(size=-fpx)
    def clipboard_set(self, text):
        self.clipboard_clear(); self.clipboard_append(text)
    def clipboard_get(self):             # tk's raises TclError on an empty clipboard
        try:
            return tk.Frame.clipboard_get(self)
        except tk.TclError:
            return ""

    # --- scrollbars ---------------------------------------------------
    def _sync_bars(self):
        g, nrows = self.geom, self.model.nrows()
        full = g.full_rows()
        self.vbar.set(g.top_row / max(1, nrows),
                      min(1, (g.top_row + full) / max(1, nrows)))
        total = g.content_w() - g.frozen_w()
        avail = g.w - g.freeze_x()
        self.hbar.set(g.scroll_x / max(1, total),
                      min(1, (g.scroll_x + avail) / max(1, total)))

    def _scroll_rows(self, dr):
        self.geom.top_row += dr
        self._coalesce(self._scroll_v)          # small step -> native band blit

    def _scroll_px(self, dx):
        self.geom.scroll_x = max(0, self.geom.scroll_x + dx)
        self.geom.clamp(self.model.nrows())
        self._coalesce(self.redraw)

    def _on_vscroll(self, *a):
        if a[0] == "moveto":
            # Thumb drag jumps far each event, so the band's overscan is wasted
            # work -- a plain viewport paint is cheaper than rebuilding the band.
            self.geom.top_row = int(float(a[1]) * self.model.nrows())
            self._coalesce(self.redraw)
        else:
            self.geom.top_row += int(a[1]) * (3 if a[2] == "units" else self.geom.full_rows() - 1)
            self._coalesce(self._scroll_v)      # arrow/page: small step -> band blit

    def _on_hscroll(self, *a):
        if a[0] == "moveto":
            self.geom.scroll_x = int(float(a[1]) * (self.geom.content_w() - self.geom.frozen_w()))
        else:
            self.geom.scroll_x += int(a[1]) * 40
        self._coalesce(self.redraw)

    # --- events -> controller -----------------------------------------
    def _on_press(self, e):
        self.canvas.focus_set()
        self.ctl.on_press(e.x, e.y, bool(e.state & 0x4), bool(e.state & 0x1),
                          (e.x_root, e.y_root))

    def _on_context(self, e):
        self.canvas.focus_set()
        self.ctl.context_select(e.x, e.y)
        m = tk.Menu(self, tearoff=0)
        st = "normal" if self.editable else "disabled"
        m.add_command(label="Copy", command=self.ctl.copy)
        m.add_command(label="Cut", command=self.ctl.cut, state=st)      # no-op on read-only
        m.add_command(label="Paste", command=self.ctl.paste, state=st)
        m.add_command(label="Delete", command=self.ctl.delete, state=st)
        m.tk_popup(e.x_root, e.y_root)

    def _on_key(self, e):
        key = {"BackSpace": "Delete", "KP_Enter": "Return"}.get(e.keysym, e.keysym)
        if len(key) == 1:
            key = key.lower()
        handled = self.ctl.on_key(key, bool(e.state & 0x1), bool(e.state & 0x4), e.char)
        return "break" if handled else None

    # --- editor -------------------------------------------------------
    def begin_edit(self, initial=None):
        if not self.editable:
            return
        self.commit_editor()
        r, c = self.ctl.active
        bg, fg = edit_colors(r)            # keep the cell's own background + text colour
        var = tk.StringVar(value=initial if initial is not None else self.model.cell(r, c))
        ed = tk.Entry(self.canvas, textvariable=var, relief="solid", bd=1,
                      font=self.font, bg=bg, fg=fg, insertbackground=fg,
                      highlightthickness=1, highlightcolor=T.ACCENT)
        ed._cell, ed._var = (r, c), var
        self._editor = ed
        ed.bind("<Return>", lambda e: self.commit_editor(move=(1, 0)))
        ed.bind("<Down>", lambda e: (self.commit_editor(move=(1, 0)), "break")[1])
        ed.bind("<Up>", lambda e: (self.commit_editor(move=(-1, 0)), "break")[1])
        ed.bind("<Tab>", lambda e: (self.commit_editor(move=(0, 1)), "break")[1])
        ed.bind("<Escape>", lambda e: self._cancel_editor())
        self._place_editor()
        ed.focus_set()
        ed.icursor("end")

    def _place_editor(self):
        ed = self._editor
        if not ed:
            return
        r, c = ed._cell
        if not self.geom.cell_visible(r, c) or self.geom.col_x(c) < self.geom.gutter_w:
            ed.place_forget(); return
        ed.place(x=self.geom.col_x(c), y=self.geom.row_y(r),
                 width=self.geom.col_w[c] + 1, height=self.geom.row_h_at(r) + 1)

    def commit_editor(self, move=None):
        ed = self._editor
        if not ed:
            return
        r, c = ed._cell
        self.model.set_cell(r, c, ed._var.get())
        ed.destroy()
        self._editor = None
        self.canvas.focus_set()        # return focus so the next keystroke edits again
        if move:
            self.ctl.active = self.ctl.anchor = (r, c)
            self.ctl.move(move)
        else:
            self.redraw()

    def _cancel_editor(self):
        if self._editor:
            self._editor.destroy()
            self._editor = None
            self.canvas.focus_set()
            self.redraw()


# ---------------------------------------------------------------------
# Toolkit widgets: filter popup + find bar. The behaviour they drive lives in
# core (FilterController / FindController); these are just the Tk widgets.
# ---------------------------------------------------------------------
CHECK, UNCHECK = "☑", "☐"


class FilterPopup(tk.Toplevel):
    def __init__(self, grid, col, x_root, y_root):
        super().__init__(grid)
        self.g, self.model = grid, grid.model
        self.ctl = FilterController(grid.model, col)
        self.overrideredirect(True)
        self.configure(bg=_UI_BG, bd=1, relief="solid")
        self.geometry("+%d+%d" % (x_root, y_root))

        self._btn("Sort A → Z", lambda: self._sort(True))
        self._btn("Sort Z → A", lambda: self._sort(False))
        tk.Frame(self, height=1, bg=T.GRID).pack(fill="x", padx=4, pady=3)
        self._btn("Clear Filter", self._clear, self.model.has_filter(col))
        self._btn("Contains…", lambda: self._text("contains"))
        self._btn("Equals…", lambda: self._text("equals"))
        tk.Frame(self, height=1, bg=T.GRID).pack(fill="x", padx=4, pady=3)
        self.search = tk.Entry(self, width=32)
        self.search.pack(padx=6, pady=(2, 4))
        self.search.bind("<KeyRelease>",
                         lambda e: None if e.keysym in ("Return", "KP_Enter") else self._repopulate())
        self.search.bind("<Return>", lambda e: self._apply())
        self.search.bind("<KP_Enter>", lambda e: self._apply())
        self.lst = tk.Listbox(self, width=34, height=12, activestyle="none",
                              exportselection=False, highlightthickness=0)
        self.lst.pack(padx=6)
        self.lst.bind("<Button-1>", self._toggle)
        self.lst.insert("end", "  loading…")
        foot = tk.Frame(self, bg=_UI_BG)
        foot.pack(fill="x", padx=6, pady=6)
        tk.Button(foot, text="Cancel", command=self.destroy).pack(side="right")
        tk.Button(foot, text="OK", command=self._apply).pack(side="right", padx=4)
        self.bind("<Escape>", lambda e: self.destroy())
        # Show now; do the (possibly heavy) distinct scan on the next tick so the
        # window pops instantly instead of blocking on a 1M-row column.
        self.after(1, self._load)
        self.after(10, lambda: (self.focus_force(), self.search.focus_set()))

    def _load(self):
        if not self.winfo_exists():
            return
        self.ctl.load()
        self._repopulate()

    def _btn(self, text, cmd, enabled=True):
        tk.Button(self, text=text, command=cmd, anchor="w", relief="flat",
                  bg=_UI_BG, fg=T.TXT, activebackground="#ddd6c8",
                  state="normal" if enabled else "disabled").pack(fill="x", padx=2)

    def _repopulate(self):
        if self.ctl.state is None:         # still loading the distinct scan
            return
        self.lst.delete(0, "end")
        rows = self.ctl.rows(self.search.get())
        self.lst.insert("end", "%s (Select all)" % (CHECK if self.ctl.all_on(rows) else UNCHECK))
        for v in rows:
            self.lst.insert("end", "%s %s" % (CHECK if self.ctl.checked(v) else UNCHECK,
                                              v if v else "(blank)"))
        if self.ctl.truncated(rows):       # list truncated -> tell the user to narrow
            self.lst.insert("end", "  … too many to list — type to search")
            self.lst.itemconfig("end", fg="#8a8578")

    def _toggle(self, e):
        if self.ctl.state is None:
            return "break"
        i = self.lst.nearest(e.y)
        rows = self.ctl.rows(self.search.get())
        if i == 0:
            self.ctl.toggle_all(rows)
        elif 1 <= i <= len(rows):
            self.ctl.toggle(rows[i - 1])
        self._repopulate()
        return "break"

    def _sort(self, asc):
        self.model.set_sort(self.ctl.col, asc); self.destroy()

    def _clear(self):
        self.model.clear_column_filter(self.ctl.col); self.destroy()

    def _text(self, op):
        val = simpledialog.askstring("Text filter", op.capitalize() + ":", parent=self)
        if val is not None:
            self.model.set_text_filter(self.ctl.col, op, val); self.destroy()

    def _apply(self):
        if self.ctl.state is None:         # OK before the list loaded = no-op
            self.destroy(); return
        self.ctl.commit(self.search.get())
        self.destroy()


class FindBar(tk.Frame):
    """Tk find bar -- a thin widget over the shared core FindController."""

    def __init__(self, grid):
        super().__init__(grid.canvas, bg=_UI_BG, bd=2, relief="solid",
                         highlightbackground=T.ACCENT, highlightthickness=2)
        self.g = grid
        self.ctl = FindController(grid.ctl)
        self.ctl.on_count = lambda text: self.count.configure(text=text)
        self._case = self._scope_on = False
        self._after = None
        self.entry = tk.Entry(self, width=22, bd=0, bg="#fbf9f4")
        self.entry.pack(side="left", padx=3, pady=2)
        self.entry.bind("<KeyRelease>", self._on_type)
        self.entry.bind("<Return>", lambda e: self.ctl.step(1))
        self.entry.bind("<Shift-Return>", lambda e: self.ctl.step(-1))
        self.entry.bind("<Escape>", lambda e: self.close())
        self.count = tk.Label(self, text="", width=9, bg=_UI_BG, fg=T.TXT)
        self.count.pack(side="left")
        for txt, d in (("‹", -1), ("›", 1)):
            tk.Button(self, text=txt, command=lambda d=d: self.ctl.step(d),
                      relief="flat", bg=_UI_BG).pack(side="left")
        self.case_btn = tk.Button(self, text="Aa", relief="flat", bg=_UI_BG, command=self._toggle_case)
        self.case_btn.pack(side="left")
        self.scope_btn = tk.Button(self, text="In", relief="flat", bg=_UI_BG, command=self._toggle_scope)
        self.scope_btn.pack(side="left")
        tk.Button(self, text="✕", command=self.close, relief="flat", bg=_UI_BG).pack(side="left")

    def reveal(self):
        has_scope = self.ctl.open(self.g.ctl.ranges())
        self._scope_on = has_scope
        self.scope_btn.configure(fg=T.ACCENT if has_scope else "#999",
                                 state="normal" if has_scope else "disabled")
        self.case_btn.configure(fg=T.ACCENT if self._case else "#999")
        self.place(relx=1.0, y=4, x=-6, anchor="ne")
        self.lift()
        self.entry.focus_set()
        self.entry.select_range(0, "end")

    def close(self):
        if self._after:
            self.after_cancel(self._after); self._after = None
        self.place_forget()
        self.ctl.close()
        self.g.canvas.focus_set()

    def _on_type(self, e):
        if e.keysym in ("Return", "Escape"):
            return
        if self._after:                                # 120ms keystroke debounce
            self.after_cancel(self._after)
        self._after = self.after(120, lambda: self.ctl.run(self.entry.get(), navigate=False))

    def _toggle_case(self):
        self._case = not self._case
        self.case_btn.configure(fg=T.ACCENT if self._case else "#999")
        self.ctl.set_case(self._case)

    def _toggle_scope(self):
        if self.ctl.scope_range is None:
            return
        self._scope_on = not self._scope_on
        self.scope_btn.configure(fg=T.ACCENT if self._scope_on else "#999")
        self.ctl.set_scope(self._scope_on)


def make_sheet(headers, rows, frozen=0, view_only=False, master=None,
               col_w=None, title="fastgrid (tk)"):
    """One-call editable sheet, Tk renderer."""
    if master is None:
        _enable_dpi_awareness()                # before the first Tk root exists
        win = tk.Tk()
    else:
        win = tk.Toplevel(master)
    win.title(title)
    scale = _screen_scale(win)
    try:
        win.tk.call("tk", "scaling", scale * 96 / 72)   # crisp point-based std widgets
    except tk.TclError:
        pass
    win.geometry("%dx%d" % (round(980 * scale), round(620 * scale)))
    from ..core.model import GridModel
    model = GridModel(headers, rows, editable=not view_only)
    bar = tk.Frame(win, bg=_UI_BG)
    bar.pack(fill="x")
    grid = TkGrid(win, model, editable=not view_only, frozen=frozen, col_w=col_w, scale=scale)
    grid.pack(fill="both", expand=True)
    tk.Button(bar, text="Clear filters", relief="flat", bg=_UI_BG, fg=T.TXT,
              command=model.clear_filters).pack(side="left", padx=6, pady=3)
    status = tk.Label(bar, bg=_UI_BG, fg=T.TXT, anchor="e")
    status.pack(side="right", padx=8)
    prev = model.changed
    model.changed = lambda: (prev(), status.configure(
        text="%d rows%s   ·   Ctrl+F find · ▼ filter" % (
            model._real_rows() - 1, "  ·  filtered" if model.any_filters() else "")))
    model.changed()
    win.model, win.grid_view = model, grid
    return win
