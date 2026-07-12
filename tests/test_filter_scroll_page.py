"""The filter-list scrollbar track-click uses the same Excel-style hold-and-glide
as the grid scrollbar: holding on the track eases f["top"] toward the pointer and
stops once the thumb reaches it (instead of the old one-page-per-click jump)."""
from fastpygrid.core.gpu import GpuEngine
from fastpygrid.core.coremodel import make_model


class _Host:
    def size(self):                return 800, 600
    def measure(self, t, b=False): return len(t) * 7
    def set_zoom_px(self, px):     pass
    def after_idle(self, fn):      pass
    def after(self, ms, fn):       return None            # ticks are pumped manually
    def hwnd(self):                return 0


def _eng():
    m = make_model(["A"], [["x"]], editable=True)
    e = GpuEngine(_Host(), m, col_w=[80])
    e.redraw = lambda: None
    return e


def _filter_layout(nvis=5, rh=20, ly=100, n=100):
    listh = nvis * rh
    th = max(rh // 2, int(listh * nvis / n))
    return {"list": (10, ly, 190, rh, nvis, 10),
            "sbthumb": (200, ly, 10, th),
            "items": list(range(n))}


def _pump(e):
    while e._sbpage:                                       # run the glide to a standstill
        e._sbpage_tick()


def test_glide_converges_to_far_click():
    e = _eng()
    e._filter = {"top": 0, "layout": _filter_layout()}
    e._start_fpage(200)                                    # click near the track bottom
    _pump(e)
    assert e._filter["top"] == 95                          # landed at max top (n - nvis)
    assert e._sbpage is None                               # and stopped, no overshoot loop


def test_glide_reaims_on_drag():
    e = _eng()
    e._filter = {"top": 50, "layout": _filter_layout()}
    e._start_fpage(200)
    e._sbpage_tick()                                       # one glide step downward
    assert e._filter["top"] > 50
    e._sbpage = ("fpage", None, 100)                       # drag re-aims toward the top
    _pump(e)
    assert e._filter["top"] == 0
