"""Zoom repaints coalesce like drags: while the ease animation is live (_zoom_to
set) redraw defers to one paint per idle instead of a synchronous GPU upload per
6ms tick, so a heavy sheet can't backlog the timer. Once idle, paint is immediate."""
from fastpygrid.core.gpu import GpuEngine
from fastpygrid.core.coremodel import make_model


class _Host:
    def size(self):               return 800, 600
    def measure(self, t, b=False): return len(t) * 7
    def set_zoom_px(self, px):    pass
    def after_idle(self, fn):     self.idle = fn        # captured, not run
    def after(self, ms, fn):      return None
    def hwnd(self):               return 0


def _eng():
    m = make_model(["A", "B"], [["1", "2"], ["3", "4"]], editable=True)
    e = GpuEngine(_Host(), m, col_w=[80, 80])
    painted = []
    e._paint_now = lambda: painted.append(1)      # stand in for the GL upload
    return e, painted


def test_zoom_animation_coalesces():
    e, painted = _eng()
    e._zoom_to = 2.0                    # animation in flight
    e.redraw()
    assert painted == []                # deferred, not painted synchronously
    assert e._paint_pending             # queued via after_idle


def test_idle_paints_synchronously():
    e, painted = _eng()
    e._zoom_to = None                   # no animation
    e.redraw()
    assert painted == [1]               # final frame lands immediately
