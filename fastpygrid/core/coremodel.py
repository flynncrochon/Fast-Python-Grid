"""CoreModel: GridModel backed by the C++ arena engine (gridcore.dll).

Cell text lives in C++. The bulk text paths (copy/delete/paste/find) and undo
run there in one ctypes call each, avoiding the millions of Python str objects
that are the pure-Python floor. Everything else (view/filter/sort orchestration,
styles, choices, readonly, geometry) stays in Python: those touch one column or
the visible viewport, where per-cell access is already cheap.

`self._rows` is a thin shim delegating to the engine, so every inherited
GridModel method keeps working unchanged. Only the handful of bulk hot methods
are overridden to call C++ directly.

Scope: data-row mutations. Header rows are read-only through the fast path
(headers stay Python, as in GridModel). Editing a header cell is not routed
through this backend. Falls back to GridModel if the DLL is unavailable.
"""
import ctypes
import os
import struct

from .model import GridModel, PAD_ROWS, _clean, _grow, _norm_ranges

_DLL = os.path.join(os.path.dirname(__file__), "gridcore.dll")   # installs into core/, beside this file


def _load():
    if not os.path.exists(_DLL):
        return None
    try:
        lib = ctypes.CDLL(_DLL)
    except OSError:
        return None
    P, I, C = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
    IP = ctypes.POINTER(I)
    sig = {
        "gc_new": ([I, I], P), "gc_free": ([P], None),
        "gc_ndata": ([P], I), "gc_grow_cols": ([P, I], None),
        "gc_load_packed": ([P, C, I], None),
        "gc_cell": ([P, I, I], C), "gc_set_raw": ([P, I, I, C], I),
        "gc_set_view": ([P, IP, I], None),
        "gc_copy": ([P, I, I, I, I, IP], P),
        "gc_delete": ([P, IP, I, IP, I, IP, I, IP], I),
        "gc_paste": ([P, I, I, C, I, IP, I, IP, I, I, I, IP], I),
        "gc_set_cell": ([P, I, I, C, IP, I, IP, I], I),
        "gc_undo": ([P, IP], I), "gc_redo": ([P, IP], I),
        "gc_find": ([P, C, I, I, IP, I, I, IP, IP], P),
    }
    for name, (args, res) in sig.items():
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, res
    return lib


_LIB = _load()


def _iarr(seq):
    """A ctypes int array (or None) from a Python iterable."""
    seq = list(seq)
    if not seq:
        return None, 0
    return (ctypes.c_int * len(seq))(*seq), len(seq)


def _pack(rows):
    """Length-prefixed (u32 len + utf-8 bytes) row-major buffer for gc_load_packed."""
    parts = []
    for r in rows:
        for c in r:
            b = c.encode("utf-8")
            parts.append(struct.pack("<I", len(b)))
            parts.append(b)
    return b"".join(parts)


def make_model(headers, rows, editable=True, on_edit=None):
    """CoreModel (C++ backend) when gridcore.dll is available, else the pure
    Python GridModel. Drop-in: identical public API."""
    cls = CoreModel if _LIB else GridModel
    return cls(headers, rows, editable=editable, on_edit=on_edit)


class _CoreRow:
    __slots__ = ("_c", "_r")

    def __init__(self, core, r):
        self._c, self._r = core, r

    def __getitem__(self, col):
        return _LIB.gc_cell(self._c, self._r, col).decode("utf-8")


class _CoreRows:
    """Read shim making `self._rows[r][c]` delegate to the C++ engine, so inherited
    GridModel code (sort/filter/distinct/cell/style) works unchanged. Reads only:
    every inherited mutation path (set_cell/paste/delete/undo/grow) is overridden
    to call the engine directly."""
    __slots__ = ("_c",)

    def __init__(self, core):
        self._c = core

    def __len__(self):
        return _LIB.gc_ndata(self._c)

    def __getitem__(self, r):
        return _CoreRow(self._c, r)

    def __iter__(self):
        for r in range(len(self)):
            yield _CoreRow(self._c, r)


class CoreModel(GridModel):
    # ---- storage: build the engine instead of a Python matrix ----
    def set_data(self, headers, rows):
        if headers and isinstance(headers[0], (list, tuple)):
            hdr = [[str(h) for h in hrow] for hrow in headers]
        else:
            hdr = [[str(h) for h in headers] or ["A"]]
        w = max(len(hrow) for hrow in hdr)
        self._headers = [hrow + [""] * (w - len(hrow)) for hrow in hdr]
        self._hdr = len(self._headers)
        self._w = w

        data = [([str(c) for c in r][:w] + [""] * (w - len(r))) for r in rows]
        if getattr(self, "_core", None):
            _LIB.gc_free(self._core)
        self._core = _LIB.gc_new(w, 0)            # hdr=0: engine holds DATA rows only
        if data:
            _LIB.gc_load_packed(self._core, _pack(data), len(data))
        self._rows = _CoreRows(self._core)
        self._init_view_state()                   # _undo holds tagged ops, cell diffs live in C++

    def __del__(self):
        c = getattr(self, "_core", None)
        if c and _LIB:
            _LIB.gc_free(c)

    # ---- view: keep GridModel's Python rebuild, then push the mapping to C++ ----
    def _rebuild(self):
        super()._rebuild()
        if self._is_plain():
            _LIB.gc_set_view(self._core, None, -1)
        else:
            arr, n = _iarr(self._view)
            _LIB.gc_set_view(self._core, arr, n)

    def _ro_arrays(self):
        rc, n_rc = _iarr(sorted(self._readonly))
        rr, n_rr = _iarr(sorted(k for k in self._readonly_rows if k >= 0))   # data rows only
        return rc, n_rc, rr, n_rr

    def _touch(self, cols):
        for c in cols:
            self._distinct.pop(c, None)

    def _push_snap(self, rng):
        # Cell diff lives in the C++ engine. The Python entry carries the view state
        # active at this edit + the touched rect (to reselect on undo), tagged to
        # interleave with 'view' ops.
        self._undo.append(("edit", self._filt_snapshot(), rng))
        del self._undo[:-200]
        self._redo.clear()

    # ---- bulk hot paths -> C++ ----
    def selection_text(self, ranges):
        rs = _norm_ranges(ranges)
        if not rs:
            return ""
        r1 = max(0, min(r[0] for r in rs)); c1 = max(0, min(r[1] for r in rs))
        r2 = max(r[2] for r in rs); c2 = min(self._w - 1, max(r[3] for r in rs))
        H = self._hdr
        lines = []
        for gr in range(r1, min(H, r2 + 1)):          # header rows (Python)
            lines.append("\t".join(_clean(self.cell(gr, c)) for c in range(c1, c2 + 1)))
        d_lo = max(r1, H)                              # data rows (C++)
        if d_lo <= r2:
            n = ctypes.c_int()
            p = _LIB.gc_copy(self._core, d_lo - H, c1, r2 - H, c2, ctypes.byref(n))
            lines.append(ctypes.string_at(p, n.value).decode("utf-8"))
        return "\n".join(lines)

    def delete_selection(self, ranges):
        if not self.editable:
            return False
        H = self._hdr
        rects, box = [], None
        for r1, c1, r2, c2 in _norm_ranges(ranges):
            r1 = max(r1, H)                            # data rows only
            if r1 <= r2:
                rects.extend((r1 - H, c1, r2 - H, c2))
                box = _grow(_grow(box, r1, c1), r2, c2)   # grid rect to reselect on undo
        if not rects:
            return False
        ra = (ctypes.c_int * len(rects))(*rects)
        rc, n_rc, rr, n_rr = self._ro_arrays()
        tgt = (ctypes.c_int * 2)()
        changed = _LIB.gc_delete(self._core, ra, len(rects) // 4, rc, n_rc, rr, n_rr, tgt)
        if changed:
            cols = {c for i in range(0, len(rects), 4) for c in range(max(0, rects[i + 1]),
                    min(self._w, rects[i + 3] + 1))}
            self._touch(cols)
            self._push_snap(box)
            self._find_cache = None
            self._used = None
            if not self._is_plain():
                self._rebuild()
            if self.on_edit:
                self.on_edit()
            self.changed()
        return bool(changed)

    def paste_text(self, text, ranges, active):
        if not self.editable or not text:
            return None
        rs = _norm_ranges(ranges)
        if rs:
            start_gr = min(r[0] for r in rs); start_col = min(r[1] for r in rs)
            sel_r2 = max(r[2] for r in rs); sel_c2 = max(r[3] for r in rs)
        else:
            start_gr, start_col = active
            sel_r2, sel_c2 = active
        H = self._hdr
        d_start = max(0, start_gr - H)                  # header-row paste not routed
        rc, n_rc, rr, n_rr = self._ro_arrays()
        pre = _LIB.gc_ndata(self._core)
        # C++ parses the raw clipboard, fills a 1x1 over a multi-cell selection,
        # and returns the block dims, no Python split/scan.
        payload = text.encode("utf-8")
        dims = (ctypes.c_int * 2)()
        changed = _LIB.gc_paste(self._core, d_start, start_col, payload, len(payload),
                                rc, n_rc, rr, n_rr, sel_r2 - start_gr + 1,
                                sel_c2 - start_col + 1, dims)
        nblock, maxw = dims[0], dims[1]
        if nblock == 0:                                # clipboard was all-blank
            return None
        if changed:
            if _LIB.gc_ndata(self._core) > pre:        # materialised -> all columns gained a blank
                self._distinct.clear()
            else:
                self._touch(range(start_col, start_col + maxw))
            self._push_snap((start_gr, start_col,
                             start_gr + nblock - 1, start_col + maxw - 1))
            self._find_cache = None
            self._used = None
        if not self._is_plain():
            self._rebuild()
        if self.on_edit:
            self.on_edit()
        self.changed()
        end_gr = start_gr + nblock - 1
        end_col = start_col + maxw - 1
        return (start_gr, start_col, min(end_gr, self.nrows() - 1), min(end_col, self._w - 1))

    def set_cell(self, gr, col, text):
        if not self.editable or col in self._readonly or self.row_readonly(gr) \
                or not (0 <= col < self._w) or gr < self._hdr:
            return False
        text = str(text)
        rc, n_rc, rr, n_rr = self._ro_arrays()
        pre = _LIB.gc_ndata(self._core)
        changed = _LIB.gc_set_cell(self._core, gr - self._hdr, col, text.encode("utf-8"),
                                   rc, n_rc, rr, n_rr)
        if not changed:
            return False
        if _LIB.gc_ndata(self._core) > pre:           # materialised -> all columns gained a blank
            self._distinct.clear()
        else:
            self._touch([col])
        self._push_snap((gr, col, gr, col))
        self._find_cache = None
        self._used = None
        if self._rebuilds_on_edit(gr, col):
            self._rebuild()
        if self.on_edit:
            self.on_edit()
        self.changed()
        return True

    def grow_cols(self, new_w):
        """Widen the sheet to `new_w` columns (editing past the last column, uncapped).
        The C++ core re-strides its buffer, headers gain blank trailing cells. No-op
        if it already has that many. New columns start blank and are fully editable."""
        if new_w <= self._w:
            return
        _LIB.gc_grow_cols(self._core, new_w)
        for hrow in self._headers:
            hrow += [""] * (new_w - len(hrow))
        self._w = new_w
        self._distinct.clear()
        self._used = None
        self.changed()

    def _replay_edit(self, entry, use_new):
        # base undo()/redo() dispatch 'view' vs 'edit', a cell edit drives the C++ stack.
        tgt = (ctypes.c_int * 2)()
        fn = _LIB.gc_redo if use_new else _LIB.gc_undo
        if not fn(self._core, tgt):
            return None
        self._install_filt(entry[1])
        if self.on_edit:
            self.on_edit()
        self.changed()
        return self._clamp_target(entry[2])

    # ---- find -> C++ (data) + Python (header rows) ----
    def find_matches(self, query, case=False, scope=None):
        if not query:
            self._find_cache = None
            return [], False
        H = self._hdr
        out = []
        needle = query if case else query.lower()
        # header rows in Python (tiny), in grid order first
        hdr_scope = None
        if scope:
            hdr_scope = [(r1, c1, r2, c2) for (r1, c1, r2, c2) in scope if r1 < H]
        hrows = range(H) if not scope else None
        for hr in (range(H) if hrows is not None else []):
            for c in range(self._w):
                v = self._headers[hr][c]
                if v and needle in (v if case else v.lower()):
                    out.append((hr, c))
        if scope and hdr_scope:
            seen = set()
            for (r1, c1, r2, c2) in hdr_scope:
                for hr in range(max(0, r1), min(H, r2 + 1)):
                    for c in range(max(0, c1), min(self._w, c2 + 1)):
                        if (hr, c) in seen:
                            continue
                        seen.add((hr, c))
                        v = self._headers[hr][c]
                        if v and needle in (v if case else v.lower()):
                            out.append((hr, c))
        # data rows in C++
        nb = query.encode("utf-8")
        sc = None
        if scope:
            flat = []
            for (r1, c1, r2, c2) in scope:
                if r2 < H:
                    continue
                flat.extend((max(0, r1 - H), c1, r2 - H, c2))
            sc, nsc = _iarr(flat)
        else:
            nsc = 0
        cnt, capped = ctypes.c_int(), ctypes.c_int()
        p = _LIB.gc_find(self._core, nb, len(nb), 1 if case else 0, sc, nsc // 4 if scope else 0,
                         self.FIND_LIMIT, ctypes.byref(cnt), ctypes.byref(capped))
        raw = ctypes.string_at(p, cnt.value * 8)
        data = [(raw[i] | raw[i + 1] << 8 | raw[i + 2] << 16 | raw[i + 3] << 24,
                 raw[i + 4] | raw[i + 5] << 8 | raw[i + 6] << 16 | raw[i + 7] << 24)
                for i in range(0, len(raw), 8)]
        for gr, c in data:
            out.append((gr + H, c))
        if scope:
            out = sorted(set(out))
        return out, bool(capped.value)
