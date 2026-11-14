"""Branching histories on real workbooks and documents, compared from persisted versions. Counts and statuses only."""
from __future__ import annotations
import hashlib, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from iso_client import Client  # noqa: E402
ROOT = Path(__file__).resolve().parents[2]; CORPUS = ROOT / "Policy Copies"
results = []
def check(n, ok, d=""):
    results.append((n, bool(ok))); print(("PASS " if ok else "FAIL ") + n + (f" — {d}" if d else ""))

def xlsx_branches():
    c = Client(); src = next(p for p in CORPUS.rglob("*") if p.name == "GMC QCR Format.XLSX"); sha = hashlib.sha256(src.read_bytes()).hexdigest()
    s, reg, _ = c.upload(src); d, v1 = reg["document_id"], reg["version_id"]
    cells = [x for r in c.json(f"/api/documents/{d}/versions/{v1}/preview?virtual=true")["sheets"] or [] for x in r] if False else None
    pv = c.json(f"/api/documents/{d}/versions/{v1}/preview")
    text = [x for r in pv["sheets"][0]["rows"] for x in r if x and x["e"] and x["t"] == "string" and x["v"] and len(x["v"]) < 40]
    A, B, C = text[0], text[3], text[6]
    def edit(parent, cell, new):
        parent_cells = {x["id"]: x for r in c.json(f"/api/documents/{d}/versions/{parent}/preview")["sheets"][0]["rows"] for x in r if x}
        cur = parent_cells[cell["id"]]
        s, b, _ = c.post(f"/api/documents/{d}/versions/{parent}/save", {"changes": [{"target_id": cur["id"], "fingerprint": cur["fp"], "value": new}]}); assert s == 200, b
        return b["version_id"]
    v2 = edit(v1, A, A["v"] + " one"); v3 = edit(v1, B, B["v"] + " two"); v4 = edit(v2, C, C["v"] + " three")        # v3 and v4 hang off different parents
    fold = next(f for f in c.json("/api/folders")["items"] if f["document_id"] == d)
    check("xlsx: real lineage is v1 -> v2 -> v4 and v1 -> v3", [x["parent_number"] for x in fold["versions"]] == [None, 1, 1, 2], [x["parent_number"] for x in fold["versions"]])
    def cmp(a, b):
        s, r, _ = c.call("GET", f"/api/documents/{d}/compare?base={a}&target={b}&wait=30"); return s, r
    s, r = cmp(v2, v3); check("xlsx: v2 vs v3 are branches (route 2 -> 1 -> 3) with both edits shown, COMPLETE", s == 200 and r["relation"]["type"] == "branch" and r["relation"]["path_numbers"] == [2, 1, 3] and r["summary"]["total"] == 2 and r["complete"], f"{r['relation']['path_numbers']} {r['summary']['total']}")
    s, r = cmp(v1, v4); check("xlsx: v1 vs v4 is cumulative through v2 (A and C), not B", s == 200 and r["relation"]["path_numbers"] == [1, 2, 4] and r["summary"]["total"] == 2 and {x["location"]["cell"] for x in r["changes"]} == {A["id"].split("!")[-1], C["id"].split("!")[-1]}, r["relation"]["path_numbers"])
    s, r = cmp(v4, v3); check("xlsx: v4 vs v3 (different branches) shows all three edits (A, C vs B)", s == 200 and r["summary"]["total"] == 3 and r["relation"]["type"] == "branch", r["summary"]["total"])
    s, r = cmp(v3, v1); check("xlsx: reverse direction reports the ancestor relation", s == 200 and r["relation"]["type"] == "target_is_ancestor" and r["summary"]["total"] == 1, r["relation"]["type"])
    check("xlsx: v1 byte-identical after all branches", hashlib.sha256(c.file(d, v1)[1]).hexdigest() == sha)

def docx_branches():
    c = Client(); src = CORPUS / "QCR/Health Quote Format.docx"; sha = hashlib.sha256(src.read_bytes()).hexdigest()
    s, reg, _ = c.upload(src); d, v1 = reg["document_id"], reg["version_id"]
    pv = c.json(f"/api/documents/{d}/versions/{v1}/preview")
    paras = [b for b in pv["blocks"] if b["e"] and not b["table"] and 12 < len(b["text"]) < 90][:3]
    def edit(parent, blk, new):
        cur = next(b for b in c.json(f"/api/documents/{d}/versions/{parent}/preview")["blocks"] if b["id"] == blk["id"])
        s, b, _ = c.post(f"/api/documents/{d}/versions/{parent}/save", {"changes": [{"target_id": cur["target_id"], "fingerprint": cur["fp"], "value": new}]}); assert s == 200, b
        return b["version_id"]
    v2 = edit(v1, paras[0], paras[0]["text"] + " alpha"); v3 = edit(v1, paras[1], paras[1]["text"] + " beta"); v4 = edit(v3, paras[2], paras[2]["text"] + " gamma")
    def cmp(a, b): s, r, _ = c.call("GET", f"/api/documents/{d}/compare?base={a}&target={b}"); return s, r
    s, r = cmp(v2, v3); check("docx: v2 vs v3 branch shows both paragraph edits", s == 200 and r["relation"]["type"] == "branch" and r["summary"]["total"] == 2, r["relation"]["path_numbers"])
    s, r = cmp(v1, v4); check("docx: v1 vs v4 cumulative through v3 shows two paragraph edits", s == 200 and r["relation"]["path_numbers"] == [1, 3, 4] and r["summary"]["total"] == 2, r["relation"]["path_numbers"])
    s, r = cmp(v2, v4); check("docx: v2 vs v4 (branch) shows three edits", s == 200 and r["summary"]["total"] == 3, r["summary"]["total"])
    check("docx: every change carries a navigable location", all(x["location"].get("block") for x in r["changes"]), "")
    check("docx: v1 byte-identical after all branches", hashlib.sha256(c.file(d, v1)[1]).hexdigest() == sha)

if __name__ == "__main__":
    xlsx_branches(); docx_branches()
    bad = [n for n, ok in results if not ok]; print(f"\n{len(results) - len(bad)} passed, {len(bad)} failed"); sys.exit(1 if bad else 0)
