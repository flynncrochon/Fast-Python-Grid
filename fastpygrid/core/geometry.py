"""All the grid MATH: no toolkit, no colours. Column x-positions, scroll
clamping, pixel<->cell hit-testing, visible-range computation. Both renderers
share this instead of each re-deriving it.

Grid rows: 0..hdr_rows-1 are header rows (field names on the bottom one, any
rows above it are group bands), pinned in the field band. Rows hdr_rows..N are
data and scroll in the body. ``header_h`` (letter band + header rows) is where
the scrolling body begins. ``top_row`` is the first visible DATA row (>= hdr_rows).
"""
from bisect import bisect_left, bisect_right
from itertools import accumulate


class Geometry:
    MAX_COLS = 16384          # column cap (the classic spreadsheet limit), uncapped scroll stops there
    def __init__(self, col_w, frozen=0, gutter_w=56, row_h=22, hdr_rows=1,
                 uncap_rows=False, uncap_cols=False):
        # uncap_*: let the view scroll past the last row/col into empty space
        # (spreadsheet-style). The scrollbar thumb shrinks as you overscroll and snaps
        # back when you scroll in again, unless you typed out there, which grows
        # the model. False (default) = clamped to the data.
        self.uncap_rows = uncap_rows
        self.uncap_cols = uncap_cols
        self.frozen = frozen
        self.gutter_w = gutter_w
        self.hdr_rows = max(1, hdr_rows)
        self.row_h = row_h
        self.letter_h = row_h + 2          # column-letter band (A, B, C…)
        self.field_h = row_h               # height of ONE header row
        self.header_h = self.letter_h + self.hdr_rows * self.field_h   # body starts here
        self.set_cols(col_w)
        self.w = self.h = 0                 # viewport size, set by the renderer
        self.top_row = self.hdr_rows        # first visible DATA row
        self.scroll_x = 0                   # horizontal pixel offset (scrollable cols)
        # Used range (nrows-equiv, ncols) the scrollbar thumb reflects, set by the
        # renderer from model.used_extent(). None = unknown -> fall back to the full
        # grown size. Trims blank rows/cols left behind by overscroll editing so the
        # thumb snaps back to the data (you can still scroll into the blanks).
        self.used_rows = self.used_cols = None

    # --- layout -------------------------------------------------------
    def set_cols(self, col_w):
        self.col_w = list(col_w)
        self._cum = [0, *accumulate(self.col_w)]      # column left-edge prefix sums

    def set_col_w(self, c, w):
        """Resize one column (used by drag-resize / dbl-click autofit)."""
        self.col_w[c] = max(24, int(w))
        self._cum = [0, *accumulate(self.col_w)]

    def set_metrics(self, row_h, gutter_w, col_w):
        """Rescale every pixel dimension at once (zoom). Header bands track row_h
        as __init__ derives them, so a zoom matches building the grid at that size."""
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
        """Width of a phantom column (past the last real one, for uncapped
        overscroll): the average real width, so it tracks zoom and looks native."""
        return max(24, self._cum[-1] // len(self.col_w)) if self.col_w else 80

    def col_left(self, c):
        """Content-space left edge of column c. Valid past the last real column:
        phantom columns are uniform _phantom_w() wide (spreadsheet-style empty columns)."""
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
        return self.header_h + (gr - self.top_row) * self.row_h

    def row_h_at(self, gr):
        return self.field_h if gr < self.hdr_rows else self.row_h

    def full_rows(self):
        return max(0, (self.h - self.header_h) // self.row_h)

    def vis_rows(self):
        return max(1, (self.h - self.header_h) // self.row_h + 1)

    def row_extent(self, nrows):
        """Row count the scrollbar and clamp see. Uncapped: grown to include the
        current overscroll, so the thumb shrinks past the end and the extent snaps
        back to the data when you scroll up again."""
        if not self.uncap_rows:
            return nrows
        base = nrows if self.used_rows is None else self.used_rows
        return max(base, self.top_row + self.full_rows())

    def col_extent(self):
        """Scrollable content width (excludes the frozen block). Uncapped: grown to
        include the current horizontal overscroll, plus one phantom column of
        headroom so the extent always exceeds the view: the scrollbar stays
        visible and there's always another empty column to scroll into."""
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
        return max(0, self.col_extent() - (self.w - self.freeze_x()))

    def clamp(self, nrows):
        self.top_row = max(self.hdr_rows, min(self.top_row, self.max_top(nrows)))
        self.scroll_x = max(0, min(self.scroll_x, self.max_scroll_x()))

    def visible_data_rows(self, nrows):
        """Visible DATA grid rows, the header rows are always pinned. Uncapped:
        keeps filling the viewport past the data with phantom (blank) rows, so the
        gutter keeps numbering, spreadsheet-style."""
        hi = self.top_row + self.vis_rows()
        if not self.uncap_rows:
            hi = min(hi, nrows)
        return list(range(max(self.hdr_rows, self.top_row), hi))

    def visible_cols(self, ncols):
        fx = self.freeze_x()
        out = list(range(min(self.frozen, ncols)))    # frozen block always shown
        # First scrollable real column poking past the freeze line: bisect the
        # prefix sums instead of scanning every column (O(log n) for wide sheets).
        c = max(self.frozen, bisect_right(self._cum, self.frozen_w() + self.scroll_x) - 1)
        while c < ncols and self.col_x(c) < self.w:
            if self.col_x(c) + self.col_w[c] > fx:
                out.append(c)
            c += 1
        if self.uncap_cols:                          # phantom columns fill the overscroll
            c, pw = max(c, ncols), self._phantom_w()  # (empty, lettered, spreadsheet-style, capped at 16384)
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
        if content_x < self._cum[-1]:                           # a real column, bisect the sums
            c = bisect_right(self._cum, content_x) - 1
            return c if 0 <= c < ncols else None
        if self.uncap_cols:                                     # a phantom column (capped at XFD)
            return min(self.MAX_COLS - 1,
                       ncols + int((content_x - self._cum[-1]) // self._phantom_w()))
        return None

    def hit(self, x, y, nrows, ncols):
        """(region, row, col). region in all/gutter/band/cell.

        The column-letter band (A/B/C) is 'band' (whole-column select). The
        header rows are normal selectable 'cell's (the bottom one's filter
        button is intercepted by the renderer before selection)."""
        col = self.x_to_col(x, ncols)
        if y < self.letter_h:                                  # column-letter band
            return ("all", 0, 0) if (x < self.gutter_w or col is None) else ("band", 0, col)
        if y < self.header_h:                                  # a pinned header row
            hr = min(self.hdr_rows - 1, int((y - self.letter_h) // self.field_h))
            if x < self.gutter_w:
                return "gutter", hr, 0
            return "cell", hr, (col if col is not None else ncols - 1)
        row = self.top_row + int((y - self.header_h) // self.row_h)
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

    def col_edge_hit(self, x, y, ncols, grab=4):
        """Column whose RIGHT border sits within `grab` px of screen-x `x`, when
        the pointer is in the header band, else None. Drives column drag-resize
        and dbl-click autofit, resizes the column LEFT of the grabbed border."""
        if not (0 <= y < self.header_h):
            return None
        # Only the border nearest the pointer can match: bisect to it, then test
        # it and its neighbours (grab tolerance), instead of scanning every column.
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
        if not (0 <= c < len(self.col_w)):        # phantom/out-of-range column: no filter button
            return False
        if not (self.header_h - self.field_h <= y < self.header_h):
            return False
        bx, by, sz = self.filter_btn_rect(c)
        return bx <= x <= bx + sz and by <= y <= by + sz

    # --- dropdown button (right edge of a data cell with choices). Sized to the
    # native combo arrow the renderer draws there, flush to the cell's right edge.
    def dropdown_btn_rect(self, gr, c):
        sz = max(16, self.row_h - 4)
        bx = self.col_x(c) + self.col_w[c] - sz
        by = self.row_y(gr) + (self.row_h - sz) // 2
        return bx, by, sz

    def dropdown_btn_hit(self, x, y, gr, c):
        bx, by, sz = self.dropdown_btn_rect(gr, c)
        return bx <= x <= bx + sz and by <= y <= by + sz

    def in_corner(self, x, y):
        """True over the top-left select-all corner box (letter-band gutter)."""
        return x < self.gutter_w and y < self.letter_h

    def cell_visible(self, gr, c):
        col_ok = not (c >= self.frozen and self.col_x(c) < self.freeze_x())
        if gr < self.hdr_rows:
            return col_ok
        return col_ok and self.top_row <= gr < self.top_row + self.vis_rows()

    def drag_row(self, y, nrows):
        """Row a cell-drag targets from a pointer y. Above the body it reaches a
        header row ONLY when the body is at the top, otherwise it clamps to the
        topmost visible data row so the caller's autoscroll reveals rows above
        one at a time instead of jumping onto the header band."""
        if y >= self.header_h:
            row = self.top_row + int((y - self.header_h) // self.row_h)
            return max(self.hdr_rows, row if self.uncap_rows else min(nrows - 1, row))
        if self.top_row <= self.hdr_rows:
            return max(0, min(self.hdr_rows - 1, int((y - self.letter_h) // self.field_h)))
        return self.top_row

    def scroll_into_view(self, gr, c):
        if gr >= self.hdr_rows:
            if gr < self.top_row:
                self.top_row = gr
            elif gr >= self.top_row + self.full_rows():
                self.top_row = gr - self.full_rows() + 1
        if c >= self.frozen:
            x = self.col_x(c)
            cw = self.col_width(c)
            if x < self.freeze_x():
                self.scroll_x -= (self.freeze_x() - x)
            elif x + cw > self.w:
                self.scroll_x += x + cw - self.w


if __name__ == "__main__":   # overscroll clamp / extent self-check (no toolkit)
    g = Geometry([100, 100], hdr_rows=1)
    g.w, g.h = 400, 220
    N = 20
    # capped (default): scroll clamps to the data extent
    g.top_row = 999; g.clamp(N)
    assert g.top_row == g.max_top(N) <= N and g.row_extent(N) == N, g.top_row
    # uncapped: overscroll sticks, extent grows to match, snaps back on scroll-up
    g.uncap_rows = True
    g.top_row = 999; g.clamp(N)
    assert g.top_row == 999 and g.row_extent(N) == 999 + g.full_rows(), g.top_row
    g.top_row = 1; g.clamp(N)
    assert g.top_row == g.hdr_rows and g.row_extent(N) == N, g.top_row
    # columns overscroll the same way (cols wider than the viewport)
    g.set_cols([400, 400])
    g.uncap_cols = True
    g.scroll_x = 99999; g.clamp(N)
    assert 0 < g.scroll_x <= g.max_scroll_x(), g.scroll_x   # overscroll sticks, still room past it
    g.scroll_x = 0; g.clamp(N)
    base = g.content_w() - g.frozen_w()
    assert g.scroll_x == 0 and g.col_extent() == base + g._phantom_w()  # snapped back, one column of headroom
    assert g.max_scroll_x() > 0                              # ...but always room to scroll further right
    # phantom rows/cols keep filling the viewport past the data when uncapped, so the
    # gutter keeps numbering and the letter band keeps lettering (spreadsheet-style)
    g2 = Geometry([80, 80], hdr_rows=1, uncap_rows=True, uncap_cols=True)
    g2.w, g2.h = 300, 200
    vr = g2.visible_data_rows(5)            # only 5 grid rows of data
    assert vr and max(vr) > 5, vr           # rows numbered past the data
    vc = g2.visible_cols(2)                 # only 2 real columns
    assert vc and max(vc) > 2, vc           # columns lettered past the data
    assert g2.col_width(50) == g2._phantom_w() and g2.col_x(4) > g2.col_x(3)
    # uncapped columns stop at the 16384-column cap: can't scroll or hit past it
    g2.scroll_x = 10**9; g2.clamp(5)
    assert g2.x_to_col(g2.w - 1, 2) <= Geometry.MAX_COLS - 1
    assert max(g2.visible_cols(2)) <= Geometry.MAX_COLS - 1
    # hit-testing a phantom column's header must not index col_w (no filter button there)
    assert g2.filter_btn_hit(g2.w - 1, g2.header_h - 1, 500) is False
    # used_rows/used_cols trim the thumb's base to real content, even after the model
    # grew: scrolled back to the top, the extent reflects the used range, not the grown size
    gt = Geometry([80, 80], hdr_rows=1, uncap_rows=True, uncap_cols=True)
    gt.w, gt.h = 300, 200
    gt.top_row = gt.hdr_rows                          # scrolled back to the top
    gt.used_rows, gt.used_cols = 8, 2                 # 2 real cols, few used rows (rest blank overscroll)
    assert gt.row_extent(9999) == max(8, gt.top_row + gt.full_rows())   # not 9999
    assert gt.col_extent() < gt.col_left(9999)        # not the grown width
    # bisect hit-testing matches a brute-force linear scan at every pixel
    g3 = Geometry([40, 90, 25, 70, 60], frozen=1); g3.w, g3.h = 300, 200; g3.scroll_x = 55
    for x in range(0, 320):
        want = next((c for c in range(5) if g3._cum[c] <= (
            (x - g3.gutter_w) if x < g3.freeze_x() else (x - g3.gutter_w + g3.scroll_x)
        ) < g3._cum[c + 1]), None) if x >= g3.gutter_w else None
        assert g3.x_to_col(x, 5) == want, (x, g3.x_to_col(x, 5), want)
    print("geometry overscroll self-check ok")
