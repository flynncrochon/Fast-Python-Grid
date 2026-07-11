"""Selection-correctness check for fastpygrid's shared state machine.

The one copy of "what does a click / drag / arrow do" lives in
core/selection.py and drives BOTH renderers -- so this asserts it directly,
with no GUI. Spreadsheet semantics: plain click resets, Ctrl banks a range, Shift
extends, whole-row/col/select-all, Ctrl+arrow block jumps.

    python scripts/tests/check_select.py
"""
import os
import sys

# needs fastpygrid installed -- run demos/setup.bat, or `pip install .`

from fastpygrid.core import selection as S                        # noqa: E402

B = dict(top_hrow=0, last_row=99, last_col=6)     # a 100x7 grid


def click(region, r, c, sel=(0, 0, 0, 0), extra=(), anchor=(0, 0), ctrl=False, shift=False):
    return S.resolve_click(region, r, c, anchor=anchor, sel=sel, extra=list(extra),
                           ctrl=ctrl, shift=shift, **B)


def main():
    # plain cell click -> single-cell selection, anchor + active on it
    sel, extra, active, anchor = click("cell", 5, 3)
    assert sel == (5, 3, 5, 3) and extra == [] and active == (5, 3) and anchor == (5, 3)

    # shift-click from anchor -> rectangle
    sel, extra, active, _ = click("cell", 8, 5, anchor=(5, 3), shift=True)
    assert sel == (5, 3, 8, 5) and active == (8, 5)

    # ctrl-click banks the previous range and starts a fresh one
    sel, extra, active, anchor = click("cell", 2, 2, sel=(5, 3, 5, 3), ctrl=True)
    assert extra == [(5, 3, 5, 3)] and sel == (2, 2, 2, 2)

    # whole row (gutter) and whole column (band)
    assert click("gutter", 9, 0)[0] == (9, 0, 9, 6)
    assert click("band", 0, 4)[0] == (0, 4, 99, 4)

    # select-all (corner) ignores modifiers
    assert click("all", 0, 0, ctrl=True)[0] == (0, 0, 99, 6)

    # drag extends a cell rectangle from the anchor
    sel, active = S.resolve_drag("cell", 20, 6, anchor=(10, 2), **B)
    assert sel == (10, 2, 20, 6) and active == (20, 6)

    # arrow: plain Down moves one, Shift extends, clamps at edges
    sel, _e, active, _a = S.resolve_arrow("Down", active=(5, 3), anchor=(5, 3),
                                          shift=False, ctrl=False, **B)
    assert active == (6, 3) and sel == (6, 3, 6, 3)
    sel, _e, active, _a = S.resolve_arrow("Right", active=(0, 6), anchor=(0, 6),
                                          shift=True, ctrl=False, **B)
    assert active == (0, 6)                       # already at last col, clamps

    # Ctrl+Down block jump: from a filled run stop at the last filled cell before a gap
    filled = {0, 1, 2, 3, 7, 8}
    _s, _e, active, _a = S.resolve_arrow("Down", active=(0, 0), anchor=(0, 0),
                                         shift=False, ctrl=True,
                                         occupied_row=lambda r: r in filled, **B)
    assert active[0] == 3, active

    print("check_select OK: 11 selection cases pass")


if __name__ == "__main__":
    main()
