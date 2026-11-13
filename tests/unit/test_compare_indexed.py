"""Complete streamed XLSX comparison over windowed indexes: every cell examined, nothing dropped, nothing invented."""
import sqlite3
import tracemalloc

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


def test_full_scan_reports_truncated_change_list_and_keeps_exact_counts(monkeypatch):
    monkeypatch.setattr(c, "MAX_CHANGES", 100)
    monkeypatch.setattr(c, "MAX_CHANGE_BYTES", 20_000)
    edits = {(r, 1): f"edited-{r}" for r in range(1, 501)}
    out = run(make_db(500, 2), make_db(500, 2, edits=edits))
    assert out["scan_status"] == "COMPLETE"
    assert out["list_status"] == "TRUNCATED"
    assert out["complete"] is True
    assert out["has_more"] is True
    assert out["total_changes_detected"] == 500
    assert out["returned_changes"] == len(out["changes"]) == 100
    assert out["summary"] == {"added": 0, "removed": 0, "modified": 500, "total": 500, "truncated": True}
    assert out["retained_change_bytes"] <= 20_000
    assert [change["label"] for change in out["changes"][:3]] == ["S!A1", "S!A2", "S!A3"]


def test_change_retention_memory_is_bounded_during_high_change_scan(monkeypatch):
    monkeypatch.setattr(c, "MAX_CHANGES", 100)
    monkeypatch.setattr(c, "MAX_CHANGE_BYTES", 20_000)
    edits = {(r, 1): f"different-{r}" for r in range(1, 10_001)}
    a, b = make_db(10_000, 1), make_db(10_000, 1, edits=edits)
    tracemalloc.start()
    try:
        out = run(a, b)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert out["scan_status"] == "COMPLETE" and out["total_changes_detected"] == 10_000
    assert out["returned_changes"] == 100 and out["list_status"] == "TRUNCATED"
    assert out["retained_change_bytes"] <= 20_000
    assert peak < 2 * 1024 * 1024


def test_full_scan_handles_worksheets_with_different_sizes_and_sheet_order():
    a1, b1 = make_db(3, 2), make_db(3, 2, edits={(3, 2): "changed"})
    a2, b2 = make_db(1, 1), sqlite3.connect(":memory:")
    b2.execute("CREATE TABLE cells (r INTEGER, c INTEGER, v TEXT, t TEXT, s INTEGER, id TEXT, fp TEXT, e INTEGER, rs TEXT, PRIMARY KEY (r, c)) WITHOUT ROWID")
    b2.execute("INSERT INTO cells VALUES (4,1,'new-row','string',0,'id','fp',1,NULL)")
    base = {"styles": [{}], "sheets": [
        {"index": 0, "name": "First", "merges": [], "images": [], "cols": {}, "rows": {}, "freeze": None, "hidden": False},
        {"index": 1, "name": "Second", "merges": [], "images": [], "cols": {}, "rows": {}, "freeze": None, "hidden": False},
    ]}
    target = {**base, "sheets": [dict(base["sheets"][0]), dict(base["sheets"][1])]}
    out = c.diff_xlsx_indexed(base, target, lambda i: Handle((a1, a2)[i]), lambda i: Handle((b1, b2)[i]))
    assert out["examined_cells"] == 8
    assert [(change["label"], change["kind"]) for change in out["changes"]] == [
        ("First!B3", "modified"), ("Second!A1", "removed"), ("Second!A4", "added")]
