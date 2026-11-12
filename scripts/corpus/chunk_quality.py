#!/usr/bin/env python3
"""Chunk-quality audit for policy PDFs against an independent pdfium keyword scan. Private detail -> .private/evidence/chunk_quality.json.
Console output is masked (digits -> #) and truncated."""
import json, os, re, subprocess, sys, urllib.parse, urllib.request
from pathlib import Path
import pypdfium2 as pdfium
ROOT = Path(__file__).resolve().parents[2]
API = os.environ.get("CHANNEL360_API", "http://127.0.0.1:8010")
OUT = ROOT / ".private/evidence"; OUT.mkdir(parents=True, exist_ok=True)
tok = json.loads(subprocess.run([sys.executable, str(ROOT / "scripts/host/issue_test_token.py")], capture_output=True, text=True, check=True).stdout)
H = {"Authorization": f"Bearer {tok['token']}"}
def call(m, p, b=None):
    r = urllib.request.Request(API + p, data=b, method=m, headers={**H, "Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(r, timeout=600) as x: return json.loads(x.read())
mask = lambda s, n=84: re.sub(r"\d", "#", s)[:n]

CATS = {  # key -> (independent regex, section key(s) expected)
    "exclusions": (r"exclusion|not covered|shall not be liable|not liable|excluded", ["exclusions"]),
    "warranties": (r"warrant", ["warranties"]),
    "conditions": (r"\bconditions?\b", ["conditions"]),
    "sums insured": (r"sum insured|sum assured|limit of liability|limits? of indemnity", ["sum_insured"]),
    "premium and taxes": (r"\bpremium\b|\bgst\b|\bigst\b|\bcgst\b", ["premium"]),
    "policy schedule facts": (r"policy\s*(no|number)|period of insurance|policy period|date of issue", ["schedule"]),
    "insured details": (r"name of (the )?insured|insured'?s? name|address of (the )?insured|proposer", ["insured"]),
    "deductible/excess": (r"deductible|\bexcess\b|co-?pay", ["conditions", "sum_insured", "schedule"]),
    "clauses/add-ons": (r"\bclauses?\b|add-?on|extension|endorsement", ["clauses", "addons"]),
}
report = {}
for fname in sys.argv[1:]:
    path = Path(fname); data = path.read_bytes()
    reg = call("PUT", "/api/documents?filename=" + urllib.parse.quote(path.name), data); d, v = reg["document_id"], reg["version_id"]; call("POST", f"/api/documents/{d}/inspect")
    ch = call("GET", f"/api/documents/{d}/versions/{v}/chunks")["sections"]
    doc = pdfium.PdfDocument(data)
    def visible(i):                       # only text inside the page box (tiled exports carry clipped text from other pages)
        w, h = doc[i].get_size(); return doc[i].get_textpage().get_text_bounded(0, 0, w, h)
    raw_pages = {i + 1: visible(i) for i in range(len(doc))}
    pages = {k: v.lower() for k, v in raw_pages.items()}
    heads = {k: [l.strip().lower() for l in re.split(r"[\r\n]+", v) if 3 <= len(l.strip()) <= 70] for k, v in raw_pages.items()}
    cited = {}
    for s in ch:
        key = s["id"].replace("sec-", ""); cited[key] = set((s.get("source") or {}).get("pages") or [])
    rep = {"sections": [{"title": s["title"], "source": (s["source"] or {}).get("label"), "points": len(s["points"]), "facts": len(s["facts"]), "words": s["words"]} for s in ch], "categories": {}, "weak": [], "facts": []}
    for cat, (rx, keys) in CATS.items():
        mentioned = {p for p, ls in heads.items() if any(re.search(rx, l) for l in ls)}       # heading-level: a short line containing the term
        covered = set().union(*[cited.get(k, set()) for k in keys])
        rep["categories"][cat] = {"pages_mentioning": sorted(mentioned), "pages_cited_by_sections": sorted(covered), "section_present": any(k in cited for k in keys),
                                  "uncited_pages": sorted(mentioned - covered), "page_recall": round(len(mentioned & covered) / len(mentioned), 2) if mentioned else None}
    for s in ch:
        for f in s["facts"]: rep["facts"].append({"section": s["title"], "label": f["label"], "page": f.get("page"), "value": f["value"]})
        for p in s["points"]:
            body = re.sub(r"^.*?\(p\. \d+\):\s*", "", p)
            letters = sum(c.isalpha() for c in body)
            if len(body) < 30 or letters < 0.5 * max(len(body), 1) or body[:1].islower():
                rep["weak"].append({"section": s["title"], "point": p, "why": "very short" if len(body) < 30 else "mostly numbers/table residue" if letters < 0.5 * len(body) else "starts mid-sentence"})
    excerpts = [re.sub(r"^.*?\(p\. \d+\):\s*", "", p) for s in ch for p in s["points"] if "(p. " in p and "):" in p]
    rep["duplicate_excerpts"] = len(excerpts) - len(set(excerpts))
    report[path.name] = rep
    print(f"\n=== {path.name} | {len(ch)} sections: " + ", ".join(f"{s['title']} [{(s['source'] or {}).get('label')}]" for s in ch)[:330])
    print("  facts surfaced:", sorted({(f['label'], f['page']) for f in rep["facts"]})[:14])
    for cat, c in rep["categories"].items():
        print(f"  {cat:24} short lines with the term on {len(c['pages_mentioning']):2} pages | section {'yes' if c['section_present'] else 'NO '} | page recall {c['page_recall']} | uncited: {c['uncited_pages'][:8]}")
    print("  weak takeaways:", len(rep["weak"]), "| duplicate excerpts:", rep["duplicate_excerpts"])
    for w in rep["weak"][:3]: print("    -", w["section"], "|", w["why"], "|", mask(w["point"]))
    for s in ch[:12]:
        print("    •", s["title"][:26].ljust(26), "|", mask(s["points"][0] if s["points"] else "(no takeaway)", 100))
json.dump(report, open(OUT / "chunk_quality.json", "w"), indent=1)
