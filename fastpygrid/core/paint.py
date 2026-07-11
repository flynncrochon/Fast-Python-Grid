"""paint() -> a display list. Pure data, draws nothing.

Given the model, geometry, active cell and selection ranges, it returns the
~visible viewport as toolkit-neutral draw ops any renderer can blit. Every
layout / z-order / colour decision lives here exactly once.

    dl.cells    : [(x, y, w, h, text, bg, fg, flags), ...]   back-to-front
    dl.overlays : chrome drawn AFTER all cells --
        ("line",      x1, y1, x2, y2, color, width)      # frozen-pane divider
        ("vline"|"hline", x1, y1, x2, y2, color, width)  # thick section dividers
        ("ring",      x1, y1, x2, y2)                    # selection-outline edge (SEL_RING, 2px)
        ("filterbtn", bx, by, sz, state)                 # state: funnel|asc|desc|idle
        ("tri",       x1, y1, sz, color)                 # corner select-all triangle
        ("dropdown",  x, y, w, h)                         # native drop button at cell's right

Grid rows: 0..H-1 are the pinned header rows (field names on the bottom one;
rows above it are GROUP bands whose adjacent same-label cells merge into one
spanning cell); rows H..N are data. Cells are emitted back-to-front (data body,
gutter, header rows, letter band, corner) so a dumb front-to-back renderer gets
correct occlusion without a clip region.
"""
from . import theme as T
from .selection import normalize


class DisplayList:
    __slots__ = ("cells", "overlays")

    def __init__(self):
        self.cells = []
        self.overlays = []


def edit_colors(gr, hdr_rows=1):
    """(bg, fg) an in-cell editor should use to blend with the cell it edits:
    the zebra body colour (or the dark header) and its matching text colour."""
    if gr < hdr_rows:
        return T.FIELD_BG, T.FIELD_FG
    return (T.ZEBRA if (gr - hdr_rows) % 2 == 0 else T.BG), T.TXT


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


def paint(model, geom, active, ranges, hover_corner=False):
    g = geom
    g.clamp(model.nrows())
    dl = DisplayList()
    w, h = g.w, g.h
    fx = g.freeze_x()
    lh, bh = g.letter_h, g.header_h
    H = g.hdr_rows
    nrows, ncols = model.nrows(), model.ncols
    data_rows = g.visible_data_rows(nrows)
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
    cell_choices = model.cell_choices
    # dropdown buttons only on editable grids, and only in columns that actually hold
    # a dropdown -- so the per-cell cell_choices() lookup is skipped on every other column.
    drop_cols = model.dropdown_cols() if model.editable else ()
    # body: data rows (grid >= 1), scrollable columns first then frozen (on top)
    for band in (False, True):
        for c in cols:
            if (c < g.frozen) != band:
                continue
            x, cw = g.col_x(c), g.col_width(c)
            lo_x = max(g.gutter_w, fx) if c >= g.frozen else g.gutter_w
            is_drop_col = c in drop_cols and x + cw > lo_x + 2   # once per column, not per cell
            for gr in data_rows:
                y = g.row_y(gr)
                st = model.find_state(gr, c)
                sty = cell_style(gr, c)
                fg, flags, base = T.TXT, 0, None
                if sty:
                    fg = sty.get("fg", T.TXT)
                    flags = T.FLAG_BOLD if sty.get("bold") else 0
                    base = sty.get("bg")                      # user fill, else zebra
                zeb = (gr - H) % 2 == 0                       # first data row = zebra
                if st == 2:
                    bg = T.FIND_ACTIVE
                elif washed(gr, c):                           # selection tints over the fill
                    bg = _blend(base, T.SEL_TINT, SEL_WASH_A) if base else (wash_odd if zeb else wash_even)
                elif st == 1:
                    bg = T.FIND_MATCH
                else:
                    bg = base if base else (T.ZEBRA if zeb else T.BG)
                dl.cells.append((x, y, cw, g.row_h, model.cell(gr, c), bg, fg, flags))
                # always-visible drop button on every dropdown cell (spreadsheet data-validation
                # style). Carries the CELL rect: the renderer draws the button at its right.
                if is_drop_col and cell_choices(gr, c) is not None:
                    dl.overlays.append(("dropdown", x, y, cw, g.row_h))

    # gutter for data rows -- numbered gr+1 (so data starts at "2")
    for gr in data_rows:
        y = g.row_y(gr)
        bg = T.SEL_HDR if row_sel(gr) else T.HEADER_BG
        dl.cells.append((0, y, g.gutter_w, g.row_h, str(gr + 1), bg, T.LETTER_FG, T.FLAG_CENTER))

    # header rows (grid rows 0..H-1): pinned, selectable. The BOTTOM row holds
    # the field names + filter buttons; rows above it are GROUP bands -- adjacent
    # cells sharing label and fill merge into one spanning, centered cell (a
    # selection wash or per-cell style difference splits the run on its own).
    for hr in range(H):
        y = lh + hr * g.field_h
        bottom = hr == H - 1
        for band in (False, True):
            run = None                     # pending [x, w, text, bg, fg] group span
            for c in cols:
                if (c < g.frozen) != band:
                    continue
                x, cw = g.col_x(c), g.col_width(c)
                # A header row does NOT act as a column indicator (only the A/B/C
                # letter band does); it tints only when the cell itself is selected.
                sty = cell_style(hr, c)
                fg = sty.get("fg", T.FIELD_FG) if sty else T.FIELD_FG
                base = sty.get("bg") if sty else None         # header is always bold
                if washed(hr, c):
                    bg = _blend(base, T.SEL_TINT, SEL_WASH_A) if base else wash_hdr
                else:
                    bg = base if base else T.FIELD_BG
                txt = model.cell(hr, c)
                if not bottom:
                    if run and txt and run[2] == txt and run[3] == bg and run[4] == fg \
                            and run[0] + run[1] == x:
                        run[1] += cw                          # extend the group span
                        continue
                    if run:
                        dl.cells.append((run[0], y, run[1], g.field_h, run[2], run[3],
                                         run[4], T.FLAG_BOLD | T.FLAG_CENTER))
                    run = [x, cw, txt, bg, fg]
                    continue
                dl.cells.append((x, y, cw, g.field_h, txt, bg, fg, T.FLAG_BOLD))
                if c >= ncols:                    # phantom column: no filter button
                    continue
                if model.has_filter(c):
                    state = "funnel"
                elif model.has_sort(c):
                    state = "asc" if model.sort_ascending(c) else "desc"
                else:
                    state = "idle"
                bx, by, sz = g.filter_btn_rect(c)
                dl.overlays.append(("filterbtn", bx, by, sz, state))
            if run:
                dl.cells.append((run[0], y, run[1], g.field_h, run[2], run[3],
                                 run[4], T.FLAG_BOLD | T.FLAG_CENTER))

        # header-row gutter -> "1", "2", …
        dl.cells.append((0, y, g.gutter_w, g.field_h, str(hr + 1),
                         T.SEL_HDR if row_sel(hr) else T.HEADER_BG, T.LETTER_FG,
                         T.FLAG_CENTER))

    # column-letter band (A, B, C…)
    for band in (False, True):
        for c in cols:
            if (c < g.frozen) != band:
                continue
            x, cw = g.col_x(c), g.col_width(c)
            bg = T.SEL_HDR if col_sel(c) else T.LETTER_BG
            dl.cells.append((x, 0, cw, lh, column_label(c), bg, T.LETTER_FG, T.FLAG_CENTER))

    # corner (letter-band gutter)
    dl.cells.append((0, 0, g.gutter_w, lh, "", T.HEADER_BG, T.LETTER_FG, 0))

    # chrome
    if g.frozen > 0:
        dl.overlays.append(("line", fx, 0, fx, h, T.DIVIDER, 1))
    # thick black section dividers (set_vline / set_hline), in SCREEN coords like
    # the rings below. Own overlay kinds, kept out of the pinned frozen-divider "line".
    vl, hl = model.vlines(), model.hlines()
    if vl:
        last = nrows - 1                                   # clamp to rendered grid (incl. blank pad), not viewport
        # uncapped rows: extend through the phantom rows to the viewport bottom (the
        # empty repeatable cells keep the divider running, spreadsheet-style)
        y_end = h if g.uncap_rows else min(h, g.row_y(last) + g.row_h_at(last))
        for c in vl:
            if not (0 <= c < ncols):
                continue
            x = g.col_x(c) + g.col_w[c]                     # right edge of column c
            lo_x = fx if c >= g.frozen else g.gutter_w
            if lo_x <= x <= w:                              # on-screen, not under frozen/gutter
                dl.overlays.append(("vline", x, 0, x, y_end, T.SECTION, T.SECTION_W))
    if hl:
        vis = set(data_rows)
        # uncapped cols: extend through the phantom columns to the viewport right edge
        x_end = w if g.uncap_cols else min(w, g.col_x(ncols - 1) + g.col_w[ncols - 1])
        for gr in hl:
            if gr < H or gr in vis:                         # header rows pinned; data must be visible
                y = g.row_y(gr) + g.row_h_at(gr)           # bottom edge of row gr
                dl.overlays.append(("hline", g.gutter_w, y, x_end, y, T.SECTION, T.SECTION_W))
    # corner triangle: selection orange ONLY when the whole sheet is selected
    # (Ctrl+A / a drag spanning header+data extent), hover orange on hover, else grey.
    lr, lc = model.data_extent()
    all_selected = any(r1 == 0 and c1 == 0 and r2 >= lr and c2 >= lc for (r1, c1, r2, c2) in norm)
    tri_color = T.SEL_RING if all_selected else T.ACCENT if hover_corner else T.GRID
    dl.overlays.append(("tri", g.gutter_w - 3, lh - 3, max(5, min(g.gutter_w, lh) // 2),
                        tri_color))
    # Selection outline = perimeter of the selected union: emit an edge only where
    # the neighbour across it isn't selected. Adjacent ranges merge into one outline
    # (no doubled line), and the active cell gets no separate internal box.
    ar, ac = active

    def sel_at(r, c):
        return (r == ar and c == ac) or _in_ranges(norm, r, c)

    for gr in list(range(H)) + data_rows:
        y0 = g.row_y(gr)
        y1, lo_y = (y0 + g.field_h, lh) if gr < H else (y0 + g.row_h, bh)
        for c in cols:
            if not sel_at(gr, c):
                continue
            x0 = g.col_x(c)
            x1 = x0 + g.col_width(c)
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
