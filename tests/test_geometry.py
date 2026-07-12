"""Overscroll clamp / extent self-check (no toolkit)."""
from fastpygrid.core.geometry import Geometry


def test_overscroll_and_extents():
    g = Geometry([100, 100], hdr_rows=1)
    g.w, g.h = 400, 220
    N = 20
    # capped (default): scroll clamps to the data extent + a small pad past the last row
    g.top_row = 999; g.clamp(N)
    flush = (g.max_top(N) - g.hdr_rows) * g.row_h
    assert flush < g.scroll_y <= flush + g.OVERSCROLL_PAD and g.row_extent(N) == N, g.scroll_y
    # uncapped: overscroll sticks, extent grows to match, snaps back on scroll-up
    g.uncap_rows = True
    g.top_row = 999; g.clamp(N)
    assert g.top_row == 999 and g.row_extent(N) == 999 + g.full_rows(), g.top_row
    assert g.scroll_y < g.max_scroll_y(N), (g.scroll_y, g.max_scroll_y(N))  # room to scroll further down
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


def test_phantom_rows_cols():
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


def test_filter_btn_hit_disabled():
    # filters=False: the ▼ button is never hit-testable, even over a real column's button
    gf = Geometry([80, 80], filters=False); gf.w, gf.h = 300, 200
    bx, by, sz = gf.filter_btn_rect(0)
    assert gf.filter_btn_hit(bx + 1, by + 1, 0) is False


def test_used_range_trims_extent():
    # used_rows/used_cols trim the thumb's base to real content, even after the model
    # grew: scrolled back to the top, the extent reflects the used range, not the grown size
    gt = Geometry([80, 80], hdr_rows=1, uncap_rows=True, uncap_cols=True)
    gt.w, gt.h = 300, 200
    gt.top_row = gt.hdr_rows                          # scrolled back to the top
    gt.used_rows, gt.used_cols = 8, 2                 # 2 real cols, few used rows (rest blank overscroll)
    assert gt.row_extent(9999) == max(8, gt.top_row + gt.full_rows())   # not 9999
    assert gt.col_extent() < gt.col_left(9999)        # not the grown width


def test_bisect_matches_linear_scan():
    # bisect hit-testing matches a brute-force linear scan at every pixel
    g3 = Geometry([40, 90, 25, 70, 60], frozen=1); g3.w, g3.h = 300, 200; g3.scroll_x = 55
    for x in range(0, 320):
        want = next((c for c in range(5) if g3._cum[c] <= (
            (x - g3.gutter_w) if x < g3.freeze_x() else (x - g3.gutter_w + g3.scroll_x)
        ) < g3._cum[c + 1]), None) if x >= g3.gutter_w else None
        assert g3.x_to_col(x, 5) == want, (x, g3.x_to_col(x, 5), want)


def test_subrow_scroll():
    # sub-row (pixel) vertical scroll: top_row derives from scroll_y, row_y shifts by
    # the sub-row remainder, and hit/drag map screen-y back to the right row across the seam.
    gp = Geometry([100], hdr_rows=1); gp.w, gp.h = 300, 400
    rh = gp.row_h
    gp.scroll_y = 2 * rh + 5                       # 2 rows + 5px down
    assert gp.top_row == gp.hdr_rows + 2, gp.top_row
    assert gp.row_y(gp.top_row) == gp.header_h - 5, gp.row_y(gp.top_row)   # partially above the header
    assert gp.hit(gp.gutter_w + 1, gp.header_h + 1, 1000, 1)[1] == gp.top_row
    assert gp.hit(gp.gutter_w + 1, gp.header_h + rh, 1000, 1)[1] == gp.top_row + 1   # across the row seam
