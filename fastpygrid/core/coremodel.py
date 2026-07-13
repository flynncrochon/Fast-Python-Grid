"""CoreModel: GridModel backed by the C++ arena engine (gridcore.dll).

Cell text lives in C++. Bulk text paths (copy/delete/paste/find) and undo run
there in one ctypes call each, avoiding the millions of Python str objects that
are the pure-Python floor. Everything else (view/filter/sort, styles, choices,
readonly, geometry) stays in Python; those touch one column or the viewport,
already cheap.

`self._rows` is a shim delegating to the engine, so inherited GridModel methods
work unchanged; only the bulk hot methods are overridden to call C++.

Scope: data-row mutations only. Header rows stay Python (read-only via the fast
path). Requires the DLL; make_model() raises without it.
"""
import ctypes
import os
import struct
import sys
from array import array as _array

from .model import GridModel, _clean, _grow
from .selection import normalize as _norm_ranges

# installs beside this file
_EXT = ".dll" if sys.platform == "win32" else ".so"
_DLL = os.path.join(os.path.dirname(__file__), "gridcore" + _EXT)


def _load():
    if not os.path.exists(_DLL):
        return None
    try:
        lib = ctypes.CDLL(_DLL)
    except OSError:
        return None
    P, I, C = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
    F = ctypes.c_float
    IP, FP = ctypes.POINTER(I), ctypes.POINTER(F)
    sig = {
        "gc_new": ([I, I], P), "gc_free": ([P], None),
        "gc_ndata": ([P], I), "gc_grow_cols": ([P, I], None),
        "gc_load_packed": ([P, C, I], None),
        "gc_cell": ([P, I, I], C), "gc_set_raw": ([P, I, I, C], I),
        "gc_set_view": ([P, IP, I], None),
        "gc_copy": ([P, I, I, I, I, IP], P),
        "gc_block": ([P, IP, I, IP, I, IP], P),
        "gc_paint_body": ([P, IP, FP, FP, I, IP, FP, I, F, I, F, F,
                           IP, I, I, I, I, I, I, I, I, F,
                           C, I, I, IP, I, I, I, I, I, IP], P),
        "gc_delete": ([P, IP, I, IP, I, IP, I, IP], I),
        "gc_paste": ([P, I, I, C, I, IP, I, IP, I, I, I, IP], I),
        "gc_set_cell": ([P, I, I, C, IP, I, IP, I], I),
        "gc_undo": ([P, IP], I), "gc_redo": ([P, IP], I),
        "gc_find": ([P, C, I, I, IP, I, I, IP, IP], P),
        "gc_filter_set": ([P, I, C, I, IP, I, IP], P),
        "gc_filter_text": ([P, I, I, C, I, IP, I, IP], P),
        "gc_sort": ([P, I, I, I, IP, I, IP], P),
        "gc_set_style": ([P, I, I, I, I, I, I], None),
        "gc_set_styles": ([P, IP, I], None),
        "gc_get_style": ([P, I, I, IP], I),
        "gc_style_filter": ([P, I, I, I, IP, I, IP], P),
        "gc_style_sort": ([P, I, I, I, I, IP, I, IP], P),
        "gc_distinct_colors": ([P, I, I, IP], P),
    }
    for name, (args, res) in sig.items():
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, res
    return lib


_LIB = _load()


def _iarr(seq):
    """ctypes int array (or None) + length from an iterable."""
    seq = list(seq)
    if not seq:
        return None, 0
    return (ctypes.c_int * len(seq))(*seq), len(seq)


def _farr(seq):
    """ctypes float array (or None) from an iterable."""
    seq = list(seq)
    if not seq:
        return None
    return (ctypes.c_float * len(seq))(*seq)


# text-filter op name -> gc_filter_text code (default contains)
_TEXT_OP = {"contains": 0, "equals": 1, "not_equals": 2,
            "begins": 3, "ends": 4, "not_contains": 5}

# style attr -> gc_style_filter/sort/distinct index; hex <-> packed int at the FFI boundary.
_WHICH = {"fg": 0, "bg": 1}


def _col_int(hexstr):
    """'#rrggbb' -> 0xRRGGBB, None/'' -> -1 (unset/uncolored, matching the C++ sentinel)."""
    return int(hexstr[1:], 16) if hexstr else -1


def _hex(v):
    """0xRRGGBB -> '#rrggbb' (lowercase), or None if unset (-1)."""
    return "#%06x" % v if v >= 0 else None


def _pack(rows):
    """Length-prefixed (u32 + utf-8) row-major buffer for gc_load_packed."""
    parts = []
    for r in rows:
        for c in r:
            b = c.encode("utf-8")
            parts.append(struct.pack("<I", len(b)))
            parts.append(b)
    return b"".join(parts)


def make_model(headers, rows, editable=True):
    """CoreModel backed by the C++ arena engine. Raises if gridcore.dll is
    missing (no silent degrade to pure-Python GridModel)."""
    if not _LIB:
        raise RuntimeError(
            "gridcore.dll unavailable, build it with "
            "`python -m fastpygrid.core.gpu --build`.")
    return CoreModel(headers, rows, editable=editable)


class _CoreRow:
    __slots__ = ("_c", "_r")

    def __init__(self, core, r):
        self._c, self._r = core, r

    def __getitem__(self, col):
        return _LIB.gc_cell(self._c, self._r, col).decode("utf-8")


class _CoreRows:
    """Read-only shim: `self._rows[r][c]` delegates to C++, so inherited GridModel
    code (sort/filter/distinct/cell/style) works unchanged. Mutations are all
    overridden to call the engine directly."""
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
    # ---- storage: engine instead of a Python matrix ----
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
        self._init_view_state()                   # _undo holds tagged ops; cell diffs in C++

    def __del__(self):
        c = getattr(self, "_core", None)
        if c and _LIB:
            _LIB.gc_free(c)

    # ---- view: filter + sort in C++ (off Python's per-cell FFI loop), push mapping ----
    def _rebuild(self):
        self._find_cache = None            # view changed -> cached grid-row coords stale
        self._used = None                  # ...and the used-range (scrollbar) snapshot
        if self._is_plain():
            self._view = []
            _LIB.gc_set_view(self._core, None, -1)
            return
        self._view = self._native_view()
        self._push_view()

    def _native_view(self):
        """Value/text/color filters + text/numeric/color sort all run natively in C++
        (off Python's per-cell FFI loop), chained value->text->color filter->sort to
        keep GridModel._rebuild's order. Styles live in the core now (gc_style_*), so
        no Python composition step. Returns the final view as array('i')."""
        outn = ctypes.c_int()

        def cand(buf):                     # bytes of int32 -> (c_int*, n); n<0 => all rows
            if buf is None:
                return None, -1            # no filter yet: C side iterates all data rows
            n = len(buf) // 4              # n may be 0: empty set (all filtered out), NOT all
            return ((ctypes.c_int * n).from_buffer_copy(buf) if n else None), n

        # 1. value + text filters, native. buf = bytes of di, or None = all data rows.
        buf = None
        for col, allowed in self._filters.items():
            cp, cn = cand(buf)
            packed = _pack([list(allowed)]) if allowed else b""
            p = _LIB.gc_filter_set(self._core, col, packed, len(allowed),
                                   cp, cn, ctypes.byref(outn))
            buf = ctypes.string_at(p, outn.value * 4)
        for col, (op, needle) in self._text_filters.items():
            cp, cn = cand(buf)
            nb = str(needle).encode("utf-8")
            p = _LIB.gc_filter_text(self._core, col, _TEXT_OP.get(op, 0), nb, len(nb),
                                    cp, cn, ctypes.byref(outn))
            buf = ctypes.string_at(p, outn.value * 4)
        # 2. color filters, native (over the core's style map).
        for col, (which, color) in self._color_filters.items():
            cp, cn = cand(buf)
            p = _LIB.gc_style_filter(self._core, col, _WHICH[which], _col_int(color),
                                     cp, cn, ctypes.byref(outn))
            buf = ctypes.string_at(p, outn.value * 4)
        # 3. sort (last): color sort or text/numeric sort, both native.
        if self._sort is not None:
            cp, cn = cand(buf)
            if len(self._sort) == 4:            # color sort
                col, asc, which, color = self._sort
                p = _LIB.gc_style_sort(self._core, col, _WHICH[which], _col_int(color),
                                       1 if asc else 0, cp, cn, ctypes.byref(outn))
            else:
                col, asc = self._sort[0], self._sort[1]
                p = _LIB.gc_sort(self._core, col, 1 if asc else 0,
                                 1 if col in self._numeric else 0, cp, cn, ctypes.byref(outn))
            buf = ctypes.string_at(p, outn.value * 4)
        return _array("i", buf) if buf is not None else _array("i")

    def _push_view(self):
        v = self._view
        if not v:
            _LIB.gc_set_view(self._core, None, 0)      # empty view (all filtered out)
        elif isinstance(v, _array):
            _LIB.gc_set_view(self._core, (ctypes.c_int * len(v)).from_buffer(v), len(v))
        else:                                          # list, from the Python fallback
            arr, n = _iarr(v)
            _LIB.gc_set_view(self._core, arr, n)

    def _ro_arrays(self):
        rc, n_rc = _iarr(sorted(self._readonly))
        rr, n_rr = _iarr(sorted(k for k in self._readonly_rows if k >= 0))   # data rows only
        return rc, n_rc, rr, n_rr

    def _touch(self, cols):
        for c in cols:
            self._distinct.pop(c, None)

    def _push_snap(self, rng):
        # Cell diff lives in C++. Python entry carries the edit's view state + touched
        # rect (to reselect on undo), tagged to interleave with 'view' ops.
        self._undo.append(("edit", self._filt_snapshot(), rng))
        del self._undo[:-200]
        self._redo.clear()

    def cell(self, gr, col):
        """Hot path (~1900 calls/frame): read one cell straight from C++. Base
        GridModel.cell does ``self._rows[r][col]``, which allocates a _CoreRow proxy
        and calls gc_ndata (a 2nd FFI) for bounds. gc_cell bounds-checks internally,
        so call it directly: one FFI, no proxy alloc, same result."""
        if not (0 <= col < self._w):
            return ""
        if gr < self._hdr:
            return self._headers[gr][col]
        r = self._src_data(gr - self._hdr)
        return _LIB.gc_cell(self._core, r, col).decode("utf-8")

    def block_text(self, data_rows, cols):
        """One FFI for a whole viewport (vs ~1900 gc_cell calls). gc_block returns
        length-prefixed UTF-8 for data_rows x cols (view-resolved in C++); unpack once
        into {(data_idx, col): str}."""
        data_rows = list(data_rows); cols = list(cols)
        ra, nr = _iarr(data_rows)
        ca, nc = _iarr(cols)
        if not nr or not nc:
            return {}
        ln = ctypes.c_int(0)
        p = _LIB.gc_block(self._core, ra, nr, ca, nc, ctypes.byref(ln))
        blob = ctypes.string_at(p, ln.value)
        out = {}
        off = 0
        unpack = struct.Struct("<I").unpack_from
        for di in data_rows:
            for c in cols:
                n = unpack(blob, off)[0]; off += 4
                out[(di, c)] = blob[off:off + n].decode("utf-8") if n else ""
                off += n
        return out

    def gc_paint_body(self, cols, colx, colw, grs, rowy, row_h, H, fpx, rect_w,
                      sel, single_cell, col_txt, col_zebra, col_bg, wash_even, wash_odd,
                      sel_tint, sel_wash_a, find_match, find_active):
        """Emit wire bytes for the visible body cells natively (the viewport-sized hot
        loop). Styles + find highlight are resolved in C++ (gc_paint_body reads the core's
        style map and the find state pulled from self here), so _blit_fast marshals only
        geometry/palette -- no per-cell style gather, and it runs even during a find."""
        ca, ncol = _iarr(cols)
        ga, nrow = _iarr(grs)
        sa, nsel4 = _iarr(sel)
        # find state (needle pre-lowered by set_find when !case); scope/active are grid coords.
        nb = (getattr(self, "_find_needle", "") or "").encode("utf-8")
        cs = 1 if getattr(self, "_find_case", False) else 0
        scope = getattr(self, "_find_scope", None)
        if scope:
            fsc, nf = _iarr([v for rect in scope for v in rect])
            nfscope = nf // 4
        else:
            fsc, nfscope = None, 0
        active = getattr(self, "_find_active", None)
        ag, ac = (int(active[0]), int(active[1])) if active else (-1, -1)
        ln = ctypes.c_int(0)
        p = _LIB.gc_paint_body(self._core, ca, _farr(colx), _farr(colw), ncol,
                               ga, _farr(rowy), nrow, float(row_h), int(H),
                               float(fpx), float(rect_w), sa, nsel4 // 4,
                               1 if single_cell else 0, col_txt, col_zebra, col_bg,
                               wash_even, wash_odd, sel_tint, float(sel_wash_a),
                               nb, len(nb), cs, fsc, nfscope, ag, ac,
                               find_match, find_active, ctypes.byref(ln))
        return ctypes.string_at(p, ln.value)

    # ---- per-cell styles -> C++ (was GridModel's Python _styles dict) ----
    def set_cell_style(self, gr, col, fg=None, bg=None, bold=None):
        key = self._style_key(gr, col)
        if key is None:
            return
        mask = (1 if fg is not None else 0) | (2 if bg is not None else 0) \
            | (4 if bold is not None else 0)
        if not mask:
            return
        _LIB.gc_set_style(self._core, key[0], key[1],
                          _col_int(fg), _col_int(bg), 1 if bold else 0, mask)
        self.changed()

    def set_cell_styles(self, entries):
        """Bulk-apply many styles in ONE FFI. entries: iterable of (gr, col, fg, bg, bold),
        each attr None = leave as-is (same semantics as set_cell_style). Use this instead of
        a per-cell set_cell_style loop for mass styling (e.g. demo startup)."""
        recs = []
        for gr, col, fg, bg, bold in entries:
            key = self._style_key(gr, col)
            if key is None:
                continue
            mask = (1 if fg is not None else 0) | (2 if bg is not None else 0) \
                | (4 if bold is not None else 0)
            if not mask:
                continue
            recs += [key[0], key[1], _col_int(fg), _col_int(bg), 1 if bold else 0, mask]
        if not recs:
            return
        arr = (ctypes.c_int * len(recs))(*recs)
        _LIB.gc_set_styles(self._core, arr, len(recs) // 6)
        self.changed()

    def cell_style(self, gr, col):
        key = self._style_key(gr, col)
        if key is None:
            return None
        out3 = (ctypes.c_int * 3)()
        if not _LIB.gc_get_style(self._core, key[0], key[1], out3):
            return None
        st = {}
        if out3[0] >= 0:
            st["fg"] = _hex(out3[0])
        if out3[1] >= 0:
            st["bg"] = _hex(out3[1])
        if out3[2]:
            st["bold"] = True
        return st or None

    def distinct_colors(self, col, which):
        outn = ctypes.c_int()
        p = _LIB.gc_distinct_colors(self._core, col, _WHICH[which], ctypes.byref(outn))
        return [_hex(v) for v in _array("i", ctypes.string_at(p, outn.value * 4))]

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
        # C++ parses the clipboard, fills a 1x1 over a multi-cell selection, returns
        # the block dims, no Python split/scan.
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
        self.changed()
        return True

    def grow_cols(self, new_w):
        """Widen the sheet to `new_w` columns (editing past the last, uncapped). C++
        re-strides its buffer, headers gain blank trailing cells. No-op if already
        that wide. New columns start blank and editable."""
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
        # base undo()/redo() dispatch 'view' vs 'edit'; a cell edit drives the C++ stack.
        tgt = (ctypes.c_int * 2)()
        fn = _LIB.gc_redo if use_new else _LIB.gc_undo
        if not fn(self._core, tgt):
            return None
        self._install_filt(entry[1])
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
        # header rows in Python (tiny), grid order first
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
