"""Differential fuzz: CoreModel (C++ backend) must match GridModel (oracle) over
random op sequences. Run: python scripts/tests/fuzz_coremodel.py [seed_count]"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "dist"))
from fastpygrid.core.model import GridModel
from fastpygrid.core.coremodel import CoreModel

W, H, R0 = 3, 1, 5
CELLS = ["", "a", "b", "cc", "a", "z", "mm"]


def mk_data(rng):
    return [[rng.choice(CELLS) for _ in range(W)] for _ in range(rng.randint(2, R0))]


def dump(m):
    return [[m.cell(gr, c) for c in range(W)] for gr in range(m.nrows())], m.nrows()


def rects(rng, m):
    n = m.nrows()
    if n <= H:
        return None
    r1 = rng.randint(H, n - 1); r2 = rng.randint(r1, n - 1)
    c1 = rng.randint(0, W - 1); c2 = rng.randint(c1, W - 1)
    return [(r1, c1, r2, c2)]


def check(a, b, ctx):
    if dump(a) != dump(b):
        da, db = dump(a), dump(b)
        for gr in range(max(len(da[0]), len(db[0]))):
            ra = da[0][gr] if gr < len(da[0]) else "--"
            rb = db[0][gr] if gr < len(db[0]) else "--"
            if ra != rb:
                print(f"  row {gr}: oracle={ra} core={rb}")
        raise AssertionError(f"cell mismatch after {ctx}\n oracle nrows={da[1]} core={db[1]}")
    for _ in range(3):
        rr = rects(random, a)
        if rr is None:
            break
        if a.selection_text(rr) != b.selection_text(rr):
            raise AssertionError(f"selection_text mismatch after {ctx}: {rr}\n"
                                 f" oracle={a.selection_text(rr)!r}\n core={b.selection_text(rr)!r}")
    for q in ("a", "z", "m", "c"):
        for cs in (False, True):
            fa = a.find_matches(q, cs)
            fb = b.find_matches(q, cs)
            if fa != fb:
                raise AssertionError(f"find({q!r},{cs}) mismatch after {ctx}\n oracle={fa}\n core={fb}")
    for c in range(W):
        if a.distinct_capped(c) != b.distinct_capped(c):
            raise AssertionError(f"distinct(col {c}) mismatch after {ctx}")


def run(seed):
    rng = random.Random(seed)
    data = mk_data(rng)
    oracle = GridModel([f"H{i}" for i in range(W)], [r[:] for r in data])
    core = CoreModel([f"H{i}" for i in range(W)], [r[:] for r in data])
    check(oracle, core, "init")
    for step in range(60):
        op = rng.choice(["set", "set", "del", "del", "paste", "paste",
                         "undo", "redo", "filter", "textfilter", "sort", "clearf",
                         "rocol", "rorow"])
        ctx = f"seed={seed} step={step} op={op}"
        if op == "set":
            if oracle.nrows() <= H:
                continue
            gr = rng.randint(H, oracle.nrows() - 1); c = rng.randint(0, W - 1)
            v = rng.choice(CELLS)
            oracle.set_cell(gr, c, v); core.set_cell(gr, c, v)
        elif op == "del":
            rr = rects(rng, oracle)
            if rr is None:
                continue
            oracle.delete_selection(rr); core.delete_selection(rr)
        elif op == "paste":
            rr = rects(rng, oracle)
            if rr is None:
                continue
            nr = rng.randint(1, 3); nc = rng.randint(1, W)
            txt = "\n".join("\t".join(rng.choice(CELLS) for _ in range(nc)) for _ in range(nr))
            oracle.paste_text(txt, rr, (rr[0][0], rr[0][1]))
            core.paste_text(txt, rr, (rr[0][0], rr[0][1]))
        elif op == "undo":
            oracle.undo(); core.undo()
        elif op == "redo":
            oracle.redo(); core.redo()
        elif op == "filter":
            c = rng.randint(0, W - 1)
            vals, _ = oracle.distinct_capped(c)
            allowed = set(rng.sample(vals, rng.randint(0, len(vals)))) if vals else set()
            allowed = allowed or None
            oracle.set_filter(c, allowed); core.set_filter(c, allowed)
        elif op == "textfilter":
            c = rng.randint(0, W - 1)
            if rng.random() < 0.3:
                oracle.set_text_filter(c, "contains", None); core.set_text_filter(c, "contains", None)
            else:
                op2 = rng.choice(["contains", "equals", "begins", "ends", "not_contains"])
                t = rng.choice(CELLS)
                oracle.set_text_filter(c, op2, t); core.set_text_filter(c, op2, t)
        elif op == "sort":
            c = rng.randint(0, W - 1); asc = rng.random() < 0.5
            oracle.set_sort(c, asc); core.set_sort(c, asc)
        elif op == "clearf":
            oracle.clear_filters(); core.clear_filters()
        elif op == "rocol":
            c = rng.randint(0, W - 1); on = rng.random() < 0.5
            oracle.set_readonly_col(c, on); core.set_readonly_col(c, on)
        elif op == "rorow":
            if oracle.nrows() > H:
                gr = rng.randint(H, oracle.nrows() - 1); on = rng.random() < 0.5
                oracle.set_readonly_row(gr, on); core.set_readonly_row(gr, on)
        check(oracle, core, ctx)


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    for s in range(n):
        run(s)
    print(f"OK: {n} fuzz seeds x 60 ops matched oracle")
