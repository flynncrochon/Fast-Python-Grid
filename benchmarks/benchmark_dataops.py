"""Data-op perf: the one-shot operations that block the UI thread -- sort, filter,
find -- must each stay under a frame (<5 ms => 200 fps) at scale, or the sheet
freezes when you click a header. They run natively in gridcore.dll (filter/sort/find
off Python's per-cell FFI loop) and parallelize across cores above ~20k rows.

Reports p50 / p90 ms and fps@p50 over repeated runs at 100k rows x 20 cols. Sort and
find need the DLL's threads; text/value filter clear the bar single-threaded.

Run:  python benchmarks/benchmark_dataops.py
Needs the DLLs built (build-windows.bat / build.sh) into fastpygrid/core/.
"""
import random
import sys
import time

from fastpygrid.core.coremodel import _LIB, make_model

NROWS = 100_000
NCOLS = 20
REPS = 25


def _dataset():
    rng = random.Random(1)
    headers = ["c%02d" % i for i in range(NCOLS)]
    # col 0 numeric (6-digit), col 1 has 1000 distinct values (realistic filter cap),
    # rest structured text.
    rows = [["%06d" % rng.randrange(1_000_000), "v%03d" % rng.randrange(1000)]
            + ["r%dc%d" % (r, c) for c in range(2, NCOLS)] for r in range(NROWS)]
    return headers, rows


def _time(fn):
    fn()                                   # warm
    xs = []
    for _ in range(REPS):
        t0 = time.perf_counter(); fn(); xs.append((time.perf_counter() - t0) * 1e3)
    xs.sort()
    return xs[len(xs) // 2], xs[min(len(xs) - 1, int(0.9 * len(xs)))]


def main():
    if _LIB is None:
        print("gridcore.dll not built, run build-windows.bat / build.sh"); return 1
    headers, rows = _dataset()
    m = make_model(headers, rows)
    m.set_column_numeric(0, True)

    # color the bg of col 2 across 4 colors on ~50% of rows (rest uncolored), so the
    # native color filter/sort/distinct have realistic work. One bulk FFI (gc_set_styles).
    rng = random.Random(2)
    palette = ["#ff0000", "#00ff00", "#0000ff", "#ffff00"]
    m.set_cell_styles([(r + m._hdr, 2, None, palette[rng.randrange(4)], None)
                       for r in range(NROWS) if rng.random() < 0.5])

    cases = [
        ("numeric sort",   lambda: m.set_sort(0, True),                       m.clear_sort),
        ("text sort",      lambda: m.set_sort(2, True),                       m.clear_sort),
        ("text filter",    lambda: m.set_text_filter(2, "contains", "c2"),    lambda: m.clear_column_filter(2)),
        ("value filter",   lambda: m.set_filter(1, set("v%03d" % i for i in range(1000))),
                                                                              lambda: m.clear_column_filter(1)),
        ("color filter",   lambda: m.set_color_filter(2, "bg", "#ff0000"),    lambda: m.clear_column_filter(2)),
        ("color sort",     lambda: m.set_color_sort(2, "bg", "#ff0000", True), m.clear_sort),
        ("distinct colors", lambda: m.distinct_colors(2, "bg"),               None),
        ("find_matches",   lambda: m.find_matches("r999"),                    None),
    ]
    print("data-op benchmark  (%d rows x %d cols)\n" % (NROWS, NCOLS))
    print("  %-16s %8s %8s   %s" % ("op", "p50 ms", "p90 ms", "fps@p50"))
    for name, run, reset in cases:
        p50, p90 = _time(run)
        if reset:
            reset()
        flag = "" if p90 <= 5 else "  (p90 over 5ms budget)"
        print("  %-16s %8.2f %8.2f   %6.0f%s" % (name, p50, p90, 1e3 / p50 if p50 else 0, flag))
    return 0


if __name__ == "__main__":
    sys.exit(main())
