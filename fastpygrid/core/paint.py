"""paint() -> a display list. Pure data, draws nothing.

Given the model, geometry, active cell and selection ranges, it returns the
~visible viewport as toolkit-neutral draw ops any renderer can blit. Every
layout / z-order / colour decision lives here exactly once.

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

Grid rows: 0..H-1 are the pinned header rows (field names on the bottom one,
rows above it are GROUP bands whose adjacent same-label cells merge into one
spanning cell). Rows H..N are data. Cells are emitted back-to-front (data body,
gutter, header rows, letter band, corner) so a dumb front-to-back renderer gets
correct occlusion without a clip region.

The body data-cell loop (the ~viewport-sized hot path) is factored out so a
core-backed model can emit it natively in C++ (see engine._paint_fast /
CoreModel.gc_paint_body): _prelude/_chrome/_dropdowns are shared by the Python
reference path (paint) and the fast path, so both agree byte-for-byte.
"""
from types import SimpleNamespace

from . import theme as T
from .selection import normalize


class DisplayList:
    # Three z-layers, ordered by how many axes each scrolls -- fewer-scrolling layers
    # draw LATER (on top). The GL backend batches every cell fill ahead of every glyph
    # within a barrier segment, so strict painter's order only holds ACROSS barriers; a
    # barrier between each layer keeps a scrolled-under cell's text from bleeding over
    # the layer that pins it. The frozen-column / frozen-header (2D freeze) split makes
    # four quadrants; grouping them by scroll freedom gives three strata:
    #   cells  (L0) scrolls BOTH axes  : scrollable-column body
    #   frozen (L1) scrolls ONE axis   : scrollable header + letter (x-only) and
    #                                    frozen-column body + row gutter (y-only)
    #   chrome (L2) scrolls NEITHER    : frozen header + letter, header gutter, corner
    # drops / frozen_drops are dropdown buttons drawn WITH their body layer (L0 / L1)
    # rather than as top overlays, so the layer that pins them overpaints the part that
    # scrolls under -- the button fades behind the frozen band / header instead of
    # floating over it.
    __slots__ = ("cells", "frozen", "chrome", "drops", "frozen_drops", "overlays")

    def __init__(self):
        self.cells = []
        self.frozen = []
        self.chrome = []
        self.drops = []
        self.frozen_drops = []
        self.overlays = []


def edit_colors(gr, hdr_rows=1):
    """(bg, fg) an in-cell editor should use to blend with the cell it edits:
    the zebra body colour (or the dark header) and its matching text colour."""
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
    """Data-rows x visible-cols: fill + text per cell, back-to-front. Pure Python
    reference for the fast C++ path (gc_paint_body). Appends dropdown buttons too.
    Frozen-column cells route to `frozen` (layer L1); dropdown buttons route to
    `drops`/`frozen_drops` so their body layer's pins overpaint them (see DisplayList)."""
    g, H, norm = C.g, C.H, C.norm

    def washed(gr, c):
        return not C.single_cell and _in_ranges(norm, gr, c)

    cell_style, cell_choices = model.cell_style, model.cell_choices
    drop_cols = model.dropdown_cols() if model.editable else ()
    # prefetch every visible body cell's text in ONE model call (core-backed models
    # batch it into a single FFI instead of ~1900 per-cell calls). Keyed (data_idx, col).
    body_txt = model.block_text([gr - H for gr in C.data_rows], C.cols)
    for band in (False, True):
        dst = frozen if band else cells
        dst_drop = frozen_drops if band else drops
        for c in C.cols:
            if (c < g.frozen) != band:
                continue
            x, cw = g.col_x(c), g.col_width(c)
            lo_x = max(g.gutter_w, C.fx) if c >= g.frozen else g.gutter_w
            is_drop_col = c in drop_cols and x + cw > lo_x + 2   # once per column, not per cell
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
                elif washed(gr, c):                           # selection tints over the fill
                    bg = _blend(base, T.SEL_TINT, SEL_WASH_A) if base else (C.wash_odd if zeb else C.wash_even)
                elif st == 1:
                    bg = T.FIND_MATCH
                else:
                    bg = base if base else (T.ZEBRA if zeb else T.BG)
                dst.append((x, y, cw, g.row_h, body_txt[(gr - H, c)], bg, fg, flags))
                if is_drop_col and cell_choices(gr, c) is not None:
                    dst_drop.append((x, y, cw, g.row_h))       # faded by its layer's pins


def _dropdowns(model, C, drops, frozen_drops):
    """Dropdown buttons only (the fast path does the body fills/text in C++, but the
    per-cell choice check stays in Python). Cheap: only over dropdown columns. Buttons
    route to drops/frozen_drops by column so their body layer's pins overpaint them."""
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
    corner, and all chrome overlays (dividers, filter buttons, corner triangle,
    selection rings). Shared by paint() and the fast path. Routing by scroll freedom
    (see DisplayList): one-axis scrollers -> `frozen` (L1) = the data-row gutter and the
    SCROLLABLE header/letter; zero-axis pins -> `chrome` (L2) = the FROZEN header/letter,
    header gutter and corner."""
    g, w, h, fx, lh, bh, H = C.g, C.w, C.h, C.fx, C.lh, C.bh, C.H
    nrows, ncols, data_rows, cols, norm = C.nrows, C.ncols, C.data_rows, C.cols, C.norm

    def col_sel(c):
        return any(c1 <= c <= c2 for (_r1, c1, _r2, c2) in norm)

    def row_sel(r):
        return any(r1 <= r <= r2 for (r1, _c1, r2, _c2) in norm)

    def washed(gr, c):
        return not C.single_cell and _in_ranges(norm, gr, c)

    cell_style = model.cell_style

    # gutter for data rows -- numbered gr+1 (so data starts at "2"). Scrolls with the
    # body, so it goes in layer B (`frozen`) to sit above the scrolled body but under
    # the pinned corner/letter band it slides beneath.
    for gr in data_rows:
        y = g.row_y(gr)
        bg = T.SEL_HDR if row_sel(gr) else T.HEADER_BG
        frozen.append((0, y, g.gutter_w, g.row_h, str(gr + 1), bg, T.LETTER_FG, T.FLAG_CENTER))

    # header rows (grid rows 0..H-1): pinned, selectable. The BOTTOM row holds
    # the field names + filter buttons. Rows above it are GROUP bands, adjacent
    # cells sharing label and fill merge into one spanning, centered cell (a
    # selection wash or per-cell style difference splits the run on its own).
    for hr in range(H):
        y = lh + hr * g.field_h
        bottom = hr == H - 1
        for band in (False, True):
            dst = chrome if band else frozen   # frozen header pins (L2); scrollable
            run = None                         # header scrolls in x only (L1)
            for c in cols:
                if (c < g.frozen) != band:
                    continue
                x, cw = g.col_x(c), g.col_width(c)
                # A header row does NOT act as a column indicator (only the A/B/C
                # letter band does), it tints only when the cell itself is selected.
                sty = cell_style(hr, c)
                fg = sty.get("fg", T.FIELD_FG) if sty else T.FIELD_FG
                base = sty.get("bg") if sty else None         # header is always bold
                if washed(hr, c):
                    bg = _blend(base, T.SEL_TINT, SEL_WASH_A) if base else C.wash_hdr
                else:
                    bg = base if base else T.FIELD_BG
                txt = model.cell(hr, c)
                if not bottom:
                    if run and txt and run[2] == txt and run[3] == bg and run[4] == fg \
                            and run[0] + run[1] == x:
                        run[1] += cw                          # extend the group span
                        continue
                    if run:
                        dst.append((run[0], y, run[1], g.field_h, run[2], run[3],
                                    run[4], T.FLAG_BOLD | T.FLAG_CENTER))
                    run = [x, cw, txt, bg, fg]
                    continue
                dst.append((x, y, cw, g.field_h, txt, bg, fg, T.FLAG_BOLD))
                if c >= ncols or not g.filters:   # phantom column / filters off: no filter button
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

        # header-row gutter -> "1", "2", …
        chrome.append((0, y, g.gutter_w, g.field_h, str(hr + 1),
                       T.SEL_HDR if row_sel(hr) else T.HEADER_BG, T.LETTER_FG,
                       T.FLAG_CENTER))

    # column-letter band (A, B, C…): frozen letters pin (L2), scrollable scroll in x (L1).
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

    # chrome
    if g.frozen > 0:
        overlays.append(("line", fx, 0, fx, h, T.DIVIDER, 1))
    # thick black section dividers (set_vline / set_hline), in SCREEN coords like
    # the rings below. Own overlay kinds, kept out of the pinned frozen-divider "line".
    vl, hl = model.vlines(), model.hlines()
    if vl:
        last = nrows - 1                                   # clamp to rendered grid (incl. blank pad), not viewport
        # uncapped rows: extend through the phantom rows to the viewport bottom (the
        # empty repeatable cells keep the divider running, spreadsheet-style)
        y_end = h if g.uncap_rows else min(h, g.row_y(last) + g.row_h_at(last))
        for c, cw in vl.items():
            if not (0 <= c < ncols):
                continue
            x = g.col_x(c) + g.col_w[c]                     # right edge of column c
            lo_x = fx if c >= g.frozen else g.gutter_w
            if lo_x <= x <= w:                              # on-screen, not under frozen/gutter
                overlays.append(("vline", x, 0, x, y_end, T.SECTION, cw or T.SECTION_W))
    if hl:
        # uncapped cols: extend through the phantom columns to the viewport right edge
        x_end = w if g.uncap_cols else min(w, g.col_x(ncols - 1) + g.col_w[ncols - 1])
        # hlines follow their source row, so walk the rows actually on screen (pinned
        # headers + visible data) and ask each for its divider -- no src->grid reverse map.
        for gr in list(range(H)) + data_rows:
            rw = model.hline_width(gr)
            if rw is model._NO_HLINE:
                continue
            y = g.row_y(gr) + g.row_h_at(gr)               # bottom edge of row gr
            overlays.append(("hline", g.gutter_w, y, x_end, y, T.SECTION, rw or T.SECTION_W))
    # corner triangle: selection orange ONLY when the whole sheet is selected
    # (Ctrl+A / a drag spanning header+data extent), hover orange on hover, else grey.
    lr, lc = model.data_extent()
    all_selected = any(r1 == 0 and c1 == 0 and r2 >= lr and c2 >= lc for (r1, c1, r2, c2) in norm)
    tri_color = T.SEL_RING if all_selected else T.ACCENT if hover_corner else T.GRID
    overlays.append(("tri", g.gutter_w - 3, lh - 3, max(5, min(g.gutter_w, lh) // 2),
                     tri_color))
    # Selection outline = perimeter of the selected union: emit an edge only where
    # the neighbour across it isn't selected. Adjacent ranges merge into one outline
    # (no doubled line), and the active cell gets no separate internal box.
    ar, ac = active

    def sel_at(r, c):
        return (r == ar and c == ac) or _in_ranges(norm, r, c)

    # Each column's clipped x-span is loop-invariant in the row, so compute it ONCE
    # here instead of per (row, col) -- the inner loop below runs over every visible
    # selected cell (a viewport-filling selection = ~thousands/frame). Hidden columns
    # (fully under the gutter/frozen band or off the right edge) drop out of the map.
    col_span = {}
    for c in cols:
        x0 = g.col_x(c)
        cx0 = max(x0, max(g.gutter_w, fx) if c >= g.frozen else g.gutter_w)
        cx1 = min(x0 + g.col_width(c), w)
        if cx0 < cx1:
            col_span[c] = (cx0, cx1)

    # Only SELECTED cells emit outline edges, so walk each row's selected column
    # intervals instead of testing every visible cell. A single-cell selection (the
    # common scroll case) skips every unselected row for one range-check -- the old
    # per-cell scan cost ~one sel_at per VISIBLE cell every frame just to find nothing.
    # A viewport-filling selection still costs O(visible cells). Neighbour tests stay
    # on sel_at (it sees all ranges + the active cell), so merged/adjacent outlines and
    # clipping are unchanged -- verified identical to the per-cell scan.
    for gr in list(range(H)) + data_rows:
        y0 = g.row_y(gr)
        y1, lo_y = (y0 + g.field_h, lh) if gr < H else (y0 + g.row_h, bh)
        cy0, cy1 = max(y0, lo_y), min(y1, h)
        if cy0 >= cy1:
            continue                                  # row hidden under the header / off-screen
        ivals = [(c1, c2) for (r1, c1, r2, c2) in norm if r1 <= gr <= r2]
        if gr == ar:
            ivals.append((ac, ac))                    # the active cell (may be outside norm)
        if not ivals:
            continue                                  # nothing selected in this row -> skip cheaply
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
