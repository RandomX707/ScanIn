"""The indexed formula graph must answer exactly like the original linear scan (and not be quadratic)."""
import random
import time

from packages.core.adapters.xlsx.adapter import CELL_COORD, _FormulaGraph, _column_index, _covers


def linear(refs, sheet, cell):
    key = cell.replace("$", "").upper()
    return frozenset(fid for ts, first, last, fid in refs if ts == sheet and _covers(first, last, key))


def test_indexed_lookup_equals_linear_scan_on_random_references():
    rnd = random.Random(7)
    cols = "ABCDEFGHIJ"
    refs = []
    for i in range(400):
        sheet = rnd.choice(["S1", "S2"])
        a = f"{rnd.choice(cols)}{rnd.randint(1, 30)}"
        b = a if rnd.random() < 0.6 else f"{rnd.choice(cols)}{rnd.randint(1, 30)}"
        refs.append((sheet, a, b, f"S1!Z{i}"))
    refs.append(("S1", "not-a-cell", "A1", "S1!Z999"))
    graph = _FormulaGraph(refs)
    for _ in range(600):
        sheet, cell = rnd.choice(["S1", "S2", "S3"]), f"{rnd.choice(cols)}{rnd.randint(1, 35)}"
        assert graph.direct(sheet, cell) == linear(refs, sheet, cell), (sheet, cell)


def test_lookup_cost_does_not_grow_with_formula_count_times_cells():
    refs = [("S", f"B{r}", f"B{r}", f"S!AF{r}") for r in range(1, 20001)]
    graph = _FormulaGraph(refs)
    start = time.perf_counter()
    for r in range(1, 20001):
        assert graph.direct("S", f"B{r}") == frozenset({f"S!AF{r}"})
    assert time.perf_counter() - start < 2.0
