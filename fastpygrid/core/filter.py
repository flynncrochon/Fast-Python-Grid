"""Column filter/sort popup logic, GUI-free, like FindController.

Behaviour (deferred distinct scan, per-value checked state, search over a capped
column, commit rules for "clear vs keep exactly the checked") lives here. The
popup just renders ``rows(query)`` with a checkbox per ``checked(v)`` and forwards
to ``toggle`` / ``toggle_all`` / ``commit``.
"""


class FilterController:
    def __init__(self, model, col):
        self.model = model
        self.col = col
        self.state = None          # v -> bool user toggles, None until load()
        self.active = None         # the column's active filter set, or None
        self.preloaded = []        # distinct preview (may be capped)
        self.capped = False

    def load(self):
        """Deferred distinct scan (next event-loop tick), so opening the popup is
        instant even on a 1M-row column."""
        self.active = self.model._filters.get(self.col)
        self.preloaded, self.capped = self.model.distinct_capped(self.col)
        self.state = {v: self.checked(v) for v in self.preloaded}

    def checked(self, v):
        """Explicit user toggle, else the active filter's default (all allowed when
        unfiltered)."""
        if self.state and v in self.state:
            return self.state[v]
        return self.active is None or v in self.active

    def rows(self, query):
        q = query.strip().lower()
        if not q:
            return self.preloaded
        if self.capped:                # search whole column, not just the preview
            return self.model.distinct_matching(self.col, q)
        return [v for v in self.preloaded if q in v.lower()]

    def all_on(self, rows):
        return bool(rows) and all(self.checked(v) for v in rows)

    def truncated(self, rows):
        return len(rows) >= self.model.DISTINCT_CAP

    def toggle(self, v):
        self.state[v] = not self.checked(v)

    def toggle_all(self, rows):
        target = not self.all_on(rows)
        for v in rows:
            self.state[v] = target

    def commit(self, query):
        """Apply on OK. Query present: too-many -> "contains", else filter TO the
        checked matches. Empty query: clear when all-checked and known complete, else
        keep exactly the checked."""
        query = query.strip()
        if query:
            rows = self.rows(query)
            if self.truncated(rows):
                self.model.set_text_filter(self.col, "contains", query)
            else:
                keep = {v for v in rows if self.checked(v)}
                self.model.set_filter(self.col, keep or None)
            return
        known = set(self.preloaded) | set(self.state)
        checked = {v for v in known if self.checked(v)}
        if len(checked) == len(known) and (self.active is None or not self.capped):
            self.model.set_filter(self.col, None)
        else:
            self.model.set_filter(self.col, checked)
