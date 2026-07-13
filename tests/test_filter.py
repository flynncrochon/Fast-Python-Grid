"""Headless self-check of the FilterController commit rules."""
from fastpygrid.core.filter import FilterController
from fastpygrid.core.coremodel import make_model


def test_commit_rules():
    m = make_model(["A"], [["x"], ["y"], ["x"], ["z"]])
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
