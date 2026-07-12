"""Optional pandas bridge. pandas is NOT a dependency -- this only duck-types a
DataFrame's ``.columns`` / ``.itertuples`` so it works with any pandas install
(or a look-alike) without importing pandas here.

    from fastpygrid.render.tk import make_sheet
    from fastpygrid import dataframe_to_grid
    make_sheet(*dataframe_to_grid(df))            # or make_model(*dataframe_to_grid(df))

A MultiIndex columns object (df with several header rows) becomes one grid header
row per level -- the model already treats a list-of-lists header as multi-row
(GridModel.set_data). NaN/NaT/None render as blank.
"""


def dataframe_to_grid(df):
    """(headers, rows) from a pandas DataFrame, ready to splat into
    make_model()/make_sheet(). MultiIndex columns -> multi-row header."""
    cols = df.columns
    nlev = getattr(cols, "nlevels", 1)
    if nlev > 1:                                   # each label is a per-level tuple
        headers = [[str(c[i]) for c in cols] for i in range(nlev)]
    else:
        headers = [[str(c) for c in cols]]
    blank = lambda v: "" if v is None or v != v else str(v)   # v!=v catches NaN/NaT
    rows = [[blank(v) for v in r]
            for r in df.itertuples(index=False, name=None)]    # name=None: plain
    return headers, rows                                       # tuples, no 255-col cap
