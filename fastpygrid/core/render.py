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


def blit(dl, cv):
    # Grid lines are the 1px gaps between inset cell fills over a single grid-colour
    # backing rect -- one fill per cell, no per-cell stroke (~4x faster in Qt,
    # pixel-identical). Cells tile gaplessly, the backing shows only at each right/bottom edge.
    if dl.cells:
        x0 = min(c[0] for c in dl.cells)
        y0 = min(c[1] for c in dl.cells)
        x1 = max(c[0] + c[2] for c in dl.cells)
        y1 = max(c[1] + c[3] for c in dl.cells)
        cv.rect(x0, y0, x1 - x0, y1 - y0, fill=T.GRID)
    for (x, y, w, h, text, bg, fg, flags) in dl.cells:
        cv.rect(x, y, w - 1, h - 1, fill=bg)
        if text:
            cv.text(x, y, w, h, text, fg, bool(flags & T.FLAG_BOLD), bool(flags & T.FLAG_CENTER))
    for ov in dl.overlays:
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


if __name__ == "__main__":
    # grid-gap invariant: a backing rect covers the cell bbox, and each cell fill
    # is inset 1px on the right/bottom so the backing shows through as a grid line.
    class _Rec:
        def __init__(self): self.rects = []
        def rect(self, x, y, w, h, fill=None, outline=None, width=1):
            self.rects.append((x, y, w, h, fill))
        def text(self, *a, **k): pass

    class _DL:
        cells = [(0, 0, 10, 5, "a", "#111", "#fff", 0), (10, 0, 20, 5, "b", "#222", "#fff", 0)]
        overlays = []

    rec = _Rec(); blit(_DL(), rec)
    assert rec.rects[0] == (0, 0, 30, 5, T.GRID), rec.rects[0]      # backing = bbox
    assert rec.rects[1] == (0, 0, 9, 4, "#111"), rec.rects[1]       # cell inset by 1px
    assert rec.rects[2] == (10, 0, 19, 4, "#222"), rec.rects[2]
    print("ok")
