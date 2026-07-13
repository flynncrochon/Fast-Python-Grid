"""Shared selection + frozen-pane state machine, pure functions so read-only and
editable grids select, extend and cross frozen panes IDENTICALLY.

Coordinates are the HOST grid's own (bounds passed in, results in the same
coords). Grids differ only in header placement and frozen columns:

  * `top_hrow`   : topmost header pseudo-row (and where a column selection starts).
                   Single top header -> ``0``; header bands above data row 0 ->
                   ``-header_rows`` (e.g. ``-2`` for a two-band header).
  * `last_row`   : last visible data row.
  * `last_col`   : last column.
  * `frozen_cols`: pinned leading columns; ``0`` makes freeze logic a no-op.

Press regions (from the host hit-test): ``"all"`` corner=whole sheet,
``"gutter"`` row-number=whole row, ``"band"`` letter-band=whole column,
``"cell"`` data/header-title=single cell.

Modifiers (spreadsheet-style): plain/Shift collapses disjoint Ctrl ranges to one;
Ctrl banks the active range and starts fresh; Ctrl+Shift extends while keeping the
others.
"""


def normalize(ranges):
    """Each (r1, c1, r2, c2) rewritten so r1<=r2 and c1<=c2."""
    return [(min(a, c), min(b, d), max(a, c), max(b, d)) for (a, b, c, d) in ranges]


def resolve_click(region, row, col, *, top_hrow, last_row, last_col,
                  anchor, sel, extra, ctrl, shift):
    """Resolve a press (with Ctrl/Shift) into ``(sel, extra, active, anchor)``."""
    if region == "all":  # select-all ignores modifiers
        full = (top_hrow, 0, last_row, last_col)
        return full, [], (top_hrow, 0), (top_hrow, 0)
    if shift and anchor is not None:
        ar, ac = anchor
        if region == "gutter":
            new = (min(ar, row), 0, max(ar, row), last_col)
            active = (row, 0)
        elif region == "band":
            new = (top_hrow, min(ac, col), last_row, max(ac, col))
            active = (top_hrow, col)
        else:  # cell (header-title cell = normal cell)
            erow = max(row, top_hrow)
            new = (min(ar, erow), min(ac, col), max(ar, erow), max(ac, col))
            active = (erow, col)
        # Plain Shift collapses to one range; only Ctrl+Shift keeps disjoint ranges.
        kept = list(extra) if ctrl else []
        return new, kept, active, anchor
    # No Shift: Ctrl banks the active range and starts a fresh single one.
    new_extra = (list(extra) + [sel]) if (ctrl and sel) else []
    if region == "gutter":
        new = (row, 0, row, last_col)
        na = (row, 0)
    elif region == "band":
        new = (top_hrow, col, last_row, col)
        na = (top_hrow, col)
    else:  # cell
        new = (row, col, row, col)
        na = (row, col)
    return new, new_extra, na, na


def resolve_drag(drag_region, row, col, *, top_hrow, last_row, last_col, anchor):
    """Resolve a drag-extend into ``(sel, active)``; anchor unchanged.

    ``drag_region`` is where the drag STARTED: ``"gutter"`` extends whole rows,
    ``"band"`` whole columns, else a cell rectangle. Caller handles frozen-pane
    crossing (see :func:`edge_reveal_col`) before passing ``col`` for a cell drag.
    """
    ar, ac = anchor
    if drag_region == "gutter":
        row = max(0, row)
        return (min(ar, row), 0, max(ar, row), last_col), (row, 0)
    if drag_region == "band":
        return (top_hrow, min(ac, col), last_row, max(ac, col)), (top_hrow, col)
    erow = max(row, top_hrow)
    return (min(ar, erow), min(ac, col), max(ar, erow), max(ac, col)), (erow, col)


def edge_reveal_col(col, *, anchor_col, frozen_cols, scroll_x, ncols,
                    pointer_x, gutter_w, frozen_w, body_w, leaf_x):
    """Frozen-pane crossing for a horizontal cell drag.

    No frozen columns (``frozen_cols <= 0``) -> no-op, returns ``col`` unchanged.
    Otherwise keyed on the ANCHOR column, so a vertical drag begun in a frozen
    column keeps its column:

      * Started scrollable, pointer at/left of the freeze line with columns
        scrolled off -> target the scrollable column hidden under the frozen block
        (caller's scroll-into-view reveals it). NEVER snap onto a frozen column.
      * Pointer past the right edge -> next column right.
      * Else -> the pointer's own column.

    ``leaf_x`` maps a column index to its current screen x.
    """
    if frozen_cols <= 0:
        return col
    fx0 = gutter_w + frozen_w
    if anchor_col >= frozen_cols and pointer_x < fx0 and scroll_x > 0:
        lvc = frozen_cols
        while lvc < ncols - 1 and leaf_x(lvc) < fx0 - 0.5:
            lvc += 1
        return max(frozen_cols, lvc - 1)
    if pointer_x > body_w:
        return min(ncols - 1, col + 1)
    return col


def edge_scan(start, step, lo, hi, occupied):
    """Ctrl+arrow target along one axis. ``occupied(i)`` = cell ``i`` has a value.
    From a filled run, stop at its last filled cell before a gap; from a gap/edge,
    jump to the next filled cell (or the boundary)."""
    nxt = start + step
    if nxt < lo or nxt > hi:
        return start
    i = start
    if occupied(start) and occupied(nxt):
        while lo <= i + step <= hi and occupied(i + step):
            i += step
        return i
    i = nxt
    while lo <= i + step <= hi and not occupied(i):
        i += step
    return i


def resolve_arrow(key, *, active, anchor, top_hrow, last_row, last_col,
                  shift, ctrl, page_rows=1, occupied_row=None, occupied_col=None):
    """Resolve a navigation key into ``(sel, extra, active, anchor)``.

    Header bands are normal cells, so Up clamps to ``top_hrow`` and the freeze never
    blocks the cursor. Ctrl moves to an edge: with an ``occupied_*`` callback it's a
    data-block jump, without one a jump to the grid boundary. Shift extends from the
    anchor (collapsing disjoint ranges); a plain move resets to the new cell.
    """
    r, c = active if active is not None else (top_hrow, 0)
    nr, nc = r, c
    if key == "Up":
        nr = (edge_scan(r, -1, top_hrow, last_row, occupied_row)
              if (ctrl and occupied_row) else top_hrow if ctrl else max(top_hrow, r - 1))
    elif key == "Down":
        nr = (edge_scan(r, 1, top_hrow, last_row, occupied_row)
              if (ctrl and occupied_row) else last_row if ctrl else min(last_row, r + 1))
    elif key == "Left":
        nc = (edge_scan(c, -1, 0, last_col, occupied_col)
              if (ctrl and occupied_col) else 0 if ctrl else max(0, c - 1))
    elif key == "Right":
        nc = (edge_scan(c, 1, 0, last_col, occupied_col)
              if (ctrl and occupied_col) else last_col if ctrl else min(last_col, c + 1))
    elif key == "Home":
        nc = 0
    elif key == "End":
        nc = last_col
    elif key == "Prior":  # Page Up
        nr = max(top_hrow, r - max(1, page_rows))
    elif key == "Next":   # Page Down
        nr = min(last_row, r + max(1, page_rows))
    if shift and anchor is not None:
        ar, ac = anchor
        new = (min(ar, nr), min(ac, nc), max(ar, nr), max(ac, nc))
        return new, [], (nr, nc), anchor
    return (nr, nc, nr, nc), [], (nr, nc), (nr, nc)
