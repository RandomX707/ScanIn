"""Complete streamed XLSX comparison over windowed indexes: every cell examined, nothing dropped, nothing invented."""
import sqlite3

from packages.core import compare as c


def make_db(rows: int, cols: int = 10, edits: dict | None = None, drop: set | None = None, extra: list | None = None):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE cells (r INTEGER, c INTEGER, v TEXT, t TEXT, s INTEGER, id TEXT, fp TEXT, e INTEGER, rs TEXT, PRIMARY KEY (r, c)) WITHOUT ROWID")
    batch = []
    for r in range(1, rows + 1):
        for k in range(1, cols + 1):
            if drop and (r, k) in drop:
                continue
            v = (edits or {}).get((r, k), f"{r}x{k}")
            batch.append((r, k, v, "string", 0, f"xlsx:S!{r}:{k}", "fp", 1, None))
    batch += extra or []
    db.executemany("INSERT INTO cells VALUES (?,?,?,?,?,?,?,?,?)", batch)
    return db


def meta(styles=None, merges=(), name="S", index=0):
    return {"styles": styles if styles is not None else [{}], "sheets": [{"index": index, "name": name, "merges": [list(m) for m in merges], "images": [], "cols": {}, "rows": {}, "freeze": None, "hidden": False}]}


class Handle:
    """sqlite connection that ignores close() so in-memory databases can be walked more than once."""
    def __init__(self, db): self.db = db
    def execute(self, *a): return self.db.execute(*a)
    def close(self): pass


def run(a, b, ma=None, mb=None):
    return c.diff_xlsx_indexed(ma or meta(), mb or meta(), lambda i: Handle(a), lambda i: Handle(b))



def test_changes_far_beyond_the_first_16000_cells_are_found_and_nothing_is_invented():
    a = make_db(8000, 10)                                              # 80,000 cells
    b = make_db(8000, 10, edits={(7990, 3): "changed", (4000, 1): "changed too", (1, 10): "first"})
    out = run(a, b)
    assert out["complete"] is True and out["examined_cells"] == 80000
    assert [x["label"] for x in out["changes"]] == ["S!J1", "S!A4000", "S!C7990"]            # stable (row, column) order
    assert out["summary"] == {"added": 0, "removed": 0, "modified": 3, "total": 3, "truncated": False}
    assert all(x["category"] == "value" for x in out["changes"])


def test_added_and_removed_cells_are_reported_as_such_not_as_modifications():
    a = make_db(100, 5, drop={(50, 2)})
    b = make_db(100, 5, drop={(60, 3)})
    out = run(a, b)
    by = {x["label"]: x["kind"] for x in out["changes"]}
    assert by == {"S!B50": "added", "S!C60": "removed"}


def test_identical_workbooks_have_no_changes_and_are_still_fully_examined():
    out = run(make_db(300, 8), make_db(300, 8))
    assert out["changes"] == [] and out["examined_cells"] == 2400 and out["complete"] is True


def test_formula_and_style_and_merge_and_sheet_changes():
    f_a = (5, 5, "=SUM(A1:A3)", "string", 1, "id", "fp", 0, "formula_cell")
    f_b = (5, 5, "=SUM(A1:A4)", "string", 2, "id", "fp", 0, "formula_cell")
    a, b = make_db(10, 3, extra=[f_a]), make_db(10, 3, extra=[f_b])
    out = run(a, b, meta([{}, {"bl": 1}], merges=[(1, 1, 1, 2)]), meta([{}, {"bl": 1}, {"bl": 1, "sz": 12.0}], merges=[(1, 1, 1, 3)]))
    cats = {(x["category"], x["label"], x["kind"]) for x in out["changes"]}
    assert ("formula", "S!E5", "modified") in cats
    assert any(x["category"] == "format" and x["label"] == "S!E5" for x in out["changes"])
    assert ("structure", "S merged range", "removed") in cats and ("structure", "S merged range", "added") in cats
    removed = c.diff_xlsx_indexed(meta(), {"styles": [{}], "sheets": []}, lambda i: Handle(a), lambda i: Handle(b))
    assert ("structure", "Sheet S", "removed") in {(x["category"], x["label"], x["kind"]) for x in removed["changes"]}


def test_summary_counts_cover_everything_even_when_a_page_is_requested():
    a = make_db(500, 10)
    b = make_db(500, 10, edits={(r, 1): f"new{r}" for r in range(1, 501)})
    out = run(a, b)
    assert out["summary"]["total"] == 500 and len(out["changes"]) == 500 and out["complete"]
