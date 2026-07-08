"""Plain-Python grid model: a matrix of strings with filter / sort / find,
in-cell editing, paste, and undo/redo. Zero Qt.

ROW SPACE (matches paintgrid): grid row 0 is the header (field names) -- a real,
selectable, numbered "1" row that the renderer pins at the top; grid rows 1..N
are the data (filtered/sorted view). So a click, copy, or find treats the header
uniformly as just another row. Internally, data index ``di = gr - 1`` maps
through ``_src_data`` to a source row in ``self._rows``.
"""
import csv
from io import StringIO

# ponytail: row growth only (append blank data rows past the end). No
# column-append, no "breathing" scrollbar frontier -- add later only if needed.
PAD_ROWS = 50   # blank data rows kept navigable past the data, plain view


def _clean(cell):
    return cell.replace("\t", " ").replace("\n", " ").replace("\r", " ")


class GridModel:
    def __init__(self, headers, rows, editable=True, on_edit=None):
        self.editable = editable
        self.on_edit = on_edit
        self.changed = lambda: None     # the view assigns its redraw here
        self.set_data(headers, rows)

    # --- data load ----------------------------------------------------
    def set_data(self, headers, rows):
        self._headers = [str(h) for h in headers] or ["A"]
        w = len(self._headers)
        self._w = w
        self._rows = [([str(c) for c in r][:w] + [""] * (w - len(r))) for r in rows]
        self._filters = {}        # col -> set(allowed display strings)
        self._text_filters = {}   # col -> (op, operand)
        self._sort = None         # (col, ascending)
        self._undo, self._redo = [], []
        self._find_needle = ""
        self._find_case = False
        self._find_scope = None   # list of (r1,c1,r2,c2) or None
        self._find_active = None  # (row, col)
        self._distinct = {}       # col -> sorted distinct values (filter popup); data-edit invalidates
        self._rebuild()

    # --- view (filter + sort), over DATA rows -------------------------
    def _is_plain(self):
        return not self._filters and not self._text_filters and self._sort is None

    @staticmethod
    def _match_text(text, spec):
        op, needle = spec
        lhs, rhs = text.lower(), str(needle).lower()
        if op == "equals":       return lhs == rhs
        if op == "not_equals":   return lhs != rhs
        if op == "begins":       return lhs.startswith(rhs)
        if op == "ends":         return lhs.endswith(rhs)
        if op == "not_contains": return rhs not in lhs
        return rhs in lhs        # contains

    def _sort_rows(self, rows, col, ascending):
        dec = [(self._rows[r][col], r) for r in rows]
        blanks = [r for t, r in dec if t == ""]
        filled = [(t, r) for t, r in dec if t != ""]
        filled.sort(key=lambda it: it[0].lower(), reverse=not ascending)
        return [r for _t, r in filled] + blanks

    def _rebuild(self):
        rows = list(range(len(self._rows)))
        for col, allowed in self._filters.items():
            rows = [r for r in rows if self._rows[r][col] in allowed]
        for col, spec in self._text_filters.items():
            rows = [r for r in rows if self._match_text(self._rows[r][col], spec)]
        if self._sort is not None:
            rows = self._sort_rows(rows, self._sort[0], self._sort[1])
        self._view = rows

    # --- shape / access (GRID rows: 0 = header, 1..N = data) ----------
    @property
    def ncols(self):
        return self._w

    def _data_count(self):
        return len(self._rows) + PAD_ROWS if self._is_plain() else len(self._view)

    def nrows(self):
        """Total grid rows = header (1) + data (view or data+pad)."""
        return 1 + self._data_count()

    def _real_rows(self):
        """Grid rows EXCLUDING the blank pad -- header + real data. Find scans
        these; select-all covers them."""
        return 1 + (len(self._rows) if self._is_plain() else len(self._view))

    def _src_data(self, di):
        """Data index (0-based) -> source row in self._rows."""
        return di if self._is_plain() else self._view[di]

    def header(self, col):
        return self._headers[col] if 0 <= col < self._w else ""

    def cell(self, gr, col):
        if not (0 <= col < self._w):
            return ""
        if gr == 0:
            return self._headers[col]
        r = self._src_data(gr - 1)
        return self._rows[r][col] if 0 <= r < len(self._rows) else ""

    def data_extent(self):
        """(last_real_row, last_col) for Ctrl+A -- header + real data, never the
        blank pad."""
        return max(0, self._real_rows() - 1), max(0, self._w - 1)

    def occupied_row(self, gr):
        return any(self.cell(gr, c).strip() for c in range(self._w))

    def occupied_col_at(self, gr, col):
        return bool(self.cell(gr, col).strip())

    # --- filter / sort API (driven by the filter popup) ---------------
    def distinct_values(self, col):
        # Cached: the filter popup re-asks on every keystroke; a full scan+sort
        # over all source rows each time is the filter-select cost. Data edits
        # invalidate the affected column in _write / _restore.
        vals = self._distinct.get(col)
        if vals is None:
            vals = self._distinct[col] = sorted({row[col] for row in self._rows})
        return vals

    DISTINCT_CAP = 1000

    def distinct_capped(self, col, cap=DISTINCT_CAP):
        """(sorted values, capped?) for the filter checklist. Early-exits once
        more than `cap` distinct values are seen: a value checklist is useless
        (and slow to build) on a high-cardinality column -- the popup shows the
        cap and the user narrows via Contains…/Equals…. A column that fits under
        the cap is scanned fully and cached like distinct_values."""
        vals = self._distinct.get(col)
        if vals is not None:
            return vals[:cap], len(vals) > cap
        seen = set()
        for row in self._rows:
            seen.add(row[col])
            if len(seen) > cap:
                return sorted(seen)[:cap], True     # partial -> don't cache
        vals = self._distinct[col] = sorted(seen)   # complete -> cache
        return vals, False

    def distinct_matching(self, col, query, cap=DISTINCT_CAP):
        """Up to `cap` sorted distinct values in `col` that contain `query`
        (case-insensitive). Lets the filter popup's search box reach members
        beyond the capped preview on a high-cardinality column."""
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
        return col in self._filters or col in self._text_filters

    def has_sort(self, col):
        return self._sort is not None and self._sort[0] == col

    def sort_ascending(self, col):
        return bool(self._sort and self._sort[0] == col and self._sort[1])

    def any_filters(self):
        return bool(self._filters or self._text_filters or self._sort)

    def set_filter(self, col, allowed):
        (self._filters.pop(col, None) if allowed is None
         else self._filters.__setitem__(col, set(allowed)))
        self._after_view_change()

    def set_text_filter(self, col, op, text):
        (self._text_filters.pop(col, None) if text is None
         else self._text_filters.__setitem__(col, (op, str(text))))
        self._after_view_change()

    def clear_column_filter(self, col):
        self._filters.pop(col, None)
        self._text_filters.pop(col, None)
        self._after_view_change()

    def set_sort(self, col, ascending):
        self._sort = (col, ascending)
        self._after_view_change()

    def clear_filters(self):
        self._filters, self._text_filters, self._sort = {}, {}, None
        self._after_view_change()

    def _after_view_change(self):
        self._find_active = None
        self._rebuild()
        self.changed()

    # --- find (GRID rows, header included) ----------------------------
    FIND_LIMIT = 100_000

    def find_matches(self, query, case=False, scope=None):
        """(cells, capped) -- up to FIND_LIMIT (row, col) whose text contains
        ``query``. ``scope`` is a list of (r1,c1,r2,c2) rects or None."""
        if not query:
            return [], False
        needle = query if case else query.lower()
        nr, nc = self._real_rows(), self._w
        out = []
        if scope:
            seen = set()
            for (r1, c1, r2, c2) in scope:
                r1, r2 = max(0, min(nr - 1, r1)), max(0, min(nr - 1, r2))
                c1, c2 = max(0, min(nc - 1, c1)), max(0, min(nc - 1, c2))
                for row in range(r1, r2 + 1):
                    for col in range(c1, c2 + 1):
                        if (row, col) in seen:
                            continue
                        seen.add((row, col))
                        v = self.cell(row, col)
                        if v and needle in (v if case else v.lower()):
                            out.append((row, col))
            out.sort()
            return (out[:self.FIND_LIMIT], True) if len(out) > self.FIND_LIMIT else (out, False)
        for row in range(nr):
            for col in range(nc):
                v = self.cell(row, col)
                if v and needle in (v if case else v.lower()):
                    out.append((row, col))
                    if len(out) >= self.FIND_LIMIT:
                        return out, True
        return out, False

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
        """0 none, 1 match-highlight, 2 active match -- tested per VISIBLE cell."""
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
    def _snapshot(self):
        return (self._headers[:], [r[:] for r in self._rows], dict(self._filters),
                dict(self._text_filters), self._sort)

    def _push_undo(self):
        self._undo.append(self._snapshot())
        del self._undo[:-200]
        self._redo.clear()

    def _restore(self, snap):
        self._headers = snap[0][:]
        self._rows = [r[:] for r in snap[1]]
        self._filters = {c: set(v) for c, v in snap[2].items()}
        self._text_filters = dict(snap[3])
        self._sort = snap[4]
        self._distinct = {}                # rows wholesale-replaced
        self._rebuild()
        if self.on_edit:
            self.on_edit()

    def undo(self):
        if self._undo:
            self._redo.append(self._snapshot())
            self._restore(self._undo.pop())
            self.changed()

    def redo(self):
        if self._redo:
            self._undo.append(self._snapshot())
            self._restore(self._redo.pop())
            self.changed()

    def _materialize(self, r):
        while len(self._rows) <= r:
            self._rows.append([""] * self._w)

    def _write(self, gr, col, text):
        """Write one GRID cell (no undo/signals). Returns True if it changed."""
        if gr == 0:
            if self._headers[col] == text:
                return False
            self._headers[col] = text
            return True
        r = self._src_data(gr - 1)
        if r >= len(self._rows):
            if not text.strip():
                return False
            self._materialize(r)
        if self._rows[r][col] == text:
            return False
        self._rows[r][col] = text
        self._distinct.pop(col, None)      # this column's distinct set changed
        return True

    def _rebuilds_on_edit(self, gr, col):
        return gr >= 1 and (col in self._filters or col in self._text_filters
                            or (self._sort is not None and self._sort[0] == col))

    def set_cell(self, gr, col, text):
        if not self.editable or not (0 <= col < self._w):
            return False
        text = str(text)
        if self.cell(gr, col) == text:
            return False
        self._push_undo()
        self._write(gr, col, text)
        if self._rebuilds_on_edit(gr, col):
            self._rebuild()
        if self.on_edit:
            self.on_edit()
        self.changed()
        return True

    @staticmethod
    def _norm(ranges):
        return [(min(a, c), min(b, d), max(a, c), max(b, d)) for (a, b, c, d) in ranges]

    def delete_selection(self, ranges):
        if not self.editable:
            return False
        snap, touched = None, False
        for r1, c1, r2, c2 in self._norm(ranges):
            for gr in range(max(0, r1), r2 + 1):
                for col in range(max(0, c1), min(self._w, c2 + 1)):
                    if self.cell(gr, col):
                        if snap is None:
                            snap = self._snapshot()
                        self._write(gr, col, "")
                        touched = True
        if touched:
            self._undo.append(snap); del self._undo[:-200]; self._redo.clear()
            self._rebuild()
            if self.on_edit:
                self.on_edit()
            self.changed()
        return touched

    def selection_text(self, ranges):
        rs = self._norm(ranges)
        if not rs:
            return ""
        r1 = min(r[0] for r in rs); c1 = min(r[1] for r in rs)
        r2 = max(r[2] for r in rs); c2 = max(r[3] for r in rs)
        c1, c2 = max(0, c1), min(self._w - 1, c2)
        return "\n".join(
            "\t".join(_clean(self.cell(gr, c)) for c in range(c1, c2 + 1))
            for gr in range(max(0, r1), r2 + 1))

    @staticmethod
    def _parse_clip(text):
        if not text:
            return []
        rows = list(csv.reader(StringIO(text), csv.excel_tab if "\t" in text else csv.excel))
        while rows and not any(c.strip() for c in rows[-1]):
            rows.pop()
        return rows

    def paste_text(self, text, ranges, active):
        if not self.editable:
            return None
        block = self._parse_clip(text)
        if not block:
            return None
        rs = self._norm(ranges)
        if rs:
            start_gr = min(r[0] for r in rs); start_col = min(r[1] for r in rs)
            sel_r2 = max(r[2] for r in rs); sel_c2 = max(r[3] for r in rs)
        else:
            start_gr, start_col = active
            sel_r2, sel_c2 = active
        # single clipboard cell over a multi-cell selection fills the block
        if len(block) == 1 and len(block[0]) == 1 and (sel_r2 > start_gr or sel_c2 > start_col):
            v = block[0][0]
            block = [[v] * (sel_c2 - start_col + 1) for _ in range(sel_r2 - start_gr + 1)]
        self._push_undo()
        for roff, brow in enumerate(block):
            gr = start_gr + roff
            for coff, val in enumerate(brow):
                col = start_col + coff
                if 0 <= col < self._w:
                    self._write(gr, col, val)
        self._rebuild()
        if self.on_edit:
            self.on_edit()
        self.changed()
        end_gr = start_gr + len(block) - 1
        end_col = start_col + max(len(r) for r in block) - 1
        return (start_gr, start_col, min(end_gr, self.nrows() - 1),
                min(end_col, self._w - 1))
