"""Shared sample data for the demos + benchmarks. Also puts the repo root on
sys.path so `import fastgrid` works when a script is run from scripts/."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HEADERS = ["Ticker", "Company", "Sector", "Price", "Chg%", "Volume", "Note"]
SECTORS = ["Technology", "Energy", "Finance", "Health", "Consumer", "Utilities"]
COL_W = [110, 190, 130, 90, 80, 110, 90]

# A batch of extra columns so the demo scrolls horizontally (exercises column
# resize / autofit). 8 quarters of revenue + a few text fields.
QUARTERS = ["%s %d" % (q, y) for y in (2023, 2024) for q in ("Q1", "Q2", "Q3", "Q4")]
HEADERS += QUARTERS + ["Analyst", "Rating", "Country", "Notes"]
COL_W += [95] * len(QUARTERS) + [140, 80, 120, 260]
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
