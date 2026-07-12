"""blit(display_list, canvas) -- the toolkit-neutral draw walk.

paint.py decides WHAT the frame looks like (a display list of pure data). This
walks that list and issues primitive draw calls against a ``canvas`` (GpuCanvas).
Layout, z-order and the funnel/arrow decomposition live here. The canvas is just
"here's how I draw a rect / text / line / polygon / glyph":

    canvas.rect(x, y, w, h, fill=None, outline=None, width=1)
    canvas.text(x, y, w, h, s, color, bold=False, center=False)   # clipped to w
    canvas.line(x1, y1, x2, y2, color, width)
    canvas.poly(points, color)                    # filled, points = [(x, y), ...]
    canvas.glyph(cx, cy, s, color, px)            # one char centred at pixel size
"""
from . import theme as T


def _bbox(cells, box):
    """Union `box` [x0,y0,x1,y1] (or None) with every cell rect, in one pass."""
    for c in cells:
        cx, cy = c[0], c[1]
        r, b = cx + c[2], cy + c[3]
        if box is None:
            box = [cx, cy, r, b]
        else:
            if cx < box[0]: box[0] = cx
            if cy < box[1]: box[1] = cy
            if r > box[2]: box[2] = r
            if b > box[3]: box[3] = b
    return box


def _blit_cells(cells, cv):
    # hoist the per-cell attribute/global lookups out of the ~2000-iteration loop
    _rect, _text, _BOLD, _CENTER = cv.rect_fill, cv.text, T.FLAG_BOLD, T.FLAG_CENTER
    for (x, y, w, h, text, bg, fg, flags) in cells:
        _rect(x, y, w - 1, h - 1, bg)                  # fill-only: cells never carry an outline
        if text:
            _text(x, y, w, h, text, fg, bool(flags & _BOLD), bool(flags & _CENTER))


def _blit_drops(drops, cv):
    for (x, y, w, h) in drops:                         # native drop button at the cell's right
        cv.combo(x, y, w, h)


def _blit_overlays(overlays, cv):
    for ov in overlays:
        k = ov[0]
        if k == "line" or k == "vline" or k == "hline":
            cv.line(ov[1], ov[2], ov[3], ov[4], ov[5], ov[6])
        elif k == "ring":                              # selection-outline edge
            cv.line(ov[1], ov[2], ov[3], ov[4], T.SEL_RING, 2)
        elif k == "tri":
            x1, y1, sz, col = ov[1], ov[2], ov[3], ov[4]
            cv.poly([(x1 - sz, y1), (x1, y1 - sz), (x1, y1)], col)
        elif k == "filterbtn":
            _filter_btn(cv, *ov[1:])
        elif k == "dropdown":                          # native drop button at the cell's right
            cv.combo(ov[1], ov[2], ov[3], ov[4])


def blit(dl, cv):
    # Grid lines are the 1px gaps between inset cell fills over a single grid-colour
    # backing rect -- one fill per cell, no per-cell stroke (~4x faster in Qt,
    # pixel-identical). Cells tile gaplessly, the backing shows only at each right/bottom edge.
    box = _bbox(dl.chrome, _bbox(dl.frozen, _bbox(dl.cells, None)))
    if box:
        x0, y0, x1, y1 = box
        cv.rect(x0, y0, x1 - x0, y1 - y0, fill=T.GRID)
    _blit_cells(dl.cells, cv)                       # L0: scrollable body
    _blit_drops(dl.drops, cv)                       # its dropdowns, faded by L1/L2 next
    cv.barrier()
    _blit_cells(dl.frozen, cv)                      # L1: one-axis scrollers, over L0
    _blit_drops(dl.frozen_drops, cv)               # frozen dropdowns, faded by L2 next
    cv.barrier()
    _blit_cells(dl.chrome, cv)                      # L2: pins, over L0 and L1
    _blit_overlays(dl.overlays, cv)


def blit_fast(chrome_cells, mid_cells, body_wire, frozen_wire, body_box,
              drops, frozen_drops, overlays, cv):
    """Fast-path assembly: the body cells are already encoded as wire bytes
    (gc_paint_body); splice them in, reproducing blit()'s exact 3-layer order --
    backing rect (union of every extent); L0 scrollable body bytes + its dropdowns;
    barrier; L1 frozen body bytes + `mid_cells` (gutter + scrollable header/letter) +
    frozen dropdowns; barrier; L2 `chrome_cells` (pins); overlays. `body_box` is the
    union of both bodies' [x0,y0,x1,y1]."""
    box = _bbox(chrome_cells, _bbox(mid_cells, list(body_box) if body_box else None))
    if box:
        x0, y0, x1, y1 = box
        cv.rect(x0, y0, x1 - x0, y1 - y0, fill=T.GRID)
    cv.buf += body_wire                             # L0: scrollable body
    _blit_drops(drops, cv)
    cv.barrier()
    cv.buf += frozen_wire                           # L1: frozen body + one-axis chrome
    _blit_cells(mid_cells, cv)
    _blit_drops(frozen_drops, cv)
    cv.barrier()
    _blit_cells(chrome_cells, cv)                   # L2: pins
    _blit_overlays(overlays, cv)


def _filter_btn(cv, bx, by, sz, state):
    cv.rect(bx, by, sz, sz, fill=T.BTN_BG, outline=T.BTN_BORDER)
    if state == "funnel":                          # active filter -> amber funnel
        mx = bx + sz / 2
        t, n, b = by + sz * 0.24, by + sz * 0.49, by + sz * 0.72
        fw, sw = sz * 0.22, sz * 0.05
        cv.poly([(mx - fw, t), (mx + fw, t), (mx + sw, n),
                 (mx + sw, b), (mx - sw, b), (mx - sw, n)], T.FUNNEL)
    else:                                          # sort arrow (▲ asc / ▼ desc / idle)
        glyph = "▲" if state == "asc" else "▼"
        col = T.ARROW_IDLE if state == "idle" else T.ARROW_SORT
        cv.glyph(bx + sz / 2, by + sz / 2, glyph, col, max(6, int(sz * 0.66)))
