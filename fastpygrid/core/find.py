"""Ctrl+F find controller, GUI-free search/navigation over model.find_matches.

Behaviour the GpuEngine find bar drives:
  * highlight every match + a distinct active-match marker,
  * count "i/N" ("i/N+" if capped, "No results", "" when empty),
  * Next/Prev wrap; the FIRST Enter lands on the nearest match, not past it,
  * nearest = current cell, row-major >=,
  * optional scope (a multi-cell selection) confines the search and stays visible
    while stepping (selection not collapsed),
  * case toggle.

Talks to the host grid via ``.active``/``.anchor``/``.sel``/``.extra``, ``.model``
and ``.scroll_into_view(r, c)``. Highlight repaints via ``model.set_find`` /
``model.clear_find`` (fire the model change callback -> redraw).
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
        self.on_count = lambda text: None    # widget sets this to update its label

    @staticmethod
    def _scope_of(ranges):
        """Meaningful scope = a selection covering more than one cell."""
        rngs = normalize(ranges)
        multi = len(rngs) > 1 or (rngs and (rngs[0][0] != rngs[0][2]
                                            or rngs[0][1] != rngs[0][3]))
        return rngs if multi else None

    def open(self, ranges):
        """Enter find: capture scope from the selection, anchor nearest on the active
        cell. Returns whether a scope is available."""
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
            if self.scope is None:                       # collapse selection onto match
                self.grid.active = self.grid.anchor = cell
                self.grid.sel, self.grid.extra = (cell[0], cell[1], cell[0], cell[1]), []
            self.grid.scroll_into_view(*cell)            # scoped: keep selection, reveal
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
