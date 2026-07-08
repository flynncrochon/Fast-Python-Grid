"""Tk renderer: blit the core display list with native Canvas items and wire
tkinter events. NO Pillow -- only the ~visible cells become Canvas items
(virtualized), so 100k rows stay smooth. All layout/colour lives in core; this
file is "draw these tuples + turn clicks into core calls".
"""
import tkinter as tk
from tkinter import font as tkfont, simpledialog

from ..core import selection as S, theme as T
from ..core.find import FindController
from ..core.geometry import Geometry
from ..core.paint import paint, edit_colors

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
        # Zoom state: metrics are recomputed from these DPI-scaled base values
        # times _zoom (never ratio-chained), so it never drifts or fights the
        # min-size floors. Manual column resizes write back to _base_w (see
        # _resize_to) so a later zoom keeps them proportional.
        self._zoom = 1.0
        self._base_fpx, self._base_row_h, self._base_gutter = 13 * s, 22 * s, 56 * s
        self._base_w = list(widths)

        self.active = (1, 0)               # start on the first DATA cell (row 0 = header)
        self.anchor = (1, 0)
        self.sel = (1, 0, 1, 0)
        self.extra = []
        self._drag_region = None
        self._editor = None
        self._corner_hover = False
        self._resize_col = None            # column being drag-resized, else None

        self.canvas = tk.Canvas(self, highlightthickness=0, bg=T.BG)
        self.vbar = tk.Scrollbar(self, orient="vertical", command=self._on_vscroll)
        self.hbar = tk.Scrollbar(self, orient="horizontal", command=self._on_hscroll)
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
        c.bind("<B1-Motion>", self._on_drag)
        c.bind("<ButtonRelease-1>", self._on_release)
        c.bind("<Double-Button-1>", self._on_double)
        c.bind("<MouseWheel>", lambda e: (self._scroll_rows(-(e.delta // 120) * 3)))
        c.bind("<Shift-MouseWheel>", lambda e: self._scroll_px(-(e.delta // 120) * 40))
        c.bind("<Control-MouseWheel>", lambda e: self._zoom_by(1.1 if e.delta > 0 else 1 / 1.1))
        c.bind("<Motion>", self._on_motion)
        c.bind("<Leave>", lambda e: self._set_corner_hover(False))
        c.bind("<Key>", self._on_key)
        try:                                                   # X11 horizontal wheel
            c.bind("<Button-6>", lambda e: self._scroll_px(-40))
            c.bind("<Button-7>", lambda e: self._scroll_px(40))
        except tk.TclError:                                    # not a valid event on Windows
            pass
        c.configure(takefocus=1)
        c.focus_set()
        # Tk 8.6 drops WM_MOUSEHWHEEL (trackpad horizontal) on Windows -- hook it in.
        self._hacc = 0
        self.after(0, self._install_hwheel)

    # --- render -------------------------------------------------------
    def redraw(self):
        if not self.winfo_exists():
            return
        g = self.geom
        g.w, g.h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if g.w < 2 or g.h < 2:
            return
        dl = paint(self.model, g, self.active, self._ranges(), self._corner_hover)
        c = self.canvas
        c.delete("all")
        for (x, y, w, h, text, bg, fg, flags) in dl.cells:
            c.create_rectangle(x, y, x + w, y + h, fill=bg, outline=T.GRID)
            if text:
                f = self.hfont if flags & T.FLAG_BOLD else self.font
                if flags & T.FLAG_CENTER:
                    c.create_text(x + w / 2, y + h / 2, anchor="center", fill=fg,
                                  font=f, text=self._clip(text, w - 6, f))
                else:
                    c.create_text(x + 5, y + h / 2, anchor="w", fill=fg, font=f,
                                  text=self._clip(text, w - 9, f))
        for ov in dl.overlays:
            k = ov[0]
            if k == "line":
                c.create_line(ov[1], ov[2], ov[3], ov[4], fill=ov[5],
                              width=max(1, round(ov[6] * self._scale)))
            elif k == "rect":
                c.create_rectangle(ov[1], ov[2], ov[1] + ov[3], ov[2] + ov[4],
                                   outline=ov[5], width=max(1, round(ov[6] * self._scale)))
            elif k == "filterbtn":
                self._draw_filter_btn(c, *ov[1:])
            elif k == "tri":
                x1, y1, sz, col = ov[1], ov[2], ov[3], ov[4]
                c.create_polygon(x1 - sz, y1, x1, y1 - sz, x1, y1, fill=col, outline=col)
        self._sync_bars()
        self._place_editor()

    def _clip(self, txt, px, f):
        return _fit(f, txt, px)

    def _draw_filter_btn(self, c, bx, by, sz, state):
        c.create_rectangle(bx, by, bx + sz, by + sz, fill=T.BTN_BG, outline=T.BTN_BORDER)
        if state == "funnel":                          # active filter -> amber funnel
            mx, t, n, b, fw, sw = bx + sz / 2, by + sz * 0.24, by + sz * 0.49, by + sz * 0.72, sz * 0.22, sz * 0.05
            c.create_polygon(mx - fw, t, mx + fw, t, mx + sw, n, mx + sw, b,
                             mx - sw, b, mx - sw, n, fill=T.FUNNEL, outline=T.FUNNEL)
        else:                                          # sort arrow (▲ asc / ▼ desc / idle)
            glyph = "▲" if state == "asc" else "▼"
            col = T.ARROW_IDLE if state == "idle" else T.ARROW_SORT
            c.create_text(bx + sz / 2, by + sz / 2, anchor="center", text=glyph, fill=col,
                          font=(self.font.actual("family"), -max(7, round(sz * 0.62))))

    def _ranges(self):
        return list(self.extra) + [self.sel]

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

    def _zoom_by(self, factor):
        z = max(0.4, min(4.0, self._zoom * factor))
        if z == self._zoom:
            return
        self._zoom = z
        fpx = max(9, round(self._base_fpx * z))
        self.font.configure(size=-fpx)
        self.hfont.configure(size=-fpx)
        self.geom.set_metrics(max(10, round(self._base_row_h * z)),
                              max(24, round(self._base_gutter * z)),
                              [max(20, round(w * z)) for w in self._base_w])
        self.geom.clamp(self.model.nrows())
        self.redraw()

    def _resize_to(self, c, w):
        """Set column c's width (drag-resize / autofit) and record it as the new
        base so a later zoom keeps it proportional instead of snapping back."""
        self.geom.set_col_w(c, w)
        self._base_w[c] = self.geom.col_w[c] / self._zoom

    def _scroll_rows(self, dr):
        self.geom.top_row += dr
        self.redraw()

    def _scroll_px(self, dx):
        self.geom.scroll_x = max(0, self.geom.scroll_x + dx)
        self.geom.clamp(self.model.nrows())
        self.redraw()

    def _hwheel(self, delta):
        # WM_MOUSEHWHEEL delta accumulates so a precision trackpad's small deltas
        # still scroll. Same sign as <Shift-MouseWheel> above (right swipe -> right).
        self._hacc -= delta * (40 / 120)
        step = int(self._hacc)
        if step:
            self._hacc -= step
            self._scroll_px(step)

    def _install_hwheel(self):
        """Subclass the Win32 wndproc to catch WM_MOUSEHWHEEL, which Tk 8.6 ignores
        (only 8.7+ delivers it). Hooks both the canvas (window under the cursor) and
        its toplevel since drivers differ in which one they post to; only the one
        that receives the message fires. No-op off Windows / on failure."""
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return
        try:
            user32 = ctypes.windll.user32
            LRESULT = LONG_PTR = ctypes.c_ssize_t
            GWLP_WNDPROC, WM_MOUSEHWHEEL = -4, 0x020E
            WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                         wintypes.WPARAM, wintypes.LPARAM)
            setf = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            getf = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            for f in (setf, getf, user32.CallWindowProcW):
                f.restype = LRESULT
            setf.argtypes = [wintypes.HWND, ctypes.c_int, LONG_PTR]
            getf.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.CallWindowProcW.argtypes = [LONG_PTR, wintypes.HWND, wintypes.UINT,
                                               wintypes.WPARAM, wintypes.LPARAM]
            self._hwheel_cb = []                   # keep refs alive (else GC -> crash)
            for hwnd in {self.canvas.winfo_id(), self.winfo_toplevel().winfo_id()}:
                old = getf(hwnd, GWLP_WNDPROC)

                def proc(h, msg, wp, lp, old=old):
                    if msg == WM_MOUSEHWHEEL:
                        self._hwheel(ctypes.c_short((wp >> 16) & 0xFFFF).value)
                        return 0
                    return user32.CallWindowProcW(old, h, msg, wp, lp)

                cb = WNDPROC(proc)
                self._hwheel_cb.append(cb)
                setf(hwnd, GWLP_WNDPROC, ctypes.cast(cb, ctypes.c_void_p).value)
                self.canvas.bind("<Destroy>",
                                 lambda e, h=hwnd, o=old: setf(h, GWLP_WNDPROC, o), add="+")
        except Exception:
            pass

    def _on_vscroll(self, *a):
        if a[0] == "moveto":
            self.geom.top_row = int(float(a[1]) * self.model.nrows())
        else:
            self.geom.top_row += int(a[1]) * (3 if a[2] == "units" else self.geom.full_rows() - 1)
        self.redraw()

    def _on_hscroll(self, *a):
        if a[0] == "moveto":
            self.geom.scroll_x = int(float(a[1]) * (self.geom.content_w() - self.geom.frozen_w()))
        else:
            self.geom.scroll_x += int(a[1]) * 40
        self.redraw()

    # --- selection bounds --------------------------------------------
    def _bounds(self):
        return dict(top_hrow=0, last_row=self.model.nrows() - 1,
                    last_col=self.model.ncols - 1)

    def _scroll_into_view(self, r, c):
        self.geom.scroll_into_view(r, c)

    # --- mouse --------------------------------------------------------
    def _on_motion(self, e):
        self._set_corner_hover(self.geom.in_corner(e.x, e.y))
        on_edge = self.geom.col_edge_hit(e.x, e.y, self.model.ncols) is not None
        self.canvas.configure(cursor="sb_h_double_arrow" if on_edge else "")

    def _set_corner_hover(self, over):
        if over != self._corner_hover:
            self._corner_hover = over
            self.redraw()

    def _on_press(self, e):
        self.canvas.focus_set()
        self._commit_editor()
        region, row, col = self.geom.hit(e.x, e.y, self.model.nrows(), self.model.ncols)
        if row == 0 and region == "cell" and col is not None \
                and self.geom.filter_btn_hit(e.x, e.y, col):   # header filter button
            self._drag_region = None
            FilterPopup(self, col, e.x_root, e.y_root)
            return
        ec = self.geom.col_edge_hit(e.x, e.y, self.model.ncols)
        if ec is not None:                                     # grab a column border
            self._resize_col = ec
            self._resize_x0, self._resize_w0 = e.x, self.geom.col_w[ec]
            self._drag_region = None
            return
        ctrl, shift = bool(e.state & 0x4), bool(e.state & 0x1)
        self.sel, self.extra, self.active, self.anchor = S.resolve_click(
            region, row, col, anchor=self.anchor, sel=self.sel, extra=self.extra,
            ctrl=ctrl, shift=shift, **self._bounds())
        self._drag_region = region
        self._scroll_into_view(*self.active)
        self.redraw()

    def _on_drag(self, e):
        if self._resize_col is not None:                       # live column resize
            self._resize_to(self._resize_col, self._resize_w0 + (e.x - self._resize_x0))
            self.redraw()
            return
        if self._drag_region is None:
            return
        g = self.geom
        nrows, ncols = self.model.nrows(), self.model.ncols
        # autoscroll: down past the bottom edge; up only when data exists above
        # (so dragging into the pinned header selects it instead of scrolling)
        if e.y > g.h - g.row_h:
            g.top_row += 1
        elif e.y < g.header_h and g.top_row > 1:
            g.top_row -= 1
        row = g.drag_row(e.y, nrows)
        col = g.x_to_col(e.x, ncols)
        if col is None:
            col = g.frozen if e.x < g.gutter_w else ncols - 1
        # spreadsheet frozen-pane crossing: dragging left past the freeze line targets the
        # scrollable column hidden under the frozen block (scroll-into-view reveals
        # it, one per motion) instead of snapping onto a pinned frozen column.
        col = S.edge_reveal_col(col, anchor_col=self.anchor[1], frozen_cols=g.frozen,
                                scroll_x=g.scroll_x, ncols=ncols, pointer_x=e.x,
                                gutter_w=g.gutter_w, frozen_w=g.frozen_w(),
                                body_w=g.w, leaf_x=g.col_x)
        self.sel, self.active = S.resolve_drag(
            self._drag_region, row, col, anchor=self.anchor, **self._bounds())
        self._scroll_into_view(*self.active)           # push the view to the pressed-to cell
        g.clamp(nrows)
        self.redraw()

    def _on_release(self, e):
        self._resize_col = None

    def _on_double(self, e):
        ec = self.geom.col_edge_hit(e.x, e.y, self.model.ncols)
        if ec is not None:                                     # dbl-click border = autofit
            self._autofit_col(ec)
            return
        region, row, col = self.geom.hit(e.x, e.y, self.model.nrows(), self.model.ncols)
        if region == "cell" and self.editable:
            self.active = (row, col)
            self._begin_edit()

    def _autofit_col(self, c):
        # ponytail: fit header + currently-visible rows only. Scanning 1M rows for
        # the widest cell would stall; visible-fit matches what the user sees.
        sel = self._selected_cols()
        cols = sorted(sel) if c in sel and len(sel) > 1 else [c]   # Ctrl+A -> fit all
        rows = [0] + self.geom.visible_data_rows(self.model.nrows())
        btn = self.geom.row_h - 8 + 8                          # filter button + gap (header only)
        for cc in cols:
            w = max((self.hfont if r == 0 else self.font).measure(self.model.cell(r, cc))
                    + (btn if r == 0 else 0) for r in rows)
            self._resize_to(cc, w + 12)                        # 5px text inset + margin
        self.redraw()

    def _selected_cols(self):
        cols = set()
        for (r1, c1, r2, c2) in self._ranges():
            cols.update(range(min(c1, c2), max(c1, c2) + 1))
        return cols

    # --- keyboard -----------------------------------------------------
    def _on_key(self, e):
        k, ch = e.keysym, e.char
        ctrl, shift = bool(e.state & 0x4), bool(e.state & 0x1)
        if ctrl:
            low = k.lower()
            if low == "f":
                self.find.reveal(); return "break"
            if low == "c":
                self._copy(); return "break"
            if low == "v":
                self._paste(); return "break"
            if low == "z":
                self.model.undo(); return "break"
            if low == "y":
                self.model.redo(); return "break"
            if low == "a":
                lr, lc = self.model.data_extent()
                self.sel, self.extra = (0, 0, lr, lc), []   # header + data (active at 0,0 -> paste in place)
                self.active = self.anchor = (0, 0)
                self.redraw(); return "break"
        if k in ("Up", "Down", "Left", "Right", "Home", "End", "Prior", "Next"):
            self._arrow(k, shift, ctrl); return "break"
        if k in ("Return", "Tab"):
            self._move((0, 1) if k == "Tab" else (1, 0)); return "break"
        if k in ("Delete", "BackSpace"):
            self.model.delete_selection(self._ranges()); return "break"
        if k == "F2":
            self._begin_edit(); return "break"
        if len(ch) == 1 and ch.isprintable() and not ctrl and self.editable:
            self._begin_edit(initial=ch); return "break"

    def _arrow(self, key, shift, ctrl):
        self.sel, self.extra, self.active, self.anchor = S.resolve_arrow(
            key, active=self.active, anchor=self.anchor, shift=shift, ctrl=ctrl,
            page_rows=self.geom.full_rows(),
            occupied_row=self.model.occupied_row,
            occupied_col=(lambda c: self.model.occupied_col_at(self.active[0], c)),
            **self._bounds())
        self._scroll_into_view(*self.active)
        self.redraw()

    def _move(self, d):
        r = max(0, min(self.model.nrows() - 1, self.active[0] + d[0]))
        c = max(0, min(self.model.ncols - 1, self.active[1] + d[1]))
        self.active = self.anchor = (r, c)
        self.sel, self.extra = (r, c, r, c), []
        self._scroll_into_view(r, c)
        self.redraw()

    # --- clipboard ----------------------------------------------------
    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append(self.model.selection_text(self._ranges()))

    def _paste(self):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return
        box = self.model.paste_text(text, self._ranges(), self.active)
        if box:
            self.sel, self.extra = box, []

    # --- editor -------------------------------------------------------
    def _begin_edit(self, initial=None):
        if not self.editable:
            return
        self._commit_editor()
        r, c = self.active
        bg, fg = edit_colors(r)            # keep the cell's own background + text colour
        var = tk.StringVar(value=initial if initial is not None else self.model.cell(r, c))
        ed = tk.Entry(self.canvas, textvariable=var, relief="solid", bd=1,
                      font=self.font, bg=bg, fg=fg, insertbackground=fg,
                      highlightthickness=1, highlightcolor=T.ACCENT)
        ed._cell, ed._var = (r, c), var
        self._editor = ed
        ed.bind("<Return>", lambda e: self._commit_editor(move=(1, 0)))
        ed.bind("<Tab>", lambda e: (self._commit_editor(move=(0, 1)), "break")[1])
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

    def _commit_editor(self, move=None):
        ed = self._editor
        if not ed:
            return
        r, c = ed._cell
        self.model.set_cell(r, c, ed._var.get())
        ed.destroy()
        self._editor = None
        self.canvas.focus_set()        # return focus so the next keystroke edits again
        if move:
            self.active = self.anchor = (r, c)
            self._move(move)
        else:
            self.redraw()

    def _cancel_editor(self):
        if self._editor:
            self._editor.destroy()
            self._editor = None
            self.canvas.focus_set()
            self.redraw()


# ---------------------------------------------------------------------
# Toolkit widgets: filter popup + find bar. The find NAVIGATION logic is
# generic (match list / index / nearest), but the widgets are Tk-specific.
# ---------------------------------------------------------------------
CHECK, UNCHECK = "☑", "☐"


class FilterPopup(tk.Toplevel):
    def __init__(self, grid, col, x_root, y_root):
        super().__init__(grid)
        self.g, self.model, self.col = grid, grid.model, col
        self.overrideredirect(True)
        self.configure(bg=_UI_BG, bd=1, relief="solid")
        self.geometry("+%d+%d" % (x_root, y_root))
        self._state = None                 # distinct scan is deferred (see _load)

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
        self._active = self.model._filters.get(self.col)
        self._preloaded, self._capped = self.model.distinct_capped(self.col)
        self._state = {v: self._checked(v) for v in self._preloaded}
        self._repopulate()

    def _checked(self, v):
        """Checked state of a value -- an explicit user toggle, else the default
        from the active filter (all allowed when there's no filter)."""
        if self._state and v in self._state:
            return self._state[v]
        return self._active is None or v in self._active

    def _btn(self, text, cmd, enabled=True):
        tk.Button(self, text=text, command=cmd, anchor="w", relief="flat",
                  bg=_UI_BG, fg=T.TXT, activebackground="#ddd6c8",
                  state="normal" if enabled else "disabled").pack(fill="x", padx=2)

    def _rows(self):
        q = self.search.get().strip().lower()
        if not q:
            return self._preloaded
        if self._capped:                   # search the whole column, not just the preview
            return self.model.distinct_matching(self.col, q)
        return [v for v in self._preloaded if q in v.lower()]

    def _repopulate(self):
        if self._state is None:            # still loading the distinct scan
            return
        self.lst.delete(0, "end")
        rows = self._rows()
        all_on = bool(rows) and all(self._checked(v) for v in rows)
        self.lst.insert("end", "%s (Select all)" % (CHECK if all_on else UNCHECK))
        for v in rows:
            self.lst.insert("end", "%s %s" % (CHECK if self._checked(v) else UNCHECK,
                                              v if v else "(blank)"))
        if len(rows) >= self.model.DISTINCT_CAP:     # list truncated -> tell the user to narrow
            self.lst.insert("end", "  … too many to list — type to search")
            self.lst.itemconfig("end", fg="#8a8578")

    def _toggle(self, e):
        if self._state is None:
            return "break"
        i = self.lst.nearest(e.y)
        rows = self._rows()
        if i == 0:
            target = not (rows and all(self._checked(v) for v in rows))
            for v in rows:
                self._state[v] = target
        elif 1 <= i <= len(rows):
            v = rows[i - 1]
            self._state[v] = not self._checked(v)
        self._repopulate()
        return "break"

    def _sort(self, asc):
        self.model.set_sort(self.col, asc); self.destroy()

    def _clear(self):
        self.model.clear_column_filter(self.col); self.destroy()

    def _text(self, op):
        val = simpledialog.askstring("Text filter", op.capitalize() + ":", parent=self)
        if val is not None:
            self.model.set_text_filter(self.col, op, val); self.destroy()

    def _apply(self):
        if self._state is None:            # OK before the list loaded = no-op
            self.destroy(); return
        self._commit(self.search.get().strip())
        self.destroy()

    def _commit(self, query):
        if query:
            rows = self._rows()
            if len(rows) >= self.model.DISTINCT_CAP:       # still too many -> "contains"
                self.model.set_text_filter(self.col, "contains", query)
            else:                                          # filter TO the checked matches
                keep = {v for v in rows if self._checked(v)}
                self.model.set_filter(self.col, keep or None)
            return
        known = set(self._preloaded) | set(self._state)
        checked = {v for v in known if self._checked(v)}
        # Clear when everything's checked and we truly know it's everything: no
        # active filter, or the full distinct set fits (not capped). Otherwise
        # keep exactly the checked members (inclusion).
        if len(checked) == len(known) and (self._active is None or not self._capped):
            self.model.set_filter(self.col, None)
        else:
            self.model.set_filter(self.col, checked)


class FindBar(tk.Frame):
    """Tk find bar -- a thin widget over the shared core FindController."""

    def __init__(self, grid):
        super().__init__(grid.canvas, bg=_UI_BG, bd=2, relief="solid",
                         highlightbackground=T.ACCENT, highlightthickness=2)
        self.g = grid
        self.ctl = FindController(grid)
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
        has_scope = self.ctl.open(self.g._ranges())
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
        if self._after:                                # 120ms debounce (matches the example)
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
