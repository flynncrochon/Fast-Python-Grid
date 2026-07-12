"""Zoom performance benchmark.

Ctrl+wheel zoom eases toward a target ~165 Hz (gpu._ZOOM_MS=6), and every eased
frame runs GridController.zoom_to: recompute metrics + rebuild the whole display
list (paint_to). This times exactly that per-frame CPU cost, headless: it drives
the real controller + engine + C++ body emitter but skips the GL upload (the one
part a benchmark can't measure without a window). Lower ms/frame = snappier zoom.

    python benchmarks/bench_zoom.py                 # default 100k rows
    python benchmarks/bench_zoom.py --rows 500000

Compare a change: run before, apply, run after, diff the median ms/frame.
"""
import argparse
import gc
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastpygrid.core.gpu import GpuEngine, GpuCanvas
from fastpygrid.core.coremodel import make_model

HEADERS = ["Ticker", "Name", "Sector", "Price", "Chg%", "Volume", "Rating"]
COL_W = [90, 200, 130, 90, 80, 110, 90]


class FakeHost:
    """Minimal toolkit host: enough of the contract for paint_to + zoom_to to run
    without Tk/Qt/GL. measure() estimates text width the way a real font would."""
    def __init__(self, w, h):
        self._w, self._h, self._px = w, h, 13
    def size(self):                 return self._w, self._h
    def measure(self, text, bold=False):  return round(len(text) * self._px * 0.6)
    def set_zoom_px(self, px):      self._px = px            # what a real host recreates fonts on
    def after(self, ms, fn):        return None              # zoom easing is driven by the bench, not timers
    def after_cancel(self, h):      pass
    def after_idle(self, fn):       pass
    def focus(self):                pass
    def set_cursor(self, kind):     pass
    def hwnd(self):                 return 0


# A representative eased z-sweep for measuring per-frame build cost. Real zoom eases
# by real elapsed time (gpu._ZOOM_TAU); this fixed-fraction synth just needs a similar
# frame COUNT, not the exact curve -- what it measures is the cost of building one frame.
def eased_targets(start, target, ease=0.12, snap=GpuEngine._ZOOM_SNAP):
    """A ~refresh-rate eased z sequence gliding start -> target (representative frame count)."""
    cur, out = start, []
    while abs(target - cur) > snap:
        cur += (target - cur) * ease
        out.append(cur)
    out.append(target)
    return out


def zoom_sweep():
    """A realistic gesture: zoom all the way in, then all the way out, then home."""
    return (eased_targets(1.0, 4.0) + eased_targets(4.0, 0.4) + eased_targets(0.4, 1.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=100_000)
    ap.add_argument("--reps", type=int, default=30)
    ap.add_argument("--size", default="1600x900")
    args = ap.parse_args()

    w, h = (int(v) for v in args.size.split("x"))
    rows = [[f"R{r}C{c}" for c in range(len(HEADERS))] for r in range(args.rows)]
    model = make_model(HEADERS, rows, editable=True)

    host = FakeHost(w, h)
    eng = GpuEngine(host, model, col_w=COL_W, frozen=2)
    eng._surf = 1                               # non-None so the engine considers itself attached
    # Build a real frame per redraw, minus the GL upload (gpu_render). This is the
    # CPU-side per-frame cost zoom triggers ~165x/sec.
    eng.redraw = lambda: eng.paint_to(GpuCanvas(eng._fpx, eng._scale))

    targets = zoom_sweep()
    frames = len(targets)

    eng.ctl.zoom_to(1.0001); eng.redraw()       # warm caches / JIT of the C++ path

    # Time EACH frame (not the whole sweep): a smooth high fps is decided by the WORST
    # frame, not the average -- one frame over the vblank budget drops a refresh and
    # reads as a stutter, however fast the median is.
    frame_ms = []
    gc.disable()                                # engine freezes GC for the glide (gpu.zoom); mirror it
    for _ in range(args.reps):
        eng.ctl._zoom = 1.0                     # reset without a repaint
        for z in targets:
            t0 = time.perf_counter()
            eng.ctl.zoom_to(z)
            frame_ms.append((time.perf_counter() - t0) * 1000)
    gc.enable()

    frame_ms.sort()
    n = len(frame_ms)
    med = frame_ms[n // 2]
    p99 = frame_ms[min(n - 1, int(n * 0.99))]
    worst = frame_ms[-1]
    print(f"rows={args.rows:,}  viewport={w}x{h}  frames/sweep={frames}  reps={args.reps}")
    print(f"per-frame CPU  median {med:6.3f} ms   p99 {p99:6.3f} ms   worst {worst:6.3f} ms")
    print(f"theoretical fps (vsync off, CPU-bound):  median {1000/med:6.0f}   "
          f"sustained (worst) {1000/worst:6.0f}")
    print("vblank headroom (CPU frame vs refresh budget -- <100% = frame fits, sustains that fps):")
    for hz, budget in ((60, 16.67), (120, 8.33), (144, 6.94), (240, 4.17)):
        pct = worst / budget * 100
        print(f"   {hz:3d} Hz  budget {budget:5.2f} ms   worst frame = {pct:5.1f}%   "
              f"{'OK' if pct < 100 else 'MISS'}")


if __name__ == "__main__":
    main()
