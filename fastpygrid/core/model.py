"""Plain-Python grid model: matrix of strings with filter/sort/find, in-cell
editing, paste, undo/redo. Zero Qt.

Grid rows 0..H-1 are header rows (real, selectable, pinned; ``headers`` may be
one list or a list of lists for a multi-row header, bottom row = field names).
Data index ``di = gr - H`` maps via ``_src_data`` to a source row in ``self._rows``.
"""
from html.parser import HTMLParser


PAD_ROWS = 50   # blank data rows kept navigable past the data (plain view only)


_CLEAN = str.maketrans("\t\n\r", "   ")


class _HtmlTable(HTMLParser):
    """First <table> in the clipboard HTML -> list of row lists of cell text.
    Ignores colspan/rowspan (Jira tables rarely merge), add if needed."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self._row, self._cell, self._done = [], None, None, False
    def handle_starttag(self, tag, attrs):
        if self._done:
            return
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append("\n")
    def handle_endtag(self, tag):
        if tag == "tr" and self._row is not None:
            self.rows.append(self._row); self._row = None
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip()); self._cell = None
        elif tag == "table" and self.rows:
            self._done = True                        # stop at first table
    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _parse_html_table(text):
    p = _HtmlTable()
    try:
        p.feed(text)
    except Exception:
        return []
    rows = [r for r in p.rows if r is not None]
    if not rows:
        return []
    w = max(len(r) for r in rows)
    return [r + [""] * (w - len(r)) for r in rows]   # pad ragged rows to a rectangle


def _grow(box, gr, col):
    """Extend an (r1,c1,r2,c2) rect to include a cell: bounding box of the cells an
    edit touched, so undo/redo reselect exactly them."""
    if box is None:
        return (gr, col, gr, col)
    return (min(box[0], gr), min(box[1], col), max(box[2], gr), max(box[3], col))


def _clean(cell):
    # skip rebuilding the common already-clean string
    if "\t" in cell or "\n" in cell or "\r" in cell:
        return cell.translate(_CLEAN)
    return cell


class GridModel:
    def __init__(self, headers, rows, editable=True):
        self.editable = editable
        self.changed = lambda: None     # view assigns its redraw here
        self.set_data(headers, rows)

    # --- data load ----------------------------------------------------
    def set_data(self, headers, rows):
        if headers and isinstance(headers[0], (list, tuple)):   # multi-row header
            hdr = [[str(h) for h in hrow] for hrow in headers]
        else:
            hdr = [[str(h) for h in headers] or ["A"]]
        w = max(len(hrow) for hrow in hdr)
        self._headers = [hrow + [""] * (w - len(hrow)) for hrow in hdr]
        self._hdr = len(self._headers)
        self._w = w
        self._rows = [([str(c) for c in r][:w] + [""] * (w - len(r))) for r in rows]
        self._init_view_state()

    def _init_view_state(self):
        """Fresh filter/sort/undo/find/presentation state: everything except cell
        storage, so set_data() here and in CoreModel share it."""
        self._filters = {}        # col -> set(allowed display strings)
        self._text_filters = {}   # col -> (op, operand)
        self._color_filters = {}  # col -> ('fg'|'bg', color hex or None): keep only that color
        self._sort = None         # (col, ascending) text, or (col, asc, 'fg'|'bg', color) by color
        self._undo, self._redo = [], []
        self._find_needle = ""
        self._find_case = False
        self._find_scope = None   # list of (r1,c1,r2,c2) or None
        self._find_active = None  # (row, col)
        self._find_cache = None    # (needle, case, scope, matches) of last full scan --
                                   # typing path refines instead of rescanning
        self._distinct = {}       # col -> sorted distinct values (filter popup), data-edit invalidates
        self._vlines = {}         # column index -> divider width px on its RIGHT edge (None = theme default)
        self._hlines = {}         # SOURCE row (-1-gr for header) -> divider width px on its BOTTOM edge; follows sort/filter
        self._numeric = set()     # columns sorted numerically (smallest->largest) not a->z
        self._readonly = set()    # columns that reject edits/paste/delete (still selectable)
        self._readonly_rows = set()  # SOURCE rows (-1-gr for header) that reject edits, follows sort/filter
        self._choices = {}        # (src_row | -1, col) -> (choice, ...), a dropdown cell
        self._col_choices = {}    # col -> (choice, ...), whole-column dropdown default (O(1))
        self._choice_cols = set() # columns holding any dropdown (paint() per-column skip)
        self._choice_intern = {}  # option-list -> itself: identical dropdowns in a
                                  # column share ONE tuple, not a copy per cell
        self._rebuild()
        self._committed_filt = self._filt_snapshot()   # last view state pushed to undo

    # --- view (filter + sort), over DATA rows -------------------------
    def _is_plain(self):
        return (not self._filters and not self._text_filters
                and not self._color_filters and self._sort is None)

    def _rebuild(self):
        """GridModel is the abstract base; CoreModel (C++) is the concrete model. Data ops
        (filter/sort/color) and styling/paint all live in CoreModel -- there's no pure-Python
        reimplementation to keep in sync. A bare GridModel supports only a plain view (the
        shared view-state/geometry/choices scaffolding the tests exercise)."""
        self._find_cache = None            # view changed -> cached grid-row coords stale
        self._used = None                  # ...and the used-range (scrollbar) snapshot
        if self._is_plain():
            self._view = []      # unread while plain; don't build 1M ids
            return
        raise NotImplementedError(
            "filter/sort/color views require the C++ engine; use make_model() (CoreModel).")

    # --- shape / access (GRID rows: 0..H-1 = header, H..N = data) -----
    @property
    def ncols(self):
        return self._w

    @property
    def header_rows(self):
        return self._hdr

    def _data_count(self):
        return len(self._rows) + PAD_ROWS if self._is_plain() else len(self._view)

    def nrows(self):
        """Total grid rows = header rows + data (view or data+pad)."""
        return self._hdr + self._data_count()

    def _real_rows(self):
        """Grid rows EXCLUDING the blank pad: headers + real data. Find scans these;
        select-all covers them."""
        return self._hdr + (len(self._rows) if self._is_plain() else len(self._view))

    def _src_data(self, di):
        """Data index -> source row in self._rows. A phantom overscroll row past a
        filtered view has no source -> -1 (callers guard 0<=r<len(rows))."""
        if self._is_plain():
            return di
        return self._view[di] if di < len(self._view) else -1

    def cell(self, gr, col):
        if not (0 <= col < self._w):
            return ""
        if gr < self._hdr:
            return self._headers[gr][col]
        r = self._src_data(gr - self._hdr)
        return self._rows[r][col] if 0 <= r < len(self._rows) else ""

    def block_text(self, data_rows, cols):
        """Text for a viewport block as {(data_idx, col): str}, data_idx = grid_row -
        hdr_rows. paint() prefetches the whole body so a core model can batch it into
        one FFI (CoreModel.block_text). Base = per-cell."""
        H, cell = self._hdr, self.cell
        return {(di, c): cell(di + H, c) for di in data_rows for c in cols}

    def data_extent(self):
        """(last_real_row, last_col) for Ctrl+A: header + real data, never the pad."""
        return max(0, self._real_rows() - 1), max(0, self._w - 1)

    def used_extent(self):
        """(nrows-equiv, ncols) trimmed to real content, for the scrollbar thumb.
        Overscroll edits materialise blank rows/cols; clearing them leaves the blanks,
        so nrows()/ncols would keep the thumb tiny forever. Reports the last row/col
        holding text (+ PAD nav rows) so the thumb snaps back. Cached, invalidated on
        every edit / view change.
        Backward scan, O(trailing-blank cells); on the C++ model that's
        a ctypes call per cell. Add a gc_used_extent DLL export if a giant
        overscroll delete ever hitches."""
        if self._used is not None:
            return self._used
        if not self._is_plain():                  # a filtered view can't overscroll-materialise
            self._used = (self.nrows(), self._w)
            return self._used
        rows, W = self._rows, self._w
        r = len(rows) - 1
        while r >= 0 and not any(rows[r][c] for c in range(W)):
            r -= 1
        used_nrows = self._hdr + (r + 1) + PAD_ROWS
        uc = 0
        for hrow in self._headers:                # headers keep the original columns "used"
            for c in range(W - 1, uc - 1, -1):
                if hrow[c]:
                    uc = c + 1
                    break
        for i in range(r + 1):                    # only the used data rows
            row = rows[i]
            for c in range(W - 1, uc - 1, -1):
                if row[c]:
                    uc = c + 1
                    break
        self._used = (used_nrows, uc)
        return self._used

    # --- per-cell style (display only, keyed by SOURCE row so it follows sort/filter;
    # not undoable). bg = base fill (wash tints over it, find overrides); fg/bold always.
    def _style_key(self, gr, col):
        if not (0 <= col < self._w):
            return None
        if gr < self._hdr:
            return (-1 - gr, col)                # header row -> -1, -2, …
        di = gr - self._hdr
        if not (0 <= di < self._data_count()):
            return None
        r = self._src_data(di)
        return (r, col) if r < len(self._rows) else None    # not the pad

    # --- per-cell dropdown choices (display only, keyed like styles so they follow
    # sort/filter). Editing offers a select menu instead of free text (renderers'
    # begin_edit).
    def set_cell_choices(self, gr, col, choices):
        """Make a cell a dropdown offering `choices` (strings). None clears it to
        plain text."""
        key = self._style_key(gr, col)
        if key is None:
            return
        # _choice_cols tracks columns with any dropdown (paint() skips per-cell lookup).
        # O(1) on set; clear rescans this column only (clears are rare).
        if choices is None:
            if self._choices.pop(key, None) is not None \
                    and not any(cc == col for _r, cc in self._choices) \
                    and col not in self._col_choices:
                self._choice_cols.discard(col)
        else:
            t = tuple(map(str, choices))
            t = self._choice_intern.setdefault(t, t)   # equal lists share one tuple
            self._choices[key] = t
            self._choice_cols.add(col)
        self.changed()

    def set_col_choices(self, col, choices):
        """Make an ENTIRE column a dropdown offering `choices`. O(1) regardless of row
        count; prefer over per-row set_cell_choices(), which still overrides this.
        None clears the column default."""
        if not (0 <= col < self._w):
            return
        if choices is None:
            if self._col_choices.pop(col, None) is not None \
                    and not any(cc == col for _r, cc in self._choices):
                self._choice_cols.discard(col)
        else:
            t = tuple(map(str, choices))
            t = self._choice_intern.setdefault(t, t)
            self._col_choices[col] = t
            self._choice_cols.add(col)
        self.changed()

    def dropdown_cols(self):
        """Columns containing at least one dropdown cell. paint() checks this once
        per column instead of cell_choices() per cell."""
        return self._choice_cols

    def cell_choices(self, gr, col):
        """Choice list for a dropdown cell, or None. Hot path: no-dropdowns case is
        one dict-empty check. Per-cell choice wins over the column default."""
        if not self._choices and not self._col_choices:
            return None
        return self._choices.get(self._style_key(gr, col)) or self._col_choices.get(col)

    # --- thick section dividers (display only, black). vlines keyed by column (fixed
    # place, like the frozen divider); hlines keyed by SOURCE row so they follow
    # sort/filter.
    def set_vline(self, col, on=True, width=None):
        """Thick black divider on the RIGHT edge of column `col` (on=False clears).
        `width` = stroke px (DPI-scaled); None = theme default."""
        if on:
            self._vlines[col] = width
        else:
            self._vlines.pop(col, None)
        self.changed()

    def set_hline(self, gr, on=True, width=None):
        """Thick black divider on the BOTTOM edge of row `gr` (on=False clears). Keyed
        by source row, so it follows sort/filter. `width` = stroke px; None = theme default."""
        src = self._grid_to_src(gr)
        if on:
            self._hlines[src] = width
        else:
            self._hlines.pop(src, None)
        self.changed()

    def vlines(self):
        return self._vlines

    def hlines(self):
        """Source-keyed {src: width}. Truthiness guard for paint; per-row lookup goes
        through hline_width."""
        return self._hlines

    _NO_HLINE = object()

    def hline_width(self, gr):
        """Divider width on grid row `gr`'s bottom edge, or _NO_HLINE if none (width
        None already means theme default)."""
        if not self._hlines:
            return self._NO_HLINE
        return self._hlines.get(self._grid_to_src(gr), self._NO_HLINE)

    # --- read-only columns (reject edits/paste/delete, still selectable/copyable) --
    def set_readonly_col(self, col, on=True):
        """Block edits/paste/delete in `col` (on=False re-enables)."""
        self._readonly.add(col) if on else self._readonly.discard(col)

    def col_readonly(self, col):
        return col in self._readonly

    # --- read-only rows (reject edits/paste/delete, still selectable/copyable). Keyed
    # by SOURCE row so the lock follows sort/filter. Accepts any grid row.
    def set_readonly_row(self, gr, on=True):
        """Freeze row `gr`: block edits/paste/delete (on=False unlocks)."""
        src = self._grid_to_src(gr)
        self._readonly_rows.add(src) if on else self._readonly_rows.discard(src)

    def row_readonly(self, gr):
        return bool(self._readonly_rows) and self._grid_to_src(gr) in self._readonly_rows

    def occupied_row(self, gr):
        return any(self.cell(gr, c).strip() for c in range(self._w))

    def occupied_col_at(self, gr, col):
        return bool(self.cell(gr, col).strip())

    # --- filter / sort API (driven by the filter popup) ---------------
    DISTINCT_CAP = 1000

    def distinct_capped(self, col, cap=DISTINCT_CAP):
        """(sorted values, capped?) for the filter checklist. Early-exits past `cap`
        distinct values (checklist useless there; user narrows via Contains…/Equals…).
        A column under the cap is scanned and cached."""
        vals = self._distinct.get(col)
        if vals is not None:
            return vals[:cap], len(vals) > cap
        seen = set()
        add = seen.add                              # bind once, called per row
        for row in self._rows:
            add(row[col])
            if len(seen) > cap:                     # high-card bails after ~cap rows
                return sorted(seen)[:cap], True     # partial -> don't cache
        vals = self._distinct[col] = sorted(seen)   # complete -> cache
        return vals, False

    def distinct_matching(self, col, query, cap=DISTINCT_CAP):
        """Up to `cap` sorted distinct values in `col` containing `query`
        (case-insensitive), lets the search box reach members beyond the capped
        preview on a high-cardinality column."""
        q = query.lower()
        seen = set()
        for row in self._rows:
            v = row[col]
            if q in v.lower():
                seen.add(v)
                if len(seen) > cap:
                    break
        return sorted(seen)[:cap]

    def has_filter(self, col):
        return (col in self._filters or col in self._text_filters
                or col in self._color_filters)

    def has_sort(self, col):
        return self._sort is not None and self._sort[0] == col

    def sort_ascending(self, col):
        return bool(self._sort and self._sort[0] == col and self._sort[1])

    def set_filter(self, col, allowed):
        (self._filters.pop(col, None) if allowed is None
         else self._filters.__setitem__(col, set(allowed)))
        self._after_view_change()

    def set_text_filter(self, col, op, text):
        (self._text_filters.pop(col, None) if text is None
         else self._text_filters.__setitem__(col, (op, str(text))))
        self._after_view_change()

    def set_color_filter(self, col, which, color):
        """Keep only rows whose `col` cell has `which` ('fg'/'bg') == `color` (hex, or
        None for uncolored). which=None clears the color filter."""
        (self._color_filters.pop(col, None) if which is None
         else self._color_filters.__setitem__(col, (which, color)))
        self._after_view_change()

    def clear_column_filter(self, col):
        self._filters.pop(col, None)
        self._text_filters.pop(col, None)
        self._color_filters.pop(col, None)
        self._after_view_change()

    def set_column_numeric(self, col, numeric=True):
        """Mark `col` numeric: sort becomes smallest->largest instead of a->z.
        Re-sorts if active."""
        self._numeric.add(col) if numeric else self._numeric.discard(col)
        if self.has_sort(col) and len(self._sort) == 2:
            self._rebuild(); self.changed()

    def is_column_numeric(self, col):
        return col in self._numeric

    def set_sort(self, col, ascending):
        self._sort = (col, ascending)
        self._after_view_change()

    def set_color_sort(self, col, which, color, ascending=True):
        """Bring `col` rows with `which` ('fg'/'bg') == `color` to the top
        (color=None = uncolored)."""
        self._sort = (col, ascending, which, color)
        self._after_view_change()

    def clear_sort(self):
        self._sort = None
        self._after_view_change()

    def clear_filters(self):
        self._filters, self._text_filters, self._color_filters, self._sort = {}, {}, {}, None
        self._after_view_change()

    def _after_view_change(self):
        self._find_active = None
        self._rebuild()
        self.changed()
        post = self._filt_snapshot()          # filter/sort undoable like an edit
        if self._committed_filt != post:
            self._undo.append(("view", self._committed_filt, post))
            del self._undo[:-200]
            self._redo.clear()
        self._committed_filt = post

    # --- find (GRID rows, header included) ----------------------------
    FIND_LIMIT = 100_000


    def set_find(self, needle, case, scope, active):
        self._find_needle = (needle if case else needle.lower()) if needle else ""
        self._find_case = case
        self._find_scope = scope
        self._find_active = tuple(active) if active else None
        self.changed()

    def clear_find(self):
        self._find_needle, self._find_scope, self._find_active = "", None, None
        self.changed()

    def find_state(self, gr, col):
        """0 none, 1 match-highlight, 2 active match; per VISIBLE cell."""
        if self._find_active == (gr, col):
            return 2
        n = self._find_needle
        if not n:
            return 0
        if self._find_scope and not any(r1 <= gr <= r2 and c1 <= col <= c2
                                        for r1, c1, r2, c2 in self._find_scope):
            return 0
        v = self.cell(gr, col)
        return 1 if v and n in (v if self._find_case else v.lower()) else 0

    # --- editing + undo/redo (GRID rows) ------------------------------
    # Undo is a DIFF log: each entry is (changes, filt, target, pre_len), changes =
    # [(src_row, col, old, new)] (src_row -1 = header). Cost O(cells changed).
    def _filt_snapshot(self):
        # an entry also restores the filters/sort active when it ran
        return (dict(self._filters), dict(self._text_filters), self._sort,
                dict(self._color_filters))

    def _grid_to_src(self, gr):
        return -1 - gr if gr < self._hdr else self._src_data(gr - self._hdr)


    def _clamp_target(self, rng):
        # rng = view rect the undo/redo touched, so selection lands back on it. A
        # sorted/filtered-column edit can reorder rows -> lands near-but-not-exact.
        if rng is None:
            return None
        R, C = self.nrows() - 1, self._w - 1
        return (max(0, min(R, rng[0])), max(0, min(C, rng[1])),
                max(0, min(R, rng[2])), max(0, min(C, rng[3])))

    def _install_filt(self, filt):
        """Restore a filter/sort snapshot and rebuild the view (both models)."""
        self._filters = {c: set(v) for c, v in filt[0].items()}
        self._text_filters = dict(filt[1])
        self._sort = filt[2]
        self._color_filters = dict(filt[3])
        self._distinct = {}
        self._rebuild()
        self._committed_filt = self._filt_snapshot()


    def _apply_entry(self, entry, use_new):
        if entry[0] == "view":                       # filter/sort change: no cell
            self._find_active = None
            self._install_filt(entry[2] if use_new else entry[1])
            self.changed()
            return None
        return self._replay_edit(entry, use_new)

    def undo(self):
        if self._undo:
            entry = self._undo.pop()
            self._redo.append(entry)
            return self._apply_entry(entry, use_new=False)

    def redo(self):
        if self._redo:
            entry = self._redo.pop()
            self._undo.append(entry)
            return self._apply_entry(entry, use_new=True)


    def _rebuilds_on_edit(self, gr, col):
        return gr >= self._hdr and (col in self._filters or col in self._text_filters
                            or (self._sort is not None and self._sort[0] == col))
