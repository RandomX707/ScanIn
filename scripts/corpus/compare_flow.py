"""Live Feature-A check on the ISOLATED stack: real stakeholder files, real edits, real comparisons. Prints counts only."""
from __future__ import annotations
import hashlib, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from iso_client import API, Client, new_tenant, token  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "Policy Copies"
OUT = Path(os.environ.get("CHANNEL360_RESULTS_DIR") or (ROOT / ".private/feature-results"))
results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
    return ok


def cmp(c, d, a, b):
    return c.call("GET", f"/api/documents/{d}/compare?base={a}&target={b}")


def pdf_flow():
    c = Client(); src = CORPUS / "CAR Policy copy.pdf"; sha = hashlib.sha256(src.read_bytes()).hexdigest()
    s, reg, insp = c.upload(src); d, v1 = reg["document_id"], reg["version_id"]
    runs = [r for r in c.json(f"/api/documents/{d}/versions/{v1}/pdf-text?page=1")["runs"] if r["e"] and 14 <= len(r["text"]) <= 40 and r["size"] >= 7]
    def edit(parent, run, new):
        # run ids are version-specific: find the same line (by its text) in the version being edited
        cur = next(x for x in c.json(f"/api/documents/{d}/versions/{parent}/pdf-text?page=1")["runs"] if x["text"] == run["text"] and x["e"])
        s, body, _ = c.post(f"/api/documents/{d}/versions/{parent}/save", {"pdf_edits": [{"id": cur["id"], "old": cur["text"], "new": new}]})
        assert s == 200 and body["outcome"] == "VERIFIED", body
        return body["version_id"]
    new0 = " ".join(runs[0]["text"].split(" ")[:-1]); v2 = edit(v1, runs[0], new0)
    new1 = " ".join(runs[1]["text"].split(" ")[:-1]); v3 = edit(v2, runs[1], new1)          # v3 child of v2
    run5 = runs[5]; v4 = edit(v1, run5, " ".join(run5["text"].split(" ")[:-1]))                # v4 child of v1 (branch)
    s, r, _ = cmp(c, d, v1, v2)
    check("pdf v1->v2 shows exactly the edited line", s == 200 and r["summary"]["modified"] == 1 and r["summary"]["total"] == 1 and r["changes"][0]["page"] == 1
          and r["changes"][0]["target_bbox"] and r["relation"]["type"] == "base_is_ancestor", f"{s} {r.get('summary')}")
    ch = r["changes"][0]; check("pdf change carries before/after and a box", ch["before"] == runs[0]["text"] and ch["after"].strip() == new0.strip(), "")
    check("pdf change reports verification outcome and modification type", r["target"]["outcome"] == "VERIFIED" and r["target"]["modification"] == "pdf_text", r["target"]["modification"])
    check("pdf actor/timestamp come from the audit trail", r["target"]["actor"] == "channel360-demo-owner" and r["target"]["recorded_at"], r["target"]["actor"])
    s, r, _ = cmp(c, d, v2, v3); check("pdf v2->v3 one change", s == 200 and r["summary"]["total"] == 1, r.get("summary"))
    s, r, _ = cmp(c, d, v1, v3); check("pdf v1->v3 cumulative: two changes via v2", s == 200 and r["summary"]["total"] == 2 and r["relation"]["path_numbers"] == [1, 2, 3], f"{r.get('summary')} {r['relation']['path_numbers']}")
    s, r, _ = cmp(c, d, v3, v4); check("pdf v3 vs v4 are branches from v1 (not consecutive ancestry)", s == 200 and r["relation"]["type"] == "branch" and r["relation"]["path_numbers"] == [3, 2, 1, 4], f"{r['relation']['type']} {r['relation']['path_numbers']}")
    s, r, _ = cmp(c, d, v3, v1); check("pdf reverse direction works", s == 200 and r["relation"]["type"] == "target_is_ancestor" and r["summary"]["total"] == 2, r["relation"]["type"])
    s, r, _ = cmp(c, d, v1, v1); check("pdf same version: identical, no changes", s == 200 and r["identical_bytes"] and r["summary"]["total"] == 0, "")
    s, r, _ = c.call("GET", f"/api/documents/{d}/versions/{v4}/changes"); check("changes-from-parent uses real parent (v4 -> v1)", s == 200 and r["base"]["number"] == 1 and r["target"]["number"] == 4, f"{r['base']['number']}->{r['target']['number']}")
    s, r, _ = c.call("GET", f"/api/documents/{d}/versions/{v1}/changes"); check("original has no parent to compare", s == 422, s)
    # sources untouched by comparing
    s, b, _ = c.file(d, v1); check("v1 byte-identical after comparisons", hashlib.sha256(b).hexdigest() == sha, "")
    # authorization
    other = Client(); s, _, _ = other.call("GET", f"/api/documents/{d}/compare?base={v1}&target={v2}"); check("cross-tenant compare is 404", s == 404, s)
    import urllib.request, urllib.error
    def raw(tok):
        req = urllib.request.Request(f"{API}/api/documents/{d}/compare?base={v1}&target={v2}", headers=({"Authorization": f"Bearer {tok}"} if tok else {}))
        try: return urllib.request.urlopen(req).status
        except urllib.error.HTTPError as e: return e.code
    check("compare without a token is 401", raw(None) == 401, raw(None))
    check("compare with a non-owner role is rejected", raw(token(c.tenant, role="VIEWER")) in (401, 403), raw(token(c.tenant, role="VIEWER")))
    s, r, _ = cmp(c, d, v1, "00000000-0000-0000-0000-000000000000"); check("unknown version is 404", s == 404, s)
    s, r, _ = c.call("GET", f"/api/documents/{d}/compare?base=bad&target={v2}"); check("malformed id is 404", s == 404, s)
    return c


def xlsx_flow():
    c = Client()
    for name, formulas in (("CAR-QCR.xlsx", False), ("GMC RFQ.XLSX", True)):
        src = next(p for p in CORPUS.rglob("*") if p.name.lower() == name.lower()); sha = hashlib.sha256(src.read_bytes()).hexdigest()
        s, reg, _ = c.upload(src); d, v1 = reg["document_id"], reg["version_id"]
        pv = c.json(f"/api/documents/{d}/versions/{v1}/preview"); cells = [x for r in pv["sheets"][0]["rows"] for x in r if x]
        blanks = [x for x in cells if x["e"] and x["t"] == "null"]; strings = [x for x in cells if x["e"] and x["t"] == "string" and x["v"] and len(x["v"]) < 50]
        picks = strings[:1] + blanks[:2]
        if formulas:        # edit a real precedent of a formula so the verified result is INCOMPLETE (recalculation not done)
            import re
            fcells = [x for x in cells if str(x["v"]).startswith("=")]
            by_ref = {x["id"].split("!")[-1]: x for x in cells}
            prec = None
            for f in fcells:
                for ref in re.findall(r"\$?([A-Z]{1,2})\$?(\d{1,4})", f["v"]):
                    cand = by_ref.get(ref[0] + ref[1])
                    if cand and cand["e"] and cand["t"] in ("null", "integer", "decimal"):
                        prec = cand; break
                if prec: break
            if prec: picks = [prec]
        changes = [{"target_id": p["id"], "fingerprint": p["fp"], "value": (p["v"] + " x") if p["t"] == "string" else (str(int(float(p["v"])) + 7) if p["t"] in ("integer", "decimal") and p["v"] else "12345" if formulas else ["Acme Test Ltd", "12345"][i % 2])} for i, p in enumerate(picks)]
        s, body, _ = c.post(f"/api/documents/{d}/versions/{v1}/save", {"changes": changes}); assert s == 200, body
        s, r, _ = cmp(c, d, v1, body["version_id"])
        vals = [x for x in r["changes"] if x["category"] == "value"]
        check(f"xlsx {name[:8]}: exactly the {len(changes)} edited cells are reported", s == 200 and len(vals) == len(changes) and {x["location"]["cell"] for x in vals} == {p["id"].split("!")[-1] for p in picks},
              f"{s} value changes {len(vals)} of {len(r['changes'])} total, outcome {body['outcome']}")
        check(f"xlsx {name[:8]}: no spurious format/structure/image changes", all(x["category"] == "value" for x in r["changes"]), {x["category"] for x in r["changes"]})
        check(f"xlsx {name[:8]}: cell references and before/after present", all(x["location"].get("sheet") and x["label"] and "before" in x for x in vals), "")
        if formulas:
            check(f"xlsx {name[:8]}: INCOMPLETE derivative compares but stays withheld from download", body["outcome"] == "INCOMPLETE" and c.file(d, body["version_id"])[0] == 409, c.file(d, body["version_id"])[0])
        s, b, _ = c.file(d, v1); check(f"xlsx {name[:8]}: v1 byte-identical", hashlib.sha256(b).hexdigest() == sha, "")


def docx_flow():
    c = Client(); src = CORPUS / "QCR" / "Health Quote Format.docx"; sha = hashlib.sha256(src.read_bytes()).hexdigest()
    s, reg, _ = c.upload(src); d, v1 = reg["document_id"], reg["version_id"]
    pv = c.json(f"/api/documents/{d}/versions/{v1}/preview")
    ed = [b for b in pv["blocks"] if b["e"] and not b["table"] and 12 < len(b["text"]) < 80][:1]
    cell = [b for b in pv["blocks"] if b["e"] and b["table"] and b["text"].strip()][:1]
    picks = ed + cell
    changes = [{"target_id": b["target_id"], "fingerprint": b["fp"], "value": b["text"] + " (revised)"} for b in picks]
    s, body, _ = c.post(f"/api/documents/{d}/versions/{v1}/save", {"changes": changes}); assert s == 200, body
    s, r, _ = cmp(c, d, v1, body["version_id"])
    check("docx: paragraph and table-cell edits are both reported", s == 200 and r["summary"]["modified"] == len(picks) and any(x["location"].get("table") for x in r["changes"]) and r["format"] == "docx", f"{s} {r.get('summary')}")
    check("docx: table change is addressed by table/row/column", any(x["label"].startswith("Table") for x in r["changes"]), "")
    check("docx: limits are disclosed (images/shapes position not compared)", "images" in " ".join(r["limits"]), "")
    s, b, _ = c.file(d, v1); check("docx: v1 byte-identical", hashlib.sha256(b).hexdigest() == sha, "")
    s, r2, _ = cmp(c, d, v1, v1); check("docx: same version has no changes", r2["summary"]["total"] == 0, "")
    return c, d, v1


if __name__ == "__main__":
    pdf_flow(); xlsx_flow(); docx_flow()
    failed = [r for r in results if not r[1]]
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / "compare.json"
    out.write_text(json.dumps([{"check": n, "pass": ok} for n, ok, _ in results], indent=1))
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)
