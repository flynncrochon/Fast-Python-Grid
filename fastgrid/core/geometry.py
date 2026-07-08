"""All the grid MATH -- no toolkit, no colours. Column x-positions, scroll
clamping, pixel<->cell hit-testing, visible-range computation. Both renderers
share this instead of each re-deriving it.

Grid rows: 0 is the header (field names), pinned at the field-name band; rows
1..N are data and scroll in the body. ``header_h`` (letter band + field row) is
where the scrolling body begins. ``top_row`` is the first visible DATA row (>=1).
"""


class Geometry:
    def __init__(self, col_w, frozen=0, gutter_w=56, row_h=22):
        self.frozen = frozen
        self.gutter_w = gutter_w
        self.row_h = row_h
        self.letter_h = row_h + 2          # column-letter band (A, B, C…)
        self.field_h = row_h               # field-name row = grid row 0
        self.header_h = self.letter_h + self.field_h   # body (data rows) starts here
        self.set_cols(col_w)
        self.w = self.h = 0                 # viewport size, set by the renderer
        self.top_row = 1                    # first visible DATA row (grid row >= 1)
        self.scroll_x = 0                   # horizontal pixel offset (scrollable cols)

    # --- layout -------------------------------------------------------
    @staticmethod
    def _prefix(widths):
        cum, acc = [0], 0
        for w in widths:
            acc += w
            cum.append(acc)
        return cum

    def set_cols(self, col_w):
        self.col_w = list(col_w)
        self._cum = self._prefix(self.col_w)

    def set_col_w(self, c, w):
        """Resize one column (used by drag-resize / dbl-click autofit)."""
        self.col_w[c] = max(24, int(w))
        self._cum = self._prefix(self.col_w)

    def set_metrics(self, row_h, gutter_w, col_w):
        """Rescale every pixel dimension at once (zoom). The header bands track
        row_h exactly as __init__ derives them, so a zoom looks identical to
        having constructed the grid at that size. Cheap: renderers are
        viewport-virtualized, so the follow-up redraw is O(visible), not O(rows)."""
        self.row_h = row_h
        self.letter_h = row_h + 2
        self.field_h = row_h
        self.header_h = self.letter_h + self.field_h
        self.gutter_w = gutter_w
        self.set_cols(col_w)

    def frozen_w(self):
        return self._cum[self.frozen]

    def content_w(self):
        return self._cum[-1]

    def freeze_x(self):
        return self.gutter_w + self.frozen_w()

    def col_x(self, c):
        """Left screen-x of column c (frozen columns ignore horizontal scroll)."""
        base = self.gutter_w + self._cum[c]
        return base if c < self.frozen else base - self.scroll_x

    # --- vertical geometry (row 0 pinned, rows 1..N scroll) -----------
    def row_y(self, gr):
        """Top screen-y of grid row gr. Row 0 is pinned in the field band."""
        return self.letter_h if gr == 0 else self.header_h + (gr - self.top_row) * self.row_h

    def row_h_at(self, gr):
        return self.field_h if gr == 0 else self.row_h

    def full_rows(self):
        return max(0, (self.h - self.header_h) // self.row_h)

    def vis_rows(self):
        return max(1, (self.h - self.header_h) // self.row_h + 1)

    def max_top(self, nrows):
        return max(1, nrows - self.full_rows())

    def max_scroll_x(self):
        avail = self.w - self.freeze_x()
        return max(0, self.content_w() - self.frozen_w() - avail)

    def clamp(self, nrows):
        self.top_row = max(1, min(self.top_row, self.max_top(nrows)))
        self.scroll_x = max(0, min(self.scroll_x, self.max_scroll_x()))

    def visible_data_rows(self, nrows):
        """Visible DATA grid rows (>=1); the header (row 0) is always pinned."""
        return [self.top_row + i for i in range(self.vis_rows())
                if 1 <= self.top_row + i <= nrows - 1]

    def visible_cols(self, ncols):
        fx = self.freeze_x()
        out = []
        for c in range(ncols):
            x = self.col_x(c)
            if c < self.frozen or (x + self.col_w[c] > fx and x < self.w):
                out.append(c)
        return out

    # --- hit testing --------------------------------------------------
    def x_to_col(self, x, ncols):
        if x < self.gutter_w:
            return None
        content_x = (x - self.gutter_w) if x < self.freeze_x() \
            else (x - self.gutter_w + self.scroll_x)
        for c in range(ncols):
            if self._cum[c] <= content_x < self._cum[c + 1]:
                return c
        return None

    def hit(self, x, y, nrows, ncols):
        """(region, row, col). region in all/gutter/band/cell.

        The column-letter band (A/B/C) is 'band' (whole-column select); the
        field-name row is grid row 0 -- a normal selectable 'cell' (its filter
        button is intercepted by the renderer before selection)."""
        col = self.x_to_col(x, ncols)
        if y < self.letter_h:                                  # column-letter band
            return ("all", 0, 0) if (x < self.gutter_w or col is None) else ("band", 0, col)
        if y < self.header_h:                                  # field-name row = grid row 0
            if x < self.gutter_w:
                return "gutter", 0, 0
            return "cell", 0, (col if col is not None else ncols - 1)
        row = self.top_row + int((y - self.header_h) // self.row_h)
        row = max(1, min(nrows - 1, row))
        if x < self.gutter_w:
            return "gutter", row, 0
        return "cell", row, (col if col is not None else ncols - 1)

    # --- filter button (field-name row, right edge of a column) -------
    def filter_btn_rect(self, c):
        sz = self.row_h - 8
        bx = self.col_x(c) + self.col_w[c] - sz - 4
        by = self.letter_h + (self.field_h - sz) // 2
        return bx, by, sz

    def col_edge_hit(self, x, y, ncols, grab=4):
        """Column whose RIGHT border sits within `grab` px of screen-x `x`, when
        the pointer is in the header band -- else None. Drives column drag-resize
        and dbl-click autofit; resizes the column LEFT of the grabbed border."""
        if not (0 <= y < self.header_h):
            return None
        for c in range(ncols):
            edge = self.col_x(c) + self.col_w[c]
            if c >= self.frozen and edge < self.freeze_x() - grab:
                continue                       # scrolled left under the frozen band
            if edge >= self.gutter_w and abs(x - edge) <= grab:
                return c
        return None

    def filter_btn_hit(self, x, y, c):
        if not (self.letter_h <= y < self.header_h):
            return False
        bx, by, sz = self.filter_btn_rect(c)
        return bx <= x <= bx + sz and by <= y <= by + sz

    def in_corner(self, x, y):
        """True over the top-left select-all corner box (letter-band gutter)."""
        return x < self.gutter_w and y < self.letter_h

    def cell_visible(self, gr, c):
        col_ok = not (c >= self.frozen and self.col_x(c) < self.freeze_x())
        if gr == 0:
            return col_ok
        return col_ok and self.top_row <= gr < self.top_row + self.vis_rows()

    def drag_row(self, y, nrows):
        """Row a cell-drag targets from a pointer y. Above the body it reaches the
        header (row 0) ONLY when the body is at the top (top_row==1); otherwise it
        clamps to the topmost visible data row so the caller's autoscroll reveals
        rows above one at a time instead of jumping onto the header band."""
        if y >= self.header_h:
            return max(1, min(nrows - 1, self.top_row + int((y - self.header_h) // self.row_h)))
        return 0 if self.top_row <= 1 else self.top_row

    def scroll_into_view(self, gr, c):
        if gr >= 1:
            if gr < self.top_row:
                self.top_row = gr
            elif gr >= self.top_row + self.full_rows():
                self.top_row = gr - self.full_rows() + 1
        if c >= self.frozen:
            x = self.col_x(c)
            if x < self.freeze_x():
                self.scroll_x -= (self.freeze_x() - x)
            elif x + self.col_w[c] > self.w:
                self.scroll_x += x + self.col_w[c] - self.w
