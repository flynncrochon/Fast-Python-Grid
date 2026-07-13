"""Grid math shared by both renderers: column x-positions, scroll clamping,
pixel<->cell hit-testing, visible-range computation. No toolkit, no colours.

Rows 0..hdr_rows-1 are pinned header rows (field names on the bottom, group
bands above). Rows hdr_rows..N scroll in the body. ``header_h`` (letter band +
header rows) is where the body begins; ``top_row`` is the first visible data row.
"""
from bisect import bisect_left, bisect_right
from itertools import accumulate


class Geometry:
    MAX_COLS = 16384          # column cap (classic spreadsheet limit)
    OVERSCROLL_PAD = 24       # px you can always pan past the last cell so it never sits flush
    def __init__(self, col_w, frozen=0, gutter_w=56, row_h=22, hdr_rows=1,
                 uncap_rows=False, uncap_cols=False, filters=True):
        # uncap_*: scroll past the last row/col into empty space (spreadsheet-style).
        # Thumb shrinks on overscroll, snaps back on scroll-in unless you typed out
        # there (grows the model). False (default) = clamped to the data.
        self.uncap_rows = uncap_rows
        self.uncap_cols = uncap_cols
        self.filters = filters             # show header filter/sort ▼ buttons
        self.frozen = frozen
        self.gutter_w = gutter_w
        self.hdr_rows = max(1, hdr_rows)
        self.row_h = row_h
        self.letter_h = row_h + 2          # column-letter band (A, B, C…)
        self.field_h = row_h               # one header row
        self.header_h = self.letter_h + self.hdr_rows * self.field_h   # body starts here
        self.set_cols(col_w)
        self.w = self.h = 0                 # viewport size, set by the renderer
        # Vertical scroll is a PIXEL offset (like scroll_x) for smooth sub-row wheel
        # panning; top_row is derived so row-index call sites are unchanged.
        self.scroll_y = 0                   # vertical pixel offset from the first data row
        self.scroll_x = 0                   # horizontal pixel offset (scrollable cols)
        # Used range the scrollbar thumb reflects, from model.used_extent(). None =
        # unknown -> full grown size. Trims blank overscroll rows/cols so the thumb
        # snaps back to the data (still scrollable into the blanks).
        self.used_rows = self.used_cols = None

    # top_row derived from scroll_y. Setter maps a target row to a row-aligned offset
    # so `g.top_row = ...` call sites snap to a whole row (keyboard/paging); wheel
    # writes scroll_y directly.
    @property
    def top_row(self):
        return self.hdr_rows + int(self.scroll_y // self.row_h)

    @top_row.setter
    def top_row(self, v):
        self.scroll_y = max(0, v - self.hdr_rows) * self.row_h

    # --- layout -------------------------------------------------------
    def set_cols(self, col_w):
        self.col_w = list(col_w)
        self._cum = [0, *accumulate(self.col_w)]      # column left-edge prefix sums

    def set_col_w(self, c, w):
        """Resize one column (drag-resize / dbl-click autofit)."""
        self.col_w[c] = max(24, int(w))
        self._cum = [0, *accumulate(self.col_w)]

    def set_metrics(self, row_h, gutter_w, col_w):
        """Rescale every pixel dimension at once (zoom). Header bands track row_h."""
        self.row_h = row_h
        self.letter_h = row_h + 2
        self.field_h = row_h
        self.header_h = self.letter_h + self.hdr_rows * self.field_h
        self.gutter_w = gutter_w
        self.set_cols(col_w)

    def frozen_w(self):
        return self._cum[self.frozen]

    def content_w(self):
        return self._cum[-1]

    def freeze_x(self):
        return self.gutter_w + self.frozen_w()

    def _phantom_w(self):
        """Phantom-column width (past the last real one): average real width, so it
        tracks zoom."""
        return max(24, self._cum[-1] // len(self.col_w)) if self.col_w else 80

    def col_left(self, c):
        """Content-space left edge of column c. Valid past the last real column
        (phantom columns are uniform _phantom_w() wide)."""
        n = len(self.col_w)
        return self._cum[c] if c <= n else self._cum[-1] + (c - n) * self._phantom_w()

    def col_width(self, c):
        """Width of column c, real or phantom."""
        return self.col_w[c] if c < len(self.col_w) else self._phantom_w()

    def col_x(self, c):
        """Left screen-x of column c (frozen columns ignore horizontal scroll)."""
        base = self.gutter_w + self.col_left(c)
        return base if c < self.frozen else base - self.scroll_x

    # --- vertical geometry (header rows pinned, the rest scroll) ------
    def row_y(self, gr):
        """Top screen-y of grid row gr. Header rows are pinned in the field band."""
        if gr < self.hdr_rows:
            return self.letter_h + gr * self.field_h
        return self.header_h + (gr - self.hdr_rows) * self.row_h - self.scroll_y

    def row_h_at(self, gr):
        return self.field_h if gr < self.hdr_rows else self.row_h

    def full_rows(self):
        return max(0, (self.h - self.header_h) // self.row_h)

    def vis_rows(self):
        return max(1, (self.h - self.header_h) // self.row_h + 1)

    def row_extent(self, nrows):
        """Row count the scrollbar/clamp see. Uncapped: grown to include the current
        overscroll, snapping back to the data on scroll-up."""
        if not self.uncap_rows:
            return nrows
        base = nrows if self.used_rows is None else self.used_rows
        return max(base, self.top_row + self.full_rows())

    def col_extent(self):
        """Scrollable content width (excludes the frozen block). Uncapped: grown to
        the current overscroll plus one phantom column of headroom, so the scrollbar
        stays visible and there's always another empty column to scroll into."""
        base = self.content_w() - self.frozen_w()
        if not self.uncap_cols:
            return base
        if self.used_cols is not None:                         # trim blank overscroll columns
            base = self.col_left(self.used_cols) - self.frozen_w()
        avail = self.w - self.freeze_x()
        grown = max(base, self.scroll_x + avail) + self._phantom_w()
        cap = self.col_left(self.MAX_COLS) - self.frozen_w()   # right edge of the 16384-column cap
        return min(grown, cap)

    def max_top(self, nrows):
        return max(self.hdr_rows, self.row_extent(nrows) - self.full_rows())

    def max_scroll_x(self):
        raw = self.col_extent() - (self.w - self.freeze_x())
        return raw + self.OVERSCROLL_PAD if raw > 0 else 0   # pad only when it overflows

    def max_scroll_y(self, nrows):
        """Vertical pixel-scroll limit: last screen shows `full_rows` whole rows
        (row-aligned bottom, matching max_top). Uncapped: add one viewport of headroom
        past the current position (twin of col_extent's phantom headroom) so the wheel
        can advance past the last data row instead of clamping where it already is."""
        cap = max(0, (self.max_top(nrows) - self.hdr_rows) * self.row_h)
        if self.uncap_rows:
            cap += self.h - self.header_h
        elif cap > 0:
            cap += self.OVERSCROLL_PAD    # pad only when it overflows
        return cap

    def clamp(self, nrows):
        self.scroll_y = max(0, min(self.scroll_y, self.max_scroll_y(nrows)))
        self.scroll_x = max(0, min(self.scroll_x, self.max_scroll_x()))

    def visible_data_rows(self, nrows):
        """Visible DATA rows (headers always pinned). Uncapped: keeps filling the
        viewport past the data with phantom rows so the gutter keeps numbering."""
        # +2 rows headroom so partially-visible rows at top AND bottom (sub-row
        # scroll) are included; off-screen extras are clipped by the surface.
        hi = self.hdr_rows + int((self.scroll_y + (self.h - self.header_h)) // self.row_h) + 2
        if not self.uncap_rows:
            hi = min(hi, nrows)
        return list(range(max(self.hdr_rows, self.top_row), hi))

    def visible_cols(self, ncols):
        fx = self.freeze_x()
        out = list(range(min(self.frozen, ncols)))    # frozen block always shown
        # First scrollable column past the freeze line: bisect the prefix sums
        # instead of scanning (O(log n) for wide sheets).
        c = max(self.frozen, bisect_right(self._cum, self.frozen_w() + self.scroll_x) - 1)
        while c < ncols and self.col_x(c) < self.w:
            if self.col_x(c) + self.col_w[c] > fx:
                out.append(c)
            c += 1
        if self.uncap_cols:                          # phantom columns fill the overscroll
            c, pw = max(c, ncols), self._phantom_w()
            while c < self.MAX_COLS:
                x = self.col_x(c)
                if x >= self.w:
                    break
                if x + pw > fx:
                    out.append(c)
                c += 1
        return out

    # --- hit testing --------------------------------------------------
    def x_to_col(self, x, ncols):
        if x < self.gutter_w:
            return None
        content_x = (x - self.gutter_w) if x < self.freeze_x() \
            else (x - self.gutter_w + self.scroll_x)
        if content_x < self._cum[-1]:                           # real column, bisect the sums
            c = bisect_right(self._cum, content_x) - 1
            return c if 0 <= c < ncols else None
        if self.uncap_cols:                                     # phantom column (capped at XFD)
            return min(self.MAX_COLS - 1,
                       ncols + int((content_x - self._cum[-1]) // self._phantom_w()))
        return None

    def hit(self, x, y, nrows, ncols):
        """(region, row, col), region in all/gutter/band/cell. The letter band
        (A/B/C) is 'band' (whole-column select); header rows are selectable 'cell's
        (bottom row's filter button is intercepted by the renderer first)."""
        col = self.x_to_col(x, ncols)
        if y < self.letter_h:                                  # column-letter band
            return ("all", 0, 0) if (x < self.gutter_w or col is None) else ("band", 0, col)
        if y < self.header_h:                                  # a pinned header row
            hr = min(self.hdr_rows - 1, int((y - self.letter_h) // self.field_h))
            if x < self.gutter_w:
                return "gutter", hr, 0
            return "cell", hr, (col if col is not None else ncols - 1)
        row = self.hdr_rows + int((self.scroll_y + y - self.header_h) // self.row_h)
        row = max(self.hdr_rows, row if self.uncap_rows else min(nrows - 1, row))
        if x < self.gutter_w:
            return "gutter", row, 0
        return "cell", row, (col if col is not None else ncols - 1)

    # --- filter button (BOTTOM header row, right edge of a column) ----
    def filter_btn_rect(self, c):
        sz = self.row_h - 8
        bx = self.col_x(c) + self.col_w[c] - sz - 4
        by = self.header_h - self.field_h + (self.field_h - sz) // 2
        return bx, by, sz

    def col_edge_hit(self, x, y, ncols, grab=7):
        """Column whose RIGHT border sits within `grab` px of `x` in the header band,
        else None. Drives drag-resize/autofit (resizes the column LEFT of the border)."""
        if not (0 <= y < self.header_h):
            return None
        # Only the border nearest the pointer can match: bisect to it, test it and
        # its neighbours (grab tolerance), instead of scanning every column.
        content_x = (x - self.gutter_w) if x < self.freeze_x() \
            else (x - self.gutter_w + self.scroll_x)
        i = bisect_left(self._cum, content_x)
        for c in (i - 1, i, i + 1):
            if not (0 <= c < ncols):
                continue
            edge = self.col_x(c) + self.col_w[c]
            if c >= self.frozen and edge < self.freeze_x() - grab:
                continue                       # scrolled left under the frozen band
            if edge >= self.gutter_w and abs(x - edge) <= grab:
                return c
        return None

    def filter_btn_hit(self, x, y, c):
        if not self.filters:                      # filters disabled
            return False
        if not (0 <= c < len(self.col_w)):        # phantom/out-of-range column
            return False
        if not (self.header_h - self.field_h <= y < self.header_h):
            return False
        bx, by, sz = self.filter_btn_rect(c)
        return bx <= x <= bx + sz and by <= y <= by + sz

    # --- dropdown button (right edge of a data cell with choices), sized to the
    # native combo arrow the renderer draws there.
    def dropdown_btn_rect(self, gr, c):
        sz = max(16, self.row_h - 4)
        bx = self.col_x(c) + self.col_w[c] - sz
        by = self.row_y(gr) + (self.row_h - sz) // 2
        return bx, by, sz

    def dropdown_btn_hit(self, x, y, gr, c):
        bx, by, sz = self.dropdown_btn_rect(gr, c)
        return bx <= x <= bx + sz and by <= y <= by + sz

    def in_corner(self, x, y):
        """Over the top-left select-all corner box (letter-band gutter)."""
        return x < self.gutter_w and y < self.letter_h

    def cell_visible(self, gr, c):
        col_ok = not (c >= self.frozen and self.col_x(c) < self.freeze_x())
        if gr < self.hdr_rows:
            return col_ok
        return col_ok and self.top_row <= gr < self.top_row + self.vis_rows()

    def drag_row(self, y, nrows):
        """Row a cell-drag targets from a pointer y. Above the body it reaches a
        header row ONLY when the body is at the top, else clamps to the topmost
        visible data row so autoscroll reveals rows one at a time instead of
        jumping onto the header band."""
        if y >= self.header_h:
            row = self.hdr_rows + int((self.scroll_y + y - self.header_h) // self.row_h)
            return max(self.hdr_rows, row if self.uncap_rows else min(nrows - 1, row))
        if self.top_row <= self.hdr_rows:
            return max(0, min(self.hdr_rows - 1, int((y - self.letter_h) // self.field_h)))
        return self.top_row

    def scroll_into_view(self, gr, c):
        if gr >= self.hdr_rows:                       # pixel-exact reveal so a partially-
            y = self.row_y(gr)                         # scrolled row snaps flush
            if y < self.header_h:
                self.scroll_y = max(0, self.scroll_y - (self.header_h - y))
            elif y + self.row_h > self.h:
                self.scroll_y += (y + self.row_h) - self.h
        if c >= self.frozen:
            x = self.col_x(c)
            cw = self.col_width(c)
            if x < self.freeze_x():
                self.scroll_x -= (self.freeze_x() - x)
            elif x + cw > self.w:
                self.scroll_x += x + cw - self.w
