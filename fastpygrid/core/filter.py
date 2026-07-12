"""Column filter/sort popup logic -- GUI-free, like FindController.

The GpuEngine filter popup is just a widget: a value checklist, a search box
and OK/Cancel. All the actual behaviour (deferred distinct scan, per-value
checked state, search over a capped column, and the exact commit rules for
"clear vs keep exactly the checked members") lives here. The popup only renders
``rows(query)`` with a checkbox per ``checked(v)`` and forwards clicks to
``toggle`` / ``toggle_all`` and OK to ``commit``.
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
        """Deferred distinct scan, run on the next event-loop tick so opening the
        popup is instant even on a 1M-row column."""
        self.active = self.model._filters.get(self.col)
        self.preloaded, self.capped = self.model.distinct_capped(self.col)
        self.state = {v: self.checked(v) for v in self.preloaded}

    def checked(self, v):
        """An explicit user toggle, else the default from the active filter (all
        allowed when there's no filter)."""
        if self.state and v in self.state:
            return self.state[v]
        return self.active is None or v in self.active

    def rows(self, query):
        q = query.strip().lower()
        if not q:
            return self.preloaded
        if self.capped:                # search the whole column, not just the preview
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
        """Apply the popup on OK. With a search query: too-many -> "contains",
        else filter TO the checked matches. Empty query: clear when everything's
        checked and we know it's everything, else keep exactly the checked."""
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


if __name__ == "__main__":   # headless self-check of the commit rules
    from .model import GridModel
    m = GridModel(["A"], [["x"], ["y"], ["x"], ["z"]])
    f = FilterController(m, 0); f.load()
    assert f.rows("") == ["x", "y", "z"], f.rows("")
    assert f.all_on(f.rows(""))
    f.toggle("y"); assert not f.checked("y")
    f.commit("")                                    # keep exactly {x, z}
    assert m._filters[0] == {"x", "z"}, m._filters
    f2 = FilterController(m, 0); f2.load()          # everything checked -> clears
    f2.toggle_all(f2.rows("")); assert f2.all_on(f2.rows(""))
    # active filter present + not capped -> "all checked" clears it
    f2.commit(""); assert 0 not in m._filters, m._filters
    print("filter self-check ok")
