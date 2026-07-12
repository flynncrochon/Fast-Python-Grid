"""Ctrl+F find controller -- GUI-free search/navigation over model.find_matches.

The GpuEngine find bar drives this logic:

  * highlight every matching cell (lazy, via model find-state) + a distinct
    active-match marker,
  * count reads "i/N" (or "i/N+" when the navigable list was capped, "No results",
    or "" when the query is empty),
  * Next/Prev wrap. The FIRST Enter lands on the highlighted nearest match rather
    than skipping past it,
  * nearest match is relative to the current cell (row-major >=),
  * an optional scope (the selection, when it covers more than one cell) confines
    the search and is KEPT visible while stepping (selection isn't collapsed),
  * case-sensitivity toggle.

The controller talks to the host grid through a small surface it already exposes:
``.active``/``.anchor``/``.sel``/``.extra`` (selection state), ``.model`` and
``.scroll_into_view(r, c)``. Highlight repaints happen via ``model.set_find`` /
``model.clear_find`` (which fire the model's change callback -> the grid redraws).
"""
from .selection import normalize


class FindController:
    def __init__(self, grid):
        self.grid = grid
        self.model = grid.model
        self.matches = []
        self.idx = -1
        self.capped = False
        self.case = False
        self.scope = None            # active scope rects, or None
        self.scope_range = None      # scope captured at open() (for the toggle)
        self.query = ""
        self.current = (1, 0)        # anchor for nearest / "already on match"
        self.on_count = lambda text: None    # the widget sets this to update its label

    @staticmethod
    def _scope_of(ranges):
        """A meaningful scope = the selection when it covers more than one cell."""
        rngs = normalize(ranges)
        multi = len(rngs) > 1 or (rngs and (rngs[0][0] != rngs[0][2]
                                            or rngs[0][1] != rngs[0][3]))
        return rngs if multi else None

    def open(self, ranges):
        """Enter find: capture scope from the current selection, anchor nearest on
        the active cell. Returns whether a scope is available (for the widget)."""
        self.scope_range = self._scope_of(ranges)
        self.scope = self.scope_range
        self.current = tuple(self.grid.active)
        self.query = ""
        self.matches, self.idx = [], -1
        self.run("", navigate=False)
        return self.scope_range is not None

    def run(self, query, navigate=False):
        self.query = query
        self.matches, self.capped = self.model.find_matches(query, self.case, self.scope)
        if not self.matches:
            self.idx = -1
            self.on_count("No results" if query else "")
            self.model.set_find(query, self.case, self.scope, None)
            return
        self.idx = next((i for i, c in enumerate(self.matches) if c >= self.current), 0)
        self._activate(navigate)

    def _activate(self, navigate):
        cell = self.matches[self.idx]
        self.on_count("%d/%d%s" % (self.idx + 1, len(self.matches), "+" if self.capped else ""))
        if navigate:
            self.current = cell
            if self.scope is None:                       # collapse selection onto the match
                self.grid.active = self.grid.anchor = cell
                self.grid.sel, self.grid.extra = (cell[0], cell[1], cell[0], cell[1]), []
            self.grid.scroll_into_view(*cell)            # scoped: keep selection, just reveal
        self.model.set_find(self.query, self.case, self.scope, cell)   # highlight + redraw

    def step(self, delta):
        if not self.matches:
            self.run(self.query, navigate=True)
            return
        if tuple(self.current) != tuple(self.matches[self.idx]):
            self._activate(True)                         # first Enter lands on the match
            return
        self.idx = (self.idx + delta) % len(self.matches)
        self._activate(True)

    def set_case(self, on):
        self.case = on
        self.run(self.query, navigate=False)

    def set_scope(self, on):
        self.scope = self.scope_range if on else None
        self.run(self.query, navigate=False)

    def close(self):
        self.matches, self.idx = [], -1
        self.model.clear_find()
