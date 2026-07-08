"""Plain-Python grid model: a matrix of strings with filter / sort / find,
in-cell editing, paste, and undo/redo. Zero Qt.

ROW SPACE: grid row 0 is the header (field names) -- a real,
selectable, numbered "1" row that the renderer pins at the top; grid rows 1..N
are the data (filtered/sorted view). So a click, copy, or find treats the header
uniformly as just another row. Internally, data index ``di = gr - 1`` maps
through ``_src_data`` to a source row in ``self._rows``.
"""
import csv
from io import StringIO

from .selection import normalize as _norm_ranges

# row growth only (append blank data rows past the end). No
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
        self._styles = {}         # (src_row | -1 for header, col) -> {fg,bg,bold}; display only
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

    # --- per-cell style (display only; keyed by SOURCE row so it follows the
    # data through sort/filter). Not part of undo -- styling is metadata, not an
    # edit. bg is used as the cell's base fill (selection wash still tints over
    # it, find-highlight still overrides); fg/bold always apply. -------------
    def _style_key(self, gr, col):
        if not (0 <= col < self._w):
            return None
        if gr == 0:
            return (-1, col)                     # header
        di = gr - 1
        if not (0 <= di < self._data_count()):
            return None
        r = self._src_data(di)
        return (r, col) if r < len(self._rows) else None    # not the blank pad

    def set_cell_style(self, gr, col, fg=None, bg=None, bold=None):
        """Style one cell. Pass any of fg/bg (#rrggbb) or bold (bool); None
        leaves that attribute unchanged. A later value overrides an earlier one."""
        key = self._style_key(gr, col)
        if key is None:
            return
        st = self._styles.setdefault(key, {})
        for k, v in (("fg", fg), ("bg", bg), ("bold", bold)):
            if v is not None:
                st[k] = v
        self.changed()

    def clear_cell_style(self, gr, col):
        if self._styles.pop(self._style_key(gr, col), None) is not None:
            self.changed()

    def cell_style(self, gr, col):
        """The style dict for a cell, or None. Hot path (per visible cell), so
        the common no-styles case is a single dict-empty check."""
        if not self._styles:
            return None
        return self._styles.get(self._style_key(gr, col))

    def occupied_row(self, gr):
        return any(self.cell(gr, c).strip() for c in range(self._w))

    def occupied_col_at(self, gr, col):
        return bool(self.cell(gr, col).strip())

    # --- filter / sort API (driven by the filter popup) ---------------
    DISTINCT_CAP = 1000

    def distinct_capped(self, col, cap=DISTINCT_CAP):
        """(sorted values, capped?) for the filter checklist. Early-exits once
        more than `cap` distinct values are seen: a value checklist is useless
        (and slow to build) on a high-cardinality column -- the popup shows the
        cap and the user narrows via Contains…/Equals…. A column that fits under
        the cap is scanned fully and cached."""
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
    # Undo is a DIFF log, not a snapshot: each entry is
    # (changes, filt, target, pre_len) where changes = [(src_row, col, old, new)]
    # (src_row -1 = header). So an edit + its undo/redo cost O(cells changed), not a
    # deep copy of the whole matrix -- the old snapshot approach stalled on a big
    # grid and hoarded up to 200 full copies.
    def _filt_snapshot(self):
        # Filters/sort aren't undoable ops, but an edit's entry still restores what
        # was active when it ran (matches the old full-snapshot behaviour). Tiny.
        return (dict(self._filters), dict(self._text_filters), self._sort)

    def _grid_to_src(self, gr):
        return -1 if gr == 0 else self._src_data(gr - 1)

    def _push_edit(self, changes, target, pre_len):
        self._undo.append((changes, self._filt_snapshot(), target, pre_len))
        del self._undo[:-200]
        self._redo.clear()

    def _write_src(self, src, col, val):
        """Replay a recorded write straight to its SOURCE cell (no view mapping),
        materialising a blank row if the edit had grown the grid."""
        if src < 0:
            self._headers[col] = val
        else:
            if src >= len(self._rows):
                if not val:
                    return
                self._materialize(src)
            self._rows[src][col] = val
        self._distinct.pop(col, None)

    def _clamp_target(self, target):
        # ponytail: target is a view (grid-row, col), valid after undo/redo restore
        # the sort/filter it was captured under. Editing a sorted/filtered column
        # can reorder rows so redo lands near-but-not-exact -- upgrade to a source
        # remap (see example's _view_target) only if that edge case ever matters.
        if target is None:
            return None
        return (max(0, min(self.nrows() - 1, target[0])),
                max(0, min(self._w - 1, target[1])))

    def _replay(self, entry, use_new):
        changes, filt, _target, pre_len = entry
        for src, col, old, new in (changes if use_new else reversed(changes)):
            self._write_src(src, col, new if use_new else old)
        if not use_new:
            del self._rows[pre_len:]                 # drop rows the edit materialised
        self._filters = {c: set(v) for c, v in filt[0].items()}
        self._text_filters = dict(filt[1])
        self._sort = filt[2]
        self._distinct = {}
        if not self._is_plain():                     # plain view never reads _view
            self._rebuild()
        if self.on_edit:
            self.on_edit()
        self.changed()

    def undo(self):
        if self._undo:
            entry = self._undo.pop()
            self._replay(entry, use_new=False)
            self._redo.append(entry)
            return self._clamp_target(entry[2])

    def redo(self):
        if self._redo:
            entry = self._redo.pop()
            self._replay(entry, use_new=True)
            self._undo.append(entry)
            return self._clamp_target(entry[2])

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
        old = self.cell(gr, col)
        if old == text:
            return False
        pre_len = len(self._rows)
        src = self._grid_to_src(gr)
        self._write(gr, col, text)
        self._push_edit([(src, col, old, text)], (gr, col), pre_len)
        if self._rebuilds_on_edit(gr, col):
            self._rebuild()
        if self.on_edit:
            self.on_edit()
        self.changed()
        return True

    def delete_selection(self, ranges):
        if not self.editable:
            return False
        pre_len = len(self._rows)
        changes, target = [], None
        for r1, c1, r2, c2 in _norm_ranges(ranges):
            for gr in range(max(0, r1), r2 + 1):
                for col in range(max(0, c1), min(self._w, c2 + 1)):
                    old = self.cell(gr, col)
                    if old:
                        if target is None:
                            target = (gr, col)
                        changes.append((self._grid_to_src(gr), col, old, ""))
                        self._write(gr, col, "")
        if changes:
            self._push_edit(changes, target, pre_len)
            if not self._is_plain():
                self._rebuild()
            if self.on_edit:
                self.on_edit()
            self.changed()
        return bool(changes)

    def selection_text(self, ranges):
        rs = _norm_ranges(ranges)
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
        rs = _norm_ranges(ranges)
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
        pre_len = len(self._rows)
        changes = []
        for roff, brow in enumerate(block):
            gr = start_gr + roff
            for coff, val in enumerate(brow):
                col = start_col + coff
                if 0 <= col < self._w:
                    old = self.cell(gr, col)
                    src = self._grid_to_src(gr)
                    if self._write(gr, col, val):
                        changes.append((src, col, old, val))
        if changes:
            self._push_edit(changes, (start_gr, start_col), pre_len)
        if not self._is_plain():
            self._rebuild()
        if self.on_edit:
            self.on_edit()
        self.changed()
        end_gr = start_gr + len(block) - 1
        end_col = start_col + max(len(r) for r in block) - 1
        return (start_gr, start_col, min(end_gr, self.nrows() - 1),
                min(end_col, self._w - 1))


if __name__ == "__main__":   # ponytail: headless check of diff-based undo/redo
    m = GridModel(["A", "B"], [["a1", "b1"], ["a2", "b2"]])

    m.set_cell(1, 0, "X")                       # edit existing cell
    assert m.cell(1, 0) == "X"
    assert m.undo() == (1, 0) and m.cell(1, 0) == "a1"
    assert m.redo() == (1, 0) and m.cell(1, 0) == "X"
    m.undo()

    n0 = m.nrows()                              # edit into the blank pad -> grows rows
    m.set_cell(6, 1, "deep")
    assert m.cell(6, 1) == "deep" and m.nrows() > n0
    m.undo()
    assert m.cell(6, 1) == "" and m.nrows() == n0   # materialised row dropped

    m.paste_text("p\tq\nr\ts", [(1, 0, 1, 0)], (1, 0))
    assert (m.cell(1, 0), m.cell(2, 1)) == ("p", "s")
    m.undo()
    assert (m.cell(1, 0), m.cell(2, 1)) == ("a1", "b2")

    m.delete_selection([(1, 0, 2, 1)])
    assert m.cell(1, 0) == "" and m.cell(2, 1) == ""
    assert m.undo() == (1, 0) and m.cell(2, 1) == "b2"
    print("model self-check ok")
