"""Tk host for the toolkit-neutral GpuEngine (fastpygrid.core.gpu).

The engine owns all rendering/input LOGIC and imports no GUI toolkit. This thin Tk
adapter owns the window, surface frame, fonts, event translation, clipboard and
context menu, and implements the ~dozen host-adapter methods the engine calls.
qt.py is the same shape over QWidget.
"""
import ctypes
import sys
import tkinter as tk
from tkinter import font as tkfont

from ..core import theme as T
from ..core.coremodel import make_model
from ..core.gpu import GpuEngine, _load_lib, _enable_dpi_awareness, _screen_scale, UI_FONT


def _win_clip_html():
    """Windows CF_HTML clipboard flavor (UTF-8), or "", Tk can't fetch it. The
    table format browsers/spreadsheets put alongside plain text."""
    if sys.platform != "win32":
        return ""
    try:
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        u32.RegisterClipboardFormatW.restype = ctypes.c_uint
        u32.GetClipboardData.restype = ctypes.c_void_p
        u32.GetClipboardData.argtypes = [ctypes.c_uint]
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalLock.argtypes = [ctypes.c_void_p]
        k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        fmt = u32.RegisterClipboardFormatW("HTML Format")
        if not fmt or not u32.OpenClipboard(0):
            return ""
        try:
            h = u32.GetClipboardData(fmt)
            if not h:
                return ""
            ptr = k32.GlobalLock(h)
            if not ptr:
                return ""
            try:
                data = ctypes.string_at(ptr)          # CF_HTML is NUL-terminated UTF-8
            finally:
                k32.GlobalUnlock(h)
            return data.decode("utf-8", "replace")
        finally:
            u32.CloseClipboard()
    except Exception:
        return ""


class GpuGrid(tk.Frame):
    """Thin Tk host: owns the surface frame + fonts, implements the host-adapter API,
    translates Tk events into the engine's normalized input."""

    def __init__(self, master, model, editable=True, frozen=0, col_w=None, scale=1.0, lib=None,
                 uncap_rows=False, uncap_cols=False, filters=True):
        super().__init__(master)
        self._fpx = max(9, round(13 * scale))
        self.font = tkfont.Font(family=UI_FONT, size=-self._fpx)
        self.hfont = tkfont.Font(family=UI_FONT, size=-self._fpx, weight="bold")
        self.surface = tk.Frame(self, bg=T.LETTER_BG)  # native Gpu child HWND attaches here
        self.surface.grid(row=0, column=0, sticky="nsew")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.model = model
        self.engine = GpuEngine(self, model, editable=editable, frozen=frozen,
                                col_w=col_w, scale=scale, lib=lib,
                                uncap_rows=uncap_rows, uncap_cols=uncap_cols, filters=filters)
        E = self.engine
        c = self.surface
        c.bind("<Configure>", lambda e: E.configure_size(*self.size()))
        c.bind("<Button-1>", lambda e: E.press(e.x, e.y, bool(e.state & 0x4),
                                                bool(e.state & 0x1)))
        c.bind("<B1-Motion>", lambda e: E.drag(e.x, e.y))
        c.bind("<ButtonRelease-1>", lambda e: E.release())
        c.bind("<Double-Button-1>", lambda e: E.double(e.x, e.y))
        c.bind("<Triple-Button-1>", lambda e: E.triple(e.x, e.y))
        c.bind("<Motion>", lambda e: E.motion(e.x, e.y))
        c.bind("<Leave>", lambda e: E.leave())
        c.bind("<MouseWheel>", lambda e: E.wheel(e.delta // 120))
        c.bind("<Shift-MouseWheel>", lambda e: E._scroll_px(-(e.delta // 120) * 40))
        c.bind("<Control-MouseWheel>", lambda e: E.zoom(1.1 if e.delta > 0 else 1 / 1.1))
        c.bind("<Button-3>", lambda e: E.context(e.x, e.y, (e.x_root, e.y_root)))
        c.bind("<Key>", self._on_key)
        c.configure(takefocus=1)
        top = self.winfo_toplevel()
        top.bind("<F11>", lambda e: (E.toggle_fullscreen(), "break")[1])
        top.bind("<Escape>", lambda e: top.attributes("-fullscreen", False))

    def _on_key(self, e):
        consumed = self.engine.key(e.keysym, e.char, bool(e.state & 0x1), bool(e.state & 0x4))
        return "break" if consumed else None

    # --- host-adapter API the engine calls (a Qt host implements these over Qt) ---
    def measure(self, text, bold=False):
        return (self.hfont if bold else self.font).measure(text)

    def size(self):
        return self.surface.winfo_width(), self.surface.winfo_height()

    def hwnd(self):
        return self.surface.winfo_id()

    def focus(self):
        self.surface.focus_set()

    _CURSORS = {"resize": "sb_h_double_arrow", "hand": "hand2", "text": "xterm"}

    def set_cursor(self, kind):
        self.surface.configure(cursor=self._CURSORS.get(kind, ""))

    def set_zoom_px(self, px):
        self.font.configure(size=-px)
        self.hfont.configure(size=-px)

    def clip_get(self):
        try:
            return tk.Frame.clipboard_get(self)
        except tk.TclError:
            return ""

    def clip_get_html(self):
        h = _win_clip_html()
        return h if "<table" in h.lower() else ""

    def clip_set(self, text):
        self.clipboard_clear(); self.clipboard_append(text)

    def fullscreen_toggle(self):
        top = self.winfo_toplevel()
        top.attributes("-fullscreen", not top.attributes("-fullscreen"))

    def context_menu(self, root, actions):
        m = tk.Menu(self, tearoff=0, activebackground=T.SEL_RING, activeforeground="white")
        for label, cmd, enabled in actions:
            m.add_command(label=label, command=cmd, state="normal" if enabled else "disabled")
        m.tk_popup(*root)

    # after / after_cancel / after_idle are inherited from tk.Frame (used by the engine)

    def destroy(self):
        self.engine.close()
        super().destroy()


def make_sheet(headers, rows, frozen_columns=0, view_only=False, master=None,
               col_w=None, title="fastpygrid (gpu)", uncap_rows=False, uncap_cols=False,
               filters=True):
    """One-call sheet under a Tk host (OpenGL 1.1 backend). Raises if the surface
    lib isn't built."""
    lib = _load_lib()
    if lib is None:
        raise RuntimeError(
            "OpenGL surface unavailable, build it with `python -m fastpygrid.core.gpu --build`.")
    if master is None:
        _enable_dpi_awareness()
        win = tk.Tk()
    else:
        win = tk.Toplevel(master)
    win.title(title)
    scale = _screen_scale(win)
    win.geometry("%dx%d" % (round(980 * scale), round(620 * scale)))
    model = make_model(headers, rows, editable=not view_only)
    grid = GpuGrid(win, model, editable=not view_only, frozen=frozen_columns,
                   col_w=col_w, scale=scale, lib=lib,
                   uncap_rows=uncap_rows, uncap_cols=uncap_cols, filters=filters)
    grid.pack(fill="both", expand=True)
    win.model, win.grid_view = model, grid
    return win
