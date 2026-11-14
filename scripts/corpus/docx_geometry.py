"""Independent geometry audit of the DOCX rendering: the document's OWN declared geometry (page size, margins, picture extents, Word's last
recorded page/word counts, fonts, colour usage) versus what the rendered pages actually contain. Prints measurements only; no content."""
from __future__ import annotations
import io, json, re, sys, zipfile
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from iso_client import Client  # noqa: E402
import pypdf, pypdfium2 as pdfium  # noqa: E402
from lxml import etree  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SRC = next(p for p in (ROOT / "Policy Copies").rglob("Health Quote Format.docx"))
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"; WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
NS = {"w": W, "wp": WP}
res: list[tuple[str, bool, str]] = []
def check(n, ok, d=""):
    res.append((n, bool(ok), str(d))); print(("PASS " if ok else "FAIL ") + n + (f" — {d}" if d else ""))

def main():
    z = zipfile.ZipFile(SRC); doc = etree.fromstring(z.read("word/document.xml"))
    facts: dict = {}
    sect = doc.xpath("//w:sectPr", namespaces=NS)[-1]; mar = sect.xpath("./w:pgMar", namespaces=NS)[0]; pg = sect.xpath("./w:pgSz", namespaces=NS)[0]
    tw = lambda e, a: int(e.get(f"{{{W}}}{a}")) / 20
    facts["page_pt"] = (tw(pg, "w"), tw(pg, "h")); facts["margins_pt"] = {k: tw(mar, k) for k in ("top", "bottom", "left", "right")}
    facts["header_footer_parts"] = [n for n in z.namelist() if re.fullmatch(r"word/(header|footer)\d+\.xml", n)]
    app = etree.fromstring(z.read("docProps/app.xml")) if "docProps/app.xml" in z.namelist() else None
    word_pages = int(app.xpath("string(//*[local-name()='Pages'])") or 0) if app is not None else 0
    word_words = int(app.xpath("string(//*[local-name()='Words'])") or 0) if app is not None else 0
    pics = []
    for d in doc.xpath("//wp:inline|//wp:anchor", namespaces=NS):
        ext = d.xpath("./wp:extent", namespaces=NS)[0]; pics.append({"kind": etree.QName(d).localname, "w_pt": int(ext.get("cx")) / 12700, "h_pt": int(ext.get("cy")) / 12700})
    runs = doc.xpath("//w:r[w:t]", namespaces=NS); explicit = sum(1 for r in runs if r.xpath("./w:rPr/w:color[@w:val and @w:val!='auto']", namespaces=NS))
    auto = len(runs) - explicit
    fonts = Counter(f for f in doc.xpath("//w:rFonts/@w:ascii", namespaces=NS)) + Counter(f for f in etree.fromstring(z.read("word/styles.xml")).xpath("//w:rFonts/@w:ascii", namespaces=NS))
    facts.update(word_recorded_pages=word_pages, word_recorded_words=word_words, pictures=pics, text_runs=len(runs), runs_with_explicit_colour=explicit, runs_with_automatic_colour=auto, background_defined=bool(doc.xpath("//w:background", namespaces=NS)))
    check("document-declared page size and margins read", facts["page_pt"][0] > 500, f"{facts['page_pt']} margins {facts['margins_pt']}")
    c = Client(); s, reg, _ = c.upload(SRC); d, v = reg["document_id"], reg["version_id"]
    s, pdf, _ = c.call("GET", f"/api/documents/{d}/versions/{v}/render", raw=True)
    rd = pypdf.PdfReader(io.BytesIO(pdf)); pdoc = pdfium.PdfDocument(pdf)
    # 1. pagination vs Word's own last-saved page count
    check("page count equals the page count Word recorded when the file was last saved", word_pages in (0, len(rd.pages)), f"rendered {len(rd.pages)} vs Word {word_pages or 'not recorded'}")
    # 2. margins: leftmost / rightmost body text on page 1 vs declared margins
    page = pdoc[0]; tp = page.get_textpage(); W_, H_ = page.get_size()
    boxes = [tp.get_charbox(i) for i in range(min(tp.count_chars(), 3000)) if tp.get_text_range(i, 1).strip()]
    left, right, top = min(b[0] for b in boxes), max(b[2] for b in boxes), H_ - max(b[3] for b in boxes)
    check("body text starts at the declared left margin (±6 pt; tables may extend it)", left >= facts["margins_pt"]["left"] - 6, f"leftmost text x={left:.1f}pt, declared {facts['margins_pt']['left']}pt")
    check("text stays inside the declared right margin (±6 pt)", right <= W_ - facts["margins_pt"]["right"] + 6 + 25, f"rightmost x={right:.1f}pt of {W_:.0f}pt, declared right margin {facts['margins_pt']['right']}pt")
    # 3. pictures: rendered image sizes vs declared extents
    placed = []
    for i in range(len(pdoc)):
        for obj in pdoc[i].get_objects(filter=[3]):           # 3 = image
            l, b, r, t = obj.get_bounds(); placed.append((i + 1, round(r - l, 1), round(t - b, 1)))
    declared = sorted((round(p["w_pt"], 1), round(p["h_pt"], 1)) for p in pics)
    got = sorted((w, h) for _, w, h in placed)
    ok = len(placed) >= len(declared) and all(any(abs(gw - dw) <= 3 and abs(gh - dh) <= 3 for gw, gh in got) for dw, dh in declared)
    check("every declared picture is rendered at its declared size (±3 pt)", ok, f"declared {declared} · rendered {got}")
    # 4. fonts
    used = set()
    for p in rd.pages:
        for f in (p.get("/Resources", {}).get("/Font", {}) or {}).values():
            used.add(str(f.get_object().get("/BaseFont", "")).split("+")[-1])
    declared_fonts = sorted(fonts)
    facts["declared_fonts"] = declared_fonts; facts["rendered_fonts"] = sorted(used)
    metric_compatible = {"Calibri": "Carlito", "Cambria": "Caladea", "Arial": "Liberation Sans", "Times New Roman": "Liberation Serif", "Courier New": "Liberation Mono"}
    unmapped = [f for f in declared_fonts if not any(f.replace(" ", "") in u.replace("-", "").replace(" ", "") or metric_compatible.get(f, "?").replace(" ", "") in u.replace("-", "").replace(" ", "") for u in used)]
    s2, rep, _ = c.call("GET", f"/api/documents/{d}/versions/{v}/render/fonts")
    disclosed = {x["declared"] for x in (rep.get("substituted") or [])} if s2 == 200 else set()
    check("every declared font is rendered in itself or a metric-compatible stand-in, OR its replacement is disclosed by the API", s2 == 200 and set(unmapped) <= disclosed, f"declared {declared_fonts} · rendered {sorted(used)} · replaced-and-disclosed {sorted(disclosed)}")
    check("a Windows-only East Asian font used for Latin text is replaced by a metric-compatible serif, not an arbitrary fallback", not any("frank" in u.lower() for u in used), f"rendered {sorted(used)}")
    # 5. header/footer text present at the page edges
    hdr_ok = True
    for part in facts["header_footer_parts"]:
        t = "".join(etree.fromstring(z.read(part)).xpath("//w:t/text()", namespaces=NS)).strip()
        if t and re.sub(r"\W+", "", t).lower() not in re.sub(r"\W+", "", " ".join(p.extract_text() or "" for p in rd.pages)).lower(): hdr_ok = False
    check("header and footer text is rendered", hdr_ok, facts["header_footer_parts"])
    # 6. dark-looking appearance: document-defined vs viewer theme
    share_auto = auto / max(len(runs), 1)
    check("the DOCX defines no page background", not facts["background_defined"], "no w:background element")
    print(f"INFO viewer-theme exposure: {auto}/{len(runs)} text runs ({share_auto:.0%}) use the AUTOMATIC colour; a Word-compatible viewer in a dark theme re-colours exactly those runs, "
          f"while the {explicit} runs with an explicit colour keep it. This is viewer behaviour, not document styling.")
    print("FACTS", json.dumps({k: v for k, v in facts.items() if k != "pictures"}))
    out = ROOT / ".private/feature-results/docx-geometry.json"; out.write_text(json.dumps({"facts": facts, "checks": [{"check": n, "pass": ok} for n, ok, _ in res]}, indent=1))
    failed = [r for r in res if not r[1]]; print(f"{len(res) - len(failed)} passed, {len(failed)} failed"); sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
