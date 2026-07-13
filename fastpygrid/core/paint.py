"""paint() -> a display list. Pure data, draws nothing. Every layout/z-order/
colour decision lives here once.

    dl.cells    : [(x, y, w, h, text, bg, fg, flags), ...]   scrollable body (L0)
    dl.frozen   : scrollable header/letter + frozen body + gutter (L1, over L0)
    dl.chrome   : frozen header/letter, header gutter, corner (L2, over L0/L1)
    dl.drops / dl.frozen_drops : dropdown buttons drawn with their body layer (L0/L1)
    dl.overlays : chrome drawn AFTER all cells:
        ("line",      x1, y1, x2, y2, color, width)      # frozen-pane divider
        ("vline"|"hline", x1, y1, x2, y2, color, width)  # thick section dividers
        ("ring",      x1, y1, x2, y2)                    # selection-outline edge (SEL_RING, 2px)
        ("filterbtn", bx, by, sz, state)                 # state: funnel|asc|desc|idle
        ("tri",       x1, y1, sz, color)                 # corner select-all triangle
        ("dropdown",  x, y, w, h)                         # native drop button at cell's right

Grid rows 0..H-1 = pinned header rows (field names on the bottom; rows above are
GROUP bands whose adjacent same-label cells merge into one spanning cell). Rows
H..N = data. Cells emit back-to-front so a front-to-back renderer occludes
correctly without a clip region.

The body cell loop (viewport-sized hot path) is factored out so a core-backed
model can emit it in C++ (engine._paint_fast / CoreModel.gc_paint_body);
_prelude/_chrome/_dropdowns are shared by both paths, so they agree byte-for-byte.
"""
from types import SimpleNamespace

from . import theme as T
from .selection import normalize


class DisplayList:
    # Three z-layers by scroll freedom; fewer-scrolling layers draw LATER (on top).
    # GL backend batches all fills before all glyphs within a barrier, so painter's
    # order only holds ACROSS barriers; a barrier per layer stops scrolled-under
    # text bleeding over the layer that pins it.
    #   cells  (L0) scrolls BOTH axes  : scrollable-column body
    #   frozen (L1) scrolls ONE axis   : scrollable header/letter (x) + frozen body/gutter (y)
    #   chrome (L2) scrolls NEITHER    : frozen header/letter, header gutter, corner
    # drops/frozen_drops: dropdown buttons drawn WITH their body layer so its pins
    # overpaint them (button fades behind the frozen band/header, not over it).
    __slots__ = ("cells", "frozen", "chrome", "drops", "frozen_drops", "overlays")

    def __init__(self):
        self.cells = []
        self.frozen = []
        self.chrome = []
        self.drops = []
        self.frozen_drops = []
        self.overlays = []


def edit_colors(gr, hdr_rows=1):
    """(bg, fg) for an in-cell editor to blend with the cell it edits: zebra body
    colour (or dark header) and its matching text colour."""
    if gr < hdr_rows:
        return T.FIELD_BG, T.FIELD_FG
    return (T.ZEBRA if (gr - hdr_rows) % 2 == 0 else T.BG), T.TXT


def column_label(col):
    """Column letters: 0 -> A, 25 -> Z, 26 -> AA, …"""
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


def _prelude(model, geom, ranges):
    """Shared per-frame context for the body + chrome passes. Cheap: no per-cell work."""
    g = geom
    norm = normalize(ranges)
    single_cell = (len(norm) == 1 and norm[0][0] == norm[0][2] and norm[0][1] == norm[0][3])
    return SimpleNamespace(
        g=g, w=g.w, h=g.h, fx=g.freeze_x(), lh=g.letter_h, bh=g.header_h, H=g.hdr_rows,
        nrows=model.nrows(), ncols=model.ncols,
        data_rows=g.visible_data_rows(model.nrows()), cols=g.visible_cols(model.ncols),
        norm=norm, single_cell=single_cell,
        wash_even=_blend(T.BG, T.SEL_TINT, SEL_WASH_A),      # tint over white / zebra rows
        wash_odd=_blend(T.ZEBRA, T.SEL_TINT, SEL_WASH_A),
        wash_hdr=_blend(T.FIELD_BG, T.SEL_TINT, SEL_WASH_A))


def _body_cells(model, C, cells, frozen, drops, frozen_drops, overlays):
    """Data-rows x visible-cols fill + text, back-to-front. Python reference for
    the C++ fast path (gc_paint_body). Frozen-column cells route to `frozen` (L1);
    dropdown buttons to `drops`/`frozen_drops` so their layer's pins overpaint them."""
    g, H, norm = C.g, C.H, C.norm

    def washed(gr, c):
        return not C.single_cell and _in_ranges(norm, gr, c)

    cell_style, cell_choices = model.cell_style, model.cell_choices
    drop_cols = model.dropdown_cols() if model.editable else ()
    # prefetch all visible text in ONE call (core models batch it into a single FFI).
    body_txt = model.block_text([gr - H for gr in C.data_rows], C.cols)
    for band in (False, True):
        dst = frozen if band else cells
        dst_drop = frozen_drops if band else drops
        for c in C.cols:
            if (c < g.frozen) != band:
                continue
            x, cw = g.col_x(c), g.col_width(c)
            lo_x = max(g.gutter_w, C.fx) if c >= g.frozen else g.gutter_w
            is_drop_col = c in drop_cols and x + cw > lo_x + 2   # once per column
            for gr in C.data_rows:
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
                elif washed(gr, c):                           # selection tint over fill
                    bg = _blend(base, T.SEL_TINT, SEL_WASH_A) if base else (C.wash_odd if zeb else C.wash_even)
                elif st == 1:
                    bg = T.FIND_MATCH
                else:
                    bg = base if base else (T.ZEBRA if zeb else T.BG)
                dst.append((x, y, cw, g.row_h, body_txt[(gr - H, c)], bg, fg, flags))
                if is_drop_col and cell_choices(gr, c) is not None:
                    dst_drop.append((x, y, cw, g.row_h))


def _dropdowns(model, C, drops, frozen_drops):
    """Dropdown buttons only (fast path does fills/text in C++; per-cell choice check
    stays in Python). Routes to drops/frozen_drops by column."""
    if not model.editable:
        return
    g = C.g
    drop_cols = model.dropdown_cols()
    if not drop_cols:
        return
    cell_choices = model.cell_choices
    for c in C.cols:
        if c not in drop_cols:
            continue
        x, cw = g.col_x(c), g.col_width(c)
        lo_x = max(g.gutter_w, C.fx) if c >= g.frozen else g.gutter_w
        if x + cw <= lo_x + 2:
            continue
        dst_drop = frozen_drops if c < g.frozen else drops
        for gr in C.data_rows:
            if cell_choices(gr, c) is not None:
                dst_drop.append((x, g.row_y(gr), cw, g.row_h))


def _chrome(model, C, active, hover_corner, frozen, chrome, overlays):
    """Everything that is NOT a data-body cell: gutter, header rows, letter band,
    corner, and overlays (dividers, filter buttons, corner triangle, rings). Routed by
    scroll freedom: one-axis -> `frozen` (L1) = data gutter + scrollable header/letter;
    zero-axis -> `chrome` (L2) = frozen header/letter, header gutter, corner."""
    g, w, h, fx, lh, bh, H = C.g, C.w, C.h, C.fx, C.lh, C.bh, C.H
    nrows, ncols, data_rows, cols, norm = C.nrows, C.ncols, C.data_rows, C.cols, C.norm

    def col_sel(c):
        return any(c1 <= c <= c2 for (_r1, c1, _r2, c2) in norm)

    def row_sel(r):
        return any(r1 <= r <= r2 for (r1, _c1, r2, _c2) in norm)

    def washed(gr, c):
        return not C.single_cell and _in_ranges(norm, gr, c)

    cell_style = model.cell_style

    # data-row gutter: numbered by SOURCE row (+H+1) so a row keeps its label
    # through sort/filter, Excel-style. Scrolls with body -> `frozen` (L1).
    for gr in data_rows:
        y = g.row_y(gr)
        bg = T.SEL_HDR if row_sel(gr) else T.HEADER_BG
        num = model._src_data(gr - H) + H + 1
        frozen.append((0, y, g.gutter_w, g.row_h, str(num), bg, T.LETTER_FG, T.FLAG_CENTER))

    # header rows (0..H-1): pinned, selectable. BOTTOM row = field names + filter
    # buttons. Rows above = GROUP bands: adjacent cells sharing label+fill merge into
    # one spanning centered cell (a wash or style difference splits the run).
    for hr in range(H):
        y = lh + hr * g.field_h
        bottom = hr == H - 1
        for band in (False, True):
            dst = chrome if band else frozen   # frozen header pins (L2); scrollable (L1)
            run = None
            for c in cols:
                if (c < g.frozen) != band:
                    continue
                x, cw = g.col_x(c), g.col_width(c)
                # header row tints only when the cell itself is selected (not a
                # column indicator, only the A/B/C letter band is).
                sty = cell_style(hr, c)
                fg = sty.get("fg", T.FIELD_FG) if sty else T.FIELD_FG
                base = sty.get("bg") if sty else None
                if washed(hr, c):
                    bg = _blend(base, T.SEL_TINT, SEL_WASH_A) if base else C.wash_hdr
                else:
                    bg = base if base else T.FIELD_BG
                txt = model.cell(hr, c)
                if not bottom:
                    if run and txt and run[2] == txt and run[3] == bg and run[4] == fg \
                            and run[0] + run[1] == x:
                        run[1] += cw                          # extend group span
                        continue
                    if run:
                        dst.append((run[0], y, run[1], g.field_h, run[2], run[3],
                                    run[4], T.FLAG_BOLD | T.FLAG_CENTER))
                    run = [x, cw, txt, bg, fg]
                    continue
                dst.append((x, y, cw, g.field_h, txt, bg, fg, T.FLAG_BOLD))
                if c >= ncols or not g.filters:   # phantom column / filters off
                    continue
                if model.has_filter(c):
                    state = "funnel"
                elif model.has_sort(c):
                    state = "asc" if model.sort_ascending(c) else "desc"
                else:
                    state = "idle"
                bx, by, sz = g.filter_btn_rect(c)
                overlays.append(("filterbtn", bx, by, sz, state))
            if run:
                dst.append((run[0], y, run[1], g.field_h, run[2], run[3],
                            run[4], T.FLAG_BOLD | T.FLAG_CENTER))

        # header-row gutter
        chrome.append((0, y, g.gutter_w, g.field_h, str(hr + 1),
                       T.SEL_HDR if row_sel(hr) else T.HEADER_BG, T.LETTER_FG,
                       T.FLAG_CENTER))

    # column-letter band (A, B, C…): frozen letters pin (L2), scrollable in x (L1).
    for band in (False, True):
        dst = chrome if band else frozen
        for c in cols:
            if (c < g.frozen) != band:
                continue
            x, cw = g.col_x(c), g.col_width(c)
            bg = T.SEL_HDR if col_sel(c) else T.LETTER_BG
            dst.append((x, 0, cw, lh, column_label(c), bg, T.LETTER_FG, T.FLAG_CENTER))

    # corner (letter-band gutter)
    chrome.append((0, 0, g.gutter_w, lh, "", T.HEADER_BG, T.LETTER_FG, 0))

    if g.frozen > 0:
        overlays.append(("line", fx, 0, fx, h, T.DIVIDER, 1))
    # thick section dividers (set_vline/set_hline), SCREEN coords. Own overlay kinds,
    # kept out of the pinned frozen-divider "line".
    vl, hl = model.vlines(), model.hlines()
    if vl:
        last = nrows - 1                                   # rendered grid (incl. pad), not viewport
        # uncapped rows: extend through phantom rows to viewport bottom
        y_end = h if g.uncap_rows else min(h, g.row_y(last) + g.row_h_at(last))
        for c, cw in vl.items():
            if not (0 <= c < ncols):
                continue
            x = g.col_x(c) + g.col_w[c]                     # right edge of column c
            lo_x = fx if c >= g.frozen else g.gutter_w
            if lo_x <= x <= w:                              # on-screen, not under frozen/gutter
                overlays.append(("vline", x, 0, x, y_end, T.SECTION, cw or T.SECTION_W))
    if hl:
        x_end = w if g.uncap_cols else min(w, g.col_x(ncols - 1) + g.col_w[ncols - 1])
        # hlines follow source row -> walk on-screen rows and ask each; no reverse map.
        for gr in list(range(H)) + data_rows:
            rw = model.hline_width(gr)
            if rw is model._NO_HLINE:
                continue
            y = g.row_y(gr) + g.row_h_at(gr)               # bottom edge of row gr
            overlays.append(("hline", g.gutter_w, y, x_end, y, T.SECTION, rw or T.SECTION_W))
    # corner triangle: selection orange only when whole sheet selected (Ctrl+A / drag
    # spanning header+data), hover orange on hover, else grey.
    lr, lc = model.data_extent()
    all_selected = any(r1 == 0 and c1 == 0 and r2 >= lr and c2 >= lc for (r1, c1, r2, c2) in norm)
    tri_color = T.SEL_RING if all_selected else T.ACCENT if hover_corner else T.GRID
    overlays.append(("tri", g.gutter_w - 3, lh - 3, max(5, min(g.gutter_w, lh) // 2),
                     tri_color))
    # Selection outline = perimeter of the union: emit an edge only where the neighbour
    # across it isn't selected. Adjacent ranges merge (no doubled line); active cell
    # gets no separate internal box.
    ar, ac = active

    def sel_at(r, c):
        return (r == ar and c == ac) or _in_ranges(norm, r, c)

    # Each column's clipped x-span is row-invariant; compute once, not per (row, col)
    # (inner loop runs over every visible selected cell, ~thousands/frame). Hidden
    # columns (under gutter/frozen band or off the right edge) drop out.
    col_span = {}
    for c in cols:
        x0 = g.col_x(c)
        cx0 = max(x0, max(g.gutter_w, fx) if c >= g.frozen else g.gutter_w)
        cx1 = min(x0 + g.col_width(c), w)
        if cx0 < cx1:
            col_span[c] = (cx0, cx1)

    # Only SELECTED cells emit edges -> walk each row's selected column intervals, not
    # every visible cell. Single-cell selection (common scroll case) skips unselected
    # rows in one range-check. Neighbour tests stay on sel_at (sees all ranges + active
    # cell) so merged/adjacent outlines are identical to the old per-cell scan.
    for gr in list(range(H)) + data_rows:
        y0 = g.row_y(gr)
        y1, lo_y = (y0 + g.field_h, lh) if gr < H else (y0 + g.row_h, bh)
        cy0, cy1 = max(y0, lo_y), min(y1, h)
        if cy0 >= cy1:
            continue                                  # row hidden under header / off-screen
        ivals = [(c1, c2) for (r1, c1, r2, c2) in norm if r1 <= gr <= r2]
        if gr == ar:
            ivals.append((ac, ac))                    # active cell (may be outside norm)
        if not ivals:
            continue                                  # nothing selected in this row
        for c in cols:
            span = col_span.get(c)
            if span is None or not any(lo <= c <= hi for lo, hi in ivals):
                continue
            cx0, cx1 = span
            if not sel_at(gr - 1, c):
                overlays.append(("ring", cx0, cy0, cx1, cy0))   # top
            if not sel_at(gr + 1, c):
                overlays.append(("ring", cx0, cy1, cx1, cy1))   # bottom
            if not sel_at(gr, c - 1):
                overlays.append(("ring", cx0, cy0, cx0, cy1))   # left
            if not sel_at(gr, c + 1):
                overlays.append(("ring", cx1, cy0, cx1, cy1))   # right


def paint(model, geom, active, ranges, hover_corner=False):
    geom.clamp(model.nrows())
    C = _prelude(model, geom, ranges)
    dl = DisplayList()
    _body_cells(model, C, dl.cells, dl.frozen, dl.drops, dl.frozen_drops, dl.overlays)
    _chrome(model, C, active, hover_corner, dl.frozen, dl.chrome, dl.overlays)
    return dl
