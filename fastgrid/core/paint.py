"""paint() -> a display list. Pure data, draws nothing.

Given the model, geometry, active cell and selection ranges, it returns the
~visible viewport as toolkit-neutral draw ops any renderer can blit. Every
layout / z-order / colour decision lives here exactly once.

    dl.cells    : [(x, y, w, h, text, bg, fg, flags), ...]   back-to-front
    dl.overlays : chrome drawn AFTER all cells --
        ("line",      x1, y1, x2, y2, color, width)
        ("rect",      x, y, w, h, color, width)          # outline only
        ("filterbtn", bx, by, sz, state)                 # state: funnel|asc|desc|idle
        ("tri",       x1, y1, sz, color)                 # corner select-all triangle

Grid rows: 0 is the header (field names), pinned in the field-name band and
numbered "1"; rows 1..N are data (numbered 2..N+1). Cells are emitted
back-to-front (data body, gutter, header row, letter band, corner) so a dumb
front-to-back renderer gets correct occlusion without a clip region.
"""
from . import theme as T


class DisplayList:
    __slots__ = ("cells", "overlays")

    def __init__(self):
        self.cells = []
        self.overlays = []


def edit_colors(gr):
    """(bg, fg) an in-cell editor should use to blend with the cell it edits:
    the zebra body colour (or the dark header) and its matching text colour."""
    if gr == 0:
        return T.FIELD_BG, T.FIELD_FG
    return (T.ZEBRA if gr % 2 else T.BG), T.TXT


def column_label(col):
    """spreadsheet column letters: 0 -> A, 25 -> Z, 26 -> AA, …"""
    label, col = "", col + 1
    while col:
        col, rem = divmod(col - 1, 26)
        label = chr(ord("A") + rem) + label
    return label


def _in_ranges(ranges, r, c):
    for r1, c1, r2, c2 in ranges:
        if r1 <= r <= r2 and c1 <= c <= c2:
            return True
    return False


def _blend(base, over, a):
    """`over` painted at opacity `a` on top of opaque `base` (both #rrggbb)."""
    b = (int(base[1:3], 16), int(base[3:5], 16), int(base[5:7], 16))
    o = (int(over[1:3], 16), int(over[3:5], 16), int(over[5:7], 16))
    return "#%02x%02x%02x" % tuple(round(b[i] * (1 - a) + o[i] * a) for i in range(3))


SEL_WASH_A = 0.6            # selection tint opacity (so the zebra still shows through)


def _sel_rect(g, r1, c1, r2, c2, w, h):
    """Screen rect for a selection range, clipped -- spans the pinned header row
    (0) and/or scrolling data rows."""
    x1 = g.col_x(c1)
    x2 = g.col_x(c2) + g.col_w[c2]
    if c1 >= g.frozen:
        x1 = max(x1, g.freeze_x())
    x1 = max(g.gutter_w, x1)
    y1 = g.letter_h if r1 == 0 else max(g.header_h, g.row_y(r1))
    if r2 == 0:
        y2 = g.header_h
    else:
        y2 = min(h, g.row_y(r2) + g.row_h)
        if r1 == 0:
            y2 = max(y2, g.header_h)     # keep the header's ring even if data scrolled off
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, min(x2, w) - x1, y2 - y1)


def paint(model, geom, active, ranges, hover_corner=False):
    g = geom
    g.clamp(model.nrows())
    dl = DisplayList()
    w, h = g.w, g.h
    fx = g.freeze_x()
    lh, bh = g.letter_h, g.header_h
    nrows, ncols = model.nrows(), model.ncols
    data_rows = g.visible_data_rows(nrows)
    cols = g.visible_cols(ncols)
    norm = [(min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2))
            for (r1, c1, r2, c2) in ranges]

    def col_sel(c):
        return any(c1 <= c <= c2 for (_r1, c1, _r2, c2) in norm)

    def row_sel(r):
        return any(r1 <= r <= r2 for (r1, _c1, r2, _c2) in norm)

    # A lone selected cell gets only the orange outline -- no fill. The grey wash
    # appears only when the selection covers more than one cell.
    single_cell = (len(norm) == 1 and norm[0][0] == norm[0][2] and norm[0][1] == norm[0][3])

    def washed(gr, c):
        return not single_cell and _in_ranges(norm, gr, c)

    wash_even = _blend(T.BG, T.SEL_TINT, SEL_WASH_A)      # tint over white / zebra rows
    wash_odd = _blend(T.ZEBRA, T.SEL_TINT, SEL_WASH_A)
    wash_hdr = _blend(T.FIELD_BG, T.SEL_TINT, SEL_WASH_A)

    # body: data rows (grid >= 1), scrollable columns first then frozen (on top)
    for band in (False, True):
        for c in cols:
            if (c < g.frozen) != band:
                continue
            x, cw = g.col_x(c), g.col_w[c]
            for gr in data_rows:
                y = g.row_y(gr)
                st = model.find_state(gr, c)
                if st == 2:
                    bg = T.FIND_ACTIVE
                elif washed(gr, c):
                    bg = wash_odd if gr % 2 else wash_even    # grey tint over the zebra
                elif st == 1:
                    bg = T.FIND_MATCH
                else:
                    bg = T.ZEBRA if gr % 2 else T.BG
                dl.cells.append((x, y, cw, g.row_h, model.cell(gr, c), bg, T.TXT, 0))

    # gutter for data rows -- numbered gr+1 (so data starts at "2")
    for gr in data_rows:
        y = g.row_y(gr)
        bg = T.SEL_HDR if row_sel(gr) else T.HEADER_BG
        dl.cells.append((0, y, g.gutter_w, g.row_h, str(gr + 1), bg, T.LETTER_FG, T.FLAG_CENTER))

    # header row (grid row 0): field names, pinned, selectable, + filter button
    for band in (False, True):
        for c in cols:
            if (c < g.frozen) != band:
                continue
            x, cw = g.col_x(c), g.col_w[c]
            # The field-name row does NOT act as a column indicator (only the A/B/C
            # letter band does); it tints only when the header cell itself is selected.
            if washed(0, c):
                bg, fg = wash_hdr, T.FIELD_FG       # grey tint over the field-name row
            else:
                bg, fg = T.FIELD_BG, T.FIELD_FG
            dl.cells.append((x, lh, cw, g.field_h, model.cell(0, c), bg, fg, T.FLAG_BOLD))
            if model.has_filter(c):
                state = "funnel"
            elif model.has_sort(c):
                state = "asc" if model.sort_ascending(c) else "desc"
            else:
                state = "idle"
            bx, by, sz = g.filter_btn_rect(c)
            dl.overlays.append(("filterbtn", bx, by, sz, state))

    # header-row gutter -> "1"
    dl.cells.append((0, lh, g.gutter_w, g.field_h, "1",
                     T.SEL_HDR if row_sel(0) else T.HEADER_BG, T.LETTER_FG, T.FLAG_CENTER))

    # column-letter band (A, B, C…)
    for band in (False, True):
        for c in cols:
            if (c < g.frozen) != band:
                continue
            x, cw = g.col_x(c), g.col_w[c]
            bg = T.SEL_HDR if col_sel(c) else T.LETTER_BG
            dl.cells.append((x, 0, cw, lh, column_label(c), bg, T.LETTER_FG, T.FLAG_CENTER))

    # corner (letter-band gutter)
    dl.cells.append((0, 0, g.gutter_w, lh, "", T.HEADER_BG, T.LETTER_FG, 0))

    # chrome
    if g.frozen > 0:
        dl.overlays.append(("line", fx, 0, fx, h, T.DIVIDER, 1))
    dl.overlays.append(("tri", g.gutter_w - 3, lh - 3, max(5, min(g.gutter_w, lh) // 2),
                        T.ACCENT if hover_corner else T.GRID))
    for (r1, c1, r2, c2) in norm:
        rect = _sel_rect(g, r1, c1, r2, c2, w, h)
        if rect:
            dl.overlays.append(("rect", *rect, T.SEL_RING, 2))   # match the single-cell ring
    ar, ac = active
    if g.cell_visible(ar, ac):
        x = max(g.gutter_w, g.col_x(ac))
        x2 = min(g.col_x(ac) + g.col_w[ac], w)
        if x2 > x:
            dl.overlays.append(("rect", x, g.row_y(ar), x2 - x, g.row_h_at(ar), T.SEL_RING, 2))
    return dl
