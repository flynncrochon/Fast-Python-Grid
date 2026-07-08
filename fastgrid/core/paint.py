"""paint() -> a display list. Pure data, draws nothing.

Given the model, geometry, active cell and selection ranges, it returns the
~visible viewport as toolkit-neutral draw ops any renderer can blit. Every
layout / z-order / colour decision lives here exactly once.

    dl.cells    : [(x, y, w, h, text, bg, fg, flags), ...]   back-to-front
    dl.overlays : chrome drawn AFTER all cells --
        ("line",      x1, y1, x2, y2, color, width)
        ("ring",      x1, y1, x2, y2)                    # selection-outline edge (SEL_RING, 2px)
        ("filterbtn", bx, by, sz, state)                 # state: funnel|asc|desc|idle
        ("tri",       x1, y1, sz, color)                 # corner select-all triangle

Grid rows: 0 is the header (field names), pinned in the field-name band and
numbered "1"; rows 1..N are data (numbered 2..N+1). Cells are emitted
back-to-front (data body, gutter, header row, letter band, corner) so a dumb
front-to-back renderer gets correct occlusion without a clip region.
"""
from . import theme as T
from .selection import normalize


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


SEL_WASH_A = 0.45           # selection tint opacity (so the zebra still shows through)


def paint(model, geom, active, ranges, hover_corner=False, row_range=None):
    g = geom
    g.clamp(model.nrows())
    dl = DisplayList()
    w, h = g.w, g.h
    fx = g.freeze_x()
    lh, bh = g.letter_h, g.header_h
    nrows, ncols = model.nrows(), model.ncols
    # row_range=(r0, r1) materialises an explicit band (Tk's native-scroll cache
    # paints viewport+overscan at once); default = just the visible rows.
    if row_range is None:
        data_rows = g.visible_data_rows(nrows)
    else:
        r0, r1 = row_range
        data_rows = [r for r in range(r0, r1) if 1 <= r <= nrows - 1]
    cols = g.visible_cols(ncols)
    norm = normalize(ranges)

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

    cell_style = model.cell_style
    # body: data rows (grid >= 1), scrollable columns first then frozen (on top)
    for band in (False, True):
        for c in cols:
            if (c < g.frozen) != band:
                continue
            x, cw = g.col_x(c), g.col_w[c]
            for gr in data_rows:
                y = g.row_y(gr)
                st = model.find_state(gr, c)
                sty = cell_style(gr, c)
                fg, flags, base = T.TXT, 0, None
                if sty:
                    fg = sty.get("fg", T.TXT)
                    flags = T.FLAG_BOLD if sty.get("bold") else 0
                    base = sty.get("bg")                      # user fill, else zebra
                if st == 2:
                    bg = T.FIND_ACTIVE
                elif washed(gr, c):                           # selection tints over the fill
                    bg = _blend(base, T.SEL_TINT, SEL_WASH_A) if base else (wash_odd if gr % 2 else wash_even)
                elif st == 1:
                    bg = T.FIND_MATCH
                else:
                    bg = base if base else (T.ZEBRA if gr % 2 else T.BG)
                dl.cells.append((x, y, cw, g.row_h, model.cell(gr, c), bg, fg, flags))

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
            sty = cell_style(0, c)
            fg = sty.get("fg", T.FIELD_FG) if sty else T.FIELD_FG
            base = sty.get("bg") if sty else None             # header is always bold
            if washed(0, c):
                bg = _blend(base, T.SEL_TINT, SEL_WASH_A) if base else wash_hdr
            else:
                bg = base if base else T.FIELD_BG
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
    # corner triangle: selection orange whenever a real selection exists (Ctrl+A
    # or any multi-cell/banked range), hover orange on hover, else inert grey --
    # unlit once the selection collapses to a single cell or nothing.
    selected = bool(norm) and not single_cell
    tri_color = T.SEL_RING if selected else T.ACCENT if hover_corner else T.GRID
    dl.overlays.append(("tri", g.gutter_w - 3, lh - 3, max(5, min(g.gutter_w, lh) // 2),
                        tri_color))
    # Selection outline = perimeter of the selected union: emit an edge only where
    # the neighbour across it isn't selected. Adjacent ranges merge into one outline
    # (no doubled line), and the active cell gets no separate internal box.
    # one segment per perimeter cell-edge; coalesce colinear runs only if
    # a huge selection's drag ever feels slow.
    ar, ac = active

    def sel_at(r, c):
        return (r == ar and c == ac) or _in_ranges(norm, r, c)

    for gr in [0] + data_rows:
        y0, y1, lo_y = (lh, bh, lh) if gr == 0 else (g.row_y(gr), g.row_y(gr) + g.row_h, bh)
        for c in cols:
            if not sel_at(gr, c):
                continue
            x0 = g.col_x(c)
            x1 = x0 + g.col_w[c]
            lo_x = max(g.gutter_w, fx) if c >= g.frozen else g.gutter_w
            cx0, cx1 = max(x0, lo_x), min(x1, w)      # clip to gutter/frozen/viewport
            cy0, cy1 = max(y0, lo_y), min(y1, h)
            if cx0 >= cx1 or cy0 >= cy1:
                continue                              # hidden under gutter/frozen or off-screen
            if not sel_at(gr - 1, c):
                dl.overlays.append(("ring", cx0, cy0, cx1, cy0))   # top
            if not sel_at(gr + 1, c):
                dl.overlays.append(("ring", cx0, cy1, cx1, cy1))   # bottom
            if not sel_at(gr, c - 1):
                dl.overlays.append(("ring", cx0, cy0, cx0, cy1))   # left
            if not sel_at(gr, c + 1):
                dl.overlays.append(("ring", cx1, cy0, cx1, cy1))   # right
    return dl
