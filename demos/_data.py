"""Shared sample data for the demos + benchmarks. Pure data: each caller puts
fastpygrid on sys.path itself (demos use demos/fastpygrid, scripts use dist/)."""

HEADERS = ["Ticker", "Company", "Sector", "Price", "Chg%", "Volume", "Note"]
SECTORS = ["Technology", "Energy", "Finance", "Health", "Consumer", "Utilities"]
COL_W = [110, 190, 155, 90, 80, 110, 90]   # Sector (idx 2) widened for its ▼ dropdown arrow

# A batch of extra columns so the demo scrolls horizontally (exercises column
# resize / autofit). 8 quarters of revenue + a few text fields.
QUARTERS = ["%s %d" % (q, y) for y in (2023, 2024) for q in ("Q1", "Q2", "Q3", "Q4")]
HEADERS += QUARTERS + ["Analyst", "Rating", "Country", "Notes"]
COL_W += [95] * len(QUARTERS) + [140, 120, 120, 260]   # Rating widened for its ▼ dropdown arrow

# Two-row grouped header: a band of "FY 2023"/"FY 2024" spanning each year's
# quarters above the field names (adjacent same-label cells merge into one
# spanning cell). Pass GROUPED_HEADERS instead of HEADERS to exercise it.
GROUPS = ([""] * 7 + ["FY %d" % y for y in (2023, 2024) for _q in range(4)]
          + [""] * 4)
GROUPED_HEADERS = [GROUPS, HEADERS]
RATINGS = ["Buy", "Hold", "Sell", "Strong Buy", "Underweight"]
COUNTRIES = ["USA", "Germany", "Japan", "United Kingdom", "South Korea", "Brazil"]


def gen_rows(n):
    return [[
        "TIK%05d" % i,
        "Company %d Inc." % i,
        SECTORS[i % len(SECTORS)],
        "%.2f" % (10 + (i * 7 % 9000) / 10.0),
        "%+.2f" % (((i * 13) % 800 - 400) / 100.0),
        str((i * 3779) % 5_000_000),
        "watch" if i % 17 == 0 else "",
        *("%.1fM" % (((i * (q + 7)) % 9000) / 10.0) for q in range(len(QUARTERS))),
        "Analyst %d" % (i % 40),
        RATINGS[i % len(RATINGS)],
        COUNTRIES[i % len(COUNTRIES)],
        "Longer free-text note for row %d to show autofit clipping." % i if i % 5 == 0 else "",
    ] for i in range(n)]


def rows_arg(argv, default=100_000):
    return int(argv[argv.index("--rows") + 1]) if "--rows" in argv else default


def style_demo(model, lo=None, hi=None):
    """Data-driven per-cell styling so the demos exercise fg / bold / bg:
    Chg% red/green by sign, 'Strong Buy' ratings bold-green, 'watch' notes flagged
    amber. Optional [lo, hi) GRID-row range so stream_styles() can apply it in
    chunks. Defaults to the whole dataset."""
    chg, rating, note = HEADERS.index("Chg%"), HEADERS.index("Rating"), HEADERS.index("Note")
    lo = model.header_rows if lo is None else lo
    hi = model._real_rows() if hi is None else hi
    for gr in range(lo, hi):
        v = model.cell(gr, chg)
        if v:
            model.set_cell_style(gr, chg, fg="#c0392b" if v.startswith("-") else "#1e8449", bold=True)
        if model.cell(gr, rating) == "Strong Buy":
            model.set_cell_style(gr, rating, fg="#1e8449", bold=True)
        if model.cell(gr, note) == "watch":
            model.set_cell_style(gr, note, bg="#fff3b0")


def stream_styles(win, chunk=4000):
    """Apply the per-cell style pass in chunks AFTER the first frame, so the grid
    shows instantly and never freezes on load. The first chunk covers the visible
    rows, so what you see is styled within a frame. The rest streams in behind it."""
    m, view = win.model, win.grid_view
    total = m._real_rows()

    def step(lo):
        style_demo(m, lo, min(lo + chunk, total))
        m.changed()
        if lo + chunk < total:
            view.after(1, lambda: step(lo + chunk))

    view.after(0, lambda: step(m.header_rows))


# Stress dropdown: 1000 options, most far wider than any column: exercises the
# popup-widening (list grows to the text) and large option lists. The whole column
# shares one interned tuple, so this costs one list, not one copy per row.
_LONG = ("Comprehensive multi-word option label that is far wider than the column",
         "Another lengthy descriptive choice overflowing the narrow cell by a lot",
         "Short",
         "Medium-length option text")
LONG_OPTS = ["%04d - %s" % (i, _LONG[i % len(_LONG)]) for i in range(1000)]


def lines_demo(model):
    """Thick black section dividers: a vertical rule right of the Chg% column
    (splits the identity/price block from the quarterly financials) and a couple
    of horizontal rules to band the data. The vline is positional; the hlines
    latch onto their data row and follow it through sort/filter."""
    model.set_vline(HEADERS.index("Chg%"))
    model.set_hline(10)                        # under whatever row is at grid 10 now
    model.set_hline(25)


def readonly_demo(model):
    """Lock the Ticker + Price columns: still selectable and copyable, but edits,
    paste and delete are rejected (try double-clicking a Ticker cell, no editor)."""
    model.set_readonly_col(HEADERS.index("Ticker"))
    model.set_readonly_col(HEADERS.index("Price"))


def numeric_demo(model):
    """Mark the number columns so their sort is smallest->largest, not a->z
    (try sorting Price or Volume: without this "100" would land before "9")."""
    for name in ("Price", "Chg%", "Volume"):   # QUARTERS carry an "M" suffix -> not plain numbers
        model.set_column_numeric(HEADERS.index(name))


def choices_demo(model):
    """Make Sector/Rating dropdowns, plus a 1000-option long-text dropdown on the
    narrow Note column (to exercise popup widening). Same bulk-before-build note as
    style_demo."""
    sector, rating, note = (HEADERS.index("Sector"), HEADERS.index("Rating"),
                            HEADERS.index("Note"))
    model.set_col_choices(sector, SECTORS)     # whole-column dropdowns: O(1), not per row
    model.set_col_choices(rating, RATINGS)
    model.set_col_choices(note, LONG_OPTS)
